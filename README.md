# quacklint 🦆

**Declarative data quality for DuckDB.** Describe your rules in a YAML;
quacklint compiles them to SQL and runs them directly over your Parquet/CSV/JSON
files. No pandas, no services, built for CI.

## The problem

Data quality checks often end up as ad hoc pandas scripts: slow, imperative,
hard to review in a PR and coupled to whoever wrote them. quacklint bets on the
opposite:

- **Declarative** — the YAML suite says *what* must hold, not *how* to check it,
  and it's reviewed in a PR just like code.
- **SQL first** — each check compiles to a DuckDB query that returns the rows
  that violate the rule (0 rows = pass). Data is never loaded into Python;
  DuckDB reads Parquet/CSV/JSON directly and in parallel.
- **Built for CI** — stable exit codes, `table`/`json`/`junit` reports, and
  actionable configuration errors (never tracebacks).

More on the why in [docs/philosophy.md](docs/philosophy.md).

## Installation

Requires Python 3.11+ and [uv](https://docs.astral.sh/uv/).

**Global (the `quacklint` command available in any folder):**

```console
$ uv tool install --editable /path/to/repo/quacklint
$ quacklint --help
```

- `--editable` makes changes to the repo's code take effect without
  reinstalling (recommended while the project is in active development); drop it
  if you prefer a frozen copy.
- If `uv` warns that `~/.local/bin` is not on your PATH: `uv tool update-shell`.
- To upgrade or uninstall: `uv tool upgrade quacklint` /
  `uv tool uninstall quacklint`.

**For development** (repo environment, without installing anything globally): see
[Development](#development).

## 30-second quickstart

A CSV with problems:

```csv
# trips.csv
trip_id,payment_type,fare
t-001,card,12.5
t-002,cash,9.0
t-002,voucher,-3.0
```

A suite that declares the rules:

```yaml
# suite.yaml
version: 1
sources:
  trips:
    path: trips.csv
checks:
  trips:
    - not_null: [trip_id, fare]
    - unique: trip_id
    - accepted_values: {column: payment_type, values: [card, cash]}
    - range: {column: fare, min: 0}
```

And run it:

```console
$ quacklint run suite.yaml
┏━━━━━━━━┳━━━━━━━━┳━━━━━━━━━━━━━━━━━┳━━━━━━┳━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━┓
┃ status ┃ source ┃ check           ┃ rows ┃ detail                            ┃
┡━━━━━━━━╇━━━━━━━━╇━━━━━━━━━━━━━━━━━╇━━━━━━╇━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━┩
│ PASS   │ trips  │ not_null        │    0 │                                   │
│ FAIL   │ trips  │ unique          │    1 │ 1 row(s) violate the rule         │
│        │        │                 │      │ sample: trip_id=t-002,            │
│        │        │                 │      │ occurrences=2                     │
│ FAIL   │ trips  │ accepted_values │    1 │ 1 row(s) violate the rule         │
│        │        │                 │      │ sample: trip_id=t-002,            │
│        │        │                 │      │ payment_type=voucher, fare=-3.0   │
│ FAIL   │ trips  │ range           │    1 │ 1 row(s) violate the rule         │
│        │        │                 │      │ sample: trip_id=t-002,            │
│        │        │                 │      │ payment_type=voucher, fare=-3.0   │
└────────┴────────┴─────────────────┴──────┴───────────────────────────────────┘
1/4 checks OK
$ echo $?
1
```

There's a complete, ready-to-run example in [examples/taxi](examples/taxi).

## Available checks

| Check              | Rule                                                               |
| ------------------ | ------------------------------------------------------------------ |
| `not_null`         | The columns contain no `NULL`.                                     |
| `unique`           | No duplicates in the column or combination (`NULL`s are ignored).  |
| `row_count`        | Row count within `[min, max]` (inclusive).                        |
| `accepted_values`  | Every non-null value belongs to a given set.                      |
| `range`            | Non-null numeric values within `[min, max]` (inclusive).          |
| `regex_match`      | Non-null values fully match an RE2 regular expression.            |
| `not_empty_string` | Text columns contain no empty/whitespace-only value.              |
| `string_length`    | Non-null string length within `[min, max]`.                       |
| `expected_columns` | The source's schema contains the expected columns.               |
| `freshness`        | The most recent value of a timestamp column is not older than an age. |
| `relationship`     | Column values exist in another source's column (foreign key).    |
| `custom_sql`       | Arbitrary SQL whose result rows are violations.                  |

Any check can also set `severity: warn` (report without failing), a `tolerance`
(`max_failed_rows` / `max_failed_pct`), and `tags` (for `--select`). Sources can
be files (Parquet/CSV/JSON, incl. globs) or databases attached via DuckDB
(`type: postgres | mysql | sqlite | …`).

Syntax and generated SQL for each:
[docs/checks-reference.md](docs/checks-reference.md). The full YAML format
contract: [docs/spec.yaml.md](docs/spec.yaml.md).

## CLI

```console
$ quacklint validate suite.yaml          # validate without touching the data
$ quacklint run suite.yaml               # rich table with a sample of failing rows
$ quacklint run suite.yaml -f json       # JSON report
$ quacklint run suite.yaml -f junit      # JUnit XML for CI
$ quacklint run suite.yaml --explain     # print the compiled SQL, without running
$ quacklint run suite.yaml --fail-fast   # stop at the first error (warnings don't stop)
$ quacklint run suite.yaml --select critical  # only run checks tagged 'critical'
$ quacklint run                          # no argument: uses ./quacklint.yaml
```

The suite argument is optional: if omitted, `run` and `validate` look for
`./quacklint.yaml` in the current directory.

Exit codes:

| Code | Meaning                                            |
| ---- | -------------------------------------------------- |
| 0    | All checks pass                                    |
| 1    | There are failed checks                            |
| 2    | Configuration error (invalid suite, sources…)      |

GitHub Actions integration examples:
[docs/ci-integration.md](docs/ci-integration.md).

## Development

```console
$ uv sync                                              # deps (including the dev group)
$ uv run pytest                                        # tests
$ uv run ruff check                                    # lint
$ uv run mypy                                          # types (strict)
$ uv run python scripts/gen_checks_reference.py        # regenerate docs/checks-reference.md
```

`docs/checks-reference.md` is generated from the docstrings in
[builtin.py](src/quacklint/checks/builtin.py); a test fails if it goes out of
date.

## Publishing to PyPI (future steps)

The package already builds correctly (`uv build` produces a wheel and sdist).
To publish it, what remains is:

1. Complete the metadata in `pyproject.toml`: `license = "MIT"` + a `LICENSE`
   file, `authors`, `keywords` and `classifiers`.
2. Optional dry run on TestPyPI:
   `uv publish --publish-url https://test.pypi.org/legacy/ --token ...`.
3. An account on [pypi.org](https://pypi.org) + API token, and publish:
   `uv publish --token pypi-XXXX`.
4. From then on installation becomes `uv tool install quacklint` (the name is
   free on PyPI, verified 2026-07-19).
