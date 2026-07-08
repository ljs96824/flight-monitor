"""Run a deterministic full-chain snapshot for the standard same-day business trip.

This script intentionally uses fixture flight data instead of live APIs. It keeps the
baseline repeatable while still exercising collection shaping, analyzer logic, and
notification payload/rendering without sending anything.
"""

from __future__ import annotations

import argparse
import json
import sys
import types
from copy import deepcopy
from pathlib import Path
from typing import Iterable

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="strict")

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from airport_logistics import estimate_airport_to_meeting
from analyzer import (
    _flight_arrival_datetime,
    _flight_departure_datetime,
    _same_day_outbound_passes_window,
    _same_day_return_passes_window,
    analyze_round_trip,
    compute_same_day_windows,
)
from price_calendar import analyze_row_savings, roundtrip_calendar_rows
from price_estimator import passenger_price_factor
from pricing import budget_to_pp, itinerary_price_pp, price_in_scope

try:
    import httpx  # noqa: F401
except ModuleNotFoundError:
    class _OfflineResponse:
        text = "{}"

        def json(self):
            return {}

    def _offline_post(*args, **kwargs):
        return _OfflineResponse()

    sys.modules["httpx"] = types.SimpleNamespace(post=_offline_post)

import notifier

DEPART_DATE = "2026-06-26"
RETURN_DATE = "2026-06-26"
MEETING_LOCATION = "大兴区"
ROUTE_TYPE = "domestic"
PASSENGERS = {"adult": 3, "child": 0, "elderly": 0, "infant": 0}
TARGET_PRICE = 1200
MAX_BUDGET = 1700
MAX_BUDGET_SCOPE = "per_person"
TARGET_PRICE_SCOPE = "per_person"
PRICE_SCOPE_UNIT_ONEWAY = "单人单程参考价"
PRICE_SCOPE_UNIT_ROUNDTRIP = "单人往返参考价"
PRICE_SCOPE_PASSENGER_ROUNDTRIP = "3人往返总价"
PRICE_SCOPE_PASSENGER_ROUNDTRIP_REF = "3人往返参考价"
PRICE_SCOPE_TOTAL_BUDGET = "单人往返预算"


def _budget_visible_scope(scope: str | None) -> str:
    return "all_passengers_roundtrip" if str(scope or "").lower() in {"all", "total", "all_passengers"} else "per_person_roundtrip"

def _budget_scope_label(scope: str | None) -> str:
    return "全员往返预算" if _budget_visible_scope(scope) == "all_passengers_roundtrip" else "单人往返预算"


def standard_subscription() -> dict:
    """Return the fixed baseline subscription requested for regression snapshots."""
    constraints = {
        "same_day_round_trip": True,
        "business_start": "10:30",
        "business_end": "17:00",
        "meeting_location": MEETING_LOCATION,
        "meeting_importance": "important",
        "transport_mode": "taxi",
        "direct_only": True,
        "transfer_policy": "direct_only",
        "baggage": "required",
        "need_baggage": "required",
        "checked_baggage_required": True,
        "budget_scope": MAX_BUDGET_SCOPE,
        "max_budget_scope": MAX_BUDGET_SCOPE,
        "target_price_scope": TARGET_PRICE_SCOPE,
        "passengers": dict(PASSENGERS),
        "passenger_count": 3,
        "route_type": ROUTE_TYPE,
    }
    return {
        "id": "snapshot-standard-shanghai-beijing-20260626",
        "status": "active",
        "basic": {
            "route_type": ROUTE_TYPE,
            "origin": "上海",
            "destination": "北京",
            "origin_airports_active": ["PVG", "SHA"],
            "destination_airports_active": ["PEK", "PKX"],
            "round_trip": True,
            "depart_date": DEPART_DATE,
            "return_date": RETURN_DATE,
            "passenger_count": 3,
        },
        "preferences": {
            "passengers": dict(PASSENGERS),
            "target_price": TARGET_PRICE,
            "max_budget": MAX_BUDGET,
            "budget_scope": MAX_BUDGET_SCOPE,
            "max_budget_scope": MAX_BUDGET_SCOPE,
            "target_price_scope": TARGET_PRICE_SCOPE,
        },
        "hard_constraints": dict(constraints),
        "constraints": dict(constraints),
        "soft_preferences": {
            "travel_scenarios": ["business", "meeting"],
            "trip_natures": ["business", "meeting"],
        },
        "notification_goals": {"main_goal": "suitable_price", "frequency": "important_only"},
    }


def _flight(
    flight_no: str,
    airline: str,
    origin: str,
    dest: str,
    dep: str,
    arr: str,
    price: int,
    aircraft: str,
    *,
    dep_date: str = DEPART_DATE,
    arr_date: str = DEPART_DATE,
) -> dict:
    return {
        "flight_no": flight_no,
        "flight_combo": flight_no,
        "airline": airline,
        "departure_airport": origin,
        "arrival_airport": dest,
        "departure_date": dep_date,
        "arrival_date": arr_date,
        "departure_time": dep,
        "arrival_time": arr,
        "price": price,
        "stops": 0,
        "aircraft": aircraft,
        "total_duration_min": 135,
        "buyability": {"label": "需支付页确认"},
        "fare_rules": {
            "baggage": {"included": True, "checked_kg": 20, "note": "含20kg托运"},
            "refund": {"label": "退改适中"},
        },
    }


FIXTURE_FLIGHTS: dict[tuple[str, str, str], list[dict]] = {
    ("PVG", "PKX", DEPART_DATE): [
        _flight("MU5185", "\u4e1c\u65b9\u822a\u7a7a", "PVG", "PKX", "22:30", "00:05", 1050, "32N", arr_date="2026-06-27"),
    ],
    ("SHA", "PKX", DEPART_DATE): [
        _flight("MU5099", "东方航空", "SHA", "PKX", "07:00", "09:15", 831, "333"),
        _flight("MU5121", "东方航空", "SHA", "PKX", "08:10", "10:25", 980, "32A"),
    ],
    ("PVG", "PEK", DEPART_DATE): [
        _flight("CA1566", "\u4e2d\u56fd\u56fd\u9645\u822a\u7a7a", "PVG", "PEK", "22:50", "00:55", 980, "78A", arr_date="2026-06-27"),
    ],
    ("SHA", "PEK", DEPART_DATE): [
        _flight("MU5107", "东方航空", "SHA", "PEK", "11:00", "13:15", 1036, "333"),
    ],
    ("PKX", "SHA", RETURN_DATE): [
        _flight("MU5128", "东方航空", "PKX", "SHA", "20:10", "22:20", 1921, "32N"),
        _flight("MU5170", "东方航空", "PKX", "SHA", "21:00", "23:10", 1720, "32A"),
    ],
    ("PKX", "PVG", RETURN_DATE): [
        _flight("CA1589", "中国国际航空", "PKX", "PVG", "20:30", "22:40", 1820, "773"),
    ],
    ("PEK", "SHA", RETURN_DATE): [
        _flight("CA1507", "中国国际航空", "PEK", "SHA", "19:30", "22:00", 1350, "789"),
    ],
    ("PEK", "PVG", RETURN_DATE): [
        _flight("CA1511", "中国国际航空", "PEK", "PVG", "21:30", "23:55", 1880, "78A"),
    ],
}


class FixtureSource:
    name = "snapshot_fixture"

    def __init__(self) -> None:
        self.calls: list[dict] = []

    def fetch_and_parse(self, origin: str, dest: str, date_str: str) -> list[dict]:
        key = (origin, dest, date_str)
        rows = [deepcopy(row) for row in FIXTURE_FLIGHTS.get(key, [])]
        self.calls.append({"source": self.name, "origin": origin, "dest": dest, "date": date_str, "count": len(rows)})
        return rows


def collect_flights(source: FixtureSource, origins: Iterable[str], dests: Iterable[str], date_str: str) -> list[dict]:
    flights: list[dict] = []
    for origin in origins:
        for dest in dests:
            flights.extend(source.fetch_and_parse(origin, dest, date_str))
    return flights


def collect_standard_data(subscription: dict) -> tuple[list[dict], list[dict], list[dict]]:
    basic = subscription["basic"]
    source = FixtureSource()
    origins = basic["origin_airports_active"]
    dests = basic["destination_airports_active"]
    outbound = collect_flights(source, origins, dests, basic["depart_date"])
    returns = collect_flights(source, dests, origins, basic["return_date"])
    return outbound, returns, source.calls


def _arrival_iso(flight: dict, default_date: str) -> str:
    dt = _flight_arrival_datetime(flight, default_date)
    return dt.isoformat(sep=" ") if dt else ""


def _departure_iso(flight: dict, default_date: str) -> str:
    dt = _flight_departure_datetime(flight, default_date)
    return dt.isoformat(sep=" ") if dt else ""


def airport_transport_snapshot(subscription: dict) -> dict:
    meeting_location = subscription["constraints"]["meeting_location"]
    return {
        airport: estimate_airport_to_meeting(airport, meeting_location, "taxi").get("minutes")
        for airport in subscription["basic"]["destination_airports_active"]
    }


def outbound_window_snapshot(subscription: dict, outbound_flights: list[dict]) -> tuple[list[dict], dict[str, dict]]:
    windows_by_airport: dict[str, dict] = {}
    matches: list[dict] = []
    for flight in outbound_flights:
        airport = str(flight.get("arrival_airport") or "").upper()
        if airport not in windows_by_airport:
            windows_by_airport[airport] = compute_same_day_windows(subscription, None, airport)
        windows = windows_by_airport[airport]
        passed = _same_day_outbound_passes_window(flight, windows, DEPART_DATE)
        if passed:
            matches.append(
                {
                    "flight_no": flight.get("flight_no"),
                    "arrival_airport": airport,
                    "arrival_datetime": _arrival_iso(flight, DEPART_DATE),
                    "price_scope": "单人单程参考价",
                    "price": flight.get("price"),
                    "window_arrive_by": f"{DEPART_DATE} {windows.get('outbound_arrive_by')}",
                }
            )
    matches.sort(key=lambda item: (item["arrival_datetime"], item["price"]))
    return matches, windows_by_airport


def return_window_snapshot(subscription: dict, return_flights: list[dict]) -> tuple[list[dict], dict[str, dict]]:
    windows_by_airport: dict[str, dict] = {}
    matches: list[dict] = []
    for flight in return_flights:
        airport = str(flight.get("departure_airport") or "").upper()
        if airport not in windows_by_airport:
            windows_by_airport[airport] = compute_same_day_windows(subscription, None, airport)
        windows = windows_by_airport[airport]
        passed = _same_day_return_passes_window(flight, windows, RETURN_DATE)
        if passed:
            matches.append(
                {
                    "flight_no": flight.get("flight_no"),
                    "departure_airport": airport,
                    "departure_datetime": _departure_iso(flight, RETURN_DATE),
                    "price_scope": "单人单程参考价",
                    "price": flight.get("price"),
                    "window_depart_after": f"{RETURN_DATE} {windows.get('return_depart_after')}",
                }
            )
    matches.sort(key=lambda item: (item["price"], item["departure_datetime"]))
    return matches, windows_by_airport


def calendar_snapshot(passenger_factor: float) -> dict:
    outbound_calendar = {
        "route": "SHA-PKX",
        "dates": {
            "2026-06-23": {"min_price": 547, "airline": "MU", "updated_at": "2026-06-22T00:00:00+08:00"},
            "2026-06-24": {"min_price": 570, "airline": "MU", "updated_at": "2026-06-22T00:00:00+08:00"},
            "2026-06-25": {"min_price": 760, "airline": "CA", "updated_at": "2026-06-22T00:00:00+08:00"},
            "2026-06-26": {"min_price": 831, "airline": "MU", "updated_at": "2026-06-22T00:00:00+08:00"},
            "2026-06-27": {"min_price": 679, "airline": "MU", "updated_at": "2026-06-22T00:00:00+08:00"},
        },
    }
    return_low = 648
    rows = roundtrip_calendar_rows(outbound_calendar, DEPART_DATE, return_low=return_low, return_date=RETURN_DATE)
    unit_prices = [float(row["min_price"]) for row in rows]
    passenger_prices = [
        price_in_scope(
            row.get("outbound_min_price") or 0,
            PASSENGERS,
            scope="all_passengers_roundtrip",
            route_type=ROUTE_TYPE,
            round_trip=True,
            return_per_person_oneway=row.get("return_min_price") or return_low,
        )
        for row in rows
    ]
    selected_row = next(row for row in rows if row.get("selected"))
    selected_unit = float(selected_row["min_price"])
    selected_total = round(selected_unit * passenger_factor, 2)
    passenger_rows = []
    for row, passenger_price in zip(rows, passenger_prices):
        updated = dict(row)
        updated["unit_roundtrip_price"] = row["min_price"]
        updated["min_price"] = passenger_price
        updated["value"] = passenger_price
        updated["scope"] = "passenger_roundtrip"
        updated["price_scope"] = "3人往返参考价"
        updated["passenger_factor"] = passenger_factor
        passenger_rows.append(updated)
    return {
        "route": "SHA-PKX + PKX-SHA",
        "scope": "3人往返参考价",
        "return_date": RETURN_DATE,
        "return_low_unit": return_low,
        "passenger_factor": passenger_factor,
        "before_unit_prices": unit_prices,
        "after_passenger_prices": passenger_prices,
        "before_unit_first3": unit_prices[:3],
        "after_passenger_first3": passenger_prices[:3],
        "selected_date": DEPART_DATE,
        "selected_unit_price": selected_unit,
        "selected_passenger_price": selected_total,
        "selected_price_scope": "3人往返参考价",
        "rows": passenger_rows,
        "savings": analyze_row_savings(passenger_rows, DEPART_DATE, threshold=100, limit=3),
        "note": "每行=该出发日单人单程最低+固定返程日单人单程最低，再按3名成人换算。",
    }


def budget_snapshot(outbound_matches: list[dict], return_matches: list[dict], passenger_factor: float, budget_limit: float) -> dict:
    compare_scope = _budget_visible_scope(MAX_BUDGET_SCOPE)
    compare_label = _budget_scope_label(MAX_BUDGET_SCOPE)
    budget_pp = budget_to_pp(
        budget_limit,
        PASSENGERS,
        scope=compare_scope,
        route_type=ROUTE_TYPE,
        round_trip=True,
    )
    budget_compare_limit = price_in_scope(
        budget_pp,
        PASSENGERS,
        scope=compare_scope,
        route_type=ROUTE_TYPE,
        round_trip=True,
    )
    budget_all_passengers_limit = price_in_scope(
        budget_pp,
        PASSENGERS,
        scope="all_passengers_roundtrip",
        route_type=ROUTE_TYPE,
        round_trip=True,
    )
    combos = []
    for outbound in outbound_matches:
        for ret in return_matches:
            outbound_price = float(outbound["price"] or 0)
            return_price = float(ret["price"] or 0)
            adult_roundtrip = itinerary_price_pp(outbound_price, return_per_person_oneway=return_price)
            passenger_roundtrip = price_in_scope(
                outbound_price,
                PASSENGERS,
                scope="all_passengers_roundtrip",
                route_type=ROUTE_TYPE,
                round_trip=True,
                return_per_person_oneway=return_price,
            )
            budget_compare_price = price_in_scope(
                outbound_price,
                PASSENGERS,
                scope=compare_scope,
                route_type=ROUTE_TYPE,
                round_trip=True,
                return_per_person_oneway=return_price,
            )
            combos.append(
                {
                    "outbound": outbound["flight_no"],
                    "return": ret["flight_no"],
                    "single_adult_roundtrip": adult_roundtrip,
                    "passenger_roundtrip": passenger_roundtrip,
                    "budget_compare_price": budget_compare_price,
                    "budget_compare_scope": compare_scope,
                    "within_budget": budget_compare_price <= budget_compare_limit,
                }
            )
    combos.sort(key=lambda item: item["budget_compare_price"])
    within = [item for item in combos if item["within_budget"]]
    cheapest = combos[0] if combos else None
    if not combos:
        reason = "会议时间窗口内没有完整往返组合。"
    elif not within:
        reason = (
            f"会议窗口内共有{len(combos)}个往返组合，按{compare_label}¥{budget_compare_limit:,.0f}过滤后=0；"
            f"最低组合{cheapest['outbound']}+{cheapest['return']}为单人往返¥{cheapest['single_adult_roundtrip']:,.0f}"
            f"/3人往返¥{cheapest['passenger_roundtrip']:,.0f}，"
            f"按当前预算口径超出¥{cheapest['budget_compare_price'] - budget_compare_limit:,.0f}。"
        )
    else:
        reason = f"会议窗口内共有{len(within)}个组合满足{compare_label}¥{budget_compare_limit:,.0f}。"
    return {
        "budget_scope": MAX_BUDGET_SCOPE,
        "budget_compare_scope": compare_scope,
        "budget_limit": budget_compare_limit,
        "budget_all_passengers_limit": budget_all_passengers_limit,
        "price_scope": compare_label,
        "window_combo_count": len(combos),
        "after_budget_count": len(within),
        "cheapest_window_combo": cheapest,
        "no_result_reason": reason,
        "combos": combos,
    }



def _price_number(value):
    try:
        if value is None or value == "":
            return None
        return round(float(value), 2)
    except (TypeError, ValueError):
        return None


def _roundtrip_unit_price_from_item(item: dict | None) -> float | None:
    item = item or {}
    direct = _price_number(
        item.get("single_adult_roundtrip")
        or item.get("adult_roundtrip_price")
        or item.get("unit_roundtrip")
        or item.get("roundtrip_unit_price")
    )
    if direct is not None:
        return direct
    outbound_price = _price_number(item.get("outbound_price") or (item.get("outbound") or {}).get("price"))
    return_price = _price_number(item.get("return_price") or (item.get("return") or item.get("return_flight") or {}).get("price"))
    if outbound_price is None or return_price is None:
        return None
    return round(outbound_price + return_price, 2)


def _passenger_roundtrip_price_from_item(item: dict | None, passenger_factor: float) -> float | None:
    item = item or {}
    direct = _price_number(
        item.get("passenger_roundtrip")
        or item.get("roundtrip_price")
        or item.get("total_price")
        or item.get("price")
    )
    if direct is not None:
        return direct
    unit = _roundtrip_unit_price_from_item(item)
    return round(unit * passenger_factor, 2) if unit is not None else None


def _flight_no_from_item(item: dict | None, key: str) -> str | None:
    flight = (item or {}).get(key) or {}
    if not isinstance(flight, dict):
        return None
    return flight.get("flight_no") or flight.get("flight_combo")


def _channel_scope_text(scope: str | None, is_roundtrip: bool) -> str:
    text = str(scope or "").lower()
    if "round" in text or (is_roundtrip and not text):
        return PRICE_SCOPE_UNIT_ROUNDTRIP
    if "oneway" in text or "single" in text:
        return PRICE_SCOPE_UNIT_ONEWAY
    return "参考价(口径待确认)"


def price_points_snapshot(round_trip: dict, calendar: dict, budget: dict, payload: dict | None) -> dict:
    """Collect every price-bearing snapshot point with an explicit scope label."""
    payload = payload or {}
    passenger_factor = float(calendar.get("passenger_factor") or passenger_price_factor(PASSENGERS, ROUTE_TYPE) or 1)
    top_combinations = round_trip.get("top_combinations") or []
    primary = top_combinations[0] if top_combinations else None
    primary_unit = _roundtrip_unit_price_from_item(primary)
    primary_total = _passenger_roundtrip_price_from_item(primary, passenger_factor)
    analysis_unit = _price_number(round_trip.get("budget_price") or round_trip.get("total_min"))
    analysis_total = round(analysis_unit * passenger_factor, 2) if analysis_unit is not None else None

    alternatives = []
    for alt in round_trip.get("same_day_alternatives") or []:
        outbound_price = _price_number(alt.get("outbound_price") or (alt.get("outbound") or {}).get("price"))
        return_price = _price_number(alt.get("return_price") or (alt.get("return") or alt.get("return_flight") or {}).get("price"))
        unit_roundtrip = _roundtrip_unit_price_from_item(alt)
        passenger_roundtrip = _passenger_roundtrip_price_from_item(alt, passenger_factor)
        budget_compare_price = (
            price_in_scope(
                outbound_price,
                PASSENGERS,
                scope=_budget_visible_scope(MAX_BUDGET_SCOPE),
                route_type=ROUTE_TYPE,
                round_trip=True,
                return_per_person_oneway=return_price,
            )
            if outbound_price is not None and return_price is not None else None
        )
        overage = round(budget_compare_price - MAX_BUDGET, 2) if budget_compare_price is not None else None
        alternatives.append(
            {
                "title": alt.get("title") or alt.get("type") or "备选方案",
                "outbound_flight": _flight_no_from_item(alt, "outbound"),
                "return_flight": _flight_no_from_item(alt, "return") or _flight_no_from_item(alt, "return_flight"),
                "outbound_price": outbound_price,
                "outbound_price_scope": PRICE_SCOPE_UNIT_ONEWAY,
                "return_price": return_price,
                "return_price_scope": PRICE_SCOPE_UNIT_ONEWAY,
                "single_adult_roundtrip": unit_roundtrip,
                "single_adult_roundtrip_scope": PRICE_SCOPE_UNIT_ROUNDTRIP,
                "price": passenger_roundtrip,
                "price_scope": PRICE_SCOPE_PASSENGER_ROUNDTRIP,
                "max_acceptable_price": MAX_BUDGET,
                "budget_compare_price": budget_compare_price,
                "budget_compare_price_scope": _budget_visible_scope(MAX_BUDGET_SCOPE),
                "max_acceptable_price_scope": PRICE_SCOPE_TOTAL_BUDGET,
                "over_budget": bool(overage is not None and overage > 0),
                "budget_overage": overage if overage is not None and overage > 0 else 0,
            }
        )

    excluded_items = []
    for item in round_trip.get("excluded_roundtrip_combos") or round_trip.get("excluded_combos") or []:
        passenger_roundtrip = _passenger_roundtrip_price_from_item(item, passenger_factor)
        excluded_items.append(
            {
                "outbound_flight": _flight_no_from_item(item, "outbound"),
                "return_flight": _flight_no_from_item(item, "return") or _flight_no_from_item(item, "return_flight"),
                "price": passenger_roundtrip,
                "price_scope": PRICE_SCOPE_PASSENGER_ROUNDTRIP,
                "reason": item.get("reason") or item.get("exclude_reason") or item.get("exclusion_reason"),
            }
        )

    before_prices = [float(value) for value in calendar.get("before_unit_prices") or []]
    after_prices = [float(value) for value in calendar.get("after_passenger_prices") or []]
    selected_unit = _price_number(calendar.get("selected_unit_price"))
    selected_passenger = _price_number(calendar.get("selected_passenger_price"))
    is_passenger_scoped = False
    if before_prices and after_prices:
        is_passenger_scoped = all(
            abs(after - before * passenger_factor) < 0.01
            for before, after in zip(before_prices, after_prices)
        )

    channel_rows = payload.get("channel_price_rows") or []
    channel_items = []
    for row in channel_rows:
        if not isinstance(row, dict):
            continue
        price = _price_number(row.get("value") or row.get("price"))
        if price is None:
            continue
        channel_items.append(
            {
                "label": row.get("label") or row.get("platform") or "渠道",
                "price": price,
                "price_scope": _channel_scope_text(row.get("scope"), bool(payload.get("is_roundtrip"))),
                "raw_scope": row.get("scope"),
            }
        )

    return {
        "recommended_plan_vs_budget": {
            "has_primary_recommendation": primary is not None,
            "recommended_plan_price": primary_total,
            "recommended_plan_price_scope": PRICE_SCOPE_PASSENGER_ROUNDTRIP if primary_total is not None else "无完全符合方案",
            "recommended_unit_roundtrip_price": primary_unit,
            "recommended_unit_roundtrip_price_scope": PRICE_SCOPE_UNIT_ROUNDTRIP if primary_unit is not None else "无完全符合方案",
            "analysis_reference_price": analysis_total,
            "analysis_reference_price_scope": PRICE_SCOPE_PASSENGER_ROUNDTRIP if analysis_total is not None else "无参考价",
            "analysis_reference_unit_price": analysis_unit,
            "analysis_reference_unit_price_scope": PRICE_SCOPE_UNIT_ROUNDTRIP if analysis_unit is not None else "无参考价",
            "max_acceptable_price": MAX_BUDGET,
            "max_acceptable_price_scope": PRICE_SCOPE_TOTAL_BUDGET,
            "budget_scope": MAX_BUDGET_SCOPE,
            "max_budget_scope": MAX_BUDGET_SCOPE,
            "target_price_scope": TARGET_PRICE_SCOPE,
        },
        "exclusion_diagnosis": {
            "price_scope": PRICE_SCOPE_PASSENGER_ROUNDTRIP,
            "max_acceptable_price": MAX_BUDGET,
            "max_acceptable_price_scope": PRICE_SCOPE_TOTAL_BUDGET,
            "excluded_count": len(excluded_items),
            "items": excluded_items,
            "budget_filter": {
                "window_combo_count": budget.get("window_combo_count"),
                "after_budget_count": budget.get("after_budget_count"),
                "cheapest_window_combo": budget.get("cheapest_window_combo"),
                "no_result_reason": budget.get("no_result_reason"),
                "price_scope": budget.get("price_scope") or PRICE_SCOPE_PASSENGER_ROUNDTRIP,
            },
        },
        "alternative_price_diagnosis": {
            "price_scope": PRICE_SCOPE_PASSENGER_ROUNDTRIP,
            "count": len(alternatives),
            "items": alternatives,
        },
        "calendar_array": {
            "unit_prices": before_prices,
            "unit_price_scope": PRICE_SCOPE_UNIT_ROUNDTRIP,
            "prices": after_prices,
            "price_scope": PRICE_SCOPE_PASSENGER_ROUNDTRIP_REF,
            "passenger_factor": passenger_factor,
            "is_passenger_scoped": is_passenger_scoped,
            "before_after_first3": [
                {"unit_price": unit, "passenger_price": total}
                for unit, total in zip(before_prices[:3], after_prices[:3])
            ],
        },
        "selected_date_price": {
            "date": calendar.get("selected_date"),
            "unit_price": selected_unit,
            "unit_price_scope": PRICE_SCOPE_UNIT_ROUNDTRIP,
            "price": selected_passenger,
            "price_scope": PRICE_SCOPE_PASSENGER_ROUNDTRIP_REF,
            "is_passenger_scoped": bool(
                selected_unit is not None
                and selected_passenger is not None
                and abs(selected_passenger - selected_unit * passenger_factor) < 0.01
            ),
        },
        "channel_comparison": {
            "price_scope": "无渠道对比价" if not channel_items else "渠道价按各行price_scope声明",
            "shown_in_notification": len(channel_items) >= 2,
            "count": len(channel_items),
            "items": channel_items,
            "note": "少于2个渠道/来源时不展示渠道对比" if len(channel_items) < 2 else "渠道价已逐项标注口径",
        },
    }


def _analysis_inputs(subscription: dict, outbound: list[dict], returns: list[dict]) -> tuple[dict, dict]:
    constraints = {**subscription["hard_constraints"], **subscription["preferences"]}
    outbound_analysis = {
        "all_flights": outbound,
        "raw_valid_outbound": outbound,
        "hard_constraints": constraints,
        "user_preferences": constraints,
        "depart_date": DEPART_DATE,
        "route_type": ROUTE_TYPE,
        "days_to_dept": 4,
    }
    return_analysis = {
        "all_flights": returns,
        "raw_valid_outbound": returns,
        "hard_constraints": constraints,
        "user_preferences": constraints,
        "depart_date": RETURN_DATE,
        "route_type": ROUTE_TYPE,
        "days_to_dept": 4,
    }
    return outbound_analysis, return_analysis


def _build_payload(subscription: dict, outbound_analysis: dict, return_analysis: dict, round_trip: dict, calendar: dict) -> dict:
    original_track = notifier.track_plan_status
    original_last_price = notifier.get_last_push_price
    original_last_snapshot = notifier.get_last_push_snapshot
    try:
        notifier.track_plan_status = lambda *args, **kwargs: None
        notifier.get_last_push_price = lambda *args, **kwargs: None
        notifier.get_last_push_snapshot = lambda *args, **kwargs: None
        analysis_result = {
            **outbound_analysis,
            "round_trip_analysis": round_trip,
            "return_analysis": return_analysis,
            "price_calendar": calendar,
            "target_price": TARGET_PRICE,
            "max_budget": MAX_BUDGET,
            "budget_scope": MAX_BUDGET_SCOPE,
            "max_budget_scope": MAX_BUDGET_SCOPE,
            "target_price_scope": TARGET_PRICE_SCOPE,
        }
        route_info = {
            "origin": "上海",
            "destination": "北京",
            "origin_airports_active": ["PVG", "SHA"],
            "destination_airports_active": ["PEK", "PKX"],
            "depart_date": DEPART_DATE,
            "return_date": RETURN_DATE,
            "round_trip": True,
            "route_type": ROUTE_TYPE,
            "target_price": TARGET_PRICE,
            "max_budget": MAX_BUDGET,
            "budget_scope": MAX_BUDGET_SCOPE,
            "max_budget_scope": MAX_BUDGET_SCOPE,
            "target_price_scope": TARGET_PRICE_SCOPE,
            "price_calendar": calendar,
        }
        payload = notifier.build_notification_payload(
            analysis_result,
            outbound_analysis=outbound_analysis,
            return_analysis=return_analysis,
            route_info=route_info,
            subscription=subscription,
            price_history=[],
            source_stats={"route_type": ROUTE_TYPE, "sources": ["snapshot_fixture"]},
        )
        subject, email_html = notifier.render_email(payload)
        pushplus = notifier.render_pushplus(payload)
        return {
            "payload": payload,
            "rendered": {
                "email_subject": subject,
                "email_html_chars": len(email_html),
                "pushplus_chars": len(pushplus),
            },
        }
    finally:
        notifier.track_plan_status = original_track
        notifier.get_last_push_price = original_last_price
        notifier.get_last_push_snapshot = original_last_snapshot


def build_snapshot() -> dict:
    subscription = standard_subscription()
    outbound, returns, calls = collect_standard_data(subscription)
    outbound_analysis, return_analysis = _analysis_inputs(subscription, outbound, returns)
    round_trip = analyze_round_trip(outbound_analysis, return_analysis, target_price=TARGET_PRICE, max_budget=MAX_BUDGET)

    passenger_factor = passenger_price_factor(PASSENGERS, ROUTE_TYPE)
    calendar = calendar_snapshot(passenger_factor)
    outbound_matches, outbound_windows = outbound_window_snapshot(subscription, outbound)
    return_matches, return_windows = return_window_snapshot(subscription, returns)
    budget = budget_snapshot(outbound_matches, return_matches, passenger_factor, MAX_BUDGET)
    return_recommendation = return_matches[0] if return_matches else {}
    no_result_reason = round_trip.get("same_day_no_feasible_note") if not outbound_matches else budget["no_result_reason"]
    if not no_result_reason:
        no_result_reason = budget["no_result_reason"]
    budget["no_result_reason"] = no_result_reason
    payload_result = _build_payload(subscription, outbound_analysis, return_analysis, round_trip, calendar)
    price_points = price_points_snapshot(round_trip, calendar, budget, payload_result["payload"])

    snapshot = {
        "snapshot_version": 1,
        "scenario": "standard_same_day_business_shanghai_beijing",
        "subscription": subscription,
        "collection": {
            "source": "snapshot_fixture",
            "calls": calls,
            "outbound_count": len(outbound),
            "return_count": len(returns),
        },
        "airport_transport_to_meeting": airport_transport_snapshot(subscription),
        "price_calendar": calendar,
        "price_points": price_points,
        "same_day": {
            "windows_by_arrival_airport": outbound_windows,
            "windows_by_return_airport": return_windows,
            "analysis_filter_counts": round_trip.get("filter_counts") or {},
            "return_window_debug": round_trip.get("same_day_return_window_debug") or [],
            "outbound_window_match_count": len(outbound_matches),
            "outbound_window_matches": outbound_matches,
            "return_window_match_count": len(return_matches),
            "return_window_matches": return_matches,
            "return_recommendation": {
                **return_recommendation,
                "selection_rule": "满足返程时间窗口后按单人单程价升序",
            } if return_recommendation else {},
            "budget_filter": budget,
            "no_result_reason": no_result_reason,
            "same_day_alternatives": round_trip.get("same_day_alternatives") or [],
        },
        "analysis": {
            "top_combinations": round_trip.get("top_combinations") or [],
            "same_day_time_conflict": round_trip.get("same_day_time_conflict"),
            "same_day_no_feasible_note": round_trip.get("same_day_no_feasible_note"),
            "budget_limits": round_trip.get("budget_limits"),
        },
        "notification": payload_result["rendered"],
    }
    return snapshot


def print_key_fields(snapshot: dict) -> None:
    calendar = snapshot["price_calendar"]
    same_day = snapshot["same_day"]
    price_points = snapshot.get("price_points") or {}
    print("[snapshot] 单人日历前3=", calendar["before_unit_first3"])
    print("[snapshot] 全员日历前3=", calendar["after_passenger_first3"])
    print(
        "[snapshot] 你选日期价=",
        calendar["selected_passenger_price"],
        "口径=",
        calendar["selected_price_scope"],
    )
    print("[snapshot] 推荐价/预算=", price_points.get("recommended_plan_vs_budget"))
    print("[snapshot] 排除诊断=", price_points.get("exclusion_diagnosis"))
    print("[snapshot] 备选价格诊断=", price_points.get("alternative_price_diagnosis"))
    print("[snapshot] 日历数组=", price_points.get("calendar_array"))
    print("[snapshot] 你选日期价详情=", price_points.get("selected_date_price"))
    print("[snapshot] 渠道对比=", price_points.get("channel_comparison"))
    print("[snapshot] 去程窗口符合数=", same_day["outbound_window_match_count"])
    print("[snapshot] 去程窗口符合航班=", same_day["outbound_window_matches"])
    print("[snapshot] 返程推荐=", same_day["return_recommendation"])
    print("[snapshot] \u8fd4\u7a0b\u9010\u73ed\u6bd4\u8f83=", same_day.get("return_window_debug"))
    print("[snapshot] 无方案理由=", same_day["no_result_reason"])
    print("[snapshot] 各机场到会场车程=", snapshot["airport_transport_to_meeting"])

def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Write deterministic full-chain flight-monitor snapshot.")
    parser.add_argument("--output", default=str(PROJECT_ROOT / "before.json"), help="Snapshot JSON path. Defaults to project before.json.")
    parser.add_argument("--quiet", action="store_true", help="Do not print key fields.")
    args = parser.parse_args(argv)

    snapshot = build_snapshot()
    output = Path(args.output)
    if not output.is_absolute():
        output = PROJECT_ROOT / output
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(snapshot, ensure_ascii=False, indent=2, sort_keys=True, default=str), encoding="utf-8")
    if not args.quiet:
        print_key_fields(snapshot)
    print(f"Wrote {output}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
