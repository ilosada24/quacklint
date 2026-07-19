"""Clase base de los checks, resultado y registro por decorador.

Todo check compila a SQL de DuckDB que devuelve las filas que VIOLAN la regla:
el check pasa si esa consulta no devuelve ninguna fila. Nunca se cargan datos
a Python/pandas para validar.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from collections.abc import Callable
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, ClassVar, TypeVar

import duckdb

from quacklint.errors import ExecutionError

if TYPE_CHECKING:
    from quacklint.suite import BaseCheckSpec


def quote_ident(name: str) -> str:
    """Cita un identificador (columna, vista) para SQL de DuckDB."""
    return '"' + name.replace('"', '""') + '"'


@dataclass(frozen=True, slots=True)
class CheckResult:
    """Resultado de evaluar un check contra una fuente."""

    check: str
    source: str
    passed: bool
    failed_rows: int
    message: str = ""
    sample_columns: tuple[str, ...] = ()
    sample_rows: tuple[tuple[object, ...], ...] = ()


class Check(ABC):
    """Un check ejecutable sobre una fuente (vista DuckDB)."""

    name: ClassVar[str]
    sample_limit: ClassVar[int] = 5

    def __init__(self, source: str) -> None:
        self.source = source

    @property
    def display_name(self) -> str:
        """Nombre con el que se reporta el check (custom_sql usa el suyo propio)."""
        return self.name

    @classmethod
    @abstractmethod
    def from_spec(cls, source: str, spec: BaseCheckSpec) -> Check:
        """Construye el check a partir de su modelo de configuración validado."""

    @abstractmethod
    def to_sql(self, source: str) -> str:
        """SQL de DuckDB cuyas filas resultantes son las violaciones sobre `source`."""

    def evaluate(self, conn: duckdb.DuckDBPyConnection) -> CheckResult:
        """Evalúa el check contando las filas que violan la regla.

        Si hay violaciones, adjunta hasta `sample_limit` filas de muestra.
        """
        sql = self.to_sql(self.source)
        row = self._fetchone(conn, f"SELECT count(*) FROM ({sql}) AS violations")
        failed_rows = int(row[0]) if row is not None else 0
        if failed_rows == 0:
            return CheckResult(
                check=self.display_name, source=self.source, passed=True, failed_rows=0
            )
        columns, sample = self._sample(conn, sql)
        return CheckResult(
            check=self.display_name,
            source=self.source,
            passed=False,
            failed_rows=failed_rows,
            message=f"{failed_rows} fila(s) violan la regla",
            sample_columns=columns,
            sample_rows=sample,
        )

    def _sample(
        self, conn: duckdb.DuckDBPyConnection, sql: str
    ) -> tuple[tuple[str, ...], tuple[tuple[object, ...], ...]]:
        # COLUMNS(*)::VARCHAR: los valores llegan como texto, evitando conversiones
        # de tipos DuckDB→Python (p. ej. TIMESTAMPTZ requiere pytz).
        sample_sql = (
            f"SELECT COLUMNS(*)::VARCHAR FROM ({sql}) AS violations LIMIT {self.sample_limit}"
        )
        try:
            cursor = conn.execute(sample_sql)
            rows = cursor.fetchall()
            columns = tuple(str(desc[0]) for desc in cursor.description or [])
        except duckdb.Error as exc:
            raise ExecutionError(
                f"check '{self.name}' sobre '{self.source}': error de DuckDB: {exc}"
            ) from exc
        return columns, tuple(tuple(item) for item in rows)

    def _fetchone(
        self, conn: duckdb.DuckDBPyConnection, sql: str
    ) -> tuple[Any, ...] | None:
        try:
            return conn.execute(sql).fetchone()
        except duckdb.Error as exc:
            raise ExecutionError(
                f"check '{self.name}' sobre '{self.source}': error de DuckDB: {exc}"
            ) from exc


_CheckT = TypeVar("_CheckT", bound=Check)

_REGISTRY: dict[str, type[Check]] = {}


def register(name: str) -> Callable[[type[_CheckT]], type[_CheckT]]:
    """Decorador que registra una implementación de check bajo su nombre del YAML."""

    def decorator(cls: type[_CheckT]) -> type[_CheckT]:
        if name in _REGISTRY:
            raise ValueError(f"check duplicado en el registro: '{name}'")
        cls.name = name
        _REGISTRY[name] = cls
        return cls

    return decorator


def get_check(name: str) -> type[Check]:
    """Devuelve la implementación registrada para un nombre de check."""
    try:
        return _REGISTRY[name]
    except KeyError:
        available = ", ".join(sorted(_REGISTRY)) or "(ninguno)"
        raise ExecutionError(
            f"el check '{name}' todavía no tiene implementación ejecutable. "
            f"Checks implementados: {available}"
        ) from None


def build_check(source: str, spec: BaseCheckSpec) -> Check:
    """Instancia la implementación registrada para una configuración de check."""
    return get_check(spec.check_type).from_spec(source, spec)


def registered_checks() -> dict[str, type[Check]]:
    """Copia del registro actual de checks."""
    return dict(_REGISTRY)
