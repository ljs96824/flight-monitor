"""航班规律的只读描述统计，不参与方案排序或判断。"""

from __future__ import annotations

import os
import re
import statistics
from collections import defaultdict
from datetime import date

from method_registry import method_version
from provenance import load_route_observations
from tcurve import _clean_number, percentile_linear


METHOD_VERSION = method_version("patterns")
REGULAR_RATE = 0.80
OCCASIONAL_RATE = 0.20


def _positive_int_env(name, default):
    try:
        value = int(os.environ.get(name, default))
    except (TypeError, ValueError):
        return default
    return value if value > 0 else default


MIN_PATTERN_N = _positive_int_env("MIN_PATTERN_N", 5)


def _observed_day(row):
    return str(row.get("observed_at") or "")[:10]


def _carrier(row):
    explicit = str(row.get("airline") or "").strip().upper()
    if explicit:
        return explicit
    match = re.match(r"([A-Z0-9]{2})", str(row.get("flight_combo") or "").upper())
    return match.group(1) if match else "未知"


def _appearance_label(rate, n):
    if abs(float(rate) - 1.0) < 1e-12:
        return f"在{n}次有效观测中均出现(100%)"
    category = "常驻" if rate >= REGULAR_RATE else "偶发" if rate <= OCCASIONAL_RATE else "常见"
    return f"{category}({rate * 100:.0f}%·n={n})"


def build_patterns(rows, *, min_n=MIN_PATTERN_N):
    physical = {}
    for row in rows:
        day = _observed_day(row)
        combo = str(row.get("flight_combo") or "").strip().upper()
        depart = str(row.get("depart_date") or "")
        if not day or not combo or not depart:
            continue
        key = (day, depart, combo)
        current = physical.get(key)
        if current is None or float(row.get("price_cny") or 0) < float(current.get("price_cny") or 0):
            physical[key] = dict(row)
    items = list(physical.values())
    observed_days = sorted({_observed_day(item) for item in items})
    denominator = len(observed_days)

    combo_days = defaultdict(set)
    for item in items:
        combo_days[str(item["flight_combo"]).upper()].add(_observed_day(item))
    occurrence = []
    for combo, days in combo_days.items():
        n = len(days)
        rate = n / denominator if denominator else 0
        occurrence.append({"combo": combo, "n": n, "observed_day_n": denominator, "rate": _clean_number(rate, 4), "label": _appearance_label(rate, n), "sufficient": n >= min_n})
    occurrence.sort(key=lambda item: (-item["rate"], item["combo"]))

    carrier_prices = defaultdict(list)
    lowest_days = defaultdict(int)
    by_day = defaultdict(list)
    for item in items:
        carrier = _carrier(item)
        price = float(item.get("price_cny") or 0)
        if price <= 0:
            continue
        carrier_prices[carrier].append(price)
        by_day[_observed_day(item)].append((price, carrier))
    for day_items in by_day.values():
        minimum = min(price for price, _ in day_items)
        for carrier in {carrier for price, carrier in day_items if price == minimum}:
            lowest_days[carrier] += 1
    carrier_positions = []
    for carrier, prices in carrier_prices.items():
        carrier_positions.append({"carrier": carrier, "n": len(prices), "median_price": _clean_number(statistics.median(prices)), "lowest_day_share": _clean_number(lowest_days[carrier] / max(1, len(by_day)), 4), "basis": "市场承运口径", "sufficient": len(prices) >= min_n})
    carrier_positions.sort(key=lambda item: (item["median_price"], item["carrier"]))

    combo_depart_dates = defaultdict(set)
    for item in items:
        combo_depart_dates[str(item["flight_combo"]).upper()].add(str(item["depart_date"]))
    weekday = []
    for combo, depart_dates in combo_depart_dates.items():
        counts = defaultdict(int)
        for depart in depart_dates:
            counts[date.fromisoformat(depart).weekday()] += 1
        weekday.append({"combo": combo, "depart_date_n": len(depart_dates), "weekday_counts": dict(sorted(counts.items())), "sufficient": len(depart_dates) >= min_n})
    weekday.sort(key=lambda item: item["combo"])

    direct = transfer = 0
    for item in items:
        stops = item.get("stops")
        if stops is None:
            stops = max(0, len(str(item.get("flight_combo") or "").split("+")) - 1)
        if int(stops) == 0:
            direct += 1
        else:
            transfer += 1
    total = direct + transfer
    supply = {"n": total, "direct": direct, "transfer": transfer, "direct_share": _clean_number(direct / total, 4) if total else None, "transfer_share": _clean_number(transfer / total, 4) if total else None, "basis": "基于组合结构", "sufficient": total >= min_n}

    return {"method_version": METHOD_VERSION, "observed_day_n": denominator, "combo_occurrence": occurrence, "carrier_price_position": carrier_positions, "weekday_stability": weekday, "supply_mix": supply, "departure_period": {"status": "字段不可得", "reason": "面板未存起飞时刻(obs_store v1),待schema扩展后自动点亮"}}


def build_route_patterns(
    db_path,
    *,
    route,
    airport_pair=None,
    min_n=MIN_PATTERN_N,
    as_of_day=None,
):
    rows = load_route_observations(db_path, route=route, airport_pair=airport_pair)
    if as_of_day is not None:
        rows = [row for row in rows if _observed_day(row) <= str(as_of_day)]
    return build_patterns(rows, min_n=min_n)
