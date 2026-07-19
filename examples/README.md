# Ejemplos

Cada directorio contiene un dataset pequeño y una suite YAML funcional:

- [taxi](taxi/) — viajes de taxi en CSV con errores deliberados; usa los 8
  tipos de check del contrato y muestra cómo se ven los fallos.

Para ejecutar cualquiera:

```console
$ uv run quacklint run examples/<nombre>/quacklint.yaml
```
