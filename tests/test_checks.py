"""Tests for the implemented checks (not_null, unique, row_count).

The data is synthetic Parquet generated with duckdb and materialized as views
via `sources.create_views`, just like in a real run.
"""

from __future__ import annotations

from collections.abc import Callable, Iterator
from datetime import timedelta
from pathlib import Path
from textwrap import dedent

import duckdb
import pytest

import quacklint.checks.builtin  # noqa: F401  (registers the built-in checks)
from quacklint.checks.base import build_check, get_check
from quacklint.errors import ExecutionError, SourceError, SpecError
from quacklint.sources import create_views
from quacklint.suite import (
    AcceptedValuesSpec,
    CustomSqlSpec,
    FreshnessSpec,
    NotNullSpec,
    RangeSpec,
    RegexMatchSpec,
    RowCountSpec,
    SourceSpec,
    Suite,
    UniqueSpec,
)

MakeParquet = Callable[[str, str], Path]


@pytest.fixture()
def conn() -> Iterator[duckdb.DuckDBPyConnection]:
    connection = duckdb.connect()
    yield connection
    connection.close()


@pytest.fixture()
def make_parquet(tmp_path: Path, conn: duckdb.DuckDBPyConnection) -> MakeParquet:
    """Generate a synthetic Parquet from a query and expose it as a view."""

    def _make(name: str, query: str) -> Path:
        path = tmp_path / f"{name}.parquet"
        conn.execute(f"COPY ({query}) TO '{path.as_posix()}' (FORMAT parquet)")
        create_views(conn, {name: SourceSpec(path=path.name)}, tmp_path)
        return path

    return _make


# ---------------------------------------------------------------------------
# not_null
# ---------------------------------------------------------------------------


def test_not_null_passes_without_nulls(
    conn: duckdb.DuckDBPyConnection, make_parquet: MakeParquet
) -> None:
    make_parquet("t", "SELECT * FROM (VALUES (1, 'a'), (2, 'b')) AS v(id, name)")
    result = build_check("t", NotNullSpec(columns=["id", "name"])).evaluate(conn)
    assert result.passed
    assert result.failed_rows == 0


def test_not_null_counts_rows_with_nulls(
    conn: duckdb.DuckDBPyConnection, make_parquet: MakeParquet
) -> None:
    make_parquet("t", "SELECT * FROM (VALUES (1, 'a'), (NULL, 'b'), (2, NULL)) AS v(id, name)")
    result = build_check("t", NotNullSpec(columns=["id", "name"])).evaluate(conn)
    assert not result.passed
    assert result.failed_rows == 2
    assert result.check == "not_null"
    assert result.source == "t"


def test_not_null_only_checks_declared_columns(
    conn: duckdb.DuckDBPyConnection, make_parquet: MakeParquet
) -> None:
    make_parquet("t", "SELECT * FROM (VALUES (1, NULL), (2, NULL)) AS v(id, name)")
    result = build_check("t", NotNullSpec(columns=["id"])).evaluate(conn)
    assert result.passed


# ---------------------------------------------------------------------------
# unique
# ---------------------------------------------------------------------------


def test_unique_passes_without_duplicates(
    conn: duckdb.DuckDBPyConnection, make_parquet: MakeParquet
) -> None:
    make_parquet("t", "SELECT * FROM (VALUES (1), (2), (3)) AS v(id)")
    result = build_check("t", UniqueSpec(columns=["id"])).evaluate(conn)
    assert result.passed


def test_unique_counts_duplicated_keys(
    conn: duckdb.DuckDBPyConnection, make_parquet: MakeParquet
) -> None:
    make_parquet("t", "SELECT * FROM (VALUES (1), (1), (2), (2), (2), (3)) AS v(id)")
    result = build_check("t", UniqueSpec(columns=["id"])).evaluate(conn)
    assert not result.passed
    assert result.failed_rows == 2  # two duplicated keys: 1 and 2


def test_unique_ignores_nulls(
    conn: duckdb.DuckDBPyConnection, make_parquet: MakeParquet
) -> None:
    make_parquet("t", "SELECT * FROM (VALUES (NULL), (NULL), (1)) AS v(id)")
    result = build_check("t", UniqueSpec(columns=["id"])).evaluate(conn)
    assert result.passed


def test_unique_composite_key(
    conn: duckdb.DuckDBPyConnection, make_parquet: MakeParquet
) -> None:
    make_parquet(
        "t",
        "SELECT * FROM (VALUES (1, 'a'), (1, 'b'), (1, 'a')) AS v(id, side)",
    )
    result = build_check("t", UniqueSpec(columns=["id", "side"])).evaluate(conn)
    assert not result.passed
    assert result.failed_rows == 1  # only (1, 'a') is duplicated


# ---------------------------------------------------------------------------
# row_count
# ---------------------------------------------------------------------------


def test_row_count_within_bounds(
    conn: duckdb.DuckDBPyConnection, make_parquet: MakeParquet
) -> None:
    make_parquet("t", "SELECT * FROM (VALUES (1), (2), (3)) AS v(id)")
    result = build_check("t", RowCountSpec(min=1, max=10)).evaluate(conn)
    assert result.passed
    assert result.failed_rows == 0


def test_row_count_below_min(
    conn: duckdb.DuckDBPyConnection, make_parquet: MakeParquet
) -> None:
    make_parquet("t", "SELECT * FROM (VALUES (1), (2), (3)) AS v(id)")
    result = build_check("t", RowCountSpec(min=5)).evaluate(conn)
    assert not result.passed
    assert "3 row(s)" in result.message
    assert "at least 5" in result.message


def test_row_count_above_max(
    conn: duckdb.DuckDBPyConnection, make_parquet: MakeParquet
) -> None:
    make_parquet("t", "SELECT * FROM (VALUES (1), (2), (3)) AS v(id)")
    result = build_check("t", RowCountSpec(max=2)).evaluate(conn)
    assert not result.passed
    assert "at most 2" in result.message


# ---------------------------------------------------------------------------
# accepted_values
# ---------------------------------------------------------------------------


def test_accepted_values_passes(
    conn: duckdb.DuckDBPyConnection, make_parquet: MakeParquet
) -> None:
    make_parquet("t", "SELECT * FROM (VALUES ('card'), ('cash')) AS v(payment)")
    spec = AcceptedValuesSpec(column="payment", values=["card", "cash"])
    result = build_check("t", spec).evaluate(conn)
    assert result.passed


def test_accepted_values_counts_unexpected_values(
    conn: duckdb.DuckDBPyConnection, make_parquet: MakeParquet
) -> None:
    make_parquet("t", "SELECT * FROM (VALUES ('card'), ('voucher'), ('voucher')) AS v(payment)")
    spec = AcceptedValuesSpec(column="payment", values=["card", "cash"])
    result = build_check("t", spec).evaluate(conn)
    assert not result.passed
    assert result.failed_rows == 2


def test_accepted_values_ignores_nulls(
    conn: duckdb.DuckDBPyConnection, make_parquet: MakeParquet
) -> None:
    make_parquet("t", "SELECT * FROM (VALUES ('card'), (NULL)) AS v(payment)")
    spec = AcceptedValuesSpec(column="payment", values=["card"])
    result = build_check("t", spec).evaluate(conn)
    assert result.passed


def test_accepted_values_numeric_values(
    conn: duckdb.DuckDBPyConnection, make_parquet: MakeParquet
) -> None:
    make_parquet("t", "SELECT * FROM (VALUES (1), (2), (3)) AS v(status)")
    spec = AcceptedValuesSpec(column="status", values=[1, 2])
    result = build_check("t", spec).evaluate(conn)
    assert not result.passed
    assert result.failed_rows == 1


def test_accepted_values_escapes_quotes_in_literals(
    conn: duckdb.DuckDBPyConnection, make_parquet: MakeParquet
) -> None:
    make_parquet("t", "SELECT * FROM (VALUES ('x')) AS v(name)")
    spec = AcceptedValuesSpec(column="name", values=["o'brien"])
    result = build_check("t", spec).evaluate(conn)
    assert not result.passed
    assert result.failed_rows == 1


# ---------------------------------------------------------------------------
# range
# ---------------------------------------------------------------------------


def test_range_passes_inclusive_bounds(
    conn: duckdb.DuckDBPyConnection, make_parquet: MakeParquet
) -> None:
    make_parquet("t", "SELECT * FROM (VALUES (0), (5), (10)) AS v(fare)")
    result = build_check("t", RangeSpec(column="fare", min=0, max=10)).evaluate(conn)
    assert result.passed


def test_range_counts_out_of_bounds(
    conn: duckdb.DuckDBPyConnection, make_parquet: MakeParquet
) -> None:
    make_parquet("t", "SELECT * FROM (VALUES (-1), (5), (2000)) AS v(fare)")
    result = build_check("t", RangeSpec(column="fare", min=0, max=1000)).evaluate(conn)
    assert not result.passed
    assert result.failed_rows == 2


def test_range_ignores_nulls(
    conn: duckdb.DuckDBPyConnection, make_parquet: MakeParquet
) -> None:
    make_parquet("t", "SELECT * FROM (VALUES (5), (NULL)) AS v(fare)")
    result = build_check("t", RangeSpec(column="fare", min=0, max=10)).evaluate(conn)
    assert result.passed


def test_range_min_only(
    conn: duckdb.DuckDBPyConnection, make_parquet: MakeParquet
) -> None:
    make_parquet("t", "SELECT * FROM (VALUES (-1), (999999)) AS v(fare)")
    result = build_check("t", RangeSpec(column="fare", min=0)).evaluate(conn)
    assert not result.passed
    assert result.failed_rows == 1  # only the -1; no upper bound


# ---------------------------------------------------------------------------
# regex_match
# ---------------------------------------------------------------------------


def test_regex_match_passes(
    conn: duckdb.DuckDBPyConnection, make_parquet: MakeParquet
) -> None:
    make_parquet("t", "SELECT * FROM (VALUES ('t-001'), ('t-002')) AS v(trip_id)")
    spec = RegexMatchSpec(column="trip_id", pattern=r"t-[0-9]{3}")
    result = build_check("t", spec).evaluate(conn)
    assert result.passed


def test_regex_match_counts_mismatches(
    conn: duckdb.DuckDBPyConnection, make_parquet: MakeParquet
) -> None:
    make_parquet("t", "SELECT * FROM (VALUES ('t-001'), ('bogus'), ('???')) AS v(trip_id)")
    spec = RegexMatchSpec(column="trip_id", pattern=r"t-[0-9]{3}")
    result = build_check("t", spec).evaluate(conn)
    assert not result.passed
    assert result.failed_rows == 2


def test_regex_match_requires_full_match(
    conn: duckdb.DuckDBPyConnection, make_parquet: MakeParquet
) -> None:
    make_parquet("t", "SELECT * FROM (VALUES ('xt-001y')) AS v(trip_id)")
    spec = RegexMatchSpec(column="trip_id", pattern=r"t-[0-9]{3}")
    result = build_check("t", spec).evaluate(conn)
    assert not result.passed  # would match partially, but we require a full match


def test_regex_match_ignores_nulls(
    conn: duckdb.DuckDBPyConnection, make_parquet: MakeParquet
) -> None:
    make_parquet("t", "SELECT * FROM (VALUES ('t-001'), (NULL)) AS v(trip_id)")
    spec = RegexMatchSpec(column="trip_id", pattern=r"t-[0-9]{3}")
    result = build_check("t", spec).evaluate(conn)
    assert result.passed


# ---------------------------------------------------------------------------
# freshness
# ---------------------------------------------------------------------------


def test_freshness_passes_with_recent_data(
    conn: duckdb.DuckDBPyConnection, make_parquet: MakeParquet
) -> None:
    make_parquet("t", "SELECT now() - INTERVAL 1 HOUR AS ts")
    spec = FreshnessSpec(column="ts", max_age=timedelta(hours=24))
    result = build_check("t", spec).evaluate(conn)
    assert result.passed


def test_freshness_fails_with_stale_data(
    conn: duckdb.DuckDBPyConnection, make_parquet: MakeParquet
) -> None:
    make_parquet("t", "SELECT now() - INTERVAL 3 DAY AS ts")
    spec = FreshnessSpec(column="ts", max_age=timedelta(hours=24))
    result = build_check("t", spec).evaluate(conn)
    assert not result.passed
    assert result.failed_rows == 1
    assert "max age (1d)" in result.message  # 24h normalizes to the largest unit
    assert "ts" in result.message


def test_freshness_empty_source_passes(
    conn: duckdb.DuckDBPyConnection, make_parquet: MakeParquet
) -> None:
    make_parquet("t", "SELECT now() AS ts WHERE false")
    spec = FreshnessSpec(column="ts", max_age=timedelta(hours=1))
    result = build_check("t", spec).evaluate(conn)
    assert result.passed


def test_freshness_all_null_column_passes(
    conn: duckdb.DuckDBPyConnection, make_parquet: MakeParquet
) -> None:
    make_parquet("t", "SELECT CAST(NULL AS TIMESTAMP) AS ts")
    spec = FreshnessSpec(column="ts", max_age=timedelta(hours=1))
    result = build_check("t", spec).evaluate(conn)
    assert result.passed


def test_freshness_handles_tz_naive_timestamp_column(
    conn: duckdb.DuckDBPyConnection, make_parquet: MakeParquet
) -> None:
    # A tz-naive TIMESTAMP column must not error and must give the right verdict
    # (regression for the TIMESTAMP vs TIMESTAMPTZ comparison).
    conn.execute("SET TimeZone='UTC'")
    make_parquet("recent", "SELECT CAST(now() - INTERVAL 1 HOUR AS TIMESTAMP) AS ts")
    make_parquet("stale", "SELECT CAST(now() - INTERVAL 3 DAY AS TIMESTAMP) AS ts")
    spec = FreshnessSpec(column="ts", max_age=timedelta(hours=24))
    assert build_check("recent", spec).evaluate(conn).passed
    assert not build_check("stale", spec).evaluate(conn).passed


def test_suite_run_pins_utc_timezone(tmp_path: Path) -> None:
    # Suite.run must pin the session tz to UTC; a custom_sql that flags a
    # non-UTC session should therefore find no violations.
    _write_trips_parquet(tmp_path / "trips.parquet")
    (tmp_path / "suite.yaml").write_text(
        dedent(
            """\
            version: 1
            sources:
              trips:
                path: trips.parquet
            checks:
              trips:
                - custom_sql:
                    name: session_is_utc
                    query: SELECT 1 WHERE current_setting('TimeZone') <> 'UTC'
            """
        ),
        encoding="utf-8",
    )
    results = Suite.from_file(tmp_path / "suite.yaml").run()
    assert results[0].passed


# ---------------------------------------------------------------------------
# custom_sql
# ---------------------------------------------------------------------------


def test_custom_sql_reports_custom_name_and_failures(
    conn: duckdb.DuckDBPyConnection, make_parquet: MakeParquet
) -> None:
    make_parquet(
        "trips",
        "SELECT * FROM (VALUES "
        "(TIMESTAMP '2026-07-18 12:00:00', TIMESTAMP '2026-07-18 11:00:00'), "
        "(TIMESTAMP '2026-07-18 08:00:00', TIMESTAMP '2026-07-18 09:00:00')"
        ") AS v(pickup_ts, dropoff_ts)",
    )
    spec = CustomSqlSpec(
        name="no_negative_duration",
        query="SELECT * FROM trips WHERE dropoff_ts < pickup_ts",
    )
    result = build_check("trips", spec).evaluate(conn)
    assert not result.passed
    assert result.failed_rows == 1
    assert result.check == "no_negative_duration"  # reports its name, not 'custom_sql'


def test_custom_sql_passes_without_violations(
    conn: duckdb.DuckDBPyConnection, make_parquet: MakeParquet
) -> None:
    make_parquet("t", "SELECT 1 AS n")
    spec = CustomSqlSpec(name="never_fails", query="SELECT * FROM t WHERE n < 0")
    result = build_check("t", spec).evaluate(conn)
    assert result.passed


def test_custom_sql_handles_trailing_semicolon(
    conn: duckdb.DuckDBPyConnection, make_parquet: MakeParquet
) -> None:
    make_parquet("t", "SELECT 1 AS n")
    spec = CustomSqlSpec(name="with_semicolon", query="SELECT * FROM t WHERE n < 0;")
    result = build_check("t", spec).evaluate(conn)
    assert result.passed


# ---------------------------------------------------------------------------
# nonexistent columns → configuration error, not a check failure
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "check_line",
    [
        "- not_null: missing_col",
        "- unique: missing_col",
        "- accepted_values: {column: missing_col, values: [a]}",
        "- range: {column: missing_col, min: 0}",
        "- regex_match: {column: missing_col, pattern: x}",
        "- freshness: {column: missing_col, max_age: 24h}",
    ],
)
def test_missing_column_is_config_error(tmp_path: Path, check_line: str) -> None:
    _write_trips_parquet(tmp_path / "trips.parquet")
    (tmp_path / "suite.yaml").write_text(
        dedent(
            f"""\
            version: 1
            sources:
              trips:
                path: trips.parquet
            checks:
              trips:
                {check_line}
            """
        ),
        encoding="utf-8",
    )
    with pytest.raises(SpecError, match=r"missing_col.*Available columns: trip_id, n"):
        Suite.from_file(tmp_path / "suite.yaml").run()


# ---------------------------------------------------------------------------
# wrong column type → configuration error, not a cryptic DuckDB error
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("check_line", "expected"),
    [
        # trip_id is VARCHAR, n is INTEGER
        ("- range: {column: trip_id, min: 0}", r"trip_id.*type VARCHAR.*requires a numeric"),
        ("- freshness: {column: n, max_age: 24h}", r"'n'.*type INTEGER.*requires a temporal"),
        ("- regex_match: {column: n, pattern: x}", r"'n'.*type INTEGER.*requires a text"),
    ],
)
def test_wrong_column_type_is_config_error(
    tmp_path: Path, check_line: str, expected: str
) -> None:
    _write_trips_parquet(tmp_path / "trips.parquet")
    (tmp_path / "suite.yaml").write_text(
        dedent(
            f"""\
            version: 1
            sources:
              trips:
                path: trips.parquet
            checks:
              trips:
                {check_line}
            """
        ),
        encoding="utf-8",
    )
    with pytest.raises(SpecError, match=expected):
        Suite.from_file(tmp_path / "suite.yaml").run()


def test_sample_rows_are_deterministic(
    conn: duckdb.DuckDBPyConnection, make_parquet: MakeParquet
) -> None:
    make_parquet(
        "t", "SELECT * FROM (VALUES (3, NULL), (1, NULL), (2, NULL)) AS v(id, name)"
    )
    result = build_check("t", NotNullSpec(columns=["name"])).evaluate(conn)
    assert not result.passed
    # ORDER BY ALL sorts the sampled violations; ids come back 1, 2, 3.
    ids = [row[result.sample_columns.index("id")] for row in result.sample_rows]
    assert ids == ["1", "2", "3"]


# ---------------------------------------------------------------------------
# sources.create_views
# ---------------------------------------------------------------------------


def test_create_views_reads_csv(conn: duckdb.DuckDBPyConnection, tmp_path: Path) -> None:
    (tmp_path / "t.csv").write_text("id,name\n1,a\n2,b\n", encoding="utf-8")
    create_views(conn, {"t": SourceSpec(path="t.csv")}, tmp_path)
    row = conn.execute('SELECT count(*) FROM "t"').fetchone()
    assert row is not None
    assert row[0] == 2


def test_create_views_missing_file(conn: duckdb.DuckDBPyConnection, tmp_path: Path) -> None:
    with pytest.raises(SourceError, match="does not exist"):
        create_views(conn, {"t": SourceSpec(path="nope.parquet")}, tmp_path)


def test_create_views_unsupported_extension(
    conn: duckdb.DuckDBPyConnection, tmp_path: Path
) -> None:
    (tmp_path / "t.xlsx").write_text("x", encoding="utf-8")
    with pytest.raises(SourceError, match="unsupported extension"):
        create_views(conn, {"t": SourceSpec(path="t.xlsx")}, tmp_path)


def test_create_views_glob_unions_matching_files(
    conn: duckdb.DuckDBPyConnection, tmp_path: Path
) -> None:
    for letter, value in (("a", 1), ("b", 2)):
        path = (tmp_path / f"{letter}.parquet").as_posix()
        conn.execute(f"COPY (SELECT {value} AS id) TO '{path}' (FORMAT parquet)")
    create_views(conn, {"t": SourceSpec(path="*.parquet")}, tmp_path)
    rows = conn.execute('SELECT count(*) FROM "t"').fetchone()
    assert rows is not None
    assert rows[0] == 2


def test_create_views_glob_no_match(conn: duckdb.DuckDBPyConnection, tmp_path: Path) -> None:
    with pytest.raises(SourceError, match="matches no files"):
        create_views(conn, {"t": SourceSpec(path="*.parquet")}, tmp_path)


# ---------------------------------------------------------------------------
# Suite.run end-to-end
# ---------------------------------------------------------------------------


def _write_trips_parquet(path: Path) -> None:
    connection = duckdb.connect()
    try:
        connection.execute(
            f"""
            COPY (
                SELECT * FROM (VALUES ('a', 1), ('a', 2), (NULL, 3)) AS v(trip_id, n)
            ) TO '{path.as_posix()}' (FORMAT parquet)
            """
        )
    finally:
        connection.close()


def test_suite_run_end_to_end(tmp_path: Path) -> None:
    _write_trips_parquet(tmp_path / "trips.parquet")
    (tmp_path / "suite.yaml").write_text(
        dedent(
            """\
            version: 1
            sources:
              trips:
                path: trips.parquet
            checks:
              trips:
                - not_null: trip_id
                - unique: trip_id
                - row_count: {min: 1, max: 100}
            """
        ),
        encoding="utf-8",
    )

    results = Suite.from_file(tmp_path / "suite.yaml").run()

    assert [r.source for r in results] == ["trips", "trips", "trips"]
    by_check = {r.check: r for r in results}
    assert not by_check["not_null"].passed
    assert by_check["not_null"].failed_rows == 1  # the NULL trip_id
    assert not by_check["unique"].passed
    assert by_check["unique"].failed_rows == 1  # 'a' duplicated (the NULL is ignored)
    assert by_check["row_count"].passed


def test_get_check_unknown_name_raises_clear_error() -> None:
    with pytest.raises(ExecutionError, match="has no runnable implementation"):
        get_check("nope")


def test_suite_run_missing_source_file(tmp_path: Path) -> None:
    (tmp_path / "suite.yaml").write_text(
        dedent(
            """\
            version: 1
            sources:
              trips:
                path: nope.parquet
            """
        ),
        encoding="utf-8",
    )
    with pytest.raises(SourceError, match="does not exist"):
        Suite.from_file(tmp_path / "suite.yaml").run()
