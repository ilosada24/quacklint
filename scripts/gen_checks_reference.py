"""Genera docs/checks-reference.md a partir de los docstrings de los checks.

La tabla y las secciones salen del docstring de cada clase registrada en
`quacklint.checks.builtin`; el SQL de ejemplo se obtiene llamando a `to_sql()`
sobre instancias reales, así el documento no puede desincronizarse del código.

Uso:
    uv run python scripts/gen_checks_reference.py          # (re)escribe el fichero
    uv run python scripts/gen_checks_reference.py --check  # falla si está desactualizado
"""

from __future__ import annotations

import inspect
import sys
from datetime import timedelta
from pathlib import Path

from quacklint.checks.base import Check, registered_checks
from quacklint.checks.builtin import (
    AcceptedValuesCheck,
    CustomSqlCheck,
    FreshnessCheck,
    NotNullCheck,
    RangeCheck,
    RegexMatchCheck,
    RowCountCheck,
    UniqueCheck,
)

DOC_PATH = Path(__file__).resolve().parents[1] / "docs" / "checks-reference.md"

# Instancias de ejemplo para mostrar el SQL real que genera cada check.
_EXAMPLES: dict[str, Check] = {
    "not_null": NotNullCheck("trips", ["trip_id", "pickup_ts"]),
    "unique": UniqueCheck("trips", ["trip_id"]),
    "accepted_values": AcceptedValuesCheck("trips", "payment_type", ["card", "cash"]),
    "range": RangeCheck("trips", "fare", 0, 1000),
    "regex_match": RegexMatchCheck("trips", "trip_id", "t-[0-9]{3}"),
    "freshness": FreshnessCheck("trips", "pickup_ts", timedelta(hours=24)),
    "row_count": RowCountCheck("trips", 1, 100000),
    "custom_sql": CustomSqlCheck(
        "trips", "no_negative_duration", "SELECT * FROM trips WHERE dropoff_ts < pickup_ts"
    ),
}

_HEADER = """\
<!-- Generado por scripts/gen_checks_reference.py a partir de los docstrings
     de src/quacklint/checks/builtin.py. NO editar a mano:
     uv run python scripts/gen_checks_reference.py -->

# Referencia de checks

Cada check compila a una consulta SQL de DuckDB cuyas filas son las
**violaciones** de la regla: 0 filas = el check pasa. Puedes inspeccionar el
SQL de tu suite con `quacklint run suite.yaml --explain`. Referenciar una
columna inexistente es un error de configuración (exit 2), no un fallo del
check. La sintaxis completa del formato YAML está en el contrato:
[spec.yaml.md](spec.yaml.md).
"""


def _docstring(check_cls: type[Check]) -> str:
    doc = inspect.getdoc(check_cls)
    if not doc:
        raise SystemExit(f"el check '{check_cls.__name__}' no tiene docstring")
    return doc


def _summary(check_cls: type[Check]) -> str:
    return _docstring(check_cls).splitlines()[0].rstrip(".")


def generate() -> str:
    checks = registered_checks()  # orden de registro = orden de builtin.py
    lines: list[str] = [_HEADER]

    lines.append("| Check | Regla |")
    lines.append("| ----- | ----- |")
    for name, check_cls in checks.items():
        lines.append(f"| [`{name}`](#{name}) | {_summary(check_cls)}. |")
    lines.append("")

    for name, check_cls in checks.items():
        lines.append(f"## {name}")
        lines.append("")
        lines.append(_docstring(check_cls))
        lines.append("")
        lines.append("SQL generado (ejemplo):")
        lines.append("")
        lines.append("```sql")
        lines.append(_EXAMPLES[name].to_sql("trips"))
        lines.append("```")
        lines.append("")

    return "\n".join(lines).rstrip() + "\n"


def main(argv: list[str]) -> int:
    content = generate()
    if "--check" in argv:
        current = DOC_PATH.read_text(encoding="utf-8") if DOC_PATH.exists() else ""
        if current != content:
            print(
                f"{DOC_PATH} está desactualizado. "
                "Regenéralo con: uv run python scripts/gen_checks_reference.py"
            )
            return 1
        print(f"{DOC_PATH} está al día.")
        return 0
    DOC_PATH.write_text(content, encoding="utf-8")
    print(f"escrito {DOC_PATH}")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
