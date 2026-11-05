"""带跨进程锁和原子替换的本地 JSON 存储。"""

from __future__ import annotations

from copy import deepcopy
import json
import os
from pathlib import Path
import time
from typing import Callable, TypeVar
from uuid import uuid4

from local_file_lock import file_lock
from log_utils import safe_log


JsonValue = TypeVar("JsonValue")
ATOMIC_REPLACE_RETRIES = 2
ATOMIC_REPLACE_RETRY_DELAY_SECONDS = 0.05


class JsonStoreReadError(RuntimeError):
    """JSON 文件无法完整、可信地读取。"""


# R25：JSON 存储接受的最大嵌套层数(对象与数组各算一层，标量不算)。本项目各 JSON 状态文件实际只有个位数层；
# read_json 交出的与 update_json 写入的内容都不超过这个层数，deepcopy、比较与写回因此不会触发递归上限。
MAX_JSON_NESTING = 100


# R28：参与嵌套计数的容器。元组按 json 序列化写成数组，读回后就是数组，因此同样算一层；解析出来的内容不含元组。
_NESTING_CONTAINERS = (dict, list, tuple)


def _check_nesting(payload) -> None:
    """不用递归地逐层检查对象与数组的嵌套层数，超过 MAX_JSON_NESTING 时抛 ValueError(R25)。

    R28：元组也算一层(见 _NESTING_CONTAINERS)；读取侧解析结果不含元组，不受影响。
    """

    pending = [(payload, 1)] if isinstance(payload, _NESTING_CONTAINERS) else []
    while pending:
        node, depth = pending.pop()
        if depth > MAX_JSON_NESTING:
            raise ValueError(f"JSON 嵌套超过 {MAX_JSON_NESTING} 层")
        children = node.values() if isinstance(node, dict) else node
        pending.extend((child, depth + 1) for child in children if isinstance(child, _NESTING_CONTAINERS))


def check_json_nesting(payload) -> None:
    """写入前检查(R28)：payload 嵌套超过 MAX_JSON_NESTING 层(对象、数组与元组各算一层，标量不算)时抛 ValueError。

    与 update_json 写入前、read_json 与 parse_json_text 读回后的检查同一判定，供自行写 JSON 文件的写入方在写之前
    调用，不留下自己读不回的文件。不用递归，不记日志。
    """

    _check_nesting(payload)


# R26：read_json 认定为"读不了"的异常类型(R25 口径)，供自行解析 JSON 的读取方沿用同一口径。
JSON_READ_ERRORS = (OSError, ValueError, RecursionError)


def parse_json_text(text: str):
    """按 read_json 的口径解析一段 JSON 文本，解析后检查嵌套层数(R26)。

    只接受 str：read_json 按 UTF-8 文本读入，而 json.loads 收到 bytes 时会自行识别 BOM 与 UTF-16，口径不同，
    因此其他类型一律抛 TypeError。解析失败时原样抛出 ValueError(含语法错误、超长整数与超过 MAX_JSON_NESTING
    层)或 RecursionError，二者都在 JSON_READ_ERRORS 内；不记日志、不包装，由调用方按自己的口径处理；其他异常
    照旧原样外抛。
    """

    if not isinstance(text, str):
        raise TypeError(f"parse_json_text 只接受 str，收到 {type(text).__name__}")
    payload = json.loads(text)
    _check_nesting(payload)
    return payload


def read_json(path: str | Path):
    """读取 JSON；任何读取或解析失败都会显式抛出。

    R25：除打不开、编码与语法错误外，数字超过整数位数上限(ValueError)、解析时嵌套过深(RecursionError)与解析后
    嵌套超过 MAX_JSON_NESTING 层，同样记一行并抛 JsonStoreReadError，与无法解析的 JSON 同口径，不再原样外抛；
    其他异常(如 TypeError、MemoryError)照旧原样外抛。
    """

    target = Path(path)
    try:
        with target.open("r", encoding="utf-8") as stream:
            payload = json.load(stream)
        _check_nesting(payload)
        return payload
    except JSON_READ_ERRORS as exc:
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
        for attempt in range(ATOMIC_REPLACE_RETRIES + 1):
            try:
                os.replace(temporary, path)
                break
            except PermissionError:
                if attempt >= ATOMIC_REPLACE_RETRIES:
                    raise
                time.sleep(ATOMIC_REPLACE_RETRY_DELAY_SECONDS * (attempt + 1))
    finally:
        try:
            temporary.unlink(missing_ok=True)
        except OSError:
            pass


def write_text_atomic(path: str | Path, text: str) -> None:
    """把一份完整生成的文本快照原子发布到 path；不读取、不校验旧文件。

    写出的字节与 Path(path).write_text(text, encoding="utf-8") 相同(文本模式，按平台转换换行)。
    内容先完整写入同目录临时文件并落盘，再替换最终文件；替换前任一步失败，原文件保持不变，
    临时文件被清除，异常原样抛出。目录须由调用方保证存在。
    """

    target = Path(path)
    temporary = target.with_name(f"{target.name}.{uuid4().hex}.tmp")
    try:
        with temporary.open("w", encoding="utf-8") as stream:
            stream.write(text)
            stream.flush()
            os.fsync(stream.fileno())
        for attempt in range(ATOMIC_REPLACE_RETRIES + 1):
            try:
                os.replace(temporary, target)
                break
            except PermissionError:
                if attempt >= ATOMIC_REPLACE_RETRIES:
                    raise
                time.sleep(ATOMIC_REPLACE_RETRY_DELAY_SECONDS * (attempt + 1))
    finally:
        try:
            temporary.unlink(missing_ok=True)
        except OSError:
            pass


def update_json(
    path: str | Path,
    mutator: Callable[[object], JsonValue],
    *,
    missing=None,
) -> JsonValue:
    """在同一文件锁内完成完整read-modify-write。

    文件尚不存在时，mutator 接收 missing 的一份深拷贝(缺省 None)；已存在但无法读取时会抛错，绝不把
    损坏文件伪装成空结构。已存在的文件内容为 JSON null 时 mutator 接收 None，调用方可借 missing
    在同一把锁内区分"文件不存在"与"现有内容为 null"(R16)。mutator 必须返回要持久化的完整 JSON 值。
    R25：mutator 返回的内容嵌套超过 MAX_JSON_NESTING 层时拒绝写入——记一行写入失败并抛 ValueError，原文件不变，
    不留下 read_json 读不回的文件。
    """

    target = Path(path)
    with file_lock(target):
        exists = target.exists()
        current = read_json(target) if exists else deepcopy(missing)
        original = deepcopy(current)
        updated = mutator(current)
        if updated is None:
            raise ValueError("JSON mutator 必须返回完整持久化值，不能返回 None")
        if exists and updated == original:
            return updated
        try:
            _check_nesting(updated)
            _write_json_atomic(target, updated)
        except Exception as exc:
            safe_log(
                f"[JSON存储] 写入失败 path={target} "
                f"原因={type(exc).__name__}:{exc}"
            )
            raise
        return updated
