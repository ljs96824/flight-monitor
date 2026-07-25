"""订阅采集前的无状态日期校验。"""

from __future__ import annotations

from datetime import date, datetime, timedelta

from project_time import SHANGHAI_TZ
from airlines import LCC_POLICIES, resolve_lcc_policy

FLEX_STAGES = (1, 3, 7)


def shanghai_today() -> date:
    return datetime.now(SHANGHAI_TZ).date()


def _first_value(subscription: dict, key: str):
    value = subscription.get(key)
    if value not in (None, ""):
        return value
    for section_name in ("hard_constraints", "constraints", "basic", "preferences"):
        section = subscription.get(section_name)
        if not isinstance(section, dict):
            continue
        value = section.get(key)
        if value not in (None, ""):
            return value
    return None


def _as_bool(value) -> bool:
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)):
        return bool(value)
    return str(value or "").strip().lower() in {"1", "true", "yes", "on"}


def _as_nonnegative_int(value) -> int:
    try:
        return max(0, int(value or 0))
    except (TypeError, ValueError):
        return 0


def _parse_date(value) -> date | None:
    try:
        return date.fromisoformat(str(value or "")[:10])
    except (TypeError, ValueError):
        return None


def _flex_dates(center: date, flexibility) -> set[date]:
    """复刻主流程的 1/3/7 天分阶段弹性日期上界。"""
    days_range = _as_nonnegative_int(flexibility)
    offsets = {0}
    for stage in FLEX_STAGES:
        if days_range >= stage:
            offsets.update(range(-stage, stage + 1))
    return {center + timedelta(days=offset) for offset in offsets}


def derive_subscription_collection_dates(subscription: dict) -> list[date]:
    """返回订阅主行程可能触及的全部采集日期，不含独立固定篮子。"""
    dates: set[date] = set()
    depart_date = _parse_date(_first_value(subscription, "depart_date"))
    if depart_date is not None:
        dates.update(
            _flex_dates(
                depart_date,
                _first_value(subscription, "date_flexibility"),
            )
        )

    same_day = _as_bool(_first_value(subscription, "same_day_round_trip"))
    round_trip = _as_bool(_first_value(subscription, "round_trip")) or same_day
    return_date = _parse_date(_first_value(subscription, "return_date"))
    if same_day and return_date is None:
        return_date = depart_date
    if round_trip and return_date is not None:
        dates.update(
            _flex_dates(
                return_date,
                _first_value(subscription, "return_date_flexibility"),
            )
        )

    # 当天往返无完整组合时，主流程还可能补采前一晚去程和次日返程。
    if same_day and depart_date is not None:
        dates.add(depart_date - timedelta(days=1))
    if same_day and return_date is not None:
        dates.add(return_date + timedelta(days=1))

    return sorted(dates)


def evaluate_subscription_preflight(
    subscription: dict,
    *,
    today: date | None = None,
) -> dict:
    lcc_policy = str(resolve_lcc_policy(subscription, "any")).strip()
    if lcc_policy not in LCC_POLICIES:
        return {
            "skip": True,
            "reason_code": "invalid_lcc_policy",
            "reason": f"lcc_policy取值无效({lcc_policy})",
            "today": today or shanghai_today(),
            "collection_dates": [],
            "latest_date": None,
        }
    invalid_reason = str(subscription.get("invalid_reason") or "").strip()
    if subscription.get("validation_status") == "invalid" or invalid_reason:
        return {
            "skip": True,
            "reason_code": "invalid_location",
            "reason": invalid_reason or "地点无法解析",
            "today": today or shanghai_today(),
            "collection_dates": [],
            "latest_date": None,
        }
    collection_dates = derive_subscription_collection_dates(subscription)
    latest_date = max(collection_dates) if collection_dates else None
    effective_today = today or shanghai_today()
    return {
        "skip": bool(latest_date is not None and latest_date < effective_today),
        "reason_code": "expired" if latest_date is not None and latest_date < effective_today else "",
        "reason": "全部采集日期已过期" if latest_date is not None and latest_date < effective_today else "",
        "today": effective_today,
        "collection_dates": collection_dates,
        "latest_date": latest_date,
    }
