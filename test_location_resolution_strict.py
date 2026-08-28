import importlib.util
import json
import logging
import sys
import tempfile
import types
import unittest
from datetime import date
from pathlib import Path
from unittest.mock import patch


sys.modules.setdefault("dotenv", types.SimpleNamespace(load_dotenv=lambda *a, **k: None))
sys.modules.setdefault(
    "httpx",
    types.SimpleNamespace(get=lambda *a, **k: None, post=lambda *a, **k: None),
)
logging.basicConfig = lambda *a, **k: None

import airports
import main
import web_form
from web_test_utils import enable_csrf


ROOT = Path(__file__).resolve().parent


class _Form(dict):
    def getlist(self, key):
        value = self.get(key)
        if value is None:
            return []
        return value if isinstance(value, list) else [value]


def _minimal_form(destination: str) -> _Form:
    return _Form(
        {
            "monitor_mode": "quick",
            "round_trip": "false",
            "origin_select": "上海",
            "destination": destination,
            "depart_date": "2026-10-01",
            "price_strategy": "auto_judge",
            "travel_scenario": ["tourism"],
            "transfer_policy": "reasonable",
            "baggage": "not_required",
            "primary_goal": "buy_timing",
            "notification_method": "page_only",
            "notification_frequency": "important_only",
        }
    )


class StrictLocationResolverTest(unittest.TestCase):
    def test_partial_pinyin_and_unknown_iata_are_not_silently_resolved(self):
        for value in ("北", "bei", "beij", "BWI", "ZZZ"):
            with self.subTest(value=value):
                result = airports.resolve_location(value)
                self.assertEqual(result["type"], "unknown")
                self.assertEqual(result["airports"], [])

    def test_unknown_chinese_location_returns_objective_candidates(self):
        result = airports.resolve_location("北")

        self.assertIn("candidates", result)
        self.assertIn("北京", [item["value"] for item in result["candidates"]])
        self.assertLessEqual(len(result["candidates"]), 5)

    def test_curated_alias_remains_the_only_automatic_correction(self):
        with patch("airports.safe_log", create=True) as log:
            result = airports.resolve_location("大版")

        self.assertEqual(result["value"], "大阪")
        self.assertEqual(result["airports"], ["KIX", "ITM"])
        self.assertTrue(
            any("[地点纠错] 大版 -> 大阪" in str(call.args[0]) for call in log.call_args_list)
        )

    def test_timezone_labels_all_come_from_the_single_mapping(self):
        labels = getattr(airports, "TIMEZONE_LABELS")
        iana_by_airport = getattr(airports, "AIRPORT_IANA_TIMEZONE")

        self.assertEqual(set(iana_by_airport), set(airports.AIRPORTS))
        for code, iana_name in iana_by_airport.items():
            with self.subTest(code=code):
                self.assertIn(iana_name, labels)
                self.assertEqual(airports.get_airport_timezone(code), labels[iana_name])
        self.assertEqual(iana_by_airport["ABQ"], "America/Denver")
        self.assertEqual(airports.get_airport_timezone("ABQ"), "美山")


class WebLocationValidationTest(unittest.TestCase):
    def test_unknown_location_is_rejected_with_candidates_before_save(self):
        with self.assertRaisesRegex(ValueError, "无法识别目的地'北'.*北京"):
            web_form.build_subscription(_minimal_form("北"))

    def test_unknown_location_post_does_not_create_subscription(self):
        with (
            web_form.app.test_client() as client,
            patch("web_form._subscription_repository") as repository_factory,
            patch("web_form.start_background_collection") as start_collection,
        ):
            enable_csrf(client)
            response = client.post("/subscribe", data=_minimal_form("北"))

        self.assertEqual(response.status_code, 400)
        self.assertIn("无法识别目的地", response.get_data(as_text=True))
        self.assertIn("北京", response.get_data(as_text=True))
        repository_factory.assert_not_called()
        start_collection.assert_not_called()

    def test_price_hint_rejects_partial_or_unknown_input_without_reading_calendar(self):
        with web_form.app.test_client() as client, patch("web_form.load_calendar") as load_calendar:
            for dest in ("北", "bei", "beij", "BWI", "ZZZ"):
                with self.subTest(dest=dest):
                    response = client.get(
                        "/price_hint",
                        query_string={"origin": "SHA", "dest": dest, "date": "2026-10-01"},
                    )
                    self.assertEqual(response.status_code, 200)
                    self.assertFalse(response.get_json()["has_data"])

        load_calendar.assert_not_called()

    def test_typing_beijing_only_queries_exact_beijing_airports(self):
        with (
            web_form.app.test_client() as client,
            patch("web_form.load_calendar", return_value={}) as load_calendar,
        ):
            for dest in ("北", "bei", "beij", "北京"):
                response = client.get(
                    "/price_hint",
                    query_string={"origin": "SHA", "dest": dest, "date": "2026-10-01"},
                )
                self.assertEqual(response.status_code, 200)

        queried_routes = [call.args[0] for call in load_calendar.call_args_list]
        self.assertTrue(queried_routes)
        self.assertTrue(all("BWI" not in route for route in queried_routes))
        self.assertEqual(
            set(queried_routes),
            {
                "SHA-PEK",
                "SHA_PEK",
                "SHA→PEK",
                "SHA-PKX",
                "SHA_PKX",
                "SHA→PKX",
            },
        )

    def test_price_hint_exact_iata_reads_local_calendar_only(self):
        local_calendar = {
            "route": "SHA-PEK",
            "dates": {"2026-10-01": {"min_price": 620}},
        }
        with (
            web_form.app.test_client() as client,
            patch("web_form.load_calendar", return_value=local_calendar) as load_calendar,
            patch("price_calendar.cached_fetch", side_effect=AssertionError("price_hint 禁止调用外部源")),
        ):
            response = client.get(
                "/price_hint",
                query_string={"origin": "SHA", "dest": "PEK", "date": "2026-10-01"},
            )

        self.assertTrue(response.get_json()["has_data"])
        self.assertGreaterEqual(load_calendar.call_count, 1)

    def test_main_price_hint_rejects_partial_input_before_cache_read(self):
        with patch("main.load_calendar") as load_calendar:
            hint = main.price_hint_for_route("SHA", "beij")

        self.assertFalse(hint["has_data"])
        load_calendar.assert_not_called()

    def test_template_validates_iata_against_airport_dictionary(self):
        self.assertIn("const airportCodes = new Set", web_form.FORM_TEMPLATE)
        self.assertIn("airportCodes.has(upper)", web_form.FORM_TEMPLATE)
        self.assertIn("function suggestions", web_form.FORM_TEMPLATE)
        self.assertIn("if (query.length < 2) return []", web_form.FORM_TEMPLATE)


class InvalidSubscriptionPreflightTest(unittest.TestCase):
    def test_normalization_rederives_airports_from_location_truth(self):
        normalized = main.normalize_subscription(
            {
                "origin": "上海",
                "destination": "北京",
                "origin_airports": ["BWI"],
                "origin_airports_active": ["BWI", "SHA"],
                "destination_airports": ["ABQ"],
                "destination_airports_active": ["ABQ", "PKX"],
                "depart_date": "2026-10-01",
            }
        )

        self.assertEqual(normalized["origin_airports"], ["PVG", "SHA"])
        self.assertEqual(normalized["origin_airports_active"], ["SHA"])
        self.assertEqual(normalized["destination_airports"], ["PEK", "PKX"])
        self.assertEqual(normalized["destination_airports_active"], ["PKX"])

    def test_unresolvable_synced_subscription_is_marked_invalid_and_skipped(self):
        normalized = main.normalize_subscription(
            {
                "_index": 12,
                "name": "坏地点订阅",
                "origin": "上海",
                "destination": "不存在城市",
                "depart_date": "2026-10-01",
            }
        )

        self.assertEqual(normalized["status"], "invalid")
        self.assertEqual(normalized["validation_status"], "invalid")
        self.assertIn("地点无法解析", normalized["invalid_reason"])
        self.assertIn("输入=不存在城市", normalized["invalid_reason"])

        result = main.evaluate_subscription_preflight(normalized, today=date(2026, 7, 22))
        self.assertTrue(result["skip"])
        self.assertEqual(result["reason_code"], "invalid_location")

        with (
            patch("main.set_current_round") as set_round,
            patch("main.start_request_cache_round") as start_round,
            patch("main.safe_log") as log,
        ):
            ok = main.process_subscription(normalized, ensure_db=False)

        self.assertTrue(ok)
        set_round.assert_not_called()
        start_round.assert_not_called()
        self.assertTrue(
            any(
                "原因=地点无法解析(输入=不存在城市)" in str(call.args[0])
                for call in log.call_args_list
            )
        )
        self.assertTrue(
            any(
                "[订阅前置校验] 本轮检查=1 跳过=1" in str(call.args[0])
                for call in log.call_args_list
            )
        )

    def test_unresolvable_list_script_is_read_only(self):
        script_path = ROOT / "scripts" / "list_unresolvable_subs.py"
        self.assertTrue(script_path.exists(), "缺少只读清单脚本")
        spec = importlib.util.spec_from_file_location("list_unresolvable_subs", script_path)
        module = importlib.util.module_from_spec(spec)
        assert spec.loader is not None
        spec.loader.exec_module(module)

        payload = [
            {"name": "正常", "origin": "PVG", "destination": "KIX"},
            {"name": "坏地点", "origin": "上海", "destination": "不存在城市"},
        ]
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "subscriptions.json"
            path.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
            before = path.read_bytes()
            rows = module.list_unresolvable(path)
            after = path.read_bytes()

        self.assertEqual(before, after)
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["name"], "坏地点")
        self.assertIn("不存在城市", rows[0]["reason"])


if __name__ == "__main__":
    unittest.main()
