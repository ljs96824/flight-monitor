import ast
import copy
import hashlib
import json
import tempfile
import unittest
from contextlib import ExitStack
from pathlib import Path
from unittest.mock import patch

import analyzer
import config_loader
import notifier
import plan_tracker
import test_hasdata_retirement
import test_legacy_notification_renderer_retirement as renderer_guard
import test_pushplus_no_live_api_sink
from tests import test_baggage_eligibility_and_budget_caliber as budget
from tests import test_plan_tracker_degraded_text as tracker


ROOT = Path(__file__).resolve().parents[1]


def rules(**kwargs):
    return {"fare_rules": {"baggage": kwargs}}


def detail(**kwargs):
    return {"has_baggage_info": True, "extra": {"baggage_detail": {"checked": kwargs}}}


def carriers(payload):
    return (notifier.render_email(copy.deepcopy(payload))[1],
            notifier.prepare_push_render(notifier.render_pushplus_sections(copy.deepcopy(payload))).content,
            notifier.render_detail_html(copy.deepcopy(payload)))


def payload(row, *, roundtrip=False, return_only=False, previous=None, errors=None):
    route = {"origin": "KIX", "destination": "PVG", "route_type": "international",
             "depart_date": "2026-10-06", "round_trip": roundtrip}
    analysis = {"all_flights": [row], "economy_recommendations": [{"flight": row}],
                "recommendations": [{"flight": row}], "source_errors": errors or []}
    if roundtrip:
        route["return_date"] = "2026-10-10"
        other = {**row, "flight_no": "JL892", "flight_combo": "JL892"}
        combo = {"outbound": row, "return": other, "outbound_price": row["price"],
                 "return_price": other["price"], "total_price": row["price"] + other["price"]}
        analysis["round_trip_analysis"] = {"top_combinations": [combo]}
    if return_only:
        route.update(processing_phase="return_only", phase_label="返程", original_outbound_date="2026-10-01")
    with ExitStack() as stack:
        temp = Path(stack.enter_context(tempfile.TemporaryDirectory()))
        stack.enter_context(patch.object(plan_tracker, "DEFAULT_DATA_DIR", temp))
        stack.enter_context(patch.object(notifier, "get_last_push_snapshot", return_value=previous))
        stack.enter_context(patch.object(notifier, "get_last_push_price",
                                         return_value={"price": 2000} if previous else None))
        stack.enter_context(patch.object(notifier, "track_plan_status", return_value=None))
        return notifier.build_notification_payload(
            analysis, route_info=route, subscription={"id": tracker.SUB_ID, "need_baggage": "optional"},
            price_history=[], source_stats={"juhe": {"count": 1, "status": "success"}})


class TestTrack1BaggageInclusionFailClosed(unittest.TestCase):
    def test_g01_pushplus_baggage_line_rejects_paid_and_unverified_allowances(self):
        line = notifier._pushplus_baggage_line_for_flight
        cases = [(detail(quantity=1, is_free=False), "不含免费托运"),
                 (detail(quantity=1), "权益未确认"),
                 ({"fare_rules": {"source": "国内标准规则推断", "baggage": {"included": True, "checked_kg": 20}}}, "权益未确认"),
                 ({**rules(included=True, checked_pieces=1), "baggage_offer_matched": False,
                   "baggage_applicability": "flight_number_match_only"}, "权益未确认"),
                 (budget.paid_flight(100, **rules(checked_pieces=1)), "允许付费托运"),
                 ({"baggage": [{"type": "checked", "quantity": 1, "is_free": False}]}, "不含免费托运"),
                 ({"extra": {"checked_baggage_allowed": False}}, "不含免费托运")]
        cases.extend((rules(included=flag, **allowance), "权益未确认")
                     for flag in (None, "true", 1) for allowance in ({"checked_pieces": 1}, {"checked_kg": 20}))
        for row, expected in cases:
            self.assertNotIn("checked_baggage_eligibility", row)
            self.assertNotIn("已含", line(row), row)
            self.assertIn(expected, line(row), row)
        self.assertEqual(line(rules(included=True, checked_kg=20)), "行李:已含托运20kg")
        self.assertEqual(line(detail(quantity=2, is_free=True)), "行李:已含2件托运")
        self.assertIn("需另购", line(rules(included=False)))
        self.assertEqual(line({}), "行李:支付页需确认")

    def test_g02_compact_format_and_domestic_baggage_helpers_align_with_analyzer(self):
        for row, expected in [(detail(quantity=1, is_free=False), "不含免费托运"),
                              (detail(quantity=1), "权益未确认")]:
            self.assertFalse(analyzer._has_free_checked_baggage(row))
            for text in (notifier._compact_baggage_line(row), " ".join(notifier.format_baggage(row["extra"]))):
                self.assertIn(expected, text)
                self.assertNotIn("免费1件", text)
        row = {"flight_combo": "CA1234", "fare_rules": {"source": "国内标准规则推断",
               "baggage": {"included": True, "checked_kg": 20, "note": "含20kg托运"}}}
        self.assertFalse(analyzer._has_free_checked_baggage(row))
        for text in (notifier._domestic_baggage_line({"main_flight": row}), notifier._compact_baggage_line(row)):
            self.assertIn("权益未确认", text)
            self.assertNotIn("含20kg", text)
        carry = notifier.format_baggage({"baggage_detail": {"carry_on": {"quantity": 1, "is_free": False}}})
        self.assertNotIn("免费", next(s for s in carry if "手提" in s))

    def test_g03_three_carriers_end_to_end_never_mislabel_paid_or_unverified_baggage(self):
        with tempfile.TemporaryDirectory() as tmp, patch.object(config_loader, "RUNTIME_CONFIG_PATH", Path(tmp) / "missing.yaml"):
            for trip in (False, True):
                for extras in (detail(quantity=1, is_free=False), rules(checked_pieces=1)):
                    result = payload({**tracker.flight(), **extras}, roundtrip=trip)
                    self.assertTrue(result.get("recommended_plans"))
                    for text in carriers(result):
                        self.assertTrue(text)
                        for word in ("已含1件托运", "已含托运", "免费1件"):
                            self.assertNotIn(word, text)


class TestTrack2OneWayPlanSourceAndListingDegradation(unittest.TestCase):
    def test_g04_source_set_from_plan_covers_oneway_main_flight_return_only_and_mixed_cabin(self):
        row = tracker.flight()
        extract = notifier._source_set_from_plan
        plan = notifier._payload_single_plan(row, {}, {}, 0, "推荐")
        self.assertEqual(extract(plan), {"juhe"})
        for return_only in (False, True):
            data = payload(row, return_only=return_only)
            self.assertEqual(data["snapshot"]["source_set"], ["juhe"])
            self.assertEqual(data["snapshot"]["channels"], ["juhe"])
            if return_only:
                self.assertEqual(data["price_signal"].get("label"), "返程单程报价")
                self.assertNotIn("sources", data["price_signal"])
            else:
                self.assertEqual(data["price_signal"].get("sources"), ["juhe"])
        business = {**row, "data_source": "searchapi", "source": "searchapi"}
        for legs in ({"main_flight": row, "business_flight": business},
                     {"is_roundtrip": True, "outbound_flight": row, "return_flight": row, "business_outbound": business}):
            self.assertEqual(extract(dict(legs, mixed_cabin=True, business_price_source=" SerpAPI + JUHE ")),
                             {"juhe", "searchapi", "serpapi"})
        self.assertFalse(extract({"mixed_cabin": True, "business_price_source": "serpapi"}))

    def test_g05_serpapi_and_searchapi_degradation_detected_while_retired_hasdata_ignored(self):
        for name in ("serpapi", "searchapi", "hasdata"):
            for error_only in (False, True):
                stats = {"juhe": {"count": 5}}
                if not error_only:
                    stats[name] = {"count": 0, "status": "failed"}
                ctx = notifier._build_source_degradation_context(
                    source_stats=stats, source_errors=[{"source": name, "error": "HTTP 503"}],
                    last_snapshot={"source_set": ["juhe", name]}, current_sources={"juhe", "duffel"},
                    retired_sources={"hasdata"})
                self.assertEqual(ctx["active"], name != "hasdata")
                if ctx["active"]:
                    self.assertEqual((ctx["source"], ctx["source_label"]), (name, "Google源"))
                    self.assertIn("本轮Google数据源不可用", ctx["reason"])
                    self.assertIn("入池仅OTA,", ctx["reason"])

    def test_g06_oneway_end_to_end_snapshot_and_non_quota_error_degradation(self):
        row = tracker.flight()
        first = payload({**row, "data_source": "juhe+serpapi"})
        snap = first["snapshot"]
        self.assertEqual(set(snap["source_set"]), {"juhe", "serpapi"})
        for error in ("ReadTimeout", "HTTP 503"):
            second = payload(row, previous=snap, errors=[{"source": "serpapi", "error": error}])
            self.assertEqual(second["push_type"], "数据源受限")
            self.assertIsNone(second["diff_from_last"]["diff"])
            self.assertIn("不可直接比", second["source_degradation"]["reason"])
            meta = notifier._apply_source_degradation_to_push_meta(
                {"type": "涨价风险", "price_change": {"diff": 500}}, current_sources={"serpapi", "searchapi", "duffel"},
                previous_sources={"serpapi", "searchapi", "juhe"}, source_errors=[{"source": "juhe", "error": error}])
            self.assertIsNone(meta["price_change"])
            self.assertIn(f"不可用({error}),入池仅Google,", meta["source_degradation"]["reason"])
        self.assertEqual(notifier._apply_source_degradation_to_push_meta(
            {}, current_sources={"juhe"}, previous_sources={"hasdata", "juhe"}), {})


class TestTrack3PriceGapAnomalyAndSourceRows(unittest.TestCase):
    def test_g07_price_gap_disclosure_supports_serpapi_and_searchapi_vs_juhe(self):
        for name in ("serpapi", "searchapi"):
            a = {"flight_combo": "JL891", "diff_pct": notifier.PRICE_GAP_DISCLOSE_PCT + 20,
                 "sources": [{"source": name, "price": 900}, {"source": "juhe", "price": 1800}]}
            self.assertTrue(notifier._should_disclose_source_price_gap(a))
            self.assertFalse(notifier._should_disclose_source_price_gap(dict(a, diff_pct=0)))
            self.assertFalse(notifier._should_disclose_source_price_gap(dict(a, sources=[
                {"source": "serpapi", "price": 900}, {"source": "searchapi", "price": 1800}])))
            one = {"main_flight": tracker.flight()}
            pair = {"is_roundtrip": True, "outbound_flight": tracker.flight(), "return_flight": tracker.flight()}
            notifier._attach_source_price_anomalies_to_plans([one, pair], [a], [a])
            for leg in (one["main_flight"], pair["outbound_flight"], pair["return_flight"]):
                self.assertEqual(leg.get("source_price_anomaly"), a)

    def test_g08_international_email_source_body_discloses_serpapi_count_without_breaking_juhe_only(self):
        data = {"route_type": "international", "source_retirement": {"active": True},
                "source_stats": {"juhe": {"count": 3}}}
        original = notifier._email_source_body(data)
        for n in (6, 0):
            data["source_stats"]["serpapi"] = {"count": n}
            text = notifier._email_source_body(data)
            self.assertIn("主源:聚合数据(OTA)—3个方案", text)
            if n:
                self.assertIn("Google Flights(SerpAPI)—6个方案", text)
            else:
                self.assertEqual(text, original)


class TestTrack4LockedMainChainAstAndFrozenCarrierGuards(unittest.TestCase):
    def test_g09_locked_main_chain_ast_and_existing_degradation_contracts_intact(self):
        src = (ROOT / "notifier.py").read_text(encoding="utf-8")
        locked = renderer_guard.EXPECTED_MAIN_CHAIN_SHA256
        self.assertEqual({n.name: hashlib.sha256(ast.get_source_segment(src, n).encode("utf-8")).hexdigest()
                          for n in ast.parse(src).body if isinstance(n, ast.FunctionDef) and n.name in locked}, locked)
        cases = (test_pushplus_no_live_api_sink.PushPlusNoLiveStaticContractTest(
                     "test_gate_is_first_business_statement_and_branch_is_fixed"),
                 tracker.PlanTrackerDegradedTextTest("test_c9c_regular_pushplus_matches_baseline"),
                 test_hasdata_retirement.HasDataRetirementTest("test_retirement_notice_appears_once_and_is_not_a_fault"))
        result = unittest.TestResult()
        unittest.TestSuite(cases).run(result)
        self.assertTrue(result.wasSuccessful(), result.errors + result.failures)
        self.assertEqual(result.testsRun, 3)

    def test_g10_three_frozen_carriers_exact(self):
        fixture = ROOT / "tests/fixtures/frozen_email/economy_payload.json"
        with tempfile.TemporaryDirectory() as tmp, patch.object(config_loader, "RUNTIME_CONFIG_PATH", Path(tmp) / "missing.yaml"):
            rendered = carriers(json.loads(fixture.read_text(encoding="utf-8")))
        expected = [(53307, "1dd353d2b7bcafc02087957ccccb6661478e20472a22fb3e15dae6a56473ee6b"),
                    (5552, "2ac0e80d9f82dbe7ebccf524c0832f09d42d916212b6178316b0b7bcd60574ff"),
                    (54248, "e31b8728b8d0bc05c9bbd4bd99c299aaf6dca7b66674b6c560ec089b1681ffa6")]
        self.assertEqual([(len(b), hashlib.sha256(b).hexdigest()) for b in
                          (s.encode("utf-8") for s in rendered)], expected)
