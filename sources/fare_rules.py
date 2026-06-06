"""Fare rule normalization helpers."""

from __future__ import annotations


def _normalize_cabin_class(value) -> str:
    text = str(value or "economy").strip().lower().replace(" ", "_")
    mapping = {
        "economy": "economy",
        "coach": "economy",
        "premium_economy": "premium_economy",
        "premium": "premium_economy",
        "business": "business",
        "first": "first",
        "first_class": "first",
    }
    return mapping.get(text, text if text else "economy")


def _fee_value(value):
    if value in (None, "", False):
        return None
    if isinstance(value, (int, float)):
        return value
    text = str(value)
    if text in {"免费", "free", "Free", "FREE"}:
        return 0
    return text


def _deadline_hours(rule: dict) -> int | None:
    for key in ["deadline_hours", "deadline_before_departure_hours", "deadline"]:
        value = rule.get(key)
        if value is None:
            continue
        try:
            return int(value)
        except (TypeError, ValueError):
            return None
    return None


def _rule_conditions(rule: dict) -> str:
    for key in ["conditions", "condition", "description", "text"]:
        value = rule.get(key)
        if value:
            return str(value)
    return ""


def _rule_block(source: dict, allowed_keys: list[str], fee_keys: list[str]) -> dict:
    allowed = None
    for key in allowed_keys:
        if key in source:
            allowed = bool(source.get(key))
            break

    fee = None
    for key in fee_keys:
        if key in source:
            fee = _fee_value(source.get(key))
            break

    return {
        "allowed": allowed,
        "fee": fee,
        "deadline_hours": _deadline_hours(source),
        "conditions": _rule_conditions(source),
    }


def standardize_fare_rules(duffel_data, flight_combo) -> dict:
    """Map Duffel baggage, change, refund, and cabin data to a standard schema."""
    data = duffel_data or {}
    baggage_detail = data.get("baggage_detail") or {}
    checked = baggage_detail.get("checked") or {}
    carry_on = baggage_detail.get("carry_on") or {}
    refund_change = data.get("refund_change") or {}
    seat_detail = data.get("seat_detail") or {}

    cabin_class = (
        seat_detail.get("cabin_class")
        or data.get("cabin_class")
        or data.get("travel_class")
        or "economy"
    )

    change = _rule_block(
        {**data, **refund_change},
        ["changeable", "change_allowed", "allowed"],
        ["change_fee", "fee"],
    )
    refund = _rule_block(
        {**data, **refund_change},
        ["refundable", "refund_allowed", "allowed"],
        ["refund_fee", "fee"],
    )

    return {
        "flight_combo": flight_combo,
        "baggage": {
            "carry_on_kg": carry_on.get("weight_kg"),
            "checked_kg": checked.get("weight_kg"),
            "checked_pieces": checked.get("quantity", 0) or 0,
            "extra_bag_price": data.get("extra_bag_price"),
        },
        "change": change,
        "refund": refund,
        "cabin_class": _normalize_cabin_class(cabin_class),
    }


def standardize_domestic_fare_rules(flight: dict | None) -> dict:
    """Map domestic China inferred baggage/refund rules to the standard schema."""
    from domestic_fare_rules import standardize_domestic_fare_rules as _standardize

    return _standardize(flight)
