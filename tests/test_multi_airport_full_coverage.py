"""R36: offline full-matrix behavior (G01-G16) and compatibility guards (G17-G18)."""
import ast
from contextlib import ExitStack, redirect_stdout
from copy import deepcopy
from datetime import date, datetime, timedelta, timezone
import hashlib
import io
import json
import os
from pathlib import Path
import sqlite3
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import Mock, patch

import analyzer
import collection_plan
import config_loader
import main
import notifier
import observations_store
import plan_tracker
import request_cache
import storage
from sources import aggregator as aggregation
from pushplus_sections import PushRender, PushSection
from tests import test_return_phase_processing as scenario
from tests import test_low_price_same_caliber_history as history_fixture


ROOT = Path(__file__).resolve().parents[1]
O, D = ["PVG", "SHA"], ["KIX", "ITM"]
DAY, RETURN = "2026-10-01", "2026-10-06"
STAMP = "2026-09-30T08:00:00+00:00"


class Source:
    name, role, weight = "juhe", "primary", 1
    supported_cabins = ["economy"]

    def __init__(self):
        self.fetch = Mock(return_value={"source": self.name, "source_status": "success",
                                        "flights": [], "raw": {}, "raw_result_count": 0})

    def preflight_skip(self, *args, **kwargs):
        return None


def quote(origin, dest, day, index=0):
    item = scenario.flight(day == DAY)
    number = f"MU{100 + index}"
    item.update(flight_combo=number, departure_airport=origin, arrival_airport=dest,
                departure_date=day, search_origin=origin, search_destination=dest,
                collected_at=STAMP)
    item["segments"] = [dict(item["segments"][0], flight_no=number, dep_airport=origin,
                             arr_airport=dest, dep_time=day + " 13:00", arr_time=day + " 15:30")]
    return item


class MatrixAggregator:
    """A source-result boundary stub: no provider, HTTP, database or cache calls."""
    search_sources = [Source()]

    def __init__(self, states=None, count=8):
        self.states, self.count, self.calls = states or {}, count, []
        self.last_source_errors, self.last_fetch_evidence_units = [], []

    def collect(self, origin, destination, day, cabin_classes=None, passengers=None,
                route_type=None, request_reason=None, panel_only=None):
        self.calls.append((origin, destination, day, panel_only))
        kind = self.states.get((origin, destination, day), self.states.get((origin, destination), "quoted"))
        fs = [quote(origin, destination, day, i) for i in range(self.count)] if kind in {"quoted", "reused"} else []
        unit = dict(source="juhe", cabin_class="economy", origin_airport=origin,
                    dest_airport=destination, depart_date=day, role="primary", weight=1,
                    source_status="success" if fs else "empty" if kind in {"empty", "unusable"} else "failed",
                    valid_flight_count=len(fs), raw_result_count=len(fs), raw_flight_count=len(fs),
                    raw_schema_valid=kind != "unusable", is_enrichment=False,
                    collection_state="panel_reused" if kind == "reused" else "fresh", collected_at=STAMP,
                    observed_at_utc=STAMP, observation_round_id="collection_synthetic", fetch_event_id=origin+destination+day,
                    evidence_qualified=True, panel_snapshot_written=True)
        self.last_source_errors = []
        if kind == "quota":
            unit.update(source_status="skipped_quota_protection", skipped_reason="配额保护跳过")
        elif kind == "failed":
            unit.update(error="synthetic request failed")
            self.last_source_errors = [{"source": "juhe", "cabin_class": "economy", "error": unit["error"]}]
        elif kind == "unusable":
            unit.update(raw_result_count=3, raw_flight_count=3)
        self.last_fetch_evidence_units = [unit]
        for f in fs:
            f["source_collection"] = {"juhe": deepcopy(unit)}
        return dict(flights=fs, fetch_evidence_units=[unit], source_errors=self.last_source_errors,
                    source_stats={"juhe": {"count": len(fs), "status": "成功" if fs else kind}},
                    collected_at=STAMP, price_insights={}, raw_by_source={}) if fs else None


class MultiAirportCoverageTest(unittest.TestCase):
    def setUp(self):
        self.stack = ExitStack()
        self.addCleanup(self.stack.close)
        self.root = Path(self.stack.enter_context(TemporaryDirectory()))
        self.stack.enter_context(patch.dict(os.environ, {"NO_LIVE_API": "1"}))
        self.stack.enter_context(patch.object(storage, "DB_PATH", self.root / "prices.db"))
        self.stack.enter_context(patch.object(plan_tracker, "DEFAULT_DATA_DIR", self.root / "plans"))
        self.stack.enter_context(patch.object(config_loader, "RUNTIME_CONFIG_PATH", self.root / "nonexistent_runtime_config.yaml"))
        self.stack.enter_context(patch.object(main, "ANALYSIS_LOG", self.root / "analysis_log.jsonl"))
        self.stack.enter_context(patch.object(main, "log_signal", return_value={}))
        self.stack.enter_context(patch.object(main, "safe_log"))
        self.stack.enter_context(patch.object(aggregation, "safe_log"))
        self.stack.enter_context(patch.object(request_cache, "safe_log"))
        request_cache.reset_for_tests(self.root / "cache")
        self.addCleanup(request_cache.reset_for_tests, None)
        tokens = observations_store.set_current_round("collection_synthetic", self.root / "observations.sqlite3")
        self.addCleanup(observations_store.reset_current_round, tokens)

    def matrix(self, agg=None, origins=None, dests=None, day=DAY, **kwargs):
        agg = agg or MatrixAggregator()
        with redirect_stdout(io.StringIO()):
            data = main.collect_for_airport_matrix(agg, origins or O, dests or D, day,
                                                    cabin_classes=["economy"], **kwargs)
        return agg, data

    def coverage(self, agg, data):
        value = (data or {}).get("airport_coverage") or getattr(agg, "last_airport_coverage", {})
        self.assertIsInstance(value, dict)
        self.assertIn("planned_pair_count", value, "COVERAGE_LEDGER_REQUIRED")
        return value

    def plan(self, sub, calendars=False):
        with patch.object(collection_plan, "shanghai_today", return_value=date(2026, 9, 30)), \
                patch.object(collection_plan, "_calendar_dates", return_value=["2026-09-30", DAY]):
            return collection_plan.build_collection_plan(subscriptions=[sub], include_calendars=calendars,
                         source_builder=lambda *args, **kw: ([Source()], []))

    def subscription(self):
        sub = scenario.subscription()
        sub.update(origin_airports=O, origin_airports_active=O,
                   destination_airports=D, destination_airports_active=D)
        return sub

    def run_subscription(self, agg, sub=None, today=date(2026, 9, 30)):
        sub = deepcopy(sub or self.subscription())
        clock = type("ScenarioClock", (scenario.FixedDatetime,), {"today": today})
        captured = []
        writes, rt_write, raw = Mock(), Mock(), Mock()
        patches = {"_shanghai_today": Mock(return_value=today), "datetime": clock,
                   "_departure_reference_now": Mock(return_value=datetime(2026, 9, 30, 16, tzinfo=timezone(timedelta(hours=8)))),
                   "build_default_sources": Mock(return_value=([Source()], [])),
                   "FlightAggregator": Mock(return_value=agg),
                   "get_constraint_epoch_boundary": Mock(return_value=None),
                   "get_constraint_history_limit": Mock(return_value=14),
                   "get_previous_snapshot_prices": Mock(return_value={}),
                   "get_lowest_price_history": Mock(return_value=[]),
                   "get_roundtrip_price_history": Mock(return_value=[]),
                   "save_raw_response": raw, "save_flight_details": writes, "save_roundtrip_snapshot": rt_write,
                   "collect_nearby_dates": Mock(return_value=[]),
                   "_notification_tcurve": Mock(return_value={}), "_notification_forecast": Mock(return_value={}),
                   "_notification_provenance_context": Mock(return_value={}),
                   "system_health_check": Mock(return_value={}),
                   "_should_skip_low_price_alert": Mock(return_value=False),
                   "_deliver_notification": Mock(side_effect=lambda s, r, options: captured.append(options) or True)}
        with ExitStack() as stack, redirect_stdout(io.StringIO()):
            for name, value in patches.items():
                stack.enter_context(patch.object(main, name, value))
            for module in (analyzer, collection_plan):
                stack.enter_context(patch.object(module, "shanghai_today", return_value=today))
            ok = main._process_subscription_locked(sub, ensure_db=False, manage_collection_round=False,
                                                   collection_round_id="collection_synthetic")
        return ok, captured, writes, rt_write, raw

    def payload(self, partial=True):
        states = {(o, d): "failed" for o in O for d in D if (o, d) != (O[0], D[0])} if partial else {}
        agg, data = self.matrix(MatrixAggregator(states))
        payload = json.loads((ROOT / "tests/fixtures/frozen_email/economy_payload.json").read_text(encoding="utf-8"))
        context = notifier._build_source_degradation_context(source_stats=data.get("source_stats", {}),
                                      last_snapshot={}, source_errors=data.get("source_errors", []))
        payload.update(source_stats=data.get("source_stats", {}), source_degradation=context)
        meta = notifier._apply_source_degradation_to_push_meta(
            {"type": "价格下降", "price_change": {"diff": -100}, "reasons": ["较上次下降"]},
            current_sources={"juhe"}, previous_sources={"juhe"}, degradation_context=context)
        payload["push_type"] = meta["type"]
        payload["diff_from_last"]["diff"] = (meta.get("price_change") or {}).get("diff")
        return payload, context, agg

    def test_g01_primary_plan_and_execution_keep_every_active_pair(self):
        sub = self.subscription()
        sub["round_trip"] = sub["hard_constraints"]["round_trip"] = False
        plan = self.plan(sub)
        self.assertEqual({(k[1], k[2]) for k in plan.request_keys}, {(o, d) for o in O for d in D}, "PLAN_MATRIX")
        agg, data = self.matrix(origins=O + [O[0]])
        self.assertEqual(len(agg.calls), 4, "NO_EARLY_EXIT_AFTER_EIGHT_QUOTES")
        cov = self.coverage(agg, data)
        self.assertEqual((cov["planned_pair_count"], cov["covered_pair_count"], cov["complete"]), (4, 4, True))

    def test_g02_roundtrip_keeps_both_direction_matrices(self):
        agg, out = self.matrix()
        self.assertEqual(len(agg.calls), 4, "OUTBOUND_MATRIX")
        _, ret = self.matrix(agg, D, O, RETURN, direction="return")
        self.assertEqual(len(agg.calls), 8, "BOTH_MATRICES")
        self.assertEqual(out["airport_coverage"]["direction"], "outbound")
        self.assertEqual(ret["airport_coverage"]["direction"], "return")
        self.assertEqual(len(self.plan(self.subscription()).request_keys), 8)

    def test_g03_return_only_projects_plan_and_reaches_the_existing_entry(self):
        sub = self.subscription()
        with patch.object(collection_plan, "shanghai_today", return_value=date(2026, 10, 2)):
            plan = collection_plan.build_collection_plan(subscriptions=[sub], include_calendars=False,
                                          source_builder=lambda *args, **kw: ([Source()], []))
        self.assertEqual({k[3] for k in plan.request_keys}, {RETURN}, "NO_EXPIRED_OUTBOUND_PLAN_KEYS")
        agg = MatrixAggregator()
        ok, captured, _, rt, _ = self.run_subscription(agg, sub, today=date(2026, 10, 2))
        self.assertTrue(ok)
        self.assertTrue(captured)
        self.assertEqual({(o, d) for o, d, _, _ in agg.calls}, {(d, o) for d in D for o in O})
        self.assertTrue(all(day == RETURN for _, _, day, _ in agg.calls))
        rt.assert_not_called()

    def test_g04_same_day_alternatives_remain_runtime_conditional_and_separate(self):
        sub = self.subscription()
        sub["same_day_round_trip"] = True
        plan = self.plan(sub)
        self.assertEqual({k[3] for k in plan.request_keys}, {DAY, RETURN})
        agg = MatrixAggregator()
        with patch.object(main, "DATA_DIR", self.root), redirect_stdout(io.StringIO()):
            main._collect_same_day_fallback_flights(agg, O, D, "2026-09-30", request_reason="前一晚去程备选")
            main._collect_same_day_fallback_flights(agg, D, O, "2026-10-07", request_reason="次日返程备选")
        self.assertEqual(len(agg.calls), 8, "FULL_RUNTIME_ALTERNATIVE_MATRICES")
        self.assertEqual(set(agg.last_same_day_fallback_coverage), {"2026-09-30", "2026-10-07"})
        self.assertTrue(all(c["complete"] for c in agg.last_same_day_fallback_coverage.values()))

    def test_g05_flexible_dates_use_panel_only_for_all_five_panel_states(self):
        sub = self.subscription()
        sub["date_flexibility"] = 1
        self.assertEqual(len(self.plan(sub).panel_only_keys), 8, "FULL_OPTIONAL_PANEL_MATRIX")
        source = Source()
        source.fetch.return_value.update(flights=[quote("PVG", "KIX", DAY)], source_status="success")
        for kind in ("hit", "missing", "expired", "corrupt", "dirty", "rebuild_failed"):
            request_cache.reset_for_tests(self.root / kind)
            source.fetch.reset_mock()
            snapshot = None if kind in {"missing", "expired"} else {"rows": [], "observed_at": STAMP}
            rebuild = {"source_status": "success", "flights": [quote("PVG", "KIX", DAY)],
                       "collection_state": "panel_reused", "collected_at": STAMP}
            with ExitStack() as stack, redirect_stdout(io.StringIO()):
                stack.enter_context(patch.object(request_cache, "_panel_snapshot_for_key",
                    side_effect=sqlite3.DatabaseError("synthetic corruption") if kind == "corrupt" else None,
                    return_value=snapshot))
                stack.enter_context(patch.object(request_cache, "_rebuild_panel_result",
                    side_effect=ValueError("dirty price_cny") if kind == "dirty" else None,
                    return_value=rebuild if kind == "hit" else None))
                if kind in {"corrupt", "dirty"}:
                    self.assertIsNone(request_cache.panel_reuse_result(source, "PVG", "KIX", DAY))
                rows = main.collect_nearby_dates(aggregation.FlightAggregator([source], []), sub,
                                                 target_min_price=2000, fresh_scope="primary_only")
            self.assertEqual(source.fetch.call_count, 0, "PANEL_ONLY_ZERO_REQUESTS:" + kind)
            self.assertEqual(len(rows), 3)
            for row in rows:
                if not row["selected"]:
                    coverage = row["airport_coverage"]
                    self.assertEqual(coverage["planned_pair_count"], 4)
                    self.assertEqual({record["state"] for record in coverage["pair_records"]},
                                     {"covered_reused" if kind == "hit" else "panel_uncollected"})
        source.fetch.assert_not_called()

    def test_g06_calendar_uses_only_the_static_primary_pair_and_discloses_scope(self):
        sub = self.subscription()
        sub["route_type"] = "domestic"
        sub["round_trip"] = False
        sub["hard_constraints"]["round_trip"] = False
        plan = self.plan(sub, calendars=True)
        calendars = [r for r in plan._requests.values() if "低价日历" in r.reasons]
        self.assertTrue(calendars)
        self.assertEqual({(r.key[1], r.key[2]) for r in calendars}, {(O[0], D[0])})
        calendar = notifier._payload_price_calendar({"price_calendar": {"rows": [], "coverage_scope_note": "仅覆盖主机场组合(PVG→KIX)"}}, {})
        self.assertIn("仅覆盖主机场组合", calendar.get("coverage_scope_note", ""), "STATIC_CALENDAR_SCOPE_DISCLOSURE")
        payload = {"price_calendar": calendar}
        self.assertIn("仅覆盖主机场组合", notifier._email_price_calendar_body(payload))
        self.assertIn("仅覆盖主机场组合", " ".join(notifier._pushplus_calendar_summary_lines(payload)))

    def test_g07_source_units_distinguish_empty_reused_unusable_and_skipped(self):
        expected = {"quoted": "covered_quoted", "empty": "covered_empty", "reused": "covered_reused",
                    "unusable": "data_unusable", "failed": "uncovered_failed", "quota": "uncovered_skipped"}
        for kind, wanted in expected.items():
            agg, data = self.matrix(MatrixAggregator({("PVG", "KIX"): kind}), ["PVG"], ["KIX"])
            cov = self.coverage(agg, data)
            self.assertEqual(cov["pair_records"][0]["state"], wanted)
        for raw in ({"result": "broken"}, {"result": {"list": "broken"}}, {"error": "bad"}):
            source = Source()
            source.name = "serpapi" if "error" in raw else "juhe"
            with patch.object(aggregation, "cached_fetch", return_value=({"source_status": "empty", "raw": raw,
                                "raw_result_count": 0, "flights": []}, "fresh")), redirect_stdout(io.StringIO()):
                agg = aggregation.FlightAggregator([source], [])
                with patch.object(agg, "_ordered_search_sources", return_value=[source]):
                    agg.collect("PVG", "KIX", DAY)
            self.assertFalse(agg.last_fetch_evidence_units[0]["raw_schema_valid"])

    def test_g08_partial_main_coverage_is_limited_and_never_zero_difference(self):
        payload, context, _ = self.payload()
        self.assertTrue(context.get("airport_coverage_incomplete"), "PARTIAL_COVERAGE_DEGRADATION")
        self.assertEqual(payload["push_type"], "数据源受限")
        self.assertIsNone(payload["diff_from_last"]["diff"])
        self.assertIn("不可比较／覆盖不完整", context["reason"])
        self.assertEqual(context["leg_coverages"]["outbound"]["planned_pair_count"], 4)

    def test_g09_partial_round_keeps_raw_but_never_replaces_complete_history_or_push(self):
        agg = MatrixAggregator({("SHA", "ITM"): "failed"})
        ok, captured, writes, rt, raw = self.run_subscription(agg)
        self.assertTrue(ok)
        self.assertTrue(captured)
        self.assertEqual(writes.call_count, 0, "PARTIAL_ROUND_MUST_NOT_WRITE_COMPLETE_HISTORY")
        rt.assert_not_called()
        self.assertGreater(raw.call_count, 0)
        payload, _, _ = self.payload()
        with patch.object(notifier, "save_last_push_price") as price, \
                patch.object(notifier, "save_push_snapshot") as snapshot, \
                patch.object(notifier, "save_pushed_plans") as plans:
            notifier.persist_notification_payload(payload)
        price.assert_not_called()
        snapshot.assert_not_called()
        plans.assert_not_called()

    def test_g10_previous_plan_tracks_airport_pairs_without_inventory_inferences(self):
        f, ret = quote("PVG", "KIX", DAY), quote("KIX", "PVG", RETURN)
        choices = [{"main_flight": f, "price": 1200},
                   {"outbound_flight": f, "return_flight": ret, "price": 2400},
                   {"outbound_flight": f, "return_flight": ret, "price": 3000,
                    "mixed_cabin": True, "mixed_cabin_total": 3000},
                   {"main_flight": ret, "price": 1200}]
        for i, plan in enumerate(choices):
            folder = self.root / str(i)
            saved = plan_tracker.save_pushed_plans("s1", [plan], data_dir=folder)
            key = "outbound_dep_airport" if i in (1, 2) else "dep_airport"
            self.assertIn(key, saved["last_pushed"]["plan_a"], "ARCHIVE_SEARCH_AIRPORT_PAIR")
            degradation = {"active": True, "airport_coverage_incomplete": True,
                           "uncovered_pairs_by_direction": {"outbound": [["PVG", "KIX"]], "return": [["KIX", "PVG"]]}}
            if i == 3:
                degradation["uncovered_pairs_by_direction"].pop("outbound")
            result = plan_tracker.track_plan_status("s1", [], data_dir=folder, source_degradation=degradation)
            self.assertEqual(result["status"], "unverified_coverage")
            self.assertIsNone(result["price_diff"])
            self.assertIn("本轮未完整核验", result["msg"])
            self.assertFalse(any(word in result["msg"] for word in ("消失", "售罄", "不可售", "停飞")))

    def test_g11_full_matrix_bucket_and_partial_history_have_distinct_evidence(self):
        market = history_fixture.market()
        market[3].update(origins=O, destinations=D)
        fs, units = [], []
        for i, (o, d) in enumerate((o, d) for o in O for d in D):
            f, u = history_fixture.product(event="r36-" + str(i))
            f.update(quote(o, d, DAY, i))
            u.update(origin_airport=o, dest_airport=d)
            f["source_collection"] = {"juhe": u}
            fs.append(f)
            units.append(u)
        market[1]["outbound"]["roundtrip_candidate_flights"] = fs
        market[2]["outbound"].update(fetch_evidence_units=units, attempted_airport_pairs=[(o, d) for o in O for d in D])
        offer = history_fixture.offer(market)
        self.assertEqual(offer["coverage_policy"]["airport_coverage_policy"]["matrix_mode"], "full_matrix_v1")
        market[2]["outbound"]["airport_coverage"] = {"complete": False}
        partial = history_fixture.offer(market)
        self.assertEqual((partial["state"], partial["reason"]), ("unknown", "source_coverage_incomplete"))
        self.assertFalse(partial["can_evaluate_current"])
        self.assertFalse(partial["can_append_sample"])
        self.assertFalse((self.root / "prices.db").exists())

    def test_g12_optional_uncollected_dates_do_not_degrade_a_complete_main_matrix(self):
        payload, context, _ = self.payload(partial=False)
        self.assertFalse(context.get("active"))
        self.assertIn("4/4", notifier._data_freshness_headline(payload), "COMPLETE_MAIN_MATRIX_DISCLOSURE")
        sub = self.subscription()
        sub["date_flexibility"] = 1
        with patch.object(aggregation, "cached_fetch", return_value=({"source_status": "skipped_panel_only",
                         "flights": [], "raw_result_count": 0}, "skipped")), redirect_stdout(io.StringIO()):
            rows = main.collect_nearby_dates(aggregation.FlightAggregator([Source()], []), sub,
                                             target_min_price=1200, fresh_scope="primary_only")
        self.assertTrue(all(r["today_uncollected"] for r in rows if not r["selected"]))
        self.assertFalse(notifier._build_source_degradation_context(source_stats=payload["source_stats"],
                            last_snapshot={}, source_errors=[]).get("active"))

    def test_g13_email_and_detail_final_bodies_include_all_coverage_reasons(self):
        payload, _, _ = self.payload()
        with redirect_stdout(io.StringIO()):
            bodies = [notifier.render_email(deepcopy(payload))[1], notifier.render_detail_html(deepcopy(payload))]
        for body in bodies:
            self.assertIn("1/4", body, "FINAL_BODY_COVERAGE")
            for o in O:
                for d in D:
                    self.assertIn(o + "→" + d, body)
            self.assertIn("synthetic request failed", body)
            self.assertIn("不可比较／覆盖不完整", body)

    def test_g14_pushplus_full_and_compact_keep_final_coverage_disclosure(self):
        payload, _, _ = self.payload()
        render = notifier.render_pushplus_sections(payload)
        final = notifier._prepare_pushplus_content(render)
        self.assertIn("1/4", final, "PUSHPLUS_FINAL_COVERAGE")
        extra = PushSection("r36_long_optional", 3, "x" * (26000 - len(final)), False)
        long = PushRender(render.title, (*render.sections, extra), render.detail_url)
        prepared = notifier.prepare_push_render(long)
        self.assertEqual(prepared.mode, "compact")
        compact = notifier._prepare_pushplus_content(long)
        for text in ("1/4", "PVG→KIX", "SHA→ITM", "synthetic request failed"):
            self.assertIn(text, compact)

    def test_g15_minimal_truncation_reports_conflict_and_never_sends(self):
        disclosure = "去程覆盖(2026-10-01):1/100 个机场组合已完成 | 未覆盖:" + "SHA→ITM(配额跳过);" * 100
        render = PushRender("offline", (PushSection("current_judgment", 0, "x" * 31000, True),
                                       PushSection("data_freshness", 0, disclosure, True)), None)
        self.assertEqual(notifier.prepare_push_render(render).mode, "minimal")
        with patch.object(notifier, "safe_log") as log:
            with self.assertRaisesRegex(ValueError, "PushPlus披露冲突"):
                notifier._prepare_pushplus_content(render)
            self.assertTrue(any("PushPlus披露冲突" in str(c) for c in log.call_args_list))
        with patch.dict(os.environ, {"NO_LIVE_API": "0", "PUSHPLUS_TOKEN": "offline-token"}), \
                patch.object(notifier, "_post_pushplus") as post, \
                patch.object(notifier, "_log_notification") as local_log, \
                patch.object(notifier.httpx, "post") as http_post, \
                patch.object(notifier, "safe_log") as log:
            self.assertFalse(notifier.send(render))
            self.assertTrue(any("PushPlus披露冲突" in str(c) for c in log.call_args_list))
        local_log.assert_called_once()
        post.assert_not_called()
        http_post.assert_not_called()

    def test_g16_quota_skips_keep_the_denominator_and_retry_bound_is_unchanged(self):
        agg, data = self.matrix(MatrixAggregator({(o, d): "quota" for o in O for d in D if (o, d) != (O[0], D[0])}))
        cov = self.coverage(agg, data)
        self.assertEqual((cov["planned_pair_count"], cov["uncovered_pair_count"]), (4, 3))
        self.assertTrue(all(r["state"] == "uncovered_skipped" for r in cov["pair_records"][1:]))
        source = Source()
        source.name = "offline-r36"
        source.fetch.side_effect = [OSError("offline io"), {"source_status": "success", "flights": [], "raw": {}}]
        with patch.object(request_cache.time, "sleep"), redirect_stdout(io.StringIO()):
            request_cache.cached_fetch(source, "PVG", "KIX", DAY, force_fresh=True, persist=False)
        self.assertEqual(source.fetch.call_count, 2)

    def test_g17_locked_notification_functions_are_unchanged(self):
        from test_legacy_notification_renderer_retirement import EXPECTED_MAIN_CHAIN_SHA256
        source = (ROOT / "notifier.py").read_text(encoding="utf-8")
        tree = ast.parse(source)
        for name, expected in EXPECTED_MAIN_CHAIN_SHA256.items():
            nodes = [n for n in tree.body if isinstance(n, ast.FunctionDef) and n.name == name]
            self.assertEqual(len(nodes), 1)
            self.assertEqual(hashlib.sha256(ast.get_source_segment(source, nodes[0]).encode()).hexdigest(), expected)

    def test_g18_frozen_carriers_and_detail_are_byte_identical(self):
        payload = json.loads((ROOT / "tests/fixtures/frozen_email/economy_payload.json").read_text(encoding="utf-8"))
        with patch.object(config_loader, "RUNTIME_CONFIG_PATH", self.root / "nonexistent_runtime_config.yaml"), \
                redirect_stdout(io.StringIO()):
            outputs = (notifier.render_email(deepcopy(payload))[1],
                       notifier.prepare_push_render(notifier.render_pushplus_sections(deepcopy(payload))).content,
                       notifier.render_detail_html(deepcopy(payload)))
        specs = ((53307, "1dd353d2b7bcafc02087957ccccb6661478e20472a22fb3e15dae6a56473ee6b"),
                 (5552, "2ac0e80d9f82dbe7ebccf524c0832f09d42d916212b6178316b0b7bcd60574ff"),
                 (54248, "e31b8728b8d0bc05c9bbd4bd99c299aaf6dca7b66674b6c560ec089b1681ffa6"))
        for value, (size, digest) in zip(outputs, specs):
            raw = value.encode("utf-8")
            self.assertEqual((len(raw), hashlib.sha256(raw).hexdigest()), (size, digest))


if __name__ == "__main__":
    unittest.main()
