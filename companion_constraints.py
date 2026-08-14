"""同行约束旧字段与规范飞行偏好之间的派生单一真值。"""

from __future__ import annotations

from collections.abc import Mapping


COMPANION_CONSTRAINT_ORDER = (
    "direct_preferred",
    "no_redeye",
    "avoid_long_layover",
    "need_baggage",
    "need_refund_change",
    "daytime_arrival",
    "limited_mobility",
)

COMPANION_CONSTRAINT_AUDIT = {
    "direct_preferred": {"concept": "transfer", "disposition": "derived"},
    "no_redeye": {"concept": "time", "disposition": "derived"},
    "avoid_long_layover": {"concept": "transfer", "disposition": "derived"},
    "need_baggage": {"concept": "baggage", "disposition": "derived"},
    "need_refund_change": {"concept": "refund", "disposition": "derived"},
    "daytime_arrival": {"concept": "time", "disposition": "derived"},
    "limited_mobility": {
        "concept": "mobility",
        "disposition": "independent_control",
    },
}


def _truthy(value) -> bool:
    if isinstance(value, bool):
        return value
    return str(value or "").strip().lower() in {"1", "true", "yes", "on", "checked"}


def derive_companion_constraints(values: Mapping) -> list[str]:
    """从页面唯一飞行偏好派生旧 schema 字段，顺序保持历史文案不变。"""
    source = values or {}
    selected = set()
    transfer_policy = str(source.get("transfer_policy") or "").strip()
    if transfer_policy in {"reasonable", "direct_only"}:
        selected.add("direct_preferred")

    departure_policy = str(source.get("departure_time_policy") or "").strip()
    time_preference = str(source.get("time_preference") or "").strip()
    allow_redeye = source.get("allow_redeye")
    if (
        departure_policy in {"no_redeye", "daytime"}
        or time_preference in {"no_redeye", "daytime"}
        or (allow_redeye is not None and not _truthy(allow_redeye))
    ):
        selected.add("no_redeye")

    if str(source.get("short_transfer_limit") or "").strip() == "extra_3":
        selected.add("avoid_long_layover")
    if str(source.get("baggage") or "").strip() == "required":
        selected.add("need_baggage")
    if str(source.get("refund_flexibility") or "").strip() == "required":
        selected.add("need_refund_change")

    arrival_policy = str(source.get("arrival_time_policy") or "").strip()
    arrival_preference = str(source.get("arrival_preference") or "").strip()
    if (
        arrival_policy in {"daytime", "daytime_only"}
        or arrival_preference == "daytime"
        or _truthy(source.get("prefer_daytime_arrival"))
    ):
        selected.add("daytime_arrival")
    if _truthy(source.get("mobility_limited")):
        selected.add("limited_mobility")

    return [item for item in COMPANION_CONSTRAINT_ORDER if item in selected]
