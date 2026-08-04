
import logging
import os
import stat
import sys
import threading
import time
from pathlib import Path
from types import SimpleNamespace

REPO_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO_ROOT))

from careerdesk.bootstrap import desktop as desktop_launcher  # noqa: E402  import after explicit cross-project path setup
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


class _ScriptedUvicornServer:
    """Uvicorn stand-in whose exit behaviour is fully scripted per test."""

    def __init__(self, *, honors_graceful: bool, honors_force: bool):
        self.should_exit = False
        self.force_exit = False
        self.started = True
        self.released_by_test = threading.Event()
        self._honors_graceful = honors_graceful
        self._honors_force = honors_force

    def run(self):
        while True:
            if self._honors_graceful and self.should_exit:
                return
            if self._honors_force and self.force_exit:
                return
            if self.released_by_test.is_set():
                return
            time.sleep(0.005)


def test_window_close_stops_a_healthy_server_gracefully(monkeypatch):
    monkeypatch.setattr(desktop_launcher, "_GRACEFUL_CLOSE_SECONDS", 5.0)
    server = _ScriptedUvicornServer(honors_graceful=True, honors_force=True)
    runtime = desktop_launcher.ServerRuntime(server)
    runtime.start()

    assert desktop_launcher._stop_server_for_exit(runtime) is True
    assert runtime.wait_for_exit(timeout=2.0)
    assert server.force_exit is False


def test_window_close_escalates_when_graceful_shutdown_hangs(monkeypatch):
    monkeypatch.setattr(desktop_launcher, "_GRACEFUL_CLOSE_SECONDS", 0.05)
    monkeypatch.setattr(desktop_launcher, "_FORCED_CLOSE_SECONDS", 5.0)
    server = _ScriptedUvicornServer(honors_graceful=False, honors_force=True)
    runtime = desktop_launcher.ServerRuntime(server)
    runtime.start()

    assert desktop_launcher._stop_server_for_exit(runtime) is True
    assert server.force_exit is True
    assert runtime.wait_for_exit(timeout=2.0)


def test_window_close_reports_a_wedged_server_instead_of_waiting_forever(monkeypatch):
    """The launcher must learn about a truly stuck backend so it can end the process."""
    monkeypatch.setattr(desktop_launcher, "_GRACEFUL_CLOSE_SECONDS", 0.05)
    monkeypatch.setattr(desktop_launcher, "_FORCED_CLOSE_SECONDS", 0.05)
    server = _ScriptedUvicornServer(honors_graceful=False, honors_force=False)
    runtime = desktop_launcher.ServerRuntime(server)
    runtime.start()
    try:
        assert desktop_launcher._stop_server_for_exit(runtime) is False
    finally:
        server.released_by_test.set()
        assert runtime.wait_for_exit(timeout=2.0)


def test_file_logging_captures_warnings_in_the_private_log_directory(tmp_path, monkeypatch):
    monkeypatch.setattr(desktop_launcher, "_FILE_LOGGING_CONFIGURED", False)
    log_dir = tmp_path / "logs"
    stub_settings = SimpleNamespace(log_dir=str(log_dir))
    root = logging.getLogger()
    before = list(root.handlers)

    desktop_launcher._configure_file_logging(stub_settings)
    try:
        desktop_launcher._configure_file_logging(stub_settings)
        added = [handler for handler in root.handlers if handler not in before]
        assert len(added) == 1

        logging.getLogger("careerdesk.launcher-test").warning("marker-%s", "entry")
        added[0].flush()
        content = (log_dir / "careerdesk.log").read_text(encoding="utf-8")
        assert "marker-entry" in content
        assert "file logging active" in content
        if os.name == "posix":
            assert stat.S_IMODE(log_dir.stat().st_mode) == 0o700
            assert stat.S_IMODE((log_dir / "careerdesk.log").stat().st_mode) == 0o600
    finally:
        for handler in [handler for handler in root.handlers if handler not in before]:
            root.removeHandler(handler)
            handler.close()


def test_uvicorn_records_are_bridged_into_the_launcher_log(monkeypatch):
    handler = logging.NullHandler()
    monkeypatch.setattr(desktop_launcher, "_FILE_LOG_HANDLER", handler)
    targets = [logging.getLogger(name) for name in ("uvicorn", "uvicorn.error")]
    try:
        desktop_launcher._attach_uvicorn_file_logging()
        desktop_launcher._attach_uvicorn_file_logging()
        for target in targets:
            assert target.handlers.count(handler) == 1
    finally:
        for target in targets:
            while handler in target.handlers:
                target.removeHandler(handler)


def test_data_tool_double_click_holds_the_console_open(monkeypatch, capsys):
    from careerdesk.bootstrap import cli as data_cli

    monkeypatch.setattr("builtins.input", lambda *_args: "")

    assert data_cli._held_open_for_double_click([], windows=True, frozen=True) is True
    output = capsys.readouterr().out
    assert "备份/恢复" in output and "backup/restore" in output

    assert data_cli._held_open_for_double_click(["verify"], windows=True, frozen=True) is False
    assert data_cli._held_open_for_double_click([], windows=False, frozen=True) is False
    assert data_cli._held_open_for_double_click([], windows=True, frozen=False) is False


def test_window_smoke_reports_native_stack_health(monkeypatch, capsys):
    class _FakeWindow:
        def destroy(self):
            return None

    class _FakeWebview:
        def __init__(self):
            self.started = False

        def create_window(self, *_args, **_kwargs):
            return _FakeWindow()

        def start(self, *_args, **_kwargs):
            self.started = True

    fake = _FakeWebview()
    monkeypatch.setitem(sys.modules, "webview", fake)

    assert desktop_launcher._run_window_smoke() == 0
    assert fake.started is True
    assert "WINDOW_SMOKE_OK" in capsys.readouterr().out


def test_window_smoke_reports_failure_reason_without_crashing(monkeypatch, capsys):
    monkeypatch.setitem(sys.modules, "webview", None)

    assert desktop_launcher._run_window_smoke() == 0
    output = capsys.readouterr().out
    assert "WINDOW_SMOKE_FAILED" in output
    assert "ModuleNotFoundError" in output


def test_unblocking_assemblies_is_a_noop_outside_frozen_windows():
    assert desktop_launcher._unblock_dotnet_assemblies() == 0
