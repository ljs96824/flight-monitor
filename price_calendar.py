"""Rolling low-price calendar for route/date comparisons.

The calendar is intentionally local and quota-friendly: it keeps per-route JSON
snapshots and refreshes only a small set of nearby/sample dates when entries are
stale.
"""

from __future__ import annotations

import json
import time
from datetime import date, datetime, timedelta
from pathlib import Path

from filename_utils import sanitize_filename

DEFAULT_DATA_DIR = Path(__file__).parent / "data" / "price_calendar"
WEEKDAY_NAMES = ["周一", "周二", "周三", "周四", "周五", "周六", "周日"]


def _safe_route(route: str) -> str:
    return sanitize_filename(route)


def calendar_path(route: str, data_dir: Path | None = None) -> Path:
    base = data_dir or DEFAULT_DATA_DIR
    return Path(base) / f"{_safe_route(route)}.json"


def now_iso() -> str:
    return datetime.now().isoformat(timespec="seconds")


def parse_date(value: str | date | datetime) -> date:
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    return date.fromisoformat(str(value)[:10])


def is_stale(updated_at: str | None, hours: int = 6) -> bool:
    if not updated_at:
        return True
    try:
        dt = datetime.fromisoformat(str(updated_at).replace("Z", "+00:00"))
        dt = dt.replace(tzinfo=None)
    except (TypeError, ValueError):
        return True
    return datetime.now() - dt >= timedelta(hours=hours)


def load_calendar(route: str, data_dir: Path | None = None) -> dict:
    path = calendar_path(route, data_dir)
    if not path.exists():
        return {"route": route, "dates": {}}
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {"route": route, "dates": {}}
    if not isinstance(payload, dict):
        return {"route": route, "dates": {}}
    payload.setdefault("route", route)
    payload.setdefault("dates", {})
    if not isinstance(payload["dates"], dict):
        payload["dates"] = {}
    return payload


def save_calendar(route: str, calendar: dict, data_dir: Path | None = None) -> None:
    path = calendar_path(route, data_dir)
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = dict(calendar or {})
    payload["route"] = payload.get("route") or route
    payload["dates"] = payload.get("dates") or {}
    path.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True),
        encoding="utf-8",
    )


def _query_dates(target_date: str) -> list[date]:
    target = parse_date(target_date)
    offsets = list(range(-3, 4)) + [-14, -7, 7, 14]
    today = date.today()
    seen = set()
    dates = []
    for offset in offsets:
        current = target + timedelta(days=offset)
        if current < today:
            continue
        if current in seen:
            continue
        seen.add(current)
        dates.append(current)
    return dates


def _source_fetch(source, origin: str, dest: str, date_str: str, cabin_class: str):
    if hasattr(source, "fetch_and_parse"):
        return source.fetch_and_parse(origin, dest, date_str)
    result = source.fetch(origin, dest, date_str, cabin_class)
    if isinstance(result, dict):
        return result.get("flights") or []
    return result or []


def _valid_price(value) -> bool:
    try:
        return float(value) > 0
    except (TypeError, ValueError):
        return False


def update_calendar(
    route: str,
    origin: str,
    dest: str,
    target_date: str,
    source,
    *,
    cabin_class: str = "economy",
    data_dir: Path | None = None,
    cache_hours: int = 6,
    sleep_seconds: float = 0.5,
) -> dict:
    """Refresh a small date set around target_date and persist the calendar."""
    calendar = load_calendar(route, data_dir)
    dates = calendar.setdefault("dates", {})

    for query_date in _query_dates(target_date):
        date_str = query_date.isoformat()
        cached = dates.get(date_str)
        if isinstance(cached, dict) and not is_stale(cached.get("updated_at"), cache_hours):
            continue

        flights = _source_fetch(source, origin, dest, date_str, cabin_class)
        priced = [flight for flight in flights if isinstance(flight, dict) and _valid_price(flight.get("price"))]
        if priced:
            cheapest = min(priced, key=lambda flight: float(flight.get("price") or 10**9))
            dates[date_str] = {
                "min_price": float(cheapest.get("price")),
                "airline": cheapest.get("airline") or cheapest.get("airline_code") or cheapest.get("airline_name"),
                "flight_no": cheapest.get("flight_no") or cheapest.get("flight_combo"),
                "updated_at": now_iso(),
            }
        if sleep_seconds:
            time.sleep(sleep_seconds)

    save_calendar(route, calendar, data_dir)
    return calendar


def analyze_date_savings(
    calendar: dict,
    target_date: str,
    current_price,
    *,
    threshold: float = 100,
    limit: int = 3,
) -> list[dict]:
    """Find nearby calendar dates that are materially cheaper than target."""
    try:
        current = float(current_price)
    except (TypeError, ValueError):
        return []

    target = parse_date(target_date)
    savings = []
    for date_str, info in (calendar.get("dates") or {}).items():
        if not isinstance(info, dict) or not _valid_price(info.get("min_price")):
            continue
        d = parse_date(date_str)
        if d < date.today():
            continue
        diff_days = (d - target).days
        if diff_days == 0:
            continue
        price = float(info["min_price"])
        price_diff = round(current - price)
        if price_diff < threshold:
            continue
        direction = "提前" if diff_days < 0 else "推迟"
        weekday = WEEKDAY_NAMES[d.weekday()]
        savings.append(
            {
                "date": date_str,
                "weekday": weekday,
                "price": price,
                "save": price_diff,
                "offset": diff_days,
                "tip": f"{direction}{abs(diff_days)}天({date_str} {weekday})出发，省¥{price_diff}",
            }
        )

    savings.sort(key=lambda item: item["save"], reverse=True)
    return savings[:limit]


def analyze_weekday_pattern(calendar: dict, *, min_samples: int = 7) -> dict | None:
    """Summarize which weekday tends to be cheaper, if enough samples exist."""
    by_weekday = {i: [] for i in range(7)}
    for date_str, info in (calendar.get("dates") or {}).items():
        if not isinstance(info, dict) or not _valid_price(info.get("min_price")):
            continue
        weekday = parse_date(date_str).weekday()
        by_weekday[weekday].append(float(info["min_price"]))

    sample_count = sum(len(values) for values in by_weekday.values())
    if sample_count < min_samples:
        return {"data_insufficient": True}

    averages = {
        WEEKDAY_NAMES[index]: round(sum(values) / len(values))
        for index, values in by_weekday.items()
        if values
    }
    minimums = {
        WEEKDAY_NAMES[index]: min(values)
        for index, values in by_weekday.items()
        if values
    }
    if not averages:
        return {"data_insufficient": True}
    cheapest = min(averages, key=averages.get)
    return {
        "cheapest_weekday": cheapest,
        "by_weekday": averages,
        "min_by_weekday": minimums,
        "sample_count": sample_count,
        "tip": f"本航线{cheapest}通常更便宜",
    }


def analyze_date_savings(
    calendar: dict,
    target_date: str,
    current_price,
    *,
    threshold: float = 100,
    limit: int = 3,
) -> list[dict]:
    """Find cheaper future dates using the same single-leg calendar price scope."""
    try:
        current = float(current_price)
    except (TypeError, ValueError):
        return []

    target = parse_date(target_date)
    savings = []
    for date_str, info in (calendar.get("dates") or {}).items():
        if not isinstance(info, dict) or not _valid_price(info.get("min_price")):
            continue
        d = parse_date(date_str)
        if d < date.today():
            continue
        diff_days = (d - target).days
        if diff_days == 0:
            continue
        price = float(info["min_price"])
        price_diff = round(current - price)
        if price_diff < threshold:
            continue
        direction = "提前" if diff_days < 0 else "推迟"
        weekday = WEEKDAY_NAMES[d.weekday()]
        savings.append(
            {
                "date": date_str,
                "weekday": weekday,
                "price": price,
                "save": price_diff,
                "offset": diff_days,
                "tip": f"{direction}{abs(diff_days)}天({date_str} {weekday})出发，省¥{price_diff}/单程",
            }
        )

    savings.sort(key=lambda item: item["save"], reverse=True)
    return savings[:limit]


def analyze_weekday_pattern(calendar: dict, *, min_samples: int = 7) -> dict | None:
    """Report the actual lowest date, and only claim a usual weekday when clear."""
    by_weekday = {i: [] for i in range(7)}
    dated_prices = []
    for date_str, info in (calendar.get("dates") or {}).items():
        if not isinstance(info, dict) or not _valid_price(info.get("min_price")):
            continue
        d = parse_date(date_str)
        if d < date.today():
            continue
        price = float(info["min_price"])
        by_weekday[d.weekday()].append(price)
        dated_prices.append((d, price))

    sample_count = sum(len(values) for values in by_weekday.values())
    if sample_count < min_samples or not dated_prices:
        return {"data_insufficient": True}

    averages = {
        WEEKDAY_NAMES[index]: round(sum(values) / len(values))
        for index, values in by_weekday.items()
        if values
    }
    minimums = {
        WEEKDAY_NAMES[index]: min(values)
        for index, values in by_weekday.items()
        if values
    }
    if not averages:
        return {"data_insufficient": True}

    min_day, min_price = min(dated_prices, key=lambda item: item[1])
    min_weekday = WEEKDAY_NAMES[min_day.weekday()]
    sorted_averages = sorted(averages.items(), key=lambda item: item[1])
    avg_cheapest = sorted_averages[0][0]
    usual_tip = ""
    if len(sorted_averages) >= 2:
        avg_cheapest_index = WEEKDAY_NAMES.index(avg_cheapest)
        avg_cheapest_count = len(by_weekday[avg_cheapest_index])
        avg_gap = sorted_averages[1][1] - sorted_averages[0][1]
        if avg_cheapest_count >= 3 and avg_gap >= max(50, sorted_averages[1][1] * 0.05):
            usual_tip = f"；{avg_cheapest}平均更低"
    print(
        f"[周几统计] 最低价日={min_day.isoformat()}({min_weekday}) CNY{min_price:,.0f}, "
        f"各星期平均={averages}"
    )
    return {
        "cheapest_weekday": avg_cheapest,
        "usual_cheapest_weekday": avg_cheapest if usual_tip else "",
        "by_weekday": averages,
        "min_by_weekday": minimums,
        "min_date": min_day.isoformat(),
        "min_weekday": min_weekday,
        "min_price": min_price,
        "sample_count": sample_count,
        "tip": f"近期最低出现在{min_weekday}({min_day.isoformat()},单程¥{min_price:,.0f}){usual_tip}",
    }


def calendar_rows(calendar: dict, target_date: str) -> list[dict]:
    target = parse_date(target_date)
    rows = []
    valid_prices = [
        float(info["min_price"])
        for info in (calendar.get("dates") or {}).values()
        if isinstance(info, dict) and _valid_price(info.get("min_price"))
    ]
    lowest = min(valid_prices) if valid_prices else None
    for date_str, info in sorted((calendar.get("dates") or {}).items()):
        if not isinstance(info, dict) or not _valid_price(info.get("min_price")):
            continue
        d = parse_date(date_str)
        if d < date.today():
            continue
        price = float(info["min_price"])
        rows.append(
            {
                "date": date_str,
                "weekday": WEEKDAY_NAMES[d.weekday()],
                "min_price": price,
                "airline": info.get("airline"),
                "selected": d == target,
                "lowest": lowest is not None and price == lowest,
                "scope": "oneway",
                "label": f"{date_str[5:]} {WEEKDAY_NAMES[d.weekday()]}",
                "value": price,
            }
        )
    return rows


def calendar_price_on_date(calendar: dict, target_date: str) -> float | None:
    """Return the one-way minimum price for an exact calendar date."""
    try:
        date_key = parse_date(target_date).isoformat()
    except (TypeError, ValueError):
        return None
    info = (calendar or {}).get("dates", {}).get(date_key)
    if not isinstance(info, dict) or not _valid_price(info.get("min_price")):
        return None
    return float(info["min_price"])


def roundtrip_calendar_rows(
    outbound_calendar: dict,
    target_date: str,
    *,
    return_low,
    return_date: str,
) -> list[dict]:
    """Build fixed-return-date roundtrip reference rows.

    Each row equals the outbound date's one-way low plus the fixed return date's
    one-way low. This keeps quota usage bounded while giving round-trip monitors
    a comparable price scope.
    """
    if not _valid_price(return_low):
        return []
    return_price = float(return_low)
    rows = []
    for row in calendar_rows(outbound_calendar or {}, target_date):
        outbound_price = row.get("min_price")
        if not _valid_price(outbound_price):
            continue
        outbound_price = float(outbound_price)
        combined = outbound_price + return_price
        updated = dict(row)
        updated.update(
            {
                "outbound_min_price": outbound_price,
                "return_min_price": return_price,
                "return_date": str(return_date or ""),
                "min_price": combined,
                "value": combined,
                "scope": "roundtrip",
                "breakdown": f"去¥{outbound_price:,.0f}+返¥{return_price:,.0f}",
            }
        )
        rows.append(updated)

    if rows:
        lowest_price = min(row["min_price"] for row in rows)
        for row in rows:
            row["lowest"] = row["min_price"] == lowest_price
    return rows


def analyze_row_savings(
    rows: list[dict],
    target_date: str,
    *,
    threshold: float = 100,
    limit: int = 3,
) -> list[dict]:
    """Find cheaper dates from already-scoped calendar rows."""
    selected = next((row for row in rows if isinstance(row, dict) and row.get("selected")), None)
    if selected is None:
        try:
            target = parse_date(target_date)
        except (TypeError, ValueError):
            target = None
        if target:
            selected = next(
                (
                    row
                    for row in rows
                    if isinstance(row, dict)
                    and row.get("date")
                    and parse_date(str(row["date"])) == target
                ),
                None,
            )
    if not isinstance(selected, dict) or not _valid_price(selected.get("min_price")):
        return []

    current = float(selected["min_price"])
    target = parse_date(selected.get("date") or target_date)
    savings = []
    for row in rows:
        if not isinstance(row, dict) or row is selected or not _valid_price(row.get("min_price")):
            continue
        row_date = row.get("date")
        if not row_date:
            continue
        d = parse_date(str(row_date))
        if d < date.today():
            continue
        price = float(row["min_price"])
        price_diff = round(current - price)
        if price_diff < threshold:
            continue
        diff_days = (d - target).days
        direction = "鎻愬墠" if diff_days < 0 else "鎺ㄨ繜"
        weekday = row.get("weekday") or WEEKDAY_NAMES[d.weekday()]
        unit = "往返" if row.get("scope") == "roundtrip" else "单程"
        savings.append(
            {
                "date": str(row_date),
                "weekday": weekday,
                "price": price,
                "save": price_diff,
                "offset": diff_days,
                "tip": f"{direction}{abs(diff_days)}天({str(row_date)} {weekday})出发，省¥{price_diff}/{unit}",
            }
        )
    savings.sort(key=lambda item: item["save"], reverse=True)
    return savings[:limit]
