"""Benchmark: quacklint against a Postgres source.

Reports wall time alongside two server-side counters, so a change shows up as
*work avoided at the source* rather than only as a faster clock:

  tup_read   tuples the table's scans read   (how many rows moved)
  tx_bytes   bytes the container transmitted (how wide those rows were)

Both matter: materializing cuts tuples, pruning cuts width.

Usage: uv run python benchmarks/bench.py [--suite PATH] [--repeat N] [--modes ...]
"""

from __future__ import annotations

import argparse
import subprocess
import time
from pathlib import Path

from quacklint.suite import Suite

SUITE = Path(__file__).parent / "suite_pg.yaml"
TABLE = "bench_orders"
CONTAINER = "quacklint-postgres"


def _psql(sql: str) -> str:
    out = subprocess.run(
        ["docker", "exec", CONTAINER, "psql", "-U", "quacklint",
         "-d", "quacklint_demo", "-tAc", sql],
        capture_output=True, text=True, check=True,
    )
    return out.stdout.strip()


def _stats() -> tuple[int, int]:
    """(seq_scan, seq_tup_read) for the benchmark table."""
    row = _psql(
        "SELECT seq_scan, seq_tup_read FROM pg_stat_user_tables "
        f"WHERE relname = '{TABLE}'"
    )
    scan, tup = row.split("|")
    return int(scan), int(tup)


def _net_bytes() -> int:
    """Bytes transmitted by the Postgres container (dedicated, so all of it is ours).

    tup_read counts rows; this counts width, which is what column pruning buys.
    """
    out = subprocess.run(
        ["docker", "exec", CONTAINER, "cat", "/proc/net/dev"],
        capture_output=True, text=True, check=True,
    ).stdout
    total = 0
    for line in out.splitlines():
        iface, _, rest = line.partition(":")
        if iface.strip() in {"eth0", "ens3"} and rest:
            total += int(rest.split()[8])  # column 9 of /proc/net/dev = TX bytes
    return total


def _drop_caches() -> None:
    """Reset Postgres' view of its own stats between runs."""
    _psql(f"SELECT pg_stat_reset_single_table_counters('{TABLE}'::regclass)")


def _build(suite_path: Path, mode: str) -> Suite:
    """Suite configured for one of the three benchmark modes.

    baseline  pass-through views: every check re-reads the remote table
    a1        materialize once, all columns
    a1b1      materialize once, only the columns the checks reference
    """
    suite = Suite.from_file(suite_path)
    sources = {
        name: spec.model_copy(update={"materialize": mode != "baseline"})
        for name, spec in suite.spec.sources.items()
    }
    suite = Suite(
        spec=suite.spec.model_copy(update={"sources": sources}),
        base_dir=suite.base_dir,
    )
    if mode == "a1":  # keep A1, disable B1's pruning
        object.__setattr__(suite, "required_columns", lambda: dict.fromkeys(sources))
    return suite


def run_once(suite_path: Path, mode: str) -> dict[str, float]:
    _drop_caches()
    before, before_tx = _stats(), _net_bytes()
    start = time.perf_counter()
    results = _build(suite_path, mode).run()
    elapsed = time.perf_counter() - start
    # Stats are flushed asynchronously; give the collector a moment to settle.
    time.sleep(0.7)
    after, after_tx = _stats(), _net_bytes()
    return {
        "seconds": round(elapsed, 2),
        "tup_read": after[1] - before[1],
        "tx_bytes": after_tx - before_tx,
        "checks": len(results),
        "failed": sum(1 for r in results if not r.passed),
        # Signature of every check outcome, so modes can be proven equivalent.
        "fingerprint": hash(
            tuple(sorted((r.check, r.source, r.passed, r.failed_rows) for r in results))
        ),
    }


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--repeat", type=int, default=3)
    parser.add_argument("--suite", type=Path, default=SUITE)
    parser.add_argument("--modes", default="baseline,a1,a1b1")
    args = parser.parse_args()

    report: dict[str, dict[str, float]] = {}
    for mode in args.modes.split(","):
        runs = [run_once(args.suite, mode) for _ in range(args.repeat)]
        report[mode] = min(runs, key=lambda r: r["seconds"])
    print(f"\nsuite: {args.suite.name}  ({int(next(iter(report.values()))['checks'])} "
          f"checks, best of {args.repeat})")
    print(f"{'mode':<10} {'seconds':>9} {'tuples read':>14} {'MB from pg':>12} {'failed':>8}")
    for mode, best in report.items():
        print(
            f"{mode:<10} {best['seconds']:>9} {int(best['tup_read']):>14,} "
            f"{best['tx_bytes'] / 1e6:>12.1f} {int(best['failed']):>8}"
        )
    fingerprints = {best["fingerprint"] for best in report.values()}
    print(f"\nresults identical across modes: {len(fingerprints) == 1}")


if __name__ == "__main__":
    main()
