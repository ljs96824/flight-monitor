import json
import logging
import os
from datetime import date, datetime, timedelta
from pathlib import Path

from dotenv import load_dotenv


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
    get_lowest_price_history,
    get_previous_snapshot_prices,
    init_db,
    save_flight_details,
)
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

    budget = hard_constraints.get("budget", item.get("budget"))
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
        "origin": item.get("origin", "").strip().upper(),
        "destination": item.get("destination", "").strip().upper(),
        "depart_date": item.get("depart_date", ""),
        "budget": budget,
        "budget_mode": hard_constraints.get(
            "budget_mode",
            item.get("budget_mode", "fixed" if budget else "unknown"),
        ),
        "return_date": item.get("return_date"),
        "round_trip": bool(item.get("round_trip", False)),
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
        "need_baggage": sub.get("need_baggage", "unknown"),
        "refund_flexibility": sub.get("refund_flexibility", "unknown"),
        "trip_type": sub.get("trip_type", "tourism"),
        "companions": sub.get("companions", "solo"),
        "price_sensitivity": sub.get("price_sensitivity", "low"),
        "trip_rigidity": sub.get("trip_rigidity", "confirmed"),
        "goals": sub.get("goals", []),
        "budget": sub.get("budget"),
        "budget_mode": sub.get("budget_mode", "fixed"),
        "date_flexibility": sub.get("date_flexibility", 0),
        "round_trip": sub.get("round_trip", False),
        "return_date": sub.get("return_date"),
        "return_date_flexibility": sub.get("return_date_flexibility", 0),
        "airline_policy": sub.get("airline_policy", "any"),
        "exclude_airlines": sub.get("exclude_airlines", []),
        "max_extra_duration_hours": sub.get("max_extra_duration_hours"),
        "max_total_duration_hours": sub.get("max_total_duration_hours"),
    }


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
                data = primary_aggregator.collect(
                    sub["origin"],
                    sub["destination"],
                    date_str,
                    cabin_classes=cabin_classes,
                )
                flights = data.get("flights", []) if data else []
                prices = [
                    flight.get("price")
                    for flight in flights
                    if flight.get("price") is not None
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

def run():
    # 初始化
    init_db()
    subscriptions = load_file_subscriptions()
    if not subscriptions:
        print("暂无订阅，请通过表单添加")
        logging.info("暂无订阅，请通过表单添加")
        return

    for sub in subscriptions:
        route = f"{sub['origin']}-{sub['destination']}"
        logging.info(f"开始处理 {route}")

        try:
            search_sources, enrichment_sources = build_default_sources()
            agg = FlightAggregator(search_sources, enrichment_sources)
            data = agg.collect(
                sub["origin"],
                sub["destination"],
                sub["depart_date"],
                cabin_classes=sub.get("cabin_classes"),
            )

            if data is None or not data.get("flights"):
                logging.error(f"{route} 采集返回空")
                continue

            flights = [
                _normalize_detail_flight(
                    flight, flight.get("data_source") or flight.get("source")
                )
                for flight in data.get("flights", [])
            ]
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
            )
            days_to_dept = (
                date.fromisoformat(sub["depart_date"]) - date.today()
            ).days
            current_min_price = (
                analysis.get("price_range", [0])[0]
                if analysis.get("price_range")
                else 0
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
            return_analysis = None
            return_nearby_dates = []
            if sub.get("round_trip") and sub.get("return_date"):
                return_route = f"{sub['destination']}-{sub['origin']}"
                return_data = agg.collect(
                    sub["destination"],
                    sub["origin"],
                    sub["return_date"],
                    cabin_classes=sub.get("cabin_classes"),
                )
                return_flights = [
                    _normalize_detail_flight(
                        flight, flight.get("data_source") or flight.get("source")
                    )
                    for flight in (return_data or {}).get("flights", [])
                ]
                if return_flights:
                    save_flight_details(return_route, sub["return_date"], return_flights)
                    save_raw_response(return_route, sub["return_date"], return_data)
                    return_analysis = analyze_all_flights(
                        return_flights,
                        (return_data or {}).get("price_insights"),
                        mode=sub.get("mode", "balanced"),
                        priorities=sub.get("priorities"),
                        user_preferences=preferences,
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
                        "depart_date": sub["return_date"],
                        "date_flexibility": sub.get("return_date_flexibility", 0),
                    }
                    return_nearby_dates = collect_nearby_dates(
                        agg,
                        return_sub,
                        cabin_classes=sub.get("cabin_classes"),
                        target_min_price=return_min_price,
                    )
                    return_analysis["days_to_dept"] = (
                        date.fromisoformat(sub["return_date"]) - date.today()
                    ).days
                    return_analysis["nearby_dates"] = return_nearby_dates
                    analysis["return_analysis"] = return_analysis
                    analysis["round_trip_analysis"] = analyze_round_trip(
                        analysis, return_analysis
                    )
                    analysis["round_trip"] = True
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

            msg = format_html_message(
                analysis_result=analysis,
                route_info={
                    "origin": sub["origin"],
                    "destination": sub["destination"],
                    "depart_date": sub["depart_date"],
                    "cabin_classes": sub.get("cabin_classes"),
                    "mode": sub.get("mode", "balanced"),
                    "priorities": sub.get("priorities"),
                    "budget": sub.get("budget"),
                    "budget_mode": sub.get("budget_mode", "fixed"),
                    "return_date": sub.get("return_date"),
                    "return_date_flexibility": sub.get("return_date_flexibility", 0),
                    "round_trip": sub.get("round_trip", False),
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
                    "hard_constraints": sub.get("hard_constraints", {}),
                    "soft_preferences": sub.get("soft_preferences", {}),
                    "notification_goals": sub.get("notification_goals", {}),
                    "nearby_dates": nearby_dates,
                    "previous_prices": previous_prices,
                    "lowest_price_history": lowest_price_history,
                    "source_stats": data.get("source_stats", {}),
                },
                source_stats=data.get("source_stats"),
                price_insights=data.get("price_insights"),
            )
            send(msg)
            logging.info(f"{route} 已推送方案对比表")

        except Exception as e:
            logging.error(f"{route} 处理失败: {e}", exc_info=True)

    logging.info("本轮执行完成")


if __name__ == "__main__":
    run()
