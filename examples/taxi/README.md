# Ejemplo: taxi

Suite completa (usa los 8 tipos de check del contrato) sobre un CSV sintético
de viajes de taxi.

```console
$ uv run quacklint validate examples/taxi/quacklint.yaml
$ uv run quacklint run examples/taxi/quacklint.yaml
```

## Qué esperar

`data/trips.csv` contiene **errores deliberados**; `quacklint run` termina con
exit 1 y este desglose:

| Check                  | Resultado | Por qué                                              |
| ---------------------- | --------- | ---------------------------------------------------- |
| `not_null`             | PASS      | `trip_id` y `pickup_ts` no tienen NULL.              |
| `unique`               | FAIL      | `t-004` aparece dos veces.                           |
| `row_count`            | PASS      | Hay más de 1 fila.                                   |
| `regex_match`          | PASS      | Todos los `trip_id` siguen el patrón `t-[0-9]{3}`.   |
| `accepted_values`      | FAIL      | `t-004` paga con `voucher` (solo `card`/`cash`).     |
| `range`                | FAIL      | `t-005` tiene `fare = -3.0`.                         |
| `freshness`            | FAIL      | Los datos son de 2026-07-18, más viejos que `24h`.   |
| `no_negative_duration` | FAIL      | En `t-005`, `dropoff_ts < pickup_ts` (`custom_sql`). |

Prueba también:

```console
$ uv run quacklint run examples/taxi/quacklint.yaml --explain    # ver el SQL
$ uv run quacklint run examples/taxi/quacklint.yaml -f json      # informe JSON
```
