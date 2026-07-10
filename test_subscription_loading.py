import json
import logging
import sys
import tempfile
import types
import unittest
from pathlib import Path
from unittest.mock import patch

sys.modules.setdefault("dotenv", types.SimpleNamespace(load_dotenv=lambda *a, **k: None))
sys.modules.setdefault(
    "httpx",
    types.SimpleNamespace(get=lambda *a, **k: None, post=lambda *a, **k: None),
)
logging.basicConfig = lambda *a, **k: None

import main


class SubscriptionLoadingTest(unittest.TestCase):
    def test_collect_for_airport_matrix_filters_to_requested_active_airports(self):
        class FakeAggregator:
            def collect(self, origin, destination, date_str, cabin_classes=None):
                return {
                    "flights": [
                        {
                            "flight_combo": "ACTIVE",
                            "price": 680,
                            "departure_airport": origin,
                            "arrival_airport": destination,
                        },
                        {
                            "flight_combo": "INACTIVE_DEST",
                            "price": 500,
                            "departure_airport": origin,
                            "arrival_airport": "PKX",
                        },
                    ],
                    "source_stats": {},
                }

        data = main.collect_for_airport_matrix(
            FakeAggregator(),
            ["PVG"],
            ["PEK"],
            "2026-06-10",
        )

        self.assertEqual([flight["flight_combo"] for flight in data["flights"]], ["ACTIVE"])

    def test_collect_for_airport_matrix_preserves_dual_source_price_anomalies(self):
        class FakeAggregator:
            def collect(self, origin, destination, date_str, cabin_classes=None):
                combo = f"{origin}{destination}"
                return {
                    "flights": [
                        {
                            "flight_combo": combo,
                            "price": 680,
                            "departure_airport": origin,
                            "arrival_airport": destination,
                        }
                    ],
                    "dual_source_price_anomalies": [
                        {
                            "flight_combo": combo,
                            "diff_pct": 20,
                            "sources": [
                                {"source": "hasdata", "price": 850},
                                {"source": "juhe", "price": 680},
                            ],
                        }
                    ],
                    "source_stats": {},
                }

        data = main.collect_for_airport_matrix(
            FakeAggregator(),
            ["PVG", "SHA"],
            ["KIX"],
            "2026-10-01",
        )

        self.assertEqual(len(data["dual_source_price_anomalies"]), 2)
        self.assertEqual(
            {item["flight_combo"] for item in data["dual_source_price_anomalies"]},
            {"PVGKIX", "SHAKIX"},
        )

    def test_bad_subscription_is_skipped_without_stopping_batch(self):
        records = [
            {
                "id": "bad-location",
                "origin": "上海",
                "destination": "重庆",
                "depart_date": "2026-10-01",
                "status": "active",
            },
            {
                "id": "good-osaka",
                "origin": "上海",
                "destination": "大阪",
                "depart_date": "2026-10-01",
                "status": "active",
            },
        ]

        original_path = main.SUBSCRIPTIONS_PATH
        with tempfile.TemporaryDirectory() as tmpdir:
            path = Path(tmpdir) / "subscriptions.json"
            path.write_text(json.dumps(records, ensure_ascii=False), encoding="utf-8")
            main.SUBSCRIPTIONS_PATH = path
            try:
                loaded = main.load_file_subscriptions()
            finally:
                main.SUBSCRIPTIONS_PATH = original_path

        self.assertEqual(len(loaded), 1)
        self.assertEqual(loaded[0]["id"], "good-osaka")
        self.assertEqual(loaded[0]["destination"], "大阪")
        self.assertEqual(loaded[0]["destination_airports"], ["KIX", "ITM"])

    def test_unknown_city_inside_basic_is_reported_and_skipped(self):
        records = [
            {
                "id": "bad-basic-location",
                "basic": {
                    "origin": "上海",
                    "destination": "不存在城市",
                    "depart_date": "2026-10-01",
                },
                "status": "active",
            }
        ]

        original_path = main.SUBSCRIPTIONS_PATH
        with tempfile.TemporaryDirectory() as tmpdir:
            path = Path(tmpdir) / "subscriptions.json"
            path.write_text(json.dumps(records, ensure_ascii=False), encoding="utf-8")
            main.SUBSCRIPTIONS_PATH = path
            try:
                with patch("builtins.print") as fake_print:
                    loaded = main.load_file_subscriptions()
            finally:
                main.SUBSCRIPTIONS_PATH = original_path

        self.assertEqual(loaded, [])
        printed = "\n".join(str(call.args[0]) for call in fake_print.call_args_list if call.args)
        self.assertIn("bad-basic-location", printed)
        self.assertIn("无法识别目的地 不存在城市", printed)


    def test_paused_subscription_is_skipped_with_log(self):
        records = [
            {
                "id": "paused-sub",
                "origin": "PVG",
                "destination": "PEK",
                "depart_date": "2026-06-10",
                "status": "paused",
            },
            {
                "id": "active-sub",
                "origin": "PVG",
                "destination": "PEK",
                "depart_date": "2026-06-10",
                "status": "active",
            },
        ]

        original_path = main.SUBSCRIPTIONS_PATH
        with tempfile.TemporaryDirectory() as tmpdir:
            path = Path(tmpdir) / "subscriptions.json"
            path.write_text(json.dumps(records, ensure_ascii=False), encoding="utf-8")
            main.SUBSCRIPTIONS_PATH = path
            try:
                with patch("builtins.print") as fake_print:
                    loaded = main.load_file_subscriptions()
            finally:
                main.SUBSCRIPTIONS_PATH = original_path

        self.assertEqual([sub["id"] for sub in loaded], ["active-sub"])
        printed = "\n".join(str(call.args[0]) for call in fake_print.call_args_list if call.args)
        self.assertIn("[跳过] 订阅已暂停", printed)
        self.assertIn("paused-sub", printed)

    def test_same_day_subscription_defaults_to_roundtrip_same_return_date(self):
        normalized = main._normalize_subscription(
            {
                "id": "same-day-business",
                "origin": "PVG",
                "destination": "PEK",
                "depart_date": "2026-06-19",
                "status": "active",
                "constraints": {
                    "same_day_round_trip": True,
                    "business_start": "10:00",
                    "business_end": "17:00",
                },
            }
        )

        self.assertTrue(normalized["round_trip"])
        self.assertEqual(normalized["return_date"], "2026-06-19")
        self.assertTrue(normalized["hard_constraints"]["same_day_round_trip"])
    def test_subscription_preferences_include_travel_scenarios(self):
        prefs = main.subscription_preferences(
            {
                "soft_preferences": {
                    "travel_scenarios": ["tourism", "family"],
                    "travel_scenario": "tourism",
                },
                "companions": "solo",
            }
        )

        self.assertEqual(prefs["travel_scenarios"], ["tourism", "family"])
        self.assertEqual(prefs["travel_scenario"], "tourism")

    def test_normalized_subscription_preserves_canonical_passenger_fields(self):
        normalized = main._normalize_subscription(
            {
                "id": "family-trip",
                "origin": "上海",
                "destination": "大阪",
                "depart_date": "2026-10-01",
                "status": "active",
                "basic": {
                    "passenger_count": 5,
                },
                "preferences": {
                    "passengers": {"adult": 2, "child": 1, "elderly": 2, "infant": 0},
                    "passenger_count": 5,
                    "travel_purposes": ["tourism", "family"],
                },
                "soft_preferences": {
                    "travel_scenarios": ["tourism", "family"],
                },
            }
        )

        self.assertEqual(normalized["basic"]["passenger_count"], 5)
        self.assertEqual(
            normalized["preferences"]["passengers"],
            {"adult": 2, "child": 1, "elderly": 2, "infant": 0},
        )
        self.assertEqual(normalized["soft_preferences"]["passengers"]["elderly"], 2)

    def test_deliver_notification_ignores_persist_failure_after_send_success(self):
        payload = {"push_type": "低价提醒", "route": "上海 → 北京", "recommended_plans": []}
        with patch.object(main, "build_notification_payload", return_value=payload), \
             patch.object(main, "render_email", return_value=("subject", "<html></html>", {})), \
             patch.object(main, "render_detail_html", return_value="<html></html>"), \
             patch.object(main, "_save_result_for_page"), \
             patch.object(main, "render_pushplus", return_value="<b>push</b>"), \
             patch.object(main, "send", return_value=True), \
             patch.object(main, "persist_notification_payload", side_effect=OSError("bad filename")):
            ok = main._deliver_notification(
                {
                    "id": "上海|北京 会议",
                    "notification_goals": {"method": "pushplus"},
                    "depart_date": "2026-06-10",
                },
                "上海-北京",
                {"analysis_result": {}, "route_info": {}},
            )

        self.assertTrue(ok)


if __name__ == "__main__":
    unittest.main()
