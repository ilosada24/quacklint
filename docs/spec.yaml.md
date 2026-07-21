# quacklint YAML format contract (`version: 1`)

This document is **the contract** for the suite format. The parser
([suite.py](../src/quacklint/suite.py)) cannot deviate from what is described
here without updating this file, and vice versa.

## Complete example

```yaml
version: 1
sources:
  trips:
    path: data/trips.parquet
checks:
  trips:
    - not_null: [trip_id, pickup_ts]
    - unique: trip_id
    - accepted_values:
        column: payment_type
        values: [card, cash]
    - range: {column: fare, min: 0, max: 1000}
    - freshness: {column: pickup_ts, max_age: 24h}
    - custom_sql:
        name: no_negative_duration
        query: SELECT * FROM trips WHERE dropoff_ts < pickup_ts
```

## Overall structure

The suite file is passed to `quacklint run` / `quacklint validate` as an
argument. If omitted, both commands look for `./quacklint.yaml` in the current
directory.

A suite is a YAML mapping with exactly these keys (any other top-level key is an
error):

| Key       | Required | Type    | Description                                     |
| --------- | -------- | ------- | ----------------------------------------------- |
| `version`  | yes      | integer | Format version. Only `1` is accepted.         |
| `sources`  | yes      | mapping | Data sources. At least one.                   |
| `checks`   | no       | mapping | Checks per source. May be omitted or empty.   |
| `defaults` | no       | mapping | Suite-wide defaults (currently `severity`).   |

## `defaults`

Optional suite-wide defaults applied to any check that does not set the field
itself (an explicit value on a check always wins):

```yaml
defaults:
  severity: warn        # every check defaults to warn unless it sets its own
```

## `sources`

Mapping of **source name** to configuration:

```yaml
sources:
  <name>:
    path: <path>
```

- The name must be an identifier: letters, digits and `_`, not starting with a
  digit. Each source is exposed as a **DuckDB view with that name**, so
  `custom_sql` can reference it directly.
- `path` is the path to the data file, relative to the suite file's directory
  (or absolute). Supported extensions: `.parquet`, `.csv`, `.json`, `.ndjson`.
- `path` may also be a **glob pattern** (`data/*.parquet`, `logs/**/*.json`):
  DuckDB unions all matching files into a single view. The pattern must match at
  least one file (otherwise it's a source error) and all files must share the
  reader implied by the pattern's extension.

### Database sources

Instead of `path`, a source can point at a database that DuckDB attaches. The
source is exposed as a read-only view over the given table, and every check
works against it unchanged.

```yaml
sources:
  customers:
    type: postgres                          # postgres | mysql | sqlite | ...
    connection: "host=db dbname=shop user=ro"
    table: public.customers                 # schema-qualified table to expose
```

| Field        | Required | Description                                                     |
| ------------ | -------- | --------------------------------------------------------------- |
| `type`       | yes      | Backend / DuckDB `ATTACH` type. Built-in: `postgres`, `mysql`, `sqlite`. |
| `connection` | yes      | Connection string / DSN / file path passed to `ATTACH`.         |
| `table`      | yes      | Table to expose (may be `schema.table`).                        |
| `extension`  | no       | DuckDB extension to install/load. Inferred for built-in types.  |
| `read_only`  | no       | Attach read-only (default `true`).                              |
| `materialize`| no       | Copy the table into local DuckDB once (default `true`).         |

- A source is **either** a file (`path`) **or** a database (`type`/`connection`/
  `table`), never both. `materialize` only applies to database sources.
- Built-in types use DuckDB core extensions. Other backends — e.g. **ClickHouse**
  via a community extension — work by naming the extension explicitly:
  `{type: clickhouse, extension: <duckdb-extension>, connection: ..., table: ...}`.
  DuckDB must be able to `INSTALL`/`LOAD` that extension (needs network on first
  use); ClickHouse support is community-provided and not part of DuckDB core.

### How database sources are read

A database source is read **once**, into local DuckDB storage, and every check
then runs against that local copy. Only the columns the checks actually
reference are copied.

This matters because a remote table is not a local file: without it, each check
re-reads the table over the network, so a suite of N checks costs N full reads
of every column. On a 1M-row / 21-column Postgres table, 13 checks move 1.5 GB
as pass-through views versus 88 MB materialized and pruned (see
[`benchmarks/`](../../benchmarks)).

Column pruning is skipped — every column is copied — for any source that is
read by a check whose columns cannot be known in advance:

- **`expected_columns`**, which asserts which columns exist and so must see the
  real schema rather than the subset quacklint chose to load;
- **`custom_sql`**, whose query is arbitrary SQL; any source named in the query
  is copied in full.

Set `materialize: false` to keep the old streaming behaviour: checks read
straight from the remote table, using no local memory and re-reading per check.
Worth it only when the table is far too large to fit locally and the suite has
very few checks.

## `checks`

Mapping of source name (must exist in `sources`) to a **list** of checks. Each
list item is a mapping with **a single key**: the check type.

Common semantics: each check compiles to a DuckDB SQL query that returns the
rows that **violate** the rule. The check **passes** if the query returns 0
rows. Data is never loaded into Python to validate.

Referencing a column that does not exist in the source is a **configuration
error** (exit code 2), not a check failure: it is validated against the real
schema before anything runs. The same applies to a column of the wrong type for
a check that needs a specific one — `range` requires a numeric column,
`freshness` a temporal one (DATE/TIME/TIMESTAMP), and `regex_match` a text
(VARCHAR) column — you get an actionable error naming the column and its actual
type, never a raw engine error.

#### `severity`

Any check accepts an optional `severity` field with values `error` (the
default) or `warn`:

```yaml
- not_null: {columns: [trip_id], severity: warn}
- row_count: {min: 1000, severity: warn}
```

- `error`: a failure counts toward the exit code (`1`).
- `warn`: the failure is **reported** in every format (shown as `WARN` in the
  table, with its `severity` in JSON, and as `system-out` in JUnit) but does
  **not** affect the exit code: if only `warn` checks fail, the CLI exits `0`.
- `--fail-fast` does not stop on a `warn` either: a warning is reported and the
  run continues; it only stops at the first `severity: error` failure.
- `severity` can only be set in the mapping form of a check (not in the
  shorthand forms like `- not_null: trip_id`).

#### tags

Any check accepts an optional `tags` list. Tags let you run a subset of the
suite with `quacklint run --select <tag>` (repeatable); a check runs if it
carries at least one selected tag.

```yaml
- unique: {columns: [trip_id], tags: [critical, pk]}
```

#### tolerance

Row-level checks accept optional tolerance limits so a small number of
violations doesn't fail the check:

```yaml
- not_null: {columns: [email], max_failed_rows: 10}
- accepted_values: {column: status, values: [a, b], max_failed_pct: 0.5}
```

- `max_failed_rows` (integer `>= 0`): pass if at most this many rows violate.
- `max_failed_pct` (number `0`–`100`): pass if at most this percentage of the
  source's rows violate.
- If both are given, the check passes only when **both** limits hold.
- A tolerated pass is still reported (the row shows `PASS` with the violation
  count and a "within tolerance" note; JSON sets `"tolerated": true`).
- Only checks that count violating rows support tolerance. `row_count` and
  `freshness` (single-verdict checks) **reject** it with a configuration error.

### `not_null`

The given columns contain no `NULL`.

```yaml
- not_null: [trip_id, pickup_ts]   # list of columns
- not_null: trip_id                # shorthand: a single column
```

### `unique`

No duplicate values in the column (or combination of columns).

```yaml
- unique: trip_id                  # shorthand: a single column
- unique: [trip_id, pickup_ts]     # composite key
```

Rows with a `NULL` in any of the columns are ignored (use `not_null` to require
non-nulls).

### `row_count`

The source's row count is within the bounds (inclusive).

```yaml
- row_count: {min: 1}
- row_count: {min: 100, max: 100000}
```

- At least one of `min` / `max` is required; both are integers `>= 0`.
- If both are given, `min <= max` must hold.
- Violation: the query returns a single row with the count when it is out of
  bounds.

### `accepted_values`

Every value of the column belongs to the given set.

```yaml
- accepted_values:
    column: payment_type
    values: [card, cash]           # non-empty list of scalars (str/int/float/bool)
```

`NULL`s do not count as a violation (use `not_null` for that).

### `range`

The numeric values of the column are within the bounds (inclusive).

```yaml
- range: {column: fare, min: 0, max: 1000}
```

- At least one of `min` / `max` is required; both are numeric.
- If both are given, `min <= max` must hold.
- `NULL`s do not count as a violation.

### `regex_match`

Every non-null value of the column matches the regular expression.

```yaml
- regex_match: {column: trip_id, pattern: 't-[0-9]{3}'}
```

- The pattern must match the **whole value** (DuckDB's `regexp_full_match`); use
  explicit `.*` for partial searches.
- RE2 syntax (DuckDB's); the pattern is validated when the suite is loaded.
- `NULL`s do not count as a violation.

### `not_empty_string`

The given text columns contain no empty or whitespace-only value.

```yaml
- not_empty_string: email          # shorthand: a single column
- not_empty_string: [name, email]  # multiple columns
```

`NULL`s do not count as a violation (use `not_null` to require presence).

### `string_length`

The character length of the column's non-null values is within the bounds
(inclusive).

```yaml
- string_length: {column: code, min: 3, max: 10}
- string_length: {column: code, min: 1}    # lower bound only
```

- At least one of `min` / `max` is required (integers `>= 0`).
- `NULL`s do not count as a violation.

### `expected_columns`

Schema-drift guard: the source must contain the expected columns.

```yaml
- expected_columns: [trip_id, fare, pickup_ts]              # must be present
- expected_columns: {columns: [trip_id, fare], exact: true} # exactly these
```

- Default: every listed column must exist (extra columns are allowed).
- `exact: true`: the schema must be exactly the listed columns (extra columns
  are violations too).
- Each violation names a `missing` or `unexpected` column.

### `freshness`

The most recent value of the timestamp column is not older than `max_age`
relative to run time.

```yaml
- freshness: {column: pickup_ts, max_age: 24h}
```

- `max_age` is a **duration** (see below). The time reference is DuckDB's
  `now()` at run time.
- The column must be a **temporal** type (DATE/TIME/TIMESTAMP).
- Comparisons run with the session time zone pinned to **UTC** and the column
  cast to `TIMESTAMPTZ`, so the verdict is reproducible regardless of the host
  time zone; a tz-naive column is read as UTC.
- If the source is empty or the column is all `NULL`, the check **passes** (use
  `row_count` and `not_null` to require data).

### `relationship`

Referential integrity across sources: every non-null value of the column must
exist in another source's column.

```yaml
- relationship: {column: customer_id, to: customers, to_column: id}
```

- `to` must be a **declared source**; it is validated at parse time.
- `to_column` must exist in that source (validated against its schema).
- `NULL`s on either side do not count as a violation (use `not_null` to require
  presence).

### `custom_sql`

Arbitrary SQL query whose result rows are the violations.

```yaml
- custom_sql:
    name: no_negative_duration     # identifier, unique within the suite
    query: SELECT * FROM trips WHERE dropoff_ts < pickup_ts
```

- The query can reference any declared source by its view name.
- In reports, the check is reported under its `name` (not as `custom_sql`).
- A trailing `;` is stripped automatically; the query must be a single
  `SELECT`.

## Durations

Format: `<integer><unit>`, no spaces. Units:

| Unit | Meaning |
| ---- | ------- |
| `s`  | seconds |
| `m`  | minutes |
| `h`  | hours   |
| `d`  | days    |

Valid examples: `30s`, `15m`, `24h`, `7d`. Invalid: `24 h`, `1w`, `1.5h`.

## Errors and exit codes

- Every invalid suite produces an actionable message with the location of the
  problem, e.g. `checks.trips[3] (range): needs at least 'min' or 'max'`. Never
  a traceback.
- CLI exit codes: `0` all checks pass, `1` there are failed checks, `2`
  configuration error (invalid suite, missing source...).

## Format evolution

`version` is only incremented for incompatible changes. The parser rejects any
version other than `1` with an explicit message.
