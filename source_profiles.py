"""Route-type source strategy profiles.

The aggregator keeps source selection in one place so domestic, international,
and Greater China routes do not waste quota on sources that are not useful for
that route type.
"""

from __future__ import annotations


ROUTE_SOURCE_PROFILES = {
    "domestic": {
        "sources": [
            {"name": "juhe", "role": "primary", "weight": 1.0},
            {"name": "serpapi", "role": "cross_check", "weight": 0.6},
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
            {"name": "serpapi", "role": "primary", "weight": 1.0},
            {"name": "hasdata", "role": "primary", "weight": 1.0},
            {"name": "duffel", "role": "enrichment", "weight": 0.0},
        ],
        "query": {
            "currency": "CNY",
            "hl": "zh-cn",
            "gl": "cn",
            "stops": "two_stops_or_fewer",
        },
        "primary_source": "serpapi+hasdata",
    },
    "greater_china": {
        "sources": [
            {"name": "serpapi", "role": "primary", "weight": 1.0},
            {"name": "hasdata", "role": "cross_check", "weight": 0.6},
            {"name": "duffel", "role": "enrichment", "weight": 0.0},
        ],
        "query": {
            "currency": "CNY",
            "hl": "zh-cn",
            "gl": "cn",
            "stops": "nonstop_preferred",
        },
        "primary_source": "serpapi",
    },
}


def normalize_route_type(route_type: str | None) -> str | None:
    value = str(route_type or "").strip().lower()
    if value in ROUTE_SOURCE_PROFILES:
        return value
    return None


def get_source_profile(route_type: str | None) -> dict:
    normalized = normalize_route_type(route_type)
    return ROUTE_SOURCE_PROFILES.get(normalized or "international")
