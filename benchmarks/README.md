# Benchmarks: database sources

Measures what quacklint costs a remote database, and what the two optimisations
in `sources.py` buy.

## Fixture

`seed.sql` builds `bench_orders` in the demo Postgres: **1,000,000 rows, 21
columns, 580 MB**, of which the checks reference only 6. The width is the point
— it is what makes `SELECT *` visible.

```bash
docker exec -i quacklint-postgres psql -U quacklint -d quacklint_demo < benchmarks/seed.sql
uv run python benchmarks/bench.py --suite benchmarks/suite_pg_declarative.yaml
```

## Modes

| Mode | Behaviour |
| ---- | --------- |
| `baseline` | `materialize: false` — pass-through views, every check re-reads the remote table |
| `a1` | materialize once, all columns |
| `a1b1` | materialize once, only the columns the checks reference |

## Results

`suite_pg_declarative.yaml` — 13 declarative checks, best of 3:

| Mode | Seconds | Tuples read | MB from Postgres |
| ---- | ------: | ----------: | ---------------: |
| `baseline` | 7.21 | 16,000,000 | 1457.0 |
| `a1` | 3.08 | 1,000,000 | 617.0 |
| `a1b1` | **1.44** | **1,000,000** | **87.9** |

**5.0× faster, 16.6× less data.** The two effects are independent and compose:

- **A1 (materialize)** attacks the *tuple* count: 16M → 1M, one read instead of
  16. Baseline reads the table 16 times because 13 checks plus the extra
  queries a failing check makes (`count`, then a re-run for sample rows) each
  cost a full remote read.
- **B1 (prune)** attacks the *width*: 617 MB → 88 MB for the same 1M tuples,
  because only 6 of 21 columns are copied.

Results are byte-identical across modes (the harness fingerprints every check
outcome and asserts they match).

## The cost of `custom_sql`

`suite_pg.yaml` is the same suite plus two `custom_sql` checks. Because
`custom_sql` is arbitrary SQL, quacklint cannot know which columns it touches,
so pruning is disabled for any source its query names:

| Mode | Seconds | MB from Postgres |
| ---- | ------: | ---------------: |
| `baseline` | 7.55 | 1488.1 |
| `a1` | 3.10 | 617.1 |
| `a1b1` | 3.18 | 617.1 |

`a1b1` collapses onto `a1` — **one `custom_sql` check costs the whole suite its
column pruning**, here 3.18 s instead of 1.44 s. Preferring declarative checks
over `custom_sql` on wide remote tables is therefore a real performance
decision, not just a style one.
