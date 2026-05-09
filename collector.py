import json
import os
from datetime import datetime
from pathlib import Path

from serpapi import GoogleSearch


SERPAPI_KEY = os.environ.get("SERPAPI_KEY")
BASE_DIR = Path(__file__).parent


def _normalize_combo(combo: str) -> str:
    return combo.replace(" ", "").upper()


def fetch_flights(origin: str, dest: str, date_str: str) -> dict:
    """
    调用SerpAPI的Google Flights接口
    date_str格式: "2026-06-20"
    返回原始搜索结果
    """
    params = {
        "engine": "google_flights",
        "departure_id": origin,
        "arrival_id": dest,
        "outbound_date": date_str,
        "type": "2",  # 单程
        "currency": "CNY",
        "hl": "zh-CN",
        "sort": "2",  # 按价格排序
        "stops": "2",  # 1次中转或更少
        "api_key": SERPAPI_KEY,
    }

    search = GoogleSearch(params)
    results = search.get_dict()
    return results


def parse_flights(results: dict) -> list[dict]:
    """
    从SerpAPI返回的结果中提取航班信息
    Google Flights的结果结构：
    - best_flights: 推荐航班列表
    - other_flights: 其他航班列表
    - price_insights: 价格分析（Google自己的）
    """
    all_flights = []

    for category in ["best_flights", "other_flights"]:
        flights = results.get(category, [])
        for flight in flights:
            # 每个flight包含flights数组（各段航程）和总价
            segments = flight.get("flights", [])
            if not segments:
                continue

            flight_nos = []
            airlines = []
            cities = []

            for seg in segments:
                fn = seg.get("flight_number", "")
                airline = seg.get("airline", "")
                flight_nos.append(fn)
                airlines.append(airline)
                dep_airport = seg.get("departure_airport", {})
                arr_airport = seg.get("arrival_airport", {})
                if not cities:
                    cities.append(dep_airport.get("id", ""))
                cities.append(arr_airport.get("id", ""))

            parsed = {
                "price": flight.get("price"),
                "flight_nos": flight_nos,
                "flight_combo": "+".join(flight_nos),
                "airlines": list(set(airlines)),
                "airline": airlines[0] if airlines else "",
                "route_summary": " → ".join(cities),
                "duration_hours": round(flight.get("total_duration", 0) / 60, 2),
                "stopover_city": cities[1] if len(cities) > 2 else None,
                "stopovers": len(segments) - 1,
            }

            if parsed["price"] is not None:
                all_flights.append(parsed)

    return all_flights


def collect_and_classify(
    origin: str, dest: str, date_str: str, target_combo: str
) -> dict | None:
    """
    采集并分类：目标航班 vs 替代方案
    同时提取Google的price_insights
    """
    try:
        results = fetch_flights(origin, dest, date_str)
    except Exception as e:
        print(f"采集失败: {e}")
        return None

    # 保存原始响应
    save_raw_response(f"{origin}-{dest}", date_str, results)

    all_flights = parse_flights(results)

    if not all_flights:
        print(f"未找到任何航班: {origin}→{dest} {date_str}")
        return None

    # 分类：精确匹配 → 模糊匹配 → 航司匹配
    target = None
    match_quality = "none"
    normalized_target_combo = _normalize_combo(target_combo)

    for f in all_flights:
        if _normalize_combo(f["flight_combo"]) == normalized_target_combo:
            target = f
            match_quality = "exact"
            break

    if target is None:
        # 模糊匹配：经DFW中转的AA航班
        for f in all_flights:
            if f.get("stopover_city") == "DFW" and "AA" in str(f.get("airlines", [])):
                target = f
                match_quality = "partial"
                break

    if target is None:
        # 航司匹配：任何AA航班
        for f in all_flights:
            if any(
                "American" in a or "AA" in fn
                for a in f.get("airlines", [])
                for fn in f.get("flight_nos", [])
            ):
                target = f
                match_quality = "airline_only"
                break

    # 替代方案：排除target后最便宜的5个
    alternatives = [f for f in all_flights if f != target]
    alternatives.sort(key=lambda x: x["price"])
    alternatives = alternatives[:5]

    # 提取Google的价格洞察（额外价值）
    price_insights = results.get("price_insights", {})

    return {
        "target": target,
        "alternatives": alternatives,
        "match_quality": match_quality,
        "price_insights": price_insights,
        "total_results": len(all_flights),
    }


def save_raw_response(route: str, date_str: str, raw_json: dict):
    """保存原始API响应"""
    filepath = BASE_DIR / "data" / "raw_responses.jsonl"
    filepath.parent.mkdir(exist_ok=True)
    record = {
        "route": route,
        "date": date_str,
        "fetched_at": datetime.now().isoformat(),
        "source": "serpapi_google_flights",
        "raw": raw_json,
    }
    with open(filepath, "a", encoding="utf-8") as f:
        f.write(json.dumps(record, ensure_ascii=False) + "\n")
