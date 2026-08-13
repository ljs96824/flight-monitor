"""Route-type source strategy profiles.

The aggregator keeps source selection in one place so domestic, international,
and Greater China routes use the intended quota profile.
"""

from __future__ import annotations

from datetime import date


ROUTE_SOURCE_PROFILES = {
    "domestic": {
        "sources": [
            {"name": "juhe", "role": "primary", "weight": 1.0},
            {"name": "duffel", "role": "enrichment", "weight": 0.0},
        ],
        "query": {
            "currency": "CNY",
            "hl": "zh-cn",
            "gl": "cn",
            "stops": "nonstop_preferred",
        },
        "primary_source": "juhe",
    },
    "international": {
        "sources": [
            {"name": "juhe", "role": "primary", "weight": 1.0},
            {"name": "duffel", "role": "enrichment", "weight": 0.0},
        ],
        # HasData 自 2026-08-14 因 403/订阅终止退役；保留元数据供历史口径追溯。
        "retired_sources": [
            {
                "name": "hasdata",
                "role": "primary",
                "weight": 1.0,
                "retired_on": "2026-08-14",
                "reason": "403/订阅终止",
            }
        ],
        "query": {
            "currency": "CNY",
            "hl": "zh-cn",
            "gl": "cn",
            "stops": "two_stops_or_fewer",
        },
        "primary_source": "juhe",
    },
    "greater_china": {
        "sources": [
            {"name": "juhe", "role": "primary", "weight": 1.0},
            {"name": "hasdata", "role": "cross_check", "weight": 0.6},
            {"name": "duffel", "role": "enrichment", "weight": 0.0},
        ],
        "query": {
            "currency": "CNY",
            "hl": "zh-cn",
            "gl": "cn",
            "stops": "nonstop_preferred",
        },
        "primary_source": "juhe+hasdata",
    },
}

ROUTE_TYPE_ALIASES = {
    "hk_mo_tw": "greater_china",
    "hkmotw": "greater_china",
    "hongkong_macao_taiwan": "greater_china",
    "hong_kong_macao_taiwan": "greater_china",
    "greaterchina": "greater_china",
    "greater-china": "greater_china",
}


def normalize_route_type(route_type: str | None) -> str | None:
    value = str(route_type or "").strip().lower()
    value = ROUTE_TYPE_ALIASES.get(value, value)
    if value in ROUTE_SOURCE_PROFILES:
        return value
    return None


def get_source_profile(route_type: str | None) -> dict:
    normalized = normalize_route_type(route_type)
    return ROUTE_SOURCE_PROFILES.get(normalized or "international")


def retired_listing_sources(route_type: str | None) -> list[dict]:
    """返回当前路线类型的已退役列表源元数据。"""
    return [
        dict(item)
        for item in (get_source_profile(route_type).get("retired_sources") or [])
        if str(item.get("name") or "").strip()
    ]


def expected_listing_sources(
    route_type: str | None,
    *,
    observed_day: str | date | None = None,
) -> set[str]:
    """按观测日派生应到场的列表源；未给日期时采用当前策略。"""
    profile = get_source_profile(route_type)
    expected = {
        str(item.get("name") or "").strip().lower()
        for item in profile.get("sources") or []
        if str(item.get("role") or "").strip().lower() != "enrichment"
        and str(item.get("name") or "").strip()
    }
    if observed_day in (None, ""):
        return expected
    try:
        day = observed_day if isinstance(observed_day, date) else date.fromisoformat(str(observed_day)[:10])
    except (TypeError, ValueError):
        return expected
    for item in retired_listing_sources(route_type):
        try:
            retired_on = date.fromisoformat(str(item.get("retired_on") or "")[:10])
        except ValueError:
            continue
        if day < retired_on:
            expected.add(str(item.get("name") or "").strip().lower())
    return expected
