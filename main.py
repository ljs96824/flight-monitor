import json
import logging
import os
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
    analyze_all_flights,
    analyze_round_trip,
    price_position_description,
    waiting_risk_description,
)
from collector import _normalize_detail_flight, save_raw_response
from health_check import system_health_check
from notifier import format_html_message, send
from sources.aggregator import FlightAggregator, build_default_sources
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
    hard_constraints = item.get("hard_constraints") or {}
    soft_preferences = item.get("soft_preferences") or {}
    notification_goals = item.get("notification_goals") or {}
    return_date = item.get("return_date") or hard_constraints.get("return_date")
    round_trip = _as_bool(item.get("round_trip", hard_constraints.get("round_trip", False)))
    origin_info = resolve_location(item.get("origin", ""))
    destination_info = resolve_location(item.get("destination", ""))
    origin_airports = item.get("origin_airports") or origin_info["airports"]
    destination_airports = (
        item.get("destination_airports") or destination_info["airports"]
    )
    origin_airport_preference = hard_constraints.get(
        "origin_airport_preference", item.get("origin_airport_preference", "all")
    )
    if origin_airport_preference and origin_airport_preference != "all":
        preferred_origin = str(origin_airport_preference).strip().upper()
        if preferred_origin in origin_airports:
            origin_airports = [preferred_origin]

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
    refund_flexibility = hard_constraints.get(
        "refund_flexibility", item.get("refund_flexibility", "unknown")
    )
    exclude_airlines = hard_constraints.get("exclude_airlines", item.get("exclude_airlines", []))
    if isinstance(exclude_airlines, str):
        exclude_airlines = [
            value.strip()
            for value in exclude_airlines.replace("，", ",").split(",")
            if value.strip()
        ]

    return {
        "name": item.get("name") or "网页订阅",
        "origin": origin_info["value"],
        "origin_type": item.get("origin_type") or origin_info["type"],
        "origin_airports": origin_airports,
        "origin_airport_preference": origin_airport_preference,
        "destination": destination_info["value"],
        "destination_type": item.get("destination_type") or destination_info["type"],
        "destination_airports": destination_airports,
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
        "transfer_policy": transfer_policy or "short_ok",
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
        "airline_policy": hard_constraints.get(
            "airline_policy", item.get("airline_policy", "any")
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
        "hard_constraints": hard_constraints,
        "soft_preferences": soft_preferences,
        "mode": item.get("mode", "balanced"),
        "cabin_classes": item.get("cabin_classes"),
        "priorities": item.get("priorities"),
    }


def normalize_subscription(item: dict) -> dict:
    """Public wrapper used by web_form for immediate single-subscription runs."""
    return _normalize_subscription(item)


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
    for item in subscriptions:
        if not isinstance(item, dict):
            continue
        if item.get("status", "active") != "active":
            continue
        active.append(_normalize_subscription(item))
    return [
        sub
        for sub in active
        if sub.get("origin") and sub.get("destination") and sub.get("depart_date")
    ]


def subscription_preferences(sub: dict) -> dict:
    return {
        "direct_only": sub.get("direct_only", "flexible"),
        "transfer_policy": sub.get("transfer_policy", "short_ok"),
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
    origins = [code for code in origins if code]
    destinations = [code for code in destinations if code]
    if not origins or not destinations:
        return None

    if len(origins) == 1 and len(destinations) == 1:
        return aggregator.collect(
            origins[0],
            destinations[0],
            date_str,
            cabin_classes=cabin_classes,
        )

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
        _merge_source_stats(merged["source_stats"], data.get("source_stats", {}))
        merged["source_errors"].extend(data.get("source_errors", []))
        merged["raw_by_source"].update(data.get("raw_by_source", {}))
        for source in str(data.get("sources_used") or data.get("source") or "").split("+"):
            if source and source not in sources_used:
                sources_used.append(source)

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
                    sub.get("origin_airports") or [sub["origin"]],
                    sub.get("destination_airports") or [sub["destination"]],
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
        data = collect_for_airport_matrix(
            agg,
            sub.get("origin_airports") or [sub["origin"]],
            sub.get("destination_airports") or [sub["destination"]],
            sub["depart_date"],
            cabin_classes=sub.get("cabin_classes"),
        )

        if data is None or not data.get("flights"):
            logging.error(f"{route} 采集返回空")
            return False

        normalized_flights = [
            _normalize_detail_flight(
                flight, flight.get("data_source") or flight.get("source")
            )
            for flight in data.get("flights", [])
        ]
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
        analysis["nearby_dates"] = nearby_dates
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
                sub.get("destination_airports") or [return_origin],
                sub.get("origin_airports") or [return_dest],
                return_date,
                cabin_classes=sub.get("cabin_classes"),
            )
            normalized_return_flights = [
                _normalize_detail_flight(
                    flight, flight.get("data_source") or flight.get("source")
                )
                for flight in (return_data or {}).get("flights", [])
            ]
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
                    "origin_airports": sub.get("destination_airports") or [sub["destination"]],
                    "destination_airports": sub.get("origin_airports") or [sub["origin"]],
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
                return_analysis["nearby_dates"] = return_nearby_dates
                analysis["return_analysis"] = return_analysis
                round_trip_analysis = analyze_round_trip(
                    analysis,
                    return_analysis,
                    target_price=sub.get("target_price"),
                    max_budget=sub.get("max_budget"),
                )
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
                print("[往返] return_analysis 已传入 format_html_message")
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

        message_kwargs = {
            "analysis_result": analysis,
            "outbound_analysis": analysis,
            "return_analysis": return_analysis,
            "route_info": {
                "origin": sub["origin"],
                "origin_type": sub.get("origin_type"),
                "origin_airports": sub.get("origin_airports"),
                "origin_airport_preference": sub.get("origin_airport_preference", "all"),
                "destination": sub["destination"],
                "destination_type": sub.get("destination_type"),
                "destination_airports": sub.get("destination_airports"),
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
                "transfer_policy": sub.get("transfer_policy", "short_ok"),
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
                "previous_prices": previous_prices,
                "lowest_price_history": lowest_price_history,
                "source_stats": data.get("source_stats", {}),
            },
            "source_stats": data.get("source_stats"),
            "price_insights": data.get("price_insights"),
        }
        print(f"[DEBUG] 传给notifier的参数keys: {list(message_kwargs.keys())}")
        msg = format_html_message(**message_kwargs)
        send(msg)
        logging.info(f"{route} 已推送方案对比表")
        return True

    except Exception as e:
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
        process_subscription(sub, ensure_db=False)

    logging.info("本轮执行完成")


if __name__ == "__main__":
    run()
