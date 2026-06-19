"""Request-level cache for flight source calls.

This layer sits outside individual sources so main collection, calendar
refreshes, and fallback collection can share identical source requests in the
same Python process. It also keeps a short persistent cache for repeated runs.
"""

from __future__ import annotations

import copy
import json
import time
from datetime import datetime, timedelta
from pathlib import Path

from filename_utils import sanitize_filename


DEFAULT_CACHE_DIR = Path(__file__).parent / "data" / "cache"
DEFAULT_TTL_SECONDS = 15 * 60

_request_cache: dict[tuple, dict] = {}
_disabled_persistent_dirs: set[str] = set()
_stats = {
    "total": 0,
    "hits": 0,
    "actual": 0,
    "by_source": {},
}


def passenger_signature(passengers=None) -> str:
    passengers = passengers or {}
    if not isinstance(passengers, dict):
        return sanitize_filename(str(passengers)) or "unknown"
    return (
        f"{int(passengers.get('adult') or 1)}_"
        f"{int(passengers.get('child') or 0)}_"
        f"{int(passengers.get('elderly') or 0)}_"
        f"{int(passengers.get('infant') or 0)}"
    )


def _source_name(source) -> str:
    return str(getattr(source, "name", type(source).__name__)).lower()


def cache_key(source, origin, dest, date_str, passengers=None, cabin_class="economy") -> tuple:
    return (
        _source_name(source),
        str(origin or "").upper(),
        str(dest or "").upper(),
        str(date_str or ""),
        passenger_signature(passengers),
        str(cabin_class or "economy"),
    )


def _cache_path(key: tuple, cache_dir: Path | None = None) -> Path:
    source, origin, dest, date_str, pax, cabin_class = key
    safe = sanitize_filename("_".join([source, f"{origin}-{dest}", date_str, pax, cabin_class]))
    return Path(cache_dir or DEFAULT_CACHE_DIR) / f"api_{safe}.json"


def _fresh(fetched_at: str | None, ttl_seconds: int) -> bool:
    if ttl_seconds <= 0:
        return False
    try:
        dt = datetime.fromisoformat(str(fetched_at or "").replace("Z", "+00:00"))
        dt = dt.replace(tzinfo=None)
    except (TypeError, ValueError):
        return False
    return datetime.now() - dt < timedelta(seconds=ttl_seconds)


def _record_request(source_name: str) -> None:
    _stats["total"] += 1
    by_source = _stats.setdefault("by_source", {})
    source_stats = by_source.setdefault(source_name, {"requested": 0, "actual": 0, "hits": 0})
    source_stats["requested"] += 1


def _record_hit(source_name: str) -> None:
    _stats["hits"] += 1
    _stats.setdefault("by_source", {}).setdefault(
        source_name, {"requested": 0, "actual": 0, "hits": 0}
    )["hits"] += 1


def _record_actual(source_name: str) -> None:
    _stats["actual"] += 1
    _stats.setdefault("by_source", {}).setdefault(
        source_name, {"requested": 0, "actual": 0, "hits": 0}
    )["actual"] += 1


def _read_persistent(key: tuple, ttl_seconds: int, cache_dir: Path | None = None):
    path = _cache_path(key, cache_dir)
    if not path.exists():
        return None
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    if not _fresh(payload.get("fetched_at"), ttl_seconds):
        return None
    return payload.get("result")


def _write_persistent(key: tuple, result, cache_dir: Path | None = None) -> None:
    path = _cache_path(key, cache_dir)
    disabled_key = str(path.parent)
    if disabled_key in _disabled_persistent_dirs:
        return
    payload = {
        "fetched_at": datetime.now().isoformat(timespec="seconds"),
        "key": list(key),
        "result": result,
    }
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
    except (TypeError, OSError) as exc:
        _disabled_persistent_dirs.add(disabled_key)
        print(f"[缓存] 持久化失败,本轮将跳过该目录 {disabled_key}: {exc}")

def cached_fetch(
    source,
    origin: str,
    dest: str,
    date_str: str,
    passengers=None,
    cabin_class: str = "economy",
    *,
    cabin: str | None = None,
    ttl_seconds: int = DEFAULT_TTL_SECONDS,
    cache_dir: Path | None = None,
    persist: bool = True,
):
    """Fetch through an in-run and short persistent cache.

    The cache key intentionally includes source, direction, date, passengers,
    and cabin so only truly identical requests are deduplicated.
    """
    if cabin is not None:
        cabin_class = cabin
    key = cache_key(source, origin, dest, date_str, passengers, cabin_class)
    source_name = key[0]
    _record_request(source_name)

    memory_entry = _request_cache.get(key)
    if memory_entry and _fresh(memory_entry.get("fetched_at"), ttl_seconds):
        _record_hit(source_name)
        print(f"[缓存命中] {key[:4]} 复用已有结果,不重复调API")
        return copy.deepcopy(memory_entry.get("result"))

    if persist:
        persisted = _read_persistent(key, ttl_seconds, cache_dir)
        if persisted is not None:
            _record_hit(source_name)
            print(f"[缓存命中] {key[:4]} 复用持久缓存,不重复调API")
            _request_cache[key] = {
                "fetched_at": datetime.now().isoformat(timespec="seconds"),
                "result": copy.deepcopy(persisted),
            }
            return copy.deepcopy(persisted)

    print(
        f"[API调用] 源={source_name} 航线={key[1]}->{key[2]} 日期={key[3]} "
        f"乘客={key[4]} 时间={time.time()}"
    )
    _record_actual(source_name)
    result = source.fetch(origin, dest, date_str, cabin_class)
    stored = copy.deepcopy(result)
    _request_cache[key] = {
        "fetched_at": datetime.now().isoformat(timespec="seconds"),
        "result": stored,
    }
    if persist:
        _write_persistent(key, stored, cache_dir)
    return copy.deepcopy(result)


def get_request_cache_stats() -> dict:
    return copy.deepcopy(_stats)


def print_request_cache_stats() -> None:
    print(
        "[API统计] "
        f"本轮总调用={_stats.get('total', 0)}, "
        f"缓存命中={_stats.get('hits', 0)}, "
        f"实际API请求={_stats.get('actual', 0)}, "
        f"各源: {_stats.get('by_source', {})}"
    )


def reset_request_cache(*, clear_memory: bool = True, reset_stats: bool = True) -> None:
    if clear_memory:
        _request_cache.clear()
        _disabled_persistent_dirs.clear()
    if reset_stats:
        _stats["total"] = 0
        _stats["hits"] = 0
        _stats["actual"] = 0
        _stats["by_source"] = {}
