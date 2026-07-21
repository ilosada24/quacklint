"""Resolving source paths to DuckDB views."""

from __future__ import annotations

import glob as globlib
from collections.abc import Mapping
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


def db_source_statements(name: str, spec: SourceSpec) -> list[str]:
    """The DuckDB statements that attach a database source and expose it as a view.

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
    alias = f"_ql_src_{name}"
    conn_literal = "'" + spec.connection.replace("'", "''") + "'"
    read_only = ", READ_ONLY" if spec.read_only else ""
    qualified = ".".join(
        [quote_ident(alias)] + [quote_ident(part) for part in spec.table.split(".")]
    )
    return [
        f"INSTALL {extension}",
        f"LOAD {extension}",
        f"ATTACH {conn_literal} AS {quote_ident(alias)} (TYPE {spec.type}{read_only})",
        f"CREATE OR REPLACE VIEW {quote_ident(name)} AS SELECT * FROM {qualified}",
    ]


def _create_db_view(conn: duckdb.DuckDBPyConnection, name: str, spec: SourceSpec) -> None:
    for statement in db_source_statements(name, spec):
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
