# quacklint philosophy

## Why it exists

Data quality checks often end up as ad hoc pandas scripts: slow, imperative,
impossible to review and coupled to whoever wrote them. quacklint bets on the
opposite:

- **Declarative.** A YAML suite says *what* must hold, not *how* to check it.
  The YAML is reviewed in a PR just like code.
- **SQL first.** Every check compiles to DuckDB SQL and runs where the data is.
  Data is never loaded into pandas or Python memory to validate: DuckDB reads
  Parquet/CSV/JSON directly and in parallel.
- **DuckDB as the engine.** No services, no credentials, no infrastructure: a
  local process that scales to gigabyte-sized files.
- **Built for CI.** Stable exit codes, `table`/`json`/`junit` output, and
  actionable configuration errors (never tracebacks). A broken suite must break
  the pipeline with a message that says exactly what to fix.

## Design principles

1. A rule violation is a query: each check produces the SQL that returns the
   invalid rows. Zero rows = check passes. This makes checks composable,
   inspectable (`to_sql()`) and debuggable with any DuckDB client.
2. The YAML format is a versioned contract ([spec.yaml.md](spec.yaml.md)); the
   parser accepts nothing the contract does not document.
3. Extensible without touching the core: checks register via a decorator, and
   `custom_sql` covers whatever does not yet have a dedicated check.
