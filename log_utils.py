"""Crash-proof diagnostic logging helpers."""

from __future__ import annotations

import sys


def configure_stdio_utf8() -> None:
    """Best-effort UTF-8 stdio setup for Windows console entrypoints."""
    for stream in (getattr(sys, "stdout", None), getattr(sys, "stderr", None)):
        if not hasattr(stream, "reconfigure"):
            continue
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")
        except Exception:
            pass


def safe_log(msg: object = "") -> None:
    """Print diagnostics without allowing console encoding errors to abort a run."""
    try:
        print(msg)
    except UnicodeEncodeError:
        try:
            degraded = str(msg).encode("ascii", "backslashreplace").decode("ascii")
            print(degraded)
        except Exception:
            pass
