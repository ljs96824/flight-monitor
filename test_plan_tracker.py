import json
import tempfile
import unittest
from pathlib import Path


class PlanTrackerTest(unittest.TestCase):
    def test_tracks_price_up_for_previous_plan(self):
        from plan_tracker import save_pushed_plans, track_plan_status

        with tempfile.TemporaryDirectory() as tmp:
            data_dir = Path(tmp)
            plans = [
                {
                    "label": "方案A",
                    "price": 680,
                    "outbound_flight": {"flight_no": "MU5101", "price": 680},
                }
            ]
            save_pushed_plans("sub-1", plans, data_dir=data_dir)

            status = track_plan_status(
                "sub-1",
                [{"flight_no": "MU5101", "price": 760}],
                data_dir=data_dir,
            )

        self.assertEqual(status["status"], "price_up")
        self.assertEqual(status["flight_no"], "MU5101")
        self.assertEqual(status["price_diff"], 80)

    def test_tracks_sold_out_when_previous_plan_missing(self):
        from plan_tracker import save_pushed_plans, track_plan_status

        with tempfile.TemporaryDirectory() as tmp:
            data_dir = Path(tmp)
            save_pushed_plans(
                "sub-1",
                [{"label": "方案A", "main_flight": {"flight_no": "CA1234", "price": 500}}],
                data_dir=data_dir,
            )

            status = track_plan_status(
                "sub-1",
                [{"flight_no": "MU5101", "price": 500}],
                data_dir=data_dir,
            )

        self.assertEqual(status["status"], "sold_out")
        self.assertIn("CA1234", status["msg"])

    def test_saved_payload_contains_plan_a_record(self):
        from plan_tracker import save_pushed_plans

        with tempfile.TemporaryDirectory() as tmp:
            data_dir = Path(tmp)
            save_pushed_plans(
                "sub-1",
                [{"label": "方案A", "main_flight": {"flight_no": "KN5978", "price": 527}}],
                data_dir=data_dir,
            )
            record = json.loads((data_dir / "sub-1.json").read_text(encoding="utf-8"))

        self.assertEqual(record["last_pushed"]["plan_a"]["flight_no"], "KN5978")
        self.assertEqual(record["last_pushed"]["plan_a"]["price"], 527)

    def test_illegal_subscription_id_is_sanitized_for_windows_filename(self):
        from filename_utils import sanitize_filename
        from plan_tracker import load_pushed_plans, save_pushed_plans

        sub_id = "上海|北京 会议/当天往返"
        with tempfile.TemporaryDirectory() as tmp:
            data_dir = Path(tmp)
            save_pushed_plans(
                sub_id,
                [{"label": "方案A", "main_flight": {"flight_no": "CA1510", "price": 720}}],
                data_dir=data_dir,
            )
            expected_path = data_dir / f"{sanitize_filename(sub_id)}.json"
            loaded = load_pushed_plans(sub_id, data_dir=data_dir)
            saved = expected_path.exists()

        self.assertTrue(saved)
        self.assertEqual(loaded["last_pushed"]["plan_a"]["flight_no"], "CA1510")


if __name__ == "__main__":
    unittest.main()
