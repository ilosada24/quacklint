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

| Check | Regla |
| ----- | ----- |
| [`not_null`](#not_null) | Las columnas indicadas no contienen NULL. |
| [`unique`](#unique) | Sin duplicados en la columna o combinación de columnas. |
| [`accepted_values`](#accepted_values) | Todos los valores no nulos de la columna pertenecen al conjunto dado. |
| [`range`](#range) | Los valores no nulos de la columna están dentro de [min, max] (inclusive). |
| [`regex_match`](#regex_match) | Todos los valores no nulos de la columna casan (completos) con el patrón. |
| [`freshness`](#freshness) | El valor más reciente de la columna no es más antiguo que max_age. |
| [`row_count`](#row_count) | El número de filas de la fuente está dentro de [min, max] (inclusive). |
| [`custom_sql`](#custom_sql) | Consulta SQL arbitraria cuyas filas resultantes son las violaciones. |

## not_null

Las columnas indicadas no contienen NULL.

```yaml
- not_null: [trip_id, pickup_ts]
- not_null: trip_id            # forma abreviada: una sola columna
```

Cada fila con NULL en alguna de las columnas cuenta como una violación.

SQL generado (ejemplo):

```sql
SELECT * FROM "trips" WHERE "trip_id" IS NULL OR "pickup_ts" IS NULL
```

## unique

Sin duplicados en la columna o combinación de columnas.

```yaml
- unique: trip_id              # forma abreviada: una columna
- unique: [trip_id, pickup_ts] # clave compuesta
```

Las filas con NULL en alguna de las columnas se ignoran (para exigir no
nulos está `not_null`). Cada clave duplicada cuenta como una violación.

SQL generado (ejemplo):

```sql
SELECT "trip_id", count(*) AS occurrences FROM "trips" WHERE "trip_id" IS NOT NULL GROUP BY "trip_id" HAVING count(*) > 1
```

## accepted_values

Todos los valores no nulos de la columna pertenecen al conjunto dado.

```yaml
- accepted_values:
    column: payment_type
    values: [card, cash]
```

`values` acepta escalares str/int/float/bool. Los NULL no cuentan como
violación.

SQL generado (ejemplo):

```sql
SELECT * FROM "trips" WHERE "payment_type" IS NOT NULL AND "payment_type" NOT IN ('card', 'cash')
```

## range

Los valores no nulos de la columna están dentro de [min, max] (inclusive).

```yaml
- range: {column: fare, min: 0, max: 1000}
- range: {column: fare, min: 0}    # solo cota inferior
```

Al menos uno de `min` / `max` es obligatorio. Los NULL no cuentan como
violación.

SQL generado (ejemplo):

```sql
SELECT * FROM "trips" WHERE "fare" IS NOT NULL AND ("fare" < 0 OR "fare" > 1000)
```

## regex_match

Todos los valores no nulos de la columna casan (completos) con el patrón.

```yaml
- regex_match: {column: trip_id, pattern: 't-[0-9]{3}'}
```

Usa `regexp_full_match` de DuckDB (sintaxis RE2): el patrón debe casar
con el valor completo; añade `.*` para búsquedas parciales. El patrón se
valida al cargar la suite. Los NULL no cuentan como violación.

SQL generado (ejemplo):

```sql
SELECT * FROM "trips" WHERE "trip_id" IS NOT NULL AND NOT regexp_full_match("trip_id", 't-[0-9]{3}')
```

## freshness

El valor más reciente de la columna no es más antiguo que max_age.

```yaml
- freshness: {column: pickup_ts, max_age: 24h}
```

`max_age` es una duración (`s`, `m`, `h`, `d`) y la referencia temporal
es el `now()` de DuckDB al ejecutar. Si la fuente está vacía o la columna
es toda NULL, el check pasa (para eso están `row_count` y `not_null`).

SQL generado (ejemplo):

```sql
SELECT CAST(max("pickup_ts") AS VARCHAR) AS most_recent FROM "trips" HAVING max("pickup_ts") < now() - INTERVAL '86400 seconds'
```

## row_count

El número de filas de la fuente está dentro de [min, max] (inclusive).

```yaml
- row_count: {min: 1}
- row_count: {min: 100, max: 100000}
```

Al menos uno de `min` / `max` es obligatorio (enteros >= 0). No referencia
columnas: opera sobre la fuente completa.

SQL generado (ejemplo):

```sql
SELECT count(*) AS row_count FROM "trips" HAVING count(*) < 1 OR count(*) > 100000
```

## custom_sql

Consulta SQL arbitraria cuyas filas resultantes son las violaciones.

```yaml
- custom_sql:
    name: no_negative_duration
    query: SELECT * FROM trips WHERE dropoff_ts < pickup_ts
```

La consulta puede referenciar cualquier fuente por su nombre de vista y
en los informes el check se reporta con su `name`. Un `;` final se
elimina automáticamente; debe ser un único SELECT.

SQL generado (ejemplo):

```sql
SELECT * FROM trips WHERE dropoff_ts < pickup_ts
```
