"""CLI de quacklint (Typer).

Códigos de salida: 0 = todos los checks pasan, 1 = checks fallidos,
2 = error de configuración (suite inválida, fuente inexistente...).
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

app = typer.Typer(help="Data quality declarativo para DuckDB.", no_args_is_help=True)
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
    """Devuelve el fichero de suite indicado o, si se omite, ./quacklint.yaml."""
    if suite_file is not None:
        return suite_file
    if DEFAULT_SUITE.exists():
        return DEFAULT_SUITE
    _config_error(
        SpecError(
            "no se indicó un fichero de suite y no existe "
            f"./{DEFAULT_SUITE} en el directorio actual"
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
            help="Muestra la versión y sale.",
        ),
    ] = False,
) -> None:
    """quacklint: checks de calidad de datos declarados en YAML, ejecutados como SQL en DuckDB."""


@app.command()
def validate(
    suite_file: Annotated[
        Path | None,
        typer.Argument(help="Ruta a la suite YAML (por defecto ./quacklint.yaml)."),
    ] = None,
) -> None:
    """Valida la suite YAML sin ejecutar ningún check."""
    suite_file = _resolve_suite_file(suite_file)
    suite = _load_suite(suite_file)
    total_checks = sum(len(entries) for entries in suite.spec.checks.values())
    typer.echo(
        f"OK: {len(suite.spec.sources)} fuente(s), {total_checks} check(s) en {suite_file}"
    )


@app.command()
def run(
    suite_file: Annotated[
        Path | None,
        typer.Argument(help="Ruta a la suite YAML (por defecto ./quacklint.yaml)."),
    ] = None,
    fmt: Annotated[
        ReportFormat,
        typer.Option("--format", "-f", help="Formato del informe."),
    ] = ReportFormat.TABLE,
    explain: Annotated[
        bool,
        typer.Option("--explain", help="Imprime el SQL compilado de cada check y no ejecuta nada."),
    ] = False,
    fail_fast: Annotated[
        bool,
        typer.Option("--fail-fast", help="Se detiene en el primer check fallido."),
    ] = False,
) -> None:
    """Ejecuta los checks de la suite contra sus fuentes."""
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
            summary += f", {warnings} advertencia(s)"
        _out.print(summary)

    if errors:
        raise typer.Exit(EXIT_CHECKS_FAILED)


def main() -> None:
    app()


if __name__ == "__main__":
    main()
