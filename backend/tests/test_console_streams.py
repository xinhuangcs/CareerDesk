"""Native entrypoints must not crash on CP1252 or windowed executables."""

from __future__ import annotations

import io
import sys

from careerdesk.bootstrap.console import configure_console_streams


class _StrictTextStream:
    def __init__(self) -> None:
        self.encoding = "cp1252"
        self.errors = "strict"
        self.buffer = io.BytesIO()

    def reconfigure(self, *, encoding: str, errors: str) -> None:
        self.encoding = encoding
        self.errors = errors

    def write(self, value: str) -> int:
        self.buffer.write(value.encode(self.encoding, self.errors))
        return len(value)

    def flush(self) -> None:
        return None


def test_console_streams_switch_redirected_cp1252_to_utf8(monkeypatch):
    stdout = _StrictTextStream()
    stderr = _StrictTextStream()
    monkeypatch.setattr(sys, "stdout", stdout)
    monkeypatch.setattr(sys, "stderr", stderr)

    configure_console_streams()
    print("备份已完成并验证", file=sys.stdout)
    print("恢复失败", file=sys.stderr)

    assert stdout.buffer.getvalue().decode("utf-8") == "备份已完成并验证\n"
    assert stderr.buffer.getvalue().decode("utf-8") == "恢复失败\n"


def test_console_streams_supply_null_sinks_for_windowed_executable(monkeypatch):
    monkeypatch.setattr(sys, "stdout", None)
    monkeypatch.setattr(sys, "stderr", None)

    configure_console_streams()
    stdout = sys.stdout
    stderr = sys.stderr
    try:
        assert stdout is not None and stderr is not None
        print("windowed startup remains safe")
        print("windowed diagnostic remains safe", file=sys.stderr)
    finally:
        stdout.close()
        stderr.close()
