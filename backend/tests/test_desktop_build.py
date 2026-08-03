
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO_ROOT))

from desktop import build_desktop  # noqa: E402  import after explicit cross-project path setup


def test_non_macos_still_builds_windows_icon(monkeypatch, tmp_path):
    events: list[str] = []
    logo = tmp_path / "logo.png"
    logo.write_bytes(b"present")
    ico = tmp_path / "careerdesk.ico"

    monkeypatch.setattr(build_desktop, "LOGO", logo)
    monkeypatch.setattr(build_desktop, "build_ico", lambda: events.append("ico") or ico)
    monkeypatch.setattr(build_desktop, "build_icns", lambda: events.append("icns") or None)
    monkeypatch.setattr(build_desktop, "build_app", lambda icon: events.append(f"app:{icon}") or None)
    monkeypatch.setattr(build_desktop, "ROOT", Path(tmp_path))

    build_desktop.main()

    assert events == ["ico", "icns", "app:None"]


def test_build_icns_gracefully_skips_without_iconutil(monkeypatch):
    monkeypatch.setattr(build_desktop.shutil, "which", lambda command: None)

    assert build_desktop.build_icns() is None
