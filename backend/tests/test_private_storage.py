"""Private storage modes, link boundaries, and subprocess-local umask."""

import errno
import os
import stat
from pathlib import Path

import pytest

from careerdesk.platform.storage.private import (UnsafeManagedPath, ensure_private_directory,
                                                harden_managed_data_tree,
                                                harden_private_file_if_exists,
                                                prepare_private_file)


def _mode(path: Path) -> int:
    return stat.S_IMODE(path.lstat().st_mode)


@pytest.mark.skipif(os.name != "posix", reason="POSIX mode contract")
def test_new_managed_paths_are_private_even_with_permissive_umask(tmp_path):
    old_umask = os.umask(0)
    try:
        directory = ensure_private_directory(tmp_path / "data" / "uploads")
        secret = prepare_private_file(directory / "secret.bin")
    finally:
        os.umask(old_umask)

    assert _mode(tmp_path / "data") == 0o700
    assert _mode(directory) == 0o700
    assert _mode(secret) == 0o600


@pytest.mark.skipif(os.name != "posix", reason="POSIX mode contract")
def test_existing_paths_only_lose_non_owner_access(tmp_path):
    directory = tmp_path / "data"
    directory.mkdir(mode=0o755)
    secret = directory / "secret"
    secret.write_bytes(b"unchanged")
    os.chmod(secret, 0o644)
    inode = secret.stat().st_ino

    ensure_private_directory(directory)
    prepare_private_file(secret)

    assert _mode(directory) == 0o700
    assert _mode(secret) == 0o600
    assert secret.stat().st_ino == inode
    assert secret.read_bytes() == b"unchanged"

    os.chmod(secret, 0o400)
    prepare_private_file(secret)
    assert _mode(secret) == 0o400


def test_configured_root_symlink_is_preserved_but_final_file_is_canonical(tmp_path):
    target = tmp_path / "actual-data"
    target.mkdir()
    link = tmp_path / "data-link"
    link.symlink_to(target, target_is_directory=True)

    private = prepare_private_file(link / "careerdesk.db")

    assert link.is_symlink()
    assert private == target.resolve() / "careerdesk.db"
    assert private.is_file()
    if os.name == "posix":
        assert _mode(target) == 0o700
        assert _mode(private) == 0o600


def test_sensitive_final_symlink_non_regular_and_hardlink_are_rejected(tmp_path):
    target = tmp_path / "target"
    target.write_bytes(b"do-not-touch")

    symlink = tmp_path / "secret-link"
    symlink.symlink_to(target)
    with pytest.raises(UnsafeManagedPath, match="符号链接"):
        prepare_private_file(symlink, private_parent=False)
    assert symlink.is_symlink() and target.read_bytes() == b"do-not-touch"

    directory = tmp_path / "not-a-file"
    directory.mkdir()
    with pytest.raises(UnsafeManagedPath, match="普通文件"):
        prepare_private_file(directory, private_parent=False)

    hardlink = tmp_path / "hardlink"
    try:
        os.link(target, hardlink)
    except OSError as error:
        pytest.skip(f"hard links unavailable: {error}")
    with pytest.raises(UnsafeManagedPath, match="硬链接"):
        prepare_private_file(hardlink, private_parent=False)
    assert target.read_bytes() == b"do-not-touch"


def test_sensitive_symlink_is_rejected_without_relying_on_o_nofollow(tmp_path, monkeypatch):
    target = tmp_path / "outside"
    target.write_bytes(b"KEEP")
    link = tmp_path / "secret-link"
    try:
        link.symlink_to(target)
    except OSError as error:
        pytest.skip(f"symlinks unavailable: {error}")
    monkeypatch.delattr(os, "O_NOFOLLOW", raising=False)

    with pytest.raises(UnsafeManagedPath, match="符号链接"):
        prepare_private_file(link, private_parent=False)

    assert link.is_symlink()
    assert target.read_bytes() == b"KEEP"


def test_sidecar_disappearance_between_lstat_and_open_is_benign(tmp_path, monkeypatch):
    sidecar = tmp_path / "careerdesk.db-wal"
    sidecar.write_bytes(b"wal")
    original_open = os.open
    removed = False

    def disappearing_open(path, flags, mode=0o777, *, dir_fd=None):
        nonlocal removed
        if Path(path) == sidecar and not removed:
            removed = True
            sidecar.unlink()
            raise FileNotFoundError(errno.ENOENT, "sidecar disappeared", str(path))
        if dir_fd is None:
            return original_open(path, flags, mode)
        return original_open(path, flags, mode, dir_fd=dir_fd)

    monkeypatch.setattr(os, "open", disappearing_open)

    harden_private_file_if_exists(sidecar)

    assert removed and not sidecar.exists()


@pytest.mark.skipif(os.name != "posix", reason="POSIX sidecar unlink race")
def test_sidecar_disappearance_after_open_is_benign(tmp_path, monkeypatch):
    sidecar = tmp_path / "careerdesk.db-shm"
    sidecar.write_bytes(b"shm")
    original_open = os.open
    original_fstat = os.fstat
    watched_descriptors: set[int] = set()

    def recording_open(path, flags, mode=0o777, *, dir_fd=None):
        if dir_fd is None:
            descriptor = original_open(path, flags, mode)
        else:
            descriptor = original_open(path, flags, mode, dir_fd=dir_fd)
        if Path(path) == sidecar:
            watched_descriptors.add(descriptor)
        return descriptor

    def disappearing_fstat(descriptor):
        info = original_fstat(descriptor)
        if descriptor in watched_descriptors and sidecar.exists():
            sidecar.unlink()
        return info

    monkeypatch.setattr(os, "open", recording_open)
    monkeypatch.setattr(os, "fstat", disappearing_fstat)

    harden_private_file_if_exists(sidecar)

    assert not sidecar.exists()


@pytest.mark.skipif(os.name != "posix", reason="POSIX mode contract")
def test_recreated_regular_sidecar_is_revalidated_and_hardened(tmp_path, monkeypatch):
    sidecar = tmp_path / "careerdesk.db-wal"
    sidecar.write_bytes(b"old")
    original_open = os.open
    recreated = False

    def recreating_open(path, flags, mode=0o777, *, dir_fd=None):
        nonlocal recreated
        if Path(path) == sidecar and not recreated:
            recreated = True
            sidecar.unlink()
            sidecar.write_bytes(b"new")
            os.chmod(sidecar, 0o644)
            raise FileNotFoundError(errno.ENOENT, "sidecar recreated", str(path))
        if dir_fd is None:
            return original_open(path, flags, mode)
        return original_open(path, flags, mode, dir_fd=dir_fd)

    monkeypatch.setattr(os, "open", recreating_open)

    harden_private_file_if_exists(sidecar)

    assert recreated and sidecar.read_bytes() == b"new"
    assert _mode(sidecar) == 0o600


@pytest.mark.skipif(os.name != "posix", reason="POSIX mode contract")
def test_sidecar_recreated_after_open_is_revalidated_and_hardened(tmp_path, monkeypatch):
    sidecar = tmp_path / "careerdesk.db-wal"
    sidecar.write_bytes(b"old")
    original_open = os.open
    original_fstat = os.fstat
    watched_descriptors: set[int] = set()
    recreated = False

    def recording_open(path, flags, mode=0o777, *, dir_fd=None):
        if dir_fd is None:
            descriptor = original_open(path, flags, mode)
        else:
            descriptor = original_open(path, flags, mode, dir_fd=dir_fd)
        if Path(path) == sidecar:
            watched_descriptors.add(descriptor)
        return descriptor

    def recreating_fstat(descriptor):
        nonlocal recreated
        info = original_fstat(descriptor)
        if descriptor in watched_descriptors and not recreated:
            recreated = True
            sidecar.unlink()
            sidecar.write_bytes(b"new")
            os.chmod(sidecar, 0o644)
        return info

    monkeypatch.setattr(os, "open", recording_open)
    monkeypatch.setattr(os, "fstat", recreating_fstat)

    harden_private_file_if_exists(sidecar)

    assert recreated and sidecar.read_bytes() == b"new"
    assert _mode(sidecar) == 0o600


@pytest.mark.skipif(os.name != "posix", reason="POSIX sidecar unlink race")
def test_repeated_safe_shaped_sidecar_replacement_fails_closed(tmp_path, monkeypatch):
    sidecar = tmp_path / "careerdesk.db-shm"
    sidecar.write_bytes(b"0")
    original_open = os.open
    original_fstat = os.fstat
    watched_descriptors: set[int] = set()
    replacements = 0

    def recording_open(path, flags, mode=0o777, *, dir_fd=None):
        if dir_fd is None:
            descriptor = original_open(path, flags, mode)
        else:
            descriptor = original_open(path, flags, mode, dir_fd=dir_fd)
        if Path(path) == sidecar:
            watched_descriptors.add(descriptor)
        return descriptor

    def churning_fstat(descriptor):
        nonlocal replacements
        info = original_fstat(descriptor)
        if descriptor in watched_descriptors:
            replacements += 1
            sidecar.unlink()
            sidecar.write_bytes(str(replacements).encode())
        return info

    monkeypatch.setattr(os, "open", recording_open)
    monkeypatch.setattr(os, "fstat", churning_fstat)

    with pytest.raises(UnsafeManagedPath, match="反复发生替换"):
        harden_private_file_if_exists(sidecar)

    assert replacements == 3


def test_sidecar_disappearance_does_not_swallow_symlink_replacement(tmp_path, monkeypatch):
    sidecar = tmp_path / "careerdesk.db-wal"
    outside = tmp_path / "outside"
    sidecar.write_bytes(b"wal")
    outside.write_bytes(b"KEEP")
    original_open = os.open
    replaced = False

    def replacing_open(path, flags, mode=0o777, *, dir_fd=None):
        nonlocal replaced
        if Path(path) == sidecar and not replaced:
            replaced = True
            sidecar.unlink()
            sidecar.symlink_to(outside)
            raise FileNotFoundError(errno.ENOENT, "sidecar replaced", str(path))
        if dir_fd is None:
            return original_open(path, flags, mode)
        return original_open(path, flags, mode, dir_fd=dir_fd)

    monkeypatch.setattr(os, "open", replacing_open)

    with pytest.raises(UnsafeManagedPath, match="符号链接"):
        harden_private_file_if_exists(sidecar)

    assert sidecar.is_symlink()
    assert outside.read_bytes() == b"KEEP"


def test_strict_main_file_disappearance_after_observation_is_not_recreated(tmp_path, monkeypatch):
    database = tmp_path / "careerdesk.db"
    database.write_bytes(b"important database")
    original_open = os.open
    removed = False

    def disappearing_open(path, flags, mode=0o777, *, dir_fd=None):
        nonlocal removed
        if Path(path) == database and not removed and not flags & os.O_CREAT:
            removed = True
            database.unlink()
            raise FileNotFoundError(errno.ENOENT, "main file disappeared", str(path))
        if dir_fd is None:
            return original_open(path, flags, mode)
        return original_open(path, flags, mode, dir_fd=dir_fd)

    monkeypatch.setattr(os, "open", disappearing_open)

    with pytest.raises(FileNotFoundError, match="main file disappeared"):
        prepare_private_file(database, private_parent=False)

    assert removed and not database.exists()


def test_windows_permission_error_race_still_rejects_new_directory(tmp_path, monkeypatch):
    candidate = tmp_path / "raced-directory"
    original_open = os.open

    def raced_open(path, flags, mode=0o777, *, dir_fd=None):
        if Path(path) == candidate and flags & os.O_EXCL:
            candidate.mkdir()
            raise PermissionError(errno.EACCES, "simulated Windows create denial", str(path))
        if dir_fd is None:
            return original_open(path, flags, mode)
        return original_open(path, flags, mode, dir_fd=dir_fd)

    monkeypatch.setattr(os, "open", raced_open)

    with pytest.raises(UnsafeManagedPath, match="普通文件"):
        prepare_private_file(candidate, private_parent=False)


def test_permission_error_without_existing_target_is_preserved(tmp_path, monkeypatch):
    candidate = tmp_path / "denied"
    denial = PermissionError(errno.EACCES, "simulated parent ACL denial", str(candidate))
    original_open = os.open

    def denied_open(path, flags, mode=0o777, *, dir_fd=None):
        if Path(path) == candidate and flags & os.O_EXCL:
            raise denial
        if dir_fd is None:
            return original_open(path, flags, mode)
        return original_open(path, flags, mode, dir_fd=dir_fd)

    monkeypatch.setattr(os, "open", denied_open)

    with pytest.raises(PermissionError) as caught:
        prepare_private_file(candidate, private_parent=False)

    assert caught.value is denial
    assert not candidate.exists()


def test_filesystem_root_is_rejected_before_permissions_can_change(tmp_path):
    root = Path(tmp_path.anchor)
    before = root.stat().st_mode

    with pytest.raises(UnsafeManagedPath, match="文件系统根"):
        ensure_private_directory(root)
    with pytest.raises(UnsafeManagedPath, match="文件系统根"):
        prepare_private_file(root / "careerdesk.db")

    assert root.stat().st_mode == before


@pytest.mark.skipif(os.name != "posix", reason="POSIX mode contract")



def test_harden_managed_tree_rejects_sensitive_hardlinks(tmp_path):
    data = tmp_path / "data"
    data.mkdir()
    outside = tmp_path / "outside.db"
    outside.write_bytes(b"keep")
    try:
        os.link(outside, data / "careerdesk.db")
    except OSError as error:
        pytest.skip(f"hard links unavailable: {error}")

    with pytest.raises(UnsafeManagedPath, match="硬链接"):
        harden_managed_data_tree(data)
    assert outside.read_bytes() == b"keep"
