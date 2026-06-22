import sys
import tempfile
import types
import unittest
from pathlib import Path


sys.modules.setdefault("httpx", types.SimpleNamespace(get=lambda *args, **kwargs: None, post=lambda *args, **kwargs: None))

class CurrentFixesRegressionTest(unittest.TestCase):
    def test_aircraft_mapping_translates_787_variant_codes(self):
        from domestic_fare_rules import get_aircraft_name

        self.assertEqual(get_aircraft_name("78A"), "\u6ce2\u97f3787-8")
        self.assertEqual(get_aircraft_name("78B"), "\u6ce2\u97f3787-9")
        self.assertEqual(get_aircraft_name("78C"), "\u6ce2\u97f3787-10")
        self.assertEqual(get_aircraft_name("78J"), "\u6ce2\u97f3787-10")
        self.assertEqual(get_aircraft_name("38A"), "\u7a7a\u5ba2A380")
        self.assertEqual(get_aircraft_name("35A"), "\u7a7a\u5ba2A350")
        self.assertEqual(get_aircraft_name("35B"), "\u7a7a\u5ba2A350-1000")
        self.assertEqual(get_aircraft_name("32B"), "\u7a7a\u5ba2A321")
        self.assertEqual(get_aircraft_name("33A"), "\u7a7a\u5ba2A330")
    def test_roundtrip_calendar_body_displays_all_passenger_reference_price(self):
        from notifier import _email_price_calendar_body

        payload = {
            "is_roundtrip": True,
            "passenger_pricing": {
                "factor": 2.5,
                "passenger_label": "2\u6210\u4eba+1\u513f\u7ae5",
                "passenger_count": 3,
                "passengers": {"adult": 2, "child": 1, "elderly": 0, "infant": 0},
            },
            "price_calendar": {
                "scope": "roundtrip",
                "return_date": "2026-06-30",
                "return_min_price": 557,
                "rows": [
                    {
                        "date": "2026-06-23",
                        "weekday": "\u5468\u4e8c",
                        "outbound_min_price": 547,
                        "return_min_price": 557,
                        "min_price": 1104,
                        "lowest": True,
                    },
                    {
                        "date": "2026-06-26",
                        "weekday": "\u5468\u4e94",
                        "outbound_min_price": 679,
                        "return_min_price": 557,
                        "min_price": 1236,
                        "selected": True,
                    },
                ],
            },
        }

        body = _email_price_calendar_body(payload)

        self.assertIn("3\u4eba(2\u6210\u4eba+1\u513f\u7ae5)\u5f80\u8fd4\u53c2\u8003\u4ef7", body)
        self.assertIn("\u00a52,760", body)
        self.assertIn("\u5355\u4eba\u5f80\u8fd4\u00a51,104\u00d72.5", body)
        self.assertIn("\u00a53,090", body)
        self.assertIn("\u5355\u4eba\u5f80\u8fd4\u00a51,236\u00d72.5", body)

    def test_roundtrip_missing_combo_reports_collection_confidence(self):
        from plan_tracker import save_pushed_plans, track_plan_status

        with tempfile.TemporaryDirectory() as tmp:
            data_dir = Path(tmp)
            save_pushed_plans(
                "sub-rt",
                [
                    {
                        "label": "\u65b9\u6848A",
                        "is_roundtrip": True,
                        "outbound_flight": {"flight_no": "MU5099", "price": 1410},
                        "return_flight": {"flight_no": "CA1589", "price": 1350},
                        "roundtrip_price": 2760,
                    }
                ],
                data_dir=data_dir,
            )

            many_flights = [{"flight_no": f"MU{i}", "price": 1000 + i} for i in range(12)]
            status = track_plan_status("sub-rt", many_flights, data_dir=data_dir)

        self.assertEqual(status["status"], "unavailable")
        self.assertEqual(status["confidence"], "medium")
        self.assertIn("\u672c\u6b21\u8be5\u822a\u7ebf\u91c7\u96c6\u523012\u4e2a\u822a\u73ed", status["msg"])
        self.assertIn("\u53ef\u80fd\u5df2\u552e\u7f44\u6216\u505c\u98de", status["msg"])

    def test_roundtrip_missing_combo_reports_low_coverage_when_collection_sparse(self):
        from plan_tracker import save_pushed_plans, track_plan_status

        with tempfile.TemporaryDirectory() as tmp:
            data_dir = Path(tmp)
            save_pushed_plans(
                "sub-rt",
                [
                    {
                        "label": "\u65b9\u6848A",
                        "is_roundtrip": True,
                        "outbound_flight": {"flight_no": "MU5099", "price": 1410},
                        "return_flight": {"flight_no": "CA1589", "price": 1350},
                        "roundtrip_price": 2760,
                    }
                ],
                data_dir=data_dir,
            )

            status = track_plan_status(
                "sub-rt",
                [{"flight_no": "MU5101", "price": 1448, "source_status": "cache"}],
                data_dir=data_dir,
            )

        self.assertEqual(status["status"], "coverage_uncertain")
        self.assertEqual(status["confidence"], "low")
        self.assertIn("\u91c7\u96c6\u8986\u76d6\u53ef\u80fd\u4e0d\u5b8c\u6574", status["msg"])
        self.assertIn("\u4e0b\u6b21\u91c7\u96c6\u518d\u786e\u8ba4", status["msg"])
        self.assertIn("\u4f7f\u7528\u7f13\u5b58\u6570\u636e", status["msg"])


if __name__ == "__main__":
    unittest.main()


