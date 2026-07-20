"""Loading and validation of YAML suites.

The format contract lives in docs/spec.yaml.md: any change here must be
reflected there and vice versa.
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
from quacklint.sources import create_views, view_column_types

SUPPORTED_VERSION: Final = 1

_IDENTIFIER_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")
_DURATION_RE = re.compile(r"^(?P<value>\d+)(?P<unit>[smhd])$")
_DURATION_SECONDS = {"s": 1, "m": 60, "h": 3600, "d": 86400}

# Column-type categories that some checks require (validated before running).
ColumnCategory = Literal["numeric", "temporal", "string"]

_NUMERIC_TYPES = frozenset(
    {
        "TINYINT", "SMALLINT", "INTEGER", "BIGINT", "HUGEINT",
        "UTINYINT", "USMALLINT", "UINTEGER", "UBIGINT", "UHUGEINT",
        "FLOAT", "REAL", "DOUBLE", "DECIMAL",
    }
)
_TEMPORAL_TYPES = frozenset(
    {"DATE", "TIME", "TIMETZ", "TIMESTAMP", "TIMESTAMPTZ", "DATETIME"}
)
_STRING_TYPES = frozenset({"VARCHAR", "CHAR", "BPCHAR", "TEXT", "STRING"})

_CATEGORY_LABEL: dict[ColumnCategory, str] = {
    "numeric": "numeric",
    "temporal": "temporal (DATE/TIME/TIMESTAMP)",
    "string": "text (VARCHAR)",
}


def _type_category(duckdb_type: str) -> ColumnCategory | None:
    """Classify a DuckDB type string into a category, or None if unknown."""
    upper = duckdb_type.upper()
    if "WITH TIME ZONE" in upper or upper.startswith("TIMESTAMP_"):
        return "temporal"
    base = upper.split("(", 1)[0].strip()
    if base in _NUMERIC_TYPES:
        return "numeric"
    if base in _TEMPORAL_TYPES:
        return "temporal"
    if base in _STRING_TYPES:
        return "string"
    return None


def parse_duration(raw: str) -> timedelta:
    """Convert durations like '24h' into a timedelta. Units: s, m, h, d."""
    match = _DURATION_RE.match(raw.strip())
    if match is None:
        raise ValueError(
            f"invalid duration: {raw!r} (format: integer + unit s/m/h/d, e.g. '24h' or '30m')"
        )
    return timedelta(seconds=int(match["value"]) * _DURATION_SECONDS[match["unit"]])


def format_duration(delta: timedelta) -> str:
    """Format a timedelta using the largest exact unit ('24h', '7d'...)."""
    seconds = int(delta.total_seconds())
    for unit, size in (("d", 86400), ("h", 3600), ("m", 60)):
        if seconds >= size and seconds % size == 0:
            return f"{seconds // size}{unit}"
    return f"{seconds}s"


class SourceSpec(BaseModel):
    """A data source: a file exposed as a DuckDB view."""

    model_config = ConfigDict(extra="forbid")

    path: str = Field(min_length=1)


class BaseCheckSpec(BaseModel):
    """Base of a check's configuration within the suite."""

    model_config = ConfigDict(extra="forbid")

    check_type: ClassVar[str]

    severity: Severity = "error"

    @classmethod
    def coerce_payload(cls, payload: Any) -> Any:
        """Normalize shorthand YAML forms (string/list) to the field mapping."""
        return payload

    def referenced_columns(self) -> tuple[str, ...]:
        """Source columns the check references (validated against the schema)."""
        return ()

    def required_column_types(self) -> tuple[tuple[str, ColumnCategory], ...]:
        """(column, category) pairs whose type is validated before running."""
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
            raise ValueError("needs at least 'min' or 'max'")
        if self.min is not None and self.max is not None and self.min > self.max:
            raise ValueError(f"'min' ({self.min}) cannot be greater than 'max' ({self.max})")
        return self

    def referenced_columns(self) -> tuple[str, ...]:
        return (self.column,)

    def required_column_types(self) -> tuple[tuple[str, ColumnCategory], ...]:
        return ((self.column, "numeric"),)


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
            raise ValueError(f"invalid regular expression: {exc}") from exc
        return value

    def referenced_columns(self) -> tuple[str, ...]:
        return (self.column,)

    def required_column_types(self) -> tuple[tuple[str, ColumnCategory], ...]:
        return ((self.column, "string"),)


class RowCountSpec(BaseCheckSpec):
    check_type: ClassVar[str] = "row_count"

    min: int | None = Field(default=None, ge=0)
    max: int | None = Field(default=None, ge=0)

    @model_validator(mode="after")
    def _validate_bounds(self) -> RowCountSpec:
        if self.min is None and self.max is None:
            raise ValueError("needs at least 'min' or 'max'")
        if self.min is not None and self.max is not None and self.min > self.max:
            raise ValueError(f"'min' ({self.min}) cannot be greater than 'max' ({self.max})")
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
        raise ValueError("'max_age' must be a duration like '24h' (units: s, m, h, d)")

    def referenced_columns(self) -> tuple[str, ...]:
        return (self.column,)

    def required_column_types(self) -> tuple[tuple[str, ColumnCategory], ...]:
        return ((self.column, "temporal"),)


class CustomSqlSpec(BaseCheckSpec):
    check_type: ClassVar[str] = "custom_sql"

    name: str = Field(pattern=r"^[A-Za-z_][A-Za-z0-9_]*$")
    query: str = Field(min_length=1)

    @field_validator("query")
    @classmethod
    def _strip_query(cls, value: str) -> str:
        stripped = value.strip().rstrip(";").strip()
        if not stripped:
            raise ValueError("'query' cannot be empty")
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
    """Validated representation of a complete suite."""

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
        _fail("sources", "section is missing; declare at least one source: 'name: {path: ...}'")
    if not isinstance(raw, dict):
        _fail("sources", "must be a mapping of source name to configuration")
    if not raw:
        _fail("sources", "must declare at least one source")
    sources: dict[str, SourceSpec] = {}
    for name, payload in raw.items():
        if not isinstance(name, str) or not _IDENTIFIER_RE.match(name):
            _fail(
                "sources",
                f"invalid source name: {name!r} "
                "(letters, digits and '_', not starting with a digit)",
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
            "each check must be a mapping with a single key, "
            "e.g. '- unique: trip_id' or '- range: {column: fare, min: 0}'",
        )
    check_name, payload = next(iter(entry.items()))
    if not isinstance(check_name, str) or check_name not in CHECK_SPEC_TYPES:
        available = ", ".join(sorted(CHECK_SPEC_TYPES))
        _fail(location, f"unknown check: {check_name!r}. Available checks: {available}")
    spec_cls = CHECK_SPEC_TYPES[check_name]
    try:
        return spec_cls.model_validate(spec_cls.coerce_payload(payload))
    except ValidationError as exc:
        _fail(f"{location} ({check_name})", _format_validation_error(exc))


def _parse_checks(raw: Any, sources: dict[str, SourceSpec]) -> dict[str, list[BaseCheckSpec]]:
    if raw is None:
        return {}
    if not isinstance(raw, dict):
        _fail("checks", "must be a mapping of source name to a list of checks")
    checks: dict[str, list[BaseCheckSpec]] = {}
    for source_name, entries in raw.items():
        location = f"checks.{source_name}"
        if source_name not in sources:
            defined = ", ".join(sorted(sources))
            _fail(
                location,
                f"source '{source_name}' is not declared in 'sources'. "
                f"Declared sources: {defined}",
            )
        if not isinstance(entries, list):
            _fail(location, "must be a list of checks (each item: '- <check>: <config>')")
        checks[source_name] = [
            _parse_check_entry(f"{location}[{index}]", entry)
            for index, entry in enumerate(entries)
        ]
    return checks


def parse_suite(data: Any) -> SuiteSpec:
    """Validate the already-deserialized suite structure and build the model."""
    if not isinstance(data, dict):
        raise SpecError(
            "the suite must be a YAML mapping with the keys 'version', 'sources' and 'checks' "
            "(see docs/spec.yaml.md)"
        )
    unknown = set(data) - {"version", "sources", "checks"}
    if unknown:
        _fail(
            "suite",
            f"unknown keys: {', '.join(sorted(str(key) for key in unknown))}. "
            "Valid keys: version, sources, checks",
        )
    if "version" not in data:
        _fail("version", "missing; add 'version: 1' at the top of the suite")
    if data["version"] != SUPPORTED_VERSION:
        _fail(
            "version",
            f"unsupported version: {data['version']!r}. "
            f"This quacklint version supports 'version: {SUPPORTED_VERSION}'",
        )
    sources = _parse_sources(data.get("sources"))
    checks = _parse_checks(data.get("checks"), sources)
    return SuiteSpec(version=SUPPORTED_VERSION, sources=sources, checks=checks)


def load_suite(path: str | Path) -> SuiteSpec:
    """Read and validate a suite from a YAML file."""
    suite_path = Path(path)
    try:
        text = suite_path.read_text(encoding="utf-8")
    except FileNotFoundError:
        raise SpecError(f"suite file does not exist: {suite_path}") from None
    except OSError as exc:
        raise SpecError(f"could not read suite {suite_path}: {exc}") from exc
    try:
        data = yaml.safe_load(text)
    except yaml.YAMLError as exc:
        detail = " ".join(str(exc).split())
        raise SpecError(f"invalid YAML in {suite_path}: {detail}") from exc
    return parse_suite(data)


@dataclass(frozen=True)
class CompiledCheck:
    """A check's compiled SQL (for `quacklint run --explain`)."""

    source: str
    check: str
    sql: str


@dataclass(frozen=True)
class Suite:
    """Validated suite plus the context needed to run it."""

    spec: SuiteSpec
    base_dir: Path

    @classmethod
    def from_file(cls, path: str | Path) -> Suite:
        suite_path = Path(path)
        spec = load_suite(suite_path)
        return cls(spec=spec, base_dir=suite_path.resolve().parent)

    def compile(self) -> list[CompiledCheck]:
        """Compile each check to its violations SQL, without running anything."""
        from quacklint.checks import builtin  # noqa: F401  (registers the built-in checks)

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
        """Create the source views and evaluate all checks in DuckDB.

        With `fail_fast`, stop after the first failed check with
        `severity: error` (a warning does not stop the run).
        """
        from quacklint.checks import builtin  # noqa: F401  (registers the built-in checks)

        conn = duckdb.connect()
        try:
            # Pin the session time zone so temporal comparisons (freshness) are
            # reproducible regardless of the host time zone.
            conn.execute("SET TimeZone='UTC'")
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
        """A nonexistent column or wrong column type is a configuration error,
        not a check failure: both are validated against the real schema before
        anything runs."""
        for source_name, entries in self.spec.checks.items():
            types = view_column_types(conn, source_name)
            available = list(types)
            known = set(available)
            for index, check_spec in enumerate(entries):
                location = f"checks.{source_name}[{index}] ({check_spec.check_type})"
                missing = [
                    column
                    for column in check_spec.referenced_columns()
                    if column not in known
                ]
                if missing:
                    raise SpecError(
                        f"{location}: "
                        f"nonexistent column(s) in '{source_name}': {', '.join(missing)}. "
                        f"Available columns: {', '.join(available)}"
                    )
                for column, category in check_spec.required_column_types():
                    actual = types[column]
                    if _type_category(actual) != category:
                        raise SpecError(
                            f"{location}: column '{column}' has type {actual}, but "
                            f"{check_spec.check_type} requires a "
                            f"{_CATEGORY_LABEL[category]} column"
                        )
