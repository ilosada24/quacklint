"""Carga y validación de suites YAML.

El contrato del formato vive en docs/spec.yaml.md: cualquier cambio aquí debe
reflejarse allí y viceversa.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import timedelta
from pathlib import Path
from typing import Any, ClassVar, Final, Literal, NoReturn

import duckdb
import yaml
from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    ValidationError,
    field_validator,
    model_validator,
)

from quacklint.checks.base import CheckResult, Severity, build_check
from quacklint.errors import SpecError
from quacklint.sources import create_views, view_columns

SUPPORTED_VERSION: Final = 1

_IDENTIFIER_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")
_DURATION_RE = re.compile(r"^(?P<value>\d+)(?P<unit>[smhd])$")
_DURATION_SECONDS = {"s": 1, "m": 60, "h": 3600, "d": 86400}


def parse_duration(raw: str) -> timedelta:
    """Convierte duraciones tipo '24h' en timedelta. Unidades: s, m, h, d."""
    match = _DURATION_RE.match(raw.strip())
    if match is None:
        raise ValueError(
            f"duración inválida: {raw!r} (formato: entero + unidad s/m/h/d, p. ej. '24h' o '30m')"
        )
    return timedelta(seconds=int(match["value"]) * _DURATION_SECONDS[match["unit"]])


def format_duration(delta: timedelta) -> str:
    """Formatea un timedelta con la unidad más grande exacta ('24h', '7d'...)."""
    seconds = int(delta.total_seconds())
    for unit, size in (("d", 86400), ("h", 3600), ("m", 60)):
        if seconds >= size and seconds % size == 0:
            return f"{seconds // size}{unit}"
    return f"{seconds}s"


class SourceSpec(BaseModel):
    """Una fuente de datos: un fichero que se expondrá como vista DuckDB."""

    model_config = ConfigDict(extra="forbid")

    path: str = Field(min_length=1)


class BaseCheckSpec(BaseModel):
    """Base de la configuración de un check dentro de la suite."""

    model_config = ConfigDict(extra="forbid")

    check_type: ClassVar[str]

    severity: Severity = "error"

    @classmethod
    def coerce_payload(cls, payload: Any) -> Any:
        """Normaliza formas abreviadas del YAML (string/lista) al mapeo de campos."""
        return payload

    def referenced_columns(self) -> tuple[str, ...]:
        """Columnas de la fuente que el check referencia (se validan contra el esquema)."""
        return ()


class NotNullSpec(BaseCheckSpec):
    check_type: ClassVar[str] = "not_null"

    columns: list[str] = Field(min_length=1)

    @classmethod
    def coerce_payload(cls, payload: Any) -> Any:
        if isinstance(payload, str):
            return {"columns": [payload]}
        if isinstance(payload, list):
            return {"columns": payload}
        return payload

    def referenced_columns(self) -> tuple[str, ...]:
        return tuple(self.columns)


class UniqueSpec(BaseCheckSpec):
    check_type: ClassVar[str] = "unique"

    columns: list[str] = Field(min_length=1)

    @classmethod
    def coerce_payload(cls, payload: Any) -> Any:
        if isinstance(payload, str):
            return {"columns": [payload]}
        if isinstance(payload, list):
            return {"columns": payload}
        return payload

    def referenced_columns(self) -> tuple[str, ...]:
        return tuple(self.columns)


class AcceptedValuesSpec(BaseCheckSpec):
    check_type: ClassVar[str] = "accepted_values"

    column: str = Field(min_length=1)
    values: list[str | int | float | bool] = Field(min_length=1)

    def referenced_columns(self) -> tuple[str, ...]:
        return (self.column,)


class RangeSpec(BaseCheckSpec):
    check_type: ClassVar[str] = "range"

    column: str = Field(min_length=1)
    min: float | None = None
    max: float | None = None

    @model_validator(mode="after")
    def _validate_bounds(self) -> RangeSpec:
        if self.min is None and self.max is None:
            raise ValueError("necesita al menos 'min' o 'max'")
        if self.min is not None and self.max is not None and self.min > self.max:
            raise ValueError(f"'min' ({self.min}) no puede ser mayor que 'max' ({self.max})")
        return self

    def referenced_columns(self) -> tuple[str, ...]:
        return (self.column,)


class RegexMatchSpec(BaseCheckSpec):
    check_type: ClassVar[str] = "regex_match"

    column: str = Field(min_length=1)
    pattern: str = Field(min_length=1)

    @field_validator("pattern")
    @classmethod
    def _validate_pattern(cls, value: str) -> str:
        try:
            re.compile(value)
        except re.error as exc:
            raise ValueError(f"expresión regular inválida: {exc}") from exc
        return value

    def referenced_columns(self) -> tuple[str, ...]:
        return (self.column,)


class RowCountSpec(BaseCheckSpec):
    check_type: ClassVar[str] = "row_count"

    min: int | None = Field(default=None, ge=0)
    max: int | None = Field(default=None, ge=0)

    @model_validator(mode="after")
    def _validate_bounds(self) -> RowCountSpec:
        if self.min is None and self.max is None:
            raise ValueError("necesita al menos 'min' o 'max'")
        if self.min is not None and self.max is not None and self.min > self.max:
            raise ValueError(f"'min' ({self.min}) no puede ser mayor que 'max' ({self.max})")
        return self


class FreshnessSpec(BaseCheckSpec):
    check_type: ClassVar[str] = "freshness"

    column: str = Field(min_length=1)
    max_age: timedelta

    @field_validator("max_age", mode="before")
    @classmethod
    def _coerce_max_age(cls, value: object) -> object:
        if isinstance(value, str):
            return parse_duration(value)
        if isinstance(value, timedelta):
            return value
        raise ValueError("'max_age' debe ser una duración como '24h' (unidades: s, m, h, d)")

    def referenced_columns(self) -> tuple[str, ...]:
        return (self.column,)


class CustomSqlSpec(BaseCheckSpec):
    check_type: ClassVar[str] = "custom_sql"

    name: str = Field(pattern=r"^[A-Za-z_][A-Za-z0-9_]*$")
    query: str = Field(min_length=1)

    @field_validator("query")
    @classmethod
    def _strip_query(cls, value: str) -> str:
        stripped = value.strip().rstrip(";").strip()
        if not stripped:
            raise ValueError("'query' no puede estar vacía")
        return stripped


CHECK_SPEC_TYPES: dict[str, type[BaseCheckSpec]] = {
    cls.check_type: cls
    for cls in (
        NotNullSpec,
        UniqueSpec,
        AcceptedValuesSpec,
        RangeSpec,
        RegexMatchSpec,
        RowCountSpec,
        FreshnessSpec,
        CustomSqlSpec,
    )
}


class SuiteSpec(BaseModel):
    """Representación validada de una suite completa."""

    model_config = ConfigDict(extra="forbid")

    version: Literal[1]
    sources: dict[str, SourceSpec]
    checks: dict[str, list[BaseCheckSpec]] = Field(default_factory=dict)


def _fail(location: str, message: str) -> NoReturn:
    raise SpecError(f"{location}: {message}")


def _format_validation_error(exc: ValidationError) -> str:
    parts: list[str] = []
    for error in exc.errors():
        loc = ".".join(str(item) for item in error["loc"])
        prefix = f"'{loc}': " if loc else ""
        parts.append(f"{prefix}{error['msg']}")
    return "; ".join(parts)


def _parse_sources(raw: Any) -> dict[str, SourceSpec]:
    if raw is None:
        _fail("sources", "falta la sección; declara al menos una fuente: 'nombre: {path: ...}'")
    if not isinstance(raw, dict):
        _fail("sources", "debe ser un mapeo de nombre de fuente a configuración")
    if not raw:
        _fail("sources", "debe declarar al menos una fuente")
    sources: dict[str, SourceSpec] = {}
    for name, payload in raw.items():
        if not isinstance(name, str) or not _IDENTIFIER_RE.match(name):
            _fail(
                "sources",
                f"nombre de fuente inválido: {name!r} "
                "(letras, dígitos y '_', sin empezar por dígito)",
            )
        try:
            sources[name] = SourceSpec.model_validate(payload)
        except ValidationError as exc:
            _fail(f"sources.{name}", _format_validation_error(exc))
    return sources


def _parse_check_entry(location: str, entry: Any) -> BaseCheckSpec:
    if not isinstance(entry, dict) or len(entry) != 1:
        _fail(
            location,
            "cada check debe ser un mapeo con una única clave, "
            "p. ej. '- unique: trip_id' o '- range: {column: fare, min: 0}'",
        )
    check_name, payload = next(iter(entry.items()))
    if not isinstance(check_name, str) or check_name not in CHECK_SPEC_TYPES:
        available = ", ".join(sorted(CHECK_SPEC_TYPES))
        _fail(location, f"check desconocido: {check_name!r}. Checks disponibles: {available}")
    spec_cls = CHECK_SPEC_TYPES[check_name]
    try:
        return spec_cls.model_validate(spec_cls.coerce_payload(payload))
    except ValidationError as exc:
        _fail(f"{location} ({check_name})", _format_validation_error(exc))


def _parse_checks(raw: Any, sources: dict[str, SourceSpec]) -> dict[str, list[BaseCheckSpec]]:
    if raw is None:
        return {}
    if not isinstance(raw, dict):
        _fail("checks", "debe ser un mapeo de nombre de fuente a lista de checks")
    checks: dict[str, list[BaseCheckSpec]] = {}
    for source_name, entries in raw.items():
        location = f"checks.{source_name}"
        if source_name not in sources:
            defined = ", ".join(sorted(sources))
            _fail(
                location,
                f"la fuente '{source_name}' no está declarada en 'sources'. "
                f"Fuentes declaradas: {defined}",
            )
        if not isinstance(entries, list):
            _fail(location, "debe ser una lista de checks (cada elemento: '- <check>: <config>')")
        checks[source_name] = [
            _parse_check_entry(f"{location}[{index}]", entry)
            for index, entry in enumerate(entries)
        ]
    return checks


def parse_suite(data: Any) -> SuiteSpec:
    """Valida la estructura ya deserializada de una suite y construye el modelo."""
    if not isinstance(data, dict):
        raise SpecError(
            "la suite debe ser un mapeo YAML con las claves 'version', 'sources' y 'checks' "
            "(ver docs/spec.yaml.md)"
        )
    unknown = set(data) - {"version", "sources", "checks"}
    if unknown:
        _fail(
            "suite",
            f"claves desconocidas: {', '.join(sorted(str(key) for key in unknown))}. "
            "Claves válidas: version, sources, checks",
        )
    if "version" not in data:
        _fail("version", "falta; añade 'version: 1' al principio de la suite")
    if data["version"] != SUPPORTED_VERSION:
        _fail(
            "version",
            f"versión no soportada: {data['version']!r}. "
            f"Esta versión de quacklint soporta 'version: {SUPPORTED_VERSION}'",
        )
    sources = _parse_sources(data.get("sources"))
    checks = _parse_checks(data.get("checks"), sources)
    return SuiteSpec(version=SUPPORTED_VERSION, sources=sources, checks=checks)


def load_suite(path: str | Path) -> SuiteSpec:
    """Lee y valida una suite desde un fichero YAML."""
    suite_path = Path(path)
    try:
        text = suite_path.read_text(encoding="utf-8")
    except FileNotFoundError:
        raise SpecError(f"no existe el fichero de suite: {suite_path}") from None
    except OSError as exc:
        raise SpecError(f"no se pudo leer la suite {suite_path}: {exc}") from exc
    try:
        data = yaml.safe_load(text)
    except yaml.YAMLError as exc:
        detail = " ".join(str(exc).split())
        raise SpecError(f"YAML inválido en {suite_path}: {detail}") from exc
    return parse_suite(data)


@dataclass(frozen=True)
class CompiledCheck:
    """SQL compilado de un check (para `quacklint run --explain`)."""

    source: str
    check: str
    sql: str


@dataclass(frozen=True)
class Suite:
    """Suite validada más el contexto necesario para ejecutarla."""

    spec: SuiteSpec
    base_dir: Path

    @classmethod
    def from_file(cls, path: str | Path) -> Suite:
        suite_path = Path(path)
        spec = load_suite(suite_path)
        return cls(spec=spec, base_dir=suite_path.resolve().parent)

    def compile(self) -> list[CompiledCheck]:
        """Compila cada check a su SQL de violaciones, sin ejecutar nada."""
        from quacklint.checks import builtin  # noqa: F401  (registra los checks incorporados)

        return [
            CompiledCheck(
                source=source_name,
                check=check_spec.check_type,
                sql=build_check(source_name, check_spec).to_sql(source_name),
            )
            for source_name, entries in self.spec.checks.items()
            for check_spec in entries
        ]

    def run(self, *, fail_fast: bool = False) -> list[CheckResult]:
        """Crea las vistas de las fuentes y evalúa todos los checks en DuckDB.

        Con `fail_fast`, se detiene tras el primer check fallido con
        `severity: error` (una advertencia no detiene la ejecución).
        """
        from quacklint.checks import builtin  # noqa: F401  (registra los checks incorporados)

        conn = duckdb.connect()
        try:
            create_views(conn, self.spec.sources, self.base_dir)
            self._check_referenced_columns(conn)
            results: list[CheckResult] = []
            for source_name, entries in self.spec.checks.items():
                for check_spec in entries:
                    result = build_check(source_name, check_spec).evaluate(conn)
                    results.append(result)
                    if fail_fast and not result.passed and result.severity == "error":
                        return results
            return results
        finally:
            conn.close()

    def _check_referenced_columns(self, conn: duckdb.DuckDBPyConnection) -> None:
        """Una columna inexistente es error de configuración, no un fallo del check."""
        for source_name, entries in self.spec.checks.items():
            available = view_columns(conn, source_name)
            known = set(available)
            for index, check_spec in enumerate(entries):
                missing = [
                    column
                    for column in check_spec.referenced_columns()
                    if column not in known
                ]
                if missing:
                    raise SpecError(
                        f"checks.{source_name}[{index}] ({check_spec.check_type}): "
                        f"columna(s) inexistente(s) en '{source_name}': {', '.join(missing)}. "
                        f"Columnas disponibles: {', '.join(available)}"
                    )
