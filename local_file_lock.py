"""跨线程、跨进程的本地文件锁公共实现。"""

from __future__ import annotations

from contextlib import contextmanager
import os
from pathlib import Path
import threading
import time

try:
    import msvcrt as _msvcrt
except ImportError:
    _msvcrt = None
    import fcntl as _fcntl
else:
    _fcntl = None


DEFAULT_LOCK_TIMEOUT_SECONDS = 3.0


class FileLockTimeout(TimeoutError):
    """等待本地文件锁超时。"""


class FileLockBackend:
    """把 Windows/POSIX 文件锁收敛到相同的非阻塞接口。"""

    def __init__(self, name, try_lock, unlock):
        self.name = str(name)
        self.try_lock = try_lock
        self.unlock = unlock


def build_lock_backend(*, msvcrt_module=None, fcntl_module=None):
    if msvcrt_module is not None:
        def try_lock(lock_file):
            lock_file.seek(0)
            try:
                msvcrt_module.locking(
                    lock_file.fileno(),
                    msvcrt_module.LK_NBLCK,
                    1,
                )
            except OSError:
                return False
            return True

        def unlock(lock_file):
            lock_file.seek(0)
            msvcrt_module.locking(
                lock_file.fileno(),
                msvcrt_module.LK_UNLCK,
                1,
            )

        return FileLockBackend("windows", try_lock, unlock)

    if fcntl_module is not None:
        def try_lock(lock_file):
            try:
                fcntl_module.flock(
                    lock_file.fileno(),
                    fcntl_module.LOCK_EX | fcntl_module.LOCK_NB,
                )
            except OSError:
                return False
            return True

        def unlock(lock_file):
            fcntl_module.flock(lock_file.fileno(), fcntl_module.LOCK_UN)

        return FileLockBackend("posix", try_lock, unlock)

    raise RuntimeError("当前平台没有可用的文件锁后端")


LOCK_BACKEND = build_lock_backend(
    msvcrt_module=_msvcrt,
    fcntl_module=_fcntl,
)

_thread_locks: dict[str, threading.Lock] = {}
_thread_locks_guard = threading.Lock()


def _thread_lock_for(path: Path) -> threading.Lock:
    key = str(path.resolve())
    with _thread_locks_guard:
        return _thread_locks.setdefault(key, threading.Lock())


@contextmanager
def file_lock(
    target_path: str | Path,
    *,
    timeout: float = DEFAULT_LOCK_TIMEOUT_SECONDS,
    lock_path: str | Path | None = None,
):
    """锁住目标文件的完整本地临界区。"""

    path = Path(target_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    resolved_lock_path = (
        Path(lock_path)
        if lock_path is not None
        else path.with_name(f"{path.name}.lock")
    )
    resolved_lock_path.parent.mkdir(parents=True, exist_ok=True)
    thread_lock = _thread_lock_for(path)
    wait_seconds = max(0.0, float(timeout))
    deadline = time.monotonic() + wait_seconds
    if not thread_lock.acquire(timeout=wait_seconds):
        raise FileLockTimeout(f"等待进程内锁超时: {resolved_lock_path}")

    lock_file = None
    os_locked = False
    try:
        lock_file = resolved_lock_path.open("a+b")
        lock_file.seek(0, os.SEEK_END)
        if lock_file.tell() == 0:
            lock_file.write(b"\0")
            lock_file.flush()
            os.fsync(lock_file.fileno())

        while True:
            if LOCK_BACKEND.try_lock(lock_file):
                os_locked = True
                break
            if time.monotonic() >= deadline:
                raise FileLockTimeout(f"等待文件锁超时: {resolved_lock_path}")
            time.sleep(min(0.01, max(0.0, deadline - time.monotonic())))
        yield
    finally:
        if lock_file is not None:
            if os_locked:
                try:
                    LOCK_BACKEND.unlock(lock_file)
                except OSError:
                    pass
            lock_file.close()
        thread_lock.release()
