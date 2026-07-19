# Integración en CI

quacklint está pensado para ejecutarse en CI: códigos de salida estables
(`0` todo pasa, `1` checks fallidos, `2` configuración inválida) y salida
`junit` para que la plataforma muestre cada check como un test.

## GitHub Actions

Validar la suite en cada PR (disponible ya):

```yaml
name: data-quality

on:
  pull_request:

jobs:
  quacklint:
    runs-on: ubuntu-latest
    steps:
      - uses: actions/checkout@v4
      - uses: astral-sh/setup-uv@v5
      - run: uv sync
      - run: uv run quacklint validate quality/suite.yaml
```

Ejecutar los checks y publicar resultados como informe JUnit:

```yaml
      - run: uv run quacklint run quality/suite.yaml --format junit > results.xml
      - uses: mikepenz/action-junit-report@v5
        if: always()
        with:
          report_paths: results.xml
```

## Consejos

- Versiona las suites YAML junto al código que produce los datos: los cambios
  de reglas pasan por revisión igual que el código.
- Usa `quacklint validate` como paso rápido (no toca los datos) y `quacklint
  run` en el job que ya tiene acceso a los ficheros.
