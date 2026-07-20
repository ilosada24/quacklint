"""Rendering of check results: table (rich), json and junit."""

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


def build_table(results: Sequence[CheckResult]) -> Table:
    """Rich table with one check per row and the sampled failing rows in the detail.

    Shows every sample row attached to the result (the same set the JSON report
    includes), so the two formats never disagree on the number of rows shown.
    """
    table = Table()
    table.add_column("status")
    table.add_column("source")
    table.add_column("check")
    table.add_column("rows", justify="right")
    table.add_column("detail", overflow="fold")
    for result in results:
        status = "[green]PASS[/green]" if result.passed else _fail_status(result)
        table.add_row(
            status,
            result.source,
            result.check,
            str(result.failed_rows),
            escape(_detail(result)),
        )
    return table


def _fail_status(result: CheckResult) -> str:
    """Status label of a failed check: WARN (yellow) or FAIL (red)."""
    if result.severity == "warn":
        return "[yellow]WARN[/yellow]"
    return "[red]FAIL[/red]"


def _detail(result: CheckResult) -> str:
    if result.passed:
        return ""
    lines = [result.message] if result.message else []
    if result.sample_rows:
        sample = "; ".join(
            ", ".join(
                f"{column}={value}"
                for column, value in zip(result.sample_columns, row, strict=True)
            )
            for row in result.sample_rows
        )
        lines.append(f"sample: {sample}")
    return "\n".join(lines)


def render_json(results: Sequence[CheckResult]) -> str:
    """Stable JSON report for programmatic consumption."""
    failed = sum(1 for result in results if not result.passed)
    errors = sum(1 for result in results if not result.passed and result.severity == "error")
    payload = {
        "passed": errors == 0,
        "total": len(results),
        "failed": failed,
        "errors": errors,
        "checks": [
            {
                "check": result.check,
                "source": result.source,
                "severity": result.severity,
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
    """JUnit XML report: one testcase per check, for CI platforms.

    Only failed checks with `severity: error` count as a `<failure>` (the ones
    that break the build). A failed `warn` check is reported with `<system-out>`
    so it does not block CI.
    """
    errors = sum(1 for result in results if not result.passed and result.severity == "error")
    suite = ElementTree.Element(
        "testsuite",
        name="quacklint",
        tests=str(len(results)),
        failures=str(errors),
        errors="0",
    )
    for result in results:
        case = ElementTree.SubElement(
            suite, "testcase", classname=result.source, name=result.check
        )
        if not result.passed:
            message = result.message or f"{result.failed_rows} row(s) violate the rule"
            if result.severity == "warn":
                out = ElementTree.SubElement(case, "system-out")
                out.text = f"warning: {message}"
            else:
                ElementTree.SubElement(case, "failure", message=message)
    ElementTree.indent(suite)
    return '<?xml version="1.0" encoding="UTF-8"?>\n' + ElementTree.tostring(
        suite, encoding="unicode"
    )
