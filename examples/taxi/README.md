# Example: taxi

Complete suite (uses all 8 check types from the contract) over a synthetic CSV
of taxi trips.

```console
$ uv run quacklint validate examples/taxi/quacklint.yaml
$ uv run quacklint run examples/taxi/quacklint.yaml
```

## What to expect

`data/trips.csv` contains **deliberate errors**; `quacklint run` ends with
exit 1 and this breakdown:

| Check                  | Result | Why                                                |
| ---------------------- | ------ | -------------------------------------------------- |
| `not_null`             | PASS   | `trip_id` and `pickup_ts` have no NULL.            |
| `unique`               | FAIL   | `t-004` appears twice.                             |
| `row_count`            | PASS   | There is more than 1 row.                          |
| `regex_match`          | PASS   | Every `trip_id` follows the pattern `t-[0-9]{3}`.  |
| `accepted_values`      | FAIL   | `t-004` pays with `voucher` (only `card`/`cash`).  |
| `range`                | FAIL   | `t-005` has `fare = -3.0`.                         |
| `freshness`            | FAIL   | The data is from 2026-07-18, older than `24h`.     |
| `no_negative_duration` | FAIL   | For `t-005`, `dropoff_ts < pickup_ts` (`custom_sql`). |

Try also:

```console
$ uv run quacklint run examples/taxi/quacklint.yaml --explain    # see the SQL
$ uv run quacklint run examples/taxi/quacklint.yaml -f json      # JSON report
```
