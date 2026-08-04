"""Installable CareerDesk launcher for the backend and desktop window.

The normal surface is a native PyWebView window without a browser address bar;
closing it stops the server. Browser fallback is used only when a native window
cannot start. Repository ``run.py`` merely establishes the source resource root
before calling this module. The installed ``careerdesk`` console entry calls it
directly and therefore does not depend on the repository, uv, or Node.

Environment variables:
    PORT       Listening port, default 8000.
    NO_WINDOW  Use a browser instead of a desktop window when set to 1.
    CAREERDESK_HEADLESS  Start the service without any UI when set to 1.
"""

import logging
import os
import locale as system_locale
import shutil
import socket
import stat
import subprocess
import sys
import threading
import time
import webbrowser
from dataclasses import dataclass, field
from html import escape
from pathlib import Path
from typing import Any

from careerdesk.bootstrap.console import configure_console_streams
from careerdesk.core.paths import (
    DEFAULT_CONFIG_DIR,
    DEFAULT_DATA_DIR,
    DEFAULT_ENV_TEMPLATE,
    DEFAULT_FRONTEND_DIST_DIR,
    ENV_FILE,
    RESOURCE_ROOT,
    SOURCE_LAYOUT,
    canonical_data_dir,
)


HERE = RESOURCE_ROOT
FRONTEND_SOURCE_DIR = RESOURCE_ROOT / "frontend" if SOURCE_LAYOUT else None
os.environ.setdefault("APP_RUNTIME_MODE", "desktop")
HOST = "127.0.0.1"
PORT = int(os.environ.get("PORT", "8000"))
URL = f"http://{HOST}:{PORT}"
STARTUP_TIMEOUT_SECONDS = 30.0
# Closing the window means quit. In-flight turns are already crash-safe (ledger +
# recovery replay), so escalation may cost at most the current model response,
# never durable state. Unbounded waiting previously left an invisible resident
# process whenever a provider call hung.
_GRACEFUL_CLOSE_SECONDS = 15.0
_FORCED_CLOSE_SECONDS = 10.0


def _startup_locale() -> str:
    """Resolve the pre-UI locale without reading app or browser state."""
    language = system_locale.getlocale()[0] or os.environ.get("LANG", "")
    return "zh-CN" if language.lower().startswith("zh") else "en"


def _startup_text(zh_cn: str, en: str) -> str:
    return en if _startup_locale() == "en" else zh_cn


@dataclass(slots=True)
class ServerRuntime:
    """Launcher-owned Uvicorn lifetime; the thread is recorded before it can fail."""

    server: Any
    thread: threading.Thread | None = None
    error: BaseException | None = None
    _start_guard: threading.Lock = field(default_factory=threading.Lock, repr=False)
    _thread_exited: threading.Event = field(
        default_factory=threading.Event,
        init=False,
        repr=False,
    )
    _start_returned: bool = field(default=False, init=False, repr=False)

    def start(self) -> None:
        """Start exactly once, retaining the thread even across startup timeout/failure."""
        with self._start_guard:
            if self.thread is not None:
                raise RuntimeError(_startup_text(
                    "后端运行线程不能重复启动", "The backend runtime cannot be started twice",
                ))
            self.thread = threading.Thread(
                target=self._run,
                name="careerdesk-server",
                # A live request may still own a synchronous Tool worker. The launcher
                # must not silently abandon that process-level ownership boundary.
                daemon=False,
            )
            try:
                self.thread.start()
            except BaseException as error:  # noqa: BLE001 -- every start failure is uncertain
                # Thread.start() may raise after the native thread was already created.
                # Only _run's terminal event can prove that no server work remains.
                self.error = error
                raise
            self._start_returned = True

    def _run(self) -> None:
        try:
            self.server.run()
        except BaseException as error:  # noqa: BLE001 -- propagate thread failure to launcher
            self.error = error
        finally:
            # This is the sole source of truth for relinquishing data-dir ownership.
            self._thread_exited.set()

    @property
    def is_alive(self) -> bool:
        return not self._thread_exited.is_set()

    @property
    def may_be_running(self) -> bool:
        """Stay fail-closed until the server target itself reports terminal exit."""
        return not self._thread_exited.is_set()

    @property
    def start_returned(self) -> bool:
        """Whether Thread.start() completed, making an indefinite wait well-defined."""
        return self._start_returned

    def request_shutdown(self) -> None:
        """Idempotently stop accepting work; this does not claim the thread has exited."""
        self.server.should_exit = True

    def force_shutdown(self) -> None:
        """Escalate to Uvicorn's forced exit, abandoning graceful connection waits."""
        self.server.should_exit = True
        self.server.force_exit = True

    def wait_for_exit(self, timeout: float | None = None) -> bool:
        """Return true only once no server thread can still use the transferred lock."""
        return self._thread_exited.wait(timeout=timeout)


class DesktopBridge:
    """Narrow native bridge for explicit file selection, export, and reveal actions."""

    def __init__(self, webview_module: Any):
        self._webview = webview_module
        self._window = None
        self._last_job_import_template: Path | None = None
        self._last_job_import_template_locale = "zh-CN"

    def bind(self, window: Any) -> None:
        self._window = window

    def select_data_directory(self) -> str | None:
        if self._window is None:
            return None
        selected = self._window.create_file_dialog(
            self._webview.FileDialog.FOLDER,
            allow_multiple=False,
        )
        if not selected:
            return None
        parent = Path(selected[0]).expanduser()
        for index in range(1, 101):
            name = "CareerDesk Data" if index == 1 else f"CareerDesk Data {index}"
            candidate = parent / name
            if not candidate.exists():
                return str(candidate)
        return None

    def download_job_import_template(self, locale: str = "zh-CN") -> str:
        """Copy the bundled workbook to Downloads without overwriting an existing file."""
        if locale not in {"zh-CN", "en"}:
            raise ValueError("Unsupported locale")
        source = DEFAULT_FRONTEND_DIST_DIR / f"careerdesk-job-import-example-{locale}.xlsx"
        english = locale == "en"
        try:
            source_info = os.lstat(source)
        except OSError as error:
            raise RuntimeError("The bundled template is unavailable. Reinstall CareerDesk." if english else "内置表格模板不可用，请重新安装 CareerDesk。") from error
        if not stat.S_ISREG(source_info.st_mode) or stat.S_ISLNK(source_info.st_mode):
            raise RuntimeError("The bundled template failed a security check and was not downloaded." if english else "内置表格模板不安全，已拒绝下载。")

        downloads = (Path.home() / "Downloads").resolve()
        try:
            downloads.mkdir(parents=True, exist_ok=True)
        except OSError as error:
            raise RuntimeError("Could not access the system Downloads folder." if english else "无法访问系统下载目录。") from error
        if not downloads.is_dir():
            raise RuntimeError("The system Downloads folder is unavailable." if english else "系统下载目录不可用。")

        for index in range(1, 101):
            suffix = "" if index == 1 else f" ({index})"
            stem = "CareerDesk Role Import Template" if english else "CareerDesk 岗位导入模板"
            destination = downloads / f"{stem}{suffix}.xlsx"
            flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL
            flags |= getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0)
            try:
                descriptor = os.open(destination, flags, 0o644)
            except FileExistsError:
                continue
            except OSError as error:
                raise RuntimeError("Could not write the template to the system Downloads folder." if english else "表格模板无法写入系统下载目录。") from error
            try:
                with source.open("rb") as input_file, os.fdopen(descriptor, "wb") as output:
                    descriptor = -1
                    shutil.copyfileobj(input_file, output)
                    output.flush()
                    os.fsync(output.fileno())
            except BaseException:
                if descriptor >= 0:
                    os.close(descriptor)
                destination.unlink(missing_ok=True)
                raise
            self._last_job_import_template = destination.resolve()
            self._last_job_import_template_locale = locale
            return str(self._last_job_import_template)
        raise RuntimeError("Too many templates with the same name are already in Downloads. Remove some and retry." if english else "下载目录中已有过多同名表格模板，请先整理后重试。")

    def open_job_import_template(self, path: str) -> bool:
        """Open only the exact workbook created by the latest bridge download."""
        requested = Path(path).expanduser().resolve()
        if requested != self._last_job_import_template or not requested.is_file():
            raise RuntimeError("Only the template downloaded in this CareerDesk session can be opened." if self._last_job_import_template_locale == "en" else "只能打开本次由 CareerDesk 下载的表格模板。")
        environment = _frontend_subprocess_environment()
        try:
            if sys.platform == "darwin":
                subprocess.run(["/usr/bin/open", str(requested)], check=True, env=environment)
            elif sys.platform == "win32":
                os.startfile(requested)  # type: ignore[attr-defined]
            else:
                subprocess.run(["xdg-open", str(requested)], check=True, env=environment)
        except (OSError, subprocess.CalledProcessError) as error:
            message = f"Could not open the template: {requested}" if self._last_job_import_template_locale == "en" else f"无法打开模板：{requested}"
            raise RuntimeError(message) from error
        return True


def configured_data_dir() -> Path:
    """Read only APP_DATA_DIR so the launcher locks before loading credentials."""
    raw = os.environ.get("APP_DATA_DIR")
    if raw is None and os.path.lexists(ENV_FILE):
        from dotenv import dotenv_values

        raw = dotenv_values(ENV_FILE).get("APP_DATA_DIR")

    return canonical_data_dir(DEFAULT_DATA_DIR if raw is None else raw)


def _frontend_subprocess_environment() -> dict[str, str]:
    """Build the frontend with only the system environment Node requires."""
    allowed = {
        "PATH", "HOME", "USERPROFILE", "SYSTEMROOT", "WINDIR", "COMSPEC",
        "TEMP", "TMP", "TMPDIR", "LANG", "LC_ALL", "SSL_CERT_FILE", "SSL_CERT_DIR",
        "NODE_EXTRA_CA_CERTS", "NO_COLOR", "CI",
    }
    environment = {name: value for name, value in os.environ.items() if name in allowed}
    environment.setdefault("PATH", os.defpath)
    environment.update({
        "NPM_CONFIG_USERCONFIG": os.devnull,
        "NPM_CONFIG_AUDIT": "false",
        "NPM_CONFIG_FUND": "false",
        "NPM_CONFIG_UPDATE_NOTIFIER": "false",
        "NO_UPDATE_NOTIFIER": "1",
    })
    return environment


def ensure_env() -> None:
    """Create the initial config exclusively at 0600 without following symlinks."""
    env_file = ENV_FILE
    example = DEFAULT_ENV_TEMPLATE
    if not SOURCE_LAYOUT and (
        env_file.parent == DEFAULT_CONFIG_DIR.resolve()
        or not env_file.parent.exists()
    ):
        from careerdesk.platform.storage.private import ensure_private_directory

        ensure_private_directory(env_file.parent)
    try:
        existing = os.lstat(env_file)
    except FileNotFoundError:
        existing = None
    if existing is not None:
        if stat.S_ISLNK(existing.st_mode) or not stat.S_ISREG(existing.st_mode):
            raise RuntimeError(_startup_text(
                "配置文件必须是普通文件，不能是符号链或特殊文件",
                "The configuration must be a regular file, not a symlink or special file",
            ))
        if existing.st_nlink != 1:
            raise RuntimeError(_startup_text(
                "配置文件存在额外硬链接，为避免把密钥写入其他路径已拒绝启动",
                "The configuration has an extra hard link; startup was refused to avoid writing secrets to another path",
            ))
        if os.name != "nt":
            os.chmod(env_file, stat.S_IMODE(existing.st_mode) & 0o700)
        return
    if not example.is_file():
        return

    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL
    flags |= getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0)
    try:
        fd = os.open(env_file, flags, 0o600)
    except FileExistsError:
        # Another concurrent launcher won creation; validate it without overwriting.
        return ensure_env()
    descriptor = fd
    try:
        os.set_inheritable(descriptor, False)
        with os.fdopen(descriptor, "wb") as output:
            descriptor = -1  # The file object owns and closes the descriptor.
            output.write(example.read_bytes())
            output.flush()
            os.fsync(output.fileno())
    except BaseException:
        if descriptor >= 0:
            os.close(descriptor)
            descriptor = -1
        try:
            env_file.unlink()
        except OSError:
            pass
        raise
    finally:
        if descriptor >= 0:
            os.close(descriptor)
    if SOURCE_LAYOUT:
        print(f"🔧 已生成 {env_file.name}：请填入 APP_LLM_MODEL 与对应 API key（见文件内三档说明）后重开以解锁全部能力。")
    else:
        print(f"🔧 已生成 {env_file.name}：请启动 CareerDesk 后在设置页选择模型并保存凭据。")


def ensure_dist(*, strict_offline: bool = False) -> None:
    """Validate bundled assets; only a source layout may attempt an npm build."""
    dist = DEFAULT_FRONTEND_DIST_DIR
    if (dist / "index.html").is_file():
        return
    if not SOURCE_LAYOUT or FRONTEND_SOURCE_DIR is None:
        raise RuntimeError(_startup_text(
            "CareerDesk 安装不完整：缺少内置前端资源。请重新安装完整发行包。",
            "CareerDesk is missing bundled frontend resources. Reinstall the complete release.",
        ))
    if strict_offline:
        # npm may access a registry and run dependency scripts, so the global
        # offline gate must precede discovery and every subprocess.
        raise RuntimeError(_startup_text(
            "严格离线已开启，但本安装缺少 frontend/dist。为避免 npm 自动访问网络，已停止启动；请使用完整发行包，或在允许联网时先构建前端。",
            "Strict offline mode is enabled, but frontend/dist is missing. Startup stopped to prevent npm network access. Use a complete release or build the frontend while network access is allowed.",
        ))
    npm = shutil.which("npm")
    if not npm:
        print("🔧 未找到前端产物 frontend/dist 且本机无 npm：将只提供 API、不出网页。"
              "请先构建前端（cd frontend && npm install && npm run build）或改用 Docker。")
        return
    print("🔧 首次运行：正在构建前端（frontend/dist 不存在，需要几分钟）…")
    try:
        environment = _frontend_subprocess_environment()
        install = "ci" if (FRONTEND_SOURCE_DIR / "package-lock.json").is_file() else "install"
        subprocess.run([npm, install], cwd=FRONTEND_SOURCE_DIR, check=True, env=environment)
        subprocess.run([npm, "run", "build"], cwd=FRONTEND_SOURCE_DIR, check=True, env=environment)
    except subprocess.CalledProcessError:
        print("🔧 前端构建失败：将只提供 API。请手动 cd frontend && npm install && npm run build 排查。")


def _load_installed_credentials() -> None:
    """Inject OS-keyring credentials before importing core.config/providers."""
    if SOURCE_LAYOUT:
        return
    from careerdesk.platform.credentials import inject_system_credentials

    status = inject_system_credentials(config_file=ENV_FILE)
    if not status.available:
        print(f"🔧 {status.issue} 本地模型和无需凭据的功能仍可使用。")


def _port_in_use(host: str, port: int) -> bool:
    """Probe the port for UX; the data-directory lock enforces single instance."""
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.settimeout(0.3)
        return sock.connect_ex((host, port)) == 0


def _headless_mode() -> bool:
    return os.environ.get("CAREERDESK_HEADLESS") == "1"


def _open_browser_unless_headless() -> bool:
    if _headless_mode():
        return False
    webbrowser.open(URL)
    return True


def _show_startup_error(message: str) -> None:
    """Show terminal startup failures in a GUI while preserving console output."""
    print(f"🔧 {message}")
    if os.environ.get("NO_WINDOW") or _headless_mode():
        return
    try:
        import webview

        body = escape(message)
        title = _startup_text("CareerDesk 无法启动", "CareerDesk couldn't start")
        safety_note = _startup_text(
            "启动流程已安全停止；原有业务记录不会因本错误被主动删除。权限收紧、锁文件或已提交的事务化初始化可能已经完成。",
            "Startup stopped safely. This error does not actively delete existing records. Permission hardening, lock files, or committed transactional initialization may already have completed.",
        )
        webview.create_window(
            title,
            html=(
                "<main style='font:15px system-ui;padding:28px;line-height:1.6'>"
                f"<h2 style='margin-top:0'>{escape(title)}</h2>"
                f"<p>{body}</p><p>{escape(safety_note)}</p></main>"
            ),
            width=620,
            height=320,
        )
        webview.start()
    except Exception:
        # A server/headless environment must not replace the original error.
        return


def _create_main_window(webview_module: Any) -> tuple[Any, DesktopBridge]:
    """Create the desktop shell with ordinary document selection enabled.

    PyWebView defaults ``text_select`` to false and injects a page-wide
    ``user-select: none`` rule.  CareerDesk is a document-heavy application, so
    the native shell must opt into the same selection/copy behavior as a browser.
    """
    bridge = DesktopBridge(webview_module)
    window = webview_module.create_window(
        "CareerDesk",
        URL,
        width=1200,
        height=820,
        js_api=bridge,
        text_select=True,
    )
    # PyWebView fires ``loaded`` on a worker. Only macOS schedules the native
    # adjustment back to AppKit's main thread; other surfaces keep normal scroll.
    window.events.loaded += _schedule_macos_scroll_surface_configuration
    bridge.bind(window)
    return window, bridge


def _disable_native_scroll_rubber_banding(window: Any) -> bool:
    """Disable only WKWebView's boundary elasticity, never wheel scrolling itself."""
    try:
        native_window = getattr(window, "native", None)
        content_view = native_window.contentView() if native_window is not None else None
        setter = getattr(content_view, "_setRubberBandingEnabled_", None)
        if not callable(setter):
            return False
        setter(False)
        return True
    except Exception:  # noqa: BLE001 - optional native polish must never block app startup
        return False


def _schedule_macos_scroll_surface_configuration(window: Any) -> None:
    """Keep the private-but-guarded WKWebView adjustment on AppKit's main thread."""
    if sys.platform != "darwin":
        return
    try:
        from PyObjCTools import AppHelper

        AppHelper.callAfter(_disable_native_scroll_rubber_banding, window)
    except (ImportError, AttributeError):
        # Future WebKit/PyObjC versions may remove the guarded selector. In that
        # case ordinary scrolling must continue; only the elastic-edge polish is lost.
        return


def _start_server_and_wait(
    runtime: ServerRuntime,
    *,
    timeout: float = STARTUP_TIMEOUT_SECONDS,
) -> None:
    """Start Uvicorn and return only after lifespan, retaining thread ownership."""
    runtime.start()
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if runtime.error is not None:
            raise RuntimeError(_startup_text(
                f"后端启动失败：{runtime.error}", f"Backend startup failed: {runtime.error}",
            )) from runtime.error
        if runtime.server.started and runtime.is_alive:
            return
        if not runtime.is_alive:
            if runtime.error is not None:
                raise RuntimeError(_startup_text(
                    f"后端启动失败：{runtime.error}", f"Backend startup failed: {runtime.error}",
                )) from runtime.error
            raise RuntimeError(_startup_text(
                "后端在完成启动前已退出", "The backend exited before startup completed",
            ))
        time.sleep(0.05)
    runtime.request_shutdown()
    raise RuntimeError(_startup_text(
        f"后端在 {timeout:g} 秒内未完成启动",
        f"The backend did not finish starting within {timeout:g} seconds",
    ))


def _request_server_shutdown(
    runtime: ServerRuntime,
    *,
    timeout: float | None = None,
) -> bool:
    """Request Uvicorn shutdown and report whether its thread actually stopped."""
    runtime.request_shutdown()
    return runtime.wait_for_exit(timeout=timeout)


_FILE_LOGGING_CONFIGURED = False
_FILE_LOG_HANDLER: logging.Handler | None = None


def _configure_file_logging(settings: Any) -> None:
    """Persist WARNING+ runtime logs where the storage page already points users.

    The windowed app has no console, so without this file an unhandled server
    error leaves no trace anywhere. Diagnostics must never block startup, so
    every step including reading the configured location stays inside the guard.
    """
    global _FILE_LOGGING_CONFIGURED
    if _FILE_LOGGING_CONFIGURED:
        return
    try:
        from logging.handlers import RotatingFileHandler

        from careerdesk.platform.storage.private import ensure_private_directory

        directory = ensure_private_directory(Path(settings.log_dir))
        log_file = directory / "careerdesk.log"
        handler = RotatingFileHandler(
            log_file, maxBytes=1_000_000, backupCount=3, encoding="utf-8",
        )
        if os.name == "posix":
            os.chmod(log_file, 0o600)
        handler.setLevel(logging.WARNING)
        handler.setFormatter(logging.Formatter(
            "%(asctime)s %(levelname)s %(name)s %(message)s",
        ))
        logging.getLogger().addHandler(handler)
        global _FILE_LOG_HANDLER
        _FILE_LOG_HANDLER = handler
        _FILE_LOGGING_CONFIGURED = True
        try:
            from importlib.metadata import version as _distribution_version

            app_version = _distribution_version("careerdesk")
        except Exception:  # noqa: BLE001 -- version is banner metadata only
            app_version = "unknown"
        # One banner per session proves the log pipeline works on the user's
        # machine, so an empty file can never again mean "unknown state".
        logging.getLogger("careerdesk.launcher").warning(
            "CareerDesk %s file logging active on %s", app_version, sys.platform,
        )
    except Exception:  # noqa: BLE001 -- logging setup must never prevent startup
        return


def _attach_uvicorn_file_logging() -> None:
    """Route Uvicorn's own records into the launcher log file.

    Uvicorn's logging config keeps its loggers non-propagating, so protocol-level
    failures would otherwise reach only the invisible windowed console.
    """
    if _FILE_LOG_HANDLER is None:
        return
    for logger_name in ("uvicorn", "uvicorn.error"):
        target = logging.getLogger(logger_name)
        if _FILE_LOG_HANDLER not in target.handlers:
            target.addHandler(_FILE_LOG_HANDLER)


def _existing_careerdesk_instance(port: int) -> bool:
    """Best-effort check that the busy port is a healthy CareerDesk instance."""
    from urllib.request import urlopen

    try:
        with urlopen(f"http://{HOST}:{port}/healthz", timeout=2) as response:
            if response.status != 200 or response.read(16).strip() != b"ok":
                return False
        with urlopen(f"http://{HOST}:{port}/", timeout=2) as response:
            return response.status == 200 and b"CareerDesk" in response.read(65536)
    except Exception:  # noqa: BLE001 -- any failure means "not provably ours"
        return False


def _unblock_dotnet_assemblies() -> int:
    """Remove mark-of-the-web from bundled .NET assemblies; return how many.

    Explorer's zip extraction tags every file as downloaded, and the .NET
    Framework then refuses to load such assemblies, which silently degrades the
    native window into a browser fallback. Stripping the Zone.Identifier stream
    is safe: the assemblies ship inside our own verified archive.
    """
    if os.name != "nt" or not getattr(sys, "frozen", False):
        return 0
    bundle_root = Path(getattr(sys, "_MEIPASS", Path(sys.executable).parent / "_internal"))
    removed = 0
    for subdirectory in ("pythonnet", "clr_loader"):
        directory = bundle_root / subdirectory
        if not directory.is_dir():
            continue
        for assembly in directory.rglob("*.dll"):
            try:
                os.remove(f"{assembly}:Zone.Identifier")
                removed += 1
            except OSError:
                continue
    if removed:
        logging.getLogger("careerdesk.launcher").warning(
            "removed mark-of-the-web from %d bundled .NET assemblies", removed,
        )
    return removed


def _run_window_smoke() -> int:
    """Report whether the bundled native window stack can start on this host.

    CI runs this inside the frozen executable so a broken WebView2/pythonnet
    bundle becomes visible in build logs instead of first failing on a user's
    machine as a silent browser fallback.
    """
    _unblock_dotnet_assemblies()
    try:
        import webview

        window = webview.create_window(
            "CareerDesk window smoke", html="<html></html>", hidden=True,
        )
        timer = threading.Timer(3.0, window.destroy)
        timer.daemon = True
        timer.start()
        webview.start()
    except BaseException as error:  # noqa: BLE001 -- the exact reason must reach CI logs
        print(f"WINDOW_SMOKE_FAILED error_type={type(error).__name__} detail={error}")
        return 0
    print("WINDOW_SMOKE_OK")
    return 0


def _stop_server_for_exit(runtime: ServerRuntime) -> bool:
    """Stop Uvicorn for process exit without ever leaving a hidden resident process.

    Returns False only when even the forced stop timed out; the caller must then
    end the process, which releases the OS-level instance lock atomically with
    thread death instead of releasing it while a worker could still write.
    """
    if _request_server_shutdown(runtime, timeout=_GRACEFUL_CLOSE_SECONDS):
        return True
    print(_startup_text(
        "🔧 在途任务未在限时内结束，正在强制停止后端…",
        "🔧 In-flight work did not finish in time; forcing the backend to stop…",
    ))
    runtime.force_shutdown()
    return runtime.wait_for_exit(timeout=_FORCED_CLOSE_SECONDS)


def _release_instance_lock_if_server_stopped(
    instance_lock,
    runtime: ServerRuntime | None,
) -> bool:
    """Release the launcher lock only when the server cannot use the data root."""
    if runtime is not None and runtime.may_be_running:
        return False
    instance_lock.release()
    return True


def main() -> int:
    """Prepare config/assets, start Uvicorn, then open the native window."""
    configure_console_streams()
    if os.environ.get("CAREERDESK_WINDOW_SMOKE") == "1":
        return _run_window_smoke()
    os.chdir(HERE)  # Stabilize cwd; resources and writable roots use absolute paths.
    instance_lock = None
    server_runtime = None
    try:
        # Existing config is only validated/hardened. Initial creation waits for
        # the data lock so concurrent launches cannot observe a partial file.
        env_missing = not os.path.lexists(ENV_FILE)
        if not env_missing:
            ensure_env()

        from careerdesk.platform.runtime import (
            InstanceAlreadyRunningError,
            InstanceLockError,
            acquire_instance_lock,
        )

        try:
            instance_lock = acquire_instance_lock(
                configured_data_dir(),
                entrypoint="desktop-launcher",
                url=URL,
            )
        except InstanceAlreadyRunningError as error:
            owner_url = error.owner.get("url") if error.owner else None
            hint = (_startup_text(f"（已有实例：{owner_url}）", f" (running instance: {owner_url})")
                    if owner_url else "")
            _show_startup_error(
                _startup_text(
                    f"另一个 CareerDesk 正在使用同一数据目录{hint}。请先关闭已打开的窗口；换端口不能绕过这个保护。",
                    f"Another CareerDesk instance is using the same data directory{hint}. Close the open window first; changing ports cannot bypass this protection.",
                )
            )
            return 2
        except InstanceLockError as error:
            _show_startup_error(_startup_text(
                f"无法安全锁定数据目录：{error}。请检查 APP_DATA_DIR 权限和磁盘类型。",
                f"The data directory could not be locked safely: {error}. Check APP_DATA_DIR permissions and the filesystem type.",
            ))
            return 2

        if env_missing:
            ensure_env()

        if _port_in_use(HOST, PORT):
            if _existing_careerdesk_instance(PORT):
                # Browser-fallback sessions have no window to close, so a prior
                # instance may legitimately still be serving. Reopening its UI is
                # what this launch means; a port error would send the user to the
                # task manager instead.
                print(_startup_text(
                    f"🔧 CareerDesk 已在运行，正在打开现有实例 → {URL}",
                    f"🔧 CareerDesk is already running; opening the existing instance → {URL}",
                ))
                _open_browser_unless_headless()
                return 0
            _show_startup_error(
                _startup_text(
                    f"端口 {PORT} 已被其他程序占用。请关闭占用者后重开；如需独立第二实例，必须同时指定新端口和不同的 APP_DATA_DIR。",
                    f"Port {PORT} is already in use. Close the other program and reopen CareerDesk. A separate second instance requires both a new port and a different APP_DATA_DIR.",
                )
            )
            return 2

        _load_installed_credentials()

        from careerdesk.core.config import get_settings

        settings = get_settings()
        if Path(settings.data_dir) != instance_lock.path.parent:
            raise RuntimeError(_startup_text(
                "APP_DATA_DIR 在启动期间发生了变化，已拒绝在未持有正确锁时启动",
                "APP_DATA_DIR changed during startup; startup was refused without the correct lock",
            ))

        # Run schema initialization outside Uvicorn because lifespan collapses
        # failures into SystemExit(3). This preserves actionable migration errors.
        from careerdesk.platform.database import init_db

        _configure_file_logging(settings)
        init_db(settings.db_path)
        # The lock covers the database and initial build, preventing duplicate npm.
        ensure_dist(strict_offline=settings.strict_offline)

        import uvicorn
        from careerdesk.bootstrap.app import create_app

        server_runtime = ServerRuntime(uvicorn.Server(uvicorn.Config(
            create_app(instance_lock=instance_lock),
            host=HOST,
            port=PORT,
            log_level="info",
            # Do not force-cancel ASGI while synchronous tools run in to_thread.
            # The single-owner lock remains held until the real thread exits.
            timeout_graceful_shutdown=None,
        )))
        _attach_uvicorn_file_logging()
        _start_server_and_wait(server_runtime)

        webview_unavailable: Exception | None = None
        _unblock_dotnet_assemblies()
        try:
            import webview  # Standard dependency; unsupported hosts use a browser.
        except ImportError as import_error:
            webview = None
            webview_unavailable = import_error
        if os.environ.get("NO_WINDOW") or _headless_mode():
            webview = None
            webview_unavailable = None
        if webview is None and webview_unavailable is not None:
            logging.getLogger("careerdesk.launcher").warning(
                "native window unavailable error_type=%s detail=%s; using the browser instead",
                type(webview_unavailable).__name__,
                webview_unavailable,
            )

        if webview is None:
            destination = (
                "浏览器已打开" if _open_browser_unless_headless() else "无界面验收"
            )
            print(f"🔧 CareerDesk 运行中 → {URL}（{destination}；Ctrl-C 停止）")
            try:
                while not server_runtime.wait_for_exit(timeout=0.5):
                    pass
            except KeyboardInterrupt:
                pass
            return 0

        print(f"🔧 CareerDesk 桌面窗口模式 → {URL}（服务已就绪；关闭窗口即停止）")
        try:
            _create_main_window(webview)
            webview.start()
        except Exception as error:
            logging.getLogger("careerdesk.launcher").warning(
                "native window failed to start error_type=%s detail=%s; using the browser instead",
                type(error).__name__,
                error,
            )
            opened = _open_browser_unless_headless()
            fallback = "退回浏览器" if opened else "保持无界面运行"
            print(f"🔧 原生窗口启动失败（{error}），{fallback} → {URL}（Ctrl-C 停止）")
            try:
                while not server_runtime.wait_for_exit(timeout=0.5):
                    pass
            except KeyboardInterrupt:
                pass
        return 0
    except Exception as error:  # noqa: BLE001 -- GUI users need one actionable error
        _show_startup_error(str(error))
        return 1
    finally:
        clean_exit = True
        try:
            if server_runtime is not None:
                clean_exit = _stop_server_for_exit(server_runtime)
        finally:
            # This is an idempotent fallback when lifespan owns release. Even if
            # Ctrl-C interrupts Event.wait, the guard trusts only _run's exit event.
            if instance_lock is not None:
                released = _release_instance_lock_if_server_stopped(instance_lock, server_runtime)
                if released and not SOURCE_LAYOUT:
                    from careerdesk.platform.storage.location import (
                        perform_pending_migration,
                        record_migration_failure,
                    )

                    try:
                        destination = perform_pending_migration()
                        if destination is not None:
                            print(
                                "🔧 数据目录迁移完成并已切换；原目录仍完整保留 → "
                                f"{destination}"
                            )
                    except Exception as error:  # noqa: BLE001 -- next launch must disclose failure
                        record_migration_failure(error)
                        print(f"🔧 数据目录迁移未完成：{error}；旧目录仍保持有效。")
            if not clean_exit:
                print(_startup_text(
                    "🔧 后端未能在限时内停止；强制结束进程，实例锁随进程释放。",
                    "🔧 The backend did not stop in time; ending the process now. The instance lock is released with it.",
                ))
                logging.shutdown()
                os._exit(0)


if __name__ == "__main__":
    raise SystemExit(main())
