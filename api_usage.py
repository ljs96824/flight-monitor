"""本地 API 配额用量台账。仅记录真实 HTTP 请求次数。

任务执行未经用户明示授权不得发起真实外部API调用;获授权时须报告执行前后台账值。
"""

from __future__ import annotations

from contextlib import contextmanager
import json
import os
import threading
import time
import uuid
from datetime import date, datetime
from pathlib import Path

import msvcrt

from log_utils import safe_log


DEFAULT_USAGE_PATH = Path(__file__).parent / "data" / "api_usage.json"
DEFAULT_LOCK_TIMEOUT_SECONDS = 3.0
DEFAULT_LOCK_RETRIES = 1


class UsageLockTimeout(TimeoutError):
    """等待 API 台账文件锁超时。"""


_thread_locks: dict[str, threading.Lock] = {}
_thread_locks_guard = threading.Lock()


def _thread_lock_for(path: Path) -> threading.Lock:
    key = str(path.resolve())
    with _thread_locks_guard:
        return _thread_locks.setdefault(key, threading.Lock())


def _try_lock_file(lock_file) -> bool:
    lock_file.seek(0)
    try:
        msvcrt.locking(lock_file.fileno(), msvcrt.LK_NBLCK, 1)
    except OSError:
        return False
    return True


@contextmanager
def _usage_lock(
    usage_path: str | Path,
    *,
    timeout: float = DEFAULT_LOCK_TIMEOUT_SECONDS,
):
    """用进程内锁和 Windows 字节锁覆盖完整读改写临界区。"""

    path = Path(usage_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    lock_path = path.with_name(f"{path.name}.lock")
    thread_lock = _thread_lock_for(path)
    wait_seconds = max(0.0, float(timeout))
    deadline = time.monotonic() + wait_seconds
    if not thread_lock.acquire(timeout=wait_seconds):
        raise UsageLockTimeout(f"等待进程内锁超时: {lock_path}")

    lock_file = None
    os_locked = False
    try:
        lock_file = lock_path.open("a+b")
        lock_file.seek(0, os.SEEK_END)
        if lock_file.tell() == 0:
            lock_file.write(b"\0")
            lock_file.flush()
            os.fsync(lock_file.fileno())

        while True:
            if _try_lock_file(lock_file):
                os_locked = True
                break
            if time.monotonic() >= deadline:
                raise UsageLockTimeout(f"等待文件锁超时: {lock_path}")
            time.sleep(min(0.01, max(0.0, deadline - time.monotonic())))
        yield
    finally:
        if lock_file is not None:
            if os_locked:
                try:
                    lock_file.seek(0)
                    msvcrt.locking(lock_file.fileno(), msvcrt.LK_UNLCK, 1)
                except OSError:
                    pass
            lock_file.close()
        thread_lock.release()


def _atomic_write_json(path: Path, payload: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temp_path = path.with_name(f"{path.name}.{uuid.uuid4().hex}.tmp")
    try:
        with temp_path.open("w", encoding="utf-8", newline="") as stream:
            json.dump(payload, stream, ensure_ascii=False, indent=2, sort_keys=True)
            stream.write("\n")
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temp_path, path)
    finally:
        try:
            temp_path.unlink(missing_ok=True)
        except OSError:
            pass


def _append_conflict_audit(
    *,
    usage_path: Path,
    conflict_log_path: str | Path | None,
    round_id: str | None,
    reason: str,
) -> None:
    audit_path = Path(conflict_log_path or usage_path.parent / "api_usage_conflict.log")
    row = {
        "recorded_at": datetime.now().astimezone().isoformat(timespec="seconds"),
        "round_id": str(round_id or "unknown"),
        "status": "write_conflict",
        "usage_path": str(usage_path),
        "reason": str(reason),
    }
    try:
        audit_path.parent.mkdir(parents=True, exist_ok=True)
        encoded = (json.dumps(row, ensure_ascii=False, sort_keys=True) + "\n").encode("utf-8")
        descriptor = os.open(
            audit_path,
            os.O_APPEND | os.O_CREAT | os.O_WRONLY | getattr(os, "O_BINARY", 0),
        )
        try:
            os.write(descriptor, encoded)
            os.fsync(descriptor)
        finally:
            os.close(descriptor)
    except OSError as exc:
        safe_log(
            f"[配额台账] 冲突审计失败 round={round_id or 'unknown'} 原因={exc}"
        )


def load_usage(path: str | Path = DEFAULT_USAGE_PATH) -> dict:
    usage_path = Path(path)
    if not usage_path.exists():
        return {"version": 2, "dates": {}, "entries": []}
    try:
        payload = json.loads(usage_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {"version": 2, "dates": {}, "entries": []}
    if not isinstance(payload, dict):
        return {"version": 2, "dates": {}, "entries": []}
    dates = payload.get("dates")
    if not isinstance(dates, dict):
        dates = {}
    entries = payload.get("entries")
    if not isinstance(entries, list):
        entries = []
    return {"version": 2, "dates": dates, "entries": entries}


def record_actual_requests(
    counts: dict[str, int],
    *,
    path: str | Path = DEFAULT_USAGE_PATH,
    day: str | None = None,
    round_id: str | None = None,
    recorded_at: str | None = None,
    lock_timeout: float = DEFAULT_LOCK_TIMEOUT_SECONDS,
    lock_retries: int = DEFAULT_LOCK_RETRIES,
    conflict_log_path: str | Path | None = None,
) -> dict:
    usage_path = Path(path)
    attempts = max(0, int(lock_retries)) + 1
    last_error = None
    for _attempt in range(attempts):
        try:
            with _usage_lock(usage_path, timeout=lock_timeout):
                payload = load_usage(usage_path)
                day_key = str(day or date.today().isoformat())
                day_counts = payload["dates"].setdefault(day_key, {})
                actual_counts = {}
                for source, raw_count in (counts or {}).items():
                    count = max(0, int(raw_count or 0))
                    if count:
                        source_name = str(source)
                        actual_counts[source_name] = count
                        day_counts[source_name] = (
                            int(day_counts.get(source_name, 0) or 0) + count
                        )

                if actual_counts:
                    payload["entries"].append(
                        {
                            "recorded_at": str(
                                recorded_at
                                or datetime.now().astimezone().isoformat(
                                    timespec="seconds"
                                )
                            ),
                            "round_id": str(round_id or "unknown"),
                            "day": day_key,
                            "counts": actual_counts,
                        }
                    )

                _atomic_write_json(usage_path, payload)
                return payload
        except UsageLockTimeout as exc:
            last_error = exc

    safe_log(
        f"[配额台账] 写入冲突 round={round_id or 'unknown'} "
        f"重试={max(0, int(lock_retries))} 原因={last_error}，已放弃本笔"
    )
    _append_conflict_audit(
        usage_path=usage_path,
        conflict_log_path=conflict_log_path,
        round_id=round_id,
        reason=str(last_error or "等待锁超时"),
    )
    return load_usage(usage_path)


def usage_snapshot(payload: dict, *, day: str | None = None) -> dict:
    day_key = str(day or date.today().isoformat())
    dates = (payload or {}).get("dates") or {}
    today_counts = {
        str(source): int(count or 0)
        for source, count in (dates.get(day_key) or {}).items()
    }
    cumulative: dict[str, int] = {}
    for source_counts in dates.values():
        if not isinstance(source_counts, dict):
            continue
        for source, count in source_counts.items():
            cumulative[str(source)] = cumulative.get(str(source), 0) + int(count or 0)
    return {"today": today_counts, "cumulative": cumulative}
