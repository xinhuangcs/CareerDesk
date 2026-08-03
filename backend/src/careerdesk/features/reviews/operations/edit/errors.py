"""Review timeline-entry edit errors."""


class ReviewTimelineEntryEditOperationNotFound(LookupError):
    pass


class ReviewTimelineEntryEditOperationConflict(RuntimeError):
    pass


class _EditTargetMissing(LookupError):
    pass


class _UnsafeEditDependency(RuntimeError):
    pass
