"""Resolución de rutas de fuentes a vistas DuckDB."""

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
    """True si la ruta es un patrón glob (contiene '*', '?' o '[')."""
    return any(char in pattern for char in _GLOB_CHARS)


def _reader_for(name: str, path: Path) -> str:
    """Selecciona el lector DuckDB según la extensión de la ruta (o patrón)."""
    reader = _READERS.get(path.suffix.lower())
    if reader is None:
        supported = ", ".join(sorted(_READERS))
        raise SourceError(
            f"fuente '{name}': extensión no soportada '{path.suffix}'. "
            f"Extensiones soportadas: {supported}"
        )
    return reader


def create_views(
    conn: duckdb.DuckDBPyConnection,
    sources: Mapping[str, SourceSpec],
    base_dir: Path,
) -> None:
    """Crea una vista DuckDB por fuente para que los checks la consulten por nombre.

    Las rutas relativas se resuelven respecto al directorio del fichero de suite.
    Un `path` puede ser un patrón glob (`data/*.parquet`): DuckDB lee todos los
    ficheros que casen; debe casar al menos uno.
    """
    for name, spec in sources.items():
        path = Path(spec.path)
        if not path.is_absolute():
            path = base_dir / path
        if _is_glob(spec.path):
            if not globlib.glob(str(path), recursive=True):
                raise SourceError(
                    f"fuente '{name}': el patrón {path} no coincide con ningún fichero"
                )
        elif not path.exists():
            raise SourceError(f"fuente '{name}': no existe el fichero {path}")
        reader = _reader_for(name, path)
        escaped = str(path).replace("'", "''")
        conn.execute(
            f"CREATE OR REPLACE VIEW {quote_ident(name)} AS SELECT * FROM {reader}('{escaped}')"
        )


def view_columns(conn: duckdb.DuckDBPyConnection, name: str) -> list[str]:
    """Columnas de una vista/tabla DuckDB, en su orden de definición."""
    rows = conn.execute(f"DESCRIBE {quote_ident(name)}").fetchall()
    return [str(row[0]) for row in rows]
