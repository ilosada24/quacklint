"""Built-in checks from the contract (docs/spec.yaml.md).

Implemented: not_null, unique, row_count, accepted_values, range,
regex_match, freshness and custom_sql.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from quacklint.checks.base import Check, CheckResult, quote_ident, register
from quacklint.suite import (
    AcceptedValuesSpec,
    BaseCheckSpec,
    CustomSqlSpec,
    FreshnessSpec,
    NotNullSpec,
    RangeSpec,
    RegexMatchSpec,
    RowCountSpec,
    UniqueSpec,
    format_duration,
)

if TYPE_CHECKING:
    from datetime import timedelta

    import duckdb


def _sql_literal(value: str | int | float | bool) -> str:
    """Turn a scalar from the YAML into a DuckDB SQL literal."""
    if isinstance(value, bool):
        return "TRUE" if value else "FALSE"
    if isinstance(value, int | float):
        return str(value)
    return "'" + value.replace("'", "''") + "'"


@register("not_null")
class NotNullCheck(Check):
    """The given columns contain no NULL.

    ```yaml
    - not_null: [trip_id, pickup_ts]
    - not_null: trip_id            # shorthand: a single column
    ```

    Each row with a NULL in any of the columns counts as one violation.
    """

    def __init__(self, source: str, columns: list[str]) -> None:
        super().__init__(source)
        self.columns = columns

    @classmethod
    def from_spec(cls, source: str, spec: BaseCheckSpec) -> NotNullCheck:
        assert isinstance(spec, NotNullSpec)
        return cls(source, spec.columns)

    def to_sql(self, source: str) -> str:
        condition = " OR ".join(f"{quote_ident(col)} IS NULL" for col in self.columns)
        return f"SELECT * FROM {quote_ident(source)} WHERE {condition}"


@register("unique")
class UniqueCheck(Check):
    """No duplicates in the column or combination of columns.

    ```yaml
    - unique: trip_id              # shorthand: a single column
    - unique: [trip_id, pickup_ts] # composite key
    ```

    Rows with a NULL in any of the columns are ignored (use `not_null` to
    require non-nulls). Each duplicated key counts as one violation.
    """

    def __init__(self, source: str, columns: list[str]) -> None:
        super().__init__(source)
        self.columns = columns

    @classmethod
    def from_spec(cls, source: str, spec: BaseCheckSpec) -> UniqueCheck:
        assert isinstance(spec, UniqueSpec)
        return cls(source, spec.columns)

    def to_sql(self, source: str) -> str:
        cols = ", ".join(quote_ident(col) for col in self.columns)
        non_null = " AND ".join(f"{quote_ident(col)} IS NOT NULL" for col in self.columns)
        return (
            f"SELECT {cols}, count(*) AS occurrences FROM {quote_ident(source)} "
            f"WHERE {non_null} GROUP BY {cols} HAVING count(*) > 1"
        )


@register("accepted_values")
class AcceptedValuesCheck(Check):
    """Every non-null value of the column belongs to the given set.

    ```yaml
    - accepted_values:
        column: payment_type
        values: [card, cash]
    ```

    `values` accepts str/int/float/bool scalars. NULLs do not count as a
    violation.
    """

    def __init__(self, source: str, column: str, values: list[str | int | float | bool]) -> None:
        super().__init__(source)
        self.column = column
        self.values = values

    @classmethod
    def from_spec(cls, source: str, spec: BaseCheckSpec) -> AcceptedValuesCheck:
        assert isinstance(spec, AcceptedValuesSpec)
        return cls(source, spec.column, spec.values)

    def to_sql(self, source: str) -> str:
        col = quote_ident(self.column)
        literals = ", ".join(_sql_literal(value) for value in self.values)
        return (
            f"SELECT * FROM {quote_ident(source)} "
            f"WHERE {col} IS NOT NULL AND {col} NOT IN ({literals})"
        )


@register("range")
class RangeCheck(Check):
    """The non-null values of the column are within [min, max] (inclusive).

    ```yaml
    - range: {column: fare, min: 0, max: 1000}
    - range: {column: fare, min: 0}    # lower bound only
    ```

    At least one of `min` / `max` is required. NULLs do not count as a
    violation.
    """

    def __init__(
        self, source: str, column: str, min_value: float | None, max_value: float | None
    ) -> None:
        if min_value is None and max_value is None:
            raise ValueError("range needs at least min or max")
        super().__init__(source)
        self.column = column
        self.min_value = min_value
        self.max_value = max_value

    @classmethod
    def from_spec(cls, source: str, spec: BaseCheckSpec) -> RangeCheck:
        assert isinstance(spec, RangeSpec)
        return cls(source, spec.column, spec.min, spec.max)

    def to_sql(self, source: str) -> str:
        col = quote_ident(self.column)
        bounds: list[str] = []
        if self.min_value is not None:
            bounds.append(f"{col} < {self.min_value}")
        if self.max_value is not None:
            bounds.append(f"{col} > {self.max_value}")
        return (
            f"SELECT * FROM {quote_ident(source)} "
            f"WHERE {col} IS NOT NULL AND ({' OR '.join(bounds)})"
        )


@register("regex_match")
class RegexMatchCheck(Check):
    """Every non-null value of the column fully matches the pattern.

    ```yaml
    - regex_match: {column: trip_id, pattern: 't-[0-9]{3}'}
    ```

    Uses DuckDB's `regexp_full_match` (RE2 syntax): the pattern must match the
    whole value; add `.*` for partial searches. The pattern is validated when
    the suite is loaded. NULLs do not count as a violation.
    """

    def __init__(self, source: str, column: str, pattern: str) -> None:
        super().__init__(source)
        self.column = column
        self.pattern = pattern

    @classmethod
    def from_spec(cls, source: str, spec: BaseCheckSpec) -> RegexMatchCheck:
        assert isinstance(spec, RegexMatchSpec)
        return cls(source, spec.column, spec.pattern)

    def to_sql(self, source: str) -> str:
        col = quote_ident(self.column)
        pattern = "'" + self.pattern.replace("'", "''") + "'"
        return (
            f"SELECT * FROM {quote_ident(source)} "
            f"WHERE {col} IS NOT NULL AND NOT regexp_full_match({col}, {pattern})"
        )


@register("freshness")
class FreshnessCheck(Check):
    """The most recent value of the column is not older than max_age.

    ```yaml
    - freshness: {column: pickup_ts, max_age: 24h}
    ```

    `max_age` is a duration (`s`, `m`, `h`, `d`) and the time reference is
    DuckDB's `now()` at run time. If the source is empty or the column is all
    NULL, the check passes (use `row_count` and `not_null` for those).
    """

    def __init__(self, source: str, column: str, max_age: timedelta) -> None:
        super().__init__(source)
        self.column = column
        self.max_age = max_age

    @classmethod
    def from_spec(cls, source: str, spec: BaseCheckSpec) -> FreshnessCheck:
        assert isinstance(spec, FreshnessSpec)
        return cls(source, spec.column, spec.max_age)

    def to_sql(self, source: str) -> str:
        col = quote_ident(self.column)
        seconds = int(self.max_age.total_seconds())
        return (
            f"SELECT CAST(max({col}) AS VARCHAR) AS most_recent FROM {quote_ident(source)} "
            f"HAVING max({col}) < now() - INTERVAL '{seconds} seconds'"
        )

    def evaluate(self, conn: duckdb.DuckDBPyConnection) -> CheckResult:
        row = self._fetchone(conn, self.to_sql(self.source))
        if row is None:
            return CheckResult(
                check=self.display_name,
                source=self.source,
                passed=True,
                failed_rows=0,
                severity=self.severity,
            )
        return CheckResult(
            check=self.display_name,
            source=self.source,
            passed=False,
            failed_rows=1,
            message=(
                f"the most recent value of '{self.column}' is {row[0]}; "
                f"exceeds the max age ({format_duration(self.max_age)})"
            ),
            severity=self.severity,
        )


@register("row_count")
class RowCountCheck(Check):
    """The source's row count is within [min, max] (inclusive).

    ```yaml
    - row_count: {min: 1}
    - row_count: {min: 100, max: 100000}
    ```

    At least one of `min` / `max` is required (integers >= 0). It references no
    columns: it operates on the whole source.
    """

    def __init__(self, source: str, min_rows: int | None, max_rows: int | None) -> None:
        if min_rows is None and max_rows is None:
            raise ValueError("row_count needs at least min or max")
        super().__init__(source)
        self.min_rows = min_rows
        self.max_rows = max_rows

    @classmethod
    def from_spec(cls, source: str, spec: BaseCheckSpec) -> RowCountCheck:
        assert isinstance(spec, RowCountSpec)
        return cls(source, spec.min, spec.max)

    def to_sql(self, source: str) -> str:
        conditions: list[str] = []
        if self.min_rows is not None:
            conditions.append(f"count(*) < {self.min_rows}")
        if self.max_rows is not None:
            conditions.append(f"count(*) > {self.max_rows}")
        return (
            f"SELECT count(*) AS row_count FROM {quote_ident(source)} "
            f"HAVING {' OR '.join(conditions)}"
        )

    def evaluate(self, conn: duckdb.DuckDBPyConnection) -> CheckResult:
        row = self._fetchone(conn, self.to_sql(self.source))
        if row is None:
            return CheckResult(
                check=self.display_name,
                source=self.source,
                passed=True,
                failed_rows=0,
                severity=self.severity,
            )
        return CheckResult(
            check=self.display_name,
            source=self.source,
            passed=False,
            failed_rows=1,
            message=f"the source has {int(row[0])} row(s); expected {self._expected()}",
            severity=self.severity,
        )

    def _expected(self) -> str:
        if self.min_rows is not None and self.max_rows is not None:
            return f"between {self.min_rows} and {self.max_rows}"
        if self.min_rows is not None:
            return f"at least {self.min_rows}"
        return f"at most {self.max_rows}"


@register("custom_sql")
class CustomSqlCheck(Check):
    """Arbitrary SQL query whose result rows are the violations.

    ```yaml
    - custom_sql:
        name: no_negative_duration
        query: SELECT * FROM trips WHERE dropoff_ts < pickup_ts
    ```

    The query can reference any source by its view name, and in reports the
    check is reported under its `name`. A trailing `;` is stripped
    automatically; it must be a single SELECT.
    """

    def __init__(self, source: str, check_name: str, query: str) -> None:
        super().__init__(source)
        self.check_name = check_name
        self.query = query

    @property
    def display_name(self) -> str:
        return self.check_name

    @classmethod
    def from_spec(cls, source: str, spec: BaseCheckSpec) -> CustomSqlCheck:
        assert isinstance(spec, CustomSqlSpec)
        return cls(source, spec.name, spec.query)

    def to_sql(self, source: str) -> str:
        return self.query
