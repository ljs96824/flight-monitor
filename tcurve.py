"""提前购买曲线的只读统计引擎。"""

from __future__ import annotations

import math
import os
import sqlite3
import statistics
from collections import Counter
from contextlib import contextmanager
from datetime import date
from pathlib import Path
from typing import Iterator

from airports import get_airport_city
from collection_ledger import derive_daily_cell_state
from method_registry import method_version
from observation_time import resolve_observed_day_shanghai
from provenance import build_envelope
from source_profiles import expected_listing_sources, normalize_route_type
from subscription_preflight import shanghai_today


BASE_DIR = Path(__file__).resolve().parent
DEFAULT_DB_PATH = BASE_DIR / "data" / "observations.sqlite3"
METHOD_VERSION = method_version("tcurve")
_SAMPLE_ROLE_PRIORITY = {
    "legacy": 0,
    "cross_sectional_probe": 1,
    "user_monitor": 2,
    "trajectory_anchor": 3,
}


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


def expected_search_sources(
    route_type: str | None,
    observed_day: str | date | None = None,
) -> set[str]:
    """从生产源策略派生列表源集合，自动排除 enrichment。"""
    return expected_listing_sources(
        route_type,
        observed_day=observed_day,
        cabin_class="economy",
    )


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
    with readonly_connection(db_path, timeout=timeout) as connection:
        available = {
            row[1]
            for row in connection.execute("PRAGMA table_info(observations)").fetchall()
        }
        columns = ", ".join(
            [
                "observed_at",
                (
                    "observed_at_utc"
                    if "observed_at_utc" in available
                    else "NULL AS observed_at_utc"
                ),
                (
                    "observed_day_shanghai"
                    if "observed_day_shanghai" in available
                    else "NULL AS observed_day_shanghai"
                ),
                (
                    "legacy_time_ambiguous"
                    if "legacy_time_ambiguous" in available
                    else "0 AS legacy_time_ambiguous"
                ),
                "round_id",
                "route_type",
                "origin_airport",
                "dest_airport",
                "depart_date",
                "days_to_departure",
                "cabin_class",
                "source",
                "price_cny",
            ]
        )
        required = {
            "observed_at",
            "round_id",
            "route_type",
            "origin_airport",
            "dest_airport",
            "depart_date",
            "days_to_departure",
            "cabin_class",
            "source",
            "price_cny",
        }
        missing = sorted(required - available)
        if missing:
            raise RuntimeError(f"observations缺少字段: {', '.join(missing)}")
        if airport_pair:
            rows = connection.execute(
                f"SELECT {columns} FROM observations "
                "WHERE price_cny > 0 AND LOWER(cabin_class)='economy' "
                "AND UPPER(origin_airport)=? AND UPPER(dest_airport)=?",
                airport_pair,
            ).fetchall()
        else:
            rows = connection.execute(
                f"SELECT {columns} FROM observations "
                "WHERE price_cny > 0 AND LOWER(cabin_class)='economy'"
            ).fetchall()
        has_collection_cells = connection.execute(
            "SELECT 1 FROM sqlite_schema WHERE type='table' AND name='collection_cells'"
        ).fetchone()
        role_rows = (
            connection.execute(
                """
                SELECT round_id, request_fingerprint, source, origin_airport,
                       dest_airport, depart_date, cabin_class,
                       observed_day_shanghai, sample_role, cohort_id,
                       execution_status, valid_result_count, skip_reason_code,
                       error_type, error_code
                FROM collection_cells
                WHERE LOWER(cabin_class)='economy'
                """
            ).fetchall()
            if has_collection_cells
            else []
        )

    role_map = {}
    daily_outcome_map = {}
    for role_row in role_rows:
        role_item = dict(role_row)
        key = (
            str(role_item.get("round_id") or ""),
            str(role_item.get("source") or "").lower(),
            str(role_item.get("origin_airport") or "").upper(),
            str(role_item.get("dest_airport") or "").upper(),
            str(role_item.get("depart_date") or ""),
            str(role_item.get("cabin_class") or "economy").lower(),
        )
        role = str(role_item.get("sample_role") or "legacy")
        previous = role_map.get(key)
        if previous is None or _SAMPLE_ROLE_PRIORITY.get(role, 0) > _SAMPLE_ROLE_PRIORITY.get(
            previous[0], 0
        ):
            role_map[key] = (role, role_item.get("cohort_id"))
        daily_key = (
            str(role_item.get("origin_airport") or "").upper(),
            str(role_item.get("dest_airport") or "").upper(),
            str(role_item.get("depart_date") or ""),
            str(role_item.get("observed_day_shanghai") or ""),
            str(role_item.get("cabin_class") or "economy").lower(),
        )
        daily_outcome_map.setdefault(daily_key, []).append(role_item)

    selected = []
    for row in rows:
        item = dict(row)
        row_origin_city = get_airport_city(item.get("origin_airport"))
        row_dest_city = get_airport_city(item.get("dest_airport"))
        if row_origin_city != origin_city or row_dest_city != dest_city:
            continue
        lineage_key = (
            str(item.get("round_id") or ""),
            str(item.get("source") or "").lower(),
            str(item.get("origin_airport") or "").upper(),
            str(item.get("dest_airport") or "").upper(),
            str(item.get("depart_date") or ""),
            str(item.get("cabin_class") or "economy").lower(),
        )
        role, cohort_id = role_map.get(lineage_key, ("legacy", None))
        item["sample_role"] = role
        item["cohort_id"] = cohort_id
        daily_key = (
            str(item.get("origin_airport") or "").upper(),
            str(item.get("dest_airport") or "").upper(),
            str(item.get("depart_date") or ""),
            str(item.get("observed_day_shanghai") or ""),
            str(item.get("cabin_class") or "economy").lower(),
        )
        item["collection_outcomes"] = daily_outcome_map.get(daily_key, [])
        selected.append(item)
    return selected


def fold_tcurve_daily_cells(rows: list[dict]) -> list[dict]:
    """折叠到城市航线、出发日、观测日，并采用当日跨源最低价。"""
    grouped: dict[tuple[str, str, str, str], dict] = {}
    for row in rows:
        observed_day, timestamp_source = resolve_observed_day_shanghai(row)
        if observed_day is None:
            continue
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
                "round_ids": set(),
                "lineage_missing": False,
                "timestamp_sources": set(),
                "sample_roles": set(),
                "cohort_ids": set(),
                "collection_outcomes": {},
            },
        )
        cell["timestamp_sources"].add(timestamp_source)
        cell["sample_roles"].add(str(row.get("sample_role") or "legacy"))
        if row.get("cohort_id"):
            cell["cohort_ids"].add(str(row["cohort_id"]))
        for outcome in row.get("collection_outcomes") or []:
            fingerprint = str(outcome.get("request_fingerprint") or "")
            if fingerprint:
                cell["collection_outcomes"][fingerprint] = outcome
        cell["prices"].append(price)
        source = str(row.get("source") or "").strip().lower()
        if source:
            cell["sources"].add(source)
            cell["priced_sources"].append((price, source))
        route_type = normalize_route_type(row.get("route_type")) or str(
            row.get("route_type") or "international"
        ).strip().lower()
        cell["route_types"].add(route_type)
        round_id = str(row.get("round_id") or "").strip()
        if round_id:
            cell["round_ids"].add(round_id)
        else:
            cell["lineage_missing"] = True
        try:
            cell["stored_t_values"].add(int(row.get("days_to_departure")))
        except (TypeError, ValueError):
            pass
        cell["computed_t"] = (departure_date - observed_date).days

    cells = []
    for (origin_city, dest_city, depart_date, observed_day), values in grouped.items():
        expected = set()
        for route_type in values["route_types"]:
            expected.update(expected_search_sources(route_type, observed_day))
        coverage = set(values["sources"])
        outcomes = list(values["collection_outcomes"].values())
        collection_state = (
            derive_daily_cell_state(outcomes, expected) if outcomes else "legacy"
        )
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
                "round_ids": sorted(values["round_ids"]),
                "lineage_complete": not values["lineage_missing"],
                "timestamp_sources": sorted(values["timestamp_sources"]),
                "sample_roles": sorted(values["sample_roles"]),
                "cohort_ids": sorted(values["cohort_ids"]),
                "collection_state": collection_state,
                "degraded": (
                    bool(expected and not expected.issubset(coverage))
                    if collection_state == "legacy"
                    else collection_state != "valid"
                ),
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
    ambiguous_excluded_count = sum(
        1 for row in rows if resolve_observed_day_shanghai(row)[0] is None
    )
    legacy_fallback_row_count = sum(
        1
        for row in rows
        if resolve_observed_day_shanghai(row)[1] == "legacy_fallback"
    )
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
        role_counts = Counter(
            role
            for cell in t_cells
            for role in (cell.get("sample_roles") or ["legacy"])
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
                "sample_role_counts": dict(sorted(role_counts.items())),
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
            date.fromisoformat(str(current_depart_date))
            - (as_of_date or shanghai_today())
        ).days

    sample_role_counts = Counter(
        role
        for cell in included_cells
        for role in (cell.get("sample_roles") or ["legacy"])
    )
    collection_state_counts = Counter(
        str(cell.get("collection_state") or "legacy") for cell in included_cells
    )
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
        "ambiguous_excluded_count": ambiguous_excluded_count,
        "legacy_fallback_row_count": legacy_fallback_row_count,
        "included_cell_count": len(included_cells),
        "sample_role_counts": dict(sorted(sample_role_counts.items())),
        "collection_state_counts": dict(sorted(collection_state_counts.items())),
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


def load_tcurve_daily_cells(
    db_path: str | Path = DEFAULT_DB_PATH,
    *,
    route: str,
    airport_pair=None,
    timeout: float = 3.0,
) -> list[dict]:
    """读取并折叠 P4 日格；调用方自行决定是否保留退化日。"""
    origin_city, dest_city = _split_route(route)
    pair = _normalize_pair(airport_pair)
    rows = _load_route_rows(
        db_path,
        origin_city=origin_city,
        dest_city=dest_city,
        airport_pair=pair,
        timeout=timeout,
    )
    return fold_tcurve_daily_cells(rows)


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
