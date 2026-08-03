"""Cross-platform infrastructure for process runtime boundaries."""

from .instance_lock import (
    InstanceAlreadyRunningError,
    InstanceLock,
    InstanceLockError,
    acquire_instance_lock,
)

__all__ = [
    "InstanceAlreadyRunningError",
    "InstanceLock",
    "InstanceLockError",
    "acquire_instance_lock",
]
