"""HasData Google Flights adapter."""

from __future__ import annotations

import os

import httpx

from sources.base import FlightSource
from sources.serpapi_source import parse_google_flights


def _camel_to_snake(name: str) -> str:
    chars = []
    for char in name:
        if char.isupper() and chars:
            chars.append("_")
        chars.append(char.lower())
    return "".join(chars)


def _normalize_hasdata_response(value):
    """Convert HasData camelCase response fields to SerpAPI-style snake_case."""
    if isinstance(value, list):
        return [_normalize_hasdata_response(item) for item in value]
    if isinstance(value, dict):
        return {
            _camel_to_snake(key): _normalize_hasdata_response(item)
            for key, item in value.items()
        }
    return value


class HasDataSource(FlightSource):
    name = "hasdata"
    url = "https://api.hasdata.com/scrape/google/flights"

    def fetch(
        self, origin: str, dest: str, date_str: str, cabin_class: str = "economy"
    ) -> dict:
        api_key = os.environ.get("HASDATA_KEY", "")
        overrides = getattr(self, "query_overrides", {}) or {}
        params = {
            "departureId": origin,
            "arrivalId": dest,
            "outboundDate": date_str,
            "type": "oneWay",
            "currency": overrides.get("currency") or "CNY",
            "hl": overrides.get("hl") or "zh-cn",
            "gl": overrides.get("gl") or "cn",
            "sortBy": "price",
            "stops": _hasdata_stops_value(overrides.get("stops")),
        }
        headers = {
            "x-api-key": api_key,
            "Content-Type": "application/json",
        }

        response = httpx.get(self.url, params=params, headers=headers, timeout=30)
        response.raise_for_status()
        results = response.json()
        if "error" in results:
            raise RuntimeError(results["error"])
        normalized_results = _normalize_hasdata_response(results)

        return {
            "flights": parse_google_flights(
                normalized_results, self.name, cabin_class, date_str
            ),
            "price_insights": normalized_results.get("price_insights"),
            "source": self.name,
            "raw": results,
        }


def _hasdata_stops_value(value) -> str:
    return {
        "nonstop_preferred": "nonStop",
        "two_stops_or_fewer": "twoStopsOrFewer",
    }.get(str(value or ""), "twoStopsOrFewer")
