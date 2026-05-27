"""SerpAPI Google Flights adapter."""

from __future__ import annotations

import os
import re
from datetime import datetime, time, timedelta

from serpapi import GoogleSearch

from sources.base import FlightSource


AIRLINE_BOOKING_URLS = {
    "MU": "https://www.ceair.com",
    "CA": "https://www.airchina.com.cn",
    "CZ": "https://www.csair.com",
    "9C": "https://www.ch.com",
    "HO": "https://www.juneyaoair.com",
    "3U": "https://www.sichuanair.com",
    "NH": "https://www.ana.co.jp/zh/cn/",
    "JL": "https://www.jal.co.jp/zhcn/",
    "MM": "https://www.flypeach.com/zh-cn",
    "AA": "https://www.aa.com",
    "UA": "https://www.united.com",
    "DL": "https://zh.delta.com",
}


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


def _price_value(value):
    if value is None:
        return None
    if isinstance(value, (int, float)):
        return value if value > 0 else None
    text = str(value)
    match = re.search(r"\d+(?:,\d{3})*(?:\.\d+)?", text)
    if not match:
        return None
    try:
        price = float(match.group(0).replace(",", ""))
    except ValueError:
        return None
    return price if price > 0 else None


def _first_airline_code(flight_nos: list[str]) -> str:
    for flight_no in flight_nos:
        match = re.match(r"\s*([A-Z0-9]{2})", str(flight_no or "").upper())
        if match:
            return match.group(1)
    return ""


def _search_url(platform: str, origin: str, dest: str, date_str: str | None, flight_no: str) -> str:
    date = str(date_str or "")
    compact_flight_no = re.sub(r"\s+", "", flight_no or "")
    if platform == "ctrip":
        return (
            f"https://flights.ctrip.com/online/list/oneway-{origin}-{dest}"
            f"?depdate={date}&cabin=y&flightno={compact_flight_no}"
        )
    if platform == "fliggy":
        return (
            "https://www.fliggy.com/flight/international-search"
            f"?depCity={origin}&arrCity={dest}&depDate={date}&flightNo={compact_flight_no}"
        )
    return ""


def _normalize_booking_option(option: dict, fallback_price) -> dict | None:
    platform = (
        option.get("platform")
        or option.get("name")
        or option.get("provider")
        or option.get("booking_site")
        or option.get("agency")
        or option.get("merchant")
    )
    url = option.get("url") or option.get("link") or option.get("booking_url")
    price = _price_value(option.get("price") or option.get("total_price"))
    if price is None and option.get("price") in (0, "0"):
        price = None
    if not platform and not url:
        return None
    return {
        "platform": str(platform or "Google Flights"),
        "price": price,
        "url": str(url or ""),
        "verified": bool(price and url),
        "source": "google_flights",
    }


def _extract_booking_options(
    flight_data: dict,
    flight_nos: list[str],
    price,
    origin: str,
    dest: str,
    date_str: str | None,
) -> list[dict]:
    options = []
    booking_token = flight_data.get("booking_token") or flight_data.get("bookingToken")
    parsed_price = _price_value(price)
    if booking_token:
        options.append(
            {
                "platform": "Google Flights 预订",
                "price": parsed_price,
                "url": f"https://www.google.com/travel/flights/booking?token={booking_token}",
                "verified": bool(parsed_price),
                "source": "booking_token",
                "direct_booking": True,
            }
        )

    raw_options = flight_data.get("booking_options") or flight_data.get("bookingOptions") or []
    if isinstance(raw_options, dict):
        raw_options = raw_options.get("options") or raw_options.get("booking_options") or []
    for option in raw_options if isinstance(raw_options, list) else []:
        if isinstance(option, dict):
            normalized = _normalize_booking_option(option, parsed_price)
            if normalized:
                options.append(normalized)

    extensions = flight_data.get("extensions") or []
    for item in extensions:
        if isinstance(item, dict):
            normalized = _normalize_booking_option(item, parsed_price)
            if normalized:
                options.append(normalized)

    first_flight_no = flight_nos[0] if flight_nos else ""
    airline_code = _first_airline_code(flight_nos)
    airline_url = AIRLINE_BOOKING_URLS.get(airline_code)
    if airline_url:
        options.append(
            {
                "platform": f"{airline_code} 航司官网",
                "price": None,
                "url": airline_url,
                "verified": False,
                "source": "inferred_airline",
            }
        )
    if origin and dest:
        options.extend(
            [
                {
                    "platform": "携程搜索",
                    "price": None,
                    "url": _search_url("ctrip", origin, dest, date_str, first_flight_no),
                    "verified": False,
                    "source": "inferred_ota",
                },
                {
                    "platform": "飞猪搜索",
                    "price": None,
                    "url": _search_url("fliggy", origin, dest, date_str, first_flight_no),
                    "verified": False,
                    "source": "inferred_ota",
                },
            ]
        )

    deduped = []
    seen = set()
    for option in options:
        key = (option.get("platform"), option.get("url"), option.get("price"))
        if option.get("url") and key not in seen:
            seen.add(key)
            deduped.append(option)
    return deduped


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
            price = flight.get("price")
            route_origin = cities[0] if cities else ""
            route_dest = cities[-1] if cities else ""
            parsed = {
                "price": price,
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
                "booking_options": _extract_booking_options(
                    flight,
                    flight_nos,
                    price,
                    route_origin,
                    route_dest,
                    date_str,
                ),
            }

            if parsed["price"] is not None:
                all_flights.append(parsed)

    return all_flights
