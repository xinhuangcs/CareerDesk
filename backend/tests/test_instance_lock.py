
from __future__ import annotations

import errno
import json
import multiprocessing
import os
import stat
import threading
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import pytest

from careerdesk.platform.runtime import (
    InstanceAlreadyRunningError,
    InstanceLockError,
    acquire_instance_lock,
)
from careerdesk.platform.runtime import instance_lock as lock_module


def _hold_lock_until_killed(data_dir: str, ready) -> None:
    lock = acquire_instance_lock(data_dir, entrypoint="killed-child")
    ready.set()
    threading.Event().wait()
    lock.release()


def _read_owner_metadata(lock_path: Path) -> bytes:
    """Read JSON without touching Windows' mandatory byte-range lock."""
    with lock_path.open("rb") as handle:
        handle.seek(lock_module._METADATA_OFFSET)
        return handle.read()


def test_same_process_contention_preserves_owner_and_release_reuses_file(tmp_path):
    data_dir = tmp_path / "data"
    first = acquire_instance_lock(data_dir, entrypoint="first", url="http://127.0.0.1:8000")
    lock_path = first.path
    first_stat = lock_path.stat()
    original_metadata = _read_owner_metadata(lock_path)

    with pytest.raises(InstanceAlreadyRunningError) as caught:
        acquire_instance_lock(data_dir, entrypoint="second")

    assert caught.value.lock_path == lock_path
    assert caught.value.owner == first.owner
    assert _read_owner_metadata(lock_path) == original_metadata

    first.release()
    first.release()
    assert first.released is True
    assert lock_path.exists()

    second = acquire_instance_lock(data_dir, entrypoint="second")
    try:
        second_stat = lock_path.stat()
        if first_stat.st_ino and second_stat.st_ino:
            assert second_stat.st_ino == first_stat.st_ino
    finally:
        second.release()


def test_concurrent_threads_have_exactly_one_owner(tmp_path):
    workers = 16
    start = threading.Barrier(workers)
    attempted = threading.Barrier(workers)
    data_dir = tmp_path / "data"

    def compete(index: int) -> str:
        start.wait(timeout=10)
        acquired = None
        failure = None
        try:
            acquired = acquire_instance_lock(data_dir, entrypoint=f"thread-{index}")
            outcome = "owner"
        except InstanceAlreadyRunningError:
            outcome = "blocked"
        except BaseException as error:
            failure = error
            outcome = "error"
        attempted.wait(timeout=10)
        if acquired is not None:
            acquired.release()
        if failure is not None:
            raise failure
        return outcome

    with ThreadPoolExecutor(max_workers=workers) as pool:
        outcomes = list(pool.map(compete, range(workers)))

    assert outcomes.count("owner") == 1
    assert outcomes.count("blocked") == workers - 1


def test_context_manager_releases_after_body_error(tmp_path):
    data_dir = tmp_path / "data"

    with pytest.raises(RuntimeError, match="body failed"):
        with acquire_instance_lock(data_dir):
            raise RuntimeError("body failed")

    with acquire_instance_lock(data_dir) as reacquired:
        assert reacquired.released is False


def test_canonical_directory_aliases_share_process_slot(tmp_path):
    data_dir = tmp_path / "data"
    data_dir.mkdir()
    alias = tmp_path / "data-alias"
    try:
        alias.symlink_to(data_dir, target_is_directory=True)
    except OSError as error:
        pytest.skip(f"当前平台不允许创建目录符号链接：{error}")

    with acquire_instance_lock(data_dir, entrypoint="real"):
        with pytest.raises(InstanceAlreadyRunningError):
            acquire_instance_lock(alias, entrypoint="alias")


def test_stale_file_is_not_mistaken_for_an_active_lock(tmp_path):
    data_dir = tmp_path / "data"
    data_dir.mkdir()
    lock_path = data_dir / ".careerdesk.instance.lock"
    lock_path.write_text(
        json.dumps({"pid": 999999, "entrypoint": "stale"}), encoding="utf-8",
    )

    with acquire_instance_lock(data_dir, entrypoint="new-owner") as acquired:
        assert acquired.owner["pid"] == os.getpid()
        assert acquired.owner["entrypoint"] == "new-owner"
        assert json.loads(_read_owner_metadata(lock_path)) == acquired.owner


def test_lock_descriptor_and_posix_permissions_are_private(tmp_path):
    data_dir = tmp_path / "new-data"

    with acquire_instance_lock(data_dir) as acquired:
        assert os.get_inheritable(acquired.fileno()) is False
        if os.name != "nt":
            assert stat.S_IMODE(data_dir.stat().st_mode) == 0o700
            assert stat.S_IMODE(acquired.path.stat().st_mode) == 0o600

    with pytest.raises(ValueError, match="已经释放"):
        acquired.fileno()


@pytest.mark.skipif(os.name == "nt", reason="Windows 使用 ACL，不检查 POSIX mode")
def test_existing_posix_data_root_is_hardened_without_recursive_chmod(tmp_path):
    data_dir = tmp_path / "existing-data"
    child_dir = data_dir / "uploads"
    child_file = child_dir / "resume.txt"
    child_dir.mkdir(parents=True)
    child_file.write_text("private", encoding="utf-8")
    os.chmod(data_dir, 0o755)
    os.chmod(child_dir, 0o755)
    os.chmod(child_file, 0o644)

    with acquire_instance_lock(data_dir):
        assert stat.S_IMODE(data_dir.stat().st_mode) == 0o700
        assert stat.S_IMODE(child_dir.stat().st_mode) == 0o755
        assert stat.S_IMODE(child_file.stat().st_mode) == 0o644


def test_lock_path_symlink_is_rejected_without_touching_target(tmp_path):
    data_dir = tmp_path / "data"
    data_dir.mkdir()
    target = tmp_path / "target"
    target.write_text("do-not-touch", encoding="utf-8")
    lock_path = data_dir / ".careerdesk.instance.lock"
    try:
        lock_path.symlink_to(target)
    except OSError as error:
        pytest.skip(f"当前平台不允许创建文件符号链接：{error}")

    with pytest.raises(InstanceLockError, match="符号链接"):
        acquire_instance_lock(data_dir)

    assert target.read_text(encoding="utf-8") == "do-not-touch"
    assert lock_path.is_symlink()


def test_non_regular_lock_path_is_rejected(tmp_path):
    data_dir = tmp_path / "data"
    data_dir.mkdir()
    (data_dir / ".careerdesk.instance.lock").mkdir()

    with pytest.raises(InstanceLockError):
        acquire_instance_lock(data_dir)


def test_hardlinked_lock_path_is_rejected_before_external_file_is_changed(tmp_path):
    data_dir = tmp_path / "data"
    data_dir.mkdir()
    outside = tmp_path / "outside"
    outside.write_bytes(b"KEEP-EXACTLY")
    original_mode = stat.S_IMODE(outside.stat().st_mode)
    try:
        os.link(outside, data_dir / ".careerdesk.instance.lock")
    except OSError as error:
        pytest.skip(f"hard links unavailable: {error}")

    with pytest.raises(InstanceLockError, match="硬链接"):
        acquire_instance_lock(data_dir)

    assert outside.read_bytes() == b"KEEP-EXACTLY"
    assert stat.S_IMODE(outside.stat().st_mode) == original_mode


def test_filesystem_root_is_rejected_before_any_permission_change():
    root = Path(Path.cwd().anchor)
    before = stat.S_IMODE(root.stat().st_mode)

    with pytest.raises(InstanceLockError, match="专用子目录"):
        acquire_instance_lock(root)

    assert stat.S_IMODE(root.stat().st_mode) == before


@pytest.mark.parametrize("filesystem", ["fakeowner", "virtiofs", "nfs4", "cifs", "fuse.sshfs"])
def test_filesystems_without_reliable_lock_or_private_mode_contract_fail_closed(
    tmp_path, monkeypatch, filesystem,
):
    data_dir = tmp_path / "data"
    monkeypatch.setattr(lock_module, "_filesystem_type", lambda _path: filesystem)

    with pytest.raises(InstanceLockError, match="Docker Desktop 请使用 named volume"):
        acquire_instance_lock(data_dir)

    assert data_dir.is_dir()
    assert not (data_dir / ".careerdesk.instance.lock").exists()


def test_known_local_filesystem_can_acquire_normally(tmp_path, monkeypatch):
    monkeypatch.setattr(lock_module, "_filesystem_type", lambda _path: "ext4")

    with acquire_instance_lock(tmp_path / "data"):
        pass


def test_unexpected_lock_backend_error_fails_closed_and_cleans_registry(tmp_path, monkeypatch):
    data_dir = tmp_path / "data"
    original = lock_module._lock_fd

    def fail(_fd: int) -> None:
        raise OSError(errno.EIO, "simulated filesystem failure")

    monkeypatch.setattr(lock_module, "_lock_fd", fail)
    with pytest.raises(InstanceLockError) as caught:
        acquire_instance_lock(data_dir)
    assert not isinstance(caught.value, InstanceAlreadyRunningError)

    monkeypatch.setattr(lock_module, "_lock_fd", original)
    with acquire_instance_lock(data_dir):
        pass


def test_unlock_error_still_closes_descriptor_and_cleans_registry(tmp_path, monkeypatch):
    data_dir = tmp_path / "data"
    acquired = acquire_instance_lock(data_dir)
    original = lock_module._unlock_fd

    def fail(_fd: int) -> None:
        raise OSError(errno.EIO, "simulated unlock failure")

    monkeypatch.setattr(lock_module, "_unlock_fd", fail)
    with pytest.raises(InstanceLockError, match="显式解锁"):
        acquired.release()
    assert acquired.released is True

    monkeypatch.setattr(lock_module, "_unlock_fd", original)
    with acquire_instance_lock(data_dir):
        pass


def test_strongly_killed_process_releases_operating_system_lock(tmp_path):
    context = multiprocessing.get_context("spawn")
    ready = context.Event()
    data_dir = tmp_path / "data"
    process = context.Process(target=_hold_lock_until_killed, args=(str(data_dir), ready))
    process.start()
    try:
        assert ready.wait(10), f"子进程未取得锁，exitcode={process.exitcode}"
        with pytest.raises(InstanceAlreadyRunningError) as caught:
            acquire_instance_lock(data_dir, entrypoint="parent")
        assert caught.value.owner is not None
        assert caught.value.owner["entrypoint"] == "killed-child"
    finally:
        if process.is_alive():
            process.kill()
        process.join(timeout=10)

    assert not process.is_alive()
    with acquire_instance_lock(data_dir, entrypoint="recovered"):
        pass


@pytest.mark.parametrize(
    ("entrypoint", "url"),
    [
        ("", None),
        ("server\nsecret", None),
        ("server", "http://127.0.0.1/\nsecret"),
    ],
)
def test_metadata_fields_reject_control_or_empty_text(tmp_path, entrypoint, url):
    with pytest.raises(ValueError):
        acquire_instance_lock(tmp_path / "data", entrypoint=entrypoint, url=url)
