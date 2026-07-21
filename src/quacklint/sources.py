"""Resolving source paths to DuckDB views."""

from __future__ import annotations

import glob as globlib
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import TYPE_CHECKING

import duckdb

from quacklint.checks.base import quote_ident
from quacklint.errors import SourceError

if TYPE_CHECKING:
    from quacklint.suite import SourceSpec

_READERS: dict[str, str] = {
    ".parquet": "read_parquet",
    ".csv": "read_csv_auto",
    ".json": "read_json_auto",
    ".ndjson": "read_json_auto",
}

_GLOB_CHARS = ("*", "?", "[")

# Database backends supported out of the box (via DuckDB core extensions),
# mapping the source `type` to the DuckDB extension to install/load. Other
# backends (e.g. clickhouse via a community extension) work by naming the
# extension explicitly with the source's `extension` field.
_DB_DEFAULT_EXTENSION: dict[str, str] = {
    "postgres": "postgres",
    "mysql": "mysql",
    "sqlite": "sqlite",
}


def _is_glob(pattern: str) -> bool:
    """True if the path is a glob pattern (contains '*', '?' or '[')."""
    return any(char in pattern for char in _GLOB_CHARS)


def _reader_for(name: str, path: Path) -> str:
    """Select the DuckDB reader based on the path (or pattern) extension."""
    reader = _READERS.get(path.suffix.lower())
    if reader is None:
        supported = ", ".join(sorted(_READERS))
        raise SourceError(
            f"source '{name}': unsupported extension '{path.suffix}'. "
            f"Supported extensions: {supported}"
        )
    return reader


def create_views(
    conn: duckdb.DuckDBPyConnection,
    sources: Mapping[str, SourceSpec],
    base_dir: Path,
) -> None:
    """Create one DuckDB view per source so checks can query it by name.

    A source is either a file (`path`, possibly a glob resolved against the
    suite file's directory) or a database attached via DuckDB.
    """
    for name, spec in sources.items():
        if spec.is_database:
            _create_db_view(conn, name, spec)
        else:
            _create_file_view(conn, name, spec, base_dir)


def _create_file_view(
    conn: duckdb.DuckDBPyConnection, name: str, spec: SourceSpec, base_dir: Path
) -> None:
    raw = spec.path
    assert raw is not None  # not is_database
    path = Path(raw)
    if not path.is_absolute():
        path = base_dir / path
    if _is_glob(raw):
        if not globlib.glob(str(path), recursive=True):
            raise SourceError(f"source '{name}': pattern {path} matches no files")
    elif not path.exists():
        raise SourceError(f"source '{name}': file {path} does not exist")
    reader = _reader_for(name, path)
    escaped = str(path).replace("'", "''")
    conn.execute(
        f"CREATE OR REPLACE VIEW {quote_ident(name)} AS SELECT * FROM {reader}('{escaped}')"
    )


def _db_alias(name: str) -> str:
    return f"_ql_src_{name}"


def _qualified_table(name: str, spec: SourceSpec) -> str:
    assert spec.table is not None
    parts = [_db_alias(name), *spec.table.split(".")]
    return ".".join(quote_ident(part) for part in parts)


def db_source_statements(name: str, spec: SourceSpec) -> list[str]:
    """The DuckDB statements that attach a database source and expose it as a view.

    The view is metadata-only: no rows move until something queries it. That is
    what lets the schema pre-check validate against the real remote schema
    before `materialize_statements` decides which columns are worth copying.

    Pure (no execution) so it can be inspected and unit-tested.
    """
    assert spec.type is not None and spec.connection is not None and spec.table is not None
    extension = spec.extension or _DB_DEFAULT_EXTENSION.get(spec.type)
    if not extension:
        known = ", ".join(sorted(_DB_DEFAULT_EXTENSION))
        raise SourceError(
            f"source '{name}': unknown database type {spec.type!r}; add "
            f"'extension: <duckdb-extension>' naming the extension to load "
            f"(built-in types: {known})"
        )
    conn_literal = "'" + spec.connection.replace("'", "''") + "'"
    read_only = ", READ_ONLY" if spec.read_only else ""
    return [
        f"INSTALL {extension}",
        f"LOAD {extension}",
        f"ATTACH {conn_literal} AS {quote_ident(_db_alias(name))} "
        f"(TYPE {spec.type}{read_only})",
        f"CREATE OR REPLACE VIEW {quote_ident(name)} AS "
        f"SELECT * FROM {_qualified_table(name, spec)}",
    ]


def materialize_statements(
    name: str, spec: SourceSpec, columns: Sequence[str] | None
) -> list[str]:
    """Statements that copy a database source into local DuckDB storage.

    Replaces the pass-through view with a real table, so N checks cost one
    remote read instead of N. `columns` restricts the copy to the columns the
    checks actually reference; None copies every column.

    Pure (no execution) so it can be inspected and unit-tested.
    """
    projection = ", ".join(quote_ident(col) for col in columns) if columns else "*"
    quoted = quote_ident(name)
    return [
        f"CREATE OR REPLACE TABLE {quoted} AS SELECT {projection} "
        f"FROM {_qualified_table(name, spec)}",
        # The remote connection is dead weight once the data is local.
        f"DETACH {quote_ident(_db_alias(name))}",
    ]


def materialize_sources(
    conn: duckdb.DuckDBPyConnection,
    sources: Mapping[str, SourceSpec],
    columns: Mapping[str, Sequence[str] | None] | None = None,
) -> None:
    """Materialize every database source configured with `materialize: true`.

    Must run *after* the schema pre-check, which needs the real remote schema.
    """
    for name, spec in sources.items():
        if not (spec.is_database and spec.materialize):
            continue
        needed = (columns or {}).get(name)
        # A view and a table cannot share a name in DuckDB.
        _execute(conn, name, f"DROP VIEW IF EXISTS {quote_ident(name)}")
        for statement in materialize_statements(name, spec, needed):
            _execute(conn, name, statement)


def _create_db_view(conn: duckdb.DuckDBPyConnection, name: str, spec: SourceSpec) -> None:
    for statement in db_source_statements(name, spec):
        _execute(conn, name, statement)


def _execute(conn: duckdb.DuckDBPyConnection, name: str, statement: str) -> None:
    try:
        conn.execute(statement)
    except duckdb.Error as exc:
        raise SourceError(f"source '{name}': {exc}") from exc


def view_columns(conn: duckdb.DuckDBPyConnection, name: str) -> list[str]:
    """Columns of a DuckDB view/table, in definition order."""
    rows = conn.execute(f"DESCRIBE {quote_ident(name)}").fetchall()
    return [str(row[0]) for row in rows]


def view_column_types(conn: duckdb.DuckDBPyConnection, name: str) -> dict[str, str]:
    """Map of column name to DuckDB type for a view/table, in definition order."""
    rows = conn.execute(f"DESCRIBE {quote_ident(name)}").fetchall()
    return {str(row[0]): str(row[1]) for row in rows}
