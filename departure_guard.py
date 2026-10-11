"""计划起飞时刻核验：按出发机场当地时区比较计划起飞时刻与参考时刻，不推断实际起飞。"""

from __future__ import annotations

from datetime import datetime, tzinfo
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from airports import get_airport_iana_timezone
from flight_time import parse_flight_datetime

STATUS_KEY = "planned_departure_status"
STATUS_PASSED = "passed"
STATUS_UPCOMING = "upcoming"
STATUS_UNKNOWN = "unknown"
PASSED_REASON = "计划起飞时刻已过，已排除"
PASSED_REASON_CODE = "planned_departure_passed"
UNKNOWN_NOTE = "计划起飞时刻未知，请核实"


def resolve_timezone(name: str | None) -> tzinfo | None:
    """IANA 名称优先用 zoneinfo；本机缺少时区库时退回 python-dateutil 自带数据；仍失败返回 None。"""
    if not name:
        return None
    try:
        return ZoneInfo(name)
    except (ZoneInfoNotFoundError, ValueError):
        pass
    try:
        from dateutil.zoneinfo import get_zonefile_instance

        return get_zonefile_instance().get(name)
    except Exception:
        return None


def _first_departure(flight: dict) -> tuple[object, str]:
    segments = flight.get("segments") or []
    first = segments[0] if segments and isinstance(segments[0], dict) else {}
    time_text = (
        first.get("dep_time")
        or first.get("departure_time")
        or flight.get("departure_time")
        or flight.get("dep_time")
    )
    airport = (
        first.get("dep_airport")
        or first.get("departure_airport")
        or flight.get("departure_airport")
        or flight.get("origin")
    )
    return time_text, str(airport or "").strip().upper()


def planned_departure_at(flight: dict) -> datetime | None:
    time_text, airport = _first_departure(flight or {})
    parsed = parse_flight_datetime(time_text)
    if parsed is None:
        return None
    if parsed.utcoffset() is not None:
        return parsed
    zone = resolve_timezone(get_airport_iana_timezone(airport)) if airport else None
    if zone is None:
        return None
    return parsed.replace(tzinfo=zone)


def planned_departure_status(flight: dict, now: datetime) -> str:
    planned = planned_departure_at(flight)
    if planned is None:
        return STATUS_UNKNOWN
    return STATUS_PASSED if planned <= now else STATUS_UPCOMING


def split_by_planned_departure(flights: list[dict], now: datetime) -> tuple[list[dict], list[dict]]:
    """计划时刻已过的移入排除列表并标注；时刻未知的保留并标注；其余原样保留。"""
    if now.utcoffset() is None:
        raise ValueError("reference now must be timezone-aware")
    kept: list[dict] = []
    excluded: list[dict] = []
    for flight in flights or []:
        status = planned_departure_status(flight, now)
        if status == STATUS_PASSED:
            excluded.append({**flight, STATUS_KEY: STATUS_PASSED, "exclude_reason": PASSED_REASON})
        elif status == STATUS_UNKNOWN:
            kept.append({**flight, STATUS_KEY: STATUS_UNKNOWN})
        else:
            kept.append(flight)
    return kept, excluded
