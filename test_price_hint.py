import tempfile
import sys
import types
import unittest
import logging
from pathlib import Path
from unittest.mock import patch

from analyzer import build_price_hint_from_calendar
from price_calendar import save_calendar


sys.modules.setdefault(
    "httpx",
    types.SimpleNamespace(post=lambda *args, **kwargs: None),
)
logging.basicConfig = lambda *args, **kwargs: None

from main import price_hint_for_route
import web_form


class PriceHintTest(unittest.TestCase):
    def test_build_price_hint_from_calendar_summarizes_range_and_median(self):
        hint = build_price_hint_from_calendar(
            {
                "route": "PVG-PEK",
                "dates": {
                    "2026-06-10": {"min_price": 680},
                    "2026-06-11": {"min_price": 520},
                    "2026-06-12": {"min_price": 1080},
                },
            }
        )

        self.assertTrue(hint["has_data"])
        self.assertEqual(hint["low"], 520)
        self.assertEqual(hint["high"], 1080)
        self.assertEqual(hint["typical"], 680)
        self.assertEqual(hint["sample_count"], 3)
        self.assertEqual(hint["scope"], "oneway")

    def test_price_hint_for_route_reads_price_calendar_by_route(self):
        with tempfile.TemporaryDirectory() as tmp:
            data_dir = Path(tmp)
            save_calendar(
                "PVG-PEK",
                {
                    "route": "PVG-PEK",
                    "dates": {
                        "2026-06-10": {"min_price": 620},
                        "2026-06-11": {"min_price": 1080},
                    },
                },
                data_dir=data_dir,
            )

            hint = price_hint_for_route("PVG", "PEK", data_dir=data_dir)

        self.assertTrue(hint["has_data"])
        self.assertEqual(hint["low"], 620)
        self.assertEqual(hint["high"], 1080)
        self.assertEqual(hint["route"], "PVG-PEK")


class PriceHintRouteContractTest(unittest.TestCase):
    def _get_price_hint(self, query, *, calendar):
        with tempfile.TemporaryDirectory() as tmp:
            data_dir = Path(tmp)
            with (
                patch.object(web_form, "SUBSCRIPTIONS_PATH", data_dir / "subscriptions.json"),
                patch.object(web_form, "FEEDBACK_PATH", data_dir / "feedback.json"),
                patch.object(web_form, "PAGE_PAYLOADS_DIR", data_dir / "payloads"),
                patch.object(web_form, "load_calendar", return_value=calendar) as load_calendar,
                patch.object(web_form, "start_background_collection") as start_collection,
                patch("sqlite3.connect") as sqlite_connect,
                patch("socket.socket.connect") as socket_connect,
            ):
                response = web_form.app.test_client().get(
                    "/price_hint",
                    query_string=query,
                )
                files_after = tuple(data_dir.iterdir())

        start_collection.assert_not_called()
        sqlite_connect.assert_not_called()
        socket_connect.assert_not_called()
        self.assertEqual(files_after, ())
        return response, load_calendar

    def test_unknown_location_returns_unclassified_no_data_without_calendar_read(self):
        response, load_calendar = self._get_price_hint(
            {"origin": "not-a-location", "dest": "PEK"},
            calendar={},
        )

        self.assertEqual(response.status_code, 200)
        self.assertEqual(
            response.get_json(),
            {
                "has_data": False,
                "scope": "oneway",
                "route_type": "",
                "route_type_label": "待识别",
            },
        )
        load_calendar.assert_not_called()

    def test_valid_route_without_data_returns_route_metadata_and_no_amount_contract(self):
        response, load_calendar = self._get_price_hint(
            {"origin": "PVG", "dest": "PEK"},
            calendar={},
        )

        self.assertEqual(response.status_code, 200)
        payload = response.get_json()
        self.assertFalse(payload["has_data"])
        self.assertEqual(payload["scope"], "oneway")
        self.assertEqual(payload["route_type"], "domestic")
        self.assertEqual(payload["route_type_label"], "国内")
        self.assertGreater(load_calendar.call_count, 0)

    def test_valid_route_with_synthetic_data_returns_price_range_contract(self):
        response, load_calendar = self._get_price_hint(
            {"origin": "PVG", "dest": "PEK"},
            calendar={
                "route": "PVG-PEK",
                "dates": {
                    "2026-06-10": {"min_price": 520},
                    "2026-06-11": {"min_price": 680},
                    "2026-06-12": {"min_price": 1080},
                },
            },
        )

        self.assertEqual(response.status_code, 200)
        self.assertEqual(
            response.get_json(),
            {
                "has_data": True,
                "high": 1080,
                "low": 520,
                "route": "PVG-PEK",
                "route_type": "domestic",
                "route_type_label": "国内",
                "sample_count": 3,
                "scope": "oneway",
                "typical": 680,
            },
        )
        self.assertEqual(load_calendar.call_count, 1)


if __name__ == "__main__":
    unittest.main()
