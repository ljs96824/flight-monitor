"""Estimate checkout prices from flight search display prices."""

from __future__ import annotations


LCC_BAG_FEES = {
    "春秋航空": 280,
    "9C": 280,
    "九元航空": 200,
    "AQ": 200,
    "乐桃航空": 350,
    "MM": 350,
    "Peach": 350,
    "捷星": 300,
    "JQ": 300,
    "Jetstar": 300,
    "酷航": 320,
    "TR": 320,
    "Scoot": 320,
    "亚洲航空": 280,
    "AK": 280,
    "AirAsia": 280,
    "Spirit": 400,
    "Frontier": 380,
}


def _to_float(value):
    try:
        if value is None or value == "":
            return None
        return float(value)
    except (TypeError, ValueError):
        return None


def _to_count(value, default=0):
    try:
        return max(0, int(value))
    except (TypeError, ValueError):
        return default


def normalize_passengers_for_pricing(passengers):
    """Return a canonical passenger count dict for price estimation."""
    passengers = passengers if isinstance(passengers, dict) else {}
    normalized = {
        "adult": _to_count(passengers.get("adult")),
        "child": _to_count(passengers.get("child")),
        "elderly": _to_count(passengers.get("elderly")),
        "infant": _to_count(passengers.get("infant")),
    }
    if not any(normalized.values()):
        normalized["adult"] = 1
    return normalized


PASSENGER_FARE_RATE_SOURCE = "PASSENGER_FARE_RATES"
_logged_passenger_factor_keys = set()


def reset_passenger_factor_log_cache():
    """Clear passenger fare factor log de-duplication state for tests or new processes."""
    _logged_passenger_factor_keys.clear()


def _current_round_id_for_fare_log():
    try:
        from observations_store import get_current_round

        round_id, _ = get_current_round()
        return round_id
    except Exception:
        return None


def _should_log_passenger_factor(route_key, passengers):
    key = (
        _current_round_id_for_fare_log(),
        route_key,
        passengers.get("adult", 0),
        passengers.get("child", 0),
        passengers.get("elderly", 0),
        passengers.get("infant", 0),
    )
    if key in _logged_passenger_factor_keys:
        return False
    _logged_passenger_factor_keys.add(key)
    return True


PASSENGER_FARE_RATES = {
    "domestic": {
        "adult": 1.0,
        "elderly": 1.0,
        "child": 0.50,
        "infant": 0.10,
        "note": "儿童票按国内常规5折估算,婴儿票按国内常规1折估算,实际以支付页为准",
    },
    "international": {
        "adult": 1.0,
        "elderly": 1.0,
        "child": 0.75,
        "infant": 0.10,
        "note": "儿童票按国际航线常见约75折估算,婴儿票按约1折估算,实际以支付页为准",
    },
}


def _route_rate_key(route_type=None):
    route = str(route_type or "").lower()
    if route in {"international", "intl"}:
        return "international"
    return "domestic"


def passenger_fare_rates(route_type=None):
    """Return the single source of truth for passenger fare ratios."""
    key = _route_rate_key(route_type)
    return key, PASSENGER_FARE_RATES[key]


def _passenger_ratios(route_type=None):
    _, rates = passenger_fare_rates(route_type)
    return {
        "child": rates["child"],
        "infant": rates["infant"],
        "note": rates["note"],
    }


def passenger_price_factor(passengers, route_type=None):
    passengers = normalize_passengers_for_pricing(passengers)
    route_key, rates = passenger_fare_rates(route_type)
    factor = (
        passengers.get("adult", 0) * rates["adult"]
        + passengers.get("elderly", 0) * rates["elderly"]
        + passengers.get("child", 0) * rates["child"]
        + passengers.get("infant", 0) * rates["infant"]
    )
    factor = round(factor, 2)
    if _should_log_passenger_factor(route_key, passengers):
        print(
            f"[票价系数] route={route_key} "
            f"成人{passengers.get('adult', 0)}×{rates['adult']} "
            f"儿童{passengers.get('child', 0)}×{rates['child']} "
            f"老人{passengers.get('elderly', 0)}×{rates['elderly']} "
            f"婴儿{passengers.get('infant', 0)}×{rates['infant']} "
            f"合计={factor} 来源={PASSENGER_FARE_RATE_SOURCE}[{route_key}]"
        )
    return factor


def _passenger_label(passengers):
    parts = []
    for key, label in (("adult", "成人"), ("child", "儿童"), ("elderly", "老人"), ("infant", "婴儿")):
        count = _to_count((passengers or {}).get(key))
        if count:
            parts.append(f"{count}{label}")
    return "+".join(parts) or "1成人"


def build_passenger_price_breakdown(unit_price, passengers, cabin=None, route_type=None):
    """Build a passenger-aware total from one adult reference price.

    The returned total is an estimate when child/infant fares are not provided
    by the data source. Adult and elderly passengers use the adult reference
    fare; child/infant ratios depend on route type.
    """
    price = _to_float(unit_price) or 0
    passengers = normalize_passengers_for_pricing(passengers)
    ratios = _passenger_ratios(route_type)
    parts = []
    total = 0.0
    for key, label, ratio in (
        ("adult", "成人", 1.0),
        ("elderly", "老人", 1.0),
        ("child", "儿童", ratios["child"]),
        ("infant", "婴儿", ratios["infant"]),
    ):
        count = passengers.get(key, 0)
        if not count:
            continue
        unit = price * ratio
        subtotal = unit * count
        total += subtotal
        parts.append(
            {
                "type": key,
                "label": label,
                "count": count,
                "ratio": ratio,
                "unit_price": round(unit),
                "total": round(subtotal),
            }
        )
    return {
        "unit_price": price,
        "total": round(total),
        "factor": round(passenger_price_factor(passengers, route_type), 2),
        "passengers": passengers,
        "passenger_label": _passenger_label(passengers),
        "parts": parts,
        "cabin": cabin or "economy",
        "route_type": route_type or "",
        "note": ratios["note"],
    }


def calc_total_price_for_passengers(unit_price, passengers, cabin=None, route_type=None):
    """Return the estimated total fare for all passengers from one adult price."""
    return build_passenger_price_breakdown(unit_price, passengers, cabin, route_type)["total"]


def calc_total_for_passengers(unit_price, passengers, route_type=None, cabin=None):
    """Return all-passenger total with route type before cabin for callers."""
    return calc_total_price_for_passengers(unit_price, passengers, cabin, route_type)


def _round_price(value):
    number = _to_float(value)
    return round(number) if number is not None else None


def build_price_tiers(
    outbound_unit_price,
    return_unit_price=None,
    passengers=None,
    route_type=None,
    purchase_type=None,
    estimated_outbound=None,
    estimated_return=None,
    total_estimated=None,
    cabin=None,
):
    """Build the five public price scopes for one plan."""
    normalized = normalize_passengers_for_pricing(passengers)
    passenger_count = sum(normalized.values()) or 1
    outbound = _to_float(outbound_unit_price) or 0
    ret = _to_float(return_unit_price)
    is_roundtrip = ret is not None

    outbound_breakdown = build_passenger_price_breakdown(outbound, normalized, cabin, route_type)
    return_breakdown = build_passenger_price_breakdown(ret, normalized, cabin, route_type) if is_roundtrip else None
    unit_roundtrip = outbound + (ret or 0)
    total_ref = outbound_breakdown["total"] + (return_breakdown["total"] if return_breakdown else 0)

    explicit_estimated = _round_price(total_estimated)
    if explicit_estimated is not None:
        estimated_total = explicit_estimated
    else:
        est_outbound = _to_float(estimated_outbound)
        est_return = _to_float(estimated_return)
        if est_outbound is None:
            est_outbound = outbound
        if is_roundtrip and est_return is None:
            est_return = ret
        estimated_unit_total = est_outbound + (est_return or 0)
        estimated_total = calc_total_price_for_passengers(estimated_unit_total, normalized, cabin, route_type)

    per_person = round(estimated_total / passenger_count) if passenger_count else estimated_total
    note = outbound_breakdown.get("note") or ""
    if passenger_count > 1:
        note = (
            f"{note}；多人价格按单人参考价估算，低价舱库存不足时多人下单可能重新定价，"
            "请以支付页选择实际乘机人数后的总价为准"
        )
    return {
        "unit_oneway": {
            "outbound": _round_price(outbound),
            "return": _round_price(ret) if is_roundtrip else None,
            "scope": "single_person_oneway",
        },
        "unit_roundtrip": _round_price(unit_roundtrip) if is_roundtrip else None,
        "total_roundtrip_ref": _round_price(total_ref),
        "total_estimated": _round_price(estimated_total),
        "per_person_estimated": _round_price(per_person),
        "passenger_count": passenger_count,
        "passengers": normalized,
        "passenger_label": outbound_breakdown.get("passenger_label") or _passenger_label(normalized),
        "factor": outbound_breakdown.get("factor"),
        "is_roundtrip": is_roundtrip,
        "purchase_type": purchase_type or ("roundtrip" if is_roundtrip else "oneway"),
        "route_type": route_type or "",
        "note": note,
    }


def _airline_text(flight: dict) -> str:
    parts = []
    for key in ("airline", "airline_summary"):
        if flight.get(key):
            parts.append(str(flight.get(key)))
    parts.extend(str(name) for name in flight.get("airlines") or [] if name)
    for segment in flight.get("segments") or []:
        if isinstance(segment, dict) and segment.get("airline"):
            parts.append(str(segment.get("airline")))
        if isinstance(segment, dict) and segment.get("flight_no"):
            parts.append(str(segment.get("flight_no")))
    if flight.get("flight_combo"):
        parts.append(str(flight.get("flight_combo")))
    return " / ".join(dict.fromkeys(parts))


def _unique_airlines(flight: dict) -> list[str]:
    names = []
    names.extend(str(name) for name in flight.get("airlines") or [] if name)
    for segment in flight.get("segments") or []:
        if isinstance(segment, dict) and segment.get("airline"):
            names.append(str(segment.get("airline")))
    if not names and flight.get("airline_summary"):
        names.extend(part.strip() for part in str(flight["airline_summary"]).split("/") if part.strip())
    return list(dict.fromkeys(names))


def _has_confirmed_checked_bag(flight: dict) -> bool:
    fare_rules = flight.get("fare_rules") or {}
    baggage = fare_rules.get("baggage") or {}
    if (baggage.get("checked_pieces") or 0) > 0 or (baggage.get("checked_kg") or 0) > 0:
        return True

    extra = flight.get("extra") or {}
    detail = extra.get("baggage_detail") or {}
    checked = detail.get("checked") or {}
    if checked.get("is_free") and (checked.get("quantity") or 0) > 0:
        return True

    for bag in extra.get("baggage") or []:
        if isinstance(bag, dict) and bag.get("type") == "checked" and (bag.get("quantity") or 0) > 0:
            return True
    return False


def calc_transaction_price(flight, hard_constraints):
    """计算预估交易价 = 展示价 + 各项费用。"""

    display_price = _to_float(flight.get("price")) or 0
    extra_items = []
    extra_total = 0

    source = str(flight.get("data_source") or flight.get("source") or "")
    if "juhe" in source.lower():
        price_includes = flight.get("price_includes") or "含票面、机建、燃油(实时)"
    else:
        # Google Flights family sources usually include taxes, fuel surcharges,
        # and airport fees. Extras here are optional checkout costs.
        price_includes = "含税费、含燃油附加费、含机场建设费"

    airline = _airline_text(flight)
    baggage_req = (hard_constraints or {}).get("baggage", "unknown")

    is_lcc = False
    for lcc_name, fee in LCC_BAG_FEES.items():
        if lcc_name in airline:
            is_lcc = True
            if baggage_req in ("required", "unknown") and not _has_confirmed_checked_bag(flight):
                extra_total += fee
                extra_items.append(
                    {
                        "name": "托运行李（20kg）",
                        "amount": fee,
                        "note": "廉航不含免费托运",
                    }
                )
            break

    if is_lcc:
        extra_total += 50
        extra_items.append(
            {"name": "选座费", "amount": 50, "note": "廉航默认随机分配"}
        )

    if "小代理" in source:
        extra_total += 80
        extra_items.append({"name": "平台服务费", "amount": 80, "note": "小代理可能收取"})

    try:
        stops = int(flight.get("stops") or 0)
    except (TypeError, ValueError):
        stops = 0
    if stops > 0:
        airlines = _unique_airlines(flight)
        if len(airlines) > 1:
            extra_total += 200
            extra_items.append(
                {
                    "name": "跨航司行李重托",
                    "amount": 200,
                    "note": "非联程中转可能需重新办理托运",
                }
            )

    transaction_price = display_price + extra_total
    price_low = int(display_price * 0.98) if display_price > 0 else 0
    price_high = int(transaction_price * 1.05) if transaction_price > 0 else 0
    adjustments = [
        f"{item['name']}约 +¥{item['amount']}"
        + (f"（{item['note']}）" if item.get("note") else "")
        for item in extra_items
    ]

    return {
        "display_price": display_price,
        "extra_total": extra_total,
        "transaction_price": transaction_price,
        "extra_items": extra_items,
        "is_lcc": is_lcc,
        "price_includes": price_includes,
        "price_excludes": [item["name"] for item in extra_items] if extra_items else ["无额外费用"],
        # Backward-compatible fields used by existing notification code.
        "estimated_price": transaction_price,
        "extra_cost": extra_total,
        "price_range": [price_low, price_high],
        "adjustments": adjustments,
        "tax_included": True,
        "confidence": "high" if not adjustments else "medium",
    }


def estimate_real_price(flight, hard_constraints):
    """Backward-compatible alias for older analyzer imports."""
    return calc_transaction_price(flight, hard_constraints)
