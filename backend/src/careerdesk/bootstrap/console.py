"""Make installed CLI/windowed entrypoints safe across native console locales."""

from __future__ import annotations

import os
import sys
from typing import TextIO


def _null_output() -> TextIO:
    return open(os.devnull, "w", encoding="utf-8", errors="backslashreplace")


def configure_console_streams() -> None:
    """Use UTF-8 when a stream exists and a null sink for windowed executables."""
    for name in ("stdout", "stderr"):
        stream = getattr(sys, name, None)
        if stream is None:
            setattr(sys, name, _null_output())
            continue
        reconfigure = getattr(stream, "reconfigure", None)
        if not callable(reconfigure):
            continue
        try:
            reconfigure(encoding="utf-8", errors="backslashreplace")
        except (OSError, ValueError):
            # Test captures and host-owned streams can reject reconfiguration;
            # leaving them unchanged is safer than replacing a working stream.
            continue
