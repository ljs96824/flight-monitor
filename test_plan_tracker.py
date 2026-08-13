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
    def test_source_degradation_prevents_sold_out_inference(self):
        from plan_tracker import _missing_quote_confidence

        result = _missing_quote_confidence(
            [{"flight_no": f"MU{i}", "price": 1000 + i} for i in range(10)],
            matched_any=False,
            source_degradation={
                "active": True,
                "source": "juhe",
                "source_label": "OTA源",
            },
        )

        self.assertEqual(result["status"], "source_unavailable")
        self.assertIn("上次推荐组合来自OTA源", result["note"])
        self.assertEqual(
            result["note"],
            "上次推荐组合来自OTA源,本轮该源不可用,无法核实在售状态。",
        )
        self.assertIn("无法核实在售状态", result["note"])
        self.assertNotIn("售罄", result["note"])
        self.assertNotIn("停飞", result["note"])

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
        self.assertEqual(status["scope"], "per_person_roundtrip")
        self.assertEqual(status["previous_price"], 2760)
        self.assertEqual(status["current_price"], 2810)
        self.assertIn("MU5099去+CA1589回", status["msg"])
        self.assertIn("同单人往返口径对比", status["msg"])
        self.assertNotIn("降了", status["msg"])

    def test_roundtrip_source_degradation_suppresses_matched_price_change(self):
        from plan_tracker import _track_roundtrip_plan

        result = _track_roundtrip_plan(
            {
                "outbound_flight": "MU5099",
                "return_flight": "CA1589",
                "price_tiers": {"unit_roundtrip": 2760},
            },
            [
                {
                    "is_roundtrip": True,
                    "outbound": {"flight_no": "MU5099", "price": 3000},
                    "return": {"flight_no": "CA1589", "price": 3000},
                    "price_tiers": {"unit_roundtrip": 6000},
                }
            ],
            source_degradation={
                "active": True,
                "source": "juhe",
                "source_label": "OTA",
                "reason": "source unavailable",
            },
        )

        self.assertEqual(result["status"], "source_unavailable")
        self.assertIsNone(result["price_diff"])
        self.assertIn("OTA", result["msg"])
        self.assertNotIn("price_up", result["status"])

    def test_roundtrip_tracking_uses_unit_price_across_passenger_changes(self):
        from plan_tracker import save_pushed_plans, track_plan_status

        with tempfile.TemporaryDirectory() as tmp:
            data_dir = Path(tmp)
            save_pushed_plans(
                "sub-pax-change",
                [
                    {
                        "label": "方案A",
                        "is_roundtrip": True,
                        "outbound_flight": {"flight_no": "MU225", "price": 6000},
                        "return_flight": {"flight_no": "JL891", "price": 6215},
                        "roundtrip_price": 33591,
                        "price_tiers": {
                            "unit_roundtrip": 12215,
                            "total_roundtrip_ref": 33591,
                            "factor": 2.75,
                            "passengers": {
                                "adult": 1,
                                "child": 1,
                                "elderly": 1,
                                "infant": 0,
                            },
                        },
                    }
                ],
                data_dir=data_dir,
            )

            output = io.StringIO()
            with redirect_stdout(output):
                status = track_plan_status(
                    "sub-pax-change",
                    [
                        {
                            "is_roundtrip": True,
                            "outbound": {"flight_no": "MU225", "price": 6000},
                            "return": {"flight_no": "JL891", "price": 6426},
                            "price_tiers": {
                                "unit_roundtrip": 12426,
                                "total_roundtrip_ref": 59024,
                                "factor": 4.75,
                                "passengers": {
                                    "adult": 2,
                                    "child": 1,
                                    "elderly": 2,
                                    "infant": 0,
                                },
                            },
                        }
                    ],
                    data_dir=data_dir,
                )

        log = output.getvalue()
        self.assertEqual(status["status"], "price_up")
        self.assertEqual(status["scope"], "per_person_roundtrip")
        self.assertEqual(status["previous_price"], 12215)
        self.assertEqual(status["current_price"], 12426)
        self.assertEqual(status["price_diff"], 211)
        self.assertIn("[追踪口径] 上次=12215.0(单人往返), 本次=12426.0(单人往返)", log)
        self.assertIn("构成变化=1+1+1→2+1+2(全员价不跨轮对比)", log)
        self.assertIn("单人往返", status["msg"])
        self.assertIn("¥211", status["msg"])

    def test_legacy_roundtrip_total_uses_factor_to_restore_unit_price(self):
        from plan_tracker import track_plan_status

        with tempfile.TemporaryDirectory() as tmp:
            data_dir = Path(tmp)
            (data_dir / "sub-legacy-factor.json").write_text(
                json.dumps(
                    {
                        "subscription_id": "sub-legacy-factor",
                        "last_pushed": {
                            "plan_a": {
                                "flight_no": "MU225+JL891",
                                "is_roundtrip": True,
                                "scope": "roundtrip",
                                "outbound_flight": "MU225",
                                "return_flight": "JL891",
                                "roundtrip_price": 33591,
                                "price_tiers": {"factor": 2.75},
                            }
                        },
                    },
                    ensure_ascii=False,
                ),
                encoding="utf-8",
            )

            status = track_plan_status(
                "sub-legacy-factor",
                [
                    {
                        "is_roundtrip": True,
                        "outbound": {"flight_no": "MU225", "price": 6000},
                        "return": {"flight_no": "JL891", "price": 6426},
                        "price_tiers": {"unit_roundtrip": 12426},
                    }
                ],
                data_dir=data_dir,
            )

        self.assertEqual(status["previous_price"], 12215)
        self.assertEqual(status["current_price"], 12426)
        self.assertEqual(status["price_diff"], 211)
        self.assertEqual(status["scope"], "per_person_roundtrip")

    def test_legacy_roundtrip_total_without_factor_skips_comparison(self):
        from plan_tracker import track_plan_status

        with tempfile.TemporaryDirectory() as tmp:
            data_dir = Path(tmp)
            (data_dir / "sub-legacy-no-factor.json").write_text(
                json.dumps(
                    {
                        "subscription_id": "sub-legacy-no-factor",
                        "last_pushed": {
                            "plan_a": {
                                "flight_no": "MU225+JL891",
                                "is_roundtrip": True,
                                "scope": "roundtrip",
                                "outbound_flight": "MU225",
                                "return_flight": "JL891",
                                "roundtrip_price": 33591,
                            }
                        },
                    },
                    ensure_ascii=False,
                ),
                encoding="utf-8",
            )

            output = io.StringIO()
            with redirect_stdout(output):
                status = track_plan_status(
                    "sub-legacy-no-factor",
                    [
                        {
                            "is_roundtrip": True,
                            "outbound": {"flight_no": "MU225", "price": 6000},
                            "return": {"flight_no": "JL891", "price": 6426},
                            "price_tiers": {"unit_roundtrip": 12426},
                        }
                    ],
                    data_dir=data_dir,
                )

        self.assertEqual(status["status"], "comparison_skipped")
        self.assertEqual(status["scope"], "per_person_roundtrip")
        self.assertIsNone(status["price_diff"])
        self.assertIn("[追踪跳过] 原因=历史记录无单人口径", output.getvalue())
        self.assertNotIn("上涨", status["msg"])
        self.assertNotIn("下降", status["msg"])


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
                                "price_tiers": {"unit_roundtrip": 1000},
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
        self.assertEqual(status["scope"], "per_person_roundtrip")
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
