import json
import logging
import os
import re
import traceback
from datetime import date, datetime, timedelta
from pathlib import Path

from dotenv import load_dotenv
from airports import resolve_location


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
    determine_cabins,
    get_total_passengers,
    migrate_old_subscription,
    price_position_description,
    waiting_risk_description,
)
from collector import _normalize_detail_flight, save_raw_response
from email_notifier import render_email, send_email
from filename_utils import sanitize_filename
from health_check import system_health_check
from notifier import (
    build_notification_payload,
    persist_notification_payload,
    render_detail_html,
    render_pushplus,
    send,
)
from price_calendar import update_calendar
from sources.aggregator import FlightAggregator, build_default_sources, is_domestic_route
from storage import (
    get_roundtrip_price_history,
    get_lowest_price_history,
    get_previous_snapshot_prices,
    init_db,
    save_roundtrip_snapshot,
    save_flight_details,
)
from sync_subscriptions import sync_subscriptions
from tracker import log_signal


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


def _first_airport(codes, fallback):
    values = [str(code).strip().upper() for code in (codes or []) if str(code or "").strip()]
    return values[0] if values else str(fallback or "").strip().upper()


def _clean_airport_codes(codes) -> list[str]:
    if isinstance(codes, str):
        codes = [codes]
    return [str(code).strip().upper() for code in (codes or []) if str(code or "").strip()]


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


def _normalize_subscription(item: dict) -> dict:
    item = migrate_old_subscription(item)
    hard_constraints = item.get("hard_constraints") or {}
    soft_preferences = item.get("soft_preferences") or {}
    basic = dict(item.get("basic") or {})
    constraints = dict(item.get("constraints") or {})
    preferences = dict(item.get("preferences") or {})
    advanced_rules = dict(item.get("advanced_rules") or {})
    passenger_count, passengers = get_total_passengers(item)
    basic["passenger_count"] = passenger_count
    preferences["passenger_count"] = passenger_count
    if passengers:
        preferences["passengers"] = passengers
        soft_preferences["passengers"] = passengers
    soft_preferences["passenger_count"] = passenger_count
    notification_goals = item.get("notification_goals") or {}
    return_date = item.get("return_date") or hard_constraints.get("return_date")
    round_trip = _as_bool(item.get("round_trip", hard_constraints.get("round_trip", False)))
    origin_info = resolve_location(item.get("origin", ""))
    destination_info = resolve_location(item.get("destination", ""))
    if origin_info.get("type") == "unknown":
        raise ValueError(
            f"无法识别地点 {origin_info.get('value')},请输入机场三字码或已支持的城市"
        )
    if destination_info.get("type") == "unknown":
        raise ValueError(
            f"无法识别地点 {destination_info.get('value')},请输入机场三字码或已支持的城市"
        )
    origin_airports = (
        item.get("origin_airports")
        or basic.get("origin_airports")
        or origin_info["airports"]
    )
    destination_airports = (
        item.get("destination_airports")
        or basic.get("destination_airports")
        or basic.get("dest_airports")
        or destination_info["airports"]
    )
    origin_airports = _clean_airport_codes(origin_airports)
    destination_airports = _clean_airport_codes(destination_airports)
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
        "origin_type": item.get("origin_type") or origin_info["type"],
        "origin_airports": origin_airports,
        "origin_airports_active": origin_airports_active,
        "origin_airport_preference": origin_airport_preference,
        "destination": destination_info["value"],
        "destination_type": item.get("destination_type") or destination_info["type"],
        "destination_airports": destination_airports,
        "destination_airports_active": destination_airports_active,
        "monitor_mode": item.get("monitor_mode", "quick"),
        "depart_date": item.get("depart_date", ""),
        "budget": max_budget,
        "budget_mode": max_budget_mode,
        "max_budget": max_budget,
        "max_budget_mode": max_budget_mode,
        "target_price": target_price,
        "target_price_mode": target_price_mode,
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
        "cabin_classes": item.get("cabin_classes") or determine_cabins(hard_constraints_for_cabins),
        "priorities": item.get("priorities"),
    }
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


def _save_result_for_page(subscription_id: str, html_content: str, payload: dict | None = None) -> None:
    PAGE_PAYLOADS_DIR.mkdir(exist_ok=True)
    payload_path = PAGE_PAYLOADS_DIR / f"{sanitize_filename(subscription_id)}.json"
    record = {
        "subscription_id": str(subscription_id),
        "created_at": datetime.now().isoformat(timespec="seconds"),
        "html": html_content,
        "payload": payload or {},
    }
    payload_path.write_text(json.dumps(record, ensure_ascii=False, indent=2, default=str), encoding="utf-8")
    print(f"[详情存储] 保存订阅 {subscription_id} 的payload到 {payload_path}")

    path = DATA_DIR / "page_results.json"
    try:
        records = json.loads(path.read_text(encoding="utf-8")) if path.exists() else []
    except json.JSONDecodeError:
        records = []
    records.append(record)
    path.write_text(json.dumps(records[-100:], ensure_ascii=False, indent=2), encoding="utf-8")


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
    return str(
        sub.get("subscription_id")
        or sub.get("id")
        or sub.get("_index")
        or f"{route}|{sub.get('depart_date', '')}|{sub.get('return_date', '')}"
    )


def _deliver_notification(sub: dict, route: str, message_kwargs: dict) -> bool:
    try:
        notification_goals = sub.get("notification_goals", {}) or {}
        method = notification_goals.get("method", "pushplus")
        email = notification_goals.get("email", "").strip()
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
        print("[推送] payload构建完成")

        print("[推送] 开始渲染邮件/详情HTML")
        if method in {"email", "both"}:
            print("[推送] 邮件方式已启用，开始生成折线图PNG/邮件HTML")
        email_rendered = render_email(payload)
        if len(email_rendered) == 3:
            subject, full_html, inline_images = email_rendered
        else:
            subject, full_html = email_rendered
            inline_images = {}
        print("[推送] 邮件/详情HTML渲染完成")
        detail_html = render_detail_html(payload)
        _save_result_for_page(subscription_id, detail_html, payload)

        if method == "page_only":
            print("[推送] 开始保存页面结果")
            print("[推送] 用户选择仅页面查看，已保存页面结果")
            return True

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
            push_content = render_pushplus(payload)
            print("[推送] PushPlus短版渲染完成，开始发送")
            sent = send(
                push_content,
                title=f"【{payload.get('push_type', '价格提醒')}】{payload.get('route', route)}",
            ) or sent
            print(f"[推送] PushPlus发送完成: sent={sent}")

        if method not in {"email", "pushplus", "both", "page_only"}:
            print(f"[推送] 未识别的推送方式 {method!r}，按PushPlus兜底")
            push_content = render_pushplus(payload)
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
        if sub.get("origin") and sub.get("destination") and sub.get("depart_date")
    ]


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


def collect_for_airport_matrix(
    aggregator: FlightAggregator,
    origins: list[str],
    destinations: list[str],
    date_str: str,
    cabin_classes=None,
) -> dict | None:
    origins = _clean_airport_codes(origins)
    destinations = _clean_airport_codes(destinations)
    if not origins or not destinations:
        return None

    if len(origins) == 1 and len(destinations) == 1:
        data = aggregator.collect(
            origins[0],
            destinations[0],
            date_str,
            cabin_classes=cabin_classes,
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
        "source_stats": {},
        "source_errors": [],
        "raw_by_source": {},
        "sources_used": "",
        "source": "",
        "collected_at": datetime.now().isoformat(timespec="seconds"),
    }
    sources_used = []

    for index, (origin, destination) in enumerate(combinations):
        current_count = len(_dedupe_flights(merged["flights"]))
        if index > 0 and current_count >= 5:
            print(
                f"[城市搜索] 主机场组合已返回{current_count}个有效方案，跳过低优先级机场组合"
            )
            break

        print(f"[城市搜索] 采集 {origin}→{destination} {date_str}")
        data = aggregator.collect(
            origin,
            destination,
            date_str,
            cabin_classes=cabin_classes,
        )
        if not data:
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
        merged["source_errors"].extend(data.get("source_errors", []))
        merged["raw_by_source"].update(data.get("raw_by_source", {}))
        for source in str(data.get("sources_used") or data.get("source") or "").split("+"):
            if source and source not in sources_used:
                sources_used.append(source)

    merged = _filter_data_to_airports(merged, origins, destinations)
    merged["flights"] = _dedupe_flights(merged["flights"])
    merged["total_count"] = len(merged["flights"])
    merged["source_stats"]["after_dedup"] = len(merged["flights"])
    merged["sources_used"] = "+".join(sources_used)
    merged["source"] = merged["sources_used"]
    return merged if merged["flights"] else None


def collect_nearby_dates(
    aggregator: FlightAggregator,
    sub: dict,
    cabin_classes=None,
    target_min_price=None,
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
    primary_sources = [
        source
        for source in aggregator.search_sources
        if getattr(source, "name", "").lower() == "serpapi"
    ] or aggregator.search_sources[:1]
    primary_aggregator = FlightAggregator(primary_sources, [])

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
                )
                flights = data.get("flights", []) if data else []
                prices = [
                    flight.get("price")
                    for flight in flights
                    if _valid_price(flight.get("price"))
                ]
                stage_results.append(
                    {
                        "date": date_str,
                        "offset": offset,
                        "min_price": min(prices) if prices else None,
                        "count": len(flights),
                        "selected": False,
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
                    }
                )
        results.extend(stage_results)
        if target_min_price is None:
            break
        if not any(
            item.get("min_price") is not None and item["min_price"] < target_min_price
            for item in stage_results
        ):
            break
    return results

def process_subscription(sub: dict, ensure_db: bool = True) -> bool:
    """Process one subscription once and send the generated notification."""
    if ensure_db:
        init_db()

    route = f"{sub['origin']}-{sub['destination']}"
    logging.info(f"开始处理 {route}")

    try:
        search_sources, enrichment_sources = build_default_sources()
        agg = FlightAggregator(search_sources, enrichment_sources)
        active_origins = _subscription_airports(
            sub, "origin_airports_active", "origin_airports", "origin"
        )
        active_dests = _subscription_airports(
            sub, "destination_airports_active", "destination_airports", "destination"
        )
        print(f"[机场调试] 全部目的地机场={sub.get('destination_airports')}")
        print(f"[机场调试] 激活的目的地机场={sub.get('destination_airports_active')}")
        print(f"[机场调试] 实际采集用的机场={active_dests}")
        data = collect_for_airport_matrix(
            agg,
            active_origins,
            active_dests,
            sub["depart_date"],
            cabin_classes=sub.get("cabin_classes"),
        )

        if data is None or not data.get("flights"):
            logging.error(f"{route} 采集返回空")
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

        save_flight_details(route, sub["depart_date"], flights)
        previous_prices = get_previous_snapshot_prices(route, sub["depart_date"])
        lowest_price_history = get_lowest_price_history(
            route, sub["depart_date"], limit=14
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
        days_to_dept = (date.fromisoformat(sub["depart_date"]) - date.today()).days
        current_min_price = (
            analysis.get("price_range", [0])[0] if analysis.get("price_range") else 0
        )
        price_calendar_result = None
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
                        cabin_class=(sub.get("cabin_classes") or ["economy"])[0]
                        if isinstance(sub.get("cabin_classes"), list)
                        else (sub.get("cabin_classes") or "economy"),
                    )
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
            cabin_classes=sub.get("cabin_classes"),
            target_min_price=current_min_price,
        )
        price_history = (data.get("price_insights") or {}).get("price_history")
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
        analysis["price_position"] = price_position_description(
            current_min_price, price_history
        )
        analysis["waiting_risk"] = waiting_risk_description(
            price_history, current_min_price, days_to_dept
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
            if return_flights:
                save_flight_details(return_route, return_date, return_flights)
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
                return_min_price = (
                    return_analysis.get("price_range", [0])[0]
                    if return_analysis.get("price_range")
                    else 0
                )
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
                    cabin_classes=sub.get("cabin_classes"),
                    target_min_price=return_min_price,
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
                            cabin_classes=sub.get("cabin_classes"),
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
                            cabin_classes=sub.get("cabin_classes"),
                        )
                    except Exception as exc:
                        print(f"[当天往返备选] 次日返程补采失败: {exc}")
                save_roundtrip_snapshot(
                    route,
                    sub["depart_date"],
                    return_date,
                    round_trip_analysis.get("outbound_min"),
                    round_trip_analysis.get("return_min"),
                    round_trip_analysis.get("total_min"),
                    datetime.now().isoformat(),
                )
                roundtrip_history = get_roundtrip_price_history(
                    route, sub["depart_date"], return_date, 14
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

        if (
            (sub.get("hard_constraints", {}) or {}).get("budget_strategy")
            == "low_price_alert"
            and not analysis.get("low_price_alert_triggered", False)
        ):
            print("[推送] 当前价格未进入低价区间，按订阅策略跳过推送")
            return True

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
                "hard_constraints": sub.get("hard_constraints", {}),
                "soft_preferences": sub.get("soft_preferences", {}),
                "notification_goals": sub.get("notification_goals", {}),
                "nearby_dates": nearby_dates,
                "price_calendar": price_calendar_result,
                "previous_prices": previous_prices,
                "lowest_price_history": lowest_price_history,
                "source_stats": data.get("source_stats", {}),
                "collected_at": run_collected_at,
            },
            "source_stats": data.get("source_stats"),
            "price_insights": data.get("price_insights"),
        }
        print(f"[DEBUG] 传给notifier的参数keys: {list(message_kwargs.keys())}")
        if not _deliver_notification(sub, route, message_kwargs):
            logging.warning(f"{route} 未能完成任何主动推送")
            return False
        logging.info(f"{route} 已推送方案对比表")
        return True

    except Exception as e:
        print(f"[处理失败] {type(e).__name__}: {e}")
        print(traceback.format_exc())
        logging.error(f"{route} 处理失败: {e}", exc_info=True)
        return False


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
        return

    for sub in subscriptions:
        try:
            ok = process_subscription(sub, ensure_db=False)
            if not ok:
                print(f"[订阅处理失败] {sub.get('id') or sub.get('_index') or '未知'}: 返回失败")
        except Exception as exc:
            print(f"[订阅处理失败] {sub.get('id') or sub.get('_index') or '未知'}: {exc}")
            print(traceback.format_exc())
            logging.error(
                f"订阅处理失败 {sub.get('id') or sub.get('_index') or '未知'}: {exc}",
                exc_info=True,
            )
            continue

    logging.info("本轮执行完成")


if __name__ == "__main__":
    run()
