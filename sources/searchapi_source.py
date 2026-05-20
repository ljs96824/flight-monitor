"""SearchAPI.io Google Flights adapter."""

from __future__ import annotations

import os

import httpx

from sources.base import FlightSource
from sources.serpapi_source import _google_cabin_code, parse_google_flights


class SearchAPISource(FlightSource):
    name = "searchapi"
    url = "https://www.searchapi.io/api/v1/search"

    def fetch(
        self, origin: str, dest: str, date_str: str, cabin_class: str = "economy"
    ) -> dict:
        api_key = os.environ.get("SEARCHAPI_KEY")
        params = {
            "engine": "google_flights",
            "departure_id": origin,
            "arrival_id": dest,
            "outbound_date": date_str,
            "flight_type": "one_way",
            "gl": "cn",
            "hl": "zh-cn",
            "currency": "CNY",
            "stops": "two_stops_or_fewer",
            "sort_by": "price",
            "api_key": api_key,
        }
        selected_cabin = _google_cabin_code(cabin_class)
        if selected_cabin:
            params["selected_cabins"] = selected_cabin

        response = httpx.get(self.url, params=params, timeout=30)
        print(f"[SearchAPI] 请求URL: {_redact_url(str(response.request.url))}")
        print(f"[SearchAPI] 状态码: {response.status_code}")

        if response.status_code in {400, 401, 403}:
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

        return {
            "flights": parse_google_flights(results, self.name, cabin_class),
            "price_insights": results.get("price_insights"),
            "source": self.name,
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
