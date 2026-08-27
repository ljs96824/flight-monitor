"""Rolling low-price calendar for route/date comparisons.

The calendar is intentionally local and quota-friendly: it keeps per-route JSON
snapshots and refreshes only a small set of nearby/sample dates when entries are
stale.
"""

from __future__ import annotations

import statistics
import time
from datetime import date, datetime, timedelta
from pathlib import Path

from atomic_json_store import JsonStoreReadError, read_json, update_json
from filename_utils import sanitize_filename
from log_utils import safe_log
from method_registry import method_version
from project_time import SHANGHAI_TZ
from request_cache import cached_fetch
from subscription_preflight import shanghai_today

DEFAULT_DATA_DIR = Path(__file__).parent / "data" / "price_calendar"
WEEKDAY_NAMES = ["周一", "周二", "周三", "周四", "周五", "周六", "周日"]
CALENDAR_STATUSES = frozenset({"success", "empty", "failed", "stale"})
DEFAULT_STALE_HOURS = 6


class PriceCalendarSourceError(RuntimeError):
    def __init__(self, source_error_type: str, detail: str):
        super().__init__(detail)
        self.source_error_type = source_error_type


def _safe_route(route: str) -> str:
    return sanitize_filename(route)


def calendar_path(route: str, data_dir: Path | None = None) -> Path:
    base = data_dir or DEFAULT_DATA_DIR
    return Path(base) / f"{_safe_route(route)}.json"


def _shanghai_now() -> datetime:
    return datetime.now(SHANGHAI_TZ)


def now_iso(now: datetime | None = None) -> str:
    current = now or _shanghai_now()
    if current.tzinfo is None:
        current = current.replace(tzinfo=SHANGHAI_TZ)
    return current.astimezone(SHANGHAI_TZ).isoformat(timespec="seconds")


def parse_date(value: str | date | datetime) -> date:
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    return date.fromisoformat(str(value)[:10])


def _parse_timestamp(value) -> datetime | None:
    if not value:
        return None
    text = str(value).strip()
    if text.endswith("Z"):
        text = text[:-1] + "+00:00"
    try:
        parsed = datetime.fromisoformat(text)
    except (TypeError, ValueError):
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=SHANGHAI_TZ)
    return parsed.astimezone(SHANGHAI_TZ)


def is_stale(
    updated_at: str | None,
    hours: int = DEFAULT_STALE_HOURS,
    *,
    now: datetime | None = None,
) -> bool:
    updated = _parse_timestamp(updated_at)
    if updated is None:
        return True
    current = now or _shanghai_now()
    if current.tzinfo is None:
        current = current.replace(tzinfo=SHANGHAI_TZ)
    return current.astimezone(SHANGHAI_TZ) - updated >= timedelta(hours=hours)


def _normalized_calendar_record(
    info: dict,
    *,
    now: datetime | None = None,
    legacy_stale_hours: int = DEFAULT_STALE_HOURS,
) -> dict:
    if not isinstance(info, dict):
        raise JsonStoreReadError("低价日历日期记录不是对象")
    record = dict(info)
    legacy_updated_at = record.get("updated_at")
    last_success_at = record.get("last_success_at") or legacy_updated_at
    last_attempt_at = record.get("last_attempt_at") or legacy_updated_at
    stale_after = record.get("stale_after")
    if stale_after is None and last_success_at:
        parsed_success = _parse_timestamp(last_success_at)
        if parsed_success is not None:
            stale_after = (
                parsed_success + timedelta(hours=max(0, int(legacy_stale_hours)))
            ).isoformat(timespec="seconds")

    status = str(record.get("status") or "").strip().lower()
    if status not in CALENDAR_STATUSES:
        status = "success" if _valid_price(record.get("min_price")) else "empty"
    deadline = _parse_timestamp(stale_after)
    current = now or _shanghai_now()
    if current.tzinfo is None:
        current = current.replace(tzinfo=SHANGHAI_TZ)
    if status == "success" and deadline is not None:
        if current.astimezone(SHANGHAI_TZ) >= deadline:
            status = "stale"

    record.update(
        {
            "status": status,
            "last_attempt_at": last_attempt_at,
            "last_success_at": last_success_at,
            "error_type": record.get("error_type"),
            "stale_after": stale_after,
            "round_id": record.get("round_id"),
        }
    )
    return record


def calendar_record_is_eligible(
    info: dict,
    *,
    now: datetime | None = None,
    legacy_stale_hours: int = DEFAULT_STALE_HOURS,
) -> bool:
    """Only a fresh successful record may influence recommendations."""

    try:
        record = _normalized_calendar_record(
            info,
            now=now,
            legacy_stale_hours=legacy_stale_hours,
        )
    except JsonStoreReadError:
        return False
    return record.get("status") == "success" and _valid_price(
        record.get("min_price")
    )


def _status_text(record: dict) -> str:
    status = str(record.get("status") or "")
    if status == "stale":
        return "历史参考(已过期)"
    if status == "empty":
        return "本次无报价"
    if status == "failed":
        error_type = str(record.get("error_type") or "原因未知")
        return f"采集失败({error_type})"
    return ""


def load_calendar(
    route: str,
    data_dir: Path | None = None,
    *,
    legacy_stale_hours: int = DEFAULT_STALE_HOURS,
) -> dict:
    path = calendar_path(route, data_dir)
    if not path.exists():
        return {"route": route, "dates": {}}
    payload = read_json(path)
    if not isinstance(payload, dict):
        raise JsonStoreReadError(f"低价日历根节点不是对象: {path}")
    raw_dates = payload.get("dates", {})
    if not isinstance(raw_dates, dict):
        raise JsonStoreReadError(f"低价日历dates不是对象: {path}")
    normalized_dates = {}
    for date_key, info in raw_dates.items():
        normalized_dates[str(date_key)] = _normalized_calendar_record(
            info,
            legacy_stale_hours=legacy_stale_hours,
        )
    payload = dict(payload)
    payload.setdefault("route", route)
    payload["dates"] = normalized_dates
    return payload


def save_calendar(route: str, calendar: dict, data_dir: Path | None = None) -> None:
    path = calendar_path(route, data_dir)
    payload = dict(calendar or {})
    payload["route"] = payload.get("route") or route
    payload["dates"] = payload.get("dates") or {}
    if not isinstance(payload["dates"], dict):
        raise ValueError("低价日历dates必须是对象")
    update_json(path, lambda _current: payload)


def _query_dates(target_date: str) -> list[date]:
    target = parse_date(target_date)
    offsets = list(range(-3, 4)) + [-14, -7, 7, 14]
    today = shanghai_today()
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


def _source_fetch(source, origin: str, dest: str, date_str: str, cabin_class: str, passengers=None, ttl_seconds: int = 6 * 60 * 60):
    result = cached_fetch(source, origin, dest, date_str, passengers, cabin_class, ttl_seconds=ttl_seconds)
    if isinstance(result, dict):
        source_status = str(result.get("source_status") or "").strip().lower()
        if source_status.startswith("failed") or source_status in {
            "invalid_date",
            "not_configured",
        }:
            raise PriceCalendarSourceError(
                str(result.get("error_type") or source_status or "SourceError"),
                str(result.get("error") or result.get("reason") or source_status),
            )
        return result.get("flights") or []
    return result or []


def _valid_price(value) -> bool:
    try:
        return float(value) > 0
    except (TypeError, ValueError):
        return False


def _source_names(value) -> list[str]:
    if isinstance(value, (list, tuple, set)):
        values = value
    else:
        values = str(value or "").replace("|", "+").split("+")
    return sorted(
        {
            str(source).strip().lower()
            for source in values
            if str(source).strip()
        }
    )


def update_calendar(
    route: str,
    origin: str,
    dest: str,
    target_date: str,
    source,
    *,
    cabin_class: str = "economy",
    passengers=None,
    data_dir: Path | None = None,
    cache_hours: int = 6,
    sleep_seconds: float = 0.5,
    round_id: str | None = None,
) -> dict:
    """Refresh a small date set around target_date and persist the calendar."""
    calendar = load_calendar(
        route,
        data_dir,
        legacy_stale_hours=cache_hours,
    )
    dates = calendar.setdefault("dates", {})

    for query_date in _query_dates(target_date):
        date_str = query_date.isoformat()
        cached = dates.get(date_str)
        current = _shanghai_now()
        if isinstance(cached, dict) and calendar_record_is_eligible(
            cached,
            now=current,
            legacy_stale_hours=cache_hours,
        ):
            continue

        attempt_at = now_iso(current)
        previous = _normalized_calendar_record(cached or {}, now=current)
        try:
            flights = _source_fetch(
                source,
                origin,
                dest,
                date_str,
                cabin_class,
                passengers,
                cache_hours * 60 * 60,
            )
        except Exception as exc:
            error_type = str(
                getattr(exc, "source_error_type", None) or type(exc).__name__
            )
            failed = dict(previous)
            failed.update(
                {
                    "status": "failed",
                    "last_attempt_at": attempt_at,
                    "error_type": error_type,
                    "round_id": round_id,
                    "updated_at": attempt_at,
                }
            )
            dates[date_str] = failed
            save_calendar(route, calendar, data_dir)
            safe_log(
                f"[低价日历] 日期={date_str} 状态=failed "
                f"原因={error_type}:{exc}"
            )
            if sleep_seconds:
                time.sleep(sleep_seconds)
            continue

        priced = [flight for flight in flights if isinstance(flight, dict) and _valid_price(flight.get("price"))]
        if priced:
            cheapest = min(priced, key=lambda flight: float(flight.get("price") or 10**9))
            source_names = _source_names(
                cheapest.get("price_source")
                or cheapest.get("data_source")
                or cheapest.get("source")
                or getattr(source, "name", None)
                or type(source).__name__
            )
            dates[date_str] = {
                "status": "success",
                "min_price": float(cheapest.get("price")),
                "airline": cheapest.get("airline") or cheapest.get("airline_code") or cheapest.get("airline_name"),
                "flight_no": cheapest.get("flight_no") or cheapest.get("flight_combo"),
                "count": len(priced),
                "sources": source_names,
                "last_attempt_at": attempt_at,
                "last_success_at": attempt_at,
                "error_type": None,
                "stale_after": (
                    current + timedelta(hours=max(0, int(cache_hours)))
                ).isoformat(timespec="seconds"),
                "round_id": round_id,
                "updated_at": attempt_at,
            }
        else:
            empty = dict(previous)
            empty.update(
                {
                    "status": "empty",
                    "last_attempt_at": attempt_at,
                    "error_type": None,
                    "round_id": round_id,
                    "updated_at": attempt_at,
                }
            )
            dates[date_str] = empty
        save_calendar(route, calendar, data_dir)
        if sleep_seconds:
            time.sleep(sleep_seconds)

    return calendar


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
        if not calendar_record_is_eligible(info):
            continue
        d = parse_date(date_str)
        if d < shanghai_today():
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
        if not calendar_record_is_eligible(info):
            continue
        d = parse_date(date_str)
        if d < shanghai_today():
            continue
        price = float(info["min_price"])
        by_weekday[d.weekday()].append(price)
        dated_prices.append((d, price))

    sample_count = sum(len(values) for values in by_weekday.values())
    if sample_count < min_samples or not dated_prices:
        return {"data_insufficient": True}

    medians = {
        WEEKDAY_NAMES[index]: round(statistics.median(values))
        for index, values in by_weekday.items()
        if values
    }
    iqrs = {}
    sample_counts = {}
    for index, values in by_weekday.items():
        if not values:
            continue
        weekday_name = WEEKDAY_NAMES[index]
        if len(values) == 1:
            p25 = p75 = values[0]
        else:
            p25, _median, p75 = statistics.quantiles(
                values,
                n=4,
                method="inclusive",
            )
        iqrs[weekday_name] = [round(p25), round(p75)]
        sample_counts[weekday_name] = len(values)
    minimums = {
        WEEKDAY_NAMES[index]: min(values)
        for index, values in by_weekday.items()
        if values
    }
    if not medians:
        return {"data_insufficient": True}

    min_day, min_price = min(dated_prices, key=lambda item: item[1])
    min_weekday = WEEKDAY_NAMES[min_day.weekday()]
    sorted_medians = sorted(medians.items(), key=lambda item: item[1])
    median_cheapest = sorted_medians[0][0]
    usual_tip = ""
    if len(sorted_medians) >= 2:
        median_cheapest_count = sample_counts[median_cheapest]
        median_gap = sorted_medians[1][1] - sorted_medians[0][1]
        if median_cheapest_count >= 3 and median_gap >= max(50, sorted_medians[1][1] * 0.05):
            p25, p75 = iqrs[median_cheapest]
            usual_tip = (
                f"；{median_cheapest}中位数更低"
                f"(n={median_cheapest_count},IQR CNY{p25:,.0f}-CNY{p75:,.0f})"
            )
    print(
        f"[周几统计] 最低价日={min_day.isoformat()}({min_weekday}) CNY{min_price:,.0f}, "
        f"各星期中位={medians} IQR={iqrs} 样本={sample_counts}"
    )
    return {
        "cheapest_weekday": median_cheapest,
        "usual_cheapest_weekday": median_cheapest if usual_tip else "",
        "by_weekday": medians,
        "median_by_weekday": medians,
        "iqr_by_weekday": iqrs,
        "sample_count_by_weekday": sample_counts,
        "min_by_weekday": minimums,
        "min_date": min_day.isoformat(),
        "min_weekday": min_weekday,
        "min_price": min_price,
        "sample_count": sample_count,
        "method_version": method_version("weekday"),
        "tip": f"近期最低出现在{min_weekday}({min_day.isoformat()},单程¥{min_price:,.0f}){usual_tip}",
    }


def calendar_rows(calendar: dict, target_date: str) -> list[dict]:
    target = parse_date(target_date)
    rows = []
    valid_prices = [
        float(info["min_price"])
        for info in (calendar.get("dates") or {}).values()
        if calendar_record_is_eligible(info)
    ]
    lowest = min(valid_prices) if valid_prices else None
    for date_str, info in sorted((calendar.get("dates") or {}).items()):
        if not isinstance(info, dict):
            continue
        record = _normalized_calendar_record(info)
        d = parse_date(date_str)
        if d < shanghai_today():
            continue
        price = (
            float(record["min_price"])
            if _valid_price(record.get("min_price"))
            else None
        )
        eligible = calendar_record_is_eligible(record)
        if price is None and record.get("status") == "success":
            continue
        rows.append(
            {
                "date": date_str,
                "weekday": WEEKDAY_NAMES[d.weekday()],
                "min_price": price,
                "airline": record.get("airline"),
                "sample_n": record.get("count"),
                "sources": _source_names(
                    record.get("sources") or record.get("source")
                ),
                "observed_at": (
                    record.get("last_success_at") or record.get("updated_at")
                ),
                "last_attempt_at": record.get("last_attempt_at"),
                "last_success_at": record.get("last_success_at"),
                "stale_after": record.get("stale_after"),
                "round_id": record.get("round_id"),
                "error_type": record.get("error_type"),
                "status": record.get("status"),
                "status_text": _status_text(record),
                "eligible_for_recommendation": eligible,
                "historical_reference": bool(price is not None and not eligible),
                "selected": d == target,
                "lowest": eligible and lowest is not None and price == lowest,
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
    if not calendar_record_is_eligible(info):
        return None
    return float(info["min_price"])


def roundtrip_calendar_rows(
    outbound_calendar: dict,
    target_date: str,
    *,
    return_low,
    return_date: str,
    return_sources=None,
    return_observed_at=None,
    return_sample_n=None,
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
        combined_sources = sorted(
            set(_source_names(row.get("sources")))
            | set(_source_names(return_sources))
        )
        observed_values = [
            str(value)[:10]
            for value in (row.get("observed_at"), return_observed_at)
            if value and len(str(value)) >= 10
        ]
        updated.update(
            {
                "outbound_min_price": outbound_price,
                "return_min_price": return_price,
                "return_date": str(return_date or ""),
                "min_price": combined,
                "value": combined,
                "scope": "roundtrip",
                "sources": combined_sources,
                "sample_n": int(row.get("sample_n") or row.get("count") or 0)
                + int(return_sample_n or 0),
                "observed_at": min(observed_values) if observed_values else row.get("observed_at"),
                "observation_window": (
                    [min(observed_values), max(observed_values)]
                    if observed_values
                    else None
                ),
                "breakdown": f"去¥{outbound_price:,.0f}+返¥{return_price:,.0f}",
            }
        )
        rows.append(updated)

    if rows:
        eligible_rows = [
            row
            for row in rows
            if row.get("eligible_for_recommendation", True)
            and _valid_price(row.get("min_price"))
        ]
        lowest_price = (
            min(row["min_price"] for row in eligible_rows)
            if eligible_rows
            else None
        )
        for row in rows:
            row["lowest"] = (
                lowest_price is not None
                and row.get("eligible_for_recommendation", True)
                and row.get("min_price") == lowest_price
            )
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
    if (
        not isinstance(selected, dict)
        or not selected.get("eligible_for_recommendation", True)
        or not _valid_price(selected.get("min_price"))
    ):
        return []

    current = float(selected["min_price"])
    target = parse_date(selected.get("date") or target_date)
    savings = []
    for row in rows:
        if (
            not isinstance(row, dict)
            or row is selected
            or not row.get("eligible_for_recommendation", True)
            or not _valid_price(row.get("min_price"))
        ):
            continue
        row_date = row.get("date")
        if not row_date:
            continue
        d = parse_date(str(row_date))
        if d < shanghai_today():
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
