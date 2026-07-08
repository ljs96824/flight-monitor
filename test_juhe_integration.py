import os
import sys
import types
import unittest
from datetime import date, timedelta
from unittest.mock import patch

from sources.aggregator import FlightAggregator, build_default_sources, is_domestic_route
from sources.juhe_source import JuheSource


class DummySource:
    def __init__(self, name):
        self.name = name


class JuheIntegrationTest(unittest.TestCase):
    def test_domestic_route_recognizes_pvg_to_pek(self):
        self.assertTrue(is_domestic_route("PVG", "PEK"))
        self.assertTrue(is_domestic_route("sha", "pkx"))
        self.assertFalse(is_domestic_route("PVG", "KIX"))

    def test_build_default_sources_registers_juhe_when_key_exists(self):
        with patch.dict(os.environ, {"JUHE_FLIGHT_KEY": "test-key"}, clear=True):
            search_sources, _ = build_default_sources()

        self.assertIn("juhe", [source.name for source in search_sources])

    def test_domestic_route_orders_juhe_first(self):
        aggregator = FlightAggregator(
            [DummySource("serpapi"), DummySource("juhe"), DummySource("hasdata")],
            [],
        )

        ordered = aggregator._ordered_search_sources("PVG", "PEK")

        self.assertEqual([source.name for source in ordered], ["juhe"])

    def test_juhe_parse_reads_result_flight_info_and_ticket_price(self):
        source = JuheSource()
        raw = {
            "result": {
                "flightInfo": [
                    {
                        "flightNo": "KN5978",
                        "airline": "KN",
                        "airlineName": "中国联合航空公司",
                        "equipment": "73U",
                        "departure": "PVG",
                        "arrival": "PKX",
                        "departureName": "上海浦东",
                        "arrivalName": "北京大兴",
                        "departureDate": "2026-06-10",
                        "departureTime": "08:15",
                        "arrivalDate": "2026-06-10",
                        "arrivalTime": "10:25",
                        "duration": "02h10m",
                        "transferNum": 1,
                        "ticketPrice": 527,
                        "isCodeShare": False,
                        "segments": [],
                    },
                    {
                        "flightNo": "HO7715",
                        "airline": "HO",
                        "airlineName": "吉祥航空",
                        "equipment": "321",
                        "departure": "PVG",
                        "arrival": "PKX",
                        "departureDate": "2026-06-10",
                        "departureTime": "09:00",
                        "arrivalDate": "2026-06-10",
                        "arrivalTime": "11:20",
                        "transferNum": 1,
                        "ticketPrice": 499,
                        "isCodeShare": True,
                    },
                ]
            }
        }

        flights = source.normalize(source.parse(raw), collected_at="2026-06-06T12:00:00")

        self.assertEqual(len(flights), 1)
        flight = flights[0]
        self.assertEqual(flight["flight_no"], "KN5978")
        self.assertEqual(flight["price"], 527)
        self.assertEqual(flight["airline"], "KN")
        self.assertEqual(flight["airline_name"], "中国联合航空公司")
        self.assertEqual(flight["aircraft"], "波音737")
        self.assertEqual(flight["aircraft_code"], "73U")
        self.assertEqual(flight["departure_airport"], "PVG")
        self.assertEqual(flight["arrival_airport"], "PKX")
        self.assertEqual(flight["departure_time"], "2026-06-10 08:15")
        self.assertEqual(flight["arrival_time"], "2026-06-10 10:25")
        self.assertEqual(flight["duration_str"], "02h10m")
        self.assertEqual(flight["stops"], 0)
        self.assertFalse(flight["is_codeshare"])
        self.assertEqual(flight["data_source"], "juhe")
        self.assertTrue(flight["domestic_realtime_quote"])
        self.assertEqual(flight["price_note"], "票面价，实付以支付页为准")
        self.assertEqual(flight["segments"][0]["aircraft"], "波音737")

    def test_juhe_request_params_use_documented_departure_date_fields(self):
        source = JuheSource()

        params = source.build_request_params("PVG", "PEK", "2026-06-17", "test-key")

        self.assertEqual(params["departure"], "PVG")
        self.assertEqual(params["arrival"], "PEK")
        self.assertEqual(params["departureDate"], "2026-06-17")
        self.assertNotIn("fromCity", params)
        self.assertNotIn("toCity", params)
        self.assertNotIn("date", params)

    def test_juhe_fetch_skips_past_dates_before_request(self):
        source = JuheSource()
        past = (date.today() - timedelta(days=1)).isoformat()

        with patch.dict(os.environ, {"JUHE_FLIGHT_KEY": "test-key"}, clear=True):
            result = source.fetch("PVG", "PEK", past)

        self.assertEqual(result["flights"], [])
        self.assertEqual(result["source_status"], "skipped_past_date")
        self.assertEqual(result["skipped_reason"], "过去日期不可售")

    def test_juhe_fetch_marks_281801_as_invalid_date(self):
        source = JuheSource()
        future = (date.today() + timedelta(days=5)).isoformat()

        class FakeResponse:
            status_code = 200
            text = '{"error_code":281801}'

            def raise_for_status(self):
                return None

            def json(self):
                return {"error_code": 281801, "reason": "行程出发日期格式不正确或为空"}

        calls = []

        def fake_get(*args, **kwargs):
            calls.append((args, kwargs))
            return FakeResponse()

        fake_requests = types.SimpleNamespace(get=fake_get)
        with patch.dict(os.environ, {"JUHE_FLIGHT_KEY": "test-key"}, clear=True):
            with patch.dict(sys.modules, {"requests": fake_requests}):
                result = source.fetch("PVG", "PEK", future)

        self.assertEqual(len(calls), 1)
        self.assertEqual(result["flights"], [])
        self.assertEqual(result["source_status"], "invalid_date")
        self.assertEqual(result["skipped_reason"], "日期无效或已过期")


if __name__ == "__main__":
    unittest.main()
