"""SerpAPI Google Flights adapter."""

from __future__ import annotations

import os

from serpapi import GoogleSearch

from sources.base import FlightSource


class SerpAPISource(FlightSource):
    name = "serpapi"

    def fetch(self, origin: str, dest: str, date_str: str) -> dict:
        params = {
            "engine": "google_flights",
            "departure_id": origin,
            "arrival_id": dest,
            "outbound_date": date_str,
            "type": "2",
            "currency": "CNY",
            "hl": "zh-CN",
            "sort": "2",
            "stops": "2",
            "api_key": os.environ.get("SERPAPI_KEY"),
        }

        search = GoogleSearch(params)
        results = search.get_dict()
        if "error" in results:
            raise RuntimeError(results["error"])

        return {
            "flights": parse_google_flights(results, self.name),
            "price_insights": results.get("price_insights"),
            "source": self.name,
            "raw": results,
        }


def parse_google_flights(results: dict, source_name: str) -> list[dict]:
    all_flights = []

    for category in ["best_flights", "other_flights"]:
        for flight in results.get(category, []) or []:
            segments = flight.get("flights", []) or []
            if not segments:
                continue

            flight_nos = []
            airlines = []
            cities = []

            for segment in segments:
                flight_no = segment.get("flight_number", "")
                airline = segment.get("airline", "")
                flight_nos.append(flight_no)
                airlines.append(airline)

                dep_airport = segment.get("departure_airport", {}) or {}
                arr_airport = segment.get("arrival_airport", {}) or {}
                if not cities:
                    cities.append(dep_airport.get("id", ""))
                cities.append(arr_airport.get("id", ""))

            parsed = {
                "price": flight.get("price"),
                "flight_nos": flight_nos,
                "flight_combo": "+".join(flight_nos),
                "airlines": list(dict.fromkeys(airlines)),
                "airline": airlines[0] if airlines else "",
                "route_summary": " → ".join(city for city in cities if city),
                "duration_hours": round((flight.get("total_duration") or 0) / 60, 2),
                "stopover_city": cities[1] if len(cities) > 2 else None,
                "stopovers": len(segments) - 1,
                "data_source": source_name,
            }

            if parsed["price"] is not None:
                all_flights.append(parsed)

    return all_flights
