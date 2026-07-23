"""提前购买曲线的只读统计引擎。"""

from __future__ import annotations

import math
import os
import sqlite3
import statistics
from contextlib import contextmanager
from datetime import date
from pathlib import Path
from typing import Iterator

from airports import get_airport_city
from method_registry import method_version
from provenance import build_envelope
from source_profiles import get_source_profile, normalize_route_type


BASE_DIR = Path(__file__).resolve().parent
DEFAULT_DB_PATH = BASE_DIR / "data" / "observations.sqlite3"
METHOD_VERSION = method_version("tcurve")


def _positive_int_env(name: str, default: int) -> int:
    try:
        value = int(os.getenv(name, str(default)))
    except (TypeError, ValueError):
        return default
    return value if value > 0 else default


MIN_SAMPLE_FOR_TCURVE = _positive_int_env("MIN_SAMPLE_FOR_TCURVE", 5)
TCURVE_MIN_CELLS = _positive_int_env("TCURVE_MIN_CELLS", 3)


@contextmanager
def readonly_connection(
    db_path: str | Path = DEFAULT_DB_PATH,
    *,
    timeout: float = 3.0,
) -> Iterator[sqlite3.Connection]:
    """打开不会创建文件、不会修改 schema 的 SQLite 连接。"""
    path = Path(db_path).resolve()
    if not path.is_file():
        raise FileNotFoundError(f"观测库不存在: {path}")
    connection = sqlite3.connect(
        f"{path.as_uri()}?mode=ro",
        uri=True,
        timeout=timeout,
    )
    connection.row_factory = sqlite3.Row
    try:
        connection.execute("PRAGMA query_only=ON")
        yield connection
    finally:
        connection.close()


def expected_search_sources(route_type: str | None) -> set[str]:
    """从生产源策略派生列表源集合，自动排除 enrichment。"""
    profile = get_source_profile(route_type)
    return {
        str(item.get("name") or "").strip().lower()
        for item in profile.get("sources") or []
        if str(item.get("role") or "").strip().lower() != "enrichment"
        and str(item.get("name") or "").strip()
    }


def _split_route(route: str) -> tuple[str, str]:
    text = str(route or "").strip()
    for separator in ("->", "→", "—", "-"):
        if separator not in text:
            continue
        origin, dest = (part.strip() for part in text.split(separator, 1))
        if origin and dest:
            return origin, dest
    raise ValueError(f"航线格式无效，应为'出发地-目的地': {route!r}")


def _normalize_pair(airport_pair) -> tuple[str, str] | None:
    if airport_pair in (None, ""):
        return None
    if isinstance(airport_pair, str):
        origin, dest = _split_route(airport_pair)
    else:
        try:
            origin, dest = airport_pair
        except (TypeError, ValueError) as exc:
            raise ValueError("机场对必须为'PVG-KIX'或两个IATA值") from exc
    origin = str(origin or "").strip().upper()
    dest = str(dest or "").strip().upper()
    if not origin or not dest:
        raise ValueError("机场对不能为空")
    return origin, dest


def _clean_number(value: float, digits: int = 2):
    number = round(float(value), digits)
    return int(number) if number.is_integer() else number


def percentile_linear(values: list[float], percentile: float) -> float:
    """按线性插值(type 7)计算分位数。"""
    if not values:
        raise ValueError("分位数输入不能为空")
    if not 0 <= percentile <= 1:
        raise ValueError("percentile必须在0到1之间")
    ordered = sorted(float(value) for value in values)
    if len(ordered) == 1:
        return ordered[0]
    position = (len(ordered) - 1) * percentile
    lower = math.floor(position)
    upper = math.ceil(position)
    if lower == upper:
        return ordered[lower]
    fraction = position - lower
    return ordered[lower] + (ordered[upper] - ordered[lower]) * fraction


def _load_route_rows(
    db_path: str | Path,
    *,
    origin_city: str,
    dest_city: str,
    airport_pair: tuple[str, str] | None,
    timeout: float,
) -> list[dict]:
    columns = (
        "observed_at, route_type, origin_airport, dest_airport, "
        "depart_date, days_to_departure, source, price_cny"
    )
    with readonly_connection(db_path, timeout=timeout) as connection:
        available = {
            row[1]
            for row in connection.execute("PRAGMA table_info(observations)").fetchall()
        }
        required = {
            "observed_at",
            "route_type",
            "origin_airport",
            "dest_airport",
            "depart_date",
            "days_to_departure",
            "source",
            "price_cny",
        }
        missing = sorted(required - available)
        if missing:
            raise RuntimeError(f"observations缺少字段: {', '.join(missing)}")
        if airport_pair:
            rows = connection.execute(
                f"SELECT {columns} FROM observations "
                "WHERE price_cny > 0 AND UPPER(origin_airport)=? AND UPPER(dest_airport)=?",
                airport_pair,
            ).fetchall()
        else:
            rows = connection.execute(
                f"SELECT {columns} FROM observations WHERE price_cny > 0"
            ).fetchall()

    selected = []
    for row in rows:
        item = dict(row)
        row_origin_city = get_airport_city(item.get("origin_airport"))
        row_dest_city = get_airport_city(item.get("dest_airport"))
        if row_origin_city != origin_city or row_dest_city != dest_city:
            continue
        selected.append(item)
    return selected


def fold_tcurve_daily_cells(rows: list[dict]) -> list[dict]:
    """折叠到城市航线、出发日、观测日，并采用当日跨源最低价。"""
    grouped: dict[tuple[str, str, str, str], dict] = {}
    for row in rows:
        observed_at = str(row.get("observed_at") or "")
        observed_day = observed_at[:10]
        depart_date = str(row.get("depart_date") or "")
        try:
            observed_date = date.fromisoformat(observed_day)
            departure_date = date.fromisoformat(depart_date)
            price = float(row.get("price_cny"))
        except (TypeError, ValueError):
            continue
        if price <= 0:
            continue
        origin_city = get_airport_city(row.get("origin_airport"))
        dest_city = get_airport_city(row.get("dest_airport"))
        key = (origin_city, dest_city, depart_date, observed_day)
        cell = grouped.setdefault(
            key,
            {
                "prices": [],
                "priced_sources": [],
                "sources": set(),
                "route_types": set(),
                "stored_t_values": set(),
            },
        )
        cell["prices"].append(price)
        source = str(row.get("source") or "").strip().lower()
        if source:
            cell["sources"].add(source)
            cell["priced_sources"].append((price, source))
        route_type = normalize_route_type(row.get("route_type")) or str(
            row.get("route_type") or "international"
        ).strip().lower()
        cell["route_types"].add(route_type)
        try:
            cell["stored_t_values"].add(int(row.get("days_to_departure")))
        except (TypeError, ValueError):
            pass
        cell["computed_t"] = (departure_date - observed_date).days

    cells = []
    for (origin_city, dest_city, depart_date, observed_day), values in grouped.items():
        expected = set()
        for route_type in values["route_types"]:
            expected.update(expected_search_sources(route_type))
        coverage = set(values["sources"])
        minimum = min(values["prices"])
        computed_t = int(values["computed_t"])
        stored_t_values = values["stored_t_values"]
        cells.append(
            {
                "origin_city": origin_city,
                "dest_city": dest_city,
                "depart_date": depart_date,
                "observed_day": observed_day,
                "days_to_departure": computed_t,
                "stored_t_matches": not stored_t_values or stored_t_values == {computed_t},
                "min_price": _clean_number(minimum),
                "min_sources": sorted(
                    {
                        source
                        for price, source in values["priced_sources"]
                        if price == minimum
                    }
                ),
                "source_coverage": sorted(coverage),
                "expected_sources": sorted(expected),
                "route_types": sorted(values["route_types"]),
                "degraded": bool(expected and not expected.issubset(coverage)),
            }
        )
    return sorted(
        cells,
        key=lambda item: (
            item["depart_date"],
            item["observed_day"],
            item["origin_city"],
            item["dest_city"],
        ),
    )


def _t_ranges(t_values: list[int]) -> list[str]:
    values = sorted(set(int(value) for value in t_values))
    if not values:
        return []
    ranges = []
    start = previous = values[0]
    for value in values[1:]:
        if value == previous + 1:
            previous = value
            continue
        ranges.append(f"T={start}" if start == previous else f"T={start}-{previous}")
        start = previous = value
    ranges.append(f"T={start}" if start == previous else f"T={start}-{previous}")
    return ranges


def build_tcurve(
    db_path: str | Path = DEFAULT_DB_PATH,
    *,
    route: str,
    airport_pair=None,
    include_degraded: bool = False,
    min_sample: int = MIN_SAMPLE_FOR_TCURVE,
    timeout: float = 3.0,
    current_depart_date: str | None = None,
    as_of_date: date | None = None,
) -> dict:
    """构建城市级或指定机场对的提前购买曲线。"""
    if min_sample < 1:
        raise ValueError("min_sample必须大于0")
    origin_city, dest_city = _split_route(route)
    pair = _normalize_pair(airport_pair)
    rows = _load_route_rows(
        db_path,
        origin_city=origin_city,
        dest_city=dest_city,
        airport_pair=pair,
        timeout=timeout,
    )
    all_cells = fold_tcurve_daily_cells(rows)
    degraded_count = sum(1 for cell in all_cells if cell["degraded"])
    included_cells = [
        cell for cell in all_cells if include_degraded or not cell["degraded"]
    ]

    by_t: dict[int, list[dict]] = {}
    for cell in included_cells:
        by_t.setdefault(int(cell["days_to_departure"]), []).append(cell)
    points = []
    for t_value in sorted(by_t):
        t_cells = by_t[t_value]
        prices = [float(cell["min_price"]) for cell in t_cells]
        sufficient = len(prices) >= min_sample
        observed_days = sorted({cell["observed_day"] for cell in t_cells})
        sources = sorted(
            {
                source
                for cell in t_cells
                for source in (cell.get("min_sources") or [])
            }
        )
        excluded_at_t = sum(
            1
            for cell in all_cells
            if int(cell["days_to_departure"]) == t_value
            and cell.get("degraded")
            and not include_degraded
        )
        stat_key = f"tcurve.T{t_value}.median"
        bucket = f"航线={origin_city}-{dest_city}·T={t_value}天"
        if pair:
            bucket += f"·机场对={pair[0]}-{pair[1]}"
        envelope = build_envelope(
            stat_key,
            sample_n=len(prices),
            window=[observed_days[0], observed_days[-1]] if observed_days else [None, None],
            sources=sources,
            degraded_excluded=excluded_at_t,
            bucket=bucket,
        )
        points.append(
            {
                "t": t_value,
                "n": len(prices),
                "median": _clean_number(statistics.median(prices)) if sufficient else None,
                "p25": _clean_number(percentile_linear(prices, 0.25)) if sufficient else None,
                "p75": _clean_number(percentile_linear(prices, 0.75)) if sufficient else None,
                "sufficient": sufficient,
                "status": "ok" if sufficient else "样本不足",
                "provenance": envelope,
            }
        )

    qualified = [point for point in points if point["sufficient"]]
    lowest_median = min(
        (float(point["median"]) for point in qualified),
        default=None,
    )
    lowest_t_values = [
        int(point["t"])
        for point in qualified
        if float(point["median"]) == lowest_median
    ] if lowest_median is not None else []
    t_values = [int(cell["days_to_departure"]) for cell in included_cells]
    coverage = {
        "t_min": min(t_values) if t_values else None,
        "t_max": max(t_values) if t_values else None,
    }
    current_t = None
    if current_depart_date:
        current_t = (
            date.fromisoformat(str(current_depart_date)) - (as_of_date or date.today())
        ).days

    return {
        "route": f"{origin_city}-{dest_city}",
        "origin_city": origin_city,
        "dest_city": dest_city,
        "airport_pair": f"{pair[0]}-{pair[1]}" if pair else None,
        "price_caliber": "单人单程CNY含税",
        "method_version": METHOD_VERSION,
        "min_sample": min_sample,
        "include_degraded": bool(include_degraded),
        "daily_cell_count": len(all_cells),
        "included_cell_count": len(included_cells),
        "daily_cells": all_cells,
        "degraded_count": degraded_count,
        "degraded_excluded_count": 0 if include_degraded else degraded_count,
        "days_to_departure_mismatch_count": sum(
            1 for cell in all_cells if not cell["stored_t_matches"]
        ),
        "depart_dates": sorted({cell["depart_date"] for cell in all_cells}),
        "included_depart_dates": sorted(
            {cell["depart_date"] for cell in included_cells}
        ),
        "coverage": coverage,
        "points": points,
        "qualified_cell_count": len(qualified),
        "lowest_median": _clean_number(lowest_median) if lowest_median is not None else None,
        "lowest_median_t_values": lowest_t_values,
        "lowest_median_t_ranges": _t_ranges(lowest_t_values),
        "current_t": current_t,
    }


def route_cities_from_info(route_info: dict) -> tuple[str, str]:
    """从通知路由信息推导城市，IATA 显示名仍以 airports.py 为准。"""
    route_info = route_info or {}

    def city_for(value, active_values):
        candidates = active_values if isinstance(active_values, list) else []
        candidate = next((item for item in candidates if item), None) or value
        return get_airport_city(candidate)

    origin = city_for(
        route_info.get("origin_city") or route_info.get("origin"),
        route_info.get("origin_airports_active") or route_info.get("origin_airports"),
    )
    dest = city_for(
        route_info.get("destination_city") or route_info.get("destination"),
        route_info.get("destination_airports_active")
        or route_info.get("destination_airports"),
    )
    if not origin or not dest:
        raise ValueError("通知路由缺少可识别的出发地或目的地")
    return origin, dest


def build_notification_tcurve(
    route_info: dict,
    *,
    db_path: str | Path = DEFAULT_DB_PATH,
    min_sample: int = MIN_SAMPLE_FOR_TCURVE,
    as_of_date: date | None = None,
) -> dict:
    origin_city, dest_city = route_cities_from_info(route_info)
    curve = build_tcurve(
        db_path,
        route=f"{origin_city}-{dest_city}",
        min_sample=min_sample,
        current_depart_date=route_info.get("depart_date"),
        as_of_date=as_of_date,
    )
    # 邮件只消费聚合后的 T 格，避免原始日格随观测积累持续放大 payload。
    curve.pop("daily_cells", None)
    return curve


def select_anchor_points(
    points: list[dict],
    current_t: int | None,
    *,
    limit: int = 5,
) -> list[dict]:
    """均匀抽取合格 T 格，并保证包含距当前 T 最近的一格。"""
    qualified = sorted(
        (point for point in points or [] if point.get("sufficient")),
        key=lambda point: int(point["t"]),
    )
    if len(qualified) <= limit:
        return qualified
    nearest_index = min(
        range(len(qualified)),
        key=lambda index: abs(int(qualified[index]["t"]) - int(current_t or 0)),
    )
    selected = {0, len(qualified) - 1, nearest_index}
    while len(selected) < limit:
        candidates = [index for index in range(len(qualified)) if index not in selected]
        next_index = max(
            candidates,
            key=lambda index: (
                min(
                    abs(int(qualified[index]["t"]) - int(qualified[chosen]["t"]))
                    for chosen in selected
                ),
                -index,
            ),
        )
        selected.add(next_index)
    return [qualified[index] for index in sorted(selected)]
