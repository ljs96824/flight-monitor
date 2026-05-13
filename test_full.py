"""End-to-end smoke test without sending notifications."""

from __future__ import annotations

import json
import sys
from datetime import date, datetime
from pathlib import Path

import yaml
from dotenv import load_dotenv

from analyzer import analyze, analyze_all_flights
from collector import collect_all_flights, collect_and_classify
from notifier import format_comparison_message, format_message
from storage import DB_PATH, init_db, save_flight_details, save_snapshots


BASE_DIR = Path(__file__).parent

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8")


def _days_before_dept(depart_date: str) -> int:
    return (date.fromisoformat(depart_date) - date.today()).days


def _build_records(data: dict, route: str, depart_date: str) -> list[dict]:
    snapshot_time = datetime.now().isoformat(timespec="seconds")
    days_before_dept = _days_before_dept(depart_date)
    records = []

    if data.get("target"):
        records.append(
            {
                **data["target"],
                "is_target": 1,
                "route": route,
                "depart_date": depart_date,
                "snapshot_time": snapshot_time,
                "days_before_dept": days_before_dept,
            }
        )

    for alternative in data.get("alternatives", []):
        records.append(
            {
                **alternative,
                "is_target": 0,
                "route": route,
                "depart_date": depart_date,
                "snapshot_time": snapshot_time,
                "days_before_dept": days_before_dept,
            }
        )

    return records


def _run_single_flight(subscription: dict, route: str, target_combo: str) -> None:
    print("开始完整流程测试：单航班追踪模式\n")

    data = collect_and_classify(
        subscription["origin"],
        subscription["destination"],
        subscription["depart_date"],
        target_combo,
    )

    if data is None:
        print("采集失败：collector 返回 None")
        return

    records = _build_records(data, route, subscription["depart_date"])
    if records:
        save_snapshots(records)

    target = data.get("target") or {}
    target_price = target.get("price")
    print("采集结果：")
    print(f"- 数据源: {data.get('source')} / {data.get('sources_used')}")
    print(f"- 航班数: {data.get('total_results')}")
    print(f"- 目标航班: {target.get('flight_combo') or '未匹配'}")
    print(f"- 目标航班价格: ¥{target_price if target_price is not None else '-'}")
    if data.get("price_anomalies"):
        print(f"- 价格异常: {json.dumps(data['price_anomalies'], ensure_ascii=False)}")

    analysis = analyze(
        str(DB_PATH),
        route,
        subscription["depart_date"],
        target_combo,
        data.get("price_insights", {}),
    )

    print("\n分析结果：")
    print(f"- 分位: {analysis.get('percentile')}")
    print(f"- 动量类型: {analysis.get('movement')}")
    print(f"- 波动率: {analysis.get('volatility')}")
    print(f"- 等待价值: {analysis.get('waiting_value')}")
    print(f"- 最终信号: {analysis.get('signal')}")
    print(f"- 信号原因: {analysis.get('signal_reason')}")

    message = format_message(analysis, trigger_reason="test_full")
    print("\n推送消息：")
    print(message)


def _run_all_flights(subscription: dict, route: str) -> None:
    print("开始完整流程测试：全航线方案对比模式\n")

    data = collect_all_flights(
        subscription["origin"],
        subscription["destination"],
        subscription["depart_date"],
    )

    if data is None or not data.get("flights"):
        print("采集失败：没有返回可用航班方案")
        return

    flights = data.get("flights", [])
    save_flight_details(route, subscription["depart_date"], flights)

    cheapest = flights[0] if flights else {}
    print("采集结果：")
    print(f"- 数据源: {data.get('source')}")
    print(f"- 航班数: {data.get('total_count')}")
    print(f"- 最低价方案: {cheapest.get('flight_combo') or '-'}")
    print(f"- 最低价价格: ¥{cheapest.get('price') if cheapest.get('price') is not None else '-'}")

    analysis = analyze_all_flights(flights, data.get("price_insights"))

    print("\n分析结果：")
    print(f"- 方案总数: {analysis.get('total_options')}")
    print(f"- 价格区间: {analysis.get('price_range')}")
    print(f"- 时长区间: {analysis.get('duration_range')}")
    print("- 推荐方案:")
    for rec in analysis.get("recommendations", []):
        flight = rec.get("flight", {})
        print(f"  {rec.get('tag')} {flight.get('flight_combo')} ¥{flight.get('price')} - {rec.get('reason')}")

    route_info = {
        "origin": subscription["origin"],
        "destination": subscription["destination"],
        "depart_date": subscription["depart_date"],
    }
    message = format_comparison_message(analysis, route_info)
    print("\n推送消息：")
    print(message)


def main() -> None:
    load_dotenv(BASE_DIR / ".env", encoding="utf-8")
    init_db()

    config = yaml.safe_load((BASE_DIR / "config.yaml").read_text(encoding="utf-8"))
    subscription = config["subscriptions"][0]
    route = f"{subscription['origin']}-{subscription['destination']}"
    target_combo = subscription.get("target_combo", None)

    if target_combo:
        _run_single_flight(subscription, route, target_combo)
    else:
        _run_all_flights(subscription, route)


if __name__ == "__main__":
    main()
