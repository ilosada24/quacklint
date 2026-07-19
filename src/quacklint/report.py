"""Render de resultados de checks: table (rich), json y junit."""

from __future__ import annotations

import json
from collections.abc import Sequence
from enum import StrEnum
from xml.etree import ElementTree

from rich.markup import escape
from rich.table import Table

from quacklint.checks.base import CheckResult


class ReportFormat(StrEnum):
    TABLE = "table"
    JSON = "json"
    JUNIT = "junit"


def build_table(results: Sequence[CheckResult], sample_size: int = 3) -> Table:
    """Tabla rich con un check por fila y muestra de filas fallidas en el detalle."""
    table = Table()
    table.add_column("estado")
    table.add_column("fuente")
    table.add_column("check")
    table.add_column("filas", justify="right")
    table.add_column("detalle", overflow="fold")
    for result in results:
        estado = "[green]PASS[/green]" if result.passed else "[red]FAIL[/red]"
        table.add_row(
            estado,
            result.source,
            result.check,
            str(result.failed_rows),
            escape(_detail(result, sample_size)),
        )
    return table


def _detail(result: CheckResult, sample_size: int) -> str:
    if result.passed:
        return ""
    lines = [result.message] if result.message else []
    if result.sample_rows:
        sample = "; ".join(
            ", ".join(
                f"{column}={value}"
                for column, value in zip(result.sample_columns, row, strict=True)
            )
            for row in result.sample_rows[:sample_size]
        )
        lines.append(f"muestra: {sample}")
    return "\n".join(lines)


def render_json(results: Sequence[CheckResult]) -> str:
    """Informe JSON estable para consumo programático."""
    failed = sum(1 for result in results if not result.passed)
    payload = {
        "passed": failed == 0,
        "total": len(results),
        "failed": failed,
        "checks": [
            {
                "check": result.check,
                "source": result.source,
                "passed": result.passed,
                "failed_rows": result.failed_rows,
                "message": result.message,
                "sample": [
                    dict(zip(result.sample_columns, row, strict=True))
                    for row in result.sample_rows
                ],
            }
            for result in results
        ],
    }
    return json.dumps(payload, ensure_ascii=False, default=str, indent=2)


def render_junit(results: Sequence[CheckResult]) -> str:
    """Informe JUnit XML: un testcase por check, para plataformas de CI."""
    failed = sum(1 for result in results if not result.passed)
    suite = ElementTree.Element(
        "testsuite",
        name="quacklint",
        tests=str(len(results)),
        failures=str(failed),
        errors="0",
    )
    for result in results:
        case = ElementTree.SubElement(
            suite, "testcase", classname=result.source, name=result.check
        )
        if not result.passed:
            message = result.message or f"{result.failed_rows} fila(s) violan la regla"
            ElementTree.SubElement(case, "failure", message=message)
    ElementTree.indent(suite)
    return '<?xml version="1.0" encoding="UTF-8"?>\n' + ElementTree.tostring(
        suite, encoding="unicode"
    )
