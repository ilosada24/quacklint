"""Check base class, result and decorator-based registry.

Every check compiles to DuckDB SQL that returns the rows that VIOLATE the rule:
the check passes if that query returns no rows. Data is never loaded into
Python/pandas to validate.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from collections.abc import Callable
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, ClassVar, Literal, TypeVar

import duckdb

from quacklint.errors import ExecutionError

if TYPE_CHECKING:
    from quacklint.suite import BaseCheckSpec

Severity = Literal["error", "warn"]
"""A check's severity: 'error' affects the exit code; 'warn' only reports."""


def quote_ident(name: str) -> str:
    """Quote an identifier (column, view) for DuckDB SQL."""
    return '"' + name.replace('"', '""') + '"'


@dataclass(frozen=True, slots=True)
class CheckResult:
    """Result of evaluating a check against a source."""

    check: str
    source: str
    passed: bool
    failed_rows: int
    message: str = ""
    sample_columns: tuple[str, ...] = ()
    sample_rows: tuple[tuple[object, ...], ...] = ()
    severity: Severity = "error"
    tolerated: bool = False


class Check(ABC):
    """A runnable check over a source (DuckDB view)."""

    name: ClassVar[str]
    sample_limit: ClassVar[int] = 5

    def __init__(self, source: str, severity: Severity = "error") -> None:
        self.source = source
        self.severity = severity
        # Set from the spec by build_check; None means "no tolerance".
        self.max_failed_rows: int | None = None
        self.max_failed_pct: float | None = None

    @property
    def display_name(self) -> str:
        """Name the check is reported under (custom_sql uses its own)."""
        return self.name

    @classmethod
    @abstractmethod
    def from_spec(cls, source: str, spec: BaseCheckSpec) -> Check:
        """Build the check from its validated configuration model."""

    @abstractmethod
    def to_sql(self, source: str) -> str:
        """DuckDB SQL whose result rows are the violations over `source`."""

    def evaluate(self, conn: duckdb.DuckDBPyConnection) -> CheckResult:
        """Evaluate the check by counting the rows that violate the rule.

        If there are violations, attach up to `sample_limit` sample rows.
        """
        sql = self.to_sql(self.source)
        row = self._fetchone(conn, f"SELECT count(*) FROM ({sql}) AS violations")
        failed_rows = int(row[0]) if row is not None else 0
        if failed_rows == 0:
            return CheckResult(
                check=self.display_name,
                source=self.source,
                passed=True,
                failed_rows=0,
                severity=self.severity,
            )
        columns, sample = self._sample(conn, sql)
        tolerated = self._within_tolerance(conn, failed_rows)
        base = f"{failed_rows} row(s) violate the rule"
        message = (
            f"{base}, within tolerance ({self._tolerance_desc()})"
            if tolerated
            else f"{base}{self._tolerance_suffix()}"
        )
        return CheckResult(
            check=self.display_name,
            source=self.source,
            passed=tolerated,
            failed_rows=failed_rows,
            message=message,
            sample_columns=columns,
            sample_rows=sample,
            severity=self.severity,
            tolerated=tolerated,
        )

    def _within_tolerance(self, conn: duckdb.DuckDBPyConnection, failed_rows: int) -> bool:
        """Whether `failed_rows` stays within every configured tolerance limit."""
        if self.max_failed_rows is None and self.max_failed_pct is None:
            return False
        if self.max_failed_rows is not None and failed_rows > self.max_failed_rows:
            return False
        if self.max_failed_pct is not None:
            total = self._total_rows(conn)
            pct = (100.0 * failed_rows / total) if total else 0.0
            if pct > self.max_failed_pct:
                return False
        return True

    def _total_rows(self, conn: duckdb.DuckDBPyConnection) -> int:
        row = self._fetchone(conn, f"SELECT count(*) FROM {quote_ident(self.source)}")
        return int(row[0]) if row is not None else 0

    def _tolerance_desc(self) -> str:
        parts: list[str] = []
        if self.max_failed_rows is not None:
            parts.append(f"max_failed_rows={self.max_failed_rows}")
        if self.max_failed_pct is not None:
            parts.append(f"max_failed_pct={self.max_failed_pct}")
        return ", ".join(parts)

    def _tolerance_suffix(self) -> str:
        return f" (exceeds tolerance: {self._tolerance_desc()})" if self._tolerance_desc() else ""

    def _sample(
        self, conn: duckdb.DuckDBPyConnection, sql: str
    ) -> tuple[tuple[str, ...], tuple[tuple[object, ...], ...]]:
        # COLUMNS(*)::VARCHAR: values come back as text, avoiding DuckDB→Python
        # type conversions (e.g. TIMESTAMPTZ requires pytz).
        # ORDER BY ALL makes the sampled rows deterministic across runs.
        sample_sql = (
            f"SELECT COLUMNS(*)::VARCHAR FROM ({sql}) AS violations "
            f"ORDER BY ALL LIMIT {self.sample_limit}"
        )
        try:
            cursor = conn.execute(sample_sql)
            rows = cursor.fetchall()
            columns = tuple(str(desc[0]) for desc in cursor.description or [])
        except duckdb.Error as exc:
            raise ExecutionError(
                f"check '{self.name}' on '{self.source}': DuckDB error: {exc}"
            ) from exc
        return columns, tuple(tuple(item) for item in rows)

    def _fetchone(
        self, conn: duckdb.DuckDBPyConnection, sql: str
    ) -> tuple[Any, ...] | None:
        try:
            return conn.execute(sql).fetchone()
        except duckdb.Error as exc:
            raise ExecutionError(
                f"check '{self.name}' on '{self.source}': DuckDB error: {exc}"
            ) from exc


_CheckT = TypeVar("_CheckT", bound=Check)

_REGISTRY: dict[str, type[Check]] = {}


def register(name: str) -> Callable[[type[_CheckT]], type[_CheckT]]:
    """Decorator that registers a check implementation under its YAML name."""

    def decorator(cls: type[_CheckT]) -> type[_CheckT]:
        if name in _REGISTRY:
            raise ValueError(f"duplicate check in registry: '{name}'")
        cls.name = name
        _REGISTRY[name] = cls
        return cls

    return decorator


def get_check(name: str) -> type[Check]:
    """Return the registered implementation for a check name."""
    try:
        return _REGISTRY[name]
    except KeyError:
        available = ", ".join(sorted(_REGISTRY)) or "(none)"
        raise ExecutionError(
            f"check '{name}' has no runnable implementation yet. "
            f"Implemented checks: {available}"
        ) from None


def build_check(source: str, spec: BaseCheckSpec) -> Check:
    """Instantiate the registered implementation for a check configuration."""
    check = get_check(spec.check_type).from_spec(source, spec)
    check.severity = spec.severity
    check.max_failed_rows = spec.max_failed_rows
    check.max_failed_pct = spec.max_failed_pct
    return check


def registered_checks() -> dict[str, type[Check]]:
    """Copy of the current check registry."""
    return dict(_REGISTRY)
