
import asyncio
import os
import stat
import threading
import time
from pathlib import Path
import tomllib
from types import SimpleNamespace

import httpx
import pytest
import uvicorn

from careerdesk.bootstrap import desktop as launcher


REPOSITORY_ROOT = Path(__file__).resolve().parents[2]


@pytest.fixture(autouse=True)
def _stable_startup_locale(monkeypatch):
    """Keep legacy launcher assertions deterministic across developer machines."""
    monkeypatch.setattr(launcher, "_startup_locale", lambda: "zh-CN")


def _mode(path: Path) -> int:
    return stat.S_IMODE(path.stat().st_mode)


def test_pre_ui_copy_uses_the_system_locale(monkeypatch):
    monkeypatch.setattr(launcher, "_startup_locale", lambda: "en")
    assert launcher._startup_text("中文", "English") == "English"
    monkeypatch.setattr(launcher, "_startup_locale", lambda: "zh-CN")
    assert launcher._startup_text("中文", "English") == "中文"


class _RecordingLock:
    def __init__(self):
        self.releases = 0

    def release(self):
        self.releases += 1


def test_ensure_env_is_born_private_and_existing_file_is_only_tightened(tmp_path, monkeypatch):
    example = tmp_path / ".env.example"
    example.write_text("APP_TIMEZONE=Asia/Shanghai\n", encoding="utf-8")
    monkeypatch.setattr(launcher, "ENV_FILE", tmp_path / ".env")
    monkeypatch.setattr(launcher, "DEFAULT_ENV_TEMPLATE", example)
    monkeypatch.setattr(launcher, "SOURCE_LAYOUT", True)

    launcher.ensure_env()
    env_file = tmp_path / ".env"

    assert env_file.read_text(encoding="utf-8") == example.read_text(encoding="utf-8")
    if os.name == "posix":
        assert _mode(env_file) == 0o600
        os.chmod(env_file, 0o644)
        launcher.ensure_env()
        assert _mode(env_file) == 0o600


def test_ensure_env_rejects_redirected_secret_file(tmp_path, monkeypatch):
    monkeypatch.setattr(launcher, "ENV_FILE", tmp_path / ".env")
    monkeypatch.setattr(launcher, "SOURCE_LAYOUT", True)
    target = tmp_path / "outside"
    target.write_text("KEEP", encoding="utf-8")
    env_file = tmp_path / ".env"
    try:
        env_file.symlink_to(target)
    except OSError as error:
        pytest.skip(f"symlinks unavailable: {error}")

    with pytest.raises(RuntimeError, match="符号链"):
        launcher.ensure_env()

    assert env_file.is_symlink() and target.read_text(encoding="utf-8") == "KEEP"


def test_installed_env_uses_private_config_root_and_bundled_template(tmp_path, monkeypatch):
    config_dir = tmp_path / "config"
    env_file = config_dir / "settings.env"
    template = tmp_path / "default.env"
    template.write_text("APP_TIMEZONE=Asia/Shanghai\n", encoding="utf-8")
    monkeypatch.setattr(launcher, "SOURCE_LAYOUT", False)
    monkeypatch.setattr(launcher, "DEFAULT_CONFIG_DIR", config_dir)
    monkeypatch.setattr(launcher, "ENV_FILE", env_file)
    monkeypatch.setattr(launcher, "DEFAULT_ENV_TEMPLATE", template)

    launcher.ensure_env()

    assert env_file.read_bytes() == template.read_bytes()
    if os.name == "posix":
        assert _mode(config_dir) == 0o700
        assert _mode(env_file) == 0o600


def test_source_launcher_never_loads_os_credential_store(monkeypatch):
    from careerdesk.platform import credentials

    monkeypatch.setattr(launcher, "SOURCE_LAYOUT", True)
    monkeypatch.setattr(
        credentials,
        "inject_system_credentials",
        lambda **_kwargs: pytest.fail("source checkout must keep its .env workflow"),
    )

    launcher._load_installed_credentials()


def test_installed_launcher_discloses_unavailable_store_without_exposing_values(
    tmp_path,
    monkeypatch,
    capsys,
):
    from careerdesk.platform import credentials

    config_file = tmp_path / "settings.env"
    calls = []
    monkeypatch.setattr(launcher, "SOURCE_LAYOUT", False)
    monkeypatch.setattr(launcher, "ENV_FILE", config_file)
    monkeypatch.setenv("OPENAI_API_KEY", "do-not-print-a-secret")
    monkeypatch.setattr(
        credentials,
        "inject_system_credentials",
        lambda **kwargs: calls.append(kwargs) or credentials.CredentialStoreStatus(
            kind="system",
            available=False,
            label="系统凭据存储",
            issue="系统凭据存储不可用；请解锁后重启。",
        ),
    )

    launcher._load_installed_credentials()

    output = capsys.readouterr().out
    assert calls == [{"config_file": config_file}]
    assert "请解锁后重启" in output
    assert "本地模型" in output
    assert "do-not-print-a-secret" not in output


def test_frozen_smoke_headless_mode_never_opens_a_browser(monkeypatch):
    monkeypatch.setenv("CAREERDESK_HEADLESS", "1")
    monkeypatch.setattr(
        launcher.webbrowser,
        "open",
        lambda _url: pytest.fail("headless artifact smoke must not start a browser"),
    )

    assert launcher._headless_mode()
    assert not launcher._open_browser_unless_headless()


def test_desktop_bridge_selects_only_a_new_data_directory():
    class FakeWindow:
        def __init__(self):
            self.calls = []

        def create_file_dialog(self, dialog_type, *, allow_multiple):
            self.calls.append((dialog_type, allow_multiple))
            return ["/chosen"]

    webview = SimpleNamespace(FileDialog=SimpleNamespace(FOLDER=20))
    window = FakeWindow()
    bridge = launcher.DesktopBridge(webview)
    assert bridge.select_data_directory() is None

    bridge.bind(window)

    assert bridge.select_data_directory() == "/chosen/CareerDesk Data"
    assert window.calls == [(20, False)]


def test_desktop_bridge_downloads_and_opens_only_the_latest_job_template(
    tmp_path,
    monkeypatch,
):
    dist = tmp_path / "dist"
    dist.mkdir()
    source = dist / "careerdesk-job-import-example-zh-CN.xlsx"
    source.write_bytes(b"workbook")
    monkeypatch.setattr(launcher, "DEFAULT_FRONTEND_DIST_DIR", dist)
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("OPENAI_API_KEY", "must-not-reach-open")
    monkeypatch.setattr(launcher.sys, "platform", "darwin")
    calls = []
    monkeypatch.setattr(
        launcher.subprocess,
        "run",
        lambda command, check, env: calls.append((command, check, env)),
    )
    bridge = launcher.DesktopBridge(SimpleNamespace())

    first = bridge.download_job_import_template()
    second = bridge.download_job_import_template()

    assert Path(first).name == "CareerDesk 岗位导入模板.xlsx"
    assert Path(second).name == "CareerDesk 岗位导入模板 (2).xlsx"
    assert Path(first).read_bytes() == b"workbook"
    assert Path(second).read_bytes() == b"workbook"
    with pytest.raises(RuntimeError, match="只能打开本次由 CareerDesk 下载的表格模板"):
        bridge.open_job_import_template(first)
    assert bridge.open_job_import_template(second)
    assert calls[0][0:2] == (["/usr/bin/open", second], True)
    assert "OPENAI_API_KEY" not in calls[0][2]

    (dist / "careerdesk-job-import-example-en.xlsx").write_bytes(b"english workbook")
    english = bridge.download_job_import_template("en")
    assert Path(english).name == "CareerDesk Role Import Template.xlsx"
    assert Path(english).read_bytes() == b"english workbook"


def test_desktop_main_window_enables_document_selection_and_copy():
    calls = []

    class FakeEvent:
        def __init__(self):
            self.handlers = []

        def __iadd__(self, handler):
            self.handlers.append(handler)
            return self

    fake_window = SimpleNamespace(
        events=SimpleNamespace(loaded=FakeEvent()),
    )

    class FakeWebview:
        FileDialog = SimpleNamespace(FOLDER=20)

        @staticmethod
        def create_window(*args, **kwargs):
            calls.append((args, kwargs))
            return fake_window

    window, bridge = launcher._create_main_window(FakeWebview)

    assert window is fake_window
    assert bridge._window is fake_window
    assert fake_window.events.loaded.handlers == [
        launcher._schedule_macos_scroll_surface_configuration,
    ]
    assert calls == [(('CareerDesk', launcher.URL), {
        'width': 1200,
        'height': 820,
        'js_api': bridge,
        'text_select': True,
    })]


def test_native_scroll_configuration_disables_only_webview_rubber_banding():
    calls = []
    content_view = SimpleNamespace(
        _setRubberBandingEnabled_=lambda enabled: calls.append(enabled),
    )
    window = SimpleNamespace(
        native=SimpleNamespace(contentView=lambda: content_view),
    )

    assert launcher._disable_native_scroll_rubber_banding(window)
    assert calls == [False]
    assert not launcher._disable_native_scroll_rubber_banding(
        SimpleNamespace(native=SimpleNamespace(contentView=lambda: object())),
    )

    def unavailable_setter(_enabled):
        raise RuntimeError("selector unavailable")

    unavailable_window = SimpleNamespace(
        native=SimpleNamespace(
            contentView=lambda: SimpleNamespace(
                _setRubberBandingEnabled_=unavailable_setter,
            ),
        ),
    )
    assert not launcher._disable_native_scroll_rubber_banding(unavailable_window)


def test_server_readiness_returns_only_after_started_and_thread_can_stop():
    class FakeServer:
        started = False
        should_exit = False

        def run(self):
            self.started = True
            while not self.should_exit:
                time.sleep(0.001)

    server = FakeServer()
    runtime = launcher.ServerRuntime(server)
    launcher._start_server_and_wait(runtime, timeout=0.2)

    assert server.started is True and runtime.is_alive
    assert launcher._request_server_shutdown(runtime, timeout=1)
    assert not runtime.is_alive


def test_server_failure_before_readiness_never_returns_a_window_ready_thread():
    class FailedServer:
        started = False
        should_exit = False

        @staticmethod
        def run():
            raise RuntimeError("startup exploded")

    runtime = launcher.ServerRuntime(FailedServer())
    with pytest.raises(RuntimeError, match="startup exploded"):
        launcher._start_server_and_wait(runtime, timeout=0.2)

    assert runtime.thread is not None
    assert runtime.wait_for_exit(timeout=1)


def test_thread_start_exception_stays_fail_closed_when_target_never_reports_exit(monkeypatch):
    class NeverStartedServer:
        started = False
        should_exit = False

        @staticmethod
        def run():
            pytest.fail("Thread.start 失败时不应执行 server.run")

    runtime = launcher.ServerRuntime(NeverStartedServer())
    monkeypatch.setattr(
        threading.Thread,
        "start",
        lambda _thread: (_ for _ in ()).throw(RuntimeError("thread unavailable")),
    )

    with pytest.raises(RuntimeError, match="thread unavailable"):
        launcher._start_server_and_wait(runtime, timeout=0.02)

    assert runtime.thread is not None
    assert not runtime.start_returned
    assert not runtime.wait_for_exit(timeout=0.01)
    lock = _RecordingLock()
    assert not launcher._release_instance_lock_if_server_stopped(
        lock,
        runtime,
    )
    assert lock.releases == 0


def test_thread_start_can_raise_after_real_target_started_and_lock_stays_held(monkeypatch):
    release = threading.Event()
    target_started = threading.Event()

    class ActuallyStartedServer:
        started = False
        should_exit = False

        @staticmethod
        def run():
            target_started.set()
            release.wait()

    original_start = threading.Thread.start

    def start_then_raise(thread):
        original_start(thread)
        assert target_started.wait(timeout=1)
        raise RuntimeError("raised after native start")

    monkeypatch.setattr(threading.Thread, "start", start_then_raise)
    runtime = launcher.ServerRuntime(ActuallyStartedServer())
    lock = _RecordingLock()
    try:
        with pytest.raises(RuntimeError, match="after native start"):
            launcher._start_server_and_wait(runtime, timeout=0.02)

        assert target_started.is_set()
        assert not runtime.start_returned
        assert not runtime.wait_for_exit(timeout=0.01)
        assert not launcher._release_instance_lock_if_server_stopped(lock, runtime)
        assert lock.releases == 0
    finally:
        release.set()
        assert runtime.wait_for_exit(timeout=1)

    assert launcher._release_instance_lock_if_server_stopped(lock, runtime)
    assert lock.releases == 1


def test_interrupted_exit_wait_does_not_change_lock_ownership(monkeypatch):
    release = threading.Event()

    class RunningServer:
        started = False
        should_exit = False

        def run(self):
            self.started = True
            release.wait()

    runtime = launcher.ServerRuntime(RunningServer())
    launcher._start_server_and_wait(runtime, timeout=0.2)
    lock = _RecordingLock()
    original_wait = runtime._thread_exited.wait

    def interrupted_wait(timeout=None):
        del timeout
        raise KeyboardInterrupt

    monkeypatch.setattr(runtime._thread_exited, "wait", interrupted_wait)
    try:
        with pytest.raises(KeyboardInterrupt):
            launcher._request_server_shutdown(runtime, timeout=0.01)

        assert runtime.is_alive
        assert not launcher._release_instance_lock_if_server_stopped(lock, runtime)
        assert lock.releases == 0
    finally:
        monkeypatch.setattr(runtime._thread_exited, "wait", original_wait)
        release.set()
        assert runtime.wait_for_exit(timeout=1)

    assert launcher._release_instance_lock_if_server_stopped(lock, runtime)
    assert lock.releases == 1


def test_server_readiness_timeout_requests_shutdown():
    class HungServer:
        started = False
        should_exit = False

        def run(self):
            while not self.should_exit:
                time.sleep(0.001)

    server = HungServer()
    runtime = launcher.ServerRuntime(server)
    with pytest.raises(RuntimeError, match="未完成启动"):
        launcher._start_server_and_wait(runtime, timeout=0.02)
    assert server.should_exit is True
    assert runtime.thread is not None
    assert runtime.wait_for_exit(timeout=1)


def test_server_readiness_timeout_retains_a_still_running_thread():
    release = threading.Event()

    class ShutdownIgnoringServer:
        started = False
        should_exit = False

        @staticmethod
        def run():
            release.wait()

    runtime = launcher.ServerRuntime(ShutdownIgnoringServer())
    try:
        with pytest.raises(RuntimeError, match="未完成启动"):
            launcher._start_server_and_wait(runtime, timeout=0.02)

        assert runtime.thread is not None
        assert runtime.server.should_exit is True
        assert runtime.is_alive
    finally:
        release.set()
        assert runtime.wait_for_exit(timeout=1)


def test_real_uvicorn_shutdown_keeps_data_lock_until_active_to_thread_finishes(
    tmp_path,
    monkeypatch,
):
    from careerdesk.bootstrap.app import create_app
    from careerdesk.core.config import get_settings
    from careerdesk.platform.runtime import (
        InstanceAlreadyRunningError,
        acquire_instance_lock,
    )

    data_dir = tmp_path / "data"
    monkeypatch.setenv("APP_DATA_DIR", str(data_dir))
    monkeypatch.setenv("APP_RUNTIME_MODE", "test")
    monkeypatch.setenv("APP_LLM_MODEL", "")
    get_settings.cache_clear()

    release_tool = threading.Event()
    tool_started = threading.Event()
    request_errors: list[BaseException] = []
    request_thread: threading.Thread | None = None
    instance_lock = acquire_instance_lock(data_dir, entrypoint="launcher-test")
    app = create_app(instance_lock=instance_lock)

    async def slow_tool() -> dict[str, bool]:
        def synchronous_tool() -> None:
            tool_started.set()
            release_tool.wait()

        await asyncio.to_thread(synchronous_tool)
        return {"ok": True}

    app.add_api_route("/__launcher_test__/slow-tool", slow_tool, methods=["GET"])
    # frontend/dist may install a final SPA catch-all, so make the test-only route exact-first.
    app.router.routes.insert(0, app.router.routes.pop())
    server = uvicorn.Server(uvicorn.Config(
        app,
        host="127.0.0.1",
        port=0,
        lifespan="on",
        log_level="error",
        timeout_graceful_shutdown=None,
    ))
    runtime = launcher.ServerRuntime(server)

    try:
        launcher._start_server_and_wait(runtime, timeout=2)
        assert runtime.is_alive and server.started
        assert server.config.timeout_graceful_shutdown is None
        port = server.servers[0].sockets[0].getsockname()[1]
        base_url = f"http://127.0.0.1:{port}"
        health = httpx.get(f"{base_url}/healthz", timeout=1)
        assert health.status_code == 200 and health.text == "ok"

        def request_slow_tool() -> None:
            try:
                response = httpx.get(
                    f"{base_url}/__launcher_test__/slow-tool",
                    timeout=2,
                )
                response.raise_for_status()
            except BaseException as error:  # noqa: BLE001 -- surface thread failure below
                request_errors.append(error)

        request_thread = threading.Thread(target=request_slow_tool)
        request_thread.start()
        assert tool_started.wait(timeout=1)

        # This injected tiny window exercises cleanup after the launch deadline.
        assert not launcher._request_server_shutdown(runtime, timeout=0.02)
        assert runtime.is_alive and not instance_lock.released
        assert not launcher._release_instance_lock_if_server_stopped(instance_lock, runtime)
        with pytest.raises(InstanceAlreadyRunningError):
            acquire_instance_lock(data_dir, entrypoint="contender")

        release_tool.set()
        request_thread.join(timeout=1)
        assert not request_thread.is_alive()
        assert launcher._request_server_shutdown(runtime, timeout=2)
        assert not request_errors
        assert launcher._release_instance_lock_if_server_stopped(instance_lock, runtime)

        with acquire_instance_lock(data_dir, entrypoint="after-shutdown"):
            pass
    finally:
        release_tool.set()
        if request_thread is not None:
            request_thread.join(timeout=1)
        runtime.request_shutdown()
        runtime.wait_for_exit(timeout=2)
        instance_lock.release()
        get_settings.cache_clear()


def test_frontend_build_uses_lockfile_and_does_not_inherit_application_secrets(
    tmp_path, monkeypatch,
):
    frontend = tmp_path / "frontend"
    frontend.mkdir()
    (frontend / "package-lock.json").write_text("{}", encoding="utf-8")
    calls: list[tuple[list[str], dict[str, str]]] = []

    monkeypatch.setattr(launcher, "SOURCE_LAYOUT", True)
    monkeypatch.setattr(launcher, "FRONTEND_SOURCE_DIR", frontend)
    monkeypatch.setattr(launcher, "DEFAULT_FRONTEND_DIST_DIR", frontend / "dist")
    monkeypatch.setattr(launcher.shutil, "which", lambda _name: "/safe/npm")
    monkeypatch.setenv("OPENAI_API_KEY", "never-pass-me")
    monkeypatch.setenv("TAVILY_API_KEY", "never-pass-me")
    monkeypatch.setenv("APP_GATEWAY_AUTH_SECRET", "never-pass-me")

    def fake_run(command, **kwargs):
        calls.append((command, kwargs["env"]))

    monkeypatch.setattr(launcher.subprocess, "run", fake_run)
    launcher.ensure_dist()

    assert [command for command, _env in calls] == [
        ["/safe/npm", "ci"], ["/safe/npm", "run", "build"],
    ]
    assert all(
        "OPENAI_API_KEY" not in environment
        and "TAVILY_API_KEY" not in environment
        and "APP_GATEWAY_AUTH_SECRET" not in environment
        and environment["NPM_CONFIG_USERCONFIG"] == os.devnull
        for _command, environment in calls
    )


def test_strict_offline_missing_dist_never_discovers_or_runs_npm(tmp_path, monkeypatch):
    frontend = tmp_path / "frontend"
    frontend.mkdir()
    monkeypatch.setattr(launcher, "SOURCE_LAYOUT", True)
    monkeypatch.setattr(launcher, "FRONTEND_SOURCE_DIR", frontend)
    monkeypatch.setattr(launcher, "DEFAULT_FRONTEND_DIST_DIR", frontend / "dist")
    monkeypatch.setattr(
        launcher.shutil,
        "which",
        lambda _name: pytest.fail("严格离线下不应查找 npm"),
    )
    monkeypatch.setattr(
        launcher.subprocess,
        "run",
        lambda *_args, **_kwargs: pytest.fail("严格离线下不应运行子进程"),
    )

    with pytest.raises(RuntimeError, match="frontend/dist"):
        launcher.ensure_dist(strict_offline=True)


def test_installed_missing_frontend_fails_without_discovering_npm(tmp_path, monkeypatch):
    monkeypatch.setattr(launcher, "SOURCE_LAYOUT", False)
    monkeypatch.setattr(launcher, "FRONTEND_SOURCE_DIR", None)
    monkeypatch.setattr(launcher, "DEFAULT_FRONTEND_DIST_DIR", tmp_path / "missing")
    monkeypatch.setattr(
        launcher.shutil,
        "which",
        lambda _name: pytest.fail("安装式 launcher 不应查找 npm"),
    )

    with pytest.raises(RuntimeError, match="安装不完整"):
        launcher.ensure_dist()


def test_source_wrapper_is_thin_and_console_entry_owns_the_launcher():
    wrapper = (REPOSITORY_ROOT / "run.py").read_text(encoding="utf-8")
    configuration = tomllib.loads(
        (REPOSITORY_ROOT / "backend" / "pyproject.toml").read_text(encoding="utf-8")
    )

    assert "careerdesk.bootstrap.desktop" in wrapper
    assert "class ServerRuntime" not in wrapper
    assert configuration["project"]["scripts"] == {
        "careerdesk": "careerdesk.bootstrap.desktop:main",
        "careerdesk-data": "careerdesk.bootstrap.cli:main",
    }


def test_lock_conflict_happens_before_frontend_build(monkeypatch, tmp_path):
    from careerdesk.platform.runtime import InstanceAlreadyRunningError

    lock_path = tmp_path / ".careerdesk.instance.lock"
    messages: list[str] = []
    monkeypatch.setattr(launcher, "ENV_FILE", tmp_path / "settings.env")
    monkeypatch.setattr(launcher, "configured_data_dir", lambda: tmp_path / "data")
    monkeypatch.setattr(launcher, "ensure_env", lambda: None)
    monkeypatch.setattr(
        "careerdesk.platform.runtime.acquire_instance_lock",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(
            InstanceAlreadyRunningError(lock_path, {"pid": 123}),
        ),
    )
    monkeypatch.setattr(
        launcher,
        "ensure_dist",
        lambda **_kwargs: (_ for _ in ()).throw(AssertionError("锁冲突后不得构建前端")),
    )
    monkeypatch.setattr(launcher, "_show_startup_error", messages.append)

    assert launcher.main() == 2
    assert messages and "另一个 CareerDesk" in messages[0]


def test_database_preflight_error_reaches_window_before_server_or_frontend(
    monkeypatch,
    tmp_path,
):
    from careerdesk.core import config as config_module
    from careerdesk.platform import database as database_module

    data_dir = tmp_path / "data"
    data_dir.mkdir()
    env_file = tmp_path / "settings.env"
    env_file.write_text("", encoding="utf-8")

    class Lock(_RecordingLock):
        path = data_dir / ".careerdesk.instance.lock"

    lock = Lock()
    messages: list[str] = []
    monkeypatch.setattr(launcher, "ENV_FILE", env_file)
    monkeypatch.setattr(launcher, "configured_data_dir", lambda: data_dir)
    monkeypatch.setattr(launcher, "ensure_env", lambda: None)
    monkeypatch.setattr(launcher, "_port_in_use", lambda *_args: False)
    monkeypatch.setattr(launcher, "_load_installed_credentials", lambda: None)
    monkeypatch.setattr(launcher, "_show_startup_error", messages.append)
    monkeypatch.setattr(
        "careerdesk.platform.runtime.acquire_instance_lock",
        lambda *_args, **_kwargs: lock,
    )
    monkeypatch.setattr(
        config_module,
        "get_settings",
        lambda: SimpleNamespace(
            data_dir=str(data_dir),
            db_path=str(data_dir / "careerdesk.db"),
            strict_offline=True,
        ),
    )
    monkeypatch.setattr(
        database_module,
        "init_db",
        lambda _path: (_ for _ in ()).throw(
            RuntimeError(
                "数据库版本 v25 不是当前 fresh-only v28；本仓库不提供旧 schema 迁移"
            )
        ),
    )
    monkeypatch.setattr(
        launcher,
        "ensure_dist",
        lambda **_kwargs: pytest.fail("schema 失败后不得构建前端"),
    )

    assert launcher.main() == 1
    assert messages == [
        "数据库版本 v25 不是当前 fresh-only v28；本仓库不提供旧 schema 迁移"
    ]
    assert lock.releases == 1


def test_relaunch_reuses_a_healthy_existing_instance(monkeypatch, tmp_path):
    """A second launch must reopen the running app, never demand a task-manager kill."""
    env_file = tmp_path / "settings.env"
    env_file.write_text("", encoding="utf-8")
    opened: list[bool] = []
    messages: list[str] = []

    monkeypatch.setattr(launcher, "ENV_FILE", env_file)
    monkeypatch.setattr(launcher, "ensure_env", lambda: None)
    monkeypatch.setattr(launcher, "_port_in_use", lambda *_args: True)
    monkeypatch.setattr(launcher, "_existing_careerdesk_instance", lambda *_args: True)
    monkeypatch.setattr(
        launcher, "_open_browser_unless_headless", lambda: opened.append(True) or True,
    )
    monkeypatch.setattr(launcher, "_show_startup_error", messages.append)
    monkeypatch.setattr(
        launcher,
        "_load_installed_credentials",
        lambda: (_ for _ in ()).throw(AssertionError("healthy reuse must return before startup")),
    )

    assert launcher.main() == 0
    assert opened == [True]
    assert messages == []


def test_relaunch_still_reports_a_foreign_port_owner(monkeypatch, tmp_path):
    env_file = tmp_path / "settings.env"
    env_file.write_text("", encoding="utf-8")
    messages: list[str] = []

    monkeypatch.setattr(launcher, "ENV_FILE", env_file)
    monkeypatch.setattr(launcher, "ensure_env", lambda: None)
    monkeypatch.setattr(launcher, "_port_in_use", lambda *_args: True)
    monkeypatch.setattr(launcher, "_existing_careerdesk_instance", lambda *_args: False)
    monkeypatch.setattr(launcher, "_show_startup_error", messages.append)

    assert launcher.main() == 2
    assert messages and "端口" in messages[0]
