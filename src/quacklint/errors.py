"""Jerarquía de errores de quacklint.

El CLI captura `QuacklintError` y muestra su mensaje tal cual: los mensajes deben
ser accionables y autocontenidos, nunca depender de un traceback.
"""


class QuacklintError(Exception):
    """Error base de quacklint."""


class SpecError(QuacklintError):
    """La suite YAML es inválida (sintaxis, estructura o valores)."""


class SourceError(QuacklintError):
    """Una fuente declarada no se puede resolver a una vista DuckDB."""


class ExecutionError(QuacklintError):
    """Fallo al ejecutar el SQL de un check contra DuckDB."""
