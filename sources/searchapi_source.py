"""SearchAPI.io Google Flights adapter."""

from __future__ import annotations

import os
import time

import httpx

from sources.base import FlightSource
from sources.serpapi_source import parse_google_flights


SEARCHAPI_PRICE_NOTE = "SearchAPI展示价,税费构成未拆分,以支付页为准"
SEARCHAPI_SUPPORTED_CABINS = frozenset({"economy", "business"})
SEARCHAPI_TRAVEL_CLASS = {
    "economy": "economy",
    "premium_economy": "premium_economy",
    "business": "business",
    "first": "first_class",
    "first_class": "first_class",
}


def _missing_searchapi_key_result() -> dict:
    return {
        "flights": [],
        "source": "searchapi",
        "raw": {},
        "source_status": "not_configured",
        "skipped_reason": "缺少 SearchAPI 密钥（SEARCHAPI_KEY）",
    }


def _searchapi_travel_class(cabin_class: str) -> str:
    return SEARCHAPI_TRAVEL_CLASS.get(str(cabin_class or "economy").strip().lower(), "economy")


def _searchapi_stops_value(value) -> str:
    if value in ("nonstop_preferred", "nonstop"):
        return "nonstop"
    if value == "one_stop_or_fewer":
        return "one_stop_or_fewer"
    return "two_stops_or_fewer"


class SearchAPISource(FlightSource):
    name = "searchapi"
    url = "https://www.searchapi.io/api/v1/search"
    supported_cabins = SEARCHAPI_SUPPORTED_CABINS

    def preflight_skip(self, _origin, _dest, _date_str, _cabin_class):
        if not os.environ.get("SEARCHAPI_KEY"):
            return _missing_searchapi_key_result()
        return None

    def fetch(
        self, origin: str, dest: str, date_str: str, cabin_class: str = "economy"
    ) -> dict:
        api_key = os.environ.get("SEARCHAPI_KEY")
        if not api_key:
            return _missing_searchapi_key_result()
        overrides = getattr(self, "query_overrides", {}) or {}
        params = {
            "engine": "google_flights",
            "departure_id": origin,
            "arrival_id": dest,
            "outbound_date": date_str,
            "flight_type": "one_way",
            "gl": overrides.get("gl") or "cn",
            "hl": overrides.get("hl") or "zh-cn",
            "currency": overrides.get("currency") or "CNY",
            "stops": _searchapi_stops_value(overrides.get("stops")),
            "sort_by": "price",
            "travel_class": _searchapi_travel_class(cabin_class),
            "api_key": api_key,
        }

        response = httpx.get(self.url, params=params, timeout=30)
        print(f"[SearchAPI] 请求URL: {_redact_url(str(response.request.url))}")
        print(f"[SearchAPI] 状态码: {response.status_code}")

        if response.status_code in {400, 401, 403}:
            time.sleep(2)
            auth_response = httpx.get(
                self.url,
                params={key: value for key, value in params.items() if key != "api_key"},
                headers={"Authorization": f"Bearer {api_key}"},
                timeout=30,
            )
            print(f"[SearchAPI] Header认证URL: {_redact_url(str(auth_response.request.url))}")
            print(f"[SearchAPI] Header认证状态码: {auth_response.status_code}")
            if auth_response.status_code < 400:
                response = auth_response

        response.raise_for_status()
        results = response.json()
        if "error" in results:
            raise RuntimeError(results["error"])

        flights = parse_google_flights(
            results, self.name, cabin_class, date_str,
            marketing_fallback=True, price_note=SEARCHAPI_PRICE_NOTE,
        )
        raw_result_count = sum(
            len(results.get(category) or []) for category in ("best_flights", "other_flights")
        )
        return {
            "flights": flights,
            "raw_result_count": raw_result_count,
            "price_insights": results.get("price_insights"),
            "source": self.name,
            "source_status": "success" if flights else "empty",
            "raw": results,
        }


def _redact_url(url: str) -> str:
    if "api_key=" not in url:
        return url
    prefix, rest = url.split("api_key=", 1)
    suffix = ""
    if "&" in rest:
        _, suffix = rest.split("&", 1)
        suffix = "&" + suffix
    return prefix + "api_key=***" + suffix
