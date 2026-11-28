"""R41: verified empty responses across collection, read-only use and delivery."""

import ast
from contextlib import ExitStack, redirect_stdout
from copy import deepcopy
from datetime import date, datetime, timedelta, timezone
from hashlib import sha256
import inspect
import io
import json
import os
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

import analyzer
import collection_plan
import config_loader
import health_check
import main
import notifier
import plan_tracker
import request_cache
import storage
import tracker
import test_legacy_notification_renderer_retirement as renderer_guard
from sources import aggregator as aggregation
from tests import test_multi_airport_full_coverage as matrix_fixture
from tests import test_low_price_alert_state as low_price_guard
from tests import test_r40_same_day_combo_contracts as r40_guard
from tests import test_return_phase_processing as scenario
from tests.test_return_phase_wording import R41_REVERT


ROOT = Path(__file__).resolve().parents[1]
DAY, RETURN, STAMP = matrix_fixture.DAY, matrix_fixture.RETURN, matrix_fixture.STAMP
TODAY = date(2026, 9, 30)
GO = ("PVG", "KIX")
BACK = GO[::-1]
SUB_ID = "123e4567-e89b-42d3-a456-426614174000"
MISSING = "返程本次未取得完整查询证据"
SINGLE = "本次查询完成，所查来源未返回报价"
RETURN_ONLY = "返程阶段查询完成，所查来源未返回返程报价"
EMPTY_CASES = {
    ("covered_empty", "quoted"): "去程所查来源未返回报价；返程已有报价，本轮无法形成完整往返方案",
    ("quoted", "covered_empty"): "去程已有报价；返程所查来源未返回报价，本轮无法形成完整往返方案",
    ("covered_empty", "covered_empty"): "去程与返程所查来源均未返回报价，本轮无法形成完整往返方案",
    ("covered_empty", "panel_uncollected"): "去程所查来源未返回报价；" + MISSING,
    ("quoted", "panel_uncollected"): "去程已有报价；" + MISSING,
}
FORBIDDEN = ("售罄", "停飞", "市场无票", "价格下降", "可能是采集覆盖问题", "采集覆盖可能不完整",
             "没有当日到达的有效去程航班", "建议前一晚到达", "本轮OTA交叉源不可用", "提供0个备选", "¥0")


def _empty_raw(**overrides):
    return {"source_status": "empty", "raw": {"error_code": 0, "result": []},
            "flights": [], "raw_result_count": 0, **overrides}


class _FakeMatrix(matrix_fixture.MatrixAggregator):
    def __init__(self, states):
        super().__init__(states, count=1)
        self.live_calls = []

    def collect(self, origin, destination, day, cabin_classes=None, passengers=None,
                route_type=None, request_reason=None, panel_only=None):
        result = super().collect(origin, destination, day, cabin_classes, passengers,
                                 route_type, request_reason, panel_only)
        state = self.states.get((origin, destination, day), self.states.get((origin, destination), "quoted"))
        if state == "missing":
            self.last_fetch_evidence_units[0].update(source_status="skipped_panel_only", collection_state="panel_missing",
                                                     collected_at=None)
        if panel_only is not True:
            self.live_calls.append((origin, destination, day))
        self.last_source_stats = {"juhe": {"count": len((result or {}).get("flights", [])),
                                          "status": "empty" if state == "empty" else state}}
        return result


class _IsolatedCase(unittest.TestCase):
    def setUp(self):
        self.stack = ExitStack()
        self.addCleanup(self.stack.close)
        self.root = Path(self.stack.enter_context(TemporaryDirectory()))
        self.stack.enter_context(patch.dict(os.environ, {"NO_LIVE_API": "1"}))
        for module, attr, rel in (
            (storage, "DB_PATH", "prices.db"), (plan_tracker, "DEFAULT_DATA_DIR", "plans"),
            (plan_tracker, "DEFAULT_FEEDBACK_PATH", "feedback.json"),
            (config_loader, "RUNTIME_CONFIG_PATH", "missing.yaml"), (main, "ANALYSIS_LOG", "analysis.jsonl"),
            (health_check, "SOURCE_HEALTH_PATH", "source_health.json"), (tracker, "SIGNALS_LOG", "signals.jsonl"),
        ):
            self.stack.enter_context(patch.object(module, attr, self.root / rel))
        self.stack.enter_context(patch.object(aggregation, "safe_log"))
        self.stack.enter_context(patch.object(request_cache, "safe_log"))
        self.stack.enter_context(patch.object(main, "safe_log"))
        self.stack.enter_context(patch.object(notifier, "shanghai_today", return_value=TODAY))
        request_cache.reset_for_tests(self.root / "cache")
        self.addCleanup(request_cache.reset_for_tests, None)

    def helper(self, module, name):
        self.assertTrue(hasattr(module, name), f"R41 contract helper missing: {name}")
        return getattr(module, name)

    def matrix(self, states, *, origins=("PVG",), destinations=("KIX",), day=DAY, direction="outbound", panel_only=None):
        with redirect_stdout(io.StringIO()):
            return main.collect_for_airport_matrix(_FakeMatrix(states), list(origins), list(destinations), day,
                                                   direction=direction, cabin_classes=["economy"], panel_only=panel_only)

    def carriers(self, payload):
        with redirect_stdout(io.StringIO()):
            return (notifier.render_email(payload)[1], notifier.render_detail_html(payload),
                    notifier.prepare_push_render(notifier.render_pushplus_sections(payload)).content)

    def payload(self, going="covered_empty", returning=None, *, return_only=False, privacy=None, coverage=None):
        route = {"origin": "PVG", "destination": "KIX", "depart_date": DAY, "subscription_id": SUB_ID,
                 "round_trip": returning is not None, "return_date": RETURN, "collected_at": STAMP}
        if return_only:
            route.update(processing_phase="return_only", phase_label="返程阶段")
        if privacy:
            route["notification_goals"] = {"notification_privacy_level": privacy}
        outward = {"collection_outcome": going, "airport_coverage": coverage or {}, "all_flights": []}
        if returning is not None:
            outward["return_collection_outcome"] = returning
        backward = {"collection_outcome": returning} if returning else None
        return notifier.build_notification_payload(outward, return_analysis=backward, route_info=route)

    def run_subscription(self, matrix, *, sub=None, today=TODAY, delivered=True, extra=None, managed=False):
        captured = []
        sub = deepcopy(sub or scenario.subscription())
        clock = type("R41ScenarioClock", (scenario.FixedDatetime,), {"today": today})
        patches = {
            "_shanghai_today": Mock(return_value=today), "datetime": clock,
            "_departure_reference_now": Mock(return_value=datetime(2026, 9, 30, 16, tzinfo=timezone(timedelta(hours=8)))),
            "build_default_sources": Mock(return_value=(matrix.search_sources, [])),
            "FlightAggregator": Mock(return_value=matrix),
            "get_constraint_epoch_boundary": Mock(return_value=None), "get_constraint_history_limit": Mock(return_value=14),
            "get_previous_snapshot_prices": Mock(return_value={}), "get_lowest_price_history": Mock(return_value=[]),
            "get_roundtrip_price_history": Mock(return_value=[]), "save_raw_response": Mock(),
            "save_flight_details": Mock(), "save_roundtrip_snapshot": Mock(), "log_signal": Mock(return_value={}),
            "update_calendar": Mock(return_value={}), "_notification_tcurve": Mock(return_value={}),
            "_notification_forecast": Mock(return_value={}), "_notification_provenance_context": Mock(return_value={}),
            "_deliver_notification": Mock(side_effect=lambda s, r, options: captured.append(options) or delivered),
            "_log_subscription_failure": Mock(),
        }
        if managed:
            plan = Mock(request_keys=set(), panel_only_keys=set(), freshness_hours=6, fresh_scope="all")
            patches.update({name: Mock() for name in (
                "start_round_log_archive", "end_round_log_archive", "start_request_cache_round", "print_request_cache_stats",
                "activate_collection_plan", "deactivate_collection_plan", "set_current_round", "reset_current_round",
                "log_retention_dry_run",
            )})
            patches.update(build_collection_plan=Mock(return_value=plan),
                           _collection_plan_log_options=Mock(return_value={"fresh_scope": "all"}))
        patches.update(extra or {})
        with ExitStack() as stack, redirect_stdout(io.StringIO()):
            for name, value in patches.items():
                stack.enter_context(patch.object(main, name, value))
            for module in (analyzer, collection_plan):
                stack.enter_context(patch.object(module, "shanghai_today", return_value=today))
            ok = main._process_subscription_locked(sub, ensure_db=False, manage_collection_round=managed,
                                                   collection_round_id="collection_synthetic")
        return ok, captured, patches


class TestTrack1EmptySchemaAndSnapshotIsolation(_IsolatedCase):
    def test_g01_empty_schema_requires_source_specific_container_and_success_metadata(self):
        validate = self.helper(aggregation, "_validate_raw_empty_schema")
        accepted = {
            "juhe": [{"error_code": 0, "result": []}, {"resultcode": "200", "result": {"flightInfo": []}},
                     {"error_code": "0", "data": []}],
            "serpapi": [{"best_flights": []}, {"other_flights": [], "search_metadata": {"status": "Success"}}],
            "searchapi": [{"best_flights": []}, {"other_flights": [], "search_metadata": None}],
            "hasdata": [{"bestFlights": []}],
        }
        rejected = {
            "juhe": [{}, {"error_code": 0}, {"result": []}, {"error_code": 200, "result": []},
                     {"error_code": True, "result": []}, {"error_code": 0.0, "result": []},
                     {"error_code": 0, "resultcode": 500, "result": []}, {"error_code": 0, "result": [1]},
                     {"error_code": 0, "result": {"flightInfo": ""}}],
            "hasdata": [{}, {"bestFlights": {}}], "unknown": [{"best_flights": []}],
        }
        for source in ("serpapi", "searchapi"):
            rejected[source] = [{}, {"note": "no results"}, {"best_flights": [1]}, {"other_flights": None},
                                {"best_flights": [], "search_metadata": {"status": "Error"}},
                                *({"best_flights": [], "search_metadata": value} for value in ("Success", [], 0))]
        for cases, expected in ((accepted, True), (rejected, False)):
            for source, payloads in cases.items():
                for raw in payloads:
                    with self.subTest(source=source, raw=raw):
                        self.assertIs(validate(source, _empty_raw(raw=raw)), expected)

    def test_g02_discarded_rows_are_unusable_and_round_cache_label_is_not_evidence(self):
        source = matrix_fixture.Source()
        agg = aggregation.FlightAggregator([source], [])
        for rows, reported, status, expected in (
            ([], 2, "fresh", "data_unusable"), ([{"price": 0}], 1, "fresh", "data_unusable"),
            ([{"price": 100}], 1, "fresh", "data_unusable"), ([], 0, "round_empty", "data_unusable"),
        ):
            result = _empty_raw(raw={"error_code": 0, "result": [1]} if reported else {}, flights=rows,
                                raw_result_count=reported)
            with self.subTest(rows=rows, status=status), patch.object(aggregation, "cached_fetch", return_value=(result, status)), \
                    redirect_stdout(io.StringIO()):
                data = main.collect_for_airport_matrix(agg, [GO[0]], [GO[1]], DAY, cabin_classes=["economy"])
                self.assertIsInstance(data, dict)
                self.assertEqual(data.get("collection_outcome"), expected)

    def test_g03_scope_snapshots_clear_before_errors_and_keep_original_timestamps(self):
        source = matrix_fixture.Source()
        agg = aggregation.FlightAggregator([source], [])
        for status, state, stamp in (("round_empty", "fresh", None), ("panel", "panel_reused", STAMP),
                                     ("cache", "cache_reused", STAMP), ("skipped", "panel_missing", None),
                                     ("fresh", "fresh", None)):
            result = _empty_raw(collection_state=state, collected_at=stamp)
            with self.subTest(status=status), patch.object(aggregation, "cached_fetch", return_value=(result, status)), \
                    redirect_stdout(io.StringIO()):
                self.assertIsNone(agg.collect(*GO, DAY))
                self.assertTrue(hasattr(agg, "last_call_snapshot"))
                row = agg.last_call_snapshot["fetch_evidence_units"][0]
                self.assertEqual(row["call_scope"], agg.last_call_scope)
                if status == "fresh":
                    self.assertTrue(row["collected_at"].endswith("+08:00"))
                else:
                    self.assertEqual(row["collected_at"], stamp)
        key = request_cache.cache_key(source, *GO, DAY, None, "economy")
        outcome = SimpleNamespace(request_key=key, execution_status="empty", result=_empty_raw(collected_at=STAMP))
        self.assertIsNone(agg.collect_from_outcomes(*GO, DAY, [outcome]))
        self.assertEqual(getattr(agg, "last_collected_at", None), STAMP)
        with self.assertRaises(aggregation.DuplicateRequestOutcome):
            agg.collect_from_outcomes(*BACK, RETURN, [outcome, outcome])
        self.assertEqual(getattr(agg, "last_fetch_evidence_units", None), [])
        self.assertIsNone(getattr(agg, "last_call_snapshot", object()))
        scope = getattr(agg, "last_call_scope", None)
        self.assertIsInstance(scope, dict)
        self.assertEqual(scope.get("origin"), BACK[0])
        with self.assertRaises(aggregation.MissingRequestOutcome):
            agg.collect_from_outcomes(*BACK, RETURN, [])
        self.assertEqual(getattr(agg, "last_source_errors", None), [])
        self.assertIsNone(getattr(agg, "last_collected_at", object()))
        with patch.object(aggregation, "cached_fetch", return_value=(_empty_raw(collected_at=STAMP), "round_empty")):
            matrix = main.collect_for_airport_matrix(agg, ["PVG", "SHA"], ["KIX"], DAY)
        self.assertIsInstance(matrix, dict)
        self.assertIsInstance(matrix.get("fetch_evidence_units"), list)
        units = getattr(agg, "last_fetch_evidence_units", None)
        self.assertIsInstance(units, list)
        self.assertTrue(units)
        units[0]["collected_at"] = "changed"
        self.assertEqual([row["collected_at"] for row in matrix["fetch_evidence_units"]], [STAMP, STAMP])
        self.assertEqual(len({row["call_scope"]["call_id"] for row in matrix["fetch_evidence_units"]}), 2)

    def test_g04_structured_empty_scope_and_fail_closed_classifier(self):
        data = self.matrix({GO: "empty"})
        self.assertIsInstance(data, dict)
        self.assertEqual((data.get("count"), data.get("min_price"), data.get("collection_outcome")), (0, None, "covered_empty"))
        self.assertEqual(data["query_scope"]["planned_pairs"], [list(GO)])
        classify = self.helper(main, "_classify_leg_collection_outcome")
        for invalid in ({"flights": []}, {"flights": [], "airport_coverage": {"complete": True}},
                        {"flights": [], "source_errors": []}, {**data, "fetch_evidence_units": []},
                        {**data, "source_errors": [{"source": "juhe", "error": "offline failure"}]}):
            self.assertNotEqual(classify(invalid), "covered_empty")

    def test_g05_partial_matrix_and_filtered_prices_are_not_legitimate_empty(self):
        for bad in ("failed", "quota"):
            data = self.matrix({GO: "empty", ("SHA", "KIX"): bad}, origins=("PVG", "SHA"))
            self.assertIsInstance(data, dict)
            self.assertEqual(data.get("collection_outcome"), "incomplete_coverage")
            self.assertEqual(data["airport_coverage"]["covered_pairs"], [list(GO)])
        with patch.object(matrix_fixture, "quote", return_value=matrix_fixture.quote("SHA", "KIX", DAY)):
            filtered = self.matrix({GO: "quoted"})
        self.assertEqual(filtered.get("collection_outcome"), "filtered_empty")
        self.assertEqual((filtered["pre_filter_flight_count"], filtered["airport_filtered_flights"]), (1, 1))

    def test_g06_health_preserves_streak_only_for_verified_empty(self):
        data = self.matrix({GO: "empty"})
        self.assertIsInstance(data, dict)
        source_health = self.root / "source_health.json"
        source_health.write_text('{"juhe":{"consecutive_failures":2}}', encoding="utf-8")
        report = health_check.system_health_check(data["source_stats"], [], route_type="international")
        self.assertEqual(json.loads(source_health.read_bytes())["juhe"]["consecutive_failures"], 2)
        self.assertNotIn("数据覆盖不足", report["warnings"])
        self.assertNotIn("_airport_coverage", json.loads(source_health.read_bytes()))
        missing = self.matrix({BACK: "missing"}, origins=(BACK[0],), destinations=(BACK[1],),
                              direction="return", day=RETURN, panel_only=True)
        stats = deepcopy(data["source_stats"])
        stats["_airport_coverage"]["return"] = missing["airport_coverage"]
        report = health_check.system_health_check(stats, [], route_type="international")
        self.assertIs(report["coverage_complete"], False)
        self.assertEqual(json.loads(source_health.read_bytes())["juhe"]["consecutive_failures"], 2)
        for status in ("empty", "failed", "data_unusable"):
            history, _ = health_check._update_source_history({"juhe": {"status": status, "count": 0}})
            self.assertEqual(history["juhe"]["consecutive_failures"], ("empty", "failed", "data_unusable").index(status) + 3)


class TestTrack2ReadOnlyContinuationAndScopeAccuracy(_IsolatedCase):
    def test_g07_panel_only_wins_over_force_fresh_and_keeps_existing_hits(self):
        source = matrix_fixture.Source()
        source.fetch.return_value = _empty_raw(collected_at=STAMP)
        with patch.object(request_cache, "panel_reuse_result", return_value=None) as panel, \
                patch.object(request_cache, "_archive_listing_result"):
            skipped = request_cache.cached_fetch(source, *GO, DAY, panel_only=True, force_fresh=True, persist=False)
            self.assertEqual(skipped.get("source_status"), "skipped_panel_only")
            source.fetch.assert_not_called()
            panel.assert_called_once()
            panel.reset_mock()
            request_cache.start_request_cache_round("synthetic-r41")
            result = request_cache.cached_fetch(source, *GO, DAY, force_fresh=True, persist=False)
            self.assertEqual(result.get("source_status"), "empty")
            panel.assert_not_called()
            source.fetch.assert_called_once()
            result = request_cache.cached_fetch(source, *GO, DAY, panel_only=True, force_fresh=True, persist=False)
            self.assertEqual(result.get("collected_at"), STAMP)
            source.fetch.assert_called_once()
        request_cache.reset_for_tests(self.root / "cache")
        with patch.object(request_cache, "panel_reuse_result", return_value=_empty_raw(collected_at=STAMP)) as panel:
            result, status = request_cache.cached_fetch(source, *GO, DAY, panel_only=True, force_fresh=True,
                                                       include_cache_status=True, persist=False)
            self.assertEqual((status, result.get("collected_at")), ("panel", STAMP))
            panel.assert_called_once()
        source.fetch.assert_called_once()

    def test_g08_panel_only_keeps_verified_empty_distinct_from_missing(self):
        empty = self.matrix({GO: "empty"}, panel_only=True)
        missing = self.matrix({GO: "missing"}, panel_only=True)
        for data, outcome in ((empty, "covered_empty"), (missing, "panel_uncollected")):
            self.assertIsInstance(data, dict)
            self.assertEqual(data.get("collection_outcome"), outcome)
            self.assertEqual(data["airport_coverage"]["pair_records"][0]["state"], outcome)

    def test_g09_managed_roundtrip_reports_both_legs_without_supplemental_fetches(self):
        for first, second in (("empty", "reused"), ("empty", "empty"), ("quoted", "empty")):
            with self.subTest(outbound=first, inbound=second):
                agg = _FakeMatrix({GO: first, BACK: second})
                ok, captured, effects = self.run_subscription(agg)
                self.assertTrue(ok)
                self.assertEqual(len(captured), 1)
                request = captured[0]
                go_state = "covered_empty" if first == "empty" else "quoted"
                return_state = "covered_empty" if second == "empty" else "quoted"
                self.assertIsInstance(request.get("return_analysis"), dict)
                self.assertEqual(request["return_analysis"].get("collection_outcome"), return_state)
                self.assertFalse(request["analysis_result"].get("collection_failures"))
                self.assertEqual(len(agg.live_calls), 1 if first == "empty" else 2)
                for name in ("save_flight_details", "save_roundtrip_snapshot", "log_signal", "update_calendar"):
                    effects[name].assert_not_called()
                payload = notifier.build_notification_payload(**request)
                for carrier in self.carriers(payload):
                    self.assertIn(EMPTY_CASES[go_state, return_state], carrier)

    def test_g10_missing_return_is_failure_evidence_not_legitimate_empty(self):
        for first in ("empty", "quoted"):
            with self.subTest(outbound=first):
                agg = _FakeMatrix({GO: first, BACK: "missing"})
                ok, captured, _ = self.run_subscription(agg)
                self.assertTrue(ok)
                self.assertTrue(captured)
                request = captured[0]
                self.assertIsNone(request["return_analysis"])
                failures = request["analysis_result"].get("collection_failures") or []
                self.assertEqual([x.get("return_outcome") for x in failures],
                                 ["panel_uncollected" if first == "empty" else "incomplete_coverage"])
                if first == "empty":
                    self.assertEqual(agg.live_calls, [(*GO, DAY)])
                payload = notifier.build_notification_payload(**request)
                self.assertTrue(notifier._data_incomplete_state(payload))
                for carrier in self.carriers(payload):
                    self.assertIn(MISSING, carrier)
                    self.assertNotIn("返程所查来源未返回报价", carrier)

    def test_g11_continuation_blocks_calendar_flexible_and_same_day_fallback_requests(self):
        sub = scenario.subscription()
        sub.update(date_flexibility=1, return_date_flexibility=1, same_day_round_trip=True, fresh_scope="all")
        agg = _FakeMatrix({GO: "empty", BACK: "reused"})
        fallback = Mock(return_value=[])
        combine = Mock(return_value={"same_day_time_conflict": True})
        ok, captured, effects = self.run_subscription(agg, sub=sub, extra={
            "_collect_same_day_fallback_flights": fallback, "analyze_round_trip": combine,
        }, managed=True)
        self.assertTrue(ok)
        self.assertEqual(agg.live_calls, [(*GO, DAY)])
        self.assertGreater(len(agg.calls), 2)
        self.assertTrue(all(call[-1] is True for call in agg.calls[1:]))
        fallback.assert_not_called()
        combine.assert_not_called()
        effects["update_calendar"].assert_not_called()
        self.assertTrue(all("建议前一晚到达" not in s for s in self.carriers(notifier.build_notification_payload(**captured[0]))))

        quoted = _FakeMatrix({GO: "quoted", BACK: "quoted"})
        combine = Mock(return_value={"same_day_time_conflict": True, "combinations": []})
        ok, _, _ = self.run_subscription(quoted, sub=sub, extra={
            "_collect_same_day_fallback_flights": fallback, "analyze_round_trip": combine,
        }, managed=True)
        self.assertTrue(ok)
        self.assertEqual([call.args[3] for call in fallback.call_args_list], ["2026-09-30", "2026-10-07"])

    def test_g12_return_only_verified_empty_obeys_delivery_receipt(self):
        for delivered in (True, False):
            with self.subTest(delivered=delivered):
                agg = _FakeMatrix({BACK: "empty"})
                ok, captured, effects = self.run_subscription(agg, today=date(2026, 10, 2), delivered=delivered)
                self.assertIs(ok, delivered)
                self.assertTrue(captured)
                request = captured[0]
                self.assertEqual(request["analysis_result"].get("low_price_alert_state"), "not_applicable")
                self.assertEqual(request["route_info"].get("processing_phase"), "return_only")
                effects["save_flight_details"].assert_not_called()
                effects["save_roundtrip_snapshot"].assert_not_called()
                if not delivered:
                    self.assertEqual(effects["_log_subscription_failure"].call_args.kwargs.get("reason"), "返程通知未发送成功")
                for carrier in self.carriers(notifier.build_notification_payload(**request)):
                    self.assertIn(RETURN_ONLY, carrier)

    def test_g13_flexible_dates_and_calendars_keep_verified_empty_separate(self):
        sub = scenario.subscription()
        sub["date_flexibility"] = 1
        rows = []
        for state, expected in (("empty", "covered_empty"), ("unusable", "data_unusable"), ("missing", "panel_missing"),
                                ("failed", "incomplete_coverage"), ("quoted", "filtered_empty")):
            quote = (patch.object(matrix_fixture, "quote", return_value=matrix_fixture.quote("SHA", "KIX", DAY))
                     if state == "quoted" else ExitStack())
            agg = _FakeMatrix({GO: state})
            panel = state == "missing"
            with quote, patch.object(main, "FlightAggregator", return_value=agg), redirect_stdout(io.StringIO()):
                entries = main.collect_nearby_dates(agg, sub, fresh_scope="primary_only" if panel else "all")
            actual = [item for item in entries if item["offset"] != 0]
            self.assertTrue(actual)
            self.assertTrue(all(call[-1] is panel for call in agg.calls))
            self.assertEqual({item["collection_state"] for item in actual}, {expected})
            self.assertTrue(all(item["today_uncollected"] is panel for item in actual))
            if state in {"missing", "empty"}:
                rows.append(actual[int(panel)])
        calendar = notifier._payload_price_calendar({**sub, "nearby_dates": rows}, {})
        for content in (notifier._email_price_calendar_body({"price_calendar": calendar}),
                        "\n".join(notifier._pushplus_calendar_summary_lines({"price_calendar": calendar}))):
            self.assertIn("已查询，未返回报价", content)
            self.assertIn("今日未采", content)


class TestTrack3LowPriceDeliveryAndThreeRoundBaseline(_IsolatedCase):
    def test_g14_low_price_empty_is_unknown_and_notice_is_recorded_only_after_acceptance(self):
        for going, returning, expected in (("empty", None, "no_valid_offers"), ("empty", "reused", "no_valid_offers"),
                                            ("quoted", "empty", "no_valid_offers"),
                                            ("empty", "missing", "source_coverage_incomplete")):
            with self.subTest(going=going, returning=returning):
                sub = scenario.subscription()
                sub.update(id=SUB_ID, subscription_id=SUB_ID, round_trip=returning is not None, budget_strategy="low_price_alert")
                if not returning:
                    sub.pop("return_date")
                    sub["hard_constraints"] = {"round_trip": False, "budget_strategy": "low_price_alert"}
                else:
                    sub["hard_constraints"]["budget_strategy"] = "low_price_alert"
                handler = Mock()
                sample = Mock(side_effect=AssertionError("empty offer wrote a sample"))
                ok, captured, effects = self.run_subscription(_FakeMatrix({GO: going, BACK: returning}), sub=sub,
                    managed=True, extra={"_handle_low_price_unknown": handler, "evaluate_and_persist_low_price_alert": sample})
                self.assertTrue(ok)
                if returning == "missing":
                    self.assertEqual(len(captured), 1)
                    handler.assert_not_called()
                    analysis = captured[0]["analysis_result"]
                    self.assertTrue(analysis.get("collection_failures"))
                    context = analysis.get("low_price_alert_context") or {}
                    self.assertIs(context.get("coverage_complete"), False)
                else:
                    self.assertEqual(captured, [])
                    handler.assert_called_once()
                    context = handler.call_args.args[1].get("low_price_alert_context") or {}
                self.assertEqual((context.get("state"), context.get("reason"), context.get("sample_delta")), ("unknown", expected, 0))
                effects["end_round_log_archive"].assert_called_once_with(status="ok")
                sample.assert_not_called()

        sub = scenario.subscription()
        sub.update(id=SUB_ID, subscription_id=SUB_ID, budget_strategy="low_price_alert")
        sub["hard_constraints"]["budget_strategy"] = "low_price_alert"
        rejected = {"email_sent": False, "pushplus_sent": False}
        accepted = {"email_sent": True, "pushplus_sent": False}
        path = self.root / "notice.json"
        with patch.object(main, "LOW_PRICE_NOTICE_STATE_PATH", path), patch.object(
                main, "_send_alert_channels", side_effect=[(rejected, {}), (accepted, {})]) as sends:
            for received in (False, True, True):
                ok, _, effects = self.run_subscription(_FakeMatrix({GO: "empty", BACK: "empty"}), sub=sub, managed=True)
                self.assertTrue(ok)
                self.assertIs(path.exists(), received)
                effects["end_round_log_archive"].assert_called_once_with(status="ok")
            self.assertEqual(sends.call_count, 2)

    def test_g15_six_plus_one_wording_all_carriers_privacy_and_uuid_link(self):
        check = self.helper(notifier, "_covered_empty_state")
        for (going, returning), expected in {**EMPTY_CASES, ("covered_empty", None): SINGLE}.items():
            with self.subTest(going=going, returning=returning):
                payload = self.payload(going, returning)
                self.assertIs(check(payload), returning != "panel_uncollected" and "covered_empty" in (going, returning))
                self.assertIs(notifier._data_incomplete_state(payload), returning == "panel_uncollected")
                self.assertIsNone(payload.get("current_price"))
                for content in self.carriers(payload):
                    self.assertIn(expected, content)
                    checked_content = content
                    if notifier._data_incomplete_state(payload):
                        for disclaimer in ("不作航班可行性、市场无票或价格位置判断", "结论不代表市场无票"):
                            checked_content = checked_content.replace(disclaimer, "")
                    for word in FORBIDDEN:
                        self.assertNotIn(word, checked_content)
                self.assertIn("/detail?sub=" + SUB_ID, payload["detail_url"])
                self.assertLessEqual(len(self.carriers(payload)[2]), 30_000)
        coverage = self.matrix({GO: "empty"})["airport_coverage"]
        for privacy in ("full", "redacted", "minimal"):
            payload = self.payload(return_only=True, privacy=privacy, coverage=coverage)
            for content in self.carriers(payload):
                self.assertIn(RETURN_ONLY, content)
                self.assertIn("返程阶段", content)
                self.assertEqual("PVG→KIX" in content, privacy == "full")

    def test_g16_deliver_return_value_tracks_channel_acceptance_not_collection(self):
        for method, page, email, push, delivered in (
            ("page_only", True, False, False, True), ("page_only", False, False, False, False),
            ("email", True, True, False, True), ("email", True, False, False, False),
            ("pushplus", True, False, True, True), ("pushplus", True, False, False, False),
            ("both", True, False, True, True), ("both", True, True, False, True),
            ("both", False, False, False, False),
        ):
            with self.subTest(method=method, page=page, email=email, push=push), ExitStack() as stack:
                sub = scenario.subscription()
                sub["notification_goals"] = {"method": method, "email": "offline@example.invalid"}
                stack.enter_context(patch.object(main, "_save_result_for_page", return_value=page))
                stack.enter_context(patch.object(main, "page_only_delivered", side_effect=bool))
                email_sink = stack.enter_context(patch.object(main, "send_email", return_value=email))
                push_sink = stack.enter_context(patch.object(main, "send", return_value=push))
                stack.enter_context(patch.object(main, "feedback_acknowledgement", return_value=""))
                stack.enter_context(patch.object(main, "delivery_payload_with_detail_token", side_effect=lambda payload: payload))
                stack.enter_context(patch.object(main, "persist_notification_payload"))
                ok, _, effects = self.run_subscription(_FakeMatrix({GO: "empty", BACK: "empty"}), sub=sub,
                    managed=True, extra={"_deliver_notification": main._deliver_notification})
                self.assertIs(ok, delivered)
                effects["end_round_log_archive"].assert_called_once_with(status="ok" if delivered else "failed")
                self.assertIs(email_sink.called, method in {"email", "both"})
                self.assertIs(push_sink.called, method in {"pushplus", "both"})
                if not delivered:
                    self.assertEqual(effects["_log_subscription_failure"].call_args.kwargs.get("reason"), "通知未发送成功")

    def test_g17_empty_quotes_do_not_write_prices_or_signals(self):
        names = ("price_position_description", "waiting_risk_description", "calc_buy_vs_wait_risk")
        guards = {name: Mock() for name in names}
        sub = scenario.subscription()
        sub.update(round_trip=False, hard_constraints={})
        sub.pop("return_date")
        ok, captured, effects = self.run_subscription(_FakeMatrix({GO: "empty"}), sub=sub, extra=guards)
        self.assertTrue(ok)
        effects["save_raw_response"].assert_called_once()
        for name in (*names, "save_flight_details", "save_roundtrip_snapshot", "log_signal"):
            effects[name].assert_not_called()
        self.assertIsNone(notifier.build_notification_payload(**captured[0]).get("current_price"))
        for analysis in ({"price_range": []}, {}):
            self.assertIsNone(tracker.log_signal("PVG-KIX", DAY, analysis)["current_min_price"])

    def test_g18_verified_empty_preserves_source_and_tracking_baseline(self):
        context = self.matrix({GO: "empty"})
        self.assertIsInstance(context, dict)
        for scope in ("single", "roundtrip"):
            with self.subTest(scope=scope), patch.object(plan_tracker, "load_pushed_plans", return_value={
                    "last_pushed": {"plan_a": {"scope": scope, "flight_no": "MU100", "price": 800}}}):
                change = plan_tracker.track_plan_status(SUB_ID, [], covered_empty=True)
                self.assertEqual((change["status"], change["scope"], change["price_diff"]), ("covered_empty_unquoted", scope, None))
                self.assertEqual(change["previous_price"], 800)
                self.assertTrue(all(word not in change["msg"] for word in FORBIDDEN))
        for stats, active in ((context["source_stats"], False), ({"juhe": {"status": "empty", "count": 0}}, True)):
            self.assertIs(notifier._build_source_degradation_context(source_stats=stats,
                last_snapshot={"channels": ["juhe"]}, source_errors=[])["active"], active)

    def test_g19_three_rounds_compare_latest_prices_with_last_quoted_round(self):
        identity = {"subscription_id": SUB_ID}
        key = ("PVG-KIX", DAY)
        snapshot = dict(route=key[0], depart_date=DAY, channels=["juhe"], **identity)
        flight = matrix_fixture.quote(*GO, DAY)
        first = dict(current_price=flight["price"], push_type="价格提醒", snapshot=snapshot, **identity,
                     recommended_plans=[{"main_flight": flight, "price": flight["price"], "flight_no": flight["flight_combo"]}])
        notifier.persist_notification_payload(first)
        prev_price = storage.get_last_push_price(*key, **identity)
        prev_snapshot = storage.get_last_push_snapshot(*key, **identity)
        prev_plan = plan_tracker.load_pushed_plans(SUB_ID)
        self.assertTrue(prev_plan)
        self.assertTrue(prev_price)
        self.assertTrue(prev_snapshot)
        self.assertEqual(notifier._parse_snapshot_sources(prev_snapshot), {"juhe"})
        self.helper(notifier, "_covered_empty_state")
        second = self.payload()
        second["snapshot"] = snapshot
        self.assertIn(SINGLE, notifier.render_detail_html(second))
        with patch.object(notifier, "save_last_push_price") as last, patch.object(notifier, "save_push_snapshot") as snap, \
                patch.object(notifier, "save_pushed_plans") as plan:
            notifier.persist_notification_payload(second)
            for writer in (last, snap, plan):
                writer.assert_not_called()
        self.assertEqual(storage.get_last_push_price(*key, **identity), prev_price)
        self.assertEqual(storage.get_last_push_snapshot(*key, **identity), prev_snapshot)
        self.assertEqual(plan_tracker.load_pushed_plans(SUB_ID), prev_plan)
        third = {**flight, "price": flight["price"] + 500}
        change = plan_tracker.track_plan_status(SUB_ID, [third])
        self.assertEqual(change["previous_price"], flight["price"])
        self.assertEqual(change["price_diff"], 500)

    def test_g20_partial_return_discloses_both_pairs_and_complete_empty_creates_return_analysis(self):
        sub = scenario.subscription()
        sub.update(destination_airports_active=["KIX", "ITM"], destination_airports=["KIX", "ITM"])
        agg = _FakeMatrix({BACK: "empty", ("ITM", "PVG"): "failed"})
        ok, captured, _ = self.run_subscription(agg, sub=sub)
        self.assertTrue(ok)
        self.assertTrue(captured)
        request = captured[0]
        self.assertEqual(request["analysis_result"].get("return_collection_outcome"), "incomplete_coverage")
        for carrier in self.carriers(notifier.build_notification_payload(**request)):
            for phrase in ("KIX→PVG", "ITM→PVG", "synthetic request failed", MISSING):
                self.assertIn(phrase, carrier)
        ok, captured, _ = self.run_subscription(_FakeMatrix({BACK: "empty"}))
        self.assertTrue(ok)
        self.assertTrue(captured)
        self.assertEqual(captured[0]["return_analysis"].get("collection_outcome"), "covered_empty")
        self.assertFalse(captured[0]["analysis_result"].get("collection_failures"))

    def test_g21_main_chain_four_functions_mechanically_revert_to_pre_r41(self):
        before = {
            "build_notification_payload": "6ec18a811b7cb6353af949b74237ffae1352147eb7cd6c170141ae75942d6912",
            "render_email": "429fabb2eea4a9b88cc0c653843aa372a8e0faad9f8e2865ad37e1a55cd7471e",
            "render_detail_html": "2586b469355356e88a09a6c4872c348a8c70129864d9e93b7515280c4100cd29",
            "render_pushplus_sections": "666472ec424df694891027abf2a125e47a8c0ba7d7d92b0400d64419a2ee56ef",
        }
        blocks = {
            **R41_REVERT,
            "build_notification_payload": (
                "    if _is_covered_empty_notification(analysis_result, route_info, outbound_analysis, return_analysis):\n"
                "        return _build_covered_empty_notification_payload(\n"
                "            analysis_result, outbound_analysis, return_analysis, route_info, subscription, source_stats,\n"
                "        )\n"
            ),
            "render_pushplus_sections": (
                "    if _covered_empty_state(payload) and not _data_incomplete_state(payload):\n"
                "        return _render_covered_empty_pushplus_sections(payload)\n"
            ),
        }
        tree, source = renderer_guard._notifier_tree_and_source()
        for name, old_digest in before.items():
            with self.subTest(name=name):
                text = ast.get_source_segment(source, renderer_guard._module_function(tree, name))
                self.assertEqual(text.count(blocks[name]), 1)
                self.assertEqual(sha256(text.encode()).hexdigest(), renderer_guard.EXPECTED_MAIN_CHAIN_SHA256[name])
                self.assertEqual(sha256(text.replace(blocks[name], "", 1).encode()).hexdigest(), old_digest)


class TestTrack4FrozenAndAstGuards(unittest.TestCase):
    def test_g22_protected_files_and_source_anchors_are_exact(self):
        locked = (
            ("sources/hasdata_source.py", 2694, "464b81201403cf54696bdc9b639f87526e3a9fe6fab2ab95b6371ebd33c1f07c"),
            ("sources/serpapi_source.py", 16030, "eeefaac3e4b1bdb9fce18e7d2db55adcd82e172ef59c7b4cd17aaa53dd571ad4"),
            ("collection_plan.py", 40824, "855215c759e4d93145ef0f82dd6d4895faeedc3bd50e3806920c5aa85c897f82"),
            ("constraint_fingerprint.py", 11459, "b396aef5431cc976754bab2a5a2474641b78d38002844a808521b2f228c415a9"),
            ("constraint_summary.py", 3595, "ab7a1505ff953891c7959e7e2412bc3ce7f93f93732e48c7557f5d43ebb3752b"),
            ("web_form.py", 125145, "47f45bc97aa879bfb48e996482f13a504f1d68bc85fc0d893e885ea5b0f9ab1d"),
            ("form_concepts.py", 37206, "cf535034c4242c291b6cf1f66c7335497e0fb2e4ecbf238492662b6621e1e9b6"),
            ("form_structure.py", 53116, "5b9160c3cec816afeb92bf13dc868f1405c8db1eda3b6bb41479bc399b063882"),
            ("tests/fixtures/form_normalization_baseline_v1.json", 180179,
             "88172833002fbe747b289848341a85d4c23470070339a4775ce2f61f183a2fd9"),
        )
        for path, size, digest in locked:
            raw = (ROOT / path).read_text(encoding="utf-8").encode("utf-8")
            self.assertEqual((len(raw), sha256(raw).hexdigest()), (size, digest), path)
        fixture = json.loads((ROOT / locked[-1][0]).read_text(encoding="utf-8"))
        self.assertEqual(len(fixture["scenarios"]), 11)
        for function, digest in (
            (analyzer.parse_flight_time, "9a55d7e7d427adf2469b269546262ebc3d3264ec7f7cae4b7df537f1a4177297"),
            (analyzer.match_time_preference, "2e21c6f1cb34a5300b194c59b167b52753e321f2d7e696685c153f64c98af93f"),
        ):
            self.assertEqual(sha256(inspect.getsource(function).encode("utf-8")).hexdigest(), digest)

    def test_g23_frozen_three_carriers_and_standard_roundtrip_are_exact(self):
        r40_guard.TestTrack4FrozenAndAstGuards.test_g24_frozen_three_carriers_and_normal_roundtrip_stay_exact(self)

    def test_g24_nonempty_return_low_price_and_legacy_retirement_remain_exact(self):
        cls = scenario.ReturnPhaseProcessingTest
        cls.setUpClass()
        try:
            result = cls("test_return_new_collection").run()
            self.assertTrue(result.wasSuccessful(), result.errors + result.failures)
        finally:
            cls.tearDownClass()
        for name in ("test_one_way_triggered_unchanged", "test_return_only_not_applicable"):
            result = low_price_guard.AnalyzerStateTest(name).run()
            self.assertTrue(result.wasSuccessful(), result.errors + result.failures)
        self.assertTrue(notifier._data_incomplete_state({"collection_failures": [{"direction": "return"}]}))
        self.assertFalse(notifier._data_incomplete_state({"current_price": None}))
        result = renderer_guard.LegacyNotificationRendererRetirementTest(
            "test_public_entry_raises_domain_error_without_side_effects"
        ).run()
        self.assertTrue(result.wasSuccessful(), result.errors + result.failures)
