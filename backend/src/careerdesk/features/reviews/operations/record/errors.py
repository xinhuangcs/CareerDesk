"""Public operation errors and the internal safety exception."""


class ReviewRecordOperationNotFound(LookupError):
    """Operation/reference is absent or belongs to another tenant."""


class ReviewRecordOperationConflict(RuntimeError):
    """A persisted contract, command identity or dependency no longer matches."""


class _UnsafeRecordDependency(ValueError):
    """Persisted Review data exceeds a bounded or typed safety contract."""
