"""带跨进程锁和原子替换的本地 JSON 存储。"""

from __future__ import annotations

from copy import deepcopy
import json
import os
from pathlib import Path
from typing import Callable, TypeVar
from uuid import uuid4

from local_file_lock import file_lock
from log_utils import safe_log


JsonValue = TypeVar("JsonValue")


class JsonStoreReadError(RuntimeError):
    """JSON 文件无法完整、可信地读取。"""


def read_json(path: str | Path):
    """读取 JSON；任何读取或解析失败都会显式抛出。"""

    target = Path(path)
    try:
        with target.open("r", encoding="utf-8") as stream:
            return json.load(stream)
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        safe_log(
            f"[JSON存储] 读取失败 path={target} "
            f"原因={type(exc).__name__}:{exc}"
        )
        raise JsonStoreReadError(
            f"JSON读取失败: {target} ({type(exc).__name__}: {exc})"
        ) from exc


def _write_json_atomic(path: Path, payload) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f"{path.name}.{uuid4().hex}.tmp")
    try:
        with temporary.open("w", encoding="utf-8", newline="") as stream:
            json.dump(payload, stream, ensure_ascii=False, indent=2)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    finally:
        try:
            temporary.unlink(missing_ok=True)
        except OSError:
            pass


def update_json(path: str | Path, mutator: Callable[[object], JsonValue]) -> JsonValue:
    """在同一文件锁内完成完整read-modify-write。

    文件尚不存在时，mutator 接收 None；已存在但无法读取时会抛错，绝不把
    损坏文件伪装成空结构。mutator 必须返回要持久化的完整 JSON 值。
    """

    target = Path(path)
    with file_lock(target):
        exists = target.exists()
        current = read_json(target) if exists else None
        original = deepcopy(current)
        updated = mutator(current)
        if updated is None:
            raise ValueError("JSON mutator 必须返回完整持久化值，不能返回 None")
        if exists and updated == original:
            return updated
        try:
            _write_json_atomic(target, updated)
        except Exception as exc:
            safe_log(
                f"[JSON存储] 写入失败 path={target} "
                f"原因={type(exc).__name__}:{exc}"
            )
            raise
        return updated
