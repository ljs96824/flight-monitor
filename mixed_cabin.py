"""同航班混舱报价匹配与组合计价。"""

from __future__ import annotations

from copy import deepcopy

from cabin_allocation import validate_cabin_allocation
from flight_combo_utils import normalize_combo
from price_estimator import build_display_prices, round_display_price


MIXED_CABIN_DISCLOSURE = (
    "混舱为两舱分别报价合成,同航班两舱库存需分别验证,"
    "能否同机混订以支付页/客服为准"
)


def _flight_key(flight) -> str:
    if not isinstance(flight, dict):
        return ""
    raw = flight.get("flight_combo")
    if not raw:
        numbers = flight.get("flight_nos") or []
        raw = "+".join(str(item) for item in numbers if str(item or "").strip())
    if not raw:
        raw = flight.get("flight_no")
    return normalize_combo(raw)


def _business_price_map(flights) -> dict[str, dict]:
    result = {}
    for flight in flights or []:
        key = _flight_key(flight)
        try:
            price = float((flight or {}).get("price"))
        except (TypeError, ValueError):
            continue
        if not key or price <= 0:
            continue
        existing = result.get(key)
        if existing is None or price < float(existing.get("price") or 0):
            result[key] = flight
    return result


def _business_reference(outbound_business, return_business):
    valid = []
    for direction, flights in (("去程", outbound_business), ("返程", return_business)):
        for flight in flights or []:
            try:
                price = float((flight or {}).get("price"))
            except (TypeError, ValueError):
                continue
            if price > 0:
                valid.append((price, direction, flight))
    if not valid:
        return None
    price, direction, flight = min(valid, key=lambda item: item[0])
    return {
        "price": round_display_price(price),
        "raw_price": price,
        "direction": direction,
        "flight_combo": _flight_key(flight),
        "airline": flight.get("airline") or flight.get("airline_summary") or "",
        "source": flight.get("price_source") or flight.get("data_source") or flight.get("source") or "serpapi",
        "note": "本轮商务舱单程最低(不限航班)，仅供参照，非方案价",
    }


def _mixed_price_tiers(tree, passengers) -> dict:
    passenger_count = max(1, sum(int(value or 0) for value in passengers.values()))
    return {
        "unit_oneway": {"outbound": None, "return": None, "scope": "mixed_cabin"},
        "unit_roundtrip": None,
        "total_roundtrip_ref": tree["total"],
        "total_estimated": tree["total"],
        "per_person_estimated": round_display_price(tree["total"] / passenger_count),
        "passenger_count": passenger_count,
        "passengers": dict(passengers),
        "passenger_label": tree["passenger_label"],
        "factor": None,
        "is_roundtrip": True,
        "purchase_type": "两个单程拼接",
        "scope": "all_passengers_roundtrip",
        "mixed_cabin": True,
        "cabin_label": tree["cabin_label"],
        "note": tree["note"],
    }


def match_mixed_cabin_combinations(
    combinations,
    outbound_business_flights,
    return_business_flights,
    *,
    cabin_allocation,
    passengers,
    route_type,
) -> dict:
    """只允许同一去返航班组合命中商务报价，不做跨航班拼凑。"""
    allocation_result = validate_cabin_allocation(cabin_allocation, passengers)
    allocation = allocation_result["allocation"]
    outbound_prices = _business_price_map(outbound_business_flights)
    return_prices = _business_price_map(return_business_flights)
    priceable = []
    unpriceable = []

    for original in combinations or []:
        combo = deepcopy(original)
        outbound_key = _flight_key(combo.get("outbound") or {})
        return_key = _flight_key(combo.get("return") or {})
        business_outbound = outbound_prices.get(outbound_key)
        business_return = return_prices.get(return_key)
        missing = []
        if business_outbound is None:
            missing.append(outbound_key or "去程航班")
        if business_return is None:
            missing.append(return_key or "返程航班")
        if missing:
            combo["mixed_cabin_reason"] = (
                "商务舱价未获取(该航班未见SerpAPI报价): " + "+".join(missing)
            )
            unpriceable.append(combo)
            continue

        economy_outbound = float(
            combo.get("outbound_price") or (combo.get("outbound") or {}).get("price")
        )
        economy_return = float(
            combo.get("return_price") or (combo.get("return") or {}).get("price")
        )
        business_outbound_price = float(business_outbound["price"])
        business_return_price = float(business_return["price"])
        tree = build_display_prices(
            None,
            None,
            passengers,
            route_type,
            per_cabin_unit_prices={
                "outbound": {
                    "economy": economy_outbound,
                    "business": business_outbound_price,
                },
                "return": {
                    "economy": economy_return,
                    "business": business_return_price,
                },
            },
            cabin_allocation=allocation,
        )
        combo.update(
            {
                "mixed_cabin": True,
                "cabin_allocation": allocation,
                "cabin_label": tree["cabin_label"],
                "mixed_cabin_pricing": tree,
                "passenger_pricing": tree,
                "price_tiers": _mixed_price_tiers(tree, passengers),
                "passenger_total_price": tree["total"],
                "raw_passenger_total_price": tree["raw_total"],
                "total_price": tree["raw_total"],
                "business_outbound": business_outbound,
                "business_return": business_return,
                "business_price_source": "serpapi",
                "mixed_cabin_disclosure": MIXED_CABIN_DISCLOSURE,
                "mixed_cabin_price_notes": {
                    "economy": (
                        (combo.get("outbound") or {}).get("price_note")
                        or (combo.get("return") or {}).get("price_note")
                        or ""
                    ),
                    "business": (
                        business_outbound.get("price_note")
                        or business_return.get("price_note")
                        or "SerpAPI展示价,税费构成未拆分,以支付页为准"
                    ),
                },
            }
        )
        priceable.append(combo)

    stats = {
        "candidates": len(combinations or []),
        "full": len(priceable),
        "partial": len(unpriceable),
    }
    visible_keys = set(outbound_prices) | set(return_prices)
    return {
        "priceable": priceable,
        "unpriceable": unpriceable,
        "stats": stats,
        "matching_rate": len(priceable) / len(combinations) if combinations else 0.0,
        "business_visible_count": len(visible_keys),
        "business_reference": _business_reference(
            outbound_business_flights, return_business_flights
        ),
        "disclosure": MIXED_CABIN_DISCLOSURE,
    }
