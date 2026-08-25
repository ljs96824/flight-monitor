"""跨线程、跨进程的采集轮单飞锁。"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import threading
from typing import BinaryIO

from api_usage import _LOCK_BACKEND
from log_utils import safe_log


BASE_DIR = Path(__file__).resolve().parent
DEFAULT_LOCK_PATH = BASE_DIR / "data" / "collection_singleflight.lock"
DEFAULT_STALE_AFTER_SECONDS = 30 * 60
DEFAULT_HEARTBEAT_INTERVAL_SECONDS = 60.0

_thread_locks: dict[str, threading.Lock] = {}
_thread_locks_guard = threading.Lock()


def _thread_lock_for(path: Path) -> threading.Lock:
    key = str(path.resolve())
    with _thread_locks_guard:
        return _thread_locks.setdefault(key, threading.Lock())


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _parse_timestamp(value) -> datetime | None:
    try:
        parsed = datetime.fromisoformat(str(value or "").replace("Z", "+00:00"))
    except (TypeError, ValueError):
        return None
    if parsed.tzinfo is None:
        return parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def _read_holder_stream(lock_file: BinaryIO) -> dict:
    try:
        lock_file.seek(1)
        raw = lock_file.read().strip()
        try:
            payload = json.loads(raw.decode("utf-8")) if raw else {}
        except (UnicodeDecodeError, json.JSONDecodeError):
            lock_file.seek(0)
            raw = lock_file.read().strip()
            payload = json.loads(raw.decode("utf-8")) if raw else {}
    except (OSError, UnicodeDecodeError, json.JSONDecodeError):
        return {}
    return payload if isinstance(payload, dict) else {}


def _read_holder_path(lock_path: Path) -> dict:
    try:
        with lock_path.open("rb", buffering=0) as stream:
            return _read_holder_stream(stream)
    except OSError:
        return {}


def _write_holder(lock_file: BinaryIO, payload: dict) -> None:
    encoded = json.dumps(
        payload,
        ensure_ascii=True,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("ascii")
    lock_file.seek(1)
    lock_file.write(encoded)
    lock_file.truncate()
    lock_file.flush()
    os.fsync(lock_file.fileno())


def _holder_label(holder: dict) -> str:
    return f"{holder.get('pid', 'unknown')}/{holder.get('round_id', 'unknown')}"


def _holder_age_seconds(holder: dict, now: datetime) -> float | None:
    heartbeat = _parse_timestamp(holder.get("heartbeat_at"))
    if heartbeat is None:
        return None
    return max(0.0, (now - heartbeat).total_seconds())


@dataclass
class CollectionSingleflightGate:
    acquired: bool
    round_id: str
    lock_path: Path
    pid: int = field(default_factory=os.getpid)
    holder: dict = field(default_factory=dict)
    _lock_file: BinaryIO | None = field(default=None, repr=False)
    _thread_lock: threading.Lock | None = field(default=None, repr=False)
    _heartbeat_interval_seconds: float = field(default=0.0, repr=False)
    _heartbeat_stop: threading.Event = field(
        default_factory=threading.Event,
        repr=False,
    )
    _heartbeat_thread: threading.Thread | None = field(default=None, repr=False)
    _metadata_guard: threading.Lock = field(
        default_factory=threading.Lock,
        repr=False,
    )
    _released: bool = field(default=False, repr=False)

    def _metadata(self) -> dict:
        return {
            "pid": self.pid,
            "round_id": self.round_id,
            "state": "running",
            "heartbeat_at": _now().isoformat(),
        }

    def heartbeat(self) -> None:
        if not self.acquired or self._released or self._lock_file is None:
            return
        with self._metadata_guard:
            _write_holder(self._lock_file, self._metadata())

    def _heartbeat_loop(self) -> None:
        interval = max(0.0, float(self._heartbeat_interval_seconds))
        while interval > 0 and not self._heartbeat_stop.wait(interval):
            try:
                self.heartbeat()
            except OSError as exc:
                safe_log(
                    f"[采集] 单飞锁心跳失败 round={self.round_id} "
                    f"原因={type(exc).__name__}:{exc}"
                )

    def start_heartbeat(self) -> None:
        if not self.acquired or self._heartbeat_interval_seconds <= 0:
            return
        self._heartbeat_thread = threading.Thread(
            target=self._heartbeat_loop,
            name=f"collection-heartbeat-{self.round_id}",
            daemon=True,
        )
        self._heartbeat_thread.start()

    def release(self) -> None:
        if self._released:
            return
        self._released = True
        self._heartbeat_stop.set()
        if self._heartbeat_thread is not None:
            self._heartbeat_thread.join(timeout=2)
        try:
            if self._lock_file is not None:
                lock_file = self._lock_file
                self._lock_file = None
                try:
                    with self._metadata_guard:
                        released_metadata = self._metadata()
                        released_metadata["state"] = "released"
                        released_metadata["released_at"] = released_metadata[
                            "heartbeat_at"
                        ]
                        _write_holder(lock_file, released_metadata)
                except OSError as exc:
                    safe_log(
                        f"[采集] 单飞锁释放状态写入失败 round={self.round_id} "
                        f"原因={type(exc).__name__}:{exc}"
                    )
                try:
                    _LOCK_BACKEND.unlock(lock_file)
                except OSError:
                    pass
                lock_file.close()
        finally:
            if self._thread_lock is not None:
                self._thread_lock.release()
                self._thread_lock = None

    def __enter__(self):
        return self

    def __exit__(self, _exc_type, _exc, _traceback):
        self.release()


def _busy_gate(round_id: str, lock_path: Path, holder: dict) -> CollectionSingleflightGate:
    safe_log(
        f"[采集] 已有采集在运行,本次跳过(holder={_holder_label(holder)})"
    )
    return CollectionSingleflightGate(
        acquired=False,
        round_id=str(round_id),
        lock_path=lock_path,
        holder=holder,
    )


def acquire_collection_singleflight(
    round_id: str,
    *,
    lock_path: str | Path | None = None,
    stale_after_seconds: float = DEFAULT_STALE_AFTER_SECONDS,
    heartbeat_interval_seconds: float = DEFAULT_HEARTBEAT_INTERVAL_SECONDS,
) -> CollectionSingleflightGate:
    """非阻塞获取采集轮锁；busy时返回未获取结果，不等待。"""

    path = Path(lock_path or DEFAULT_LOCK_PATH)
    path.parent.mkdir(parents=True, exist_ok=True)
    thread_lock = _thread_lock_for(path)
    if not thread_lock.acquire(blocking=False):
        return _busy_gate(str(round_id), path, _read_holder_path(path))

    lock_file = None
    try:
        path.touch(exist_ok=True)
        lock_file = path.open("r+b", buffering=0)
        lock_file.seek(0, os.SEEK_END)
        if lock_file.tell() == 0:
            lock_file.write(b"\0{}")
            lock_file.flush()
            os.fsync(lock_file.fileno())

        previous_holder = _read_holder_stream(lock_file)
        if not _LOCK_BACKEND.try_lock(lock_file):
            holder = _read_holder_stream(lock_file) or previous_holder
            lock_file.close()
            thread_lock.release()
            return _busy_gate(str(round_id), path, holder)

        now = _now()
        age_seconds = _holder_age_seconds(previous_holder, now)
        if (
            previous_holder
            and previous_holder.get("state") != "released"
            and age_seconds is not None
            and age_seconds > max(0.0, float(stale_after_seconds))
        ):
            safe_log(
                f"[采集] 陈旧锁接管(holder={_holder_label(previous_holder)} "
                f"age={int(age_seconds)}s new_round={round_id})"
            )

        gate = CollectionSingleflightGate(
            acquired=True,
            round_id=str(round_id),
            lock_path=path,
            holder=previous_holder,
            _lock_file=lock_file,
            _thread_lock=thread_lock,
            _heartbeat_interval_seconds=max(
                0.0,
                float(heartbeat_interval_seconds),
            ),
        )
        gate.heartbeat()
        gate.start_heartbeat()
        return gate
    except Exception:
        if lock_file is not None:
            lock_file.close()
        thread_lock.release()
        raise
