import tempfile
import sys
import types
import unittest
import logging
from pathlib import Path

from analyzer import build_price_hint_from_calendar
from price_calendar import save_calendar


sys.modules.setdefault(
    "httpx",
    types.SimpleNamespace(post=lambda *args, **kwargs: None),
)
logging.basicConfig = lambda *args, **kwargs: None

from main import price_hint_for_route


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


if __name__ == "__main__":
    unittest.main()
