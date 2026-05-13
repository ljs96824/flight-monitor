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


def fetch_flights(origin: str, dest: str, date_str: str) -> dict:
    """Fetch raw Google Flights response from the first available source."""
    aggregator = get_aggregator()
    if not aggregator.sources:
        raise RuntimeError("请在.env文件中设置SERPAPI_KEY或SEARCHAPI_KEY")

    last_error = None
    for source in aggregator.sources:
        try:
            result = source.fetch(origin, dest, date_str)
        except Exception as exc:
            last_error = exc
            continue

        raw = result.get("raw") or {}
        raw["_data_source"] = result.get("source")
        return raw

    raise RuntimeError(f"所有数据源采集失败: {last_error}")


def calc_layover_minutes(arr_time_str, dep_time_str) -> int:
    """计算两个时间字符串之间的分钟数"""
    fmt = "%Y-%m-%d %H:%M"
    try:
        arr = datetime.strptime(arr_time_str, fmt)
        dep = datetime.strptime(dep_time_str, fmt)
        diff = (dep - arr).total_seconds() / 60
        return max(0, int(diff))
    except Exception:
        return 0


def parse_flight_detail(flight_data: dict, data_source: str | None = None) -> dict:
    """解析单个航班方案的完整信息"""
    segments = flight_data.get("flights", [])

    result = {
        "price": flight_data.get("price"),
        "total_duration_min": flight_data.get("total_duration", 0),
        "stops": len(segments) - 1,
        "data_source": data_source,
        "segments": [],
        "layovers": [],
    }

    for i, seg in enumerate(segments):
        dep = seg.get("departure_airport", {})
        arr = seg.get("arrival_airport", {})

        segment_info = {
            "flight_no": seg.get("flight_number", ""),
            "airline": seg.get("airline", ""),
            "aircraft": seg.get("airplane", ""),
            "dep_airport": dep.get("id", ""),
            "dep_city": dep.get("name", ""),
            "dep_time": dep.get("time", ""),
            "arr_airport": arr.get("id", ""),
            "arr_city": arr.get("name", ""),
            "arr_time": arr.get("time", ""),
            "duration_min": seg.get("duration", 0),
            "cabin_class": seg.get("travel_class", "Economy"),
        }
        result["segments"].append(segment_info)

        # 计算中转等待时间（当前段到达时间 → 下一段出发时间）
        if i < len(segments) - 1:
            next_seg = segments[i + 1]
            next_dep_time = next_seg.get("departure_airport", {}).get("time", "")
            curr_arr_time = arr.get("time", "")
            layover = {
                "city": arr.get("name", ""),
                "airport": arr.get("id", ""),
                "wait_minutes": calc_layover_minutes(curr_arr_time, next_dep_time),
            }
            result["layovers"].append(layover)

    if result["segments"]:
        result["route_summary"] = " → ".join(
            [result["segments"][0]["dep_airport"]]
            + [segment["arr_airport"] for segment in result["segments"]]
        )
    else:
        result["route_summary"] = ""

    result["airline_summary"] = " / ".join(
        sorted({segment["airline"] for segment in result["segments"] if segment["airline"]})
    )
    result["flight_combo"] = "+".join(
        segment["flight_no"] for segment in result["segments"] if segment["flight_no"]
    )
    result["total_hours"] = round(result["total_duration_min"] / 60, 1)
    result["layover_summary"] = (
        "、".join(
            f"{layover['city']}等{layover['wait_minutes'] // 60}h{layover['wait_minutes'] % 60}m"
            for layover in result["layovers"]
        )
        if result["layovers"]
        else "直飞"
    )

    return result


def collect_all_flights(origin, dest, date_str) -> dict:
    """采集所有航班并返回详细解析结果"""
    raw = fetch_flights(origin, dest, date_str)

    all_flights = []
    data_source = raw.get("_data_source")
    for category in ["best_flights", "other_flights"]:
        for flight in raw.get(category, []):
            detail = parse_flight_detail(flight, data_source)
            if detail["price"] is not None:
                all_flights.append(detail)

    # 按价格排序
    all_flights.sort(key=lambda x: x["price"])

    return {
        "flights": all_flights,
        "price_insights": raw.get("price_insights"),
        "total_count": len(all_flights),
        "source": raw.get("_data_source"),
        "collected_at": datetime.now().isoformat(),
    }


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
