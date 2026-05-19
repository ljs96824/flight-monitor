import os

import httpx

from sources.base import FlightSource


class SkyscannerSource(FlightSource):
    name = "skyscanner"

    def __init__(self):
        self.api_key = os.environ.get("RAPIDAPI_KEY", "")
        self.base_url = "https://sky-scrapper.p.rapidapi.com/api/v1"
        self.headers = {
            "X-RapidAPI-Key": self.api_key,
            "X-RapidAPI-Host": "sky-scrapper.p.rapidapi.com",
        }

    def fetch(self, origin, dest, date_str):
        """Sky Scrapper需要先查airport的entityId，再搜航班。"""
        flights = []

        try:
            origin_id = self._get_airport_id(origin)
            dest_id = self._get_airport_id(dest)

            if not origin_id or not dest_id:
                print(f"[skyscanner] 机场ID查询失败: {origin}={origin_id}, {dest}={dest_id}")
                return {"flights": [], "source": "skyscanner"}

            resp = httpx.get(
                f"{self.base_url}/flights/searchFlights",
                headers=self.headers,
                params={
                    "originSkyId": origin,
                    "destinationSkyId": dest,
                    "originEntityId": origin_id,
                    "destinationEntityId": dest_id,
                    "date": date_str,
                    "cabinClass": "economy",
                    "adults": "1",
                    "currency": "CNY",
                    "market": "CN",
                    "countryCode": "CN",
                },
                timeout=20,
            )

            if resp.status_code != 200:
                print(f"[skyscanner] 搜索失败: {resp.status_code}")
                return {"flights": [], "source": "skyscanner"}

            data = resp.json()
            itineraries = data.get("data", {}).get("itineraries", [])

            for itinerary in itineraries:
                price_raw = itinerary.get("price", {}).get("raw", 0)
                legs = itinerary.get("legs", [])

                if not legs or not price_raw:
                    continue

                leg = legs[0]
                segments_data = leg.get("segments", [])

                segments = []
                layovers = []
                flight_nos = []
                airlines = []

                for index, segment in enumerate(segments_data):
                    carrier = segment.get("marketingCarrier", {})
                    departure = segment.get("origin", {})
                    arrival = segment.get("destination", {})

                    flight_no = (
                        f"{carrier.get('alternateId', '')}{segment.get('flightNumber', '')}"
                    )
                    flight_nos.append(flight_no)

                    airline_name = carrier.get("name", "")
                    if airline_name and airline_name not in airlines:
                        airlines.append(airline_name)

                    segment_info = {
                        "flight_no": flight_no,
                        "airline": airline_name,
                        "dep_airport": departure.get("flightPlaceId", ""),
                        "dep_city": departure.get("name", ""),
                        "dep_time": segment.get("departure", ""),
                        "arr_airport": arrival.get("flightPlaceId", ""),
                        "arr_city": arrival.get("name", ""),
                        "arr_time": segment.get("arrival", ""),
                        "duration_min": segment.get("durationInMinutes", 0),
                    }
                    segments.append(segment_info)

                    if index < len(segments_data) - 1:
                        next_segment = segments_data[index + 1]
                        layover_min = 0
                        try:
                            from datetime import datetime

                            arrival_time = datetime.fromisoformat(
                                segment.get("arrival", "").replace("Z", "")
                            )
                            next_departure_time = datetime.fromisoformat(
                                next_segment.get("departure", "").replace("Z", "")
                            )
                            layover_min = int(
                                (next_departure_time - arrival_time).total_seconds()
                                / 60
                            )
                        except Exception:
                            pass

                        layovers.append(
                            {
                                "city": arrival.get("name", ""),
                                "airport": arrival.get("flightPlaceId", ""),
                                "wait_minutes": max(0, layover_min),
                            }
                        )

                if not segments:
                    continue

                flight = {
                    "price": price_raw,
                    "flight_combo": "+".join(flight_nos),
                    "airlines": airlines,
                    "airline_summary": " / ".join(airlines),
                    "route_summary": " → ".join(
                        [segments[0]["dep_airport"]]
                        + [segment["arr_airport"] for segment in segments]
                    ),
                    "total_duration_min": leg.get("durationInMinutes", 0),
                    "total_hours": round(leg.get("durationInMinutes", 0) / 60, 1),
                    "stops": len(segments) - 1,
                    "segments": segments,
                    "layovers": layovers,
                    "source": "skyscanner",
                    "data_source": "skyscanner",
                    "extra": {},
                }
                flights.append(flight)

            flights.sort(key=lambda flight: flight["price"])

        except Exception as exc:
            print(f"[skyscanner] 异常: {exc}")

        print(f"[skyscanner] 成功，返回 {len(flights)} 个方案")
        return {"flights": flights, "price_insights": None, "source": "skyscanner"}

    def _get_airport_id(self, iata_code):
        """查询机场的entityId。"""
        try:
            resp = httpx.get(
                f"{self.base_url}/flights/searchAirport",
                headers=self.headers,
                params={"query": iata_code},
                timeout=10,
            )
            if resp.status_code == 200:
                data = resp.json().get("data", [])
                if data:
                    return data[0].get("entityId", "")
        except Exception:
            pass
        return None
