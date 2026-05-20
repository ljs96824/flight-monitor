"""Duffel flight offers adapter using the REST API directly."""

from __future__ import annotations

import os
import re
from datetime import datetime

import httpx

from sources.base import FlightSource


DUFFEL_URL = "https://api.duffel.com/air/offer_requests"


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
        token = os.environ.get("DUFFEL_TOKEN")
        if not token:
            raise RuntimeError("DUFFEL_TOKEN is not set")
        self.token = token

    def fetch(self, origin, dest, date_str, cabin_class: str = "economy"):
        payload = {
            "data": {
                "slices": [
                    {
                        "origin": origin,
                        "destination": dest,
                        "departure_date": date_str,
                    }
                ],
                "passengers": [{"type": "adult"}],
                "cabin_class": cabin_class,
                "return_offers": True,
            }
        }
        headers = {
            "Authorization": f"Bearer {self.token}",
            "Duffel-Version": "v2",
            "Content-Type": "application/json",
        }

        response = httpx.post(DUFFEL_URL, json=payload, headers=headers, timeout=30)
        print(f"[Duffel] 状态码: {response.status_code}")
        response.raise_for_status()

        results = response.json()
        offers = (results.get("data") or {}).get("offers") or []
        flights = []
        for offer in offers:
            flight = self._parse_offer(offer, cabin_class)
            if flight:
                flights.append(flight)

        return {
            "flights": sorted(flights, key=lambda item: item["price"]),
            "price_insights": None,
            "source": self.name,
            "raw": results,
        }

    def _parse_offer(self, offer: dict, cabin_class: str = "economy") -> dict | None:
        segments = []
        slices = offer.get("slices") or []

        for offer_slice in slices:
            fare_brand_name = offer_slice.get("fare_brand_name") or "Economy"
            for segment in offer_slice.get("segments") or []:
                carrier = segment.get("marketing_carrier") or {}
                origin = segment.get("origin") or {}
                destination = segment.get("destination") or {}
                flight_no = (
                    f"{carrier.get('iata_code', '')} "
                    f"{segment.get('marketing_carrier_flight_number', '')}"
                ).strip()
                dep_time = _format_time(segment.get("departing_at"))
                arr_time = _format_time(segment.get("arriving_at"))

                segments.append(
                    {
                        "flight_no": flight_no,
                        "airline": carrier.get("name", ""),
                        "aircraft": (segment.get("aircraft") or {}).get("name", ""),
                        "dep_airport": origin.get("iata_code", ""),
                        "dep_city": origin.get("city_name") or origin.get("name", ""),
                        "dep_time": dep_time,
                        "arr_airport": destination.get("iata_code", ""),
                        "arr_city": destination.get("city_name")
                        or destination.get("name", ""),
                        "arr_time": arr_time,
                        "duration_min": _duration_minutes(segment.get("duration")),
                        "cabin_class": fare_brand_name,
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
            dict.fromkeys(
                segment.get("airline", "")
                for segment in segments
                if segment.get("airline")
            )
        )
        flight_nos = [
            segment.get("flight_no", "") for segment in segments if segment.get("flight_no")
        ]
        conditions = offer.get("conditions") or {}
        changeable = conditions.get("change_before_departure") is not None
        refundable = conditions.get("refund_before_departure") is not None
        extra = {
            "baggage": self._baggage_info(slices),
            "baggage_detail": self._baggage_detail(slices),
            "seat_detail": self._seat_detail(offer),
            "refund_change": {
                "changeable": changeable,
                "refundable": refundable,
                "change_fee": "免费" if changeable else None,
                "refund_fee": "免费" if refundable else None,
            },
            "changeable": changeable,
            "refundable": refundable,
        }

        return {
            "price": float(offer.get("total_amount")),
            "currency": offer.get("total_currency", ""),
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
            "cabin_class": cabin_class,
        }

    def _baggage_info(self, slices: list[dict]) -> list[dict]:
        baggage = []
        for offer_slice in slices:
            for segment in offer_slice.get("segments") or []:
                for passenger in segment.get("passengers") or []:
                    for bag in passenger.get("baggages") or []:
                        baggage.append(
                            {
                                "type": bag.get("type", ""),
                                "quantity": bag.get("quantity", 0),
                                "weight": bag.get("weight"),
                                "weight_unit": bag.get("weight_unit"),
                            }
                        )
        return baggage

    def _baggage_detail(self, slices: list[dict]) -> dict:
        baggage_info = {
            "checked": {
                "quantity": 0,
                "weight_kg": None,
                "is_free": False,
            },
            "carry_on": {
                "quantity": 0,
                "weight_kg": None,
                "is_free": False,
            },
        }

        for offer_slice in slices:
            for segment in offer_slice.get("segments") or []:
                for passenger in segment.get("passengers") or []:
                    for bag in passenger.get("baggages") or []:
                        bag_type = bag.get("type", "")
                        quantity = bag.get("quantity", 0) or 0

                        if bag_type == "checked" and quantity > 0:
                            baggage_info["checked"]["quantity"] = quantity
                            baggage_info["checked"]["is_free"] = True
                            weight_kg = self._weight_kg(bag)
                            if weight_kg is not None:
                                baggage_info["checked"]["weight_kg"] = weight_kg

                        elif bag_type == "carry_on" and quantity > 0:
                            baggage_info["carry_on"]["quantity"] = quantity
                            baggage_info["carry_on"]["is_free"] = True
                            weight_kg = self._weight_kg(bag)
                            if weight_kg is not None:
                                baggage_info["carry_on"]["weight_kg"] = weight_kg

        return baggage_info

    @staticmethod
    def _weight_kg(bag: dict):
        weight = bag.get("weight")
        if weight is None:
            return None

        unit = bag.get("weight_unit", "kg")
        if unit == "lb":
            return round(weight * 0.453592)
        return weight

    def _seat_detail(self, offer: dict) -> dict:
        seat_info = {
            "cabin_class": "economy",
            "seat_selectable": False,
            "seat_free": False,
            "seat_price": None,
            "seat_currency": None,
        }

        for offer_slice in offer.get("slices") or []:
            for segment in offer_slice.get("segments") or []:
                passengers = segment.get("passengers") or [{}]
                passenger = passengers[0] if passengers else {}
                cabin = passenger.get("cabin_class", "economy")
                cabin_name = passenger.get("cabin_class_marketing_name", "")
                seat_info["cabin_class"] = cabin
                if cabin_name:
                    seat_info["cabin_class_name"] = cabin_name

        for service in offer.get("available_services") or []:
            if service.get("type") != "seat":
                continue

            seat_info["seat_selectable"] = True
            amount = service.get("total_amount")
            if amount:
                seat_info["seat_price"] = float(amount)
                seat_info["seat_currency"] = service.get("total_currency", "CNY")
                seat_info["seat_free"] = float(amount) == 0
            else:
                seat_info["seat_free"] = True

        return seat_info
