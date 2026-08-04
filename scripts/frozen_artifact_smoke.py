"""Exercise both executables from a frozen desktop artifact on its native OS."""

from __future__ import annotations

import argparse
import json
from contextlib import closing
import os
from pathlib import Path
import re
import socket
import sqlite3
import subprocess
import sys
import tempfile
import time
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

from careerdesk.platform.database import init_db
from careerdesk.platform.database.schema import SCHEMA_VERSION


_SYSTEM_ENVIRONMENT = frozenset({
    "PATH",
    "SYSTEMROOT",
    "WINDIR",
    "COMSPEC",
    "PATHEXT",
    "LANG",
    "LC_ALL",
    "SSL_CERT_FILE",
    "SSL_CERT_DIR",
})
_ASSET_PATTERN = re.compile(r'''(?:src|href)=["'](/assets/[^"']+)["']''')


def _isolated_environment(root: Path, *, port: int | None = None) -> dict[str, str]:
    root.mkdir(parents=True, mode=0o700, exist_ok=True)
    environment = {
        name: value for name, value in os.environ.items() if name in _SYSTEM_ENVIRONMENT
    }
    environment.update({
        "HOME": str(root / "home"),
        "USERPROFILE": str(root / "home"),
        "LOCALAPPDATA": str(root / "local-app-data"),
        "TEMP": str(root / "temp"),
        "TMP": str(root / "temp"),
        "TMPDIR": str(root / "temp"),
        "APP_DATA_DIR": str(root / "runtime-data"),
        "APP_LOG_DIR": str(root / "runtime-logs"),
        "CAREERDESK_CONFIG_FILE": str(root / "runtime-config" / "settings.env"),
        "PYTHON_KEYRING_BACKEND": "keyring.backends.null.Keyring",
        "APP_LLM_MODEL": "",
        "NO_WINDOW": "1",
        "CAREERDESK_HEADLESS": "1",
    })
    if port is not None:
        environment["PORT"] = str(port)
    for name in ("home", "local-app-data", "temp"):
        (root / name).mkdir(mode=0o700, exist_ok=True)
    return environment


def _free_port() -> int:
    with closing(socket.socket(socket.AF_INET, socket.SOCK_STREAM)) as probe:
        probe.bind(("127.0.0.1", 0))
        return int(probe.getsockname()[1])


def _http_json(method: str, url: str, payload: dict | None = None) -> tuple[int, dict]:
    body = None if payload is None else json.dumps(payload).encode("utf-8")
    request = Request(url, data=body, method=method, headers={
        "Content-Type": "application/json",
        "X-CareerDesk-Request": "1",
    })
    try:
        with urlopen(request, timeout=10) as response:  # noqa: S310 -- fixed loopback URL
            return response.status, json.loads(response.read().decode("utf-8"))
    except HTTPError as error:
        body = error.read().decode("utf-8", errors="replace")
        try:
            parsed = json.loads(body)
        except json.JSONDecodeError:
            parsed = {"raw": body[:1000]}
        return error.code, parsed


def _tail(path: Path, limit: int = 4000) -> str:
    try:
        return path.read_text(encoding="utf-8", errors="replace")[-limit:]
    except OSError:
        return "<unreadable>"


def _http_get(url: str) -> tuple[int, bytes]:
    request = Request(url, headers={"Host": "127.0.0.1"})
    with urlopen(request, timeout=2) as response:
        return response.status, response.read()


def _wait_for_server(process: subprocess.Popen, port: int) -> bytes:
    deadline = time.monotonic() + 30
    last_error: BaseException | None = None
    while time.monotonic() < deadline:
        if process.poll() is not None:
            raise RuntimeError(f"frozen desktop exited before readiness: {process.returncode}")
        try:
            status, body = _http_get(f"http://127.0.0.1:{port}/healthz")
            if status == 200 and body == b"ok":
                index_status, index = _http_get(f"http://127.0.0.1:{port}/")
                if index_status != 200:
                    raise RuntimeError("frozen frontend index did not return 200")
                return index
        except (OSError, URLError, RuntimeError) as error:
            last_error = error
            time.sleep(0.1)
    raise RuntimeError("frozen desktop did not become ready in 30 seconds") from last_error


def _check_database(path: Path) -> None:
    with closing(sqlite3.connect(path)) as connection:
        if connection.execute("PRAGMA integrity_check").fetchone() != ("ok",):
            raise RuntimeError("frozen database integrity_check failed")
        if connection.execute("PRAGMA foreign_key_check").fetchone() is not None:
            raise RuntimeError("frozen database foreign_key_check failed")
        if connection.execute("PRAGMA user_version").fetchone() != (SCHEMA_VERSION,):
            raise RuntimeError("frozen database is not at the current schema version")


def _run_data_command(command: list[str], environment: dict[str, str]) -> None:
    result = subprocess.run(
        command,
        env=environment,
        check=False,
        capture_output=True,
        encoding="utf-8",
        errors="strict",
        timeout=60,
    )
    if result.returncode != 0:
        diagnostic = "\n".join(
            part.strip() for part in (result.stdout, result.stderr) if part.strip()
        )
        raise RuntimeError(
            f"frozen data command failed with exit {result.returncode}: "
            f"{diagnostic[-4000:] or 'no diagnostic output'}"
        )


def _run_data_round_trip(executable: Path, root: Path) -> None:
    source = root / "backup-source"
    init_db(str(source / "careerdesk.db"))
    upload = source / "uploads/resumes/user"
    upload.mkdir(parents=True)
    (upload / "resume.md").write_text("frozen release smoke", encoding="utf-8")
    backup = root / "smoke.jpbak"
    restored = root / "backup-restored"
    environment = _isolated_environment(root / "data-command")
    _run_data_command(
        [str(executable), "backup", str(backup), "--data-dir", str(source)],
        environment,
    )
    _run_data_command(
        [str(executable), "restore", str(backup), "--destination", str(restored)],
        environment,
    )
    _check_database(restored / "careerdesk.db")
    if (restored / "uploads/resumes/user/resume.md").read_text() != "frozen release smoke":
        raise RuntimeError("frozen data command lost an uploaded file")


def smoke(desktop_executable: Path, data_executable: Path) -> None:
    if not desktop_executable.is_file() or not data_executable.is_file():
        raise FileNotFoundError("frozen desktop artifact is missing an executable")
    # WebView2 leaves Crashpad files behind briefly; leftover temp data on an
    # ephemeral runner is acceptable while a cleanup crash would mask a green run.
    with tempfile.TemporaryDirectory(
        prefix="careerdesk-frozen-smoke-", ignore_cleanup_errors=True,
    ) as temporary:
        root = Path(temporary)
        _run_data_round_trip(data_executable, root)
        port = _free_port()
        environment = _isolated_environment(root / "desktop", port=port)
        log_path = root / "desktop.log"
        with log_path.open("wb") as log:
            process = subprocess.Popen(
                [str(desktop_executable)],
                env=environment,
                stdout=log,
                stderr=subprocess.STDOUT,
            )
            try:
                index = _wait_for_server(process, port)
                assets = sorted(set(_ASSET_PATTERN.findall(index.decode("utf-8"))))
                if not assets:
                    raise RuntimeError("frozen frontend index has no hashed assets")
                for asset in assets:
                    status, body = _http_get(f"http://127.0.0.1:{port}{asset}")
                    if status != 200 or not body:
                        raise RuntimeError(f"frozen frontend asset failed: {asset}")
            finally:
                if process.poll() is None:
                    process.terminate()
                    try:
                        process.wait(timeout=10)
                    except subprocess.TimeoutExpired:
                        process.kill()
                        process.wait(timeout=10)
        _check_database(root / "desktop/runtime-data/careerdesk.db")
        _check_file_logging(root / "desktop")
        if os.environ.get("CAREERDESK_SMOKE_KEYS") == "1" and sys.platform == "win32":
            _check_system_key_save(desktop_executable, root)
            _check_desktop_shortcut_helper(desktop_executable.parent)
            _check_native_window_after_download_taint(desktop_executable, root)


def _check_file_logging(environment_root: Path) -> None:
    """The launcher must prove its log pipeline on every platform it ships to."""
    log_file = environment_root / "runtime-logs" / "careerdesk.log"
    if "file logging active" not in _tail(log_file):
        raise RuntimeError(
            f"frozen launcher log banner missing at {log_file}: {_tail(log_file, 800)!r}"
        )


def _check_system_key_save(desktop_executable: Path, root: Path) -> None:
    """Save a key through the real OS credential store, exactly as a user would.

    This is the reported-broken path: PUT /api/settings with an API key on an
    installed build. It must fail the release, with full diagnostics, before it
    can fail on a user's machine again.
    """
    port = _free_port()
    environment = _isolated_environment(root / "keys", port=port)
    # The real platform backend is the subject under test here, and a real user
    # launch carries no APP_LLM_MODEL variable; leaving it set would trip the
    # externally-managed-environment guard instead of exercising the save path.
    environment.pop("PYTHON_KEYRING_BACKEND", None)
    environment.pop("APP_LLM_MODEL", None)
    server_log = root / "keys-desktop.log"
    with server_log.open("wb") as log:
        process = subprocess.Popen(
            [str(desktop_executable)], env=environment,
            stdout=log, stderr=subprocess.STDOUT,
        )
        try:
            _wait_for_server(process, port)
            base = f"http://127.0.0.1:{port}"
            status, state = _http_json("GET", f"{base}/api/settings")
            if status != 200:
                raise RuntimeError(f"settings read failed: {status} {state}")
            storage_kind = state["credential_storage"]["kind"]
            if storage_kind != "system":
                raise RuntimeError(
                    f"installed build must use the system credential store, got {storage_kind}"
                )
            status, synced = _http_json("POST", f"{base}/api/settings/system-timezone", {
                "timezone": "Asia/Shanghai",
            })
            if status != 200:
                raise RuntimeError(f"timezone sync failed: {status} {synced}")
            status, state = _http_json("GET", f"{base}/api/settings")
            if status != 200:
                raise RuntimeError(f"settings reread failed: {status} {state}")
            # llm_model forces the config-file staging path (fsync durability) while
            # keys exercises the OS credential store; together they cover both halves
            # of a real save.
            status, saved = _http_json("PUT", f"{base}/api/settings", {
                "revision": state["revision"],
                "llm_model": "deepseek",
                "keys": {"DEEPSEEK_API_KEY": "smoke-credential-value"},
            })
            if status != 200 or saved.get("keys", {}).get("DEEPSEEK_API_KEY") is not True:
                raise RuntimeError(f"key save failed: {status} {saved}")
            status, reread = _http_json("GET", f"{base}/api/settings")
            if status != 200 or reread.get("keys", {}).get("DEEPSEEK_API_KEY") is not True:
                raise RuntimeError(f"key did not persist: {status} {reread}")
        except BaseException as error:
            server_tail = _tail(server_log)
            launcher_tail = _tail(root / 'keys' / 'runtime-logs' / 'careerdesk.log')
            raise RuntimeError(
                "system credential save smoke failed: "
                f"{error}{chr(10)}--- server output ---{chr(10)}{server_tail}"
                f"{chr(10)}--- launcher log ---{chr(10)}{launcher_tail}"
            ) from error
        finally:
            if process.poll() is None:
                process.terminate()
                try:
                    process.wait(timeout=10)
                except subprocess.TimeoutExpired:
                    process.kill()
                    process.wait(timeout=10)
    print("frozen system credential save smoke passed")


def _check_desktop_shortcut_helper(artifact_dir: Path) -> None:
    """Run the shipped shortcut helper on real Windows and verify the result."""
    helper = artifact_dir / "Add-Desktop-Shortcut.cmd"
    if not helper.is_file():
        raise RuntimeError(f"shortcut helper missing from the artifact: {helper}")
    completed = subprocess.run(
        ["cmd", "/c", str(helper)],
        capture_output=True, text=True, encoding="utf-8", errors="replace",
        timeout=60, stdin=subprocess.DEVNULL, check=False,
    )
    resolved = subprocess.run(
        ["powershell", "-NoProfile", "-Command",
         "[Environment]::GetFolderPath('Desktop')"],
        capture_output=True, text=True, timeout=30, check=True,
    )
    shortcut = Path(resolved.stdout.strip()) / "CareerDesk.lnk"
    if completed.returncode != 0 or not shortcut.is_file():
        raise RuntimeError(
            "desktop shortcut helper failed: "
            f"rc={completed.returncode} shortcut_exists={shortcut.is_file()}\n"
            f"{completed.stdout}\n{completed.stderr}"
        )
    shortcut.unlink()
    print("frozen desktop shortcut helper smoke passed")


def _check_native_window_after_download_taint(desktop_executable: Path, root: Path) -> None:
    """The native window must start even after Explorer's zip extraction.

    Explorer tags extracted files with mark-of-the-web and the .NET Framework
    then refuses those assemblies. Reproduce that exact user state, then require
    the launcher to self-heal and open the window stack.
    """
    internal = desktop_executable.parent / "_internal"
    tainted = 0
    for subdirectory in ("pythonnet", "clr_loader"):
        directory = internal / subdirectory
        if not directory.is_dir():
            continue
        for assembly in directory.rglob("*.dll"):
            with open(f"{assembly}:Zone.Identifier", "w", encoding="ascii") as stream:
                stream.write("[ZoneTransfer]\r\nZoneId=3\r\n")
            tainted += 1
    if tainted == 0:
        raise RuntimeError("no bundled .NET assemblies found to taint; layout changed?")
    environment = _isolated_environment(root / "window")
    environment.pop("NO_WINDOW", None)
    environment.pop("CAREERDESK_HEADLESS", None)
    environment["CAREERDESK_WINDOW_SMOKE"] = "1"
    try:
        completed = subprocess.run(
            [str(desktop_executable)], env=environment,
            capture_output=True, text=True, encoding="utf-8", errors="replace",
            timeout=60, check=False,
        )
        output = (completed.stdout or "") + (completed.stderr or "")
    except subprocess.TimeoutExpired as error:
        raise RuntimeError(f"window smoke timed out: {error}") from error
    if "WINDOW_SMOKE_OK" not in output:
        raise RuntimeError(f"native window failed after download taint: {output[-1500:]!r}")
    print(f"frozen native window smoke passed (self-healed {tainted} tainted assemblies)")


def main() -> int:
    parser = argparse.ArgumentParser(description="原生运行冻结桌面与数据维护可执行文件")
    parser.add_argument("--desktop-executable", type=Path, required=True)
    parser.add_argument("--data-executable", type=Path, required=True)
    arguments = parser.parse_args()
    smoke(arguments.desktop_executable, arguments.data_executable)
    print("frozen desktop/data smoke passed")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
