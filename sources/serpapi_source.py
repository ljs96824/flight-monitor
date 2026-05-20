"""SerpAPI Google Flights adapter."""

from __future__ import annotations

import os
import re
from datetime import datetime, time, timedelta

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
            "flights": parse_google_flights(results, self.name, cabin_class, date_str),
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
        arr = _parse_datetime(arr_time)
        dep = _parse_datetime(dep_time)
    except ValueError:
        return 0
    if not arr or not dep:
        return 0
    return max(0, int((dep - arr).total_seconds() // 60))


def _parse_datetime(value: str) -> datetime | None:
    text = str(value or "").strip()
    if not text:
        return None

    for fmt in ["%Y-%m-%d %H:%M", "%Y-%m-%dT%H:%M:%S"]:
        try:
            return datetime.strptime(text, fmt)
        except ValueError:
            pass

    try:
        parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
        return parsed.replace(tzinfo=None)
    except ValueError:
        return None


def _normalize_airport_time(
    value: str,
    date_str: str | None = None,
    previous_dt: datetime | None = None,
) -> tuple[str, datetime | None]:
    """Keep full datetimes; infer dates for bare HH:MM values in sequence."""
    text = str(value or "").strip()
    if not text:
        return "", None

    parsed = _parse_datetime(text)
    if parsed:
        return text, parsed

    if re.fullmatch(r"\d{1,2}:\d{2}(:\d{2})?", text) and date_str:
        try:
            base_date = (
                previous_dt.date()
                if previous_dt is not None
                else datetime.fromisoformat(date_str).date()
            )
            hour, minute = (int(part) for part in text[:5].split(":"))
            inferred = datetime.combine(base_date, time(hour, minute))
            while previous_dt is not None and inferred < previous_dt:
                inferred += timedelta(days=1)
            return inferred.strftime("%Y-%m-%d %H:%M"), inferred
        except ValueError:
            return text, None

    return text, None


def parse_google_flights(
    results: dict,
    source_name: str,
    cabin_class: str = "economy",
    date_str: str | None = None,
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
            previous_event_dt = None

            for index, segment in enumerate(segments):
                flight_no = segment.get("flight_number", "")
                airline = segment.get("airline", "")
                flight_nos.append(flight_no)
                airlines.append(airline)

                dep_airport = segment.get("departure_airport", {}) or {}
                arr_airport = segment.get("arrival_airport", {}) or {}
                dep_time, dep_dt = _normalize_airport_time(
                    dep_airport.get("time", ""),
                    date_str,
                    previous_event_dt,
                )
                if dep_dt:
                    previous_event_dt = dep_dt
                arr_time, arr_dt = _normalize_airport_time(
                    arr_airport.get("time", ""),
                    date_str,
                    previous_event_dt,
                )
                if arr_dt:
                    previous_event_dt = arr_dt
                if not cities:
                    cities.append(dep_airport.get("id", ""))
                cities.append(arr_airport.get("id", ""))

                segment_info = {
                    "flight_no": flight_no,
                    "airline": airline,
                    "aircraft": segment.get("airplane", ""),
                    "dep_airport": dep_airport.get("id", ""),
                    "dep_city": dep_airport.get("name", ""),
                    "dep_time": dep_time,
                    "arr_airport": arr_airport.get("id", ""),
                    "arr_city": arr_airport.get("name", ""),
                    "arr_time": arr_time,
                    "duration_min": segment.get("duration", 0) or 0,
                    "cabin_class": cabin_class,
                }
                parsed_segments.append(segment_info)

                if index < len(segments) - 1:
                    next_segment = segments[index + 1]
                    next_dep, _ = _normalize_airport_time(
                        (next_segment.get("departure_airport") or {}).get("time", ""),
                        date_str,
                        arr_dt or previous_event_dt,
                    )
                    layovers.append(
                        {
                            "city": arr_airport.get("name", ""),
                            "airport": arr_airport.get("id", ""),
                            "wait_minutes": _layover_minutes(
                                arr_time,
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
