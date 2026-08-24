import inspect
import json
import logging
import os
import re
import traceback
from datetime import date, datetime, timedelta
from pathlib import Path

try:
    import httpx
except ModuleNotFoundError:  # Optional: only needed for PythonAnywhere payload sync.
    httpx = None
from dotenv import load_dotenv
from airports import AIRPORTS, location_error_message, resolve_location
from airlines import resolve_lcc_policy


BASE_DIR = Path(__file__).parent
DATA_DIR = BASE_DIR / "data"
DATA_DIR.mkdir(exist_ok=True)

# 加载环境变量
load_dotenv(BASE_DIR / ".env", encoding="utf-8")

from analyzer import (
    apply_default_rules,
    analyze_all_flights,
    analyze_price_calendar,
    analyze_round_trip,
    build_price_hint_from_calendar,
    calc_buy_vs_wait_risk,
    determine_cabins,
    get_total_passengers,
    migrate_old_subscription,
    price_position_description,
    waiting_risk_description,
)
from collector import _normalize_detail_flight, save_raw_response
from api_usage import load_usage, usage_snapshot
from basket_sentinel import run_basket_sentinel
from collection_plan import build_collection_plan, load_collection_settings
from constraint_fingerprint import constraint_fingerprint
from detail_access import canonical_detail_uuid, delivery_payload_with_detail_token
from email_notifier import render_email, send_email
from filename_utils import sanitize_filename
from health_check import system_health_check
from log_utils import end_round_log_archive, redact_text, safe_log, start_round_log_archive
from notification_config import normalize_notification_goals
from observations_store import clear_current_round, get_current_round, set_current_round
from notifier import (
    build_notification_payload,
    persist_notification_payload,
    render_detail_html,
    render_pushplus_sections,
    send,
)
from price_calendar import load_calendar, update_calendar
from request_cache import (
    activate_collection_plan,
    deactivate_collection_plan,
    print_request_cache_stats,
    start_request_cache_round,
)
from plan_tracker import feedback_acknowledgement
from provenance import build_route_provenance_context_from_info
from retention import log_retention_dry_run
from source_profiles import get_source_profile
from sources.aggregator import (
    FlightAggregator,
    _redact_api_key,
    build_default_sources,
    is_domestic_route,
    route_type_for,
)
from storage import (
    get_constraint_epoch_boundary,
    get_constraint_history_limit,
    get_roundtrip_price_history,
    get_lowest_price_history,
    get_previous_snapshot_prices,
    init_db,
    save_roundtrip_snapshot,
    save_flight_details,
)
from sync_subscriptions import sync_subscriptions
from subscription_preflight import (
    evaluate_subscription_preflight,
    shanghai_today as _shanghai_today,
)
from tracker import log_signal
from tcurve import build_notification_tcurve
from forecast import build_notification_forecast


# 日志配置
LOG_PATH = DATA_DIR / "monitor.log"
logging.basicConfig(
    filename=str(LOG_PATH),
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(message)s",
    encoding="utf-8",
)

ANALYSIS_LOG = DATA_DIR / "analysis_log.jsonl"
SUBSCRIPTIONS_PATH = DATA_DIR / "subscriptions.json"
PAGE_PAYLOADS_DIR = DATA_DIR / "payloads"
CONFIG_PATH = BASE_DIR / "config.yaml"
API_USAGE_PATH = DATA_DIR / "api_usage.json"
BASKET_SENTINEL_STATE_PATH = DATA_DIR / "basket_sentinel.json"
ROUND_LOG_ROOT = DATA_DIR / "logs" / "rounds"
PYTHONANYWHERE_PAYLOAD_PATH = "/home/{user}/flight-monitor/data/payloads/{filename}"
AIRPORT_COMBINATION_MIN_OPTIONS = 5

_SOURCE_ENV_KEYS = {
    "hasdata": "HASDATA_KEY",
    "duffel": "DUFFEL_TOKEN",
    "juhe": "JUHE_FLIGHT_KEY",
}


def _subscription_label(sub: dict) -> str:
    name = str(sub.get("name") or "").strip()
    if name and name != "网页订阅":
        return name
    for key in ("_index", "index", "id", "subscription_id"):
        value = sub.get(key)
        if value not in (None, ""):
            return str(value)
    return f"{sub.get('origin') or '?'}->{sub.get('destination') or '?'}"


def _subscription_route_label(sub: dict) -> str:
    return f"{sub.get('origin') or '?'}->{sub.get('destination') or '?'}"


def _estimated_saved_api_calls(sub: dict) -> int:
    """估算目标日首轮本会发出的真实外部请求数，Juhe 过去日期守卫不计。"""
    profile = get_source_profile(sub.get("route_type"))
    enabled_external_sources = 0
    for spec in profile.get("sources") or []:
        source_name = str(spec.get("name") or "").lower()
        if source_name == "juhe":
            continue
        env_key = _SOURCE_ENV_KEYS.get(source_name)
        if env_key is None or os.environ.get(env_key):
            enabled_external_sources += 1
    origins = sub.get("origin_airports_active") or sub.get("origin_airports") or [sub.get("origin")]
    destinations = (
        sub.get("destination_airports_active")
        or sub.get("destination_airports")
        or [sub.get("destination")]
    )
    airport_combinations = max(1, len(origins or [])) * max(1, len(destinations or []))
    cabin_count = max(1, len(sub.get("cabin_classes") or ["economy"]))
    return enabled_external_sources * airport_combinations * cabin_count


def _log_preflight_skip(sub: dict, preflight: dict) -> None:
    reason_code = str(preflight.get("reason_code") or "")
    if reason_code != "expired":
        safe_log(
            f"[订阅前置校验] 订阅={_subscription_label(sub)} "
            f"航线={_subscription_route_label(sub)} 结果=跳过 "
            f"原因={preflight.get('reason') or sub.get('invalid_reason') or reason_code or '订阅无效'} "
            f"省API={_estimated_saved_api_calls(sub)}"
        )
        return
    latest = preflight.get("latest_date")
    safe_log(
        f"[订阅前置校验] 订阅={_subscription_label(sub)} "
        f"航线={_subscription_route_label(sub)} 结果=跳过 "
        f"原因=全部采集日期已过期(最晚={latest.isoformat() if latest else '不可解析'}) "
        f"省API={_estimated_saved_api_calls(sub)}"
    )


def _source_error_items(aggregator=None, data=None) -> list[dict]:
    errors = []
    if isinstance(data, dict):
        errors.extend(data.get("source_errors") or [])
    if not errors and aggregator is not None:
        errors.extend(getattr(aggregator, "last_source_errors", None) or [])
    unique = []
    seen = set()
    for item in errors:
        if not isinstance(item, dict):
            continue
        key = (item.get("source"), item.get("cabin_class"), item.get("error"))
        if key in seen:
            continue
        seen.add(key)
        unique.append(item)
    return unique


def _format_source_failure_reason(source_errors=None, reason: str | None = None) -> str:
    parts = []
    for item in source_errors or []:
        source = str(item.get("source") or "source")
        detail = _redact_api_key(str(item.get("error") or "返回失败"))
        parts.append(f"{source}:{detail}")
    if not parts:
        parts.append(_redact_api_key(str(reason or "采集未返回有效航班")))
    return "; ".join(parts)


def _build_collection_leg_failure(
    direction_code: str,
    date_str: str,
    origin_airports,
    destination_airports,
    source_errors,
) -> dict:
    direction = "返程" if direction_code == "return" else "去程"
    errors = [dict(item) for item in (source_errors or []) if isinstance(item, dict)]
    return {
        "direction": direction,
        "direction_code": direction_code,
        "date": str(date_str or ""),
        "origin_airports": list(origin_airports or []),
        "destination_airports": list(destination_airports or []),
        "source_errors": errors,
        "reason": _format_source_failure_reason(errors),
    }


def _should_skip_low_price_alert(sub: dict, analysis: dict) -> bool:
    """数据完整时才允许低价策略静默跳过；采集失败必须通知用户。"""
    return bool(
        (sub.get("hard_constraints", {}) or {}).get("budget_strategy")
        == "low_price_alert"
        and not analysis.get("low_price_alert_triggered", False)
        and not analysis.get("collection_failures")
    )


def _log_subscription_failure(sub: dict, *, source_errors=None, reason: str | None = None) -> None:
    message = _format_source_failure_reason(source_errors, reason)
    safe_log(
        f"[订阅处理失败] 订阅={_subscription_label(sub)} "
        f"航线={_subscription_route_label(sub)} 原因={message}"
    )


def _notify_subscription_failure(sub: dict, *, source_errors=None, reason: str | None = None) -> bool:
    notification_goals = normalize_notification_goals(sub.get("notification_goals"))
    method = notification_goals["method"]
    email = notification_goals["email"]
    failure_reason = redact_text(_format_source_failure_reason(source_errors, reason))
    route_label = _subscription_route_label(sub)
    content = (
        f"本次采集失败: {route_label}<br>"
        f"原因: {failure_reason}<br>"
        "订阅已保留,下轮自动重试。"
    )
    sent = False
    try:
        if method in {"email", "both"} and email:
            sent = send_email(
                email,
                f"【航班监控采集失败】{route_label}",
                content,
                {},
            ) or sent
        if method in {"pushplus", "both"}:
            sent = send(
                content,
                title=f"【航班监控采集失败】{route_label}",
            ) or sent
    except Exception as exc:
        safe_log(f"[失败通知] 发送失败: {type(exc).__name__}: {exc}")
    if not sent:
        sub["last_failure"] = {
            "route": route_label,
            "reason": failure_reason,
            "created_at": datetime.now().isoformat(timespec="seconds"),
        }
        safe_log(f"[失败通知] 未发送,已记录状态 订阅={_subscription_label(sub)} 原因={failure_reason}")
    return sent

def _notify_system_alert(subscriptions: list[dict], title: str, content: str) -> bool:
    """Send one operational alert through the first configured subscription channel."""
    for sub in subscriptions or []:
        goals = normalize_notification_goals(sub.get("notification_goals"))
        method = goals["method"]
        email = goals["email"]
        if method == "page_only":
            continue
        sent = False
        try:
            if method in {"email", "both"} and email:
                sent = send_email(email, title, content, {}) or sent
            if method in {"pushplus", "both"}:
                sent = send(content, title=title) or sent
        except Exception as exc:
            safe_log(f"[篮子哨兵] 通知渠道失败 原因={type(exc).__name__}:{exc}")
        if sent:
            return True
    safe_log("[篮子哨兵] 无可用通知渠道")
    return False


def _run_basket_sentinel_for_main(
    subscriptions: list[dict],
    *,
    now: datetime | None = None,
) -> dict:
    threshold = os.getenv("BASKET_SENTINEL_AFTER", "20:00")
    try:
        return run_basket_sentinel(
            usage_payload=load_usage(API_USAGE_PATH),
            now=now,
            threshold=threshold,
            state_path=BASKET_SENTINEL_STATE_PATH,
            notifier=lambda title, content: _notify_system_alert(
                subscriptions,
                title,
                content,
            ),
        )
    except Exception as exc:
        safe_log(f"[篮子哨兵] 检查失败 原因={type(exc).__name__}:{exc}")
        return {"due": False, "notified": False, "status": "error"}





def _make_round_id(sub: dict) -> str:
    stamp = datetime.now().strftime("%Y%m%dT%H%M%S%f")
    raw_suffix = sub.get("_index") or sub.get("index") or sub.get("id") or "sub"
    suffix = re.sub(r"[^0-9A-Za-z_-]+", "_", str(raw_suffix)).strip("_")[:80]
    return f"{stamp}_{suffix or 'sub'}"


def _make_collection_round_id() -> str:
    return datetime.now().strftime("collection_%Y%m%dT%H%M%S%f")


def _collection_plan_log_options() -> dict:
    settings = load_collection_settings(CONFIG_PATH)
    return {
        "quota_budgets": settings["source_quota_budget"],
        "quota_low_remaining_threshold": settings[
            "source_quota_low_remaining_threshold"
        ],
        "usage_snapshot": usage_snapshot(load_usage(API_USAGE_PATH)),
        "freshness_hours": settings["freshness_hours"],
        "fresh_scope": settings["sub_round_fresh_scope"],
    }

def _first_airport(codes, fallback):
    values = [str(code).strip().upper() for code in (codes or []) if str(code or "").strip()]
    return values[0] if values else str(fallback or "").strip().upper()

def _normalize_price_scope(value) -> str:
    text = str(value or "per_person").strip().lower()
    if text in {"all", "total", "all_passengers", "all_passenger", "overall", "??", "??", "???"}:
        return "all"
    return "per_person"


def _subscription_passengers(sub: dict):
    if not isinstance(sub, dict):
        return None
    soft = sub.get("soft_preferences") or {}
    preferences = sub.get("preferences") or {}
    passengers = sub.get("passengers") or soft.get("passengers") or preferences.get("passengers")
    if isinstance(passengers, dict) and any(passengers.values()):
        return passengers
    _count, normalized = get_total_passengers(sub)
    return normalized if isinstance(normalized, dict) else None


def _clean_airport_codes(codes) -> list[str]:
    if isinstance(codes, str):
        codes = [codes]
    return [str(code).strip().upper() for code in (codes or []) if str(code or "").strip()]


def _resolved_airport_codes(location_info: dict) -> list[str]:
    """只接受 resolve_location 从 AIRPORTS 主字典推导出的 IATA。"""
    return [
        code
        for code in _clean_airport_codes((location_info or {}).get("airports"))
        if code in AIRPORTS
    ]


def _subscription_airports(sub: dict, active_key: str, all_key: str, fallback_key: str) -> list[str]:
    basic = sub.get("basic") or {}
    active = _clean_airport_codes(sub.get(active_key) or basic.get(active_key))
    all_codes = _clean_airport_codes(sub.get(all_key) or basic.get(all_key))
    if not all_codes and all_key == "destination_airports":
        all_codes = _clean_airport_codes(basic.get("dest_airports"))
    fallback = _clean_airport_codes([sub.get(fallback_key)])
    if active:
        if all_codes:
            filtered = [code for code in active if code in all_codes]
            return filtered or active
        return active
    return all_codes or fallback


def _subscription_route_type(sub: dict, origins: list[str], destinations: list[str]) -> str:
    basic = sub.get("basic") or {}
    constraints = sub.get("constraints") or {}
    explicit = (
        basic.get("route_type")
        or sub.get("route_type")
        or constraints.get("route_type")
    )
    origin = _first_airport(origins, sub.get("origin"))
    dest = _first_airport(destinations, sub.get("destination"))
    return route_type_for(origin, dest, explicit)


def _flight_airport_value(flight: dict, kind: str) -> str:
    if not isinstance(flight, dict):
        return ""
    if kind == "departure":
        keys = ("departure_airport", "dep_airport", "origin", "from")
        search_key = "search_origin"
        segment_keys = ("dep_airport", "departure_airport", "origin")
        segment_index = 0
    else:
        keys = ("arrival_airport", "arr_airport", "destination", "to")
        search_key = "search_destination"
        segment_keys = ("arr_airport", "arrival_airport", "destination")
        segment_index = -1
    for key in keys:
        value = str(flight.get(key) or "").strip().upper()
        if value:
            return value
    segments = flight.get("segments") or flight.get("flights") or flight.get("legs") or []
    if isinstance(segments, list) and segments:
        segment = segments[segment_index] if isinstance(segments[segment_index], dict) else {}
        for key in segment_keys:
            value = str(segment.get(key) or "").strip().upper()
            if value:
                return value
    return str(flight.get(search_key) or "").strip().upper()


def _filter_data_to_airports(data: dict | None, origins: list[str], destinations: list[str]) -> dict | None:
    if not data:
        return data
    active_origins = set(_clean_airport_codes(origins))
    active_dests = set(_clean_airport_codes(destinations))
    filtered = []
    for flight in data.get("flights", []) or []:
        dep = _flight_airport_value(flight, "departure")
        arr = _flight_airport_value(flight, "arrival")
        if dep and active_origins and dep not in active_origins:
            continue
        if arr and active_dests and arr not in active_dests:
            continue
        filtered.append(flight)
    data = dict(data)
    data["flights"] = filtered
    data["total_count"] = len(filtered)
    if isinstance(data.get("source_stats"), dict):
        data["source_stats"]["after_active_airport_filter"] = len(filtered)
    return data


def _calendar_source_for_route(aggregator: FlightAggregator, origin: str, dest: str):
    sources = aggregator._ordered_search_sources(origin, dest)
    if is_domestic_route(origin, dest):
        for source in sources:
            if str(getattr(source, "name", "")).lower() == "juhe":
                return source
    return sources[0] if sources else None


def _price_hint_route_candidates(origin: str, dest: str) -> list[str]:
    origin_info = resolve_location(origin)
    dest_info = resolve_location(dest)
    if origin_info.get("type") == "unknown" or dest_info.get("type") == "unknown":
        return []
    routes = []
    for origin_code in origin_info.get("airports") or []:
        for dest_code in dest_info.get("airports") or []:
            if origin_code not in AIRPORTS or dest_code not in AIRPORTS:
                continue
            routes.extend(
                [
                    f"{origin_code}-{dest_code}",
                    f"{origin_code}_{dest_code}",
                    f"{origin_code}→{dest_code}",
                ]
            )
    return routes


def price_hint_for_route(origin: str, dest: str, *, data_dir: Path | None = None) -> dict:
    """Read local low-price calendar data and return a form-friendly anchor."""
    for route in _price_hint_route_candidates(origin, dest):
        calendar = load_calendar(route, data_dir=data_dir)
        hint = build_price_hint_from_calendar(calendar)
        if hint.get("has_data"):
            hint["route"] = route
            return hint
    return {
        "has_data": False,
        "low": None,
        "high": None,
        "typical": None,
        "sample_count": 0,
        "scope": "oneway",
    }


def _as_bool(value) -> bool:
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        return value.strip().lower() in {"true", "1", "yes", "y", "on"}
    return bool(value)


def _valid_price(value) -> bool:
    try:
        return float(value) > 0
    except (TypeError, ValueError):
        return False


def _map_transfer_policy(policy: str | None) -> str:
    mapping = {
        "direct_only": "must",
        "must": "must",
        "reasonable": "flexible",
        "short_ok": "flexible",
        "flexible": "flexible",
        "cheap_ok": "cheap_ok",
        "price_first": "cheap_ok",
    }
    return mapping.get(policy or "", "flexible")


def _map_red_eye_policy(policy: str | None) -> str:
    mapping = {
        "not_allowed": "reject",
        "reject": "reject",
        "allowed": "flexible",
        "accept": "flexible",
        "flexible": "flexible",
        "cheap_ok": "cheap_ok",
    }
    return mapping.get(policy or "", "reject")


def _departure_policy_from_legacy(policy: str | None) -> str:
    if policy in {"not_allowed", "reject", "cheap_ok"}:
        return "no_redeye"
    if policy in {"allowed", "accept", "flexible"}:
        return "any"
    return "after_06"


def _normalize_goals(notification_goals: dict | None, legacy_goals) -> list[str]:
    goal_aliases = {
        "price_drop_alert": "price_drop_alert",
        "price_target": "price_drop_alert",
        "low_price_alert": "price_drop_alert",
        "buy_timing": "buy_timing",
        "price_risk_alert": "buy_timing",
        "price_rise_alert": "buy_timing",
        "cheaper_date": "cheaper_date",
        "best_overall": "best_overall",
        "better_same_day": "best_overall",
    }

    raw_goals = []
    if isinstance(notification_goals, dict):
        primary = notification_goals.get("primary")
        secondary = notification_goals.get("secondary", [])
        if primary:
            raw_goals.append(primary)
        if isinstance(secondary, list):
            raw_goals.extend(secondary)
    if isinstance(legacy_goals, list):
        raw_goals.extend(legacy_goals)

    normalized = []
    for goal in raw_goals:
        mapped = goal_aliases.get(goal)
        if mapped and mapped not in normalized:
            normalized.append(mapped)
    return normalized


def _invalid_location_subscription(
    item: dict,
    *,
    origin_value,
    destination_value,
    origin_info: dict,
    destination_info: dict,
) -> dict:
    invalid_values = []
    if origin_info.get("type") == "unknown":
        invalid_values.append(("出发地", str(origin_value or "").strip()))
    if destination_info.get("type") == "unknown":
        invalid_values.append(("目的地", str(destination_value or "").strip()))
    if len(invalid_values) == 1:
        invalid_reason = f"地点无法解析(输入={invalid_values[0][1] or '空'})"
    else:
        details = "、".join(f"{label}输入={value or '空'}" for label, value in invalid_values)
        invalid_reason = f"地点无法解析({details})"

    basic = item.get("basic") if isinstance(item.get("basic"), dict) else {}
    invalid = dict(item)
    invalid.update(
        {
            "_index": item.get("_index", item.get("index")),
            "name": item.get("name") or "网页订阅",
            "origin": origin_value,
            "destination": destination_value,
            "depart_date": item.get("depart_date") or basic.get("depart_date") or "",
            "status": "invalid",
            "validation_status": "invalid",
            "invalid_reason": invalid_reason,
            "validation_errors": [
                location_error_message("origin", origin_info)
                for _label, _value in invalid_values
                if _label == "出发地"
            ]
            + [
                location_error_message("destination", destination_info)
                for _label, _value in invalid_values
                if _label == "目的地"
            ],
        }
    )
    return invalid


def _normalize_subscription(item: dict) -> dict:
    from cabin_allocation import find_explicit_cabin_allocation, validate_cabin_allocation

    item = migrate_old_subscription(item)
    hard_constraints = dict(item.get("hard_constraints") or {})
    soft_preferences = dict(item.get("soft_preferences") or {})
    basic = dict(item.get("basic") or {})
    constraints = dict(item.get("constraints") or {})
    preferences = dict(item.get("preferences") or {})
    advanced_rules = dict(item.get("advanced_rules") or {})
    lcc_policy = resolve_lcc_policy(item)
    if not lcc_policy:
        lcc_policy = "any"
        safe_log(
            "[口径迁移] "
            f"订阅={item.get('name') or item.get('_index') or item.get('index') or '未知'} "
            "缺少lcc_policy，按any处理"
        )
    passenger_count, passengers = get_total_passengers(item)
    basic["passenger_count"] = passenger_count
    preferences["passenger_count"] = passenger_count
    if passengers:
        preferences["passengers"] = passengers
        soft_preferences["passengers"] = passengers
    soft_preferences["passenger_count"] = passenger_count
    explicit_allocation = find_explicit_cabin_allocation(
        item,
        hard_constraints,
        constraints,
        preferences,
        soft_preferences,
    )
    allocation_result = None
    if explicit_allocation is not None:
        allocation_result = validate_cabin_allocation(explicit_allocation, passengers)
        explicit_allocation = allocation_result["allocation"]
        for container in (hard_constraints, constraints):
            container["cabin_arrangement"] = "mixed"
            container["cabin_allocation"] = explicit_allocation
            container["business_seats"] = allocation_result["business_seats"]
            container["economy_seats"] = allocation_result["economy_seats"]
    notification_goals = normalize_notification_goals(
        item.get("notification_goals"),
        logger=safe_log,
    )
    return_date = item.get("return_date") or hard_constraints.get("return_date")
    same_day_round_trip = bool(
        hard_constraints.get("same_day_round_trip")
        or constraints.get("same_day_round_trip")
        or item.get("same_day_round_trip")
    )
    if same_day_round_trip and not return_date:
        return_date = item.get("depart_date", "")
    round_trip = _as_bool(item.get("round_trip", hard_constraints.get("round_trip", False))) or same_day_round_trip
    origin_value = item.get("origin") or basic.get("origin") or ""
    destination_value = (
        item.get("destination")
        or basic.get("destination")
        or basic.get("dest")
        or ""
    )
    origin_info = resolve_location(origin_value)
    destination_info = resolve_location(destination_value)
    if origin_info.get("type") == "unknown" or destination_info.get("type") == "unknown":
        return _invalid_location_subscription(
            item,
            origin_value=origin_value,
            destination_value=destination_value,
            origin_info=origin_info,
            destination_info=destination_info,
        )
    # 旧订阅可能残留错误机场列表；全部机场必须由地点解析结果重新推导。
    origin_airports = _resolved_airport_codes(origin_info)
    destination_airports = _resolved_airport_codes(destination_info)
    if not origin_airports:
        raise ValueError(location_error_message("origin", origin_info))
    if not destination_airports:
        raise ValueError(location_error_message("destination", destination_info))
    origin_airports_active = _clean_airport_codes(
        item.get("origin_airports_active")
        or basic.get("origin_airports_active")
        or origin_airports
    )
    destination_airports_active = (
        _clean_airport_codes(
            item.get("destination_airports_active")
            or basic.get("destination_airports_active")
            or basic.get("dest_airports_active")
            or destination_airports
        )
    )
    origin_airports_active = [
        code for code in origin_airports_active if code in origin_airports
    ] or origin_airports
    destination_airports_active = [
        code for code in destination_airports_active if code in destination_airports
    ] or destination_airports
    explicit_route_type = (
        basic.get("route_type")
        or item.get("route_type")
        or constraints.get("route_type")
        or hard_constraints.get("route_type")
    )
    route_type = route_type_for(
        origin_airports_active[0],
        destination_airports_active[0],
        explicit_route_type,
    )
    basic["route_type"] = route_type
    constraints["route_type"] = route_type
    hard_constraints["route_type"] = route_type
    origin_airport_preference = hard_constraints.get(
        "origin_airport_preference", item.get("origin_airport_preference", "all")
    )
    if origin_airport_preference and origin_airport_preference != "all":
        preferred_origin = str(origin_airport_preference).strip().upper()
        if preferred_origin in origin_airports:
            origin_airports_active = [preferred_origin]

    legacy_budget = hard_constraints.get("budget", item.get("budget"))
    max_budget = hard_constraints.get(
        "max_budget", item.get("max_budget", legacy_budget)
    )
    max_budget_mode = hard_constraints.get(
        "max_budget_mode",
        item.get("max_budget_mode", "fixed" if max_budget else "none"),
    )
    target_price = soft_preferences.get("target_price", item.get("target_price"))
    target_price_mode = soft_preferences.get(
        "target_price_mode",
        item.get("target_price_mode", "fixed" if target_price else "auto"),
    )
    max_budget_scope = _normalize_price_scope(
        hard_constraints.get("max_budget_scope")
        or constraints.get("max_budget_scope")
        or preferences.get("max_budget_scope")
        or soft_preferences.get("max_budget_scope")
        or item.get("max_budget_scope")
        or item.get("budget_scope")
    )
    target_price_scope = _normalize_price_scope(
        hard_constraints.get("target_price_scope")
        or constraints.get("target_price_scope")
        or preferences.get("target_price_scope")
        or soft_preferences.get("target_price_scope")
        or item.get("target_price_scope")
        or max_budget_scope
    )
    cabin_arrangement = str(
        hard_constraints.get("cabin_arrangement")
        or constraints.get("cabin_arrangement")
        or item.get("cabin_arrangement")
        or "economy_all"
    ).strip()
    if allocation_result is not None or cabin_arrangement == "business_all":
        max_budget_scope = "all"
        target_price_scope = "all"
        for container in (hard_constraints, constraints, preferences, soft_preferences):
            container["budget_scope"] = "all"
            container["max_budget_scope"] = "all"
            container["target_price_scope"] = "all"
    transfer_policy = hard_constraints.get(
        "transfer_policy", item.get("transfer_policy", item.get("direct_only"))
    )
    red_eye_policy = hard_constraints.get(
        "red_eye_policy", item.get("red_eye_policy", item.get("red_eye"))
    )
    departure_time_policy = hard_constraints.get(
        "departure_time_policy",
        item.get(
            "departure_time_policy",
            _departure_policy_from_legacy(red_eye_policy),
        ),
    )
    arrival_time_policy = hard_constraints.get(
        "arrival_time_policy", item.get("arrival_time_policy", "any")
    )
    preferred_departure_slots = hard_constraints.get(
        "preferred_departure_slots", item.get("preferred_departure_slots")
    )
    preferred_arrival_slots = hard_constraints.get(
        "preferred_arrival_slots", item.get("preferred_arrival_slots")
    )
    departure_slots = hard_constraints.get("departure_slots", item.get("departure_slots"))
    arrival_slots = hard_constraints.get("arrival_slots", item.get("arrival_slots"))
    outbound_departure_slots = hard_constraints.get(
        "outbound_departure_slots", item.get("outbound_departure_slots")
    )
    outbound_arrival_slots = hard_constraints.get(
        "outbound_arrival_slots", item.get("outbound_arrival_slots")
    )
    return_departure_slots = hard_constraints.get(
        "return_departure_slots", item.get("return_departure_slots")
    )
    return_arrival_slots = hard_constraints.get(
        "return_arrival_slots", item.get("return_arrival_slots")
    )
    baggage = hard_constraints.get("baggage", item.get("need_baggage", "unknown"))
    refund_flexibility = soft_preferences.get(
        "refund_flexibility",
        hard_constraints.get("refund_flexibility", item.get("refund_flexibility", "unknown")),
    )
    exclude_airlines = soft_preferences.get(
        "exclude_airlines",
        hard_constraints.get("exclude_airlines", item.get("exclude_airlines", [])),
    )
    if isinstance(exclude_airlines, str):
        exclude_airlines = [
            value.strip()
            for value in exclude_airlines.replace("，", ",").split(",")
            if value.strip()
        ]

    hard_constraints_for_cabins = {
        **hard_constraints,
        **constraints,
        "passenger_count": passenger_count,
    }
    normalized = {
        "id": item.get("id"),
        "subscription_id": item.get("subscription_id"),
        "_index": item.get("_index", item.get("index")),
        "name": item.get("name") or "网页订阅",
        "origin": origin_info["value"],
        "origin_type": origin_info["type"],
        "origin_airports": origin_airports,
        "origin_airports_active": origin_airports_active,
        "origin_airport_preference": origin_airport_preference,
        "destination": destination_info["value"],
        "destination_type": destination_info["type"],
        "destination_airports": destination_airports,
        "destination_airports_active": destination_airports_active,
        "route_type": route_type,
        "monitor_mode": item.get("monitor_mode", "quick"),
        "depart_date": item.get("depart_date", ""),
        "budget": max_budget,
        "budget_mode": max_budget_mode,
        "max_budget": max_budget,
        "max_budget_mode": max_budget_mode,
        "target_price": target_price,
        "target_price_mode": target_price_mode,
        "budget_scope": max_budget_scope,
        "max_budget_scope": max_budget_scope,
        "target_price_scope": target_price_scope,
        "return_date": return_date,
        "round_trip": round_trip,
        "date_flexibility": item.get("date_flexibility", 0),
        "return_date_flexibility": item.get("return_date_flexibility", 0),
        "direct_only": _map_transfer_policy(transfer_policy),
        "transfer_policy": transfer_policy or "reasonable",
        "max_extra_duration_hours": hard_constraints.get(
            "max_extra_duration_hours", item.get("max_extra_duration_hours")
        ),
        "max_total_duration_hours": hard_constraints.get(
            "max_total_duration_hours", item.get("max_total_duration_hours")
        ),
        "red_eye": _map_red_eye_policy(red_eye_policy),
        "red_eye_policy": red_eye_policy or "not_allowed",
        "departure_time_policy": departure_time_policy,
        "arrival_time_policy": arrival_time_policy,
        "preferred_departure_slots": preferred_departure_slots,
        "preferred_arrival_slots": preferred_arrival_slots,
        "departure_slots": departure_slots,
        "arrival_slots": arrival_slots,
        "outbound_departure_slots": outbound_departure_slots,
        "outbound_arrival_slots": outbound_arrival_slots,
        "return_departure_slots": return_departure_slots,
        "return_arrival_slots": return_arrival_slots,
        "need_baggage": baggage,
        "refund_flexibility": refund_flexibility,
        "airline_policy": soft_preferences.get(
            "airline_policy",
            hard_constraints.get("airline_policy", item.get("airline_policy", "any")),
        ),
        "lcc_policy": str(lcc_policy).strip(),
        "exclude_airlines": exclude_airlines if isinstance(exclude_airlines, list) else [],
        "trip_type": soft_preferences.get("trip_type", item.get("trip_type", "tourism")),
        "companions": soft_preferences.get("companions", item.get("companions", "solo")),
        "price_sensitivity": soft_preferences.get(
            "price_sensitivity", item.get("price_sensitivity", "low")
        ),
        "trip_rigidity": soft_preferences.get(
            "trip_rigidity", item.get("trip_rigidity", "confirmed")
        ),
        "goals": _normalize_goals(notification_goals, item.get("goals", [])),
        "notification_goals": notification_goals,
        "basic": basic,
        "constraints": constraints,
        "preferences": preferences,
        "advanced_rules": advanced_rules,
        "hard_constraints": hard_constraints,
        "soft_preferences": soft_preferences,
        "mode": item.get("mode", "balanced"),
        "cabin_classes": (
            determine_cabins(hard_constraints_for_cabins)
            if allocation_result is not None
            else item.get("cabin_classes") or determine_cabins(hard_constraints_for_cabins)
        ),
        "priorities": item.get("priorities"),
    }
    if allocation_result is not None:
        normalized["cabin_allocation"] = explicit_allocation
    normalized = apply_default_rules(normalized)
    normalized["goals"] = _normalize_goals(
        normalized.get("notification_goals"), normalized.get("goals", [])
    )
    return normalized


def normalize_subscription(item: dict) -> dict:
    """Public wrapper used by web_form for immediate single-subscription runs."""
    return _normalize_subscription(item)


def _message_subject(html_content: str, fallback: str = "航班监控通知") -> str:
    text = re.sub(r"<[^>]+>", "", html_content or "").replace("&nbsp;", " ").strip()
    first_line = re.split(r"(?:<br>|\n)", text, maxsplit=1)[0].strip()
    return first_line[:100] if first_line else fallback


def _email_subject(html_content: str, route_info: dict) -> str:
    subject = _message_subject(html_content)
    target = route_info.get("target_price")
    try:
        target_value = float(target) if target else None
    except (TypeError, ValueError):
        target_value = None
    if target_value:
        if route_info.get("round_trip"):
            target_value *= 2
        subject = f"{subject} (理想¥{target_value:,.0f})"
    return subject[:120]


def _save_result_for_page(
    subscription_id: str,
    html_content: str,
    payload: dict | None = None,
) -> bool:
    canonical_id = canonical_detail_uuid(subscription_id)
    if canonical_id is None:
        safe_log(f"[详情存储拒绝] 非UUID订阅标识={subscription_id!r}")
        return False
    PAGE_PAYLOADS_DIR.mkdir(parents=True, exist_ok=True)
    payload_path = PAGE_PAYLOADS_DIR / f"{canonical_id}.json"
    record = {
        "subscription_id": canonical_id,
        "created_at": datetime.now().isoformat(timespec="seconds"),
        "html": html_content,
        "payload": payload or {},
    }
    payload_path.write_text(
        json.dumps(record, ensure_ascii=False, indent=2, default=str),
        encoding="utf-8",
    )
    safe_log(f"[详情存储] 保存订阅 {canonical_id} 的payload到 {payload_path}")
    try:
        _upload_payload_to_pythonanywhere(payload_path)
    except Exception as exc:
        safe_log(f"[详情同步失败] {type(exc).__name__}: {exc}")
        logging.warning(
            f"详情payload已本地保存但同步PythonAnywhere失败: {exc}",
            exc_info=True,
        )
    return True


def _upload_payload_to_pythonanywhere(payload_path: Path) -> bool:
    if httpx is None:
        print("[详情同步] 未安装httpx,跳过远程payload同步")
        return False
    token = os.environ.get("PYTHONANYWHERE_TOKEN", "").strip()
    user = os.environ.get("PYTHONANYWHERE_USER", "").strip() or "ljs96824"
    if not token or token.lower() in {"token", "your_token"} or "your" in token.lower():
        print("[详情同步] 未配置PYTHONANYWHERE_TOKEN,跳过远程payload同步")
        return False
    remote_path = PYTHONANYWHERE_PAYLOAD_PATH.format(user=user, filename=payload_path.name)
    url = f"https://www.pythonanywhere.com/api/v0/user/{user}/files/path{remote_path}"
    with payload_path.open("rb") as file_obj:
        response = httpx.post(
            url,
            headers={"Authorization": f"Token {token}"},
            files={"content": (payload_path.name, file_obj, "application/json")},
            timeout=30,
        )
    response.raise_for_status()
    print(f"[详情同步] 已上传payload到PythonAnywhere: {remote_path}")
    return True


def _fallback_cache_path(origins: list[str], dests: list[str], date_str: str, cabin_classes=None) -> Path:
    cache_dir = DATA_DIR / "cache"
    cache_dir.mkdir(exist_ok=True)
    cabin_key = "_".join(cabin_classes or []) if isinstance(cabin_classes, list) else str(cabin_classes or "economy")
    raw = f"same_day_fallback_{'-'.join(origins)}_{'-'.join(dests)}_{date_str}_{cabin_key}"
    return cache_dir / f"{sanitize_filename(raw)}.json"


def _fresh_cached_flights(path: Path, max_age_hours: int = 6) -> list[dict] | None:
    if not path.exists():
        return None
    try:
        cached = json.loads(path.read_text(encoding="utf-8"))
        updated_at = datetime.fromisoformat(str(cached.get("updated_at")))
        if datetime.now() - updated_at <= timedelta(hours=max_age_hours):
            print(f"[当天往返备选] 使用缓存 {path}")
            return cached.get("flights") or []
    except Exception as exc:
        print(f"[当天往返备选] 缓存读取失败: {exc}")
    return None


def _collect_same_day_fallback_flights(
    agg,
    origins: list[str],
    dests: list[str],
    date_str: str,
    cabin_classes=None,
    route_type: str | None = None,
    passengers=None,
    request_reason: str | None = None,
) -> list[dict]:
    cache_path = _fallback_cache_path(origins, dests, date_str, cabin_classes)
    cached = _fresh_cached_flights(cache_path)
    if cached is not None:
        return cached
    data = collect_for_airport_matrix(
        agg,
        origins,
        dests,
        date_str,
        cabin_classes=cabin_classes,
        route_type=route_type,
        passengers=passengers,
        request_reason=request_reason,
    )
    flights = [
        _normalize_detail_flight(flight, flight.get("data_source") or flight.get("source"))
        for flight in (data or {}).get("flights", [])
    ]
    flights = _filter_data_to_airports({"flights": flights}, origins, dests).get("flights", [])
    flights = [flight for flight in flights if _valid_price(flight.get("price"))]
    cache_path.write_text(
        json.dumps(
            {
                "updated_at": datetime.now().isoformat(timespec="seconds"),
                "date": date_str,
                "flights": flights,
            },
            ensure_ascii=False,
            indent=2,
            default=str,
        ),
        encoding="utf-8",
    )
    print(f"[当天往返备选] 已缓存 {len(flights)} 个航班到 {cache_path}")
    return flights


def _subscription_identifier(sub: dict, route: str) -> str:
    for value in (
        sub.get("subscription_id"),
        sub.get("id"),
        sub.get("_index"),
    ):
        if value is not None and str(value).strip():
            return str(value)
    return f"{route}|{sub.get('depart_date', '')}|{sub.get('return_date', '')}"


def _deliver_notification(sub: dict, route: str, message_kwargs: dict) -> bool:
    try:
        notification_goals = normalize_notification_goals(sub.get("notification_goals"))
        method = notification_goals["method"]
        email = notification_goals["email"]
        subscription_id = _subscription_identifier(sub, route)
        route_info = dict(message_kwargs.get("route_info") or {})
        route_info["subscription_id"] = subscription_id
        message_kwargs = {**message_kwargs, "route_info": route_info}

        print("[推送] 开始构建payload")
        payload = build_notification_payload(
            analysis_result=message_kwargs.get("analysis_result"),
            outbound_analysis=message_kwargs.get("outbound_analysis"),
            return_analysis=message_kwargs.get("return_analysis"),
            route_info=route_info,
            subscription=sub,
            price_history=route_info.get("lowest_price_history"),
            source_stats=message_kwargs.get("source_stats"),
            price_insights=message_kwargs.get("price_insights"),
        )
        feedback_ack = feedback_acknowledgement(subscription_id)
        if feedback_ack:
            payload["feedback_ack"] = feedback_ack
        delivery_payload = delivery_payload_with_detail_token(payload)
        print("[推送] payload构建完成")

        print("[推送] 开始渲染邮件/详情HTML")
        if method in {"email", "both"}:
            print("[推送] 邮件方式已启用，开始生成折线图PNG/邮件HTML")
        email_rendered = render_email(delivery_payload)
        if len(email_rendered) == 3:
            subject, full_html, inline_images = email_rendered
        else:
            subject, full_html = email_rendered
            inline_images = {}
        print("[推送] 邮件/详情HTML渲染完成")
        detail_html = render_detail_html(payload)
        page_saved = _save_result_for_page(subscription_id, detail_html, payload)

        if method == "page_only":
            print("[推送] 开始保存页面结果")
            print("[推送] 用户选择仅页面查看，已保存页面结果")
            return page_saved

        sent = False
        if method in {"email", "both"}:
            if not email:
                print("[邮件] 用户选择邮箱推送但未填写邮箱")
            else:
                print("[推送] 开始发送邮件")
                sent = send_email(
                    email,
                    subject,
                    full_html,
                    inline_images,
                ) or sent
                print(f"[推送] 邮件发送完成: sent={sent}")

        if method in {"pushplus", "both"}:
            print("[推送] 开始渲染PushPlus短版")
            push_content = render_pushplus_sections(delivery_payload)
            print("[推送] PushPlus短版渲染完成，开始发送")
            sent = send(
                push_content,
                title=f"【{payload.get('push_type', '价格提醒')}】{payload.get('route', route)}",
            ) or sent
            print(f"[推送] PushPlus发送完成: sent={sent}")

        if method not in {"email", "pushplus", "both", "page_only"}:
            print(f"[推送] 未识别的推送方式 {method!r}，按PushPlus兜底")
            push_content = render_pushplus_sections(delivery_payload)
            sent = send(
                push_content,
                title=f"【{payload.get('push_type', '价格提醒')}】{payload.get('route', route)}",
            ) or sent
            print(f"[推送] 兜底PushPlus发送完成: sent={sent}")

        if sent:
            print("[推送] 开始保存推送payload/页面详情")
            try:
                persist_notification_payload(payload)
            except Exception as exc:
                print(f"[推送存档失败] {type(exc).__name__}: {exc}")
                print(traceback.format_exc())
                logging.warning(f"{route} 推送已发送但存档失败: {exc}", exc_info=True)
            print("[推送] 发送完成")
        else:
            print(f"[推送] 发送结果为False，method={method}, email={'yes' if email else 'no'}")

        return sent
    except Exception as e:
        print(f"[推送失败] {type(e).__name__}: {e}")
        print(traceback.format_exc())
        return False


def load_file_subscriptions() -> list[dict]:
    if not SUBSCRIPTIONS_PATH.exists():
        return []
    try:
        subscriptions = json.loads(SUBSCRIPTIONS_PATH.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        logging.error(f"subscriptions.json 解析失败: {exc}")
        return []
    if not isinstance(subscriptions, list):
        logging.error("subscriptions.json 格式错误，应为订阅数组")
        return []

    active = []
    skipped = []
    for index, item in enumerate(subscriptions):
        if not isinstance(item, dict):
            continue
        if item.get("status", "active") != "active":
            route = f"{item.get('origin') or (item.get('basic') or {}).get('origin', '')}→{item.get('destination') or (item.get('basic') or {}).get('destination', '')}"
            sub_id = item.get("id") or item.get("index") or item.get("_index") or index
            print(f"[跳过] 订阅已暂停: {sub_id} {route}")
            continue
        try:
            if not item.get("_index"):
                item = {**item, "_index": index}
            active.append(_normalize_subscription(item))
        except Exception as exc:
            sub_id = item.get("id") or item.get("index") or item.get("_index") or "未知"
            skipped.append({"id": sub_id, "error": str(exc)})
            print(f"[订阅跳过] 订阅{sub_id} 规范化失败: {exc}")
            logging.warning(f"订阅{sub_id} 规范化失败，已跳过: {exc}")
            continue
    if skipped:
        print(f"[订阅汇总] 跳过{len(skipped)}条无效订阅,正常处理{len(active)}条")
    return [
        sub
        for sub in active
        if sub.get("validation_status") == "invalid"
        or (sub.get("origin") and sub.get("destination") and sub.get("depart_date"))
    ]


def _reference_cabin_classes(sub: dict | None) -> list[str]:
    """日历、弹性日期与备选只采经济舱；商务舱仅限主日期对。"""
    cabins = (sub or {}).get("cabin_classes") or ["economy"]
    if isinstance(cabins, str):
        cabins = [cabins]
    return ["economy"] if "economy" in cabins else [str(cabins[0])]


def subscription_preferences(sub: dict) -> dict:
    soft = sub.get("soft_preferences") or {}
    preferences = sub.get("preferences") or {}
    basic = sub.get("basic") or {}
    hard = sub.get("hard_constraints") or {}
    constraints = sub.get("constraints") or {}
    travel_scenarios = soft.get("travel_scenarios") or sub.get("travel_scenarios")
    if isinstance(travel_scenarios, str):
        travel_scenarios = [item.strip() for item in travel_scenarios.split(",") if item.strip()]
    if not travel_scenarios:
        travel_scenario = soft.get("travel_scenario") or sub.get("travel_scenario")
        travel_scenarios = [travel_scenario] if travel_scenario else []
    return {
        "direct_only": sub.get("direct_only", "flexible"),
        "transfer_policy": sub.get("transfer_policy", "reasonable"),
        "red_eye": sub.get("red_eye", "reject"),
        "red_eye_policy": sub.get("red_eye_policy", "not_allowed"),
        "departure_time_policy": sub.get("departure_time_policy", "after_06"),
        "arrival_time_policy": sub.get("arrival_time_policy", "any"),
        "preferred_departure_slots": sub.get("preferred_departure_slots"),
        "preferred_arrival_slots": sub.get("preferred_arrival_slots"),
        "departure_slots": sub.get("departure_slots"),
        "arrival_slots": sub.get("arrival_slots"),
        "outbound_departure_slots": sub.get("outbound_departure_slots"),
        "outbound_arrival_slots": sub.get("outbound_arrival_slots"),
        "return_departure_slots": sub.get("return_departure_slots"),
        "return_arrival_slots": sub.get("return_arrival_slots"),
        "need_baggage": sub.get("need_baggage", "unknown"),
        "refund_flexibility": sub.get("refund_flexibility", "unknown"),
        "trip_type": sub.get("trip_type", "tourism"),
        "companions": sub.get("companions", "solo"),
        "passengers": preferences.get("passengers"),
        "passenger_count": basic.get("passenger_count") or preferences.get("passenger_count"),
        "travel_scenario": (travel_scenarios[0] if travel_scenarios else "personal"),
        "travel_scenarios": travel_scenarios or ["personal"],
        "price_sensitivity": sub.get("price_sensitivity", "low"),
        "trip_rigidity": sub.get("trip_rigidity", "confirmed"),
        "goals": sub.get("goals", []),
        "budget": sub.get("budget"),
        "budget_mode": sub.get("budget_mode", "fixed"),
        "max_budget": sub.get("max_budget"),
        "max_budget_mode": sub.get("max_budget_mode", "none"),
        "target_price": sub.get("target_price"),
        "target_price_mode": sub.get("target_price_mode", "auto"),
        "budget_scope": sub.get("budget_scope", "per_person"),
        "max_budget_scope": sub.get("max_budget_scope", sub.get("budget_scope", "per_person")),
        "target_price_scope": sub.get("target_price_scope", sub.get("budget_scope", "per_person")),
        "date_flexibility": sub.get("date_flexibility", 0),
        "round_trip": sub.get("round_trip", False),
        "return_date": sub.get("return_date"),
        "return_date_flexibility": sub.get("return_date_flexibility", 0),
        "same_day_round_trip": bool(
            hard.get("same_day_round_trip")
            or constraints.get("same_day_round_trip")
            or sub.get("same_day_round_trip")
        ),
        "business_start": hard.get("business_start") or constraints.get("business_start") or sub.get("business_start"),
        "business_end": hard.get("business_end") or constraints.get("business_end") or sub.get("business_end"),
        "buffer_hours": hard.get("buffer_hours") or constraints.get("buffer_hours") or sub.get("buffer_hours"),
        "transport_mode": hard.get("transport_mode") or constraints.get("transport_mode") or sub.get("transport_mode"),
        "user_transport_min": (
            hard.get("user_transport_min")
            or constraints.get("user_transport_min")
            or sub.get("user_transport_min")
        ),
        "redundancy_min": (
            hard.get("redundancy_min")
            or constraints.get("redundancy_min")
            or sub.get("redundancy_min")
        ),
        "time_source": hard.get("time_source") or constraints.get("time_source") or sub.get("time_source"),
        "airline_policy": sub.get("airline_policy", "any"),
        "lcc_policy": sub.get("lcc_policy", "any"),
        "exclude_airlines": sub.get("exclude_airlines", []),
        "max_extra_duration_hours": sub.get("max_extra_duration_hours"),
        "max_total_duration_hours": sub.get("max_total_duration_hours"),
        "origin_airport_preference": sub.get("origin_airport_preference", "all"),
    }


def _merge_source_stats(target: dict, incoming: dict) -> None:
    for key, value in (incoming or {}).items():
        if key in {"total_raw", "after_dedup", "after_dedup_by_cabin", "enriched_count"}:
            target[key] = target.get(key, 0) + (value or 0) if isinstance(value, int) else value
            continue
        if not isinstance(value, dict):
            target[key] = value
            continue
        current = target.setdefault(
            key,
            {"count": 0, "cabin_counts": {}, "status": value.get("status", "")},
        )
        current["count"] = current.get("count", 0) + int(value.get("count") or 0)
        current["status"] = (
            "成功" if "成功" in str(value.get("status", "")) else current.get("status", value.get("status", ""))
        )
        for cabin, count in (value.get("cabin_counts") or {}).items():
            current.setdefault("cabin_counts", {})
            current["cabin_counts"][cabin] = current["cabin_counts"].get(cabin, 0) + int(count or 0)


def _merge_source_errors(target: list[dict], incoming) -> None:
    seen = {
        (item.get("source"), item.get("cabin_class"), item.get("error"))
        for item in target
        if isinstance(item, dict)
    }
    for item in incoming or []:
        if not isinstance(item, dict):
            continue
        key = (item.get("source"), item.get("cabin_class"), item.get("error"))
        if key in seen:
            continue
        seen.add(key)
        target.append(item)


def _dedupe_flights(flights: list[dict]) -> list[dict]:
    seen = {}
    for flight in flights:
        if not _valid_price(flight.get("price")):
            continue
        key = (
            str(flight.get("flight_combo", "")).replace(" ", "").upper(),
            flight.get("cabin_class") or "economy",
            flight.get("route_summary") or "",
        )
        if key not in seen or float(flight.get("price")) < float(seen[key].get("price")):
            seen[key] = flight
    return sorted(seen.values(), key=lambda item: float(item.get("price") or 999999))



def _aggregator_collect(aggregator, origin, destination, date_str, passengers=None, **kwargs):
    try:
        params = inspect.signature(aggregator.collect).parameters
    except (TypeError, ValueError):
        params = {}
    if "passengers" in params:
        kwargs["passengers"] = passengers
    if "request_reason" not in params:
        kwargs.pop("request_reason", None)
    return aggregator.collect(origin, destination, date_str, **kwargs)

def collect_for_airport_matrix(
    aggregator: FlightAggregator,
    origins: list[str],
    destinations: list[str],
    date_str: str,
    cabin_classes=None,
    route_type: str | None = None,
    passengers=None,
    request_reason: str | None = None,
) -> dict | None:
    origins = _clean_airport_codes(origins)
    destinations = _clean_airport_codes(destinations)
    if not origins or not destinations:
        return None

    if len(origins) == 1 and len(destinations) == 1:
        collect_kwargs = {"cabin_classes": cabin_classes}
        if route_type:
            collect_kwargs["route_type"] = route_type
        data = _aggregator_collect(
            aggregator,
            origins[0],
            destinations[0],
            date_str,
            passengers=passengers,
            request_reason=request_reason,
            **collect_kwargs,
        )
        if data:
            for flight in data.get("flights", []) or []:
                flight["search_origin"] = flight.get("search_origin") or origins[0]
                flight["search_destination"] = flight.get("search_destination") or destinations[0]
        return _filter_data_to_airports(data, origins, destinations)

    combinations = [(origins[0], destinations[0])]
    combinations.extend((origins[0], dest) for dest in destinations[1:])
    combinations.extend((origin, destinations[0]) for origin in origins[1:])
    combinations.extend(
        (origin, dest)
        for origin in origins[1:]
        for dest in destinations[1:]
    )

    merged = {
        "flights": [],
        "price_insights": {},
        "dual_source_price_anomalies": [],
        "source_stats": {},
        "source_errors": [],
        "raw_by_source": {},
        "sources_used": "",
        "source": "",
        "collected_at": datetime.now().isoformat(timespec="seconds"),
        "collection_freshness": [],
    }
    sources_used = []
    primary_cache_status = None

    for index, (origin, destination) in enumerate(combinations):
        current_count = len(_dedupe_flights(merged["flights"]))
        if index > 0:
            source_label = {
                "fresh": "新鲜",
                "cache": "缓存",
                "panel": "面板复用",
            }.get(primary_cache_status, "未知")
            should_skip = current_count >= AIRPORT_COMBINATION_MIN_OPTIONS
            action = (
                "跳过"
                if should_skip
                else f"继续搜{origin}->{destination}"
            )
            safe_log(
                f"[机场组合决策] 主组合={combinations[0][0]}->{combinations[0][1]} "
                f"有效方案数={current_count} 阈值={AIRPORT_COMBINATION_MIN_OPTIONS} "
                f"数据来源={source_label} 决策={action}"
            )
            if should_skip:
                break

        print(f"[城市搜索] 采集 {origin}→{destination} {date_str}")
        collect_kwargs = {"cabin_classes": cabin_classes}
        if route_type:
            collect_kwargs["route_type"] = route_type
        if index == 0:
            collect_kwargs["request_reason"] = request_reason
        elif request_reason:
            collect_kwargs["request_reason"] = f"{request_reason}/机场组合回退"
        else:
            collect_kwargs["request_reason"] = "机场组合回退"
        data = _aggregator_collect(aggregator, origin, destination, date_str, passengers=passengers, **collect_kwargs)
        if index == 0:
            primary_cache_status = (
                data.get("request_cache_status")
                if isinstance(data, dict)
                else getattr(aggregator, "last_request_cache_status", None)
            )
        if not data:
            _merge_source_errors(
                merged["source_errors"],
                getattr(aggregator, "last_source_errors", None),
            )
            continue
        for flight in data.get("flights", []):
            flight["search_origin"] = origin
            flight["search_destination"] = destination
        merged["flights"].extend(data.get("flights", []))
        if not merged["price_insights"] and data.get("price_insights"):
            merged["price_insights"] = data["price_insights"]
        if not merged.get("collected_at") and data.get("collected_at"):
            merged["collected_at"] = data["collected_at"]
        _merge_source_stats(merged["source_stats"], data.get("source_stats", {}))
        _merge_source_errors(merged["source_errors"], data.get("source_errors", []))
        merged["dual_source_price_anomalies"].extend(
            data.get("dual_source_price_anomalies", []) or []
        )
        merged["raw_by_source"].update(data.get("raw_by_source", {}))
        merged["collection_freshness"].extend(data.get("collection_freshness", []) or [])
        for source in str(data.get("sources_used") or data.get("source") or "").split("+"):
            if source and source not in sources_used:
                sources_used.append(source)

    merged = _filter_data_to_airports(merged, origins, destinations)
    merged["flights"] = _dedupe_flights(merged["flights"])
    merged["total_count"] = len(merged["flights"])
    merged["source_stats"]["after_dedup"] = len(merged["flights"])
    merged["sources_used"] = "+".join(sources_used)
    merged["source"] = merged["sources_used"]
    aggregator.last_source_errors = list(merged["source_errors"])
    return merged if merged["flights"] else None


def collect_nearby_dates(
    aggregator: FlightAggregator,
    sub: dict,
    cabin_classes=None,
    target_min_price=None,
    fresh_scope: str = "all",
) -> list[dict]:
    try:
        days_range = int(sub.get("date_flexibility") or 0)
    except (TypeError, ValueError):
        days_range = 0
    if days_range <= 0:
        return []

    center = date.fromisoformat(sub["depart_date"])
    results = [
        {
            "date": sub["depart_date"],
            "offset": 0,
            "min_price": target_min_price,
            "count": None,
            "selected": True,
        }
    ]
    searched_offsets = {0}
    calendar_sources = list(aggregator.search_sources)
    route_type = _subscription_route_type(
        sub,
        _subscription_airports(sub, "origin_airports_active", "origin_airports", "origin"),
        _subscription_airports(sub, "destination_airports_active", "destination_airports", "destination"),
    )

    panel_only_evaluation = (
        str(fresh_scope or "").strip().lower() == "primary_only"
    )
    primary_aggregator = FlightAggregator(calendar_sources, [], route_type=route_type)

    for stage in [1, 3, 7]:
        if days_range < stage:
            continue
        stage_results = []
        for offset in range(-stage, stage + 1):
            if offset == 0 or offset in searched_offsets:
                continue
            searched_offsets.add(offset)
            check_date = center + timedelta(days=offset)
            date_str = check_date.isoformat()
            try:
                data = collect_for_airport_matrix(
                    primary_aggregator,
                    _subscription_airports(
                        sub, "origin_airports_active", "origin_airports", "origin"
                    ),
                    _subscription_airports(
                        sub, "destination_airports_active", "destination_airports", "destination"
                    ),
                    date_str,
                    cabin_classes=cabin_classes,
                    route_type=route_type,
                    passengers=_subscription_passengers(sub),
                    request_reason="弹性日期",
                )
                flights = data.get("flights", []) if data else []
                prices = [
                    flight.get("price")
                    for flight in flights
                    if _valid_price(flight.get("price"))
                ]
                freshness_rows = [
                    item
                    for item in ((data or {}).get("collection_freshness") or [])
                    if isinstance(item, dict)
                    and item.get("state") not in {"panel_missing", "skipped"}
                ]
                contributed_sources = sorted(
                    {
                        str(source).lower()
                        for flight in flights
                        for source in str(
                            flight.get("data_source") or flight.get("source") or ""
                        ).split("+")
                        if source
                    }
                )
                collected_values = [
                    str(item.get("collected_at"))
                    for item in freshness_rows
                    if item.get("collected_at")
                ]
                reused = any(
                    item.get("state") == "panel_reused" for item in freshness_rows
                )
                stage_results.append(
                    {
                        "date": date_str,
                        "offset": offset,
                        "min_price": min(prices) if prices else None,
                        "count": len(flights),
                        "selected": False,
                        "sources": contributed_sources,
                        "collected_at": min(collected_values) if collected_values else None,
                        "collection_state": "panel_reused" if reused else "fresh",
                        "today_uncollected": not bool(prices),
                        "note": "今日未采" if not prices else "",
                    }
                )
            except Exception as exc:
                logging.error(f"{date_str} 相邻日期采集失败: {exc}")
                stage_results.append(
                    {
                        "date": date_str,
                        "offset": offset,
                        "min_price": None,
                        "count": 0,
                        "selected": False,
                        "sources": [],
                        "collected_at": None,
                        "collection_state": "panel_missing",
                        "today_uncollected": True,
                        "note": "今日未采",
                    }
                )
        results.extend(stage_results)
        if target_min_price is None and not panel_only_evaluation:
            break
        if not panel_only_evaluation and not any(
            item.get("min_price") is not None and item["min_price"] < target_min_price
            for item in stage_results
        ):
            break
    return results


def _sources_for_price(flights: list[dict], price) -> list[str]:
    """返回恰好贡献指定入池价的源集合。"""
    try:
        expected = float(price)
    except (TypeError, ValueError):
        return []
    sources = set()
    for flight in flights or []:
        try:
            current = float(flight.get("price"))
        except (TypeError, ValueError):
            continue
        if current != expected:
            continue
        source_value = (
            flight.get("price_source")
            or flight.get("data_source")
            or flight.get("source")
        )
        for source in str(source_value or "").replace("|", "+").split("+"):
            source = source.strip().lower()
            if source:
                sources.add(source)
    return sorted(sources)


def _constraint_history_flights(analysis: dict | None) -> list[dict]:
    """仅返回当前硬约束过滤后可用于同条件价格历史的候选。"""
    return [
        flight
        for flight in ((analysis or {}).get("all_flights") or [])
        if isinstance(flight, dict) and _valid_price(flight.get("price"))
    ]


def _merge_constraint_history_trend(
    previous_risk: dict | None,
    constraint_risk: dict | None,
) -> dict:
    """只替换历史趋势字段，不改变既有购买/等待风险判定。"""
    result = dict(previous_risk or {})
    if isinstance(constraint_risk, dict) and "trend" in constraint_risk:
        result["trend"] = constraint_risk["trend"]
    return result


def _notification_tcurve(route_info: dict) -> dict:
    """只读生成邮件曲线；统计失败不得中断订阅交付。"""
    try:
        _round_id, db_path = get_current_round()
        return build_notification_tcurve(route_info, db_path=db_path)
    except Exception as exc:
        safe_log(f"[T曲线] 读取失败 跳过渲染 原因={type(exc).__name__}:{exc}")
        return {}


def _notification_forecast(route_info: dict) -> dict:
    """只读计算预测闸门；未通过时不向通知 payload 写空结构。"""
    try:
        _round_id, db_path = get_current_round()
        result = build_notification_forecast(route_info, db_path=db_path)
        if not result.get("eligible"):
            safe_log(f"[预测] {result.get('reason')} 跳过渲染")
        return result
    except Exception as exc:
        safe_log(f"[预测] 读取失败 跳过渲染 原因={type(exc).__name__}:{exc}")
        return {"eligible": False, "reason": "读取失败"}


def _notification_provenance_context(route_info: dict) -> dict:
    """只读加载本次通知所需的统计依据；失败时保留原通知交付。"""
    try:
        _round_id, db_path = get_current_round()
        return build_route_provenance_context_from_info(route_info, db_path=db_path)
    except Exception as exc:
        safe_log(f"[依据] 读取失败 跳过附着 原因={type(exc).__name__}:{exc}")
        return {}


def process_subscription(
    sub: dict,
    ensure_db: bool = True,
    preflight_result: dict | None = None,
    web_trigger: bool = False,
    manage_collection_round: bool = True,
    collection_round_id: str | None = None,
) -> bool:
    """Process one subscription once and send the generated notification."""
    preflight = preflight_result or evaluate_subscription_preflight(
        sub,
        today=_shanghai_today(),
    )
    if preflight.get("skip"):
        _log_preflight_skip(sub, preflight)
        safe_log("[订阅前置校验] 本轮检查=1 跳过=1")
        return True

    if ensure_db:
        init_db()

    round_id = collection_round_id or _make_round_id(sub)
    route = f"{sub['origin']}-{sub['destination']}"
    constraint_fp = constraint_fingerprint(sub)
    history_return_date = None
    if _as_bool(sub.get("round_trip", False)):
        history_return_date = sub.get("return_date") or sub.get(
            "hard_constraints", {}
        ).get("return_date")
    subscription_snapshot_id = _subscription_identifier(sub, route)
    constraint_history_since = get_constraint_epoch_boundary(
        route,
        sub["depart_date"],
        history_return_date,
        constraint_fp,
        subscription_id=subscription_snapshot_id,
    )
    constraint_history_limit = get_constraint_history_limit(
        route,
        sub["depart_date"],
        history_return_date,
        constraint_fp,
        subscription_id=subscription_snapshot_id,
        default_limit=14,
    )
    if constraint_history_since:
        safe_log(
            f"[约束桶] 订阅={subscription_snapshot_id} "
            f"当前={constraint_fp[:8]} 序列起点>{constraint_history_since} "
            f"样本上限={constraint_history_limit}"
        )
    logging.info(f"开始处理 {route}")
    agg = None
    managed_plan_active = False
    collection_options = _collection_plan_log_options()
    round_archive_started = False
    round_status = "failed"
    if manage_collection_round:
        try:
            start_round_log_archive(round_id, root_dir=ROUND_LOG_ROOT)
            round_archive_started = True
        except Exception as exc:
            safe_log(f"[轮档失败] round_id={round_id} 原因={type(exc).__name__}:{exc}")


    try:
        if manage_collection_round:
            print(f"[\u89c2\u6d4b\u8f6e\u6b21] round_id={round_id}")
            set_current_round(round_id)
            log_options = collection_options
            start_request_cache_round(
                round_id,
                track_usage=True,
                usage_path=API_USAGE_PATH,
                quota_budgets=log_options.get("quota_budgets"),
            )
            collection_plan = build_collection_plan(
                subscriptions=[sub],
                basket_requests=[],
                freshness_hours=log_options.get("freshness_hours", 6),
                fresh_scope=log_options.get("fresh_scope", "primary_only"),
            )
            activate_collection_plan(
                collection_plan.request_keys,
                panel_only_keys=collection_plan.panel_only_keys,
                freshness_hours=collection_plan.freshness_hours,
                fresh_scope=collection_plan.fresh_scope,
            )
            managed_plan_active = True
            collection_plan.log_summary(**log_options)
            collection_plan.execute()

        active_origins = _subscription_airports(
            sub, "origin_airports_active", "origin_airports", "origin"
        )
        active_dests = _subscription_airports(
            sub, "destination_airports_active", "destination_airports", "destination"
        )
        route_type = _subscription_route_type(sub, active_origins, active_dests)
        request_passengers = _subscription_passengers(sub)
        first_origin = _first_airport(active_origins, sub["origin"])
        first_dest = _first_airport(active_dests, sub["destination"])
        search_sources, enrichment_sources = build_default_sources(
            first_origin,
            first_dest,
            route_type=route_type,
        )
        agg = FlightAggregator(search_sources, enrichment_sources, route_type=route_type)
        print(f"[机场调试] 全部目的地机场={sub.get('destination_airports')}")
        print(f"[机场调试] 激活的目的地机场={sub.get('destination_airports_active')}")
        print(f"[机场调试] 实际采集用的机场={active_dests}")
        data = collect_for_airport_matrix(
            agg,
            active_origins,
            active_dests,
            sub["depart_date"],
            cabin_classes=sub.get("cabin_classes"),
            route_type=route_type,
            passengers=request_passengers,
        )

        if data is None or not data.get("flights"):
            logging.error(f"{route} 采集返回空")
            source_errors = _source_error_items(agg, data)
            _log_subscription_failure(
                sub,
                source_errors=source_errors,
                reason="采集未返回有效航班",
            )
            if web_trigger:
                _notify_subscription_failure(
                    sub,
                    source_errors=source_errors,
                    reason="采集未返回有效航班",
                )
            return False

        run_collected_at = data.get("collected_at") or datetime.now().isoformat(timespec="seconds")
        normalized_flights = [
            _normalize_detail_flight(
                flight, flight.get("data_source") or flight.get("source")
            )
            for flight in data.get("flights", [])
        ]
        for flight in normalized_flights:
            flight["collected_at"] = flight.get("collected_at") or run_collected_at
        normalized_flights = _filter_data_to_airports(
            {"flights": normalized_flights},
            active_origins,
            active_dests,
        ).get("flights", [])
        flights = [
            flight
            for flight in normalized_flights
            if _valid_price(flight.get("price"))
        ]
        print(f"[价格检查] 有效价格航班: {len(flights)}/{len(normalized_flights)}")
        data["flights"] = flights
        data["total_count"] = len(flights)

        previous_prices = get_previous_snapshot_prices(
            route,
            sub["depart_date"],
            constraint_fingerprint=constraint_fp,
        )
        for flight in flights:
            combo = flight.get("flight_combo")
            if combo and combo in previous_prices:
                flight["previous_price"] = previous_prices[combo]
        save_raw_response(route, sub["depart_date"], data)
        logging.info(f"{route} 存储{data.get('total_count', 0)}个航班方案")

        preferences = subscription_preferences(sub)
        analysis = analyze_all_flights(
            flights,
            data.get("price_insights"),
            mode=sub.get("mode", "balanced"),
            priorities=sub.get("priorities"),
            user_preferences=preferences,
            hard_constraints=sub.get("hard_constraints", {}),
        )
        constraint_history_flights = _constraint_history_flights(analysis)
        save_flight_details(
            route,
            sub["depart_date"],
            constraint_history_flights,
            constraint_fingerprint=constraint_fp,
        )
        lowest_price_history = get_lowest_price_history(
            route,
            sub["depart_date"],
            limit=constraint_history_limit,
            constraint_fingerprint=constraint_fp,
            include_metadata=True,
            since=constraint_history_since,
        )
        days_to_dept = (date.fromisoformat(sub["depart_date"]) - date.today()).days
        current_min_price = (
            analysis.get("price_range", [0])[0] if analysis.get("price_range") else 0
        )
        price_calendar_result = None
        outbound_price_calendar = None
        calendar_cabin_class = (
            (sub.get("cabin_classes") or ["economy"])[0]
            if isinstance(sub.get("cabin_classes"), list)
            else (sub.get("cabin_classes") or "economy")
        )
        calendar_origin = _first_airport(active_origins, sub["origin"])
        calendar_dest = _first_airport(active_dests, sub["destination"])
        calendar_route = f"{calendar_origin}-{calendar_dest}"
        if current_min_price and is_domestic_route(calendar_origin, calendar_dest):
            try:
                calendar_source = _calendar_source_for_route(agg, calendar_origin, calendar_dest)
                if calendar_source:
                    print(f"[低价日历] 更新 {calendar_route} {sub['depart_date']}")
                    calendar = update_calendar(
                        calendar_route,
                        calendar_origin,
                        calendar_dest,
                        sub["depart_date"],
                        calendar_source,
                        cabin_class=calendar_cabin_class,
                        passengers=request_passengers,
                    )
                    outbound_price_calendar = calendar
                    price_calendar_result = analyze_price_calendar(
                        calendar,
                        sub["depart_date"],
                        current_min_price,
                    )
                    print(
                        "[低价日历] 完成: "
                        f"{len(price_calendar_result.get('rows') or [])}个日期, "
                        f"{len(price_calendar_result.get('savings') or [])}条省钱提示"
                    )
            except Exception as exc:
                print(f"[低价日历] 更新失败: {exc}")
        nearby_dates = collect_nearby_dates(
            agg,
            sub,
            cabin_classes=_reference_cabin_classes(sub),
            target_min_price=current_min_price,
            fresh_scope=collection_options.get("fresh_scope", "primary_only"),
        )
        analysis["days_to_dept"] = days_to_dept
        analysis["budget"] = sub.get("budget")
        analysis["budget_mode"] = sub.get("budget_mode", "fixed")
        analysis["goals"] = sub.get("goals", [])
        analysis["notification_goals"] = sub.get("notification_goals", {})
        analysis["hard_constraints"] = sub.get("hard_constraints", {})
        analysis["soft_preferences"] = sub.get("soft_preferences", {})
        analysis["defaults_applied"] = sub.get("defaults_applied", [])
        analysis["collected_at"] = run_collected_at
        analysis["nearby_dates"] = nearby_dates
        if price_calendar_result:
            analysis["price_calendar"] = price_calendar_result
        analysis["source_stats"] = data.get("source_stats", {})
        analysis["source_errors"] = data.get("source_errors", [])
        analysis["collection_failures"] = []
        analysis["collection_freshness"] = data.get("collection_freshness", [])
        analysis["constraint_fingerprint"] = constraint_fp
        analysis["constraint_price_history"] = lowest_price_history
        analysis["dual_source_price_anomalies"] = data.get(
            "dual_source_price_anomalies", []
        )
        analysis["price_position"] = price_position_description(
            current_min_price, lowest_price_history
        )
        analysis["waiting_risk"] = waiting_risk_description(
            lowest_price_history, current_min_price, days_to_dept
        )
        constraint_buy_wait_risk = calc_buy_vs_wait_risk(
            current_min_price,
            lowest_price_history,
            days_to_dept,
            analysis.get("target_price_effective"),
        )
        analysis["buy_vs_wait_risk"] = _merge_constraint_history_trend(
            analysis.get("buy_vs_wait_risk"),
            constraint_buy_wait_risk,
        )

        raw_round_trip = sub.get("round_trip", False)
        raw_return_date = sub.get("return_date") or sub.get("hard_constraints", {}).get("return_date")
        print(f"[往返] round_trip={raw_round_trip}")
        print(f"[往返] return_date={raw_return_date}")
        round_trip = _as_bool(raw_round_trip)
        return_date = raw_return_date
        return_analysis = None

        if round_trip and return_date:
            return_route = f"{sub['destination']}-{sub['origin']}"
            return_origin = sub["destination"]
            return_dest = sub["origin"]
            print(f"[往返] 开始采集返程 {return_route} {return_date}")
            print(f"[DEBUG] 开始采集返程: {return_origin}→{return_dest} {return_date}")
            return_data = collect_for_airport_matrix(
                agg,
                active_dests,
                active_origins,
                return_date,
                cabin_classes=sub.get("cabin_classes"),
                route_type=route_type,
                passengers=request_passengers,
            )
            return_source_errors = _source_error_items(agg, return_data)
            _merge_source_errors(
                analysis["source_errors"],
                return_source_errors,
            )
            return_collected_at = (return_data or {}).get("collected_at") or datetime.now().isoformat(timespec="seconds")
            normalized_return_flights = [
                _normalize_detail_flight(
                    flight, flight.get("data_source") or flight.get("source")
                )
                for flight in (return_data or {}).get("flights", [])
            ]
            for flight in normalized_return_flights:
                flight["collected_at"] = flight.get("collected_at") or return_collected_at
            normalized_return_flights = _filter_data_to_airports(
                {"flights": normalized_return_flights},
                active_dests,
                active_origins,
            ).get("flights", [])
            return_flights = [
                flight
                for flight in normalized_return_flights
                if _valid_price(flight.get("price"))
            ]
            print(
                f"[价格检查] 返程有效价格航班: "
                f"{len(return_flights)}/{len(normalized_return_flights)}"
            )
            print(f"[往返] 返程采集结果={len(return_flights)}个航班")
            print(f"[DEBUG] 返程采集完成: {len(return_flights)}个航班")
            if not return_flights and return_source_errors:
                collection_failure = _build_collection_leg_failure(
                    "return",
                    return_date,
                    active_dests,
                    active_origins,
                    return_source_errors,
                )
                analysis["collection_failures"].append(collection_failure)
                safe_log(
                    f"[采集腿失败] 方向=返程 日期={return_date} "
                    f"原因={collection_failure['reason']} 结论=数据不完整"
                )
            if return_flights:
                save_raw_response(return_route, return_date, return_data)
                return_preferences = {
                    **preferences,
                    "direction": "return",
                    "departure_slots": sub.get("return_departure_slots"),
                    "arrival_slots": sub.get("return_arrival_slots"),
                    "preferred_departure_slots": sub.get("return_departure_slots"),
                    "preferred_arrival_slots": sub.get("return_arrival_slots"),
                }
                return_constraints = {
                    **(sub.get("hard_constraints", {}) or {}),
                    "direction": "return",
                    "departure_slots": sub.get("return_departure_slots"),
                    "arrival_slots": sub.get("return_arrival_slots"),
                    "preferred_departure_slots": sub.get("return_departure_slots"),
                    "preferred_arrival_slots": sub.get("return_arrival_slots"),
                }
                return_analysis = analyze_all_flights(
                    return_flights,
                    (return_data or {}).get("price_insights"),
                    mode=sub.get("mode", "balanced"),
                    priorities=sub.get("priorities"),
                    user_preferences=return_preferences,
                    hard_constraints=return_constraints,
                )
                return_constraint_history_flights = _constraint_history_flights(
                    return_analysis
                )
                save_flight_details(
                    return_route,
                    return_date,
                    return_constraint_history_flights,
                    constraint_fingerprint=constraint_fp,
                )
                return_analysis["dual_source_price_anomalies"] = (
                    return_data or {}
                ).get("dual_source_price_anomalies", [])
                return_analysis["source_errors"] = (return_data or {}).get(
                    "source_errors", []
                )
                return_analysis["collection_freshness"] = (return_data or {}).get(
                    "collection_freshness", []
                )
                _merge_source_errors(
                    analysis["source_errors"],
                    return_analysis.get("source_errors", []),
                )
                return_min_price = (
                    return_analysis.get("price_range", [0])[0]
                    if return_analysis.get("price_range")
                    else 0
                )
                if price_calendar_result and outbound_price_calendar and return_min_price:
                    try:
                        return_calendar_origin = calendar_dest
                        return_calendar_dest = calendar_origin
                        return_calendar_route = f"{return_calendar_origin}-{return_calendar_dest}"
                        return_calendar_source = _calendar_source_for_route(
                            agg, return_calendar_origin, return_calendar_dest
                        )
                        if return_calendar_source and is_domestic_route(
                            return_calendar_origin, return_calendar_dest
                        ):
                            print(f"[低价日历] 更新返程固定日 {return_calendar_route} {return_date}")
                            return_calendar = update_calendar(
                                return_calendar_route,
                                return_calendar_origin,
                                return_calendar_dest,
                                return_date,
                                return_calendar_source,
                                cabin_class=calendar_cabin_class,
                                passengers=request_passengers,
                            )
                            price_calendar_result = analyze_price_calendar(
                                outbound_price_calendar,
                                sub["depart_date"],
                                current_min_price,
                                round_trip=True,
                                return_calendar=return_calendar,
                                return_date=return_date,
                            )
                            analysis["price_calendar"] = price_calendar_result
                            print(
                                "[低价日历] 往返参考价完成: "
                                f"{len(price_calendar_result.get('rows') or [])}个日期"
                            )
                    except Exception as exc:
                        print(f"[低价日历] 返程固定日更新失败,保留单程趋势: {exc}")
                return_sub = {
                    **sub,
                    "origin": sub["destination"],
                    "destination": sub["origin"],
                    "origin_airports": active_dests,
                    "origin_airports_active": active_dests,
                    "destination_airports": active_origins,
                    "destination_airports_active": active_origins,
                    "depart_date": return_date,
                    "date_flexibility": sub.get("return_date_flexibility", 0),
                }
                return_nearby_dates = collect_nearby_dates(
                    agg,
                    return_sub,
                    cabin_classes=_reference_cabin_classes(sub),
                    target_min_price=return_min_price,
                    fresh_scope=collection_options.get(
                        "fresh_scope",
                        "primary_only",
                    ),
                )
                return_analysis["days_to_dept"] = (
                    date.fromisoformat(return_date) - date.today()
                ).days
                return_analysis["collected_at"] = return_collected_at
                return_analysis["nearby_dates"] = return_nearby_dates
                analysis["return_analysis"] = return_analysis
                round_trip_analysis = analyze_round_trip(
                    analysis,
                    return_analysis,
                    target_price=sub.get("target_price"),
                    max_budget=sub.get("max_budget"),
                    emit_diagnostics=False,
                )
                if (
                    round_trip_analysis.get("same_day_time_conflict")
                    and (
                        sub.get("same_day_round_trip")
                        or (sub.get("hard_constraints") or {}).get("same_day_round_trip")
                    )
                ):
                    try:
                        previous_depart_date = (
                            date.fromisoformat(sub["depart_date"]) - timedelta(days=1)
                        ).isoformat()
                        print(f"[当天往返备选] 补采前一晚去程 {previous_depart_date}")
                        analysis["previous_day_flights"] = _collect_same_day_fallback_flights(
                            agg,
                            active_origins,
                            active_dests,
                            previous_depart_date,
                            cabin_classes=_reference_cabin_classes(sub),
                            route_type=route_type,
                            passengers=request_passengers,
                            request_reason="前一晚备选",
                        )
                    except Exception as exc:
                        print(f"[当天往返备选] 前一晚去程补采失败: {exc}")
                    try:
                        next_return_date = (
                            date.fromisoformat(return_date) + timedelta(days=1)
                        ).isoformat()
                        print(f"[当天往返备选] 补采次日返程 {next_return_date}")
                        return_analysis["next_day_flights"] = _collect_same_day_fallback_flights(
                            agg,
                            active_dests,
                            active_origins,
                            next_return_date,
                            cabin_classes=_reference_cabin_classes(sub),
                            route_type=route_type,
                            passengers=request_passengers,
                            request_reason="次日返程备选",
                        )
                    except Exception as exc:
                        print(f"[当天往返备选] 次日返程补采失败: {exc}")
                mixed_history = round_trip_analysis.get("mixed_cabin_history") or {}
                snapshot_outbound = mixed_history.get("outbound") or round_trip_analysis.get("outbound_min")
                snapshot_return = mixed_history.get("return") or round_trip_analysis.get("return_min")
                snapshot_total = mixed_history.get("total") or round_trip_analysis.get("total_min")
                save_roundtrip_snapshot(
                    route,
                    sub["depart_date"],
                    return_date,
                    snapshot_outbound,
                    snapshot_return,
                    snapshot_total,
                    datetime.now().isoformat(),
                    constraint_fingerprint=constraint_fp,
                    sources=(mixed_history.get("sources") or sorted(
                        set(
                            _sources_for_price(
                                constraint_history_flights,
                                round_trip_analysis.get("outbound_min"),
                            )
                        )
                        | set(
                            _sources_for_price(
                                return_constraint_history_flights,
                                round_trip_analysis.get("return_min"),
                            )
                        )
                    )),
                )
                roundtrip_history = get_roundtrip_price_history(
                    route,
                    sub["depart_date"],
                    return_date,
                    constraint_history_limit,
                    constraint_fingerprint=constraint_fp,
                    since=constraint_history_since,
                )
                roundtrip_current = snapshot_total
                analysis["price_position"] = price_position_description(
                    roundtrip_current,
                    roundtrip_history,
                )
                analysis["waiting_risk"] = waiting_risk_description(
                    roundtrip_history,
                    roundtrip_current,
                    days_to_dept,
                )
                analysis["round_trip_analysis"] = analyze_round_trip(
                    analysis,
                    return_analysis,
                    target_price=sub.get("target_price"),
                    max_budget=sub.get("max_budget"),
                    history=roundtrip_history,
                )
                analysis["round_trip"] = True
                print("[往返] return_analysis 已传入 notification payload")
            else:
                print("[往返] 返程采集为空，无法生成 return_analysis")
            print(f"[DEBUG] 返程analysis是否存在: {return_analysis is not None}")
        elif round_trip:
            print("[往返] 缺少 return_date，跳过返程采集")
            print(f"[DEBUG] 返程analysis是否存在: {return_analysis is not None}")

        signal_record = log_signal(
            route=route,
            depart_date=sub["depart_date"],
            analysis_result=analysis,
            price_insights=data.get("price_insights"),
        )
        analysis["confidence"] = signal_record.get("confidence")
        analysis["system_health"] = system_health_check(
            source_stats=data.get("source_stats", {}),
            flights=flights,
            analysis_result=analysis,
        )

        log_entry = {**analysis, "logged_at": datetime.now().isoformat()}
        with ANALYSIS_LOG.open("a", encoding="utf-8") as file:
            file.write(json.dumps(log_entry, ensure_ascii=False) + "\n")

        if _should_skip_low_price_alert(sub, analysis):
            print("[推送] 当前价格未进入低价区间，按订阅策略跳过推送")
            return True

        data_freshness_legs = [
            {**item, "direction": "去程"}
            for item in (analysis.get("collection_freshness") or [])
            if isinstance(item, dict)
        ]
        data_freshness_legs.extend(
            {
                **item,
                "direction": "返程",
            }
            for item in ((return_analysis or {}).get("collection_freshness") or [])
            if isinstance(item, dict)
        )
        message_kwargs = {
            "analysis_result": analysis,
            "outbound_analysis": analysis,
            "return_analysis": return_analysis,
            "route_info": {
                "origin": sub["origin"],
                "origin_type": sub.get("origin_type"),
                "origin_airports": sub.get("origin_airports"),
                "origin_airports_active": active_origins,
                "origin_airport_preference": sub.get("origin_airport_preference", "all"),
                "destination": sub["destination"],
                "destination_type": sub.get("destination_type"),
                "destination_airports": sub.get("destination_airports"),
                "destination_airports_active": active_dests,
                "depart_date": sub["depart_date"],
                "cabin_classes": sub.get("cabin_classes"),
                "mode": sub.get("mode", "balanced"),
                "priorities": sub.get("priorities"),
                "budget": sub.get("budget"),
                "budget_mode": sub.get("budget_mode"),
                "return_date": return_date,
                "return_date_flexibility": sub.get("return_date_flexibility", 0),
                "round_trip": round_trip,
                "date_flexibility": sub.get("date_flexibility", 0),
                "direct_only": sub.get("direct_only", "flexible"),
                "transfer_policy": sub.get("transfer_policy", "reasonable"),
                "red_eye": sub.get("red_eye", "reject"),
                "red_eye_policy": sub.get("red_eye_policy", "not_allowed"),
                "departure_time_policy": sub.get("departure_time_policy", "after_06"),
                "arrival_time_policy": sub.get("arrival_time_policy", "any"),
                "need_baggage": sub.get("need_baggage", "unknown"),
                "refund_flexibility": sub.get("refund_flexibility", "unknown"),
                "trip_type": sub.get("trip_type", "tourism"),
                "companions": sub.get("companions", "solo"),
                "price_sensitivity": sub.get("price_sensitivity", "low"),
                "trip_rigidity": sub.get("trip_rigidity", "confirmed"),
                "goals": sub.get("goals", []),
                "max_budget": sub.get("max_budget"),
                "max_budget_mode": sub.get("max_budget_mode"),
                "target_price": sub.get("target_price"),
                "target_price_mode": sub.get("target_price_mode"),
                "budget_scope": sub.get("budget_scope"),
                "max_budget_scope": sub.get("max_budget_scope", sub.get("budget_scope", "per_person")),
                "target_price_scope": sub.get("target_price_scope", sub.get("budget_scope", "per_person")),
                "hard_constraints": sub.get("hard_constraints", {}),
                "soft_preferences": sub.get("soft_preferences", {}),
                "notification_goals": sub.get("notification_goals", {}),
                "nearby_dates": nearby_dates,
                "price_calendar": price_calendar_result,
                "previous_prices": previous_prices,
                "lowest_price_history": lowest_price_history,
                "source_stats": data.get("source_stats", {}),
                "source_errors": analysis.get("source_errors", []),
                "collection_failures": analysis.get("collection_failures", []),
                "collected_at": run_collected_at,
                "data_freshness": {"legs": data_freshness_legs},
                "constraint_fingerprint": constraint_fp,
            },
            "source_stats": data.get("source_stats"),
            "price_insights": data.get("price_insights"),
        }
        message_kwargs["route_info"]["tcurve"] = _notification_tcurve(
            message_kwargs["route_info"]
        )
        forecast_result = _notification_forecast(message_kwargs["route_info"])
        if forecast_result.get("eligible"):
            message_kwargs["route_info"]["forecast"] = forecast_result
        message_kwargs["route_info"]["provenance_context"] = (
            _notification_provenance_context(message_kwargs["route_info"])
        )
        print(f"[DEBUG] 传给notifier的参数keys: {list(message_kwargs.keys())}")
        if not _deliver_notification(sub, route, message_kwargs):
            logging.warning(f"{route} 未能完成任何主动推送")
            _log_subscription_failure(sub, reason="通知未发送成功")
            return False
        logging.info(f"{route} 已推送方案对比表")
        round_status = "ok"
        return True

    except Exception as e:
        print(f"[处理失败] {type(e).__name__}: {e}")
        print(traceback.format_exc())
        logging.error(f"{route} 处理失败: {e}", exc_info=True)
        _log_subscription_failure(
            sub,
            source_errors=_source_error_items(agg),
            reason=f"{type(e).__name__}: {e}",
        )
        return False


    finally:
        if manage_collection_round:
            print_request_cache_stats()
            if managed_plan_active:
                deactivate_collection_plan()
            clear_current_round()
            log_retention_dry_run(BASE_DIR, config_path=CONFIG_PATH)
            if round_archive_started:
                try:
                    end_round_log_archive(status=round_status)
                except Exception as exc:
                    safe_log(f"[轮档失败] round_id={round_id} 关闭失败={type(exc).__name__}:{exc}")

def run(sync_remote: bool = True):
    if sync_remote:
        try:
            sync_subscriptions()
        except Exception as exc:
            logging.error(f"PythonAnywhere 订阅同步失败: {exc}")
            print(f"[sync] PythonAnywhere 订阅同步失败，继续处理本地订阅: {exc}")

    # 初始化
    init_db()
    subscriptions = load_file_subscriptions()
    if not subscriptions:
        print("暂无订阅，请通过表单添加")
        logging.info("暂无订阅，请通过表单添加")
        safe_log("[订阅前置校验] 本轮检查=0 跳过=0")
        _run_basket_sentinel_for_main([])
        return

    preflight_checked = 0
    preflight_skipped = 0
    current_day = _shanghai_today()
    ready: list[tuple[dict, dict]] = []
    for sub in subscriptions:
        preflight_checked += 1
        try:
            preflight = evaluate_subscription_preflight(sub, today=current_day)
        except Exception as exc:
            _log_subscription_failure(
                sub,
                reason=f"前置校验失败: {type(exc).__name__}: {exc}",
            )
            continue
        if preflight.get("skip"):
            preflight_skipped += 1
            _log_preflight_skip(sub, preflight)
            continue
        ready.append((sub, preflight))

    if not ready:
        safe_log(
            f"[订阅前置校验] 本轮检查={preflight_checked} 跳过={preflight_skipped}"
        )
        _run_basket_sentinel_for_main(subscriptions)
        return

    round_id = _make_collection_round_id()
    round_status = "failed"
    round_archive_started = False
    try:
        start_round_log_archive(round_id, root_dir=ROUND_LOG_ROOT)
        round_archive_started = True
    except Exception as exc:
        safe_log(f"[轮档失败] round_id={round_id} 原因={type(exc).__name__}:{exc}")
    plan_active = False
    print(f"[观测轮次] round_id={round_id}")
    set_current_round(round_id)
    log_options = _collection_plan_log_options()
    start_request_cache_round(
        round_id,
        track_usage=True,
        usage_path=API_USAGE_PATH,
        quota_budgets=log_options.get("quota_budgets"),
    )
    try:
        collection_plan = build_collection_plan(
            subscriptions=[sub for sub, _ in ready],
            basket_requests=[],
            freshness_hours=log_options.get("freshness_hours", 6),
            fresh_scope=log_options.get("fresh_scope", "primary_only"),
        )
        activate_collection_plan(
            collection_plan.request_keys,
            panel_only_keys=collection_plan.panel_only_keys,
            freshness_hours=collection_plan.freshness_hours,
            fresh_scope=collection_plan.fresh_scope,
        )
        plan_active = True
        collection_plan.log_summary(**log_options)
        collection_plan.execute()

        for sub, preflight in ready:
            try:
                process_subscription(
                    sub,
                    ensure_db=False,
                    preflight_result=preflight,
                    manage_collection_round=False,
                    collection_round_id=round_id,
                )
            except Exception as exc:
                _log_subscription_failure(sub, reason=f"{type(exc).__name__}: {exc}")
                print(traceback.format_exc())
                logging.error(
                    f"订阅处理失败 {_subscription_label(sub)}: {exc}",
                    exc_info=True,
                )
                continue
        round_status = "ok"
    finally:
        print_request_cache_stats()
        _run_basket_sentinel_for_main(subscriptions)
        if plan_active:
            deactivate_collection_plan()
        clear_current_round()
        log_retention_dry_run(BASE_DIR, config_path=CONFIG_PATH)
        if round_archive_started:
            try:
                end_round_log_archive(status=round_status)
            except Exception as exc:
                safe_log(f"[轮档失败] round_id={round_id} 关闭失败={type(exc).__name__}:{exc}")

    safe_log(
        f"[订阅前置校验] 本轮检查={preflight_checked} 跳过={preflight_skipped}"
    )
    logging.info("本轮执行完成")


if __name__ == "__main__":
    run()
