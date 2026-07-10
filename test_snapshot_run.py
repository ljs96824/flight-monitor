import unittest


import io
from contextlib import redirect_stdout
from datetime import date
from unittest.mock import patch


class SnapshotRunTest(unittest.TestCase):
    def test_snapshot_dates_default_to_today_plus_21_and_allow_override(self):
        from scripts.snapshot_run import resolve_snapshot_dates

        for current, expected in (
            (date(2026, 7, 7), "2026-07-28"),
            (date(2026, 7, 10), "2026-07-31"),
            (date(2026, 7, 13), "2026-08-03"),
        ):
            with self.subTest(today=current):
                depart_date, return_date = resolve_snapshot_dates(today=current)
                self.assertEqual((depart_date, return_date), (expected, expected))

        depart_date, return_date = resolve_snapshot_dates(
            today=date(2026, 7, 10),
            depart_date="2026-12-01",
            return_date="2026-12-03",
        )
        self.assertEqual((depart_date, return_date), ("2026-12-01", "2026-12-03"))

    def test_build_snapshot_skips_failed_item_and_keeps_remaining_snapshot(self):
        from scripts import snapshot_run

        output = io.StringIO()
        with patch.object(snapshot_run, "calendar_snapshot", side_effect=RuntimeError("calendar boom")):
            with redirect_stdout(output):
                snapshot = snapshot_run.build_snapshot()

        self.assertGreater(snapshot["collection"]["outbound_count"], 0)
        self.assertEqual(snapshot["price_calendar"]["rows"], [])
        self.assertIn(
            {"item": "price_calendar", "reason": "calendar boom"},
            snapshot["skipped_items"],
        )
        self.assertIn("[快照跳过] 项=price_calendar 原因=calendar boom", output.getvalue())

    def test_build_snapshot_has_full_chain_baseline_fields(self):
        from scripts.snapshot_run import build_snapshot

        snapshot = build_snapshot()

        self.assertEqual(snapshot["subscription"]["preferences"]["passengers"]["adult"], 3)
        self.assertEqual(snapshot["subscription"]["preferences"]["max_budget"], 1700)
        self.assertEqual(snapshot["subscription"]["basic"]["route_type"], "domestic")
        self.assertIn("price_points", snapshot)
        price_points = snapshot["price_points"]
        self.assertIn("recommended_plan_vs_budget", price_points)
        self.assertIn("exclusion_diagnosis", price_points)
        self.assertIn("alternative_price_diagnosis", price_points)
        self.assertIn("calendar_array", price_points)
        self.assertIn("selected_date_price", price_points)
        self.assertIn("channel_comparison", price_points)
        self.assertEqual(price_points["recommended_plan_vs_budget"]["max_acceptable_price"], 1700)
        self.assertEqual(price_points["recommended_plan_vs_budget"]["max_acceptable_price_scope"], "单人往返预算")
        self.assertEqual(price_points["calendar_array"]["price_scope"], "3人往返参考价")
        self.assertEqual(price_points["selected_date_price"]["price_scope"], "3人往返参考价")
        self.assertTrue(price_points["calendar_array"]["is_passenger_scoped"])
        self.assertIn("price_scope", price_points["alternative_price_diagnosis"])
        self.assertIn("price_scope", price_points["channel_comparison"])
        self.assertIn("price_calendar", snapshot)
        self.assertIn("before_unit_first3", snapshot["price_calendar"])
        self.assertIn("after_passenger_first3", snapshot["price_calendar"])
        self.assertGreater(len(snapshot["price_calendar"]["after_passenger_first3"]), 0)
        self.assertEqual(snapshot["price_calendar"]["passenger_factor"], 3)
        for unit, total in zip(
            snapshot["price_calendar"]["before_unit_first3"],
            snapshot["price_calendar"]["after_passenger_first3"],
        ):
            self.assertAlmostEqual(total, unit * 3)
        self.assertIn("same_day", snapshot)
        self.assertIn("outbound_window_matches", snapshot["same_day"])
        self.assertEqual(snapshot["same_day"]["outbound_window_match_count"], 0)
        self.assertEqual(snapshot["same_day"]["outbound_window_matches"], [])
        reason = snapshot["same_day"]["no_result_reason"]
        self.assertIn("MU5099", reason)
        self.assertIn("09:15", reason)
        self.assertIn("\u672c\u6b21\u65e0\u65b9\u6848\u4e3b\u56e0\u662f\u3010\u53bb\u7a0b\u65f6\u95f4\u3011", reason)
        self.assertIn("需08:00前落地", reason)
        self.assertIn("晚1h15m", reason)
        self.assertIn("\u8fd4\u7a0b\u6709", reason)
        self.assertIn("\u975e\u963b\u585e", reason)
        self.assertIn("\u524d\u4e00\u665a\u5230\u8fbe", reason)
        matched_flights = {item["flight_no"] for item in snapshot["same_day"]["outbound_window_matches"]}
        self.assertNotIn("MU5185", matched_flights)
        self.assertNotIn("CA1566", matched_flights)
        self.assertIn("return_recommendation", snapshot["same_day"])
        self.assertIn("no_result_reason", snapshot["same_day"])
        self.assertIn("airport_transport_to_meeting", snapshot)
        self.assertEqual(snapshot["airport_transport_to_meeting"]["PKX"], 25)


if __name__ == "__main__":
    unittest.main()
