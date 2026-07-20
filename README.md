# quacklint 🦆

**Data quality declarativo para DuckDB.** Describe tus reglas en un YAML;
quacklint las compila a SQL y las ejecuta directamente sobre tus ficheros
Parquet/CSV/JSON. Sin pandas, sin servicios, hecho para CI.

## El problema

Los checks de calidad de datos suelen acabar como scripts ad hoc de pandas:
lentos, imperativos, difíciles de revisar en un PR y acoplados al entorno de
quien los escribió. quacklint apuesta por lo contrario:

- **Declarativo** — la suite YAML dice *qué* debe cumplirse, no *cómo*
  comprobarlo, y se revisa en un PR igual que el código.
- **SQL primero** — cada check compila a una consulta DuckDB que devuelve las
  filas que violan la regla (0 filas = pasa). Los datos nunca se cargan a
  Python; DuckDB lee Parquet/CSV/JSON directamente y en paralelo.
- **Hecho para CI** — códigos de salida estables, informes `table`/`json`/
  `junit`, y errores de configuración accionables (nunca tracebacks).

Más sobre el porqué en [docs/philosophy.md](docs/philosophy.md).

## Instalación

Requiere Python 3.11+ y [uv](https://docs.astral.sh/uv/).

**Global (el comando `quacklint` disponible en cualquier carpeta):**

```console
$ uv tool install --editable /ruta/al/repo/quacklint
$ quacklint --help
```

- `--editable` hace que los cambios en el código del repo se reflejen sin
  reinstalar (recomendado mientras el proyecto está en desarrollo activo);
  quítalo si prefieres una copia congelada.
- Si `uv` avisa de que `~/.local/bin` no está en tu PATH: `uv tool update-shell`.
- Para actualizar o desinstalar: `uv tool upgrade quacklint` /
  `uv tool uninstall quacklint`.

**Para desarrollar** (entorno del repo, sin instalar nada global): ver
[Desarrollo](#desarrollo).

## Quickstart en 30 segundos

Un CSV con problemas:

```csv
# trips.csv
trip_id,payment_type,fare
t-001,card,12.5
t-002,cash,9.0
t-002,voucher,-3.0
```

Una suite que declara las reglas:

```yaml
# suite.yaml
version: 1
sources:
  trips:
    path: trips.csv
checks:
  trips:
    - not_null: [trip_id, fare]
    - unique: trip_id
    - accepted_values: {column: payment_type, values: [card, cash]}
    - range: {column: fare, min: 0}
```

Y a correr:

```console
$ quacklint run suite.yaml
┏━━━━━━━━┳━━━━━━━━┳━━━━━━━━━━━━━━━━━┳━━━━━━━┳━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━┓
┃ estado ┃ fuente ┃ check           ┃ filas ┃ detalle                        ┃
┡━━━━━━━━╇━━━━━━━━╇━━━━━━━━━━━━━━━━━╇━━━━━━━╇━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━┩
│ PASS   │ trips  │ not_null        │     0 │                                │
│ FAIL   │ trips  │ unique          │     1 │ 1 fila(s) violan la regla      │
│        │        │                 │       │ muestra: trip_id=t-002,        │
│        │        │                 │       │ occurrences=2                  │
│ FAIL   │ trips  │ accepted_values │     1 │ 1 fila(s) violan la regla      │
│        │        │                 │       │ muestra: trip_id=t-002,        │
│        │        │                 │       │ payment_type=voucher,          │
│        │        │                 │       │ fare=-3.0                      │
│ FAIL   │ trips  │ range           │     1 │ 1 fila(s) violan la regla      │
│        │        │                 │       │ muestra: trip_id=t-002,        │
│        │        │                 │       │ payment_type=voucher,          │
│        │        │                 │       │ fare=-3.0                      │
└────────┴────────┴─────────────────┴───────┴────────────────────────────────┘
1/4 checks OK
$ echo $?
1
```

Hay un ejemplo completo listo para ejecutar en [examples/taxi](examples/taxi).

## Checks disponibles

| Check             | Regla                                                               |
| ----------------- | ------------------------------------------------------------------- |
| `not_null`        | Las columnas no contienen `NULL`.                                   |
| `unique`          | Sin duplicados en la columna o combinación (los `NULL` se ignoran). |
| `row_count`       | Número de filas dentro de `[min, max]` (inclusive).                 |
| `accepted_values` | Todos los valores no nulos pertenecen a un conjunto dado.           |
| `range`           | Valores numéricos no nulos dentro de `[min, max]` (inclusive).      |
| `regex_match`     | Valores no nulos casan (completos) con una expresión regular RE2.   |
| `freshness`       | El valor más reciente de una columna temporal no supera una edad.   |
| `custom_sql`      | SQL arbitrario cuyas filas resultantes son violaciones.             |

Sintaxis y SQL generado de cada uno:
[docs/checks-reference.md](docs/checks-reference.md). El contrato completo del
formato YAML: [docs/spec.yaml.md](docs/spec.yaml.md).

## CLI

```console
$ quacklint validate suite.yaml          # valida sin tocar los datos
$ quacklint run suite.yaml               # tabla rich con muestra de filas fallidas
$ quacklint run suite.yaml -f json       # informe JSON
$ quacklint run suite.yaml -f junit      # JUnit XML para CI
$ quacklint run suite.yaml --explain     # imprime el SQL compilado, sin ejecutar
$ quacklint run suite.yaml --fail-fast   # se detiene en el primer error (los warn no cortan)
$ quacklint run                          # sin argumento: usa ./quacklint.yaml
```

El argumento de suite es opcional: si se omite, `run` y `validate` buscan
`./quacklint.yaml` en el directorio actual.

Códigos de salida:

| Código | Significado                                        |
| ------ | -------------------------------------------------- |
| 0      | Todos los checks pasan                             |
| 1      | Hay checks fallidos                                |
| 2      | Error de configuración (suite inválida, fuentes…)  |

Ejemplos de integración con GitHub Actions:
[docs/ci-integration.md](docs/ci-integration.md).

## Desarrollo

```console
$ uv sync                                              # deps (incluido grupo dev)
$ uv run pytest                                        # tests
$ uv run ruff check                                    # lint
$ uv run mypy                                          # tipos (strict)
$ uv run python scripts/gen_checks_reference.py        # regenera docs/checks-reference.md
```

`docs/checks-reference.md` se genera desde los docstrings de
[builtin.py](src/quacklint/checks/builtin.py); un test falla si queda
desactualizado.

## Publicar en PyPI (pasos futuros)

El paquete ya se construye correctamente (`uv build` produce wheel y sdist).
Para publicarlo quedaría:

1. Completar metadatos en `pyproject.toml`: `license = "MIT"` + fichero
   `LICENSE`, `authors`, `keywords` y `classifiers`.
2. Ensayo opcional en TestPyPI:
   `uv publish --publish-url https://test.pypi.org/legacy/ --token ...`.
3. Cuenta en [pypi.org](https://pypi.org) + API token, y publicar:
   `uv publish --token pypi-XXXX`.
4. A partir de ahí la instalación pasa a ser `uv tool install quacklint`
   (el nombre está libre en PyPI, verificado 2026-07-19).
