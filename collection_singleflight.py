"""跨线程、跨进程的采集轮单飞锁。"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import socket
import threading
from typing import BinaryIO, Mapping
from uuid import uuid4

from dotenv import load_dotenv

from api_usage import _LOCK_BACKEND
from log_utils import safe_log


BASE_DIR = Path(__file__).resolve().parent


def _primary_worktree_root(base_dir: str | Path) -> Path:
    base = Path(base_dir).resolve()
    git_marker = base / ".git"
    if not git_marker.is_file():
        return base
    try:
        marker = git_marker.read_text(encoding="utf-8").strip()
    except OSError:
        return base
    prefix = "gitdir:"
    if not marker.lower().startswith(prefix):
        return base
    git_dir = Path(marker[len(prefix) :].strip())
    if not git_dir.is_absolute():
        git_dir = (base / git_dir).resolve()
    if git_dir.parent.name == "worktrees" and len(git_dir.parents) >= 3:
        return git_dir.parents[2]
    return base


def resolve_collection_lock_path(
    *,
    base_dir: str | Path = BASE_DIR,
    environ: Mapping[str, str] | None = None,
) -> Path:
    """解析同机共享锁路径；linked worktree默认回到主工作区。"""

    primary_root = _primary_worktree_root(base_dir)
    if environ is None:
        load_dotenv(primary_root / ".env", encoding="utf-8")
        values = os.environ
    else:
        values = environ
    configured = str(values.get("COLLECTION_LOCK_PATH") or "").strip()
    if configured:
        path = Path(configured).expanduser()
        if not path.is_absolute():
            path = primary_root / path
        return path.resolve()
    return (primary_root / "data" / "collection_singleflight.lock").resolve()


DEFAULT_LOCK_PATH = resolve_collection_lock_path()
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
    lease_id: str = ""
    hostname: str = ""
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
    _lease_mismatch_logged: bool = field(default=False, repr=False)

    def _metadata(self) -> dict:
        return {
            "pid": self.pid,
            "round_id": self.round_id,
            "lease_id": self.lease_id,
            "hostname": self.hostname,
            "state": "running",
            "heartbeat_at": _now().isoformat(),
        }

    def _metadata_lease_matches(self, lock_file: BinaryIO) -> bool:
        current = _read_holder_stream(lock_file)
        return bool(self.lease_id) and current.get("lease_id") == self.lease_id

    def _log_lease_mismatch(self, action: str) -> None:
        if self._lease_mismatch_logged:
            return
        self._lease_mismatch_logged = True
        safe_log(
            f"[采集] 单飞锁租约不匹配 round={self.round_id} "
            f"lease={self.lease_id} action={action},放弃元数据写入"
        )

    def heartbeat(self) -> bool:
        if not self.acquired or self._released or self._lock_file is None:
            return False
        with self._metadata_guard:
            if not self._metadata_lease_matches(self._lock_file):
                self._heartbeat_stop.set()
                self._log_lease_mismatch("heartbeat")
                return False
            _write_holder(self._lock_file, self._metadata())
        return True

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
        self._heartbeat_stop.set()
        self._released = True
        if self._heartbeat_thread is not None:
            self._heartbeat_thread.join()
        try:
            if self._lock_file is not None:
                lock_file = self._lock_file
                self._lock_file = None
                try:
                    with self._metadata_guard:
                        if self._metadata_lease_matches(lock_file):
                            released_metadata = self._metadata()
                            released_metadata["state"] = "released"
                            released_metadata["released_at"] = released_metadata[
                                "heartbeat_at"
                            ]
                            _write_holder(lock_file, released_metadata)
                        else:
                            self._log_lease_mismatch("release")
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


def collection_busy_status(
    gate: CollectionSingleflightGate,
    *,
    entrypoint: str,
) -> dict:
    """记录并返回不与成功/失败混用的busy状态。"""

    holder = gate.holder or {}
    status = {
        "status": "busy",
        "holder_pid": holder.get("pid"),
        "holder_round_id": holder.get("round_id"),
        "holder_heartbeat_at": holder.get("heartbeat_at"),
        "entrypoint": str(entrypoint),
    }
    safe_log(
        "[采集状态] status=busy "
        f"holder_pid={status['holder_pid']} "
        f"holder_round_id={status['holder_round_id']} "
        f"holder_heartbeat_at={status['holder_heartbeat_at']} "
        f"entrypoint={status['entrypoint']}"
    )
    return status


def acquire_collection_singleflight(
    round_id: str,
    *,
    lock_path: str | Path | None = None,
    stale_after_seconds: float = DEFAULT_STALE_AFTER_SECONDS,
    heartbeat_interval_seconds: float = DEFAULT_HEARTBEAT_INTERVAL_SECONDS,
) -> CollectionSingleflightGate:
    """非阻塞获取采集轮锁；busy时返回未获取结果，不等待。"""

    path = (
        Path(lock_path).resolve() if lock_path is not None else resolve_collection_lock_path()
    )
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
            lease_id=str(uuid4()),
            hostname=socket.gethostname(),
            holder=previous_holder,
            _lock_file=lock_file,
            _thread_lock=thread_lock,
            _heartbeat_interval_seconds=max(
                0.0,
                float(heartbeat_interval_seconds),
            ),
        )
        _write_holder(lock_file, gate._metadata())
        gate.start_heartbeat()
        return gate
    except Exception:
        if lock_file is not None:
            lock_file.close()
        thread_lock.release()
        raise
