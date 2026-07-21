"""Suite parsing and validation tests (without running any check)."""

from __future__ import annotations

from datetime import timedelta
from pathlib import Path
from textwrap import dedent
from typing import Any

import pytest
import yaml

from quacklint.errors import SpecError
from quacklint.suite import (
    AcceptedValuesSpec,
    CustomSqlSpec,
    FreshnessSpec,
    NotNullSpec,
    RangeSpec,
    RegexMatchSpec,
    RelationshipSpec,
    RowCountSpec,
    SuiteSpec,
    UniqueSpec,
    load_suite,
    parse_duration,
    parse_suite,
)

FULL_SUITE = dedent(
    """\
    version: 1
    sources:
      trips:
        path: data/trips.parquet
    checks:
      trips:
        - not_null: [trip_id, pickup_ts]
        - unique: trip_id
        - accepted_values:
            column: payment_type
            values: [card, cash]
        - range: {column: fare, min: 0, max: 1000}
        - freshness: {column: pickup_ts, max_age: 24h}
        - custom_sql:
            name: no_negative_duration
            query: SELECT * FROM trips WHERE dropoff_ts < pickup_ts
    """
)


def parse(text: str) -> SuiteSpec:
    data: Any = yaml.safe_load(dedent(text))
    return parse_suite(data)


# ---------------------------------------------------------------------------
# Valid suites
# ---------------------------------------------------------------------------


def test_full_suite_parses_into_typed_models() -> None:
    spec = parse(FULL_SUITE)

    assert spec.version == 1
    assert spec.sources["trips"].path == "data/trips.parquet"

    entries = spec.checks["trips"]
    assert len(entries) == 6

    not_null = entries[0]
    assert isinstance(not_null, NotNullSpec)
    assert not_null.columns == ["trip_id", "pickup_ts"]

    unique = entries[1]
    assert isinstance(unique, UniqueSpec)
    assert unique.columns == ["trip_id"]

    accepted = entries[2]
    assert isinstance(accepted, AcceptedValuesSpec)
    assert accepted.column == "payment_type"
    assert accepted.values == ["card", "cash"]

    fare_range = entries[3]
    assert isinstance(fare_range, RangeSpec)
    assert fare_range.column == "fare"
    assert fare_range.min == 0
    assert fare_range.max == 1000

    freshness = entries[4]
    assert isinstance(freshness, FreshnessSpec)
    assert freshness.column == "pickup_ts"
    assert freshness.max_age == timedelta(hours=24)

    custom = entries[5]
    assert isinstance(custom, CustomSqlSpec)
    assert custom.name == "no_negative_duration"
    assert custom.query.startswith("SELECT")


def test_database_source_parses() -> None:
    spec = parse(
        """
        version: 1
        sources:
          customers:
            type: postgres
            connection: "host=h dbname=d"
            table: public.customers
        """
    )
    src = spec.sources["customers"]
    assert src.is_database
    assert src.type == "postgres"
    assert src.table == "public.customers"


def test_source_cannot_mix_path_and_database() -> None:
    with pytest.raises(SpecError, match="not both"):
        parse(
            """
            version: 1
            sources:
              x: {path: t.csv, type: postgres}
            """
        )


def test_database_source_requires_all_fields() -> None:
    with pytest.raises(SpecError, match="needs 'path'"):
        parse(
            """
            version: 1
            sources:
              x: {type: postgres, connection: "c"}
            """
        )


def test_minimal_suite_without_checks() -> None:
    spec = parse(
        """
        version: 1
        sources:
          trips:
            path: data/trips.parquet
        """
    )
    assert spec.checks == {}


def test_not_null_accepts_single_column_shorthand() -> None:
    spec = parse(
        """
        version: 1
        sources:
          t: {path: t.csv}
        checks:
          t:
            - not_null: a
        """
    )
    check = spec.checks["t"][0]
    assert isinstance(check, NotNullSpec)
    assert check.columns == ["a"]


def test_unique_accepts_composite_key_list() -> None:
    spec = parse(
        """
        version: 1
        sources:
          t: {path: t.csv}
        checks:
          t:
            - unique: [a, b]
        """
    )
    check = spec.checks["t"][0]
    assert isinstance(check, UniqueSpec)
    assert check.columns == ["a", "b"]


def test_range_with_only_min_is_valid() -> None:
    spec = parse(
        """
        version: 1
        sources:
          t: {path: t.csv}
        checks:
          t:
            - range: {column: fare, min: 0}
        """
    )
    check = spec.checks["t"][0]
    assert isinstance(check, RangeSpec)
    assert check.min == 0
    assert check.max is None


def test_row_count_parses() -> None:
    spec = parse(
        """
        version: 1
        sources:
          t: {path: t.csv}
        checks:
          t:
            - row_count: {min: 1, max: 100}
        """
    )
    check = spec.checks["t"][0]
    assert isinstance(check, RowCountSpec)
    assert check.min == 1
    assert check.max == 100


def test_row_count_requires_min_or_max() -> None:
    with pytest.raises(SpecError, match="at least 'min' or 'max'"):
        parse(
            """
            version: 1
            sources:
              t: {path: t.csv}
            checks:
              t:
                - row_count: {}
            """
        )


def test_regex_match_parses() -> None:
    spec = parse(
        """
        version: 1
        sources:
          t: {path: t.csv}
        checks:
          t:
            - regex_match: {column: trip_id, pattern: 't-[0-9]{3}'}
        """
    )
    check = spec.checks["t"][0]
    assert isinstance(check, RegexMatchSpec)
    assert check.column == "trip_id"
    assert check.pattern == "t-[0-9]{3}"


def test_regex_match_rejects_invalid_pattern() -> None:
    with pytest.raises(SpecError, match="invalid regular expression"):
        parse(
            """
            version: 1
            sources:
              t: {path: t.csv}
            checks:
              t:
                - regex_match: {column: trip_id, pattern: '('}
            """
        )


def test_severity_defaults_to_error() -> None:
    spec = parse(
        """
        version: 1
        sources:
          t: {path: t.csv}
        checks:
          t:
            - unique: id
        """
    )
    assert spec.checks["t"][0].severity == "error"


def test_severity_warn_round_trips() -> None:
    spec = parse(
        """
        version: 1
        sources:
          t: {path: t.csv}
        checks:
          t:
            - row_count: {min: 1, severity: warn}
        """
    )
    assert spec.checks["t"][0].severity == "warn"


def test_severity_rejects_invalid_value() -> None:
    with pytest.raises(SpecError, match="severity"):
        parse(
            """
            version: 1
            sources:
              t: {path: t.csv}
            checks:
              t:
                - row_count: {min: 1, severity: loud}
            """
        )


def test_tags_parse() -> None:
    spec = parse(
        """
        version: 1
        sources:
          t: {path: t.csv}
        checks:
          t:
            - unique: {columns: [id], tags: [critical, pk]}
        """
    )
    assert spec.checks["t"][0].tags == ["critical", "pk"]


def test_default_severity_applied_when_unset() -> None:
    spec = parse(
        """
        version: 1
        defaults: {severity: warn}
        sources:
          t: {path: t.csv}
        checks:
          t:
            - unique: id
            - row_count: {min: 1, severity: error}
        """
    )
    assert spec.checks["t"][0].severity == "warn"   # inherited default
    assert spec.checks["t"][1].severity == "error"  # explicit wins


def test_defaults_rejects_unknown_key() -> None:
    with pytest.raises(SpecError, match="defaults"):
        parse(
            """
            version: 1
            defaults: {severities: warn}
            sources:
              t: {path: t.csv}
            """
        )


def test_relationship_parses() -> None:
    spec = parse(
        """
        version: 1
        sources:
          orders: {path: orders.csv}
          customers: {path: customers.csv}
        checks:
          orders:
            - relationship: {column: customer_id, to: customers, to_column: id}
        """
    )
    check = spec.checks["orders"][0]
    assert isinstance(check, RelationshipSpec)
    assert (check.to, check.to_column) == ("customers", "id")


def test_relationship_undeclared_target_rejected_at_parse() -> None:
    with pytest.raises(SpecError, match=r"'to' references source 'nope'.*not declared"):
        parse(
            """
            version: 1
            sources:
              orders: {path: orders.csv}
            checks:
              orders:
                - relationship: {column: customer_id, to: nope, to_column: id}
            """
        )


def test_tolerance_parses() -> None:
    spec = parse(
        """
        version: 1
        sources:
          t: {path: t.csv}
        checks:
          t:
            - not_null: {columns: [a], max_failed_rows: 5, max_failed_pct: 1.5}
        """
    )
    check = spec.checks["t"][0]
    assert check.max_failed_rows == 5
    assert check.max_failed_pct == 1.5


def test_tolerance_rejected_on_row_count() -> None:
    with pytest.raises(SpecError, match="row_count does not support tolerance"):
        parse(
            """
            version: 1
            sources:
              t: {path: t.csv}
            checks:
              t:
                - row_count: {min: 1, max_failed_rows: 5}
            """
        )


def test_tolerance_rejected_on_freshness() -> None:
    with pytest.raises(SpecError, match="freshness does not support tolerance"):
        parse(
            """
            version: 1
            sources:
              t: {path: t.csv}
            checks:
              t:
                - freshness: {column: ts, max_age: 24h, max_failed_pct: 10}
            """
        )


def test_custom_sql_strips_trailing_semicolon() -> None:
    spec = parse(
        """
        version: 1
        sources:
          t: {path: t.csv}
        checks:
          t:
            - custom_sql: {name: my_check, query: 'SELECT * FROM t WHERE x < 0;'}
        """
    )
    check = spec.checks["t"][0]
    assert isinstance(check, CustomSqlSpec)
    assert check.query == "SELECT * FROM t WHERE x < 0"


def test_load_suite_from_fixture_file(fixtures_dir: Path) -> None:
    spec = load_suite(fixtures_dir / "valid_suite.yaml")
    assert len(spec.checks["trips"]) == 6


def test_example_suite_parses() -> None:
    example = Path(__file__).parents[1] / "examples" / "taxi" / "quacklint.yaml"
    spec = load_suite(example)
    assert "trips" in spec.sources


# ---------------------------------------------------------------------------
# Invalid suites
# ---------------------------------------------------------------------------


def test_top_level_must_be_mapping() -> None:
    with pytest.raises(SpecError, match="YAML mapping"):
        parse("- version: 1")


def test_unknown_top_level_key() -> None:
    with pytest.raises(SpecError, match="unknown keys: cheks"):
        parse(
            """
            version: 1
            sources:
              t: {path: t.csv}
            cheks: {}
            """
        )


def test_missing_version() -> None:
    with pytest.raises(SpecError, match="version: missing"):
        parse(
            """
            sources:
              t: {path: t.csv}
            """
        )


def test_unsupported_version() -> None:
    with pytest.raises(SpecError, match="unsupported version: 2"):
        parse(
            """
            version: 2
            sources:
              t: {path: t.csv}
            """
        )


def test_missing_sources_section() -> None:
    with pytest.raises(SpecError, match="sources: section is missing"):
        parse("version: 1")


def test_empty_sources_section() -> None:
    with pytest.raises(SpecError, match="at least one source"):
        parse(
            """
            version: 1
            sources: {}
            """
        )


def test_invalid_source_name() -> None:
    with pytest.raises(SpecError, match="invalid source name"):
        parse(
            """
            version: 1
            sources:
              1trips: {path: t.csv}
            """
        )


def test_source_without_path() -> None:
    with pytest.raises(SpecError, match=r"sources\.trips"):
        parse(
            """
            version: 1
            sources:
              trips: {}
            """
        )


def test_source_with_unknown_field() -> None:
    with pytest.raises(SpecError, match=r"sources\.trips"):
        parse(
            """
            version: 1
            sources:
              trips: {path: t.csv, format: parquet}
            """
        )


def test_checks_for_undeclared_source() -> None:
    with pytest.raises(SpecError, match="is not declared in 'sources'"):
        parse(
            """
            version: 1
            sources:
              trips: {path: t.csv}
            checks:
              rides:
                - unique: id
            """
        )


def test_unknown_check_type_lists_available_checks() -> None:
    with pytest.raises(SpecError, match=r"unknown check: 'nonnull'.*Available"):
        parse(
            """
            version: 1
            sources:
              t: {path: t.csv}
            checks:
              t:
                - nonnull: [a]
            """
        )


def test_check_entry_with_multiple_keys() -> None:
    with pytest.raises(SpecError, match="single key"):
        parse(
            """
            version: 1
            sources:
              t: {path: t.csv}
            checks:
              t:
                - not_null: [a]
                  unique: a
            """
        )


def test_error_location_includes_check_index() -> None:
    with pytest.raises(SpecError, match=r"checks\.t\[1\]"):
        parse(
            """
            version: 1
            sources:
              t: {path: t.csv}
            checks:
              t:
                - unique: a
                - bogus_check: a
            """
        )


def test_accepted_values_requires_values() -> None:
    with pytest.raises(SpecError, match="accepted_values"):
        parse(
            """
            version: 1
            sources:
              t: {path: t.csv}
            checks:
              t:
                - accepted_values: {column: a}
            """
        )


def test_accepted_values_rejects_empty_list() -> None:
    with pytest.raises(SpecError, match="accepted_values"):
        parse(
            """
            version: 1
            sources:
              t: {path: t.csv}
            checks:
              t:
                - accepted_values: {column: a, values: []}
            """
        )


def test_range_requires_min_or_max() -> None:
    with pytest.raises(SpecError, match="at least 'min' or 'max'"):
        parse(
            """
            version: 1
            sources:
              t: {path: t.csv}
            checks:
              t:
                - range: {column: fare}
            """
        )


def test_range_rejects_min_greater_than_max() -> None:
    with pytest.raises(SpecError, match="cannot be greater"):
        parse(
            """
            version: 1
            sources:
              t: {path: t.csv}
            checks:
              t:
                - range: {column: fare, min: 10, max: 5}
            """
        )


def test_freshness_rejects_invalid_duration() -> None:
    with pytest.raises(SpecError, match="freshness"):
        parse(
            """
            version: 1
            sources:
              t: {path: t.csv}
            checks:
              t:
                - freshness: {column: ts, max_age: 24 horas}
            """
        )


def test_freshness_rejects_bare_number() -> None:
    with pytest.raises(SpecError, match="max_age"):
        parse(
            """
            version: 1
            sources:
              t: {path: t.csv}
            checks:
              t:
                - freshness: {column: ts, max_age: 24}
            """
        )


def test_custom_sql_requires_query() -> None:
    with pytest.raises(SpecError, match="custom_sql"):
        parse(
            """
            version: 1
            sources:
              t: {path: t.csv}
            checks:
              t:
                - custom_sql: {name: my_check}
            """
        )


def test_custom_sql_rejects_invalid_name() -> None:
    with pytest.raises(SpecError, match="custom_sql"):
        parse(
            """
            version: 1
            sources:
              t: {path: t.csv}
            checks:
              t:
                - custom_sql: {name: "my check!", query: SELECT 1}
            """
        )


# ---------------------------------------------------------------------------
# load_suite: file and YAML syntax
# ---------------------------------------------------------------------------


def test_load_suite_missing_file(tmp_path: Path) -> None:
    with pytest.raises(SpecError, match="does not exist"):
        load_suite(tmp_path / "nope.yaml")


def test_load_suite_invalid_yaml_syntax(tmp_path: Path) -> None:
    suite_file = tmp_path / "broken.yaml"
    suite_file.write_text("version: 1\nsources: [unclosed", encoding="utf-8")
    with pytest.raises(SpecError, match="invalid YAML"):
        load_suite(suite_file)


# ---------------------------------------------------------------------------
# parse_duration
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("90s", timedelta(seconds=90)),
        ("15m", timedelta(minutes=15)),
        ("24h", timedelta(hours=24)),
        ("7d", timedelta(days=7)),
    ],
)
def test_parse_duration_valid(raw: str, expected: timedelta) -> None:
    assert parse_duration(raw) == expected


@pytest.mark.parametrize("raw", ["24 h", "1w", "1.5h", "h", "-5h", ""])
def test_parse_duration_invalid(raw: str) -> None:
    with pytest.raises(ValueError, match="invalid duration"):
        parse_duration(raw)
