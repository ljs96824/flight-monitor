import json
import tempfile
import unittest
from datetime import date, datetime, timedelta
from pathlib import Path
from unittest.mock import patch


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

            with patch("request_cache.DEFAULT_CACHE_DIR", data_dir / "request_cache"):
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

    def test_roundtrip_reference_rows_add_fixed_return_low(self):
        from price_calendar import roundtrip_calendar_rows

        target = date.today() + timedelta(days=9)
        cheaper = target - timedelta(days=3)
        later = target + timedelta(days=1)
        return_date = target + timedelta(days=4)
        calendar = {
            "route": "SHA-PEK",
            "dates": {
                cheaper.isoformat(): {"min_price": 547, "airline": "MU"},
                target.isoformat(): {"min_price": 679, "airline": "CA"},
                later.isoformat(): {"min_price": 760, "airline": "MU"},
            },
        }

        rows = roundtrip_calendar_rows(
            calendar,
            target.isoformat(),
            return_low=557,
            return_date=return_date.isoformat(),
        )

        selected = next(row for row in rows if row["selected"])
        lowest = next(row for row in rows if row["lowest"])
        self.assertEqual(selected["outbound_min_price"], 679)
        self.assertEqual(selected["return_min_price"], 557)
        self.assertEqual(selected["min_price"], 1236)
        self.assertEqual(selected["scope"], "roundtrip")
        self.assertEqual(selected["return_date"], return_date.isoformat())
        self.assertEqual(lowest["date"], cheaper.isoformat())
        self.assertEqual(lowest["min_price"], 1104)

    def test_analyzer_roundtrip_calendar_uses_fixed_return_date_price(self):
        from analyzer import analyze_price_calendar

        target = date.today() + timedelta(days=9)
        cheaper = target - timedelta(days=3)
        return_date = target + timedelta(days=4)
        outbound_calendar = {
            "route": "SHA-PEK",
            "dates": {
                cheaper.isoformat(): {"min_price": 547},
                target.isoformat(): {"min_price": 679},
            },
        }
        return_calendar = {
            "route": "PEK-SHA",
            "dates": {return_date.isoformat(): {"min_price": 557}},
        }

        result = analyze_price_calendar(
            outbound_calendar,
            target.isoformat(),
            2760,
            round_trip=True,
            return_calendar=return_calendar,
            return_date=return_date.isoformat(),
        )

        selected = next(row for row in result["rows"] if row["selected"])
        self.assertEqual(result["scope"], "roundtrip")
        self.assertEqual(result["return_min_price"], 557)
        self.assertEqual(selected["min_price"], 1236)
        self.assertEqual(selected["outbound_min_price"], 679)
        self.assertEqual(selected["return_min_price"], 557)
        self.assertEqual(result["savings"][0]["save"], 132)

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
        self.assertEqual(pattern["min_date"], dates[2])
        self.assertEqual(pattern["min_weekday"], WEEKDAY_NAMES[(target - timedelta(days=1)).weekday()])
        self.assertEqual(pattern["min_price"], 480)
        self.assertIn("近期最低", pattern["tip"])

    def test_weekday_pattern_reports_actual_min_date_and_avoids_forced_weekend_claim(self):
        from price_calendar import analyze_weekday_pattern

        start = date.today() + timedelta(days=14)
        start += timedelta(days=(5 - start.weekday()) % 7)
        prices = [607, 599, 659, 537, 570, 760, 636, 646, 665, 679, 605, 679, 834, 669]
        calendar = {
            "route": "PVG-PEK",
            "dates": {
                (start + timedelta(days=offset)).isoformat(): {"min_price": price}
                for offset, price in enumerate(prices)
            },
        }

        pattern = analyze_weekday_pattern(calendar, min_samples=7)
        expected_min_date = (start + timedelta(days=3)).isoformat()

        self.assertEqual(pattern["min_date"], expected_min_date)
        self.assertEqual(pattern["min_weekday"], "周二")
        self.assertEqual(pattern["min_price"], 537)
        self.assertIn(expected_min_date, pattern["tip"])
        self.assertIn("周二", pattern["tip"])
        self.assertNotIn("周日通常更便宜", pattern["tip"])

    def test_weekday_pattern_requires_enough_samples(self):
        from price_calendar import analyze_weekday_pattern

        calendar = {"route": "PVG-PEK", "dates": {"2026-06-10": {"min_price": 527}}}
        self.assertEqual(analyze_weekday_pattern(calendar), {"data_insufficient": True})

    def test_weekday_pattern_uses_median_and_iqr_instead_of_mean(self):
        from price_calendar import analyze_weekday_pattern

        start = date.today() + timedelta(days=14)
        monday = start + timedelta(days=(0 - start.weekday()) % 7)
        calendar_dates = {}
        for week, price in enumerate((100, 100, 1000)):
            calendar_dates[(monday + timedelta(days=week * 7)).isoformat()] = {
                "min_price": price
            }
        for week, price in enumerate((200, 200, 200)):
            calendar_dates[(monday + timedelta(days=week * 7 + 1)).isoformat()] = {
                "min_price": price
            }

        pattern = analyze_weekday_pattern(
            {"route": "PVG-PEK", "dates": calendar_dates},
            min_samples=6,
        )

        self.assertEqual(pattern["cheapest_weekday"], "周一")
        self.assertEqual(pattern["by_weekday"]["周一"], 100)
        self.assertEqual(pattern["iqr_by_weekday"]["周一"], [100, 550])
        self.assertEqual(pattern["sample_count_by_weekday"]["周一"], 3)
        self.assertIn("中位数", pattern["tip"])
        self.assertIn("n=3", pattern["tip"])


if __name__ == "__main__":
    unittest.main()
