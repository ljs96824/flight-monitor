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

from domestic_fare_rules import AIRCRAFT_NAMES, re_match_aircraft_code
from filename_utils import sanitize_filename
from log_utils import safe_log
import observations_store


DEFAULT_CACHE_DIR = Path(__file__).parent / "data" / "cache"
DEFAULT_TTL_SECONDS = 15 * 60

_request_cache: dict[tuple, dict] = {}
_disabled_persistent_dirs: set[str] = set()
_fetch_trigger_counts: dict[tuple, int] = {}
_equipment_summary: dict[tuple[str, str], dict] = {}


def _empty_stats() -> dict:
    return {"total": 0, "hits": 0, "actual": 0, "by_source": {}}


_stats = _empty_stats()
_process_stats = _empty_stats()
_current_stats_round_id: str | None = None


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


def _source_stats_bucket(stats: dict, source_name: str) -> dict:
    return stats.setdefault("by_source", {}).setdefault(
        source_name, {"requested": 0, "actual": 0, "hits": 0, "calls": 0}
    )


def _record_request(source_name: str) -> None:
    for stats in (_stats, _process_stats):
        stats["total"] += 1
        _source_stats_bucket(stats, source_name)["calls"] += 1


def _record_hit(source_name: str) -> None:
    for stats in (_stats, _process_stats):
        stats["hits"] += 1
        _source_stats_bucket(stats, source_name)["hits"] += 1


def _record_actual(source_name: str) -> None:
    for stats in (_stats, _process_stats):
        stats["actual"] += 1
        source_stats = _source_stats_bucket(stats, source_name)
        source_stats["actual"] += 1
        source_stats["requested"] += 1


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
        safe_log(f"[缓存] 持久化失败,本轮将跳过该目录 {disabled_key}: {exc}")


def _positive_price_flights(result) -> list[dict]:
    if not isinstance(result, dict):
        return []
    flights = result.get("flights") or []
    priced = []
    for flight in flights:
        if not isinstance(flight, dict):
            continue
        try:
            if float(flight.get("price")) > 0:
                priced.append(flight)
        except (TypeError, ValueError):
            continue
    return priced


def _flight_equipment_values(flight: dict) -> list[str]:
    raw_code = str(flight.get("aircraft_code") or flight.get("equipment") or "").strip()
    if raw_code:
        return [raw_code]

    values = []
    direct_name = str(flight.get("aircraft") or "").strip()
    if direct_name:
        values.append(direct_name)
    for segment in flight.get("segments") or []:
        if not isinstance(segment, dict):
            continue
        value = str(segment.get("aircraft_code") or segment.get("aircraft") or "").strip()
        if value:
            values.append(value)
    return values


def _unmapped_aircraft_code(value: str) -> str | None:
    code = str(value or "").strip().upper()
    if code in AIRCRAFT_NAMES:
        return None
    if 2 <= len(code) <= 4 and re_match_aircraft_code(code):
        return code
    return None


def _record_equipment_summary(source_name: str, result) -> None:
    if source_name not in {"juhe", "hasdata"}:
        return
    round_key = str(_current_stats_round_id or "standalone")
    entry = _equipment_summary.setdefault(
        (round_key, source_name),
        {"combo_count": 0, "equipment": set(), "unmapped": set()},
    )
    flights = _positive_price_flights(result)
    entry["combo_count"] += len(flights)
    for flight in flights:
        for value in _flight_equipment_values(flight):
            normalized = str(value).strip().upper()
            if not normalized:
                continue
            entry["equipment"].add(normalized)
            unmapped = _unmapped_aircraft_code(normalized)
            if unmapped:
                entry["unmapped"].add(unmapped)


def _flush_equipment_summary(round_id: str | None) -> None:
    round_key = str(round_id or "standalone")
    keys = sorted(key for key in _equipment_summary if key[0] == round_key)
    for key in keys:
        _, source_name = key
        entry = _equipment_summary.pop(key)
        unmapped = ",".join(sorted(entry["unmapped"]))
        safe_log(
            f"[机型码汇总] 源={source_name} 组合数={entry['combo_count']} "
            f"机型种类={len(entry['equipment'])} 未映射机型=[{unmapped}]"
        )


def _record_observations_after_fetch(source, key: tuple, result, cabin_class: str) -> None:
    source_name = key[0]
    if source_name not in {"juhe", "hasdata"}:
        return
    flights = _positive_price_flights(result)
    if not flights:
        return
    fetch_depart_date = str(key[3])
    round_id, db_path = observations_store.get_current_round()
    if not round_id:
        safe_log(
            f"[\u89c2\u6d4b\u843d\u5e93\u8df3\u8fc7] "
            f"\u822a\u7ebf={key[1]}->{key[2]} \u65e5\u671f={fetch_depart_date} "
            f"\u539f\u56e0=\u65e0round_id"
        )
        return
    try:
        observation_result = observations_store.append_observations(
            flights,
            round_id=round_id,
            route_type=str(getattr(source, "route_type", None) or "unknown"),
            origin_airport=key[1],
            dest_airport=key[2],
            depart_date=fetch_depart_date,
            cabin_class=cabin_class,
            source=source_name,
            observed_at=datetime.now().isoformat(timespec="seconds"),
            db_path=db_path,
        )
        safe_log(
            f"[\u89c2\u6d4b\u843d\u5e93] round={round_id} "
            f"\u822a\u7ebf={key[1]}->{key[2]} \u65e5\u671f={fetch_depart_date} "
            f"\u6e90={source_name} \u5199\u5165={observation_result['written']} "
            f"\u8df3\u8fc7\u91cd\u590d={observation_result['skipped']} "
            f"\u53e3\u5f84=\u5355\u4eba\u5355\u7a0bCNY"
        )
    except Exception as exc:
        safe_log(f"[\u89c2\u6d4b\u843d\u5e93\u5931\u8d25] round={round_id} \u6e90={source_name} \u539f\u56e0={exc}")


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
    force_fresh: bool = False,
    include_cache_status: bool = False,
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

    if not force_fresh:
        memory_entry = _request_cache.get(key)
        if memory_entry and _fresh(memory_entry.get("fetched_at"), ttl_seconds):
            _record_hit(source_name)
            safe_log(f"[缓存命中] {key[:4]} 复用已有结果,不重复调API")
            cached_result = copy.deepcopy(memory_entry.get("result"))
            return (cached_result, "cache") if include_cache_status else cached_result

        if persist:
            persisted = _read_persistent(key, ttl_seconds, cache_dir)
            if persisted is not None:
                _record_hit(source_name)
                safe_log(f"[缓存命中] {key[:4]} 复用持久缓存,不重复调API")
                _request_cache[key] = {
                    "fetched_at": datetime.now().isoformat(timespec="seconds"),
                    "result": copy.deepcopy(persisted),
                }
                cached_result = copy.deepcopy(persisted)
                return (cached_result, "cache") if include_cache_status else cached_result
    else:
        safe_log(f"[缓存绕过] {key[:4]} force_fresh=True,执行真实API请求")

    safe_log(
        f"[API\u8c03\u7528] \u6e90={source_name} \u822a\u7ebf={key[1]}->{key[2]} \u65e5\u671f={key[3]} "
        f"\u4e58\u5ba2={key[4]} \u65f6\u95f4={time.time()}"
    )
    _record_actual(source_name)
    route_type = str(getattr(source, "route_type", None) or "unknown")
    trigger_key = (route_type, source_name, key[1], key[2], key[3])
    trigger_count = _fetch_trigger_counts.get(trigger_key, 0) + 1
    _fetch_trigger_counts[trigger_key] = trigger_count
    safe_log(
        f"[\u91c7\u96c6\u89e6\u53d1] route_type={route_type} \u822a\u7ebf={key[1]}->{key[2]} "
        f"\u65e5\u671f={key[3]} \u6e90={source_name} \u7b2c{trigger_count}\u6b21"
    )
    result = source.fetch(origin, dest, date_str, cabin_class)
    _record_equipment_summary(source_name, result)
    _record_observations_after_fetch(source, key, result, cabin_class)
    stored = copy.deepcopy(result)
    _request_cache[key] = {
        "fetched_at": datetime.now().isoformat(timespec="seconds"),
        "result": stored,
    }
    if persist:
        _write_persistent(key, stored, cache_dir)
    fresh_result = copy.deepcopy(result)
    return (fresh_result, "fresh") if include_cache_status else fresh_result


def get_request_cache_stats() -> dict:
    return copy.deepcopy(_stats)


def get_process_request_cache_stats() -> dict:
    return copy.deepcopy(_process_stats)


def start_request_cache_round(round_id: str) -> None:
    """开始新的采集轮，只清本轮统计，不清请求缓存和进程累计。"""
    global _current_stats_round_id
    _stats.clear()
    _stats.update(_empty_stats())
    _equipment_summary.clear()
    _current_stats_round_id = str(round_id or "") or None


def print_request_cache_stats() -> None:
    _flush_equipment_summary(_current_stats_round_id)
    safe_log(
        "[API统计] "
        f"round={_current_stats_round_id or 'unknown'}, "
        f"本轮总调用={_stats.get('total', 0)}, "
        f"缓存命中={_stats.get('hits', 0)}, "
        f"实际API请求={_stats.get('actual', 0)}, "
        f"各源: {_stats.get('by_source', {})}"
    )
    safe_log(
        "[API统计-进程累计] "
        f"总调用={_process_stats.get('total', 0)}, "
        f"缓存命中={_process_stats.get('hits', 0)}, "
        f"实际API请求={_process_stats.get('actual', 0)}, "
        f"各源: {_process_stats.get('by_source', {})}"
    )


def reset_request_cache(*, clear_memory: bool = True, reset_stats: bool = True) -> None:
    global _current_stats_round_id
    if clear_memory:
        _request_cache.clear()
        _disabled_persistent_dirs.clear()
        _fetch_trigger_counts.clear()
    if reset_stats:
        _stats.clear()
        _stats.update(_empty_stats())
        _process_stats.clear()
        _process_stats.update(_empty_stats())
        _equipment_summary.clear()
        _current_stats_round_id = None
