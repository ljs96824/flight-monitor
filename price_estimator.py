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

    source = str(flight.get("data_source") or flight.get("source") or "")
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
