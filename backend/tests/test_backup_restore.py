"""Product backup/restore contract: complete, verified, atomic, non-destructive."""

from __future__ import annotations

import json
import os
from pathlib import Path
import sqlite3
import stat
import zipfile

import pytest

from careerdesk.bootstrap import cli
from careerdesk.platform.database import init_db
from careerdesk.platform.database import backup as backup_module
from careerdesk.platform.database import schema as database_schema
from careerdesk.platform.database.backup import (
    BackupError,
    create_backup,
    restore_backup,
    verify_backup,
)
from careerdesk.platform.runtime.instance_lock import (
    InstanceAlreadyRunningError,
    acquire_instance_lock,
)


def _source_data(tmp_path: Path) -> Path:
    data = tmp_path / "source-data"
    init_db(str(data / "careerdesk.db"))
    uploads = data / "uploads/resumes/user-a"
    uploads.mkdir(parents=True)
    (uploads / "resume.md").write_text("private resume", encoding="utf-8")
    return data


def _rewrite_archive(
    source: Path,
    destination: Path,
    mutate,
) -> None:
    with zipfile.ZipFile(source, "r") as archive:
        files = {info.filename: archive.read(info) for info in archive.infolist()}
    mutate(files)
    with zipfile.ZipFile(destination, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        for name, payload in files.items():
            archive.writestr(name, payload)


def test_backup_restore_round_trip_is_complete_and_excludes_runtime_state(tmp_path):
    source = _source_data(tmp_path)
    from agentmaker import Message, Scope
    from careerdesk.agentic.memory import build_conversation_memory

    closers = []
    conversation, _ = build_conversation_memory(
        str(source / "careerdesk.db"),
        embedding_enabled=False,
        user_id="user-a",
        resource_closers=closers,
    )
    conversation.append(
        Message(role="user", content="这条历史对话必须进入正式备份"),
        scope=Scope(user="user-a", app="careerdesk", session="backup-session"),
    )
    for close in reversed(closers):
        close()
    (source / "traces.jsonl").write_text("metadata trace", encoding="utf-8")
    (source / "careerdesk.db.pre-v22.backup").write_bytes(b"old snapshot")
    (source / "unmanaged.txt").write_text("not product data", encoding="utf-8")
    backup = tmp_path / "complete.jpbak"

    created = create_backup(source, backup)
    verified = verify_backup(backup)

    assert created.path == verified.path == backup
    assert created.file_count == verified.file_count >= 2
    if os.name == "posix":
        assert stat.S_IMODE(backup.stat().st_mode) == 0o600
    with zipfile.ZipFile(backup) as archive:
        names = set(archive.namelist())
        manifest = json.loads(archive.read("manifest.json"))
    assert "careerdesk.db" in names
    assert "derived.db" not in names
    assert "uploads/resumes/user-a/resume.md" in names
    assert "traces.jsonl" not in names
    assert "careerdesk.db.pre-v22.backup" not in names
    assert "unmanaged.txt" not in names
    assert manifest["database_schema_version"] == database_schema.SCHEMA_VERSION

    destination = tmp_path / "restored-data"
    restored = restore_backup(backup, destination)

    assert restored.path == destination
    assert (destination / "uploads/resumes/user-a/resume.md").read_text() == "private resume"
    assert not (destination / "traces.jsonl").exists()
    assert not (destination / ".careerdesk.instance.lock").exists()
    with sqlite3.connect(destination / "careerdesk.db") as connection:
        assert connection.execute("PRAGMA integrity_check").fetchone() == ("ok",)
        assert connection.execute("PRAGMA foreign_key_check").fetchall() == []
        assert connection.execute("PRAGMA user_version").fetchone() == (
            database_schema.SCHEMA_VERSION,
        )
        assert connection.execute(
            "SELECT role, content FROM session_messages WHERE sc_user = ? AND sc_session = ?",
            ("user-a", "backup-session"),
        ).fetchall() == [("user", "这条历史对话必须进入正式备份")]


def test_backup_refuses_running_app_existing_output_and_output_inside_data(tmp_path):
    source = _source_data(tmp_path)
    existing = tmp_path / "existing.jpbak"
    existing.write_bytes(b"keep me")

    with pytest.raises(BackupError, match="拒绝覆盖"):
        create_backup(source, existing)
    assert existing.read_bytes() == b"keep me"

    with pytest.raises(BackupError, match="活动数据目录"):
        create_backup(source, source / "nested.jpbak")

    with acquire_instance_lock(source, entrypoint="test-app"):
        with pytest.raises(InstanceAlreadyRunningError):
            create_backup(source, tmp_path / "locked.jpbak")
    assert not (tmp_path / "locked.jpbak").exists()


def test_backup_rejects_upload_symlink_without_following_it(tmp_path):
    source = _source_data(tmp_path)
    secret = tmp_path / "outside.txt"
    secret.write_text("outside", encoding="utf-8")
    link = source / "uploads/resumes/user-a/unsafe"
    try:
        link.symlink_to(secret)
    except OSError:
        pytest.skip("platform cannot create test symlinks")

    with pytest.raises((BackupError, ValueError), match="符号链接|链接"):
        create_backup(source, tmp_path / "unsafe.jpbak")

    assert not (tmp_path / "unsafe.jpbak").exists()


def test_checksum_tamper_fails_before_restore_and_leaves_no_partial_directory(tmp_path):
    source = _source_data(tmp_path)
    backup = tmp_path / "valid.jpbak"
    create_backup(source, backup)
    tampered = tmp_path / "tampered.jpbak"

    def mutate(files: dict[str, bytes]) -> None:
        original = files["uploads/resumes/user-a/resume.md"]
        files["uploads/resumes/user-a/resume.md"] = b"x" * len(original)

    _rewrite_archive(backup, tampered, mutate)

    with pytest.raises(BackupError, match="checksum"):
        verify_backup(tampered)
    destination = tmp_path / "must-not-exist"
    with pytest.raises(BackupError, match="checksum"):
        restore_backup(tampered, destination)
    assert not destination.exists()
    assert not list(tmp_path.glob(".must-not-exist.restore-partial-*"))


def test_corrupt_database_with_matching_checksum_still_fails_integrity_gate(tmp_path):
    source = _source_data(tmp_path)
    backup = tmp_path / "valid.jpbak"
    create_backup(source, backup)
    corrupt = tmp_path / "corrupt-db.jpbak"

    def mutate(files: dict[str, bytes]) -> None:
        database = bytearray(files["careerdesk.db"])
        database[:16] = b"not-a-sqlite-db!"
        files["careerdesk.db"] = bytes(database)
        manifest = json.loads(files["manifest.json"])
        import hashlib

        entry = next(item for item in manifest["entries"] if item["path"] == "careerdesk.db")
        entry["sha256"] = hashlib.sha256(database).hexdigest()
        files["manifest.json"] = (
            json.dumps(manifest, sort_keys=True, separators=(",", ":")) + "\n"
        ).encode()

    _rewrite_archive(backup, corrupt, mutate)

    with pytest.raises(BackupError, match="数据库"):
        verify_backup(corrupt)


def test_manifest_traversal_is_rejected_before_any_restore_write(tmp_path):
    source = _source_data(tmp_path)
    backup = tmp_path / "valid.jpbak"
    create_backup(source, backup)
    unsafe = tmp_path / "unsafe-manifest.jpbak"

    def mutate(files: dict[str, bytes]) -> None:
        manifest = json.loads(files["manifest.json"])
        manifest["entries"].append({
            "kind": "directory",
            "mode": 0o700,
            "path": "uploads/../../escaped",
        })
        files["manifest.json"] = json.dumps(manifest).encode()

    _rewrite_archive(backup, unsafe, mutate)
    destination = tmp_path / "restore"

    with pytest.raises(BackupError, match="不安全路径"):
        restore_backup(unsafe, destination)
    assert not destination.exists()
    assert not (tmp_path / "escaped").exists()


def test_restore_never_overwrites_or_merges_an_existing_data_root(tmp_path):
    source = _source_data(tmp_path)
    backup = tmp_path / "valid.jpbak"
    create_backup(source, backup)
    destination = tmp_path / "existing-data"
    destination.mkdir()
    sentinel = destination / "keep.txt"
    sentinel.write_text("untouched", encoding="utf-8")

    with pytest.raises(BackupError, match="拒绝覆盖或合并"):
        restore_backup(backup, destination)

    assert sentinel.read_text() == "untouched"
    assert set(destination.iterdir()) == {sentinel}


def test_restore_publish_race_uses_native_no_replace_and_keeps_racing_target(
    tmp_path,
    monkeypatch,
):
    source = _source_data(tmp_path)
    backup = tmp_path / "valid.jpbak"
    create_backup(source, backup)
    destination = tmp_path / "racing-target"
    original_verify = backup_module._verify_archive

    def verify_then_race(*args, **kwargs):
        manifest = original_verify(*args, **kwargs)
        destination.mkdir()
        return manifest

    monkeypatch.setattr(backup_module, "_verify_archive", verify_then_race)

    with pytest.raises(BackupError, match="已存在"):
        restore_backup(backup, destination)

    assert destination.is_dir()
    assert list(destination.iterdir()) == []
    assert not list(tmp_path.glob(".racing-target.restore-partial-*"))


def test_sqlite_online_backup_includes_committed_wal_content(tmp_path):
    source = _source_data(tmp_path)
    database = source / "careerdesk.db"
    writer = sqlite3.connect(database)
    writer.execute("PRAGMA journal_mode=WAL")
    writer.execute(
        "INSERT INTO meta(key, value) VALUES ('backup_wal_marker', 'committed')",
    )
    writer.commit()
    backup = tmp_path / "wal.jpbak"
    try:
        create_backup(source, backup)
    finally:
        writer.close()
    destination = tmp_path / "wal-restored"

    restore_backup(backup, destination)

    with sqlite3.connect(destination / "careerdesk.db") as connection:
        assert connection.execute(
            "SELECT value FROM meta WHERE key = 'backup_wal_marker'",
        ).fetchone() == ("committed",)


def test_backup_failure_never_publishes_final_or_leaves_sensitive_partial(
    tmp_path,
    monkeypatch,
):
    source = _source_data(tmp_path)
    output = tmp_path / "failed.jpbak"
    original_archive_file = backup_module._archive_regular_file

    def fail_on_upload(archive, path, archive_path, **kwargs):
        if archive_path.startswith("uploads/"):
            raise OSError("simulated disk failure")
        return original_archive_file(archive, path, archive_path, **kwargs)

    monkeypatch.setattr(backup_module, "_archive_regular_file", fail_on_upload)

    with pytest.raises(OSError, match="simulated"):
        create_backup(source, output)

    assert not output.exists()
    assert not list(tmp_path.glob(".failed.jpbak.partial-*"))


def test_cli_backup_verify_restore_and_errors(tmp_path, capsys):
    source = _source_data(tmp_path)
    backup = tmp_path / "cli.jpbak"
    destination = tmp_path / "cli-restored"

    assert cli.main(["backup", str(backup), "--data-dir", str(source)]) == 0
    assert "备份已完成并验证" in capsys.readouterr().out
    assert cli.main(["verify", str(backup)]) == 0
    assert "备份校验通过" in capsys.readouterr().out
    assert cli.main([
        "restore",
        str(backup),
        "--destination",
        str(destination),
    ]) == 0
    assert "原数据未被修改" in capsys.readouterr().out
    assert cli.main(["verify", str(tmp_path / "missing.jpbak")]) == 2
    assert "careerdesk-data" in capsys.readouterr().err


def test_distribution_and_frozen_bundle_expose_data_maintenance_cli():
    root = Path(__file__).resolve().parents[2]
    pyproject = (root / "backend/pyproject.toml").read_text(encoding="utf-8")
    frozen_entry = (root / "desktop/frozen_entry.py").read_text(encoding="utf-8")

    assert 'careerdesk-data = "careerdesk.bootstrap.cli:main"' in pyproject
    assert 'stem.casefold() == "careerdesk-data"' in frozen_entry
    assert "careerdesk.bootstrap.cli" in frozen_entry
