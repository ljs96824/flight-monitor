import tempfile
import unittest
import contextlib
import io
from datetime import datetime
from pathlib import Path
from unittest.mock import patch

import analyzer
import main
import notifier
import storage
from price_estimator import build_display_prices
from provenance import attach_payload_provenance


EXPECTED_FINGERPRINT_FIELDS = {
    "direct_only",
    "transfer_policy",
    "red_eye",
    "red_eye_policy",
    "allow_red_eye",
    "no_redeye_strict",
    "departure_time_policy",
    "arrival_time_policy",
    "departure_slots",
    "arrival_slots",
    "outbound_departure_slots",
    "outbound_arrival_slots",
    "return_departure_slots",
    "return_arrival_slots",
    "departure_time_windows",
    "arrival_time_windows",
    "outbound_departure_time_windows",
    "outbound_arrival_time_windows",
    "return_departure_time_windows",
    "return_arrival_time_windows",
    "time_preference_mode",
    "same_day_round_trip",
    "day_trip_period",
    "business_start",
    "business_end",
    "meeting_location",
    "meeting_importance",
    "outbound_set_off",
    "return_set_off",
    "user_transport_min",
    "origin_transport_min",
    "destination_transport_min",
    "airport_advance_min",
    "arrival_exit_min",
    "delay_buffer_min",
    "pre_meeting_buffer_min",
    "post_meeting_buffer_min",
    "custom_redundancy_min",
    "transport_margin_mode",
    "redundancy_min",
    "need_baggage",
    "airline_policy",
    "exclude_airlines",
    "lcc_policy",
    "max_extra_duration_hours",
    "max_total_duration_hours",
    "accept_overnight_transfer",
    "accept_self_transfer",
    "origin_airport_preference",
    "origin_airports_active",
    "destination_airports_active",
    "excluded_airports",
    "cabin_classes",
    "cabin_allocation",
}


def _flight(price, source="juhe", price_source=None):
    flight = {
        "flight_combo": f"MU{int(price)}",
        "airline_summary": "测试航司",
        "price": price,
        "total_duration_min": 120,
        "stops": 0,
        "route_summary": "PVG-KIX",
        "layover_summary": "",
        "segments": [],
        "data_source": source,
    }
    if price_source:
        flight["price_source"] = price_source
    return flight


class ConstraintFingerprintTest(unittest.TestCase):
    def test_constraint_history_pool_uses_post_filter_candidates(self):
        excluded_lcc = _flight(7387)
        eligible = _flight(13452, "hasdata+juhe", "juhe")
        analysis = {
            "all_flights": [eligible],
            "excluded_flights": [
                {**excluded_lcc, "exclude_reason": "lcc_excluded"},
            ],
        }

        self.assertEqual(
            main._constraint_history_flights(analysis),
            [eligible],
        )

    def test_buy_wait_trend_accepts_constraint_history_metadata(self):
        history = [
            {"price": 14000, "constraint_fingerprint": "same"},
            {"price": 13800, "constraint_fingerprint": "same"},
            {"price": 13452, "constraint_fingerprint": "same"},
        ]

        result = analyzer.calc_buy_vs_wait_risk(
            13452,
            history,
            days_to_dept=68,
            target_price=6000,
        )

        self.assertEqual(result["trend"], "近期仍有下降")

    def test_constraint_history_recompute_preserves_risk_decisions(self):
        previous = {
            "buy_level": "低",
            "wait_level": "中",
            "buy_risks": ["原购买风险"],
            "wait_risks": ["原等待风险"],
            "leaning": "原判断",
            "summary": "原说明",
            "trend": "旧趋势",
        }
        constrained = {
            "buy_level": "高",
            "wait_level": "高",
            "buy_risks": ["新购买风险"],
            "wait_risks": ["新等待风险"],
            "leaning": "新判断",
            "summary": "新说明",
            "trend": "同条件趋势",
        }

        merged = main._merge_constraint_history_trend(previous, constrained)

        self.assertEqual(merged["trend"], "同条件趋势")
        for key in (
            "buy_level",
            "wait_level",
            "buy_risks",
            "wait_risks",
            "leaning",
            "summary",
        ):
            self.assertEqual(merged[key], previous[key])

    def test_fingerprint_field_set_is_frozen(self):
        from constraint_fingerprint import (
            CONSTRAINT_FINGERPRINT_FIELDS,
            EXPECTED_CONSTRAINT_FINGERPRINT_FIELDS,
        )

        self.assertEqual(set(CONSTRAINT_FINGERPRINT_FIELDS), EXPECTED_FINGERPRINT_FIELDS)
        self.assertEqual(
            EXPECTED_CONSTRAINT_FINGERPRINT_FIELDS,
            EXPECTED_FINGERPRINT_FIELDS,
        )

    def test_price_signal_method_version_tracks_constraint_bucket_change(self):
        from method_registry import method_version

        self.assertEqual(method_version("price_signal"), "price_signal_v2")

    def test_equivalent_constraints_share_fingerprint_and_lcc_change_does_not(self):
        from constraint_fingerprint import constraint_fingerprint

        top_level = {
            "direct_only": "flexible",
            "transfer_policy": "reasonable",
            "red_eye": "reject",
            "need_baggage": "required",
            "airline_policy": "any",
            "exclude_airlines": ["JL", "NH"],
            "lcc_policy": "any",
            "cabin_classes": ["premium_economy", "economy"],
            "departure_time_windows": [
                ["06:00", "12:00"],
                ["14:00", "18:00"],
            ],
        }
        nested = {
            "hard_constraints": {
                "direct_only": "flexible",
                "transfer_policy": "reasonable",
                "red_eye": "reject",
                "need_baggage": "required",
            },
            "soft_preferences": {
                "airline_policy": "any",
                "exclude_airlines": ["NH", "JL"],
                "lcc_policy": "any",
                "departure_time_windows": [
                    ("14:00", "18:00"),
                    ("06:00", "12:00"),
                ],
            },
            "basic": {
                "cabin_classes": ["economy", "premium_economy"],
            },
        }

        self.assertEqual(
            constraint_fingerprint(top_level),
            constraint_fingerprint(nested),
        )
        self.assertNotEqual(
            constraint_fingerprint(top_level),
            constraint_fingerprint({**top_level, "lcc_policy": "exclude_lcc"}),
        )

    def test_history_queries_isolate_fingerprints_and_restart_sample_gate(self):
        from constraint_fingerprint import constraint_fingerprint

        old_fp = constraint_fingerprint({"lcc_policy": "any"})
        new_fp = constraint_fingerprint({"lcc_policy": "exclude_lcc"})
        with tempfile.TemporaryDirectory() as temp_dir:
            db_path = Path(temp_dir) / "prices.db"
            with patch.object(storage, "DB_PATH", db_path):
                storage.init_db()
                with patch.object(storage, "datetime") as mocked_datetime:
                    mocked_datetime.now.return_value = datetime(2026, 7, 24, 9, 0, 0)
                    storage.save_flight_details(
                        "PVG-KIX",
                        "2026-10-01",
                        [_flight(7387)],
                        constraint_fingerprint=old_fp,
                    )
                    mocked_datetime.now.return_value = datetime(2026, 7, 25, 9, 0, 0)
                    storage.save_flight_details(
                        "PVG-KIX",
                        "2026-10-01",
                        [_flight(13452, "hasdata+juhe", "juhe")],
                        constraint_fingerprint=new_fp,
                    )

                old_history = storage.get_lowest_price_history(
                    "PVG-KIX",
                    "2026-10-01",
                    limit=14,
                    constraint_fingerprint=old_fp,
                    include_metadata=True,
                )
                new_history = storage.get_lowest_price_history(
                    "PVG-KIX",
                    "2026-10-01",
                    limit=14,
                    constraint_fingerprint=new_fp,
                    include_metadata=True,
                )

        self.assertEqual([row["price"] for row in old_history], [7387])
        self.assertEqual([row["price"] for row in new_history], [13452])
        self.assertEqual(new_history[0]["sources"], ["juhe"])
        signal = analyzer.build_price_signal(
            13452,
            target_price=6000,
            price_history=new_history,
        )
        self.assertEqual(signal["sample_n"], 1)
        self.assertEqual(signal["label"], "待积累")
        self.assertIn("同条件样本不足", signal["summary"])
        self.assertIsNone(analyzer.price_position_description(13452, new_history))
        self.assertIsNone(analyzer.waiting_risk_description(new_history, 13452, 68))
        self.assertEqual(
            notifier._trend_linechart_summary(new_history),
            "历史样本不足，仅供参考。",
        )

    def test_previous_prices_reads_latest_completed_constraint_snapshot(self):
        from constraint_fingerprint import constraint_fingerprint

        fingerprint = constraint_fingerprint({"lcc_policy": "exclude_lcc"})
        with tempfile.TemporaryDirectory() as temp_dir:
            db_path = Path(temp_dir) / "prices.db"
            with patch.object(storage, "DB_PATH", db_path):
                with patch.object(storage, "datetime") as mocked_datetime:
                    mocked_datetime.now.return_value = datetime(2026, 7, 25, 9, 0, 0)
                    storage.save_flight_details(
                        "PVG-KIX",
                        "2026-10-01",
                        [_flight(13452, "hasdata+juhe", "juhe")],
                        constraint_fingerprint=fingerprint,
                    )
                previous = storage.get_previous_snapshot_prices(
                    "PVG-KIX",
                    "2026-10-01",
                    constraint_fingerprint=fingerprint,
                )

        self.assertEqual(previous, {"MU13452": 13452.0})

    def test_roundtrip_history_isolated_and_keeps_actual_sources(self):
        from constraint_fingerprint import constraint_fingerprint

        old_fp = constraint_fingerprint({"lcc_policy": "any"})
        new_fp = constraint_fingerprint({"lcc_policy": "exclude_lcc"})
        with tempfile.TemporaryDirectory() as temp_dir:
            db_path = Path(temp_dir) / "prices.db"
            with patch.object(storage, "DB_PATH", db_path):
                storage.save_roundtrip_snapshot(
                    "PVG-KIX",
                    "2026-10-01",
                    "2026-10-06",
                    3500,
                    3887,
                    7387,
                    "2026-07-24T09:00:00",
                    constraint_fingerprint=old_fp,
                    sources=["hasdata", "juhe"],
                )
                storage.save_roundtrip_snapshot(
                    "PVG-KIX",
                    "2026-10-01",
                    "2026-10-06",
                    6500,
                    6952,
                    13452,
                    "2026-07-25T09:00:00",
                    constraint_fingerprint=new_fp,
                    sources=["juhe"],
                )
                history = storage.get_roundtrip_price_history(
                    "PVG-KIX",
                    "2026-10-01",
                    "2026-10-06",
                    14,
                    constraint_fingerprint=new_fp,
                )

        self.assertEqual(len(history), 1)
        self.assertEqual(history[0]["total"], 13452)
        self.assertEqual(history[0]["sources"], ["juhe"])

    def test_roundtrip_history_restarts_when_fingerprint_returns_after_other_regime(self):
        fingerprint_a = "a" * 64
        fingerprint_b = "b" * 64
        subscription_id = "roundtrip-a-b-a"
        with tempfile.TemporaryDirectory() as temp_dir:
            db_path = Path(temp_dir) / "prices.db"
            with patch.object(storage, "DB_PATH", db_path):
                for observed_at, price, fingerprint in (
                    ("2026-08-14T09:00:00", 10939, fingerprint_a),
                    ("2026-08-14T17:00:00", 10939, fingerprint_a),
                    ("2026-08-15T12:54:00", 10089, fingerprint_b),
                    ("2026-08-15T15:13:45", 10917, fingerprint_a),
                    ("2026-08-15T15:13:59", 10917, fingerprint_a),
                    ("2026-08-15T17:07:00", 10917, fingerprint_a),
                ):
                    storage.save_roundtrip_snapshot(
                        "上海-大阪",
                        "2026-10-01",
                        "2026-10-06",
                        price / 2,
                        price / 2,
                        price,
                        observed_at,
                        constraint_fingerprint=fingerprint,
                        sources=["juhe"],
                    )
                storage.save_push_snapshot(
                    "上海-大阪",
                    "2026-10-01",
                    "2026-10-06",
                    None,
                    pushed_at="2026-08-15T12:54:00",
                    push_type="无符合方案·备选参考",
                    constraint_fingerprint=fingerprint_b,
                    constraint_sample_n=1,
                    subscription_id=subscription_id,
                )
                boundary = storage.get_constraint_epoch_boundary(
                    "上海-大阪",
                    "2026-10-01",
                    "2026-10-06",
                    fingerprint_a,
                    subscription_id=subscription_id,
                )
                history_limit = storage.get_constraint_history_limit(
                    "上海-大阪",
                    "2026-10-01",
                    "2026-10-06",
                    fingerprint_a,
                    subscription_id=subscription_id,
                )
                history = storage.get_roundtrip_price_history(
                    "上海-大阪",
                    "2026-10-01",
                    "2026-10-06",
                    history_limit,
                    constraint_fingerprint=fingerprint_a,
                    since=boundary,
                )
                storage.save_push_snapshot(
                    "上海-大阪",
                    "2026-10-01",
                    "2026-10-06",
                    10917,
                    pushed_at="2026-08-15T17:07:10",
                    constraint_fingerprint=fingerprint_a,
                    constraint_sample_n=1,
                    subscription_id=subscription_id,
                )
                next_history_limit = storage.get_constraint_history_limit(
                    "上海-大阪",
                    "2026-10-01",
                    "2026-10-06",
                    fingerprint_a,
                    subscription_id=subscription_id,
                )

        self.assertEqual(boundary, "2026-08-15T12:54:00")
        self.assertEqual(history_limit, 1)
        self.assertEqual(next_history_limit, 2)
        self.assertEqual([row["total"] for row in history], [10917])
        self.assertEqual(
            [row["constraint_fingerprint"] for row in history],
            [fingerprint_a],
        )

    def test_oneway_history_restarts_when_fingerprint_returns_after_other_regime(self):
        fingerprint_a = "a" * 64
        fingerprint_b = "b" * 64
        subscription_id = "oneway-a-b-a"
        with tempfile.TemporaryDirectory() as temp_dir:
            db_path = Path(temp_dir) / "prices.db"
            with patch.object(storage, "DB_PATH", db_path), patch.object(
                storage, "datetime"
            ) as mocked_datetime:
                for observed_at, price, fingerprint in (
                    (datetime(2026, 8, 14, 9, 0, 0), 5000, fingerprint_a),
                    (datetime(2026, 8, 15, 12, 54, 0), 7000, fingerprint_b),
                    (datetime(2026, 8, 15, 17, 7, 0), 5100, fingerprint_a),
                ):
                    mocked_datetime.now.return_value = observed_at
                    storage.save_flight_details(
                        "PVG-KIX",
                        "2026-10-01",
                        [_flight(price)],
                        constraint_fingerprint=fingerprint,
                    )
                storage.save_push_snapshot(
                    "PVG-KIX",
                    "2026-10-01",
                    None,
                    None,
                    pushed_at="2026-08-15T12:54:00",
                    push_type="无符合方案·备选参考",
                    constraint_fingerprint=fingerprint_b,
                    constraint_sample_n=1,
                    subscription_id=subscription_id,
                )
                boundary = storage.get_constraint_epoch_boundary(
                    "PVG-KIX",
                    "2026-10-01",
                    None,
                    fingerprint_a,
                    subscription_id=subscription_id,
                )
                history = storage.get_lowest_price_history(
                    "PVG-KIX",
                    "2026-10-01",
                    14,
                    constraint_fingerprint=fingerprint_a,
                    include_metadata=True,
                    since=boundary,
                )

        self.assertEqual(boundary, "2026-08-15T12:54:00")
        self.assertEqual([row["price"] for row in history], [5100])
        self.assertEqual(history[0]["constraint_fingerprint"], fingerprint_a)

    def test_push_snapshot_constraint_comparison_is_subscription_scoped(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            db_path = Path(temp_dir) / "prices.db"
            with patch.object(storage, "DB_PATH", db_path):
                storage.save_push_snapshot(
                    "PVG-KIX",
                    "2026-10-01",
                    "2026-10-06",
                    7387,
                    constraint_fingerprint="a" * 64,
                    subscription_id="sub-a",
                )
                storage.save_push_snapshot(
                    "PVG-KIX",
                    "2026-10-01",
                    "2026-10-06",
                    13452,
                    constraint_fingerprint="b" * 64,
                    subscription_id="sub-b",
                )

                snapshot_a = storage.get_last_push_snapshot(
                    "PVG-KIX",
                    "2026-10-01",
                    "2026-10-06",
                    subscription_id="sub-a",
                )
                snapshot_b = storage.get_last_push_snapshot(
                    "PVG-KIX",
                    "2026-10-01",
                    "2026-10-06",
                    subscription_id="sub-b",
                )

        self.assertEqual(snapshot_a["constraint_fingerprint"], "a" * 64)
        self.assertEqual(snapshot_b["constraint_fingerprint"], "b" * 64)

    def test_no_price_push_snapshot_keeps_constraint_fingerprint(self):
        fingerprint = "b" * 64
        with tempfile.TemporaryDirectory() as temp_dir:
            db_path = Path(temp_dir) / "prices.db"
            with patch.object(storage, "DB_PATH", db_path):
                storage.save_push_snapshot(
                    "上海-大阪",
                    "2026-10-01",
                    "2026-10-06",
                    None,
                    push_type="无符合方案·备选参考",
                    constraint_fingerprint=fingerprint,
                    constraint_sample_n=1,
                    subscription_id="no-primary-example",
                )
                snapshot = storage.get_last_push_snapshot(
                    "上海-大阪",
                    "2026-10-01",
                    "2026-10-06",
                    subscription_id="no-primary-example",
                )

        self.assertIsNotNone(snapshot)
        self.assertIsNone(snapshot["price"])
        self.assertEqual(snapshot["constraint_fingerprint"], fingerprint)
        self.assertEqual(snapshot["constraint_sample_n"], 1)

    def test_zero_subscription_index_is_a_valid_snapshot_identity(self):
        self.assertEqual(
            notifier._notification_subscription_id({}, {"_index": 0}),
            0,
        )
        self.assertEqual(main._subscription_identifier({"_index": 0}, "PVG-KIX"), "0")
        self.assertTrue(
            storage._last_push_key(
                "PVG-KIX",
                "2026-10-01",
                "2026-10-06",
                subscription_id=0,
            ).startswith("subscription:0|")
        )

    def test_zero_subscription_index_survives_payload_persistence(self):
        payload = {
            "snapshot": {
                "route": "PVG-KIX",
                "subscription_id": 0,
                "depart_date": "2026-10-01",
                "return_date": "2026-10-06",
                "channels": [],
            },
            "subscription_id": 0,
            "current_price": 13452,
            "push_type": "筛选条件已变更",
            "recommended_plans": [],
        }

        with patch("notifier.save_last_push_price") as save_last, patch(
            "notifier.save_push_snapshot"
        ) as save_snapshot, patch("notifier.save_pushed_plans") as save_plans:
            notifier.persist_notification_payload(payload)

        self.assertEqual(save_last.call_args.kwargs["subscription_id"], 0)
        self.assertEqual(save_snapshot.call_args.kwargs["subscription_id"], 0)
        self.assertEqual(save_plans.call_args.args[0], 0)

    def test_limits_count_does_not_fall_back_to_legacy_history_for_empty_bucket(self):
        self.assertEqual(
            notifier._history_count_for_limits(
                {"constraint_price_history": []},
                {"price_history": list(range(14))},
                False,
            ),
            0,
        )

    def test_position_and_waiting_risk_accept_fingerprint_history_metadata(self):
        history = [
            {
                "date": f"2026-07-{day:02d}",
                "price": 1000 + day,
                "sources": ["juhe"],
                "constraint_fingerprint": "a" * 64,
            }
            for day in range(1, 11)
        ]

        position = analyzer.price_position_description(1005, history)
        risk = analyzer.waiting_risk_description(history, 1010, 68)

        self.assertEqual(position["data_points"], 10)
        self.assertEqual(position["percentile"], 40)
        self.assertEqual(risk["up_probability"], 100)

    def test_constraint_change_guard_suppresses_cross_fingerprint_market_claims(self):
        change = notifier._constraint_change_context(
            "b" * 64,
            {
                "constraint_fingerprint": "a" * 64,
                "constraint_sample_n": 14,
            },
        )
        push_meta = notifier._apply_constraint_change_to_push_meta(
            {
                "type": "涨价风险",
                "price_change": {"diff": 4534, "direction": "up"},
                "percentile": 93,
                "reasons": [
                    "较上次提醒：上涨¥4,534（上次同口径提醒）",
                    "当前搜索价高于大多数相似历史样本（n=14）",
                ],
            },
            change,
        )
        signal = notifier._apply_constraint_change_to_price_signal(
            {
                "label": "强",
                "summary": "搜索参考价处于近期低位（n=14）",
                "percentile": 93,
                "sample_n": 1,
            },
            change,
        )

        self.assertTrue(change["changed"])
        self.assertEqual(change["previous_sample_n"], 14)
        self.assertIsNone(push_meta["price_change"])
        self.assertIsNone(push_meta["percentile"])
        self.assertEqual(push_meta["type"], "筛选条件已变更")
        self.assertIn("筛选条件已变更", push_meta["reasons"][0])
        self.assertNotIn("上涨", " ".join(push_meta["reasons"]))
        self.assertNotIn("高于大多数", " ".join(push_meta["reasons"]))
        self.assertEqual(signal["label"], "待积累")
        self.assertIsNone(signal["percentile"])
        self.assertIn("同条件样本重新积累", signal["summary"])
        tracking_text = notifier._plan_status_change_text(
            {
                "constraint_change": change,
                "plan_status_change": {"msg": "较上次上涨¥4,534"},
            }
        )
        self.assertIn("筛选条件已变更", tracking_text)
        self.assertNotIn("上涨", tracking_text)

    def test_unchanged_fingerprint_keeps_price_signal_copy(self):
        fingerprint = "a" * 64
        change = notifier._constraint_change_context(
            fingerprint,
            {
                "constraint_fingerprint": fingerprint,
                "constraint_sample_n": 14,
            },
        )
        signal = {
            "label": "强",
            "summary": "搜索参考价处于近期低位（n=14）",
            "percentile": 20,
            "sample_n": 14,
        }

        self.assertFalse(change["changed"])
        self.assertEqual(
            notifier._apply_constraint_change_to_price_signal(signal, change),
            signal,
        )

    def test_oneway_trend_and_provenance_use_constraint_bucket_history(self):
        fingerprint_history = [
            {
                "date": "2026-07-25",
                "price": 13452,
                "sources": ["juhe"],
            }
        ]
        legacy_history = [
            {
                "date": "2026-07-24",
                "price": 7387,
                "sources": ["hasdata"],
            }
        ]

        chart_history = notifier._chart_history_for_message(
            {},
            {"constraint_price_history": fingerprint_history},
            {"price_history": legacy_history},
            False,
        )
        metadata = notifier._price_signal_provenance_metadata(
            fingerprint_history,
            {"price_history": legacy_history},
            {"constraint_price_history": fingerprint_history},
            False,
        )

        self.assertEqual(chart_history, fingerprint_history)
        self.assertEqual(metadata["sources"], ["juhe"])
        self.assertEqual(metadata["_provenance_history"], fingerprint_history)

    def test_empty_constraint_bucket_never_falls_back_to_legacy_history(self):
        analysis = {"constraint_price_history": []}
        legacy_history = [{"date": "2026-07-24", "price": 7387}]

        self.assertEqual(
            notifier._price_history_for_push(
                {"price_history": legacy_history},
                analysis,
                False,
            ),
            [],
        )
        self.assertEqual(
            notifier._chart_history_for_message(
                {},
                analysis,
                {"price_history": legacy_history},
                False,
            ),
            [],
        )
        metadata = notifier._price_signal_provenance_metadata(
            [],
            {"price_history": legacy_history},
            analysis,
            False,
        )
        self.assertEqual(metadata["_provenance_history"], [])
        self.assertEqual(metadata["sample_n"], 0)

    def test_oneway_reference_tiers_use_constraint_bucket_history(self):
        fingerprint_history = [
            {
                "date": "2026-07-25",
                "price": 13452,
                "sources": ["juhe"],
            }
        ]
        legacy_history = [
            {
                "date": "2026-07-24",
                "price": 7387,
                "sources": ["hasdata"],
            }
        ]
        analysis = {
            "recommendations": [
                {
                    "flight_no": "NH970",
                    "flight_combo": "NH970",
                    "price": 13452,
                    "stops": 0,
                    "price_source": "juhe",
                }
            ],
            "price_range": [13452, 13452],
            "constraint_price_history": fingerprint_history,
            "days_to_dept": 68,
        }
        with patch(
            "notifier.get_last_push_price",
            return_value=None,
        ), patch(
            "notifier.get_last_push_snapshot",
            return_value=None,
        ), patch(
            "notifier.track_plan_status",
            return_value=None,
        ), patch(
            "notifier.calculate_price_references",
            return_value={},
        ) as mocked_references, contextlib.redirect_stdout(io.StringIO()):
            notifier.build_notification_payload(
                analysis,
                route_info={
                    "origin": "PVG",
                    "destination": "KIX",
                    "depart_date": "2026-10-01",
                    "route_type": "international",
                },
                subscription={
                    "id": "oneway-reference-fingerprint",
                    "basic": {
                        "route_type": "international",
                        "passenger_count": 1,
                    },
                    "preferences": {
                        "passengers": {
                            "adult": 1,
                            "child": 0,
                            "elderly": 0,
                            "infant": 0,
                        }
                    },
                },
                price_insights={"price_history": legacy_history},
            )

        history_argument = mocked_references.call_args.args[1]
        self.assertEqual(len(history_argument), 1)
        self.assertEqual(history_argument[0][1], 13452.0)

    def test_payload_constraint_change_suppresses_all_cross_bucket_claims(self):
        from constraint_fingerprint import constraint_fingerprint

        current_fp = constraint_fingerprint({"lcc_policy": "exclude_lcc"})
        previous_fp = constraint_fingerprint({"lcc_policy": "any"})
        analysis = {
            "round_trip_analysis": {
                "top_combinations": [
                    {
                        "outbound": {
                            "flight_no": "NH970",
                            "flight_combo": "NH970",
                            "price": 6500,
                            "stops": 0,
                            "price_source": "juhe",
                        },
                        "return": {
                            "flight_no": "JL891",
                            "flight_combo": "JL891",
                            "price": 6952,
                            "stops": 0,
                            "price_source": "juhe",
                        },
                        "outbound_price": 6500,
                        "return_price": 6952,
                        "total_price": 13452,
                    }
                ],
                "total_min": 13452,
                "history": [
                    {
                        "date": "2026-07-25",
                        "total": 13452,
                        "sources": ["juhe"],
                        "constraint_fingerprint": current_fp,
                    }
                ],
            },
            "constraint_fingerprint": current_fp,
            "decision": {"conclusion": "可以观察", "confidence": "中"},
            "days_to_dept": 68,
        }
        route_info = {
            "subscription_id": "constraint-change-integration",
            "round_trip": True,
            "origin": "PVG",
            "destination": "KIX",
            "depart_date": "2026-10-01",
            "return_date": "2026-10-06",
            "target_price": 6000,
            "max_budget": 8000,
            "route_type": "international",
            "constraint_fingerprint": current_fp,
        }
        subscription = {
            "id": "constraint-change-integration",
            "lcc_policy": "exclude_lcc",
            "basic": {"route_type": "international", "passenger_count": 1},
            "preferences": {
                "passengers": {
                    "adult": 1,
                    "child": 0,
                    "elderly": 0,
                    "infant": 0,
                }
            },
            "constraints": {
                "budget_scope": "per_person",
                "max_budget_scope": "per_person",
                "target_price_scope": "per_person",
                "target_price": 6000,
                "max_budget": 8000,
                "lcc_policy": "exclude_lcc",
            },
        }
        previous_snapshot = {
            "price": None,
            "constraint_fingerprint": previous_fp,
            "constraint_sample_n": 1,
            "channels": '["hasdata", "juhe"]',
            "push_type": "无符合方案·备选参考",
        }
        output = io.StringIO()
        with patch(
            "notifier.get_last_push_price",
            return_value={"price": 7387},
        ), patch(
            "notifier.get_last_push_snapshot",
            return_value=previous_snapshot,
        ), patch(
            "notifier.track_plan_status",
            return_value={
                "status": "price_changed",
                "msg": "较上次上涨¥4,534",
            },
        ), contextlib.redirect_stdout(output):
            payload = notifier.build_notification_payload(
                analysis,
                route_info=route_info,
                subscription=subscription,
            )

        disclosure = "筛选条件已变更，旧条件样本(n=1)不再计入，同条件样本重新积累"
        self.assertEqual(payload["push_type"], "筛选条件已变更")
        self.assertIn(disclosure, payload["price_signal"]["summary"])
        self.assertIn("同条件样本不足（当前n=1）", payload["price_signal"]["summary"])
        self.assertEqual(payload["trend_summary"], disclosure)
        self.assertFalse(payload["diff_from_last"]["comparable"])
        visible_history_text = " ".join(
            [
                payload["push_type"],
                payload["price_signal"]["summary"],
                payload["trend_summary"],
                notifier._plan_status_change_text(payload),
                *payload["trigger_reason"],
            ]
        )
        self.assertNotIn("上涨", visible_history_text)
        self.assertNotIn("高于大多数", visible_history_text)
        self.assertIn("同条件样本重新积累", visible_history_text)

    def test_provenance_bucket_contains_constraint_short_fingerprint(self):
        payload = attach_payload_provenance(
            {
                "route": "上海 → 大阪",
                "depart_date": "2026-10-01",
                "return_date": "2026-10-06",
                "constraint_fingerprint": "12345678" + "a" * 56,
                "price_signal": {
                    "label": "待积累",
                    "summary": "同条件样本不足",
                    "sample_n": 1,
                    "percentile": 0,
                    "sources": ["juhe"],
                },
                "price_history": [{"date": "2026-07-25", "price": 13452}],
            },
            context={},
            computed_at="2026-07-25T10:00:00+08:00",
        )

        bucket = payload["price_signal"]["provenance"]["bucket"]
        self.assertIn("约束=12345678", bucket)
        self.assertEqual(
            payload["price_signal"]["provenance"]["sources"],
            ["juhe"],
        )


class ConstraintScopeCopyTest(unittest.TestCase):
    def setUp(self):
        self.passengers = {
            "adult": 2,
            "child": 1,
            "elderly": 2,
            "infant": 0,
        }
        self.display = build_display_prices(
            7000,
            6452,
            self.passengers,
            "international",
        )
        self.plan = {
            "label": "方案A",
            "tier": "首选方案",
            "is_roundtrip": True,
            "price": self.display["total"],
            "outbound_price": 7000,
            "return_price": 6452,
            "route_type": "international",
            "passenger_pricing": {
                "applies": True,
                "passengers": self.passengers,
                "factor": 4.75,
                "route_type": "international",
            },
            "price_tiers": {
                "unit_roundtrip": 13452,
                "total_roundtrip_ref": self.display["total"],
                "passenger_count": 5,
                "passenger_label": "2成人+1儿童+2老人",
                "route_type": "international",
            },
        }

    def test_plan_price_comparison_uses_canonical_all_passenger_scope(self):
        rows = notifier._payload_plan_price_rows([self.plan])
        html = notifier._payload_bar_html("方案价格对比", rows)

        self.assertEqual(rows[0]["value"], self.display["total"])
        self.assertEqual(rows[0]["scope"], "all_passengers_roundtrip")
        self.assertIn("全员往返", html)
        self.assertNotIn(
            f"{self.display['total']:,.0f} 单人往返",
            html,
        )

    def test_action_panel_names_full_party_reference_price(self):
        text = notifier._email_primary_plan_line(
            {"display_price": self.display["total"]},
            self.plan,
        )

        self.assertIn("全员参考价", text)
        self.assertIn("费率合计4.75×单人", text)
        self.assertNotIn("搜索参考价", text)

    def test_exclusion_basis_includes_active_lcc_constraint(self):
        basis = analyzer._roundtrip_exclusion_basis(
            {
                "lcc_policy": "exclude_lcc",
            }
        )

        self.assertIn("排除廉航", basis)


if __name__ == "__main__":
    unittest.main()
