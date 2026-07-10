import io
import json
import sys
import tempfile
import types
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from unittest.mock import patch


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

    def test_tracks_unavailable_when_previous_plan_missing(self):
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

        self.assertEqual(status["status"], "unavailable")
        self.assertIn("CA1234", status["msg"])
        self.assertIn("本次未获取到报价", status["msg"])

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

    def test_roundtrip_tracking_compares_same_combo_same_scope(self):
        from plan_tracker import save_pushed_plans, track_plan_status

        with tempfile.TemporaryDirectory() as tmp:
            data_dir = Path(tmp)
            save_pushed_plans(
                "sub-rt",
                [
                    {
                        "label": "方案A",
                        "is_roundtrip": True,
                        "outbound_flight": {"flight_no": "MU5099", "price": 1410},
                        "return_flight": {"flight_no": "CA1589", "price": 1350},
                        "price": 2760,
                        "roundtrip_price": 2760,
                    }
                ],
                data_dir=data_dir,
            )

            status = track_plan_status(
                "sub-rt",
                [
                    {
                        "is_roundtrip": True,
                        "outbound": {"flight_no": "MU5099", "price": 1448},
                        "return": {"flight_no": "CA1589", "price": 1362},
                        "total_price": 2810,
                    },
                    {"flight_no": "MU5099", "price": 1448},
                    {"flight_no": "CA1589", "price": 1362},
                ],
                data_dir=data_dir,
            )

        self.assertEqual(status["status"], "stable")
        self.assertEqual(status["scope"], "roundtrip")
        self.assertEqual(status["previous_price"], 2760)
        self.assertEqual(status["current_price"], 2810)
        self.assertIn("MU5099去+CA1589回", status["msg"])
        self.assertIn("同往返口径对比", status["msg"])
        self.assertNotIn("降了", status["msg"])


    def test_roundtrip_tracking_normalizes_historical_combo_keys(self):
        from plan_tracker import track_plan_status

        with tempfile.TemporaryDirectory() as tmp:
            data_dir = Path(tmp)
            (data_dir / "sub-rt.json").write_text(
                json.dumps(
                    {
                        "subscription_id": "sub-rt",
                        "last_pushed": {
                            "plan_a": {
                                "flight_no": "NH0970 | NH0041?+KE 2118+KE 2057?",
                                "is_roundtrip": True,
                                "scope": "roundtrip",
                                "outbound_flight": "NH0970 | NH0041",
                                "return_flight": "KE 2118+KE 2057",
                                "roundtrip_price": 1000,
                                "price": 1000,
                            }
                        },
                    },
                    ensure_ascii=False,
                ),
                encoding="utf-8",
            )

            status = track_plan_status(
                "sub-rt",
                [
                    {
                        "is_roundtrip": True,
                        "outbound": {"flight_no": "NH970+NH41", "price": 600},
                        "return": {"flight_no": "KE2118+KE2057", "price": 500},
                        "total_price": 1100,
                    }
                ],
                data_dir=data_dir,
            )

        self.assertEqual(status["status"], "price_up")
        self.assertEqual(status["current_price"], 1100)
        self.assertEqual(status["price_diff"], 100)

    def test_saved_roundtrip_payload_stores_normalized_combo_keys(self):
        from plan_tracker import save_pushed_plans

        with tempfile.TemporaryDirectory() as tmp:
            data_dir = Path(tmp)
            payload = save_pushed_plans(
                "sub-rt",
                [
                    {
                        "label": "plan_a",
                        "is_roundtrip": True,
                        "outbound_flight": {"flight_no": "NH0970 | NH0041", "price": 600},
                        "return_flight": {"flight_no": "KE 2118+KE 2057", "price": 500},
                        "roundtrip_price": 1100,
                    }
                ],
                data_dir=data_dir,
            )

        plan_a = payload["last_pushed"]["plan_a"]
        self.assertEqual(plan_a["outbound_flight"], "NH970+NH41")
        self.assertEqual(plan_a["return_flight"], "KE2118+KE2057")
        self.assertEqual(plan_a["flight_no"], "NH970+NH41+KE2118+KE2057")

    def test_roundtrip_tracking_pool_includes_full_outbound_and_return_candidates(self):
        from plan_tracker import save_pushed_plans, track_plan_status

        try:
            from notifier import _tracking_current_items
        except ModuleNotFoundError as exc:
            if exc.name != "httpx":
                raise
            with patch.dict(sys.modules, {"httpx": types.ModuleType("httpx")}):
                from notifier import _tracking_current_items

        with tempfile.TemporaryDirectory() as tmp:
            data_dir = Path(tmp)
            save_pushed_plans(
                "sub-pool",
                [
                    {
                        "label": "plan_a",
                        "is_roundtrip": True,
                        "outbound_flight": {"flight_no": "MU 0225", "price": 900},
                        "return_flight": {"flight_no": "JL0891", "price": 600},
                        "roundtrip_price": 1500,
                    }
                ],
                data_dir=data_dir,
            )
            analysis = {
                "same_day_base_flights": [
                    {"flight_no": "MU225", "price": 900, "departure_date": "2026-10-01"}
                ],
                "return_analysis": {
                    "same_day_base_flights": [
                        {"flight_no": "JL891", "price": 700, "departure_date": "2026-10-06"}
                    ]
                },
                "round_trip_analysis": {"top_combinations": []},
            }
            terminal_items = [
                {
                    "is_roundtrip": True,
                    "outbound_flight": {"flight_no": "NH970", "price": 1000},
                    "return_flight": {"flight_no": "KE2118", "price": 1000},
                    "price": 2000,
                }
            ]

            output = io.StringIO()
            with redirect_stdout(output):
                pool = _tracking_current_items(analysis, terminal_items, True)
                status = track_plan_status("sub-pool", pool, data_dir=data_dir)

        log = output.getvalue()
        self.assertEqual(status["status"], "price_up")
        self.assertEqual(status["current_price"], 1600)
        self.assertIn("[追踪池]", log)
        self.assertIn("池来源=", log)
        self.assertIn("same_day_base_flights", log)
        self.assertIn("MU225", log)
        self.assertIn("JL891", log)
        self.assertIn("目标去程=MU225 在池中=True", log)
        self.assertIn("目标返程=JL891 在池中=True", log)

    def test_single_tracking_does_not_use_roundtrip_combo_total(self):
        from plan_tracker import save_pushed_plans, track_plan_status

        with tempfile.TemporaryDirectory() as tmp:
            data_dir = Path(tmp)
            save_pushed_plans(
                "sub-single",
                [
                    {
                        "label": "plan_a",
                        "main_flight": {"flight_no": "MU5099", "price": 2579},
                    }
                ],
                data_dir=data_dir,
            )

            status = track_plan_status(
                "sub-single",
                [
                    {
                        "is_roundtrip": True,
                        "flight_no": "MU5099+CA1589",
                        "outbound": {"flight_no": "MU5099"},
                        "return": {"flight_no": "CA1589", "price": 1350},
                        "total_price": 1414,
                    }
                ],
                data_dir=data_dir,
            )

        self.assertNotEqual(status["status"], "price_down")
        self.assertNotIn("1,165", status.get("msg", ""))
        self.assertNotIn("1,312", status.get("msg", ""))

    def test_roundtrip_tracking_does_not_compare_roundtrip_to_single_leg(self):
        from plan_tracker import save_pushed_plans, track_plan_status

        with tempfile.TemporaryDirectory() as tmp:
            data_dir = Path(tmp)
            save_pushed_plans(
                "sub-rt",
                [
                    {
                        "label": "方案A",
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
                [{"flight_no": "MU5099", "price": 1448}],
                data_dir=data_dir,
            )

        self.assertEqual(status["status"], "partial_unavailable")
        self.assertEqual(status["scope"], "roundtrip")
        self.assertIn("返程CA1589本次未获取到报价", status["msg"])
        self.assertIn("无法计算完整往返价", status["msg"])
        self.assertNotIn("降了", status["msg"])
        self.assertNotIn("售罄", status["msg"])

    def test_roundtrip_tracking_reports_scope_mismatch_for_abnormal_diff(self):
        from plan_tracker import save_pushed_plans, track_plan_status

        with tempfile.TemporaryDirectory() as tmp:
            data_dir = Path(tmp)
            save_pushed_plans(
                "sub-rt",
                [
                    {
                        "label": "方案A",
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
                [
                    {
                        "is_roundtrip": False,
                        "flight_no": "MU5099",
                        "price": 1448,
                    }
                ],
                data_dir=data_dir,
            )

        self.assertIn(status["status"], {"partial_unavailable", "scope_mismatch"})
        self.assertNotIn("降了¥1,312", status["msg"])

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

    def test_get_subscription_feedback_filters_unresolved_records(self):
        from plan_tracker import feedback_acknowledgement, get_subscription_feedback

        with tempfile.TemporaryDirectory() as tmp:
            data_dir = Path(tmp)
            (data_dir / "feedback.json").write_text(
                json.dumps(
                    [
                        {"subscription_id": "sub-1", "feedback_type": "unavailable"},
                        {"subscription_id": "sub-1", "feedback_type": "price_changed", "resolved_at": "2026-06-13T10:00:00"},
                        {"subscription_id": "sub-2", "feedback_type": "no_baggage"},
                    ],
                    ensure_ascii=False,
                ),
                encoding="utf-8",
            )

            records = get_subscription_feedback("sub-1", data_dir=data_dir)
            ack = feedback_acknowledgement("sub-1", data_dir=data_dir)

        self.assertEqual(len(records), 1)
        self.assertEqual(records[0]["feedback_type"], "unavailable")
        self.assertIn("买不到", ack)
        self.assertIn("重新核实可购买性", ack)


if __name__ == "__main__":
    unittest.main()
