"""Crash-proof diagnostic logging helpers."""

from __future__ import annotations

import atexit
from datetime import datetime
import json
from pathlib import Path
import re
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

_round_log_state: dict | None = None
_SECRET_KEYS = {
    "api_key",
    "apikey",
    "access_token",
    "authorization",
    "key",
    "password",
    "secret",
    "token",
}


def _redact_round_evidence(value):
    if isinstance(value, dict):
        redacted = {}
        for key, item in value.items():
            normalized = str(key).strip().lower().replace("-", "_")
            redacted[key] = "***" if normalized in _SECRET_KEYS else _redact_round_evidence(item)
        return redacted
    if isinstance(value, list):
        return [_redact_round_evidence(item) for item in value]
    if isinstance(value, tuple):
        return tuple(_redact_round_evidence(item) for item in value)
    if isinstance(value, str):
        return re.sub(
            r"(?i)(api[_-]?key|access[_-]?token|token|authorization|password|secret|key)=([^&\s]+)",
            r"\1=***",
            value,
        )
    return value


def start_round_log_archive(
    round_id: str,
    *,
    root_dir: str | Path | None = None,
    now: datetime | None = None,
) -> Path:
    """Start one append-only UTF-8 archive segment for a collection round."""
    global _round_log_state
    if _round_log_state:
        end_round_log_archive(status="interrupted")
    stamp = now or datetime.now()
    root = Path(root_dir) if root_dir is not None else Path(__file__).resolve().parent / "data" / "logs" / "rounds"
    root.mkdir(parents=True, exist_ok=True)
    path = root / f"{stamp:%Y%m%d}.log"
    archive_file = path.open("a", encoding="utf-8", errors="strict", newline="", buffering=1)
    lock = threading.RLock()
    original_stdout = sys.stdout
    original_stderr = sys.stderr
    sys.stdout = _Utf8TeeStream(original_stdout, archive_file, lock)
    sys.stderr = _Utf8TeeStream(original_stderr, archive_file, lock)
    _round_log_state = {
        "path": path,
        "file": archive_file,
        "stdout": original_stdout,
        "stderr": original_stderr,
        "lock": lock,
        "round_id": str(round_id or "unknown"),
        "started_at": stamp.isoformat(timespec="seconds"),
    }
    safe_log(
        f"===== [轮档开始] round_id={_round_log_state['round_id']} "
        f"started_at={_round_log_state['started_at']} ====="
    )
    return path


def append_round_evidence(prefix: str, payload) -> bool:
    """Append sanitized source evidence to the active round archive only."""
    state = _round_log_state
    if not state:
        return False
    encoded = json.dumps(
        _redact_round_evidence(payload),
        ensure_ascii=False,
        sort_keys=True,
        default=str,
    )
    with state["lock"]:
        state["file"].write(f"{prefix}{encoded}\\n")
        state["file"].flush()
    return True


def end_round_log_archive(*, status: str = "completed") -> None:
    """Close the active round segment without affecting run_latest.log."""
    global _round_log_state
    state = _round_log_state
    if not state:
        return
    try:
        safe_log(
            f"===== [轮档结束] round_id={state['round_id']} status={status} ====="
        )
    finally:
        sys.stdout = state["stdout"]
        sys.stderr = state["stderr"]
        try:
            state["file"].flush()
            state["file"].close()
        except Exception:
            pass
        _round_log_state = None




atexit.register(_close_run_log)
atexit.register(end_round_log_archive)


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
