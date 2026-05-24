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

import yaml

from analyzer import (
    analyze_all_flights,
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
        active.append(
            {
                "name": item.get("name") or "网页订阅",
                "origin": item.get("origin", "").strip().upper(),
                "destination": item.get("destination", "").strip().upper(),
                "depart_date": item.get("depart_date", ""),
                "budget": item.get("budget"),
                "return_date": item.get("return_date"),
                "round_trip": bool(item.get("round_trip", False)),
                "date_flexibility": item.get("date_flexibility", 0),
                "direct_only": item.get("direct_only", "flexible"),
                "red_eye": item.get("red_eye", "reject"),
                "need_baggage": item.get("need_baggage", "unknown"),
                "trip_type": item.get("trip_type", "tourism"),
                "goals": item.get("goals", []),
                "mode": item.get("mode", "balanced"),
                "cabin_classes": item.get("cabin_classes"),
                "priorities": item.get("priorities"),
            }
        )
    return [
        sub
        for sub in active
        if sub.get("origin") and sub.get("destination") and sub.get("depart_date")
    ]


def load_all_subscriptions(config: dict) -> list[dict]:
    yaml_subscriptions = config.get("subscriptions", []) if config else []
    return list(yaml_subscriptions) + load_file_subscriptions()


def subscription_preferences(sub: dict) -> dict:
    return {
        "direct_only": sub.get("direct_only", "flexible"),
        "red_eye": sub.get("red_eye", "reject"),
        "need_baggage": sub.get("need_baggage", "unknown"),
        "trip_type": sub.get("trip_type", "tourism"),
        "goals": sub.get("goals", []),
        "budget": sub.get("budget"),
        "date_flexibility": sub.get("date_flexibility", 0),
        "round_trip": sub.get("round_trip", False),
        "return_date": sub.get("return_date"),
    }


def collect_nearby_dates(
    aggregator: FlightAggregator,
    sub: dict,
    cabin_classes=None,
) -> list[dict]:
    try:
        days_range = int(sub.get("date_flexibility") or 0)
    except (TypeError, ValueError):
        days_range = 0
    if days_range <= 0:
        return []

    center = date.fromisoformat(sub["depart_date"])
    results = []
    for offset in range(-days_range, days_range + 1):
        check_date = center + timedelta(days=offset)
        date_str = check_date.isoformat()
        if date_str == sub["depart_date"]:
            continue
        try:
            data = aggregator.collect(
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
            results.append(
                {
                    "date": date_str,
                    "offset": offset,
                    "min_price": min(prices) if prices else None,
                    "count": len(flights),
                }
            )
        except Exception as exc:
            logging.error(f"{date_str} 相邻日期采集失败: {exc}")
            results.append(
                {"date": date_str, "offset": offset, "min_price": None, "count": 0}
            )
    return results


def run():
    # 初始化
    init_db()
    config = yaml.safe_load(
        (BASE_DIR / "config.yaml").read_text(encoding="utf-8")
    )
    subscriptions = load_all_subscriptions(config)

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
            nearby_dates = collect_nearby_dates(
                agg,
                sub,
                cabin_classes=sub.get("cabin_classes"),
            )
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
            price_history = (data.get("price_insights") or {}).get("price_history")
            analysis["days_to_dept"] = days_to_dept
            analysis["budget"] = sub.get("budget")
            analysis["goals"] = sub.get("goals", [])
            analysis["nearby_dates"] = nearby_dates
            analysis["source_stats"] = data.get("source_stats", {})
            analysis["price_position"] = price_position_description(
                current_min_price, price_history
            )
            analysis["waiting_risk"] = waiting_risk_description(
                price_history, current_min_price, days_to_dept
            )
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
                    "return_date": sub.get("return_date"),
                    "round_trip": sub.get("round_trip", False),
                    "date_flexibility": sub.get("date_flexibility", 0),
                    "direct_only": sub.get("direct_only", "flexible"),
                    "red_eye": sub.get("red_eye", "reject"),
                    "need_baggage": sub.get("need_baggage", "unknown"),
                    "trip_type": sub.get("trip_type", "tourism"),
                    "goals": sub.get("goals", []),
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
