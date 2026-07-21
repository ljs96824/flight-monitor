"""Crash-proof diagnostic logging helpers."""

from __future__ import annotations

import atexit
from pathlib import Path
import sys
import threading


class _Utf8TeeStream:
    def __init__(self, console, log_file, lock: threading.RLock):
        self._console = console
        self._log_file = log_file
        self._lock = lock
        self.encoding = getattr(console, "encoding", "utf-8")
        self.errors = getattr(console, "errors", "replace")

    def write(self, value) -> int:
        text = str(value)
        with self._lock:
            if self._console is not None:
                self._console.write(text)
            if not self._log_file.closed:
                self._log_file.write(text)
                self._log_file.flush()
        return len(text)

    def flush(self) -> None:
        with self._lock:
            if self._console is not None:
                self._console.flush()
            if not self._log_file.closed:
                self._log_file.flush()

    def isatty(self) -> bool:
        return bool(self._console and self._console.isatty())

    def fileno(self):
        return self._console.fileno()

    def __getattr__(self, name):
        return getattr(self._console, name)


_run_log_state: dict | None = None


def configure_stdio_utf8() -> None:
    """Best-effort UTF-8 stdio setup for Windows console entrypoints."""
    for stream in (getattr(sys, "stdout", None), getattr(sys, "stderr", None)):
        if not hasattr(stream, "reconfigure"):
            continue
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")
        except Exception:
            pass


def _close_run_log() -> None:
    global _run_log_state
    state = _run_log_state
    if not state:
        return
    try:
        state["file"].flush()
        state["file"].close()
    except Exception:
        pass
    _run_log_state = None


def configure_run_logging(log_path: str | Path) -> Path:
    """Mirror stdout/stderr to one UTF-8 log for the current web process.

    run_latest.log represents only the current process, so it is truncated once
    at startup. The Python entrypoint owns this tee; callers must not redirect
    the same path again with PowerShell ``*>>``.
    """
    global _run_log_state
    configure_stdio_utf8()
    path = Path(log_path).resolve()
    if _run_log_state and _run_log_state.get("path") == path:
        return path
    if _run_log_state:
        _close_run_log()
    path.parent.mkdir(parents=True, exist_ok=True)
    log_file = path.open("w", encoding="utf-8", errors="strict", newline="", buffering=1)
    lock = threading.RLock()
    original_stdout = sys.stdout
    original_stderr = sys.stderr
    sys.stdout = _Utf8TeeStream(original_stdout, log_file, lock)
    sys.stderr = _Utf8TeeStream(original_stderr, log_file, lock)
    _run_log_state = {
        "path": path,
        "file": log_file,
        "stdout": original_stdout,
        "stderr": original_stderr,
    }
    return path


atexit.register(_close_run_log)


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
