"""Static and executable contracts for the tag-only CI/release pipeline."""

from __future__ import annotations

import json
from pathlib import Path
import re
import sys


REPO_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO_ROOT))

from scripts import frozen_artifact_smoke  # noqa: E402


def test_renovate_config_covers_all_pinned_container_stages_without_automerge():
    dockerfile = (REPO_ROOT / "Dockerfile").read_text(encoding="utf-8")
    config = json.loads((REPO_ROOT / ".github/renovate.json").read_text())
    docker_dependencies = {
        match.group("name")
        for match in re.finditer(
            r"(?:FROM\s+|COPY --from=)(?P<name>[^:@\s]+)(?::[^@\s]+)?@sha256:[0-9a-f]{64}",
            dockerfile,
        )
    }
    docker_rule = next(
        rule for rule in config["packageRules"] if rule["matchManagers"] == ["dockerfile"]
    )

    assert docker_dependencies == {"node", "python", "ghcr.io/astral-sh/uv"}
    assert set(docker_rule["matchDepNames"]) == docker_dependencies
    assert docker_rule["pinDigests"] is True
    assert config["automerge"] is False
    assert all(rule.get("automerge") is False for rule in config["packageRules"])
    assert config["lockFileMaintenance"]["automerge"] is False
    assert config["vulnerabilityAlerts"]["automerge"] is False


def test_automatic_ci_only_runs_for_release_tags():
    workflows = REPO_ROOT / ".github/workflows"
    release = (workflows / "unsigned-release.yml").read_text(encoding="utf-8")
    manual_eval = (workflows / "llm-eval.yml").read_text(encoding="utf-8")

    assert not (workflows / "ci.yml").exists()
    assert re.search(r'^on:\n  push:\n    tags:\n      - "v\*"$', release, re.MULTILINE)
    assert "\n  branches:" not in release
    assert "\n  pull_request:" not in release
    assert "workflow_dispatch:" not in release
    assert "workflow_dispatch:" in manual_eval
    assert "\n  push:" not in manual_eval


def test_unsigned_release_is_automatic_secret_free_and_unambiguously_labeled():
    workflow = (REPO_ROOT / ".github/workflows/unsigned-release.yml").read_text(
        encoding="utf-8"
    )
    action_references = re.findall(
        r"^\s*-\s+uses:\s*([^\s#]+)", workflow, re.MULTILINE
    )

    assert action_references
    assert all(re.search(r"@[0-9a-f]{40}$", reference) for reference in action_references)
    validate_job = workflow[workflow.index("  validate:") : workflow.index("  macos:")]
    assert "ref: main" in validate_job
    assert "fetch-depth: 0" in validate_job
    assert 'git cat-file -t "${GITHUB_REF_NAME}"' in validate_job
    assert 'refs/remotes/origin/main' in validate_job
    assert "secrets." not in workflow
    assert "environment: release" not in workflow
    assert workflow.count("UNSIGNED.zip") >= 8
    assert 'build-manifest.json")" = "ad-hoc"' in workflow
    assert '$manifest.code_signing -ne "unsigned"' in workflow
    assert '--notes "$(<DISCLAIMER.md)"' not in workflow
    assert "--generate-notes" not in workflow
    assert "cp DISCLAIMER.md" not in workflow
    assert "cp zh/DISCLAIMER.md" not in workflow
    assert 'Copy-Item "DISCLAIMER.md"' not in workflow
    assert 'Copy-Item "zh\\DISCLAIMER.md"' not in workflow
    for removed_name in (
        "PRIVACY.md",
        "SECURITY.md",
        "THIRD_PARTY_NOTICES.md",
        "UNSIGNED_RELEASE_NOTICE.md",
        "SUPPORT.md",
    ):
        assert removed_name not in workflow
    assert "UNSIGNED-README.md" not in workflow
    assert "release-assets/DISCLAIMER_zh.md" not in workflow
    assert "cp LICENSE release-assets/LICENSE" in workflow
    assert "## Download and run" in workflow
    assert "## 下载与运行" in workflow
    assert "GitHub Releases" not in workflow
    assert "Expand-Archive -LiteralPath $archive" in workflow
    assert "archive must contain only the app and build manifest" in workflow
    assert "(UNSIGNED convenience build)" in workflow
    assert workflow.count("contents: write") == 1


def test_frozen_smoke_subprocess_environment_is_an_explicit_secret_free_allowlist(tmp_path):
    root = tmp_path / "new/nested/root"
    environment = frozen_artifact_smoke._isolated_environment(root, port=32123)
    source = (REPO_ROOT / "scripts/frozen_artifact_smoke.py").read_text(encoding="utf-8")

    assert environment["PORT"] == "32123"
    assert environment["PYTHON_KEYRING_BACKEND"] == "keyring.backends.null.Keyring"
    assert environment["APP_LLM_MODEL"] == ""
    assert environment["CAREERDESK_HEADLESS"] == "1"
    assert (root / "home").is_dir()
    assert (root / "local-app-data").is_dir()
    assert (root / "temp").is_dir()
    assert "os.environ.copy" not in source
    assert not any(
        re.search(r"(?:API_KEY|TOKEN|SECRET|PASSWORD|CREDENTIAL)", name, re.IGNORECASE)
        for name in environment
    )


def test_frozen_data_cli_output_is_always_decoded_as_utf8(monkeypatch, tmp_path):
    calls = []

    class Result:
        returncode = 0
        stdout = "备份已完成"
        stderr = ""

    def fake_run(command, **kwargs):
        calls.append((command, kwargs))
        return Result()

    monkeypatch.setattr(frozen_artifact_smoke.subprocess, "run", fake_run)

    frozen_artifact_smoke._run_data_command(
        [str(tmp_path / "careerdesk-data.exe"), "verify", "smoke.jpbak"],
        {"SYSTEMROOT": "C:\\Windows"},
    )

    assert calls[0][1]["encoding"] == "utf-8"
    assert calls[0][1]["errors"] == "strict"


def test_frozen_database_check_always_closes_its_connection(monkeypatch, tmp_path):
    class Result:
        def __init__(self, value):
            self.value = value

        def fetchone(self):
            return self.value

    class Connection:
        closed = False

        def execute(self, statement):
            values = {
                "PRAGMA integrity_check": ("ok",),
                "PRAGMA foreign_key_check": None,
                "PRAGMA user_version": (frozen_artifact_smoke.SCHEMA_VERSION,),
            }
            return Result(values[statement])

        def close(self):
            self.closed = True

    connection = Connection()
    monkeypatch.setattr(
        frozen_artifact_smoke.sqlite3,
        "connect",
        lambda _path: connection,
    )

    frozen_artifact_smoke._check_database(tmp_path / "careerdesk.db")

    assert connection.closed


def test_tag_release_runs_real_frozen_artifacts_on_both_native_oses():
    workflow = (REPO_ROOT / ".github/workflows/unsigned-release.yml").read_text(
        encoding="utf-8"
    )
    native_jobs = workflow[workflow.index("  macos:") : workflow.index("  publish:")]

    assert "runs-on: macos-15" in native_jobs
    assert "runs-on: windows-2025" in native_jobs
    release_dependencies = "needs: [validate, quality, python-compat, docker-runtime]"
    assert native_jobs.count(release_dependencies) == 2
    assert native_jobs.count("desktop/package_desktop.py") == 2
    assert native_jobs.count("scripts/frozen_artifact_smoke.py") == 2
    assert native_jobs.count("actions/upload-artifact") == 2


def test_local_macos_one_click_package_script_reuses_the_verified_pipeline():
    script_path = REPO_ROOT / "build-local-macos-package.command"
    script = script_path.read_text(encoding="utf-8")

    assert script_path.stat().st_mode & 0o111
    assert script.startswith("#!/bin/zsh\nset -euo pipefail\n")
    for command in (
        "npm --prefix frontend ci",
        "npm --prefix frontend run build",
        "uv sync --project backend --group desktop-build --locked",
        "uv build --project backend --wheel",
        "desktop/package_desktop.py",
        "scripts/frozen_artifact_smoke.py",
        "codesign --verify --deep --strict",
        "ditto -c -k --sequesterRsrc --keepParent",
        "unzip -tq",
    ):
        assert command in script
    assert "UNSIGNED-local-${TIMESTAMP}" in script
    assert "UNSIGNED-README.md" not in script
    assert script.count('find "$STAGED_DIRECTORY" -mindepth 1 -maxdepth 1') == 1
    assert script.count('find "$VERIFY_DIRECTORY/$PACKAGE_NAME" -mindepth 1 -maxdepth 1') == 1
    assert 'mv "$STAGED_DIRECTORY" "$FINAL_DIRECTORY"' in script
    assert 'mv "$STAGED_ARCHIVE" "$FINAL_ARCHIVE"' in script
    assert 'rm -rf -- "$STAGING_ROOT" "$WORK_ROOT"' in script
    assert 'rm -rf -- "$FINAL_DIRECTORY"' not in script
    assert 'rm -f -- "$FINAL_ARCHIVE"' not in script
