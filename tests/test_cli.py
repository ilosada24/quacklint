"""CLI tests with CliRunner (typer.testing)."""

from __future__ import annotations

import json
from pathlib import Path
from textwrap import dedent

import duckdb
import pytest
from typer.testing import CliRunner

from quacklint.cli import app

runner = CliRunner()


def _write_parquet(path: Path, query: str) -> None:
    conn = duckdb.connect()
    try:
        conn.execute(f"COPY ({query}) TO '{path.as_posix()}' (FORMAT parquet)")
    finally:
        conn.close()


def _write_suite(directory: Path, checks_yaml: str) -> Path:
    header = dedent(
        """\
        version: 1
        sources:
          trips:
            path: trips.parquet
        checks:
          trips:
        """
    )
    suite_file = directory / "suite.yaml"
    suite_file.write_text(header + checks_yaml + "\n", encoding="utf-8")
    return suite_file


@pytest.fixture()
def failing_suite(tmp_path: Path) -> Path:
    """not_null fails (1 NULL), unique fails ('a' duplicated), row_count passes."""
    _write_parquet(
        tmp_path / "trips.parquet",
        "SELECT * FROM (VALUES ('a', 1), ('a', 2), (NULL, 3)) AS v(trip_id, n)",
    )
    return _write_suite(
        tmp_path,
        "    - not_null: trip_id\n"
        "    - unique: trip_id\n"
        "    - row_count: {min: 1}",
    )


@pytest.fixture()
def passing_suite(tmp_path: Path) -> Path:
    _write_parquet(
        tmp_path / "trips.parquet",
        "SELECT * FROM (VALUES ('a', 1), ('b', 2)) AS v(trip_id, n)",
    )
    return _write_suite(
        tmp_path,
        "    - not_null: trip_id\n"
        "    - unique: trip_id\n"
        "    - row_count: {min: 1}",
    )


# ---------------------------------------------------------------------------
# run: table
# ---------------------------------------------------------------------------


def test_run_table_all_pass_exit_0(passing_suite: Path) -> None:
    result = runner.invoke(app, ["run", str(passing_suite)])
    assert result.exit_code == 0
    assert "PASS" in result.output
    assert "FAIL" not in result.output
    assert "3/3 checks OK" in result.output


def test_run_table_reports_failures_exit_1(failing_suite: Path) -> None:
    result = runner.invoke(app, ["run", str(failing_suite)])
    assert result.exit_code == 1
    assert "FAIL" in result.output
    assert "unique" in result.output
    assert "not_null" in result.output
    assert "1/3 checks OK" in result.output
    # The table includes sample rows of the violations.
    assert "sample" in result.output


# ---------------------------------------------------------------------------
# run: --format json / junit
# ---------------------------------------------------------------------------


def test_run_json_output(failing_suite: Path) -> None:
    result = runner.invoke(app, ["run", str(failing_suite), "--format", "json"])
    assert result.exit_code == 1
    payload = json.loads(result.output)
    assert payload["passed"] is False
    assert payload["total"] == 3
    assert payload["failed"] == 2
    by_check = {item["check"]: item for item in payload["checks"]}
    assert by_check["row_count"]["passed"] is True
    assert by_check["unique"]["failed_rows"] == 1
    assert by_check["unique"]["sample"][0]["trip_id"] == "a"


def test_run_json_exit_0_when_all_pass(passing_suite: Path) -> None:
    result = runner.invoke(app, ["run", str(passing_suite), "-f", "json"])
    assert result.exit_code == 0
    assert json.loads(result.output)["passed"] is True


def test_run_junit_output(failing_suite: Path) -> None:
    result = runner.invoke(app, ["run", str(failing_suite), "--format", "junit"])
    assert result.exit_code == 1
    assert result.output.startswith('<?xml version="1.0" encoding="UTF-8"?>')
    assert 'failures="2"' in result.output
    assert 'name="unique"' in result.output
    assert "<failure" in result.output


# ---------------------------------------------------------------------------
# run: --explain
# ---------------------------------------------------------------------------


def test_run_explain_prints_sql_without_executing(failing_suite: Path) -> None:
    result = runner.invoke(app, ["run", str(failing_suite), "--explain"])
    assert result.exit_code == 0  # nothing runs, so no failed checks
    assert "-- not_null (trips)" in result.output
    assert "-- unique (trips)" in result.output
    assert "IS NULL" in result.output
    assert "HAVING count(*) > 1" in result.output
    assert "PASS" not in result.output
    assert "FAIL" not in result.output


def test_run_explain_works_without_data_files(tmp_path: Path) -> None:
    """--explain doesn't touch the sources: it compiles even if the data file is missing."""
    suite_file = _write_suite(tmp_path, "    - unique: trip_id")
    result = runner.invoke(app, ["run", str(suite_file), "--explain"])
    assert result.exit_code == 0
    assert "HAVING count(*) > 1" in result.output


# ---------------------------------------------------------------------------
# run: --fail-fast
# ---------------------------------------------------------------------------


def test_run_fail_fast_stops_at_first_failure(failing_suite: Path) -> None:
    result = runner.invoke(app, ["run", str(failing_suite), "--fail-fast"])
    assert result.exit_code == 1
    assert "not_null" in result.output
    assert "unique" not in result.output
    assert "0/1 checks OK" in result.output


def test_run_fail_fast_does_not_stop_on_warning(tmp_path: Path) -> None:
    """A failed warn doesn't stop --fail-fast; it continues to the first error."""
    _write_parquet(
        tmp_path / "trips.parquet",
        "SELECT * FROM (VALUES ('a', 1), ('a', 2), (NULL, 3)) AS v(trip_id, n)",
    )
    suite = _write_suite(
        tmp_path,
        "    - unique: {columns: [trip_id], severity: warn}\n"  # fails (warn) first
        "    - not_null: trip_id",  # fails (error): should stop here
    )
    result = runner.invoke(app, ["run", str(suite), "--fail-fast"])
    assert result.exit_code == 1
    assert "WARN" in result.output  # the warning ran and was reported
    assert "not_null" in result.output  # continued to the error
    assert "0/2 checks OK, 1 warning(s)" in result.output


# ---------------------------------------------------------------------------
# tags + --select
# ---------------------------------------------------------------------------


def test_run_select_filters_by_tag(tmp_path: Path) -> None:
    _write_parquet(
        tmp_path / "trips.parquet",
        "SELECT * FROM (VALUES ('a', 1), ('a', 2), (NULL, 3)) AS v(trip_id, n)",
    )
    suite = _write_suite(
        tmp_path,
        "    - unique: {columns: [trip_id], tags: [critical]}\n"
        "    - not_null: {columns: [trip_id], tags: [other]}",
    )
    result = runner.invoke(app, ["run", str(suite), "--select", "critical", "-f", "json"])
    payload = json.loads(result.output)
    assert payload["total"] == 1
    assert payload["checks"][0]["check"] == "unique"


def test_run_select_no_match_warns_and_exits_0(tmp_path: Path) -> None:
    _write_parquet(
        tmp_path / "trips.parquet",
        "SELECT * FROM (VALUES ('a', 1)) AS v(trip_id, n)",
    )
    suite = _write_suite(tmp_path, "    - unique: {columns: [trip_id], tags: [x]}")
    result = runner.invoke(app, ["run", str(suite), "--select", "nope"])
    assert result.exit_code == 0
    assert "0/0 checks OK" in result.output


# ---------------------------------------------------------------------------
# tolerances
# ---------------------------------------------------------------------------


def test_run_tolerance_passes_and_exit_0(tmp_path: Path) -> None:
    _write_parquet(
        tmp_path / "trips.parquet",
        "SELECT * FROM (VALUES ('a', 1), (NULL, 2)) AS v(trip_id, n)",  # 1 null
    )
    suite = _write_suite(tmp_path, "    - not_null: {columns: [trip_id], max_failed_rows: 1}")
    result = runner.invoke(app, ["run", str(suite), "-f", "json"])
    assert result.exit_code == 0
    payload = json.loads(result.output)
    assert payload["passed"] is True
    check = payload["checks"][0]
    assert check["tolerated"] is True
    assert check["failed_rows"] == 1


# ---------------------------------------------------------------------------
# configuration errors → exit 2
# ---------------------------------------------------------------------------


def test_run_missing_suite_file_exit_2(tmp_path: Path) -> None:
    result = runner.invoke(app, ["run", str(tmp_path / "nope.yaml")])
    assert result.exit_code == 2


def test_run_invalid_suite_exit_2(tmp_path: Path) -> None:
    suite_file = tmp_path / "suite.yaml"
    suite_file.write_text("version: 99\nsources: {t: {path: t.csv}}\n", encoding="utf-8")
    result = runner.invoke(app, ["run", str(suite_file)])
    assert result.exit_code == 2


def test_run_missing_source_file_exit_2(tmp_path: Path) -> None:
    suite_file = _write_suite(tmp_path, "    - unique: trip_id")
    result = runner.invoke(app, ["run", str(suite_file)])
    assert result.exit_code == 2


# ---------------------------------------------------------------------------
# validate and --version
# ---------------------------------------------------------------------------


def test_validate_ok(passing_suite: Path) -> None:
    result = runner.invoke(app, ["validate", str(passing_suite)])
    assert result.exit_code == 0
    assert "OK: 1 source(s), 3 check(s)" in result.output


def test_version() -> None:
    result = runner.invoke(app, ["--version"])
    assert result.exit_code == 0
    assert "quacklint" in result.output


# ---------------------------------------------------------------------------
# severity: warn
# ---------------------------------------------------------------------------


@pytest.fixture()
def warn_only_suite(tmp_path: Path) -> Path:
    """The only failing check (unique) is marked as severity: warn."""
    _write_parquet(
        tmp_path / "trips.parquet",
        "SELECT * FROM (VALUES ('a', 1), ('a', 2)) AS v(trip_id, n)",
    )
    return _write_suite(
        tmp_path,
        "    - unique: {columns: [trip_id], severity: warn}\n"
        "    - row_count: {min: 1}",
    )


def test_run_warn_only_exits_0_and_shows_warn(warn_only_suite: Path) -> None:
    result = runner.invoke(app, ["run", str(warn_only_suite)])
    assert result.exit_code == 0
    assert "WARN" in result.output
    assert "FAIL" not in result.output
    assert "1 warning(s)" in result.output


def test_run_warn_plus_error_exits_1(tmp_path: Path) -> None:
    _write_parquet(
        tmp_path / "trips.parquet",
        "SELECT * FROM (VALUES ('a', 1), ('a', 2), (NULL, 3)) AS v(trip_id, n)",
    )
    suite = _write_suite(
        tmp_path,
        "    - unique: {columns: [trip_id], severity: warn}\n"
        "    - not_null: trip_id",
    )
    result = runner.invoke(app, ["run", str(suite)])
    assert result.exit_code == 1
    assert "WARN" in result.output
    assert "FAIL" in result.output


def test_run_warn_json_passed_true_and_severity(warn_only_suite: Path) -> None:
    result = runner.invoke(app, ["run", str(warn_only_suite), "-f", "json"])
    assert result.exit_code == 0
    payload = json.loads(result.output)
    assert payload["passed"] is True  # only a warn fails -> not counted as an error
    assert payload["failed"] == 1
    assert payload["errors"] == 0
    by_check = {item["check"]: item for item in payload["checks"]}
    assert by_check["unique"]["severity"] == "warn"
    assert by_check["unique"]["passed"] is False


def test_run_warn_junit_excludes_from_failures(warn_only_suite: Path) -> None:
    result = runner.invoke(app, ["run", str(warn_only_suite), "-f", "junit"])
    assert result.exit_code == 0
    assert 'failures="0"' in result.output
    assert "<failure" not in result.output
    assert "system-out" in result.output


# ---------------------------------------------------------------------------
# default discovery of ./quacklint.yaml
# ---------------------------------------------------------------------------


def test_run_defaults_to_quacklint_yaml(
    passing_suite: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    default = passing_suite.parent / "quacklint.yaml"
    default.write_text(passing_suite.read_text(encoding="utf-8"), encoding="utf-8")
    monkeypatch.chdir(passing_suite.parent)
    result = runner.invoke(app, ["run"])
    assert result.exit_code == 0
    assert "3/3 checks OK" in result.output


def test_validate_defaults_to_quacklint_yaml(
    passing_suite: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    default = passing_suite.parent / "quacklint.yaml"
    default.write_text(passing_suite.read_text(encoding="utf-8"), encoding="utf-8")
    monkeypatch.chdir(passing_suite.parent)
    result = runner.invoke(app, ["validate"])
    assert result.exit_code == 0
    assert "OK:" in result.output


def test_run_no_arg_no_default_exits_2(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.chdir(tmp_path)
    result = runner.invoke(app, ["run"])
    assert result.exit_code == 2
