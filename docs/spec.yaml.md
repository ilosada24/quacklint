# Contrato del formato YAML de quacklint (`version: 1`)

Este documento es **el contrato** del formato de suite. El parser
([suite.py](../src/quacklint/suite.py)) no puede desviarse de lo aquí descrito
sin actualizar este fichero, y viceversa.

## Ejemplo completo

```yaml
version: 1
sources:
  trips:
    path: data/trips.parquet
checks:
  trips:
    - not_null: [trip_id, pickup_ts]
    - unique: trip_id
    - accepted_values:
        column: payment_type
        values: [card, cash]
    - range: {column: fare, min: 0, max: 1000}
    - freshness: {column: pickup_ts, max_age: 24h}
    - custom_sql:
        name: no_negative_duration
        query: SELECT * FROM trips WHERE dropoff_ts < pickup_ts
```

## Estructura general

El fichero de suite se pasa a `quacklint run` / `quacklint validate` como
argumento. Si se omite, ambos comandos buscan `./quacklint.yaml` en el
directorio actual.

Una suite es un mapeo YAML con exactamente estas claves (cualquier otra clave
en el nivel superior es un error):

| Clave     | Obligatoria | Tipo    | Descripción                                        |
| --------- | ----------- | ------- | -------------------------------------------------- |
| `version` | sí          | entero  | Versión del formato. Solo se acepta `1`.           |
| `sources` | sí          | mapeo   | Fuentes de datos. Al menos una.                    |
| `checks`  | no          | mapeo   | Checks por fuente. Puede omitirse o estar vacío.   |

## `sources`

Mapeo de **nombre de fuente** a configuración:

```yaml
sources:
  <nombre>:
    path: <ruta>
```

- El nombre debe ser un identificador: letras, dígitos y `_`, sin empezar por
  dígito. Cada fuente se expone como una **vista DuckDB con ese nombre**, por lo
  que los `custom_sql` pueden referenciarla directamente.
- `path` es la ruta al fichero de datos, relativa al directorio del fichero de
  suite (o absoluta). Extensiones soportadas: `.parquet`, `.csv`, `.json`,
  `.ndjson`.
- `path` también puede ser un **patrón glob** (`data/*.parquet`,
  `logs/**/*.json`): DuckDB une todos los ficheros que casen en una sola vista.
  El patrón debe casar con al menos un fichero (si no, es un error de fuente) y
  todos los ficheros deben compartir el lector que implica la extensión del
  patrón.

## `checks`

Mapeo de nombre de fuente (debe existir en `sources`) a **lista** de checks.
Cada elemento de la lista es un mapeo con **una única clave**: el tipo de check.

Semántica común: cada check compila a una consulta SQL de DuckDB que devuelve
las filas que **violan** la regla. El check **pasa** si la consulta devuelve 0
filas. Los datos nunca se cargan a Python para validar.

Referenciar una columna que no existe en la fuente es un **error de
configuración** (código de salida 2), no un fallo del check: se valida contra
el esquema real antes de ejecutar nada.

#### `severity`

Cualquier check acepta un campo opcional `severity` con valores `error` (por
defecto) o `warn`:

```yaml
- not_null: {columns: [trip_id], severity: warn}
- row_count: {min: 1000, severity: warn}
```

- `error`: un fallo cuenta para el código de salida (`1`).
- `warn`: el fallo se **reporta** en todos los formatos (aparece como `WARN` en
  la tabla, con su `severity` en JSON, y como `system-out` en JUnit) pero **no**
  afecta al código de salida: si solo fallan checks `warn`, el CLI sale con `0`.
- `--fail-fast` tampoco se detiene ante un `warn`: una advertencia se reporta y
  la ejecución continúa; solo corta con el primer fallo de `severity: error`.
- `severity` solo puede indicarse en la forma de mapeo del check (no en las
  formas abreviadas como `- not_null: trip_id`).

### `not_null`

Las columnas indicadas no contienen `NULL`.

```yaml
- not_null: [trip_id, pickup_ts]   # lista de columnas
- not_null: trip_id                # forma abreviada: una sola columna
```

### `unique`

No hay valores duplicados en la columna (o combinación de columnas).

```yaml
- unique: trip_id                  # forma abreviada: una columna
- unique: [trip_id, pickup_ts]     # clave compuesta
```

Las filas con `NULL` en alguna de las columnas se ignoran (para exigir no
nulos está `not_null`).

### `row_count`

El número de filas de la fuente está dentro de los límites (inclusive).

```yaml
- row_count: {min: 1}
- row_count: {min: 100, max: 100000}
```

- Al menos uno de `min` / `max` es obligatorio; ambos son enteros `>= 0`.
- Si se dan ambos, debe cumplirse `min <= max`.
- Violación: la consulta devuelve una única fila con el recuento cuando está
  fuera de límites.

### `accepted_values`

Todos los valores de la columna pertenecen al conjunto dado.

```yaml
- accepted_values:
    column: payment_type
    values: [card, cash]           # lista no vacía de escalares (str/int/float/bool)
```

Los `NULL` no cuentan como violación (para eso está `not_null`).

### `range`

Los valores numéricos de la columna están dentro de los límites (inclusive).

```yaml
- range: {column: fare, min: 0, max: 1000}
```

- Al menos uno de `min` / `max` es obligatorio; ambos son numéricos.
- Si se dan ambos, debe cumplirse `min <= max`.
- Los `NULL` no cuentan como violación.

### `regex_match`

Todos los valores no nulos de la columna casan con la expresión regular.

```yaml
- regex_match: {column: trip_id, pattern: 't-[0-9]{3}'}
```

- El patrón debe casar con el **valor completo** (`regexp_full_match` de
  DuckDB); usa `.*` explícitos para búsquedas parciales.
- Sintaxis RE2 (la de DuckDB); el patrón se valida al cargar la suite.
- Los `NULL` no cuentan como violación.

### `freshness`

El valor más reciente de la columna temporal no es más antiguo que `max_age`
respecto al momento de ejecución.

```yaml
- freshness: {column: pickup_ts, max_age: 24h}
```

- `max_age` es una **duración** (ver más abajo). La referencia temporal es el
  `now()` de DuckDB en el momento de la ejecución.
- Si la fuente está vacía o la columna es toda `NULL`, el check **pasa**
  (para exigir datos están `row_count` y `not_null`).

### `custom_sql`

Consulta SQL arbitraria cuyas filas resultantes son las violaciones.

```yaml
- custom_sql:
    name: no_negative_duration     # identificador, único dentro de la suite
    query: SELECT * FROM trips WHERE dropoff_ts < pickup_ts
```

- La consulta puede referenciar cualquier fuente declarada por su nombre de
  vista.
- En los informes, el check se reporta con su `name` (no como `custom_sql`).
- Un `;` final se elimina automáticamente; la consulta debe ser un único
  `SELECT`.

## Duraciones

Formato: `<entero><unidad>`, sin espacios. Unidades:

| Unidad | Significado |
| ------ | ----------- |
| `s`    | segundos    |
| `m`    | minutos     |
| `h`    | horas       |
| `d`    | días        |

Ejemplos válidos: `30s`, `15m`, `24h`, `7d`. Inválidos: `24 h`, `1w`, `1.5h`.

## Errores y códigos de salida

- Toda suite inválida produce un mensaje accionable con la ubicación del
  problema, p. ej. `checks.trips[3] (range): necesita al menos 'min' o 'max'`.
  Nunca un traceback.
- Códigos de salida del CLI: `0` todos los checks pasan, `1` hay checks
  fallidos, `2` error de configuración (suite inválida, fuente inexistente...).

## Evolución del formato

`version` solo se incrementará con cambios incompatibles. El parser rechaza
cualquier versión distinta de `1` con un mensaje explícito.
