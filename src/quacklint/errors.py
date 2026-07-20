"""quacklint error hierarchy.

The CLI catches `QuacklintError` and shows its message as-is: messages must be
actionable and self-contained, never relying on a traceback.
"""


class QuacklintError(Exception):
    """Base quacklint error."""


class SpecError(QuacklintError):
    """The YAML suite is invalid (syntax, structure or values)."""


class SourceError(QuacklintError):
    """A declared source cannot be resolved to a DuckDB view."""


class ExecutionError(QuacklintError):
    """Failure running a check's SQL against DuckDB."""
