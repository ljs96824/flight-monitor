import os

import httpx

from sources.base import FlightSource


class TravelpayoutsSource(FlightSource):
    name = "travelpayouts"

    def __init__(self):
        self.token = os.environ.get("TRAVELPAYOUTS_TOKEN", "")
        self.base_url = "https://api.travelpayouts.com"

    def fetch(self, origin, dest, date_str, cabin_class: str = "economy"):
        """
        Travelpayouts Data API 返回缓存的最低价数据。
        date_str格式: "2026-06-20"，需要转为 "2026-06"。
        """
        year_month = date_str[:7]
        flights = []

        try:
            resp = httpx.get(
                f"{self.base_url}/aviasales/v3/prices_for_dates",
                params={
                    "origin": origin,
                    "destination": dest,
                    "departure_at": date_str,
                    "one_way": "true",
                    "sorting": "price",
                    "limit": 15,
                    "currency": "cny",
                    "token": self.token,
                },
                timeout=15,
            )

            if resp.status_code == 200:
                data = resp.json()
                if data.get("success") and data.get("data"):
                    for item in data["data"]:
                        flight = self._parse_price_item(item, origin, dest, cabin_class)
                        if flight["price"] and flight["price"] > 0:
                            flights.append(flight)

        except Exception as exc:
            print(f"[travelpayouts] prices_for_dates 失败: {exc}")

        try:
            resp = httpx.get(
                f"{self.base_url}/v1/prices/direct",
                params={
                    "origin": origin,
                    "destination": dest,
                    "depart_date": year_month,
                    "token": self.token,
                },
                headers={"X-Access-Token": self.token},
                timeout=15,
            )

            if resp.status_code == 200:
                data = resp.json()
                if data.get("success") and data.get("data"):
                    dest_data = data["data"].get(dest, {})
                    for item in dest_data.values():
                        flight = self._parse_price_item(item, origin, dest, cabin_class)
                        if flight["price"] and flight["price"] > 0:
                            flights.append(flight)

        except Exception as exc:
            print(f"[travelpayouts] direct prices 失败: {exc}")

        seen = {}
        for flight in flights:
            combo = flight["flight_combo"]
            if combo not in seen or flight["price"] < seen[combo]["price"]:
                seen[combo] = flight

        unique = sorted(seen.values(), key=lambda flight: flight["price"])
        print(f"[travelpayouts] 成功，返回 {len(unique)} 个方案")

        return {
            "flights": unique,
            "price_insights": None,
            "source": "travelpayouts",
        }

    def _parse_price_item(
        self, item: dict, origin: str, dest: str, cabin_class: str
    ) -> dict:
        duration = item.get("duration_to", 0) or item.get("duration", 0) or 0
        airline = item.get("airline", "")
        flight_number = item.get("flight_number", "")

        return {
            "price": item.get("price"),
            "flight_combo": f"{airline or 'XX'}{flight_number}",
            "airlines": [airline] if airline else [],
            "airline_summary": airline,
            "route_summary": f"{origin} → {dest}",
            "total_duration_min": duration,
            "total_hours": round(duration / 60, 1) if duration else 0,
            "stops": item.get("transfers", 0),
            "stopovers": item.get("transfers", 0),
            "stopover_city": None,
            "segments": [],
            "layovers": [],
            "source": "travelpayouts",
            "data_source": "travelpayouts",
            "cabin_class": cabin_class,
            "extra": {},
        }
