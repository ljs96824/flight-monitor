"""混舱无方案场景的展示参考价组装。"""

from __future__ import annotations

from price_estimator import build_display_prices, round_display_price


def _number(value):
    try:
        return float(value) if value is not None else None
    except (TypeError, ValueError):
        return None


def build_mixed_cabin_reference_price(
    *,
    primary_plan: dict | None,
    mixed_matching: dict | None,
    cabin_summary: dict | None,
    cabin_allocation: dict | None,
    passengers: dict | None,
    route_type: str | None,
) -> dict:
    """优先返回真实完整组合价，否则给出带口径的跨航班单程拼算参考。"""
    primary = primary_plan or {}
    matching = mixed_matching or {}
    summary = cabin_summary or {}
    allocation = cabin_allocation or {}

    matched_candidates = []
    if primary.get("mixed_cabin"):
        matched_candidates.append(primary)
    matched_candidates.extend(matching.get("priceable") or [])
    matched_options = []
    for candidate in matched_candidates:
        tree = candidate.get("mixed_cabin_pricing") or {}
        total = _number(tree.get("total"))
        if candidate.get("mixed_cabin") and total is not None:
            matched_options.append((total, tree))
    if matched_options:
        matched_total, matched_tree = min(matched_options, key=lambda item: item[0])
        raw_total = _number(matched_tree.get("raw_total"))
        return {
            "kind": "matched_roundtrip",
            "amount": round_display_price(matched_total),
            "raw_amount": matched_total if raw_total is None else raw_total,
            "scope": "all_passengers_roundtrip",
            "label": "全员往返组合价(同航班两舱全匹配)",
            "display_tree": matched_tree,
        }

    economy_price = _number(summary.get("economy_unit_price"))
    business_reference = matching.get("business_reference") or {}
    business_price = _number(
        business_reference.get("raw_price")
        or business_reference.get("price")
    )
    if economy_price is None or business_price is None:
        missing = []
        if economy_price is None:
            missing.append("经济舱最低价")
        if business_price is None:
            missing.append("商务舱最低价")
        return {
            "kind": "unavailable",
            "amount": None,
            "scope": "all_passengers_oneway",
            "label": "各舱最低单程拼算参考(不同航班,非可订组合)",
            "reason": f"缺少{'、'.join(missing)}",
        }

    try:
        tree = build_display_prices(
            None,
            passengers=passengers,
            route_type=route_type,
            per_cabin_unit_prices={
                "outbound": {
                    "economy": economy_price,
                    "business": business_price,
                }
            },
            cabin_allocation=allocation,
        )
    except (TypeError, ValueError) as exc:
        return {
            "kind": "unavailable",
            "amount": None,
            "scope": "all_passengers_oneway",
            "label": "各舱最低单程拼算参考(不同航班,非可订组合)",
            "reason": str(exc),
        }

    return {
        "kind": "synthetic_oneway",
        "amount": tree["total"],
        "raw_amount": tree["raw_total"],
        "scope": "all_passengers_oneway",
        "label": "各舱最低单程拼算参考(不同航班,非可订组合)",
        "display_tree": tree,
        "source_prices": {
            "economy": economy_price,
            "business": business_price,
        },
    }
