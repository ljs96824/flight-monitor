"""Duffel flight offers adapter."""

from __future__ import annotations

import os
import re
from datetime import datetime

from sources.base import FlightSource

try:
    from duffel_api import Duffel
except ImportError:  # pragma: no cover - handled at runtime when source is enabled
    Duffel = None


def _get(obj, key, default=None):
    if isinstance(obj, dict):
        return obj.get(key, default)
    return getattr(obj, key, default)


def _format_time(value) -> str:
    if not value:
        return ""
    text = str(value)
    try:
        dt = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError:
        return text
    return dt.strftime("%Y-%m-%d %H:%M")


def _duration_minutes(value) -> int:
    if value is None:
        return 0
    if isinstance(value, (int, float)):
        return int(value)

    text = str(value)
    match = re.fullmatch(r"PT(?:(\d+)H)?(?:(\d+)M)?", text)
    if match:
        hours = int(match.group(1) or 0)
        minutes = int(match.group(2) or 0)
        return hours * 60 + minutes

    return 0


def _layover_minutes(arr_time: str, dep_time: str) -> int:
    if not arr_time or not dep_time:
        return 0
    try:
        arr = datetime.strptime(arr_time, "%Y-%m-%d %H:%M")
        dep = datetime.strptime(dep_time, "%Y-%m-%d %H:%M")
    except ValueError:
        return 0
    return max(0, int((dep - arr).total_seconds() // 60))


class DuffelSource(FlightSource):
    name = "duffel"

    def __init__(self):
        if Duffel is None:
            raise RuntimeError("duffel-api is not installed")

        token = os.environ.get("DUFFEL_TOKEN")
        if not token:
            raise RuntimeError("DUFFEL_TOKEN is not set")

        self.client = Duffel(access_token=token)

    def fetch(self, origin, dest, date_str):
        request_builder = (
            self.client.offer_requests.create()
            .slices(
                [{"origin": origin, "destination": dest, "departure_date": date_str}]
            )
            .passengers([{"type": "adult"}])
        )
        if hasattr(request_builder, "return_offers"):
            request_builder = request_builder.return_offers()
        offer_request = request_builder.execute()

        flights = []
        for offer in _get(offer_request, "offers", []) or []:
            flight = self._parse_offer(offer)
            if flight:
                flights.append(flight)

        return {
            "flights": sorted(flights, key=lambda item: item["price"]),
            "price_insights": None,
            "source": self.name,
            "raw": {},
        }

    def _parse_offer(self, offer) -> dict | None:
        segments = []
        slices = _get(offer, "slices", []) or []

        for offer_slice in slices:
            for segment in _get(offer_slice, "segments", []) or []:
                carrier = _get(segment, "marketing_carrier")
                origin = _get(segment, "origin")
                destination = _get(segment, "destination")
                flight_no = (
                    f"{_get(carrier, 'iata_code', '')} "
                    f"{_get(segment, 'marketing_carrier_flight_number', '')}"
                ).strip()
                dep_time = _format_time(_get(segment, "departing_at"))
                arr_time = _format_time(_get(segment, "arriving_at"))

                segments.append(
                    {
                        "flight_no": flight_no,
                        "airline": _get(carrier, "name", ""),
                        "aircraft": _get(_get(segment, "aircraft"), "name", ""),
                        "dep_airport": _get(origin, "iata_code", ""),
                        "dep_city": _get(origin, "city_name", None)
                        or _get(origin, "name", ""),
                        "dep_time": dep_time,
                        "arr_airport": _get(destination, "iata_code", ""),
                        "arr_city": _get(destination, "city_name", None)
                        or _get(destination, "name", ""),
                        "arr_time": arr_time,
                        "duration_min": _duration_minutes(_get(segment, "duration")),
                        "cabin_class": _get(slices[0], "fare_brand_name", None)
                        or "Economy",
                    }
                )

        if not segments:
            return None

        layovers = []
        for index, segment in enumerate(segments[:-1]):
            next_segment = segments[index + 1]
            layovers.append(
                {
                    "city": segment.get("arr_city") or segment.get("arr_airport"),
                    "airport": segment.get("arr_airport", ""),
                    "wait_minutes": _layover_minutes(
                        segment.get("arr_time", ""),
                        next_segment.get("dep_time", ""),
                    ),
                }
            )

        total_duration_min = sum(segment.get("duration_min") or 0 for segment in segments)
        total_duration_min += sum(layover.get("wait_minutes") or 0 for layover in layovers)
        airlines = list(
            dict.fromkeys(segment.get("airline", "") for segment in segments if segment.get("airline"))
        )
        flight_nos = [
            segment.get("flight_no", "") for segment in segments if segment.get("flight_no")
        ]

        extra = {
            "baggage": self._baggage_info(slices),
            "changeable": False,
            "refundable": False,
        }
        conditions = _get(offer, "conditions")
        if conditions:
            extra["changeable"] = _get(conditions, "change_before_departure") is not None
            extra["refundable"] = _get(conditions, "refund_before_departure") is not None

        return {
            "price": float(_get(offer, "total_amount")),
            "currency": _get(offer, "total_currency", ""),
            "segments": segments,
            "layovers": layovers,
            "flight_nos": flight_nos,
            "flight_combo": "+".join(flight_nos),
            "airlines": airlines,
            "airline": airlines[0] if airlines else "",
            "airline_summary": " / ".join(airlines),
            "route_summary": " → ".join(
                [segments[0]["dep_airport"]]
                + [segment["arr_airport"] for segment in segments]
            ),
            "total_duration_min": total_duration_min,
            "total_hours": round(total_duration_min / 60, 1),
            "duration_hours": round(total_duration_min / 60, 2),
            "stops": len(segments) - 1,
            "stopovers": len(segments) - 1,
            "stopover_city": segments[0]["arr_airport"] if len(segments) > 1 else None,
            "layover_summary": (
                "、".join(
                    f"{layover['city']}等{layover['wait_minutes'] // 60}h{layover['wait_minutes'] % 60}m"
                    for layover in layovers
                )
                if layovers
                else "直飞"
            ),
            "extra": extra,
            "source": self.name,
            "data_source": self.name,
        }

    def _baggage_info(self, slices) -> list[dict]:
        baggage = []
        for offer_slice in slices:
            for segment in _get(offer_slice, "segments", []) or []:
                for passenger in _get(segment, "passengers", []) or []:
                    for bag in _get(passenger, "baggages", []) or []:
                        baggage.append(
                            {
                                "type": _get(bag, "type", ""),
                                "quantity": _get(bag, "quantity", 0),
                            }
                        )
        return baggage
