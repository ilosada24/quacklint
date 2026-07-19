"""Resolución de rutas de fuentes a vistas DuckDB."""

from __future__ import annotations

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


def create_views(
    conn: duckdb.DuckDBPyConnection,
    sources: Mapping[str, SourceSpec],
    base_dir: Path,
) -> None:
    """Crea una vista DuckDB por fuente para que los checks la consulten por nombre.

    Las rutas relativas se resuelven respecto al directorio del fichero de suite.
    """
    for name, spec in sources.items():
        path = Path(spec.path)
        if not path.is_absolute():
            path = base_dir / path
        if not path.exists():
            raise SourceError(f"fuente '{name}': no existe el fichero {path}")
        reader = _READERS.get(path.suffix.lower())
        if reader is None:
            supported = ", ".join(sorted(_READERS))
            raise SourceError(
                f"fuente '{name}': extensión no soportada '{path.suffix}'. "
                f"Extensiones soportadas: {supported}"
            )
        escaped = str(path).replace("'", "''")
        conn.execute(
            f"CREATE OR REPLACE VIEW {quote_ident(name)} AS SELECT * FROM {reader}('{escaped}')"
        )


def view_columns(conn: duckdb.DuckDBPyConnection, name: str) -> list[str]:
    """Columnas de una vista/tabla DuckDB, en su orden de definición."""
    rows = conn.execute(f"DESCRIBE {quote_ident(name)}").fetchall()
    return [str(row[0]) for row in rows]
