"""SerpAPI Google Flights adapter."""

from __future__ import annotations

import os
from datetime import datetime

from serpapi import GoogleSearch

from sources.base import FlightSource


class SerpAPISource(FlightSource):
    name = "serpapi"

    def fetch(
        self, origin: str, dest: str, date_str: str, cabin_class: str = "economy"
    ) -> dict:
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
        selected_cabin = _google_cabin_code(cabin_class)
        if selected_cabin:
            params["selected_cabins"] = selected_cabin

        search = GoogleSearch(params)
        results = search.get_dict()
        if "error" in results:
            raise RuntimeError(results["error"])

        return {
            "flights": parse_google_flights(results, self.name, cabin_class),
            "price_insights": results.get("price_insights"),
            "source": self.name,
            "raw": results,
        }


def _google_cabin_code(cabin_class: str) -> str:
    return {
        "economy": "M",
        "business": "C",
    }.get(cabin_class, "M")


def _layover_minutes(arr_time: str, dep_time: str) -> int:
    if not arr_time or not dep_time:
        return 0
    try:
        arr = datetime.strptime(arr_time, "%Y-%m-%d %H:%M")
        dep = datetime.strptime(dep_time, "%Y-%m-%d %H:%M")
    except ValueError:
        return 0
    return max(0, int((dep - arr).total_seconds() // 60))


def parse_google_flights(
    results: dict, source_name: str, cabin_class: str = "economy"
) -> list[dict]:
    all_flights = []

    for category in ["best_flights", "other_flights"]:
        for flight in results.get(category, []) or []:
            segments = flight.get("flights", []) or []
            if not segments:
                continue

            flight_nos = []
            airlines = []
            cities = []
            parsed_segments = []
            layovers = []

            for index, segment in enumerate(segments):
                flight_no = segment.get("flight_number", "")
                airline = segment.get("airline", "")
                flight_nos.append(flight_no)
                airlines.append(airline)

                dep_airport = segment.get("departure_airport", {}) or {}
                arr_airport = segment.get("arrival_airport", {}) or {}
                if not cities:
                    cities.append(dep_airport.get("id", ""))
                cities.append(arr_airport.get("id", ""))

                segment_info = {
                    "flight_no": flight_no,
                    "airline": airline,
                    "aircraft": segment.get("airplane", ""),
                    "dep_airport": dep_airport.get("id", ""),
                    "dep_city": dep_airport.get("name", ""),
                    "dep_time": dep_airport.get("time", ""),
                    "arr_airport": arr_airport.get("id", ""),
                    "arr_city": arr_airport.get("name", ""),
                    "arr_time": arr_airport.get("time", ""),
                    "duration_min": segment.get("duration", 0) or 0,
                    "cabin_class": cabin_class,
                }
                parsed_segments.append(segment_info)

                if index < len(segments) - 1:
                    next_segment = segments[index + 1]
                    next_dep = (next_segment.get("departure_airport") or {}).get("time", "")
                    layovers.append(
                        {
                            "city": arr_airport.get("name", ""),
                            "airport": arr_airport.get("id", ""),
                            "wait_minutes": _layover_minutes(
                                arr_airport.get("time", ""),
                                next_dep,
                            ),
                        }
                    )

            total_duration_min = flight.get("total_duration") or 0
            parsed = {
                "price": flight.get("price"),
                "flight_nos": flight_nos,
                "flight_combo": "+".join(flight_nos),
                "airlines": list(dict.fromkeys(airlines)),
                "airline": airlines[0] if airlines else "",
                "airline_summary": " / ".join(dict.fromkeys(airlines)),
                "route_summary": " → ".join(city for city in cities if city),
                "total_duration_min": total_duration_min,
                "total_hours": round(total_duration_min / 60, 1),
                "duration_hours": round(total_duration_min / 60, 2),
                "stopover_city": cities[1] if len(cities) > 2 else None,
                "stopovers": len(segments) - 1,
                "stops": len(segments) - 1,
                "segments": parsed_segments,
                "layovers": layovers,
                "layover_summary": (
                    "、".join(
                        f"{layover['city']}等{layover['wait_minutes'] // 60}h{layover['wait_minutes'] % 60}m"
                        for layover in layovers
                    )
                    if layovers
                    else "直飞"
                ),
                "cabin_class": cabin_class,
                "data_source": source_name,
            }

            if parsed["price"] is not None:
                all_flights.append(parsed)

    return all_flights
