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
| `version` | yes      | integer | Format version. Only `1` is accepted.          |
| `sources` | yes      | mapping | Data sources. At least one.                    |
| `checks`  | no       | mapping | Checks per source. May be omitted or empty.    |

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

## `checks`

Mapping of source name (must exist in `sources`) to a **list** of checks. Each
list item is a mapping with **a single key**: the check type.

Common semantics: each check compiles to a DuckDB SQL query that returns the
rows that **violate** the rule. The check **passes** if the query returns 0
rows. Data is never loaded into Python to validate.

Referencing a column that does not exist in the source is a **configuration
error** (exit code 2), not a check failure: it is validated against the real
schema before anything runs.

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

### `freshness`

The most recent value of the timestamp column is not older than `max_age`
relative to run time.

```yaml
- freshness: {column: pickup_ts, max_age: 24h}
```

- `max_age` is a **duration** (see below). The time reference is DuckDB's
  `now()` at run time.
- If the source is empty or the column is all `NULL`, the check **passes** (use
  `row_count` and `not_null` to require data).

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
