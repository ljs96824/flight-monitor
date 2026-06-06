import unittest
import sys
import types

sys.modules.setdefault(
    "httpx",
    types.SimpleNamespace(get=lambda *args, **kwargs: None, post=lambda *args, **kwargs: None),
)

from analyzer import make_domestic_tags, verify_fare_rules
from domestic_fare_rules import (
    get_domestic_baggage,
    get_domestic_refund,
    standardize_domestic_fare_rules,
)
from notifier import _flight_status_tags, _pushplus_baggage_line_for_flight


class DomesticFareRulesTest(unittest.TestCase):
    def test_full_service_airline_includes_standard_checked_baggage(self):
        baggage = get_domestic_baggage("CA")

        self.assertTrue(baggage["included"])
        self.assertEqual(baggage["checked_kg"], 20)
        self.assertEqual(baggage["checked_pieces"], 1)
        self.assertEqual(baggage["level"], "标准")

    def test_low_cost_airline_marks_checked_baggage_as_extra(self):
        baggage = get_domestic_baggage("KN")

        self.assertFalse(baggage["included"])
        self.assertEqual(baggage["checked_kg"], 0)
        self.assertEqual(baggage["checked_pieces"], 0)
        self.assertEqual(baggage["level"], "需加购")

    def test_domestic_refund_uses_cabin_code_flexibility(self):
        self.assertEqual(get_domestic_refund("Y")["level"], "高")
        self.assertEqual(get_domestic_refund("K")["level"], "中")
        self.assertEqual(get_domestic_refund("Z")["level"], "低")

    def test_verify_fare_rules_standardizes_juhe_domestic_low_cost_flight(self):
        flight = {
            "flight_no": "KN5978",
            "flight_combo": "KN5978",
            "airline": "KN",
            "cabin_code": "Z",
            "data_source": "juhe",
            "route_type": "domestic",
            "stops": 0,
            "segments": [
                {
                    "flight_no": "KN5978",
                    "dep_airport": "PVG",
                    "arr_airport": "PKX",
                    "dep_time": "2026-06-10T08:15",
                    "arr_time": "2026-06-10T10:25",
                }
            ],
        }

        verification = verify_fare_rules(flight, {"baggage": "required"})

        self.assertEqual(flight["fare_rules"]["source"], "国内标准规则推断")
        self.assertFalse(flight["fare_rules"]["baggage"]["included"])
        self.assertIn("不含免费托运行李", " ".join(verification["issues"]))
        self.assertEqual(flight["fare_rules"]["refund"]["level"], "低")

    def test_make_domestic_tags_reflects_scene_and_airline_type(self):
        full_service = {
            "flight_no": "CA1234",
            "airline": "CA",
            "cabin_code": "Y",
            "stops": 0,
            "price": 680,
            "segments": [{"dep_time": "2026-06-10T09:30"}],
            "data_source": "juhe",
            "route_type": "domestic",
        }
        full_service["fare_rules"] = standardize_domestic_fare_rules(full_service)
        lcc = {
            "flight_no": "KN5978",
            "airline": "KN",
            "cabin_code": "Z",
            "stops": 0,
            "price": 527,
            "segments": [{"dep_time": "2026-06-10T08:15"}],
            "data_source": "juhe",
            "route_type": "domestic",
        }
        lcc["fare_rules"] = standardize_domestic_fare_rules(lcc)

        business_tags = make_domestic_tags(full_service, {"time": "high", "risk_averse": "high"})
        price_tags = make_domestic_tags(lcc, {"price": "high"})

        self.assertIn("商务友好", business_tags)
        self.assertIn("低风险", business_tags)
        self.assertIn("廉航低价", price_tags)
        self.assertIn("少折腾", price_tags)

    def test_notifier_uses_domestic_tags_and_baggage_line(self):
        flight = {
            "flight_no": "KN5978",
            "airline": "KN",
            "cabin_code": "Z",
            "stops": 0,
            "price": 527,
            "data_source": "juhe",
            "route_type": "domestic",
            "segments": [{"dep_time": "2026-06-10T08:15"}],
        }
        flight["fare_rules"] = standardize_domestic_fare_rules(flight)
        flight["domestic_tags"] = make_domestic_tags(flight, {"price": "high"}, 527)

        self.assertIn("廉航低价", _flight_status_tags(flight))
        self.assertIn("需另购", _pushplus_baggage_line_for_flight(flight))


if __name__ == "__main__":
    unittest.main()
