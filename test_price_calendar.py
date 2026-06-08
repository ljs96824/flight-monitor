import json
import tempfile
import unittest
from datetime import datetime, timedelta
from pathlib import Path


class FakeSource:
    def __init__(self, prices):
        self.prices = prices
        self.calls = []

    def fetch(self, origin, dest, date_str, cabin_class="economy"):
        self.calls.append((origin, dest, date_str, cabin_class))
        price = self.prices.get(date_str)
        if price is None:
            return {"flights": []}
        return {
            "flights": [
                {"price": price, "airline": "KN", "flight_no": f"KN{date_str[-2:]}"},
                {"price": price + 80, "airline": "CA", "flight_no": f"CA{date_str[-2:]}"},
            ]
        }


class PriceCalendarTest(unittest.TestCase):
    def test_update_calendar_queries_nearby_and_sample_dates_with_cache(self):
        from price_calendar import update_calendar

        with tempfile.TemporaryDirectory() as tmp:
            data_dir = Path(tmp)
            fresh_time = (datetime.now() - timedelta(hours=1)).isoformat(timespec="seconds")
            stale_time = (datetime.now() - timedelta(hours=7)).isoformat(timespec="seconds")
            path = data_dir / "PVG-PEK.json"
            path.write_text(
                json.dumps(
                    {
                        "route": "PVG-PEK",
                        "dates": {
                            "2026-06-09": {"min_price": 999, "airline": "MU", "updated_at": fresh_time},
                            "2026-06-11": {"min_price": 999, "airline": "MU", "updated_at": stale_time},
                        },
                    }
                ),
                encoding="utf-8",
            )
            prices = {
                "2026-05-27": 700,
                "2026-06-03": 620,
                "2026-06-07": 610,
                "2026-06-08": 540,
                "2026-06-10": 527,
                "2026-06-11": 480,
                "2026-06-12": 560,
                "2026-06-13": 780,
                "2026-06-17": 650,
                "2026-06-24": 720,
            }
            source = FakeSource(prices)

            calendar = update_calendar(
                "PVG-PEK",
                "PVG",
                "PEK",
                "2026-06-10",
                source,
                data_dir=data_dir,
                sleep_seconds=0,
            )

            called_dates = {call[2] for call in source.calls}
            self.assertNotIn("2026-06-09", called_dates)
            self.assertIn("2026-06-11", called_dates)
            self.assertIn("2026-05-27", called_dates)
            self.assertIn("2026-06-24", called_dates)
            self.assertEqual(calendar["dates"]["2026-06-11"]["min_price"], 480)
            self.assertEqual(calendar["dates"]["2026-06-11"]["airline"], "KN")

    def test_analyze_date_savings_and_weekday_pattern(self):
        from price_calendar import analyze_date_savings, analyze_weekday_pattern

        calendar = {
            "route": "PVG-PEK",
            "dates": {
                "2026-06-07": {"min_price": 620},
                "2026-06-08": {"min_price": 540},
                "2026-06-09": {"min_price": 480},
                "2026-06-10": {"min_price": 700},
                "2026-06-11": {"min_price": 560},
                "2026-06-12": {"min_price": 590},
                "2026-06-13": {"min_price": 780},
                "2026-06-14": {"min_price": 760},
            },
        }

        savings = analyze_date_savings(calendar, "2026-06-10", 700, threshold=100)
        self.assertEqual(savings[0]["date"], "2026-06-09")
        self.assertEqual(savings[0]["save"], 220)
        self.assertIn("提前1天", savings[0]["tip"])

        pattern = analyze_weekday_pattern(calendar, min_samples=7)
        self.assertEqual(pattern["cheapest_weekday"], "周二")
        self.assertIn("通常更便宜", pattern["tip"])

    def test_weekday_pattern_requires_enough_samples(self):
        from price_calendar import analyze_weekday_pattern

        calendar = {"route": "PVG-PEK", "dates": {"2026-06-10": {"min_price": 527}}}
        self.assertEqual(analyze_weekday_pattern(calendar), {"data_insufficient": True})


if __name__ == "__main__":
    unittest.main()
