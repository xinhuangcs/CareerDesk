"""Self-contained desktop packaging contract without invoking PyInstaller."""

from __future__ import annotations

import json
from pathlib import Path
import sys
import zipfile

import pytest


REPO_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO_ROOT))

from desktop import package_desktop  # noqa: E402


def _skill_files() -> dict[str, bytes]:
    return {
        f"careerdesk/agentic/skills/{name}/{filename}": f"# {name}\n".encode()
        for name in package_desktop._REQUIRED_SKILL_NAMES
        for filename in ("SKILL.md", "SKILL.en.md")
    }


def _write_test_wheel(path: Path, *, extra: dict[str, bytes] | None = None) -> None:
    files = {
        "careerdesk/__init__.py": b"",
        "careerdesk/bootstrap/console.py": b"def configure_console_streams(): pass\n",
        "careerdesk/bootstrap/desktop.py": b"def main(): return 0\n",
        "careerdesk/bootstrap/cli.py": b"def main(): return 0\n",
        "careerdesk/platform/database/backup.py": b"",
        "careerdesk/default.env": b"APP_RUNTIME_MODE=desktop\n",
        "careerdesk/frontend_dist/index.html": b"<html></html>",
        "careerdesk/frontend_dist/assets/index-123.js": b"export {};",
        "careerdesk/frontend_dist/legal/node/index.json": b'{"schema_version":1,"packages":[]}',
        "careerdesk-0.1.0.dist-info/METADATA": (
            b"Metadata-Version: 2.4\nName: careerdesk\nVersion: 0.1.0\n"
        ),
        "careerdesk-0.1.0.dist-info/WHEEL": b"Wheel-Version: 1.0\n",
        **_skill_files(),
    }
    files.update(extra or {})
    with zipfile.ZipFile(path, "w") as archive:
        for name, content in files.items():
            archive.writestr(name, content)


def _write_runtime_resources(root: Path, *, platform_name: str = "darwin") -> None:
    for relative in (
        "magika/config/content_types_kb.min.json",
        "magika/models/standard_v3_3/model.onnx",
        "magika/models/standard_v3_3/config.min.json",
        "magika/models/standard_v3_3/metadata.json",
        f"sqlite_vec/{'vec0.dylib' if platform_name == 'darwin' else 'vec0.dll'}",
    ):
        destination = root / relative
        destination.parent.mkdir(parents=True, exist_ok=True)
        destination.write_bytes(b"runtime-resource")


def test_stage_wheel_requires_installed_resources_and_preserves_identity(tmp_path):
    wheel = tmp_path / "careerdesk-0.1.0-py3-none-any.whl"
    _write_test_wheel(wheel)

    identity = package_desktop.stage_wheel(wheel, tmp_path / "site")

    assert identity == package_desktop.WheelIdentity(name="careerdesk", version="0.1.0")
    assert (tmp_path / "site/careerdesk/default.env").read_text() == "APP_RUNTIME_MODE=desktop\n"
    assert (tmp_path / "site/careerdesk/frontend_dist/assets/index-123.js").is_file()
    for relative in package_desktop._REQUIRED_PACKAGE_FILES:
        assert (tmp_path / f"site/{relative}").is_file()
    for name in package_desktop._REQUIRED_SKILL_NAMES:
        for filename in ("SKILL.md", "SKILL.en.md"):
            assert (
                tmp_path / f"site/careerdesk/agentic/skills/{name}/{filename}"
            ).is_file()


def test_stage_wheel_rejects_zip_slip_without_writing_outside_site(tmp_path):
    wheel = tmp_path / "careerdesk-0.1.0-py3-none-any.whl"
    _write_test_wheel(wheel, extra={"../escaped.txt": b"bad"})

    with pytest.raises(ValueError, match="不安全"):
        package_desktop.stage_wheel(wheel, tmp_path / "site")

    assert not (tmp_path / "escaped.txt").exists()


@pytest.mark.parametrize(
    "required",
    [
        "careerdesk/bootstrap/console.py",
        "careerdesk/bootstrap/desktop.py",
        "careerdesk/bootstrap/cli.py",
        "careerdesk/platform/database/backup.py",
    ],
)
def test_stage_wheel_rejects_missing_runtime_entrypoint_or_recovery_module(
    tmp_path,
    required,
):
    wheel = tmp_path / "careerdesk-0.1.0-py3-none-any.whl"
    _write_test_wheel(wheel)
    rewritten = tmp_path / "careerdesk-0.1.0-rewritten-py3-none-any.whl"
    with zipfile.ZipFile(wheel) as source, zipfile.ZipFile(rewritten, "w") as target:
        for member in source.infolist():
            if member.filename != required:
                target.writestr(member, source.read(member))

    with pytest.raises(ValueError, match=required):
        package_desktop.stage_wheel(rewritten, tmp_path / "site")


@pytest.mark.parametrize("unsafe_name", ["C:/escaped.txt", "folder/../escaped.txt", "folder\\escaped.txt"])
def test_stage_wheel_rejects_cross_platform_unsafe_paths(tmp_path, unsafe_name):
    wheel = tmp_path / "careerdesk-0.1.0-py3-none-any.whl"
    _write_test_wheel(wheel, extra={unsafe_name: b"bad"})

    with pytest.raises(ValueError, match="不安全"):
        package_desktop.stage_wheel(wheel, tmp_path / "site")


def test_build_refuses_existing_output_before_unpacking_or_subprocess(tmp_path):
    output = tmp_path / "existing"
    output.mkdir()

    with pytest.raises(ValueError, match="尚不存在"):
        package_desktop.build(
            wheel=tmp_path / "missing.whl",
            output_dir=output,
            platform_name="darwin",
        )


def test_build_environment_strips_credentials_and_points_discovery_at_wheel(tmp_path, monkeypatch):
    monkeypatch.setenv("OPENAI_API_KEY", "must-not-reach-builder")
    monkeypatch.setenv("APP_GATEWAY_AUTH_SECRET", "must-not-reach-builder")
    monkeypatch.setenv("APP_DATA_DIR", "/must/not/reach/builder")
    monkeypatch.setenv("GOOGLE_APPLICATION_CREDENTIALS", "/must/not/reach/builder.json")
    monkeypatch.setenv("PATH", "/safe/path")

    environment = package_desktop._build_environment(
        site=tmp_path / "site",
        version="0.1.0",
        windows_version_file=None,
        legal_dir=tmp_path / "legal",
    )

    assert environment["PATH"] == "/safe/path"
    assert environment["PYTHONPATH"] == str(tmp_path / "site")
    assert environment["CAREERDESK_BUILD_VERSION"] == "0.1.0"
    assert environment["CAREERDESK_LEGAL_DIR"] == str(tmp_path / "legal")
    assert "OPENAI_API_KEY" not in environment
    assert "APP_GATEWAY_AUTH_SECRET" not in environment
    assert "APP_DATA_DIR" not in environment
    assert "GOOGLE_APPLICATION_CREDENTIALS" not in environment
    assert "CAREERDESK_WINDOWS_VERSION_FILE" not in environment


def test_packaging_cli_reconfigures_redirected_windows_output(monkeypatch):
    calls = []

    class Stream:
        def reconfigure(self, **kwargs):
            calls.append(kwargs)

    monkeypatch.setattr(package_desktop.sys, "stdout", Stream())
    monkeypatch.setattr(package_desktop.sys, "stderr", Stream())

    package_desktop._configure_console_output()

    assert calls == [
        {"encoding": "utf-8", "errors": "backslashreplace"},
        {"encoding": "utf-8", "errors": "backslashreplace"},
    ]


def test_windows_version_resource_is_numeric_and_product_scoped(tmp_path):
    version_file = tmp_path / "windows-version.txt"

    package_desktop._windows_version_file("2.7.3", version_file)

    content = version_file.read_text(encoding="utf-8")
    assert "filevers=(2, 7, 3, 0)" in content
    assert "ProductName', 'CareerDesk'" in content
    assert "OriginalFilename', 'CareerDesk.exe'" in content


@pytest.mark.parametrize(
    ("codesign_identity", "expected_signing"),
    [(None, "ad-hoc"), ("CareerDesk Local Signing", "local-certificate")],
)
def test_build_manifest_contract_has_no_signing_claim(
    tmp_path,
    monkeypatch,
    codesign_identity,
    expected_signing,
):
    wheel = tmp_path / "careerdesk-0.1.0-py3-none-any.whl"
    _write_test_wheel(wheel)
    output = tmp_path / "new-output"

    calls = []

    def fake_run(command, **kwargs):
        calls.append((command, kwargs))
        if command[0] == "codesign":
            assert command == [
                "codesign",
                "--force",
                "--deep",
                "--timestamp=none",
                "--sign",
                codesign_identity,
                str(output / "dist/CareerDesk.app"),
            ]
            assert kwargs["check"] is True
            return
        if kwargs.get("env", {}).get("CAREERDESK_PACKAGE_SELF_TEST") == "1":
            assert command == [str(output / "dist/CareerDesk.app/Contents/MacOS/CareerDeskData")]
            assert kwargs["cwd"] == output / "dist/CareerDesk.app"
            assert kwargs["timeout"] == 60
            return
        assert "--noconfirm" not in command and "--clean" not in command
        assert kwargs["check"] is True
        assert "CAREERDESK_CODESIGN_IDENTITY" not in kwargs["env"]
        artifact = output / "dist/CareerDesk.app"
        executable = artifact / "Contents/MacOS/CareerDesk"
        executable.parent.mkdir(parents=True)
        executable.write_bytes(b"executable")
        (executable.parent / "CareerDeskData").write_bytes(b"data-executable")
        resource_root = artifact / "Contents/Resources/careerdesk"
        (resource_root / "frontend_dist").mkdir(parents=True)
        (resource_root / "default.env").write_text("", encoding="utf-8")
        (resource_root / "frontend_dist/index.html").write_text("", encoding="utf-8")
        (resource_root / "frontend_dist/legal/node").mkdir(parents=True)
        (resource_root / "frontend_dist/legal/node/index.json").write_text(
            "{}", encoding="utf-8"
        )
        for name in package_desktop._REQUIRED_SKILL_NAMES:
            for filename in ("SKILL.md", "SKILL.en.md"):
                skill = resource_root / f"agentic/skills/{name}/{filename}"
                skill.parent.mkdir(parents=True, exist_ok=True)
                skill.write_text("", encoding="utf-8")
        legal = artifact / "Contents/Resources/Legal"
        (legal / "CareerDesk").mkdir(parents=True)
        (legal / "CareerDesk/LICENSE").write_text("MIT", encoding="utf-8")
        (legal / "ThirdParty/Python").mkdir(parents=True)
        (legal / "ThirdParty/Python/index.json").write_text("{}", encoding="utf-8")
        _write_runtime_resources(artifact / "Contents/Frameworks")

    monkeypatch.setattr(package_desktop.subprocess, "run", fake_run)
    monkeypatch.setattr(package_desktop.platform, "machine", lambda: "arm64")
    monkeypatch.setattr(package_desktop.platform, "python_version", lambda: "3.12.12")

    artifact = package_desktop.build(
        wheel=wheel,
        output_dir=output,
        platform_name="darwin",
        codesign_identity=codesign_identity,
    )

    manifest = json.loads((output / "build-manifest.json").read_text())
    assert artifact == output / "dist/CareerDesk.app"
    assert manifest["format"] == "pyinstaller-onedir-windowed"
    assert manifest["code_signing"] == expected_signing
    assert len(manifest["wheel_sha256"]) == 64
    assert len(manifest["executable_sha256"]) == 64
    assert manifest["data_executable"].endswith("/CareerDeskData")
    assert len(manifest["data_executable_sha256"]) == 64
    assert manifest["legal_notices"] is True
    assert len(manifest["python_notice_index_sha256"]) == 64
    assert len(manifest["node_notice_index_sha256"]) == 64
    assert len(calls) == (3 if codesign_identity else 2)


@pytest.mark.parametrize("codesign_identity", ["   ", "CareerDesk Local Signing"])
def test_build_rejects_codesign_identity_outside_macos_or_blank(tmp_path, codesign_identity):
    platform_name = "win32" if codesign_identity.strip() else "darwin"

    with pytest.raises(ValueError, match="codesign-identity"):
        package_desktop.build(
            wheel=tmp_path / "missing.whl",
            output_dir=tmp_path / "new-output",
            platform_name=platform_name,
            codesign_identity=codesign_identity,
        )


def test_resource_verification_treats_macos_framework_symlink_as_one_copy(tmp_path):
    artifact = tmp_path / "CareerDesk.app"
    resources = artifact / "Contents/Resources"
    package = resources / "careerdesk"
    (package / "frontend_dist").mkdir(parents=True)
    (package / "default.env").write_text("", encoding="utf-8")
    (package / "frontend_dist/index.html").write_text("", encoding="utf-8")
    for name in package_desktop._REQUIRED_SKILL_NAMES:
        for filename in ("SKILL.md", "SKILL.en.md"):
            skill = package / f"agentic/skills/{name}/{filename}"
            skill.parent.mkdir(parents=True, exist_ok=True)
            skill.write_text("", encoding="utf-8")
    legal = resources / "Legal"
    (legal / "CareerDesk").mkdir(parents=True)
    (legal / "CareerDesk/LICENSE").write_text("MIT", encoding="utf-8")
    (legal / "ThirdParty/Python").mkdir(parents=True)
    (legal / "ThirdParty/Python/index.json").write_text("{}", encoding="utf-8")
    (package / "frontend_dist/legal/node").mkdir(parents=True)
    (package / "frontend_dist/legal/node/index.json").write_text("{}", encoding="utf-8")
    _write_runtime_resources(resources)
    frameworks = artifact / "Contents/Frameworks"
    frameworks.mkdir()
    (frameworks / "careerdesk").symlink_to(package, target_is_directory=True)

    package_desktop._verify_bundled_resources(artifact, "darwin")


def test_spec_uses_installed_wheel_resources_and_platform_native_artifacts():
    source = package_desktop.SPEC.read_text(encoding="utf-8")

    assert "ROOT = Path(SPECPATH).parent\n" in source
    assert 'SITE = Path(os.environ["CAREERDESK_FROZEN_SITE"])' in source
    assert 'name="CareerDesk.app"' in source
    assert 'bundle_identifier="com.careerdesk.desktop"' in source
    assert 'name="CareerDesk"' in source
    assert 'name="CareerDeskData"' in source
    assert "console=False" in source
    assert "console=True" in source
    assert "COLLECT(" in source
    assert 'LEGAL = Path(os.environ["CAREERDESK_LEGAL_DIR"])' in source
    assert '(str(LEGAL), "Legal")' in source
    assert 'collect_data_files("magika")' in source
    assert 'collect_data_files("sqlite_vec")' in source
    assert source.count("codesign_identity=None") == 3
    assert "--onefile" not in source
    assert "SOURCE_LAYOUT" not in source
