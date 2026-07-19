# Filosofía de quacklint

## Por qué existe

Los checks de calidad de datos suelen acabar como scripts ad hoc de pandas:
lentos, imperativos, imposibles de revisar y acoplados al entorno de quien los
escribió. quacklint apuesta por lo contrario:

- **Declarativo.** Una suite YAML dice *qué* debe cumplirse, no *cómo*
  comprobarlo. El YAML se revisa en un PR igual que el código.
- **SQL primero.** Todo check compila a SQL de DuckDB y se ejecuta donde están
  los datos. Nunca se cargan datos a pandas ni a memoria de Python para validar:
  DuckDB lee Parquet/CSV/JSON directamente y en paralelo.
- **DuckDB como motor.** Sin servicios, sin credenciales, sin infraestructura:
  un proceso local que escala a ficheros de gigabytes.
- **Hecho para CI.** Códigos de salida estables, salida `table`/`json`/`junit`,
  y errores de configuración accionables (nunca tracebacks). Una suite rota debe
  romper el pipeline con un mensaje que diga exactamente qué arreglar.

## Principios de diseño

1. La violación de una regla es una consulta: cada check produce el SQL que
   devuelve las filas inválidas. Cero filas = check pasa. Esto hace los checks
   componibles, inspeccionables (`to_sql()`) y depurables con cualquier cliente
   DuckDB.
2. El formato YAML es un contrato versionado ([spec.yaml.md](spec.yaml.md)); el
   parser no acepta nada que el contrato no documente.
3. Extensible sin tocar el núcleo: los checks se registran vía decorador, y
   `custom_sql` cubre lo que aún no tiene check dedicado.
