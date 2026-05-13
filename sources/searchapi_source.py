"""SearchAPI.io Google Flights adapter."""

from __future__ import annotations

import os

import httpx

from sources.base import FlightSource
from sources.serpapi_source import parse_google_flights


class SearchAPISource(FlightSource):
    name = "searchapi"
    url = "https://www.searchapi.io/api/v1/search"

    def fetch(self, origin: str, dest: str, date_str: str) -> dict:
        params = {
            "engine": "google_flights",
            "departure_id": origin,
            "arrival_id": dest,
            "outbound_date": date_str,
            "type": "2",
            "currency": "CNY",
            "api_key": os.environ.get("SEARCHAPI_KEY"),
        }

        response = httpx.get(self.url, params=params, timeout=30)
        response.raise_for_status()
        results = response.json()
        if "error" in results:
            raise RuntimeError(results["error"])

        return {
            "flights": parse_google_flights(results, self.name),
            "price_insights": results.get("price_insights"),
            "source": self.name,
            "raw": results,
        }
