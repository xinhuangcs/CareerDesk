"""Exercise both executables from a frozen desktop artifact on its native OS."""

from __future__ import annotations

import argparse
from contextlib import closing
import os
from pathlib import Path
import re
import socket
import sqlite3
import subprocess
import tempfile
import time
from urllib.error import URLError
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
    with tempfile.TemporaryDirectory(prefix="careerdesk-frozen-smoke-") as temporary:
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
