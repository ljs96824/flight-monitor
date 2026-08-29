"""Crash-proof diagnostic logging helpers."""

from __future__ import annotations

import atexit
from datetime import datetime
import hashlib
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
        text = redact_text(value)
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
    "client_secret",
    "duffel_token",
    "flask_secret_key",
    "key",
    "password",
    "pushplus_token",
    "refresh_token",
    "serp_api_key",
    "serpapi_api_key",
    "serpapi_key",
    "secret",
    "shared_detail_token",
    "token",
}
_EMAIL_KEYS = {
    "author_email",
    "contact_email",
    "email",
    "notification_email",
    "recipient",
    "recipient_email",
}
_PHONE_KEYS = {
    "contact_mobile",
    "contact_phone",
    "mobile",
    "mobile_number",
    "phone",
    "phone_number",
    "recipient_mobile",
    "recipient_phone",
    "tel",
    "telephone",
}
EMAIL_PATTERN = re.compile(
    r"(?i)(?<![A-Z0-9._%+-])"
    r"[A-Z0-9._%+-]+@[A-Z0-9.-]+\.[A-Z]{2,}"
    r"(?![A-Z0-9._%+-])"
)
_SECRET_ASSIGNMENT_PATTERN = re.compile(
    r"(?i)(?<![A-Z0-9_-])"
    r"(api[_-]?key|access[_-]?token|token|authorization|password|secret|key)"
    r"=([^&\s]+)"
)
_AUTHORIZATION_BEARER_PATTERN = re.compile(
    r"(?i)(authorization\s*:\s*bearer\s+)([^\s,;]+)"
)
_QUOTED_SECRET_PATTERN = re.compile(
    r"(?i)([\"'](?:api[_-]?key|access[_-]?token|refresh[_-]?token|"
    r"pushplus[_-]?token|token|authorization|password|secret|key)[\"']"
    r"\s*:\s*[\"'])(.*?)([\"'])"
)
_CAMEL_ACRONYM_PATTERN = re.compile(r"([A-Z]+)([A-Z][a-z])")
_CAMEL_BOUNDARY_PATTERN = re.compile(r"([a-z0-9])([A-Z])")
_NON_KEY_CHARACTER_PATTERN = re.compile(r"[^A-Za-z0-9]+")
_CYCLE_MARKER = "<CYCLE>"
_MAX_DEPTH_MARKER = "<MAX_DEPTH>"
DEFAULT_REDACTION_MAX_DEPTH = 12


def _normalize_sensitive_key(key: object) -> str:
    if not isinstance(key, str):
        return ""
    normalized = _CAMEL_ACRONYM_PATTERN.sub(r"\1_\2", key.strip())
    normalized = _CAMEL_BOUNDARY_PATTERN.sub(r"\1_\2", normalized)
    normalized = _NON_KEY_CHARACTER_PATTERN.sub("_", normalized)
    return normalized.strip("_").lower()


def _sensitive_key_kind(key: object) -> str:
    normalized = _normalize_sensitive_key(key)
    if (
        normalized in _SECRET_KEYS
        or normalized.endswith("_token")
        or normalized.endswith("_password")
        or normalized.endswith("_secret")
    ):
        return "credential"
    if normalized in _EMAIL_KEYS or normalized.endswith("_email"):
        return "email"
    if (
        normalized in _PHONE_KEYS
        or normalized.endswith("_phone")
        or normalized.endswith("_mobile")
        or normalized.endswith("_phone_number")
        or normalized.endswith("_mobile_number")
    ):
        return "phone"
    return ""


def redact_text(value: object) -> str:
    """脱敏可能进入控制台、运行日志或轮档的自由文本。"""
    if isinstance(value, str):
        text = value
    elif value is None or isinstance(value, (bool, int, float)):
        text = str(value)
    else:
        return f"<OBJECT:{type(value).__name__}>"
    text = _SECRET_ASSIGNMENT_PATTERN.sub(r"\1=***", text)
    text = _AUTHORIZATION_BEARER_PATTERN.sub(r"\1***", text)
    text = _QUOTED_SECRET_PATTERN.sub(r"\1***\3", text)
    return EMAIL_PATTERN.sub("<EMAIL>", text)


def _redact_value(value, *, depth: int, max_depth: int, active_ids: set[int]):
    if depth > max_depth:
        return _MAX_DEPTH_MARKER
    if isinstance(value, dict):
        identity = id(value)
        if identity in active_ids:
            return _CYCLE_MARKER
        active_ids.add(identity)
        try:
            redacted = {}
            for key, item in value.items():
                kind = _sensitive_key_kind(key)
                if kind == "credential":
                    redacted[key] = "***"
                elif kind == "email":
                    redacted[key] = "<EMAIL>" if item else item
                elif kind == "phone":
                    redacted[key] = "<PHONE>" if item else item
                else:
                    redacted[key] = _redact_value(
                        item,
                        depth=depth + 1,
                        max_depth=max_depth,
                        active_ids=active_ids,
                    )
            return redacted
        finally:
            active_ids.remove(identity)
    if isinstance(value, (list, tuple)):
        identity = id(value)
        if identity in active_ids:
            return _CYCLE_MARKER
        active_ids.add(identity)
        try:
            redacted_items = [
                _redact_value(
                    item,
                    depth=depth + 1,
                    max_depth=max_depth,
                    active_ids=active_ids,
                )
                for item in value
            ]
            return tuple(redacted_items) if isinstance(value, tuple) else redacted_items
        finally:
            active_ids.remove(identity)
    if isinstance(value, str):
        return redact_text(value)
    if value is None or isinstance(value, (bool, int, float)):
        return value
    return f"<OBJECT:{type(value).__name__}>"


def redact_value(value, *, max_depth: int = DEFAULT_REDACTION_MAX_DEPTH):
    """递归脱敏结构化证据，不读取未知对象的字符串表示。"""
    if max_depth < 0:
        raise ValueError("max_depth must be non-negative")
    return _redact_value(value, depth=0, max_depth=max_depth, active_ids=set())


def _json_safe_value(value):
    if isinstance(value, dict):
        safe = {}
        for key, item in value.items():
            if isinstance(key, str):
                safe_key = key
            elif key is None or isinstance(key, (bool, int, float)):
                safe_key = str(key)
            else:
                safe_key = f"<OBJECT:{type(key).__name__}>"
            safe[safe_key] = _json_safe_value(item)
        return safe
    if isinstance(value, (list, tuple)):
        return [_json_safe_value(item) for item in value]
    if value is None or isinstance(value, (str, bool, int, float)):
        return value
    return f"<OBJECT:{type(value).__name__}>"


def render_redacted_json(payload, max_chars: int = 4096) -> str:
    """Render deterministic one-line JSON after structured redaction."""
    if max_chars < 0:
        raise ValueError("max_chars must be non-negative")
    redacted = _json_safe_value(redact_value(payload))
    encoded = json.dumps(
        redacted,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )
    if len(encoded) <= max_chars:
        return encoded
    metadata = {
        "chars": len(encoded),
        "redacted_sha256": hashlib.sha256(encoded.encode("utf-8")).hexdigest(),
        "truncated": True,
    }
    return json.dumps(
        metadata,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )


def _redact_round_evidence(value):
    return redact_value(value)


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
        _json_safe_value(_redact_round_evidence(payload)),
        ensure_ascii=False,
        sort_keys=True,
    )
    with state["lock"]:
        state["file"].write(f"{redact_text(prefix)}{encoded}\\n")
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
    """脱敏诊断并避免控制台编码错误中止轮次。"""
    text = redact_text(msg)
    try:
        print(text)
    except UnicodeEncodeError:
        try:
            degraded = text.encode("ascii", "backslashreplace").decode("ascii")
            print(degraded)
        except Exception:
            pass


def safe_log_json(label: object, payload, max_chars: int = 4096) -> None:
    """Write one deterministic redacted JSON diagnostic through ``safe_log``."""
    safe_log(f"{redact_text(label)}{render_redacted_json(payload, max_chars=max_chars)}")
