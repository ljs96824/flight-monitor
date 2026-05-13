import json
import os
from datetime import datetime
from pathlib import Path

from sources.aggregator import FlightAggregator, normalize_combo
from sources.searchapi_source import SearchAPISource
from sources.serpapi_source import SerpAPISource


BASE_DIR = Path(__file__).parent


def get_aggregator() -> FlightAggregator:
    sources = []
    if os.environ.get("SERPAPI_KEY"):
        sources.append(SerpAPISource())
    if os.environ.get("SEARCHAPI_KEY"):
        sources.append(SearchAPISource())
    return FlightAggregator(sources)


def collect_and_classify(
    origin: str, dest: str, date_str: str, target_combo: str
) -> dict | None:
    """
    采集并分类：目标航班 vs 替代方案。
    内部通过聚合器支持多个Google Flights数据源。
    """
    aggregator = get_aggregator()
    if not aggregator.sources:
        print("采集失败: 请在.env文件中设置SERPAPI_KEY或SEARCHAPI_KEY")
        return None

    result = aggregator.collect(origin, dest, date_str, target_combo)
    if result is None:
        print(f"未找到任何航班: {origin}→{dest} {date_str}")
        return None

    all_flights = result.get("flights", [])
    if not all_flights:
        print(f"未找到任何航班: {origin}→{dest} {date_str}")
        return None

    target = None
    match_quality = "none"
    normalized_target_combo = normalize_combo(target_combo)

    for flight in all_flights:
        if normalize_combo(flight.get("flight_combo", "")) == normalized_target_combo:
            target = flight
            match_quality = "exact"
            break

    if target is None:
        for flight in all_flights:
            if (
                flight.get("stopover_city") == "DFW"
                and "AA" in str(flight.get("flight_nos", []))
            ):
                target = flight
                match_quality = "partial"
                break

    if target is None:
        for flight in all_flights:
            if any(
                "American" in airline or "美航" in airline or "AA" in flight_no
                for airline in flight.get("airlines", [])
                for flight_no in flight.get("flight_nos", [])
            ):
                target = flight
                match_quality = "airline_only"
                break

    alternatives = [flight for flight in all_flights if flight != target]
    alternatives.sort(key=lambda flight: flight.get("price") or float("inf"))

    return {
        "target": target,
        "alternatives": alternatives[:5],
        "match_quality": match_quality,
        "price_insights": result.get("price_insights", {}),
        "total_results": len(all_flights),
        "source": result.get("source"),
        "sources_used": result.get("sources_used", []),
        "source_errors": result.get("source_errors", []),
        "price_anomalies": result.get("price_anomalies", []),
    }


def save_raw_response(route: str, date_str: str, raw_json: dict) -> None:
    """保存原始API响应"""
    filepath = BASE_DIR / "data" / "raw_responses.jsonl"
    filepath.parent.mkdir(exist_ok=True)
    record = {
        "route": route,
        "date": date_str,
        "fetched_at": datetime.now().isoformat(),
        "source": raw_json.get("source", "aggregated_google_flights"),
        "raw": raw_json,
    }
    with filepath.open("a", encoding="utf-8") as file:
        file.write(json.dumps(record, ensure_ascii=False) + "\n")
