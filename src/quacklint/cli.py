"""quacklint CLI (Typer).

Exit codes: 0 = all checks pass, 1 = checks failed,
2 = configuration error (invalid suite, missing source...).
"""

from __future__ import annotations

from pathlib import Path
from typing import Annotated, NoReturn

import typer
from rich.console import Console

from quacklint import __version__
from quacklint.errors import QuacklintError, SpecError
from quacklint.report import ReportFormat, build_table, render_json, render_junit
from quacklint.suite import Suite

EXIT_CHECKS_FAILED = 1
EXIT_CONFIG_ERROR = 2

DEFAULT_SUITE = Path("quacklint.yaml")

app = typer.Typer(help="Declarative data quality for DuckDB.", no_args_is_help=True)
_err = Console(stderr=True)
_out = Console()


def _config_error(exc: QuacklintError) -> NoReturn:
    _err.print(f"[bold red]error:[/bold red] {exc}")
    raise typer.Exit(EXIT_CONFIG_ERROR)


def _load_suite(path: Path) -> Suite:
    try:
        return Suite.from_file(path)
    except QuacklintError as exc:
        _config_error(exc)


def _resolve_suite_file(suite_file: Path | None) -> Path:
    """Return the given suite file or, if omitted, ./quacklint.yaml."""
    if suite_file is not None:
        return suite_file
    if DEFAULT_SUITE.exists():
        return DEFAULT_SUITE
    _config_error(
        SpecError(
            "no suite file given and "
            f"./{DEFAULT_SUITE} does not exist in the current directory"
        )
    )


def _version_callback(value: bool) -> None:
    if value:
        typer.echo(f"quacklint {__version__}")
        raise typer.Exit()


@app.callback()
def main_options(
    version: Annotated[
        bool,
        typer.Option(
            "--version",
            callback=_version_callback,
            is_eager=True,
            help="Show the version and exit.",
        ),
    ] = False,
) -> None:
    """quacklint: data quality checks declared in YAML, run as SQL on DuckDB."""


@app.command()
def validate(
    suite_file: Annotated[
        Path | None,
        typer.Argument(help="Path to the YAML suite (defaults to ./quacklint.yaml)."),
    ] = None,
) -> None:
    """Validate the YAML suite without running any check."""
    suite_file = _resolve_suite_file(suite_file)
    suite = _load_suite(suite_file)
    total_checks = sum(len(entries) for entries in suite.spec.checks.values())
    typer.echo(
        f"OK: {len(suite.spec.sources)} source(s), {total_checks} check(s) in {suite_file}"
    )


@app.command()
def run(
    suite_file: Annotated[
        Path | None,
        typer.Argument(help="Path to the YAML suite (defaults to ./quacklint.yaml)."),
    ] = None,
    fmt: Annotated[
        ReportFormat,
        typer.Option("--format", "-f", help="Report format."),
    ] = ReportFormat.TABLE,
    explain: Annotated[
        bool,
        typer.Option("--explain", help="Print the compiled SQL of each check and run nothing."),
    ] = False,
    fail_fast: Annotated[
        bool,
        typer.Option("--fail-fast", help="Stop at the first error failure (warnings don't stop)."),
    ] = False,
) -> None:
    """Run the suite's checks against their sources."""
    suite_file = _resolve_suite_file(suite_file)
    suite = _load_suite(suite_file)

    if explain:
        try:
            compiled = suite.compile()
        except QuacklintError as exc:
            _config_error(exc)
        for item in compiled:
            typer.echo(f"-- {item.check} ({item.source})")
            typer.echo(item.sql)
            typer.echo("")
        return

    try:
        results = suite.run(fail_fast=fail_fast)
    except QuacklintError as exc:
        _config_error(exc)

    failed = sum(1 for result in results if not result.passed)
    warnings = sum(1 for result in results if not result.passed and result.severity == "warn")
    errors = failed - warnings
    if fmt is ReportFormat.JSON:
        typer.echo(render_json(results))
    elif fmt is ReportFormat.JUNIT:
        typer.echo(render_junit(results))
    else:
        _out.print(build_table(results))
        summary = f"{len(results) - failed}/{len(results)} checks OK"
        if warnings:
            summary += f", {warnings} warning(s)"
        _out.print(summary)

    if errors:
        raise typer.Exit(EXIT_CHECKS_FAILED)


def main() -> None:
    app()


if __name__ == "__main__":
    main()
