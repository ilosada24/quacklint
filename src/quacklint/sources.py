"""Resolving source paths to DuckDB views."""

from __future__ import annotations

import glob as globlib
from collections.abc import Mapping
from pathlib import Path
from typing import TYPE_CHECKING

from quacklint.checks.base import quote_ident
from quacklint.errors import SourceError

if TYPE_CHECKING:
    import duckdb

    from quacklint.suite import SourceSpec

_READERS: dict[str, str] = {
    ".parquet": "read_parquet",
    ".csv": "read_csv_auto",
    ".json": "read_json_auto",
    ".ndjson": "read_json_auto",
}

_GLOB_CHARS = ("*", "?", "[")


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

    Relative paths are resolved against the suite file's directory. A `path`
    may be a glob pattern (`data/*.parquet`): DuckDB reads every matching file;
    it must match at least one.
    """
    for name, spec in sources.items():
        path = Path(spec.path)
        if not path.is_absolute():
            path = base_dir / path
        if _is_glob(spec.path):
            if not globlib.glob(str(path), recursive=True):
                raise SourceError(
                    f"source '{name}': pattern {path} matches no files"
                )
        elif not path.exists():
            raise SourceError(f"source '{name}': file {path} does not exist")
        reader = _reader_for(name, path)
        escaped = str(path).replace("'", "''")
        conn.execute(
            f"CREATE OR REPLACE VIEW {quote_ident(name)} AS SELECT * FROM {reader}('{escaped}')"
        )


def view_columns(conn: duckdb.DuckDBPyConnection, name: str) -> list[str]:
    """Columns of a DuckDB view/table, in definition order."""
    rows = conn.execute(f"DESCRIBE {quote_ident(name)}").fetchall()
    return [str(row[0]) for row in rows]
