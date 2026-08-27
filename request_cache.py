"""Request-level cache for flight source calls.

This layer sits outside individual sources so main collection, calendar
refreshes, and fallback collection can share identical source requests in the
same Python process. It also keeps a short persistent cache for repeated runs.
"""

from __future__ import annotations

import copy
import json
import re
import time
from datetime import datetime, timedelta
from pathlib import Path

from domestic_fare_rules import AIRCRAFT_NAMES, re_match_aircraft_code
from filename_utils import sanitize_filename
from flight_combo_utils import normalize_combo
from log_utils import append_round_evidence, safe_log
from quota_policy import MONTHLY, metrics as quota_metrics
import observations_store
from workload_class import UNKNOWN, normalize_workload_class


PROJECT_ROOT = Path(__file__).resolve().parent
DEFAULT_CACHE_DIR = PROJECT_ROOT / "data" / "cache"
DEFAULT_TTL_SECONDS = 15 * 60
SOURCE_FETCH_IO_RETRY_DELAY_SECONDS = 0.25
LISTING_OBSERVATION_SOURCES = frozenset({"juhe", "hasdata", "serpapi"})

_request_cache: dict[tuple, dict] = {}
_persistent_cache_dir_override: Path | None = None
_disabled_persistent_dirs: set[str] = set()
_fetch_trigger_counts: dict[tuple, int] = {}
_equipment_summary: dict[tuple[str, str], dict] = {}
_source_circuit_breakers: dict[str, str] = {}
_round_only_result_keys: set[tuple] = set()
_SECRET_VALUE_PATTERN = re.compile(
    r"(?i)\b(api[_-]?key|access[_-]?token|token|authorization)=([^&\s]+)"
)


def _redact_error_text(value) -> str:
    return _SECRET_VALUE_PATTERN.sub(r"\1=***", str(value or ""))


def _redact_exception_path(value) -> str | None:
    if not value:
        return None
    raw_path = Path(str(value))
    if not raw_path.is_absolute():
        normalized = str(raw_path).replace("\\", "/").lstrip("./")
        return f"<relative>/{normalized or 'unknown'}"
    try:
        relative = raw_path.resolve(strict=False).relative_to(PROJECT_ROOT)
    except (OSError, ValueError):
        return f"<local>/{raw_path.name or 'unknown'}"
    return f"<project>/{str(relative).replace(chr(92), '/')}"


def _source_exception_metadata(exc: Exception) -> dict:
    error_code = getattr(exc, "winerror", None) or getattr(exc, "errno", None)
    path_label = _redact_exception_path(getattr(exc, "filename", None))
    detail = getattr(exc, "strerror", None) or str(exc) or type(exc).__name__
    parts = []
    if error_code is not None:
        parts.append(f"errno={error_code}")
    if path_label:
        parts.append(f"path={path_label}")
    suffix = f"({','.join(parts)})" if parts else ""
    return {
        "error": _redact_error_text(f"{type(exc).__name__}{suffix}:{detail}"),
        "error_type": type(exc).__name__,
        "errno": error_code,
        "path": path_label,
    }


def _empty_stats() -> dict:
    return {
        "total": 0,
        "hits": 0,
        "panel_reused": 0,
        "actual": 0,
        "retries": 0,
        "skipped": 0,
        "planned_actual": 0,
        "outside_actual": 0,
        "outside_unique": 0,
        "by_source": {},
    }


_stats = _empty_stats()
_process_stats = _empty_stats()
_current_stats_round_id: str | None = None
_active_collection_plan_keys: set[tuple] | None = None
_active_panel_only_keys: set[tuple] = set()
_active_collection_plan_unique = 0
_active_panel_freshness_hours = 6.0
_active_fresh_scope = "primary_only"
_outside_plan_seen: set[tuple] = set()
_actual_request_keys_this_round: set[tuple] = set()
_resolved_request_keys_this_round: set[tuple] = set()
_track_usage_for_round = False
_usage_path_for_round: Path | None = None
_quota_budgets_for_round: dict[str, object] = {}
_usage_flushed_for_round = False
_workload_class_for_round = UNKNOWN
_entrypoint_for_round = "unknown"


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


def _source_producer(source) -> str:
    source_type = type(source)
    return f"{source_type.__module__}.{source_type.__qualname__}"


def cache_key(source, origin, dest, date_str, passengers=None, cabin_class="economy") -> tuple:
    request_passengers = (
        passengers
        if bool(getattr(source, "uses_passenger_count", False))
        else {"adult": 1, "child": 0, "elderly": 0, "infant": 0}
    )
    return (
        _source_name(source),
        str(origin or "").upper(),
        str(dest or "").upper(),
        str(date_str or ""),
        passenger_signature(request_passengers),
        str(cabin_class or "economy"),
    )


def _cache_path(key: tuple, cache_dir: Path | None = None) -> Path:
    source, origin, dest, date_str, pax, cabin_class = key
    safe = sanitize_filename("_".join([source, f"{origin}-{dest}", date_str, pax, cabin_class]))
    root = cache_dir or _persistent_cache_dir_override or DEFAULT_CACHE_DIR
    return Path(root) / f"api_{safe}.json"


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
    bucket = stats.setdefault("by_source", {}).setdefault(
        source_name,
        {
            "requested": 0,
            "actual": 0,
            "retries": 0,
            "hits": 0,
            "panel_reused": 0,
            "skipped": 0,
            "calls": 0,
        },
    )
    for key in (
        "requested",
        "actual",
        "retries",
        "hits",
        "panel_reused",
        "skipped",
        "calls",
    ):
        bucket.setdefault(key, 0)
    return bucket


def _record_request(source_name: str) -> None:
    for stats in (_stats, _process_stats):
        stats["total"] += 1
        _source_stats_bucket(stats, source_name)["calls"] += 1


def _record_hit(source_name: str) -> None:
    for stats in (_stats, _process_stats):
        stats["hits"] += 1
        _source_stats_bucket(stats, source_name)["hits"] += 1


def _record_panel_reuse(source_name: str) -> None:
    for stats in (_stats, _process_stats):
        stats["panel_reused"] += 1
        _source_stats_bucket(stats, source_name)["panel_reused"] += 1


def _record_actual(source_name: str, plan_scope: str | None = None) -> None:
    for stats in (_stats, _process_stats):
        stats["actual"] += 1
        if plan_scope == "planned":
            stats["planned_actual"] += 1
        elif plan_scope == "outside":
            stats["outside_actual"] += 1
        source_stats = _source_stats_bucket(stats, source_name)
        source_stats["actual"] += 1
        source_stats["requested"] += 1


def _record_retry_actual(source_name: str, plan_scope: str | None = None) -> None:
    _record_actual(source_name, plan_scope)
    for stats in (_stats, _process_stats):
        stats["retries"] += 1
        _source_stats_bucket(stats, source_name)["retries"] += 1


def _persist_api_usage_attempt(source_name: str) -> None:
    """Persist each source attempt immediately so round interruption cannot lose it."""

    if not _track_usage_for_round:
        return
    from api_usage import DEFAULT_USAGE_PATH, record_actual_requests

    path = _usage_path_for_round or DEFAULT_USAGE_PATH
    try:
        record_actual_requests(
            {source_name: 1},
            path=path,
            round_id=_current_stats_round_id,
            workload_class=_workload_class_for_round,
            entrypoint=_entrypoint_for_round,
        )
    except Exception as exc:
        safe_log(
            f"[\u914d\u989d\u53f0\u8d26\u5931\u8d25] "
            f"round={_current_stats_round_id or 'unknown'} "
            f"\u6e90={source_name} \u539f\u56e0={exc}"
        )


def _assert_usage_ledger_readable() -> None:
    if not _track_usage_for_round:
        return
    from api_usage import DEFAULT_USAGE_PATH, load_usage_strict

    load_usage_strict(_usage_path_for_round or DEFAULT_USAGE_PATH)


def _fetch_source_attempt(source, origin, dest, date_str, cabin_class, source_name: str):
    _assert_usage_ledger_readable()
    try:
        return source.fetch(origin, dest, date_str, cabin_class)
    finally:
        _persist_api_usage_attempt(source_name)


def _record_skip(source_name: str) -> None:
    for stats in (_stats, _process_stats):
        stats["skipped"] += 1
        _source_stats_bucket(stats, source_name)["skipped"] += 1


def activate_collection_plan(
    request_keys,
    *,
    panel_only_keys=None,
    freshness_hours: float = 6,
    fresh_scope: str = "primary_only",
) -> None:
    global _active_collection_plan_keys, _active_collection_plan_unique
    global _active_panel_freshness_hours, _active_fresh_scope
    _active_collection_plan_keys = set(request_keys or [])
    _active_panel_only_keys.clear()
    _active_panel_only_keys.update(panel_only_keys or [])
    _active_collection_plan_unique = len(_active_collection_plan_keys)
    _active_panel_freshness_hours = max(0.0, float(freshness_hours or 0))
    _active_fresh_scope = (
        "all" if str(fresh_scope or "").strip().lower() == "all" else "primary_only"
    )
    _outside_plan_seen.clear()


def deactivate_collection_plan() -> None:
    global _active_collection_plan_keys, _active_collection_plan_unique
    global _active_panel_freshness_hours, _active_fresh_scope
    _active_collection_plan_keys = None
    _active_panel_only_keys.clear()
    _active_collection_plan_unique = 0
    _active_panel_freshness_hours = 6.0
    _active_fresh_scope = "primary_only"
    _outside_plan_seen.clear()


def _plan_scope_for_request(key: tuple, source_name: str, reason: str | None) -> str | None:
    if _active_collection_plan_keys is None:
        return None
    if key in _active_collection_plan_keys:
        return "planned"
    if key not in _outside_plan_seen:
        _outside_plan_seen.add(key)
        for stats in (_stats, _process_stats):
            stats["outside_unique"] += 1
        safe_log(
            f"[计划外补充] 源={source_name} od={key[1]}->{key[2]} 日期={key[3]} "
            f"原因={reason or '运行期补采'}"
        )
    return "outside"


def _quota_failure_reason(result) -> str | None:
    if not isinstance(result, dict):
        return None
    status = str(result.get("source_status") or "").lower()
    if "quota" not in status and str(result.get("quota_code") or "").strip() not in {"112", "10012"}:
        return None
    return str(result.get("error") or result.get("skipped_reason") or "配额不足").strip()


def _positive_result_flights(result) -> list[dict]:
    if not isinstance(result, dict):
        return []
    valid = []
    for flight in result.get("flights") or []:
        if not isinstance(flight, dict):
            continue
        try:
            price = float(flight.get("price") or 0)
        except (TypeError, ValueError):
            price = 0
        if price > 0:
            valid.append(flight)
    return valid


def _result_cache_status(result) -> str:
    status = str((result or {}).get("source_status") or "").strip().lower()
    if (
        status.startswith("failed")
        or status.startswith("error")
        or (result or {}).get("error")
    ):
        return "round_failed"
    if not _positive_result_flights(result):
        return "round_empty"
    return "persistent"


def _store_round_only_result(key: tuple, result: dict, cache_status: str) -> None:
    _request_cache[key] = {
        "fetched_at": datetime.now().isoformat(timespec="seconds"),
        "result": copy.deepcopy(result),
        "cache_status": cache_status,
    }
    _round_only_result_keys.add(key)
    if _current_stats_round_id:
        _resolved_request_keys_this_round.add(key)


def record_planned_source_skip(
    source,
    origin: str,
    dest: str,
    date_str: str,
    passengers=None,
    cabin_class: str = "economy",
    *,
    reason: str,
) -> dict:
    """把计划阶段的配额保护记为可复用的本轮源级跳过。"""
    key = cache_key(source, origin, dest, date_str, passengers, cabin_class)
    source_name = key[0]
    _record_request(source_name)
    _record_skip(source_name)
    result = {
        "flights": [],
        "source": source_name,
        "raw": {},
        "source_status": "skipped_quota_protection",
        "skipped_reason": str(reason),
        "error": str(reason),
        "collection_state": "quota_protected",
        "collection_label": "配额保护跳过",
    }
    _store_round_only_result(key, result, "skipped")
    safe_log(
        f"[配额保护] 源={source_name} 航线={key[1]}->{key[2]} "
        f"日期={key[3]} 舱位={key[5]} 结果=源级跳过 原因={reason}"
    )
    return copy.deepcopy(result)


def _archive_listing_result(source_name: str, key: tuple, result: dict) -> None:
    if source_name not in LISTING_OBSERVATION_SOURCES:
        return
    cache_status = _result_cache_status(result)
    if cache_status == "persistent":
        return
    append_round_evidence(
        f"[源响应证据] 源={source_name} 航线={key[1]}->{key[2]} 日期={key[3]} "
        f"状态={result.get('source_status') or cache_status} raw=",
        result.get("raw") if result.get("raw") is not None else {
            "error": result.get("error"),
            "reason": result.get("reason"),
        },
    )


def _source_preflight_skip(source, origin, dest, date_str, cabin_class):
    check = getattr(source, "preflight_skip", None)
    if not callable(check):
        return None
    try:
        result = check(origin, dest, date_str, cabin_class)
    except Exception as exc:
        safe_log(f"[源级跳过检查失败] 源={_source_name(source)} 原因={exc},继续正常请求")
        return None
    return result if isinstance(result, dict) else None


def _read_persistent_payload(key: tuple, cache_dir: Path | None = None) -> dict | None:
    path = _cache_path(key, cache_dir)
    if not path.exists():
        return None
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    return payload if isinstance(payload, dict) else None


def _cached_flight_has_actionable_detail(flight: dict) -> bool:
    segments = flight.get("segments") or []
    if not isinstance(segments, (list, tuple)) or not segments:
        return False
    first = segments[0] if isinstance(segments[0], dict) else {}
    last = segments[-1] if isinstance(segments[-1], dict) else {}
    departure_time = (
        flight.get("departure_time")
        or flight.get("dep_time")
        or first.get("departure_time")
        or first.get("dep_time")
    )
    arrival_time = (
        flight.get("arrival_time")
        or flight.get("arr_time")
        or last.get("arrival_time")
        or last.get("arr_time")
    )
    combo = normalize_combo(flight.get("flight_combo") or flight.get("flight_no"))
    return bool(combo and departure_time and arrival_time)


def _persistent_payload_matches_source(payload: dict, key: tuple, source) -> bool:
    if source is None:
        return True
    expected = _source_producer(source)
    cached = str(payload.get("producer_class") or "").strip()
    if cached:
        if cached == expected:
            return True
        safe_log(
            f"[缓存失效] {key[:4]} 缓存生产者={cached} 当前生产者={expected},不复用"
        )
        return False
    if key[0] not in LISTING_OBSERVATION_SOURCES:
        return True
    result = payload.get("result")
    flights = _positive_price_flights(result)
    if any(_cached_flight_has_actionable_detail(flight) for flight in flights):
        return True
    safe_log(
        f"[缓存失效] {key[:4]} 旧缓存缺生产者标识且无完整航段信息,不复用"
    )
    return False


def _read_persistent(
    key: tuple,
    ttl_seconds: int,
    cache_dir: Path | None = None,
    *,
    source=None,
):
    payload = _read_persistent_payload(key, cache_dir)
    if payload is None:
        return None
    if not _fresh(payload.get("fetched_at"), ttl_seconds):
        return None
    if not _persistent_payload_matches_source(payload, key, source):
        return None
    result = payload.get("result")
    if _result_cache_status(result) != "persistent":
        return None
    return result


def _parse_cache_time(value) -> datetime | None:
    try:
        parsed = datetime.fromisoformat(str(value or "").replace("Z", "+00:00"))
    except (TypeError, ValueError):
        return None
    if parsed.tzinfo is not None:
        parsed = parsed.astimezone().replace(tzinfo=None)
    return parsed


def _panel_label(observed_at: str) -> str:
    parsed = _parse_cache_time(observed_at)
    return f"面板复用{parsed.strftime('%H:%M')}" if parsed else "面板复用"


def _panel_snapshot_for_key(key: tuple, freshness_hours: float) -> dict | None:
    if key[0] not in LISTING_OBSERVATION_SOURCES:
        return None
    _round_id, db_path = observations_store.get_current_round()
    return observations_store.load_fresh_observation_snapshot(
        source=key[0],
        origin_airport=key[1],
        dest_airport=key[2],
        depart_date=key[3],
        cabin_class=key[5],
        freshness_hours=freshness_hours,
        db_path=db_path,
    )


def _rebuild_panel_result(
    key: tuple,
    snapshot: dict,
    *,
    source,
    cache_dir: Path | None = None,
    freshness_hours: float,
) -> dict | None:
    """以面板价格为准，用同键完整缓存补足航段结构。"""
    persisted = _read_persistent_payload(key, cache_dir)
    if persisted is None or not _fresh(
        persisted.get("fetched_at"),
        max(1, int(float(freshness_hours) * 60 * 60)),
    ):
        return None
    if not _persistent_payload_matches_source(persisted, key, source):
        return None
    result = persisted.get("result")
    if not isinstance(result, dict):
        return None
    observed_at = str(snapshot.get("observed_at") or "")
    cache_time = _parse_cache_time(persisted.get("fetched_at"))
    observed_time = _parse_cache_time(observed_at)
    if cache_time is None or observed_time is None:
        return None
    if cache_time + timedelta(minutes=2) < observed_time:
        return None

    cached_by_combo = {}
    for flight in result.get("flights") or []:
        if not isinstance(flight, dict):
            continue
        combo = normalize_combo(flight.get("flight_combo") or flight.get("flight_no"))
        if combo and combo not in cached_by_combo:
            cached_by_combo[combo] = flight

    rebuilt = []
    for row in snapshot.get("rows") or []:
        combo = normalize_combo(row.get("flight_combo"))
        cached_flight = cached_by_combo.get(combo)
        if not combo or cached_flight is None:
            return None
        flight = copy.deepcopy(cached_flight)
        flight["flight_combo"] = combo
        flight["price"] = float(row["price_cny"])
        flight["collected_at"] = observed_at
        flight["collection_state"] = "panel_reused"
        flight["collection_label"] = _panel_label(observed_at)
        flight["observation_round_id"] = snapshot.get("round_id")
        rebuilt.append(flight)
    if not rebuilt:
        return None

    panel_result = copy.deepcopy(result)
    panel_result["flights"] = rebuilt
    panel_result["collected_at"] = observed_at
    panel_result["collection_state"] = "panel_reused"
    panel_result["collection_label"] = _panel_label(observed_at)
    panel_result["observation_round_id"] = snapshot.get("round_id")
    return panel_result


def panel_reuse_result(
    source,
    origin: str,
    dest: str,
    date_str: str,
    passengers=None,
    cabin_class: str = "economy",
    *,
    freshness_hours: float = 6,
    cache_dir: Path | None = None,
) -> dict | None:
    key = cache_key(source, origin, dest, date_str, passengers, cabin_class)
    snapshot = _panel_snapshot_for_key(key, freshness_hours)
    if snapshot is None:
        return None
    return _rebuild_panel_result(
        key,
        snapshot,
        source=source,
        cache_dir=cache_dir,
        freshness_hours=freshness_hours,
    )


def _request_is_panel_only(
    key: tuple,
    *,
    explicit: bool | None,
    force_fresh: bool,
    reason: str | None,
) -> bool:
    if force_fresh:
        return False
    if explicit is not None:
        return bool(explicit)
    if key in _active_panel_only_keys:
        return True
    if _active_fresh_scope != "primary_only":
        return False
    reason_text = str(reason or "")
    return "弹性日期" in reason_text or "低价日历" in reason_text


def _write_persistent(
    key: tuple,
    result,
    cache_dir: Path | None = None,
    *,
    source=None,
) -> None:
    path = _cache_path(key, cache_dir)
    disabled_key = str(path.parent)
    if disabled_key in _disabled_persistent_dirs:
        return
    payload = {
        "fetched_at": datetime.now().isoformat(timespec="seconds"),
        "key": list(key),
        "producer_class": _source_producer(source) if source is not None else "",
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
    if source_name not in LISTING_OBSERVATION_SOURCES:
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
    if source_name not in LISTING_OBSERVATION_SOURCES:
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
    include_cache_details: bool = False,
    request_reason: str | None = None,
    panel_only: bool | None = None,
):
    """Fetch through an in-run and short persistent cache.

    缓存键包含源、方向、日期和舱位。仅当源明确声明人数会改变 HTTP 请求时，
    才把乘客组合纳入键；当前各源均按规范化单成人请求跨订阅复用。
    """
    def returned(value, status: str, reuse_kind: str | None = None):
        if include_cache_details:
            return value, status, reuse_kind
        if include_cache_status:
            return value, status
        return value

    if cabin is not None:
        cabin_class = cabin
    key = cache_key(source, origin, dest, date_str, passengers, cabin_class)
    source_name = key[0]
    plan_scope = _plan_scope_for_request(key, source_name, request_reason)
    panel_only_request = _request_is_panel_only(
        key,
        explicit=panel_only,
        force_fresh=force_fresh,
        reason=request_reason,
    )
    _record_request(source_name)

    if _current_stats_round_id and key in _resolved_request_keys_this_round:
        memory_entry = _request_cache.get(key)
        if memory_entry is not None:
            _record_hit(source_name)
            safe_log(f"[本轮池命中] {key[:4]} 已解析入池,整轮内不重复执行")
            cached_result = copy.deepcopy(memory_entry.get("result"))
            cache_status = (
                str(memory_entry.get("cache_status") or "cache")
                if key in _round_only_result_keys
                else "cache"
            )
            return returned(cached_result, cache_status, "in_round_cache")

    skipped_result = _source_preflight_skip(
        source,
        origin,
        dest,
        date_str,
        cabin_class,
    )
    if skipped_result is not None:
        _record_skip(source_name)
        reason = skipped_result.get("skipped_reason") or "源级前置条件不满足"
        safe_log(
            f"[源级跳过] 源={source_name} 航线={key[1]}->{key[2]} "
            f"日期={key[3]} 原因={reason}"
        )
        fresh_result = copy.deepcopy(skipped_result)
        return returned(fresh_result, "skipped")

    if not force_fresh and source_name in LISTING_OBSERVATION_SOURCES:
        panel_result = panel_reuse_result(
            source,
            origin,
            dest,
            date_str,
            passengers,
            cabin_class,
            freshness_hours=_active_panel_freshness_hours,
            cache_dir=cache_dir,
        )
        if panel_result is not None:
            _record_panel_reuse(source_name)
            _request_cache[key] = {
                "fetched_at": panel_result.get("collected_at"),
                "result": copy.deepcopy(panel_result),
                "cache_status": "panel",
            }
            if _current_stats_round_id:
                _resolved_request_keys_this_round.add(key)
            safe_log(
                f"[面板复用] 源={source_name} 航线={key[1]}->{key[2]} "
                f"日期={key[3]} 采集于={panel_result.get('collected_at')} 不发API"
            )
            copied = copy.deepcopy(panel_result)
            return returned(copied, "panel", "panel")

    if panel_only_request:
        _record_skip(source_name)
        skipped_result = {
            "flights": [],
            "source": source_name,
            "raw": {},
            "source_status": "skipped_panel_only",
            "skipped_reason": "今日未采",
            "collection_state": "panel_missing",
            "collection_label": "今日未采",
            "collected_at": None,
        }
        _request_cache[key] = {
            "fetched_at": datetime.now().isoformat(timespec="seconds"),
            "result": copy.deepcopy(skipped_result),
            "cache_status": "skipped",
        }
        if _current_stats_round_id:
            _resolved_request_keys_this_round.add(key)
        safe_log(
            f"[面板只读] 源={source_name} 航线={key[1]}->{key[2]} "
            f"日期={key[3]} 结果=今日未采 不补采"
        )
        copied = copy.deepcopy(skipped_result)
        return returned(copied, "skipped")

    if not force_fresh:
        memory_entry = _request_cache.get(key)
        if memory_entry and _fresh(memory_entry.get("fetched_at"), ttl_seconds):
            _record_hit(source_name)
            safe_log(f"[缓存命中] {key[:4]} 复用已有结果,不重复调API")
            if _current_stats_round_id:
                _resolved_request_keys_this_round.add(key)
            cached_result = copy.deepcopy(memory_entry.get("result"))
            return returned(cached_result, "cache", "persistent_cache")

        if persist:
            persisted = _read_persistent(key, ttl_seconds, cache_dir, source=source)
            if persisted is not None:
                _record_hit(source_name)
                safe_log(f"[缓存命中] {key[:4]} 复用持久缓存,不重复调API")
                _request_cache[key] = {
                    "fetched_at": datetime.now().isoformat(timespec="seconds"),
                    "result": copy.deepcopy(persisted),
                }
                if _current_stats_round_id:
                    _resolved_request_keys_this_round.add(key)
                cached_result = copy.deepcopy(persisted)
                return returned(cached_result, "cache", "persistent_cache")
    else:
        safe_log(f"[缓存绕过] {key[:4]} force_fresh=True,执行真实API请求")

    disabled_reason = _source_circuit_breakers.get(source_name)
    if disabled_reason:
        _record_skip(source_name)
        safe_log(
            f"[源熔断] 源={source_name} 原因={disabled_reason} 生效范围=本进程 "
            f"航线={key[1]}->{key[2]} 日期={key[3]}"
        )
        skipped_result = {
            "flights": [],
            "source": source_name,
            "raw": {},
            "source_status": "skipped_source_disabled",
            "skipped_reason": disabled_reason,
            "error": disabled_reason,
        }
        return returned(copy.deepcopy(skipped_result), "skipped")

    safe_log(
        f"[API\u8c03\u7528] \u6e90={source_name} \u822a\u7ebf={key[1]}->{key[2]} \u65e5\u671f={key[3]} "
        f"\u4e58\u5ba2={key[4]} \u65f6\u95f4={time.time()}"
    )
    _actual_request_keys_this_round.add(key)
    _record_actual(source_name, plan_scope)
    route_type = str(getattr(source, "route_type", None) or "unknown")
    trigger_key = (route_type, source_name, key[1], key[2], key[3])
    trigger_count = _fetch_trigger_counts.get(trigger_key, 0) + 1
    _fetch_trigger_counts[trigger_key] = trigger_count
    safe_log(
        f"[\u91c7\u96c6\u89e6\u53d1] route_type={route_type} \u822a\u7ebf={key[1]}->{key[2]} "
        f"\u65e5\u671f={key[3]} \u6e90={source_name} \u7b2c{trigger_count}\u6b21"
    )
    retry_count = 0
    try:
        try:
            result = _fetch_source_attempt(
                source,
                origin,
                dest,
                date_str,
                cabin_class,
                source_name,
            )
        except OSError as first_error:
            retry_count = 1
            first_metadata = _source_exception_metadata(first_error)
            safe_log(
                f"[采集重试] 源={source_name} 航线={key[1]}->{key[2]} "
                f"日期={key[3]} 次数=1/1 原因={first_metadata['error']} "
                f"退避={SOURCE_FETCH_IO_RETRY_DELAY_SECONDS}s"
            )
            time.sleep(SOURCE_FETCH_IO_RETRY_DELAY_SECONDS)
            _record_retry_actual(source_name, plan_scope)
            retry_trigger_count = _fetch_trigger_counts.get(trigger_key, 0) + 1
            _fetch_trigger_counts[trigger_key] = retry_trigger_count
            safe_log(
                f"[API调用] 源={source_name} 航线={key[1]}->{key[2]} 日期={key[3]} "
                f"乘客={key[4]} 时间={time.time()} 重试=1"
            )
            safe_log(
                f"[采集触发] route_type={route_type} 航线={key[1]}->{key[2]} "
                f"日期={key[3]} 源={source_name} 第{retry_trigger_count}次"
            )
            result = _fetch_source_attempt(
                source,
                origin,
                dest,
                date_str,
                cabin_class,
                source_name,
            )
    except Exception as exc:
        result = {
            "flights": [],
            "source": source_name,
            "raw": {},
            "source_status": "failed",
            "retry_count": retry_count,
            **_source_exception_metadata(exc),
        }
        _archive_listing_result(source_name, key, result)
        _store_round_only_result(key, result, "round_failed")
        safe_log(
            f"[采集失败入池] 源={source_name} 航线={key[1]}->{key[2]} "
            f"日期={key[3]} 重试={retry_count} 原因={result['error']}"
        )
        return returned(copy.deepcopy(result), "fresh")
    if retry_count and isinstance(result, dict):
        result.setdefault("retry_count", retry_count)
    quota_reason = _quota_failure_reason(result)
    if quota_reason:
        _source_circuit_breakers[source_name] = quota_reason
        _archive_listing_result(source_name, key, result)
        _store_round_only_result(key, result, "round_failed")
        safe_log(f"[源熔断] 源={source_name} 原因={quota_reason} 生效范围=本进程")
        return returned(copy.deepcopy(result), "fresh")
    collected_at = str(result.get("collected_at") or datetime.now().isoformat(timespec="seconds"))
    result["collected_at"] = collected_at
    result["collection_state"] = "fresh"
    result["collection_label"] = f"实时采集{_panel_label(collected_at).removeprefix('面板复用')}"
    for flight in result.get("flights") or []:
        if not isinstance(flight, dict):
            continue
        flight["collected_at"] = flight.get("collected_at") or collected_at
        flight["collection_state"] = "fresh"
        flight["collection_label"] = result["collection_label"]
    _record_equipment_summary(source_name, result)
    _record_observations_after_fetch(source, key, result, cabin_class)
    stored = copy.deepcopy(result)
    cache_status = _result_cache_status(stored)
    _archive_listing_result(source_name, key, stored)
    if cache_status == "persistent":
        _request_cache[key] = {
            "fetched_at": datetime.now().isoformat(timespec="seconds"),
            "result": stored,
            "cache_status": "fresh",
        }
        if _current_stats_round_id:
            _resolved_request_keys_this_round.add(key)
        if persist:
            _write_persistent(key, stored, cache_dir, source=source)
    else:
        _store_round_only_result(key, stored, cache_status)
    fresh_result = copy.deepcopy(result)
    return returned(fresh_result, "fresh")


def get_request_cache_stats() -> dict:
    return copy.deepcopy(_stats)


def get_process_request_cache_stats() -> dict:
    return copy.deepcopy(_process_stats)


def start_request_cache_round(
    round_id: str,
    *,
    track_usage: bool = False,
    usage_path: str | Path | None = None,
    quota_budgets: dict[str, object] | None = None,
    workload_class: str = UNKNOWN,
    entrypoint: str = "unknown",
) -> None:
    """开始新的采集轮，只清本轮统计，不清请求缓存和进程累计。"""
    if track_usage:
        from api_usage import DEFAULT_USAGE_PATH, load_usage_strict

        load_usage_strict(Path(usage_path) if usage_path else DEFAULT_USAGE_PATH)
    global _current_stats_round_id
    global _track_usage_for_round, _usage_path_for_round, _quota_budgets_for_round
    global _usage_flushed_for_round
    global _workload_class_for_round, _entrypoint_for_round
    _stats.clear()
    _stats.update(_empty_stats())
    _equipment_summary.clear()
    for key in tuple(_round_only_result_keys):
        _request_cache.pop(key, None)
    _round_only_result_keys.clear()
    _current_stats_round_id = str(round_id or "") or None
    _track_usage_for_round = bool(track_usage)
    _usage_path_for_round = Path(usage_path) if usage_path else None
    _quota_budgets_for_round = {
        str(source): copy.deepcopy(value)
        for source, value in (quota_budgets or {}).items()
    }
    _usage_flushed_for_round = False
    _workload_class_for_round = normalize_workload_class(workload_class)
    _entrypoint_for_round = str(entrypoint or "unknown")
    _actual_request_keys_this_round.clear()
    _resolved_request_keys_this_round.clear()


def _flush_api_usage_ledger() -> None:
    global _usage_flushed_for_round
    if not _track_usage_for_round or _usage_flushed_for_round:
        return
    from api_usage import (
        DEFAULT_USAGE_PATH,
        format_quota_overview,
        load_usage_strict,
        round_actual_counts,
        usage_snapshot,
    )

    path = _usage_path_for_round or DEFAULT_USAGE_PATH
    actual_by_source = {
        source: int(values.get("actual", 0) or 0)
        for source, values in (_stats.get("by_source") or {}).items()
        if int(values.get("actual", 0) or 0) > 0
    }
    payload = load_usage_strict(path)
    persisted_by_source = round_actual_counts(payload, _current_stats_round_id)
    safe_log(
        f"[\u914d\u989d\u6052\u7b49\u5f0f] round={_current_stats_round_id or 'unknown'} "
        f"\u5185\u5b58\u5b9e\u9645={actual_by_source} \u5df2\u843d\u8d26={persisted_by_source} "
        f"\u4e00\u81f4={actual_by_source == persisted_by_source}"
    )
    snapshot = usage_snapshot(payload)
    for source, budget in _quota_budgets_for_round.items():
        today_count = int((snapshot.get("today") or {}).get(source, 0) or 0)
        quota = quota_metrics(
            budget,
            snapshot,
            source,
            usage_payload=payload,
        )
        if quota["kind"] == MONTHLY:
            safe_log(
                f"[配额台账] {source} 今日={today_count} "
                f"本月已用={quota['used']}/{quota['total_limit']} "
                f"余量估算={quota['remaining']} 预留={quota['reserve']} "
                f"(本地估算,以SerpAPI控制台为准)"
            )
            continue
        safe_log(
            f"[配额台账] {source} 今日={today_count} "
            f"本epoch已用={quota['used']}/预算{quota['total_limit']} "
            f"余量估算={quota['remaining']} 储备={quota['reserve']} "
            f"研究可用={quota['research_available']} "
            f"(本地估算,以聚合数据控制台为准)"
        )
    _usage_flushed_for_round = True
    safe_log(format_quota_overview(payload, _quota_budgets_for_round))


def print_request_cache_stats() -> None:
    _flush_equipment_summary(_current_stats_round_id)
    classified_ok = int(_stats.get("actual", 0)) == (
        int(_stats.get("planned_actual", 0)) + int(_stats.get("outside_actual", 0))
    ) if _active_collection_plan_keys is not None else True
    unique_actual_ok = (
        int(_stats.get("actual", 0))
        == len(_actual_request_keys_this_round) + int(_stats.get("retries", 0))
        if _active_collection_plan_keys is not None
        else True
    )
    invariant_ok = classified_ok and unique_actual_ok
    safe_log(
        "[API统计] "
        f"round={_current_stats_round_id or 'unknown'}, "
        f"本轮总调用={_stats.get('total', 0)}, "
        f"缓存命中={_stats.get('hits', 0)}, "
        f"面板复用={_stats.get('panel_reused', 0)}, "
        f"源级跳过={_stats.get('skipped', 0)}, "
        f"实际API请求={_stats.get('actual', 0)}, "
        f"重试={_stats.get('retries', 0)}, "
        f"计划唯一={_active_collection_plan_unique}, "
        f"计划内实际={_stats.get('planned_actual', 0)}, "
        f"计划外补充={_stats.get('outside_actual', 0)}, "
        f"恒等式成立={invariant_ok}, "
        f"各源: {_stats.get('by_source', {})}"
    )
    safe_log(
        "[API统计-进程累计] "
        f"总调用={_process_stats.get('total', 0)}, "
        f"缓存命中={_process_stats.get('hits', 0)}, "
        f"面板复用={_process_stats.get('panel_reused', 0)}, "
        f"源级跳过={_process_stats.get('skipped', 0)}, "
        f"实际API请求={_process_stats.get('actual', 0)}, "
        f"重试={_process_stats.get('retries', 0)}, "
        f"各源: {_process_stats.get('by_source', {})}"
    )
    _flush_api_usage_ledger()


def reset_request_cache(*, clear_memory: bool = True, reset_stats: bool = True) -> None:
    global _current_stats_round_id
    global _track_usage_for_round, _usage_path_for_round, _quota_budgets_for_round
    global _usage_flushed_for_round
    global _workload_class_for_round, _entrypoint_for_round
    if clear_memory:
        _request_cache.clear()
        _round_only_result_keys.clear()
        _disabled_persistent_dirs.clear()
        _fetch_trigger_counts.clear()
    _source_circuit_breakers.clear()
    _actual_request_keys_this_round.clear()
    _resolved_request_keys_this_round.clear()
    deactivate_collection_plan()
    _track_usage_for_round = False
    _usage_path_for_round = None
    _quota_budgets_for_round = {}
    _usage_flushed_for_round = False
    _workload_class_for_round = UNKNOWN
    _entrypoint_for_round = "unknown"
    if reset_stats:
        _stats.clear()
        _stats.update(_empty_stats())
        _process_stats.clear()
        _process_stats.update(_empty_stats())
        _equipment_summary.clear()
        _current_stats_round_id = None


def reset_for_tests(cache_dir: str | Path | None) -> None:
    """隔离测试缓存目录并清空所有请求缓存运行态。"""
    global _persistent_cache_dir_override
    _persistent_cache_dir_override = Path(cache_dir) if cache_dir is not None else None
    reset_request_cache(clear_memory=True, reset_stats=True)
    if cache_dir is None:
        observations_store.clear_current_round()
    else:
        observations_store.set_current_round(
            "",
            Path(cache_dir).parent / "observations.sqlite3",
        )
