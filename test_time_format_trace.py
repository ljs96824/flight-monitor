import unittest
from unittest.mock import patch

from sources.aggregator import FlightAggregator, _flight_departure_time
from sources.juhe_source import JuheSource
from sources.serpapi_source import parse_google_flights
DUAL_SOURCE_PROFILE = {
    "sources": [
        {"name": "juhe", "role": "primary", "weight": 1.0},
        {"name": "hasdata", "role": "cross_check", "weight": 0.6},
    ],
    "query": {},
}




class FetchSource:
    def __init__(self, name, price):
        self.name = name
        self.price = price

    def fetch(self, origin, dest, date_str, cabin_class="economy"):
        hours = [1, 2, 3, 4, 5, 23]
        combos = ["OZ368+OZ112", "AA2", "AA3", "AA4", "AA5", "AA23"]
        flights = []
        for hour, combo in zip(hours, combos):
            clock = f"{hour:02d}:05"
            flights.append(
                {
                    "flight_combo": combo,
                    "flight_no": combo,
                    "departure_airport": origin,
                    "arrival_airport": dest,
                    "departure_time": f"{date_str} {clock}",
                    "arrival_time": f"{date_str} 08:00",
                    "segments": [
                        {
                            "flight_no": combo.split("+")[0],
                            "dep_airport": origin,
                            "arr_airport": dest,
                            "dep_time": f"{date_str} {clock}",
                            "arr_time": f"{date_str} 08:00",
                        }
                    ],
                    "_source_raw_departure_time": (
                        f"{date_str} {hour}:05" if self.name == "hasdata" else clock
                    ),
                    "price": self.price,
                    "data_source": self.name,
                }
            )
        return {"source_status": "success", "flights": flights}


def direct_cached_fetch(source, origin, dest, date_str, passengers, cabin_class, **kwargs):
    return source.fetch(origin, dest, date_str, cabin_class)


class TimeFormatTraceTest(unittest.TestCase):
    def test_trace_display_time_matches_segment_first_rendering(self):
        self.assertEqual(
            _flight_departure_time(
                {
                    "departure_time": "2026-10-01 23:59",
                    "segments": [{"dep_time": "2026-10-01 01:05"}],
                }
            ),
            "2026-10-01 01:05",
        )

    def test_google_parser_preserves_source_departure_time_for_trace(self):
        flights = parse_google_flights(
            {
                "best_flights": [
                    {
                        "price": 1200,
                        "total_duration": 475,
                        "flights": [
                            {
                                "flight_number": "OZ 368",
                                "airline": "Asiana Airlines",
                                "departure_airport": {
                                    "id": "PVG",
                                    "time": "2026-10-01 1:05",
                                },
                                "arrival_airport": {
                                    "id": "ICN",
                                    "time": "2026-10-01 4:05",
                                },
                                "duration": 180,
                            },
                            {
                                "flight_number": "OZ 112",
                                "airline": "Asiana Airlines",
                                "departure_airport": {
                                    "id": "ICN",
                                    "time": "2026-10-01 6:55",
                                },
                                "arrival_airport": {
                                    "id": "KIX",
                                    "time": "2026-10-01 9:00",
                                },
                                "duration": 125,
                            },
                        ],
                    }
                ]
            },
            "hasdata",
            date_str="2026-10-01",
        )

        self.assertEqual(flights[0]["_source_raw_departure_time"], "2026-10-01 1:05")

    def test_juhe_parser_preserves_source_departure_time_for_trace(self):
        flights = JuheSource().normalize(
            [
                {
                    "flightNo": "OZ0368 | OZ0112",
                    "ticketPrice": 1000,
                    "departure": "PVG",
                    "arrival": "KIX",
                    "departureDate": "2026-10-01",
                    "departureTime": "01:05",
                    "arrivalDate": "2026-10-01",
                    "arrivalTime": "09:00",
                    "transferNum": 2,
                }
            ]
        )

        self.assertEqual(flights[0]["_source_raw_departure_time"], "01:05")

    def test_collect_logs_at_most_five_suspicious_dual_source_times(self):
        aggregator = FlightAggregator(
            [FetchSource("hasdata", 1200), FetchSource("juhe", 1000)],
            [],
        )
        with (
            patch("sources.aggregator.cached_fetch", side_effect=direct_cached_fetch),
            patch("sources.aggregator.safe_log") as log,
            patch("sources.aggregator.get_source_profile", return_value=DUAL_SOURCE_PROFILE),
        ):
            result = aggregator.collect(
                "PVG",
                "HKG",
                "2026-10-01",
                route_type="greater_china",
            )

        messages = [call.args[0] for call in log.call_args_list if call.args]
        checks = [message for message in messages if message.startswith("[时刻核对]")]
        self.assertEqual(len(checks), 5)
        self.assertEqual(
            checks[0],
            "[时刻核对] combo=OZ368+OZ112 juhe原始=01:05 "
            "hasdata原始=01:05 入池显示=01:05",
        )
        self.assertTrue(
            all("_source_raw_departure_time" not in flight for flight in result["flights"])
        )


if __name__ == "__main__":
    unittest.main()
