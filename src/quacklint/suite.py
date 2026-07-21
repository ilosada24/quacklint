"""Loading and validation of YAML suites.

The format contract lives in docs/spec.yaml.md: any change here must be
reflected there and vice versa.
"""

from __future__ import annotations

import re
from collections.abc import Collection
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
from quacklint.sources import create_views, materialize_sources, view_column_types

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
    """A data source exposed as a DuckDB view.

    Either a **file** (`path`, possibly a glob) or a **database** attached via
    DuckDB (`type` + `connection` + `table`), not both.
    """

    model_config = ConfigDict(extra="forbid")

    path: str | None = Field(default=None, min_length=1)
    # Database-backed source (DuckDB ATTACH):
    type: str | None = Field(default=None, min_length=1)
    connection: str | None = Field(default=None, min_length=1)
    table: str | None = Field(default=None, min_length=1)
    extension: str | None = Field(default=None, min_length=1)
    read_only: bool = True
    # Copy the remote table into local DuckDB storage once, instead of leaving
    # the source as a pass-through view that every check re-reads over the
    # network. Set false to stream instead (lower memory, N remote reads).
    materialize: bool = True

    @property
    def is_database(self) -> bool:
        return self.path is None

    @model_validator(mode="after")
    def _validate_shape(self) -> SourceSpec:
        db_fields = (self.type, self.connection, self.table, self.extension)
        has_db = any(v is not None for v in db_fields)
        if self.path is not None:
            if has_db:
                raise ValueError(
                    "a source is either a file ('path') or a database "
                    "('type'/'connection'/'table'), not both"
                )
            if "materialize" in self.model_fields_set:
                raise ValueError("'materialize' only applies to database sources")
            return self
        if not (self.type and self.connection and self.table):
            raise ValueError(
                "a source needs 'path' (file) or 'type'+'connection'+'table' (database)"
            )
        return self


class BaseCheckSpec(BaseModel):
    """Base of a check's configuration within the suite."""

    model_config = ConfigDict(extra="forbid")

    check_type: ClassVar[str]
    # Row-level checks tolerate a bounded number of violations; single-verdict
    # checks (row_count, freshness) override this to reject tolerance config.
    supports_tolerance: ClassVar[bool] = True

    severity: Severity = "error"
    max_failed_rows: int | None = Field(default=None, ge=0)
    max_failed_pct: float | None = Field(default=None, ge=0, le=100)
    tags: list[str] = Field(default_factory=list)

    @model_validator(mode="after")
    def _validate_tolerance(self) -> BaseCheckSpec:
        if (
            self.max_failed_rows is not None or self.max_failed_pct is not None
        ) and not self.supports_tolerance:
            raise ValueError(
                f"{self.check_type} does not support tolerance "
                "(max_failed_rows/max_failed_pct)"
            )
        return self

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

    def referenced_foreign(self) -> tuple[tuple[str, str], ...]:
        """(other_source, column) pairs this check references in another source."""
        return ()

    def validate_against_sources(self, sources: dict[str, SourceSpec]) -> None:
        """Validate cross-source references at parse time (no schema needed)."""


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


class NotEmptyStringSpec(BaseCheckSpec):
    check_type: ClassVar[str] = "not_empty_string"

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

    def required_column_types(self) -> tuple[tuple[str, ColumnCategory], ...]:
        return tuple((column, "string") for column in self.columns)


class StringLengthSpec(BaseCheckSpec):
    check_type: ClassVar[str] = "string_length"

    column: str = Field(min_length=1)
    min: int | None = Field(default=None, ge=0)
    max: int | None = Field(default=None, ge=0)

    @model_validator(mode="after")
    def _validate_bounds(self) -> StringLengthSpec:
        if self.min is None and self.max is None:
            raise ValueError("needs at least 'min' or 'max'")
        if self.min is not None and self.max is not None and self.min > self.max:
            raise ValueError(f"'min' ({self.min}) cannot be greater than 'max' ({self.max})")
        return self

    def referenced_columns(self) -> tuple[str, ...]:
        return (self.column,)

    def required_column_types(self) -> tuple[tuple[str, ColumnCategory], ...]:
        return ((self.column, "string"),)


class RowCountSpec(BaseCheckSpec):
    check_type: ClassVar[str] = "row_count"
    supports_tolerance: ClassVar[bool] = False

    min: int | None = Field(default=None, ge=0)
    max: int | None = Field(default=None, ge=0)

    @model_validator(mode="after")
    def _validate_bounds(self) -> RowCountSpec:
        if self.min is None and self.max is None:
            raise ValueError("needs at least 'min' or 'max'")
        if self.min is not None and self.max is not None and self.min > self.max:
            raise ValueError(f"'min' ({self.min}) cannot be greater than 'max' ({self.max})")
        return self


class ExpectedColumnsSpec(BaseCheckSpec):
    check_type: ClassVar[str] = "expected_columns"
    supports_tolerance: ClassVar[bool] = False

    columns: list[str] = Field(min_length=1)
    exact: bool = False

    @classmethod
    def coerce_payload(cls, payload: Any) -> Any:
        if isinstance(payload, list):
            return {"columns": payload}
        return payload

    # No referenced_columns(): this check asserts which columns exist, so its
    # columns must NOT be required to pre-exist by the schema pre-check.


class FreshnessSpec(BaseCheckSpec):
    check_type: ClassVar[str] = "freshness"
    supports_tolerance: ClassVar[bool] = False

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


class RelationshipSpec(BaseCheckSpec):
    check_type: ClassVar[str] = "relationship"

    column: str = Field(min_length=1)
    to: str = Field(min_length=1)
    to_column: str = Field(min_length=1)

    def referenced_columns(self) -> tuple[str, ...]:
        return (self.column,)

    def referenced_foreign(self) -> tuple[tuple[str, str], ...]:
        return ((self.to, self.to_column),)

    def validate_against_sources(self, sources: dict[str, SourceSpec]) -> None:
        if self.to not in sources:
            declared = ", ".join(sorted(sources))
            raise ValueError(
                f"'to' references source '{self.to}', which is not declared in "
                f"'sources'. Declared sources: {declared}"
            )


CHECK_SPEC_TYPES: dict[str, type[BaseCheckSpec]] = {
    cls.check_type: cls
    for cls in (
        NotNullSpec,
        UniqueSpec,
        AcceptedValuesSpec,
        RangeSpec,
        RegexMatchSpec,
        NotEmptyStringSpec,
        StringLengthSpec,
        RowCountSpec,
        ExpectedColumnsSpec,
        FreshnessSpec,
        RelationshipSpec,
        CustomSqlSpec,
    )
}


class DefaultsSpec(BaseModel):
    """Suite-wide defaults applied to checks that don't set the field themselves."""

    model_config = ConfigDict(extra="forbid")

    severity: Severity = "error"


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
        parsed = [
            _parse_check_entry(f"{location}[{index}]", entry)
            for index, entry in enumerate(entries)
        ]
        for index, spec in enumerate(parsed):
            try:
                spec.validate_against_sources(sources)
            except ValueError as exc:
                _fail(f"{location}[{index}] ({spec.check_type})", str(exc))
        checks[source_name] = parsed
    return checks


def _parse_defaults(raw: Any) -> DefaultsSpec:
    if raw is None:
        return DefaultsSpec()
    if not isinstance(raw, dict):
        _fail("defaults", "must be a mapping (e.g. 'defaults: {severity: warn}')")
    try:
        return DefaultsSpec.model_validate(raw)
    except ValidationError as exc:
        _fail("defaults", _format_validation_error(exc))


def _apply_defaults(
    checks: dict[str, list[BaseCheckSpec]], defaults: DefaultsSpec
) -> dict[str, list[BaseCheckSpec]]:
    """Fill in a check's severity from the suite default when it set none."""
    if defaults.severity == "error":
        return checks
    return {
        source: [
            spec.model_copy(update={"severity": defaults.severity})
            if "severity" not in spec.model_fields_set
            else spec
            for spec in entries
        ]
        for source, entries in checks.items()
    }


def parse_suite(data: Any) -> SuiteSpec:
    """Validate the already-deserialized suite structure and build the model."""
    if not isinstance(data, dict):
        raise SpecError(
            "the suite must be a YAML mapping with the keys 'version', 'sources' and 'checks' "
            "(see docs/spec.yaml.md)"
        )
    unknown = set(data) - {"version", "sources", "checks", "defaults"}
    if unknown:
        _fail(
            "suite",
            f"unknown keys: {', '.join(sorted(str(key) for key in unknown))}. "
            "Valid keys: version, sources, checks, defaults",
        )
    if "version" not in data:
        _fail("version", "missing; add 'version: 1' at the top of the suite")
    if data["version"] != SUPPORTED_VERSION:
        _fail(
            "version",
            f"unsupported version: {data['version']!r}. "
            f"This quacklint version supports 'version: {SUPPORTED_VERSION}'",
        )
    defaults = _parse_defaults(data.get("defaults"))
    sources = _parse_sources(data.get("sources"))
    checks = _apply_defaults(_parse_checks(data.get("checks"), sources), defaults)
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


def _mentions_identifier(sql: str, name: str) -> bool:
    """Whether `sql` references the identifier `name` (quoted or bare).

    Deliberately generous: a false positive only costs us the column pruning,
    while a false negative would prune a column the SQL needs.
    """
    return re.search(rf"(?<![A-Za-z0-9_]){re.escape(name)}(?![A-Za-z0-9_])", sql) is not None


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

    def select(self, tags: Collection[str]) -> Suite:
        """Return a Suite keeping only checks that carry at least one of `tags`.

        An empty `tags` selects everything (no filtering).
        """
        if not tags:
            return self
        wanted = set(tags)
        filtered = {
            source: kept
            for source, entries in self.spec.checks.items()
            if (kept := [spec for spec in entries if wanted.intersection(spec.tags)])
        }
        return Suite(spec=self.spec.model_copy(update={"checks": filtered}), base_dir=self.base_dir)

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

    def required_columns(self) -> dict[str, tuple[str, ...] | None]:
        """Per source, the columns its checks reference. None means 'all of them'.

        Loading only these is what keeps a wide table from crossing the network
        in full. It is sound only when every consumer of a source has declared
        what it touches, so two cases opt a source out:

        - `expected_columns` asserts which columns exist, so it has to see the
          real schema rather than the subset we chose to load;
        - `custom_sql` runs arbitrary SQL, so any source its query names may
          reference columns no spec declared.
        """
        opaque: set[str] = set()
        required: dict[str, set[str]] = {name: set() for name in self.spec.sources}
        for source_name, entries in self.spec.checks.items():
            for check_spec in entries:
                if isinstance(check_spec, ExpectedColumnsSpec):
                    opaque.add(source_name)
                elif isinstance(check_spec, CustomSqlSpec):
                    opaque.update(
                        name
                        for name in self.spec.sources
                        if _mentions_identifier(check_spec.query, name)
                    )
                required[source_name].update(check_spec.referenced_columns())
                for fsource, fcolumn in check_spec.referenced_foreign():
                    required[fsource].add(fcolumn)
        return {
            name: None if name in opaque or not columns else tuple(sorted(columns))
            for name, columns in required.items()
        }

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
            # Validate against the real schema while the sources are still
            # pass-through views: a pruned copy would only expose the columns
            # the checks asked for, turning a typo into an opaque remote error.
            self._check_referenced_columns(conn)
            materialize_sources(conn, self.spec.sources, self.required_columns())
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
                for fsource, fcolumn in check_spec.referenced_foreign():
                    ftypes = view_column_types(conn, fsource)
                    if fcolumn not in ftypes:
                        raise SpecError(
                            f"{location}: column '{fcolumn}' does not exist in source "
                            f"'{fsource}'. Available columns: {', '.join(ftypes)}"
                        )
