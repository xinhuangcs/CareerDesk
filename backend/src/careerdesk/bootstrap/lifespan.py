"""Startup and reverse-order teardown of process resources."""

from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI

from ..auth import verify_auth_config
from ..core.config import get_settings
from ..platform.database import derived_db_path, init_db, truncate_wal_if_oversized
from ..platform.ai.tracing import close_shared_tracers, maintain_trace_files
from ..platform.runtime import (InstanceLock, InstanceLockError,
                                acquire_instance_lock)
from ..platform.storage.private import harden_managed_data_tree


@asynccontextmanager
async def lifespan(app: FastAPI):
    """Initialize and recover under single ownership of the data root."""
    settings = get_settings()
    transferred_lock = getattr(app.state, "instance_lock", None)
    instance_lock: InstanceLock | None = None
    try:
        if transferred_lock is not None:
            expected_root = Path(settings.data_dir).resolve()
            if (not isinstance(transferred_lock, InstanceLock)
                    or transferred_lock.released
                    or transferred_lock.path.parent != expected_root):
                raise InstanceLockError(
                    "启动器交付的实例锁无效或不属于当前数据目录",
                    lock_path=expected_root / ".careerdesk.instance.lock",
                )
            instance_lock = transferred_lock

        verify_auth_config()
        if instance_lock is None:
            instance_lock = acquire_instance_lock(
                settings.data_dir,
                entrypoint=settings.runtime_mode,
            )
            app.state.instance_lock = instance_lock

    # Lock before database/recovery so a second process cannot misclassify live work.
        harden_managed_data_tree(settings.data_dir)
        trace_path = Path(settings.trace_path)
        maintain_trace_files(trace_path)
        init_db(settings.db_path)

        from ..orchestration.assistant.service import maintain_turn_ledger
        from ..services.recovery import recover_interrupted_work

        maintain_turn_ledger(settings.db_path)
        recover_interrupted_work(settings.db_path)
        # Startup is the only request-free window; truncate oversized WAL if available.
        for database in (settings.db_path, derived_db_path(settings.db_path)):
            truncate_wal_if_oversized(database)
        yield
    finally:
        try:
            close_shared_tracers()
        finally:
        # The lock is the final boundary; closer failures must not retain it forever.
            if instance_lock is not None:
                instance_lock.release()
                if getattr(app.state, "instance_lock", None) is instance_lock:
                    del app.state.instance_lock
