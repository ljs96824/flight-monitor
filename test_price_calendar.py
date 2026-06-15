import json
import tempfile
import unittest
from datetime import date, datetime, timedelta
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
            today = date.today()
            target = today + timedelta(days=3)
            fresh_date = (target - timedelta(days=1)).isoformat()
            stale_date = (target + timedelta(days=1)).isoformat()
            fresh_time = (datetime.now() - timedelta(hours=1)).isoformat(timespec="seconds")
            stale_time = (datetime.now() - timedelta(hours=7)).isoformat(timespec="seconds")
            path = data_dir / "PVG-PEK.json"
            path.write_text(
                json.dumps(
                    {
                        "route": "PVG-PEK",
                        "dates": {
                            fresh_date: {"min_price": 999, "airline": "MU", "updated_at": fresh_time},
                            stale_date: {"min_price": 999, "airline": "MU", "updated_at": stale_time},
                        },
                    }
                ),
                encoding="utf-8",
            )
            prices = {
                (target + timedelta(days=offset)).isoformat(): 500 + offset
                for offset in range(-3, 15)
                if target + timedelta(days=offset) >= today
            }
            source = FakeSource(prices)

            calendar = update_calendar(
                "PVG-PEK",
                "PVG",
                "PEK",
                target.isoformat(),
                source,
                data_dir=data_dir,
                sleep_seconds=0,
            )

            called_dates = {call[2] for call in source.calls}
            self.assertTrue(all(date.fromisoformat(d) >= today for d in called_dates))
            self.assertNotIn(fresh_date, called_dates)
            self.assertIn(stale_date, called_dates)
            self.assertIn(today.isoformat(), called_dates)
            self.assertIn((target + timedelta(days=14)).isoformat(), called_dates)
            self.assertEqual(calendar["dates"][stale_date]["airline"], "KN")

    def test_calendar_rows_hide_past_dates(self):
        from price_calendar import calendar_rows

        yesterday = (date.today() - timedelta(days=1)).isoformat()
        today = date.today().isoformat()
        tomorrow = (date.today() + timedelta(days=1)).isoformat()
        rows = calendar_rows(
            {
                "route": "PVG-PEK",
                "dates": {
                    yesterday: {"min_price": 480},
                    today: {"min_price": 520},
                    tomorrow: {"min_price": 560},
                },
            },
            today,
        )

        dates = [row["date"] for row in rows]
        self.assertNotIn(yesterday, dates)
        self.assertIn(today, dates)
        self.assertIn(tomorrow, dates)

    def test_analyze_date_savings_and_weekday_pattern(self):
        from price_calendar import WEEKDAY_NAMES, analyze_date_savings, analyze_weekday_pattern

        target = date.today() + timedelta(days=3)
        dates = [(target + timedelta(days=offset)).isoformat() for offset in range(-3, 5)]
        calendar = {
            "route": "PVG-PEK",
            "dates": {
                dates[0]: {"min_price": 620},
                dates[1]: {"min_price": 540},
                dates[2]: {"min_price": 480},
                dates[3]: {"min_price": 700},
                dates[4]: {"min_price": 560},
                dates[5]: {"min_price": 590},
                dates[6]: {"min_price": 780},
                dates[7]: {"min_price": 760},
            },
        }

        savings = analyze_date_savings(calendar, dates[3], 700, threshold=100)
        self.assertEqual(savings[0]["date"], dates[2])
        self.assertEqual(savings[0]["save"], 220)
        self.assertIn(dates[2], savings[0]["tip"])

        pattern = analyze_weekday_pattern(calendar, min_samples=7)
        self.assertEqual(pattern["cheapest_weekday"], WEEKDAY_NAMES[(target - timedelta(days=1)).weekday()])
        self.assertIn("通常更便宜", pattern["tip"])

    def test_weekday_pattern_requires_enough_samples(self):
        from price_calendar import analyze_weekday_pattern

        calendar = {"route": "PVG-PEK", "dates": {"2026-06-10": {"min_price": 527}}}
        self.assertEqual(analyze_weekday_pattern(calendar), {"data_insufficient": True})


if __name__ == "__main__":
    unittest.main()
