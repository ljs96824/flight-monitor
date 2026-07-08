"""Pure price-caliber conversion helpers.

The only stored price unit is per-person one-way. All other price scopes are
derived at the edge from that unit plus explicit passenger and itinerary inputs.
"""

from __future__ import annotations


def _to_float(value, default=0.0):
    try:
        if value is None or value == "":
            return default
        return float(value)
    except (TypeError, ValueError):
        return default


def _to_count(value):
    try:
        return max(0, int(value))
    except (TypeError, ValueError):
        return 0


def _clean_price(value):
    value = round(float(value), 2)
    return int(value) if value.is_integer() else value


def _scope_key(scope):
    text = str(scope or "per_person_oneway").strip().lower().replace("-", "_")
    if text in {"per_person_oneway", "single_person_oneway", "unit_oneway", "pp_oneway", "oneway"}:
        return "per_person_oneway"
    if text in {"per_person_roundtrip", "single_person_roundtrip", "unit_roundtrip", "pp_roundtrip", "roundtrip"}:
        return "per_person_roundtrip"
    if text in {"all_passengers_oneway", "total_oneway", "passenger_oneway", "all_oneway"}:
        return "all_passengers_oneway"
    if text in {"all_passengers_roundtrip", "total_roundtrip", "passenger_roundtrip", "all_roundtrip"}:
        return "all_passengers_roundtrip"
    raise ValueError(f"unknown price scope: {scope}")


def itinerary_price_pp(per_person_oneway, return_per_person_oneway=None, round_trip=False):
    """Return the per-person itinerary price from per-person one-way values."""
    outbound = _to_float(per_person_oneway)
    if return_per_person_oneway is not None:
        return _clean_price(outbound + _to_float(return_per_person_oneway))
    if round_trip:
        return _clean_price(outbound * 2)
    return _clean_price(outbound)


def passenger_rate_sum(passengers, route_type=None):
    """Return the passenger fare multiplier using the project pricing rules."""
    passengers = passengers if isinstance(passengers, dict) else {}
    try:
        from price_estimator import passenger_price_factor

        return _clean_price(passenger_price_factor(passengers, route_type))
    except Exception:
        return _clean_price(
            _to_count(passengers.get("adult"))
            + _to_count(passengers.get("child"))
            + _to_count(passengers.get("elderly"))
            + _to_count(passengers.get("infant"))
            or 1
        )


def price_in_scope(
    per_person_oneway,
    passengers=None,
    scope="per_person_oneway",
    route_type=None,
    round_trip=False,
    return_per_person_oneway=None,
):
    """Convert the stored per-person one-way price into the requested scope."""
    key = _scope_key(scope)
    factor = passenger_rate_sum(passengers, route_type)
    one_way_pp = _to_float(per_person_oneway)
    itinerary_pp = itinerary_price_pp(
        one_way_pp,
        return_per_person_oneway=return_per_person_oneway,
        round_trip=round_trip or key.endswith("roundtrip"),
    )

    if key == "per_person_oneway":
        return _clean_price(one_way_pp)
    if key == "per_person_roundtrip":
        return _clean_price(itinerary_pp)
    if key == "all_passengers_oneway":
        return _clean_price(one_way_pp * factor)
    return _clean_price(itinerary_pp * factor)


def budget_to_pp(
    budget,
    passengers=None,
    scope="per_person_oneway",
    route_type=None,
    round_trip=False,
):
    """Convert a budget in a visible scope back to per-person one-way."""
    key = _scope_key(scope)
    value = _to_float(budget)
    factor = passenger_rate_sum(passengers, route_type) or 1
    if key == "per_person_oneway":
        return _clean_price(value)
    if key == "all_passengers_oneway":
        return _clean_price(value / factor)
    divisor = 2 if round_trip or key.endswith("roundtrip") else 1
    if key == "per_person_roundtrip":
        return _clean_price(value / divisor)
    return _clean_price(value / factor / divisor)


def assert_same_caliber(left_scope, right_scope) -> bool:
    """Fail fast when two prices are compared with different visible scopes."""
    left = _scope_key(left_scope)
    right = _scope_key(right_scope)
    if left != right:
        raise AssertionError(
            f"\u4ef7\u683c\u53e3\u5f84\u4e0d\u4e00\u81f4: "
            f"left_scope={left_scope!r}, right_scope={right_scope!r}"
        )
    return True


def caliber_label(scope, passengers=None, route_type=None):
    """Return a reader-facing label for a price scope."""
    key = _scope_key(scope)
    base = {
        "per_person_oneway": "\u5355\u4eba\u5355\u7a0b",
        "per_person_roundtrip": "\u5355\u4eba\u5f80\u8fd4",
        "all_passengers_oneway": "\u5168\u5458\u5355\u7a0b",
        "all_passengers_roundtrip": "\u5168\u5458\u5f80\u8fd4",
    }[key]
    if key.startswith("all_passengers"):
        return (
            f"{base}(\u8d39\u7387\u5408\u8ba1"
            f"{passenger_rate_sum(passengers, route_type)}"
            f"\u00d7\u5355\u4eba)"
        )
    return base
