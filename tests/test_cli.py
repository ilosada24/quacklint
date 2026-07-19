"""Tests del CLI con CliRunner (typer.testing)."""

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
    """not_null falla (1 NULL), unique falla ('a' duplicado), row_count pasa."""
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
# run: tabla
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
    # La tabla incluye filas de muestra de las violaciones.
    assert "muestra" in result.output


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
    assert result.exit_code == 0  # no ejecuta, así que no hay checks fallidos
    assert "-- not_null (trips)" in result.output
    assert "-- unique (trips)" in result.output
    assert "IS NULL" in result.output
    assert "HAVING count(*) > 1" in result.output
    assert "PASS" not in result.output
    assert "FAIL" not in result.output


def test_run_explain_works_without_data_files(tmp_path: Path) -> None:
    """--explain no toca las fuentes: compila aunque el fichero de datos no exista."""
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


# ---------------------------------------------------------------------------
# errores de configuración → exit 2
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
# validate y --version
# ---------------------------------------------------------------------------


def test_validate_ok(passing_suite: Path) -> None:
    result = runner.invoke(app, ["validate", str(passing_suite)])
    assert result.exit_code == 0
    assert "OK: 1 fuente(s), 3 check(s)" in result.output


def test_version() -> None:
    result = runner.invoke(app, ["--version"])
    assert result.exit_code == 0
    assert "quacklint" in result.output
