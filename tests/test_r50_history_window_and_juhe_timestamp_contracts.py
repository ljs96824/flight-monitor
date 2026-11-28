"""R50: observed lead-time windows and the age of a cached Juhe quote."""
import ast
from contextlib import ExitStack, closing
from datetime import date, datetime, timedelta, timezone
import hashlib
import json
import os
from pathlib import Path
import sqlite3
import tempfile
import unittest
from unittest.mock import Mock, patch

import analyzer
import config_loader
import notifier
import observations_store
import observation_time
import plan_tracker
import request_cache
from sources import aggregator, juhe_source
from tests import test_r44_roundtrip_full_pool_selection_contracts as rt
from tests import test_r49_baggage_and_source_truth_contracts as frozen
from tests import test_return_phase_processing as normal

ROOT = Path(__file__).resolve().parents[1]
SH = timezone(timedelta(hours=8))
DAY = date(2026, 9, 1)
ARGS = ("PVG", "PEK", "2026-09-30")
LOCKS = {
    ("notifier.py", None): {
        "build_notification_payload": "5db09e4d9d42c04def2766c544eb02f5fed5e24b2cfe0486a34b0c5d3ff14714",
        "render_pushplus_sections": "75f4f4f448dc777f9e763dc8da6f638fd3da24d7cbe64db15791e4ec97b314a6",
        "render_email": "1dda41baa9b20b21fd14b3e703cafcd3b4e1cdbeb0150d617d7024f8ab217955",
        "render_detail_html": "cefe927d905e3f0cbe32c7f032dc2f81aa988e84a241eac70587175d86a539f6",
    },
    ("analyzer.py", None): {
        "verify_fare_rules": "87ddfdf2bfdfc728057901b36751a544287568ef6f54ba1d8a14638daf43e082",
        "parse_flight_time": "dfbff457554895bf182c1c80900ac82f64c94d7aeaff28189ce547c85c31069f",
        "overall_score": "c762984d55d286c49e4f69387290c131161c5e8ebdcfaee796fdcf1f38296745",
        "calc_transfer_risk": "660cb5ea9a31f053122f705765a3299969f0bd14555c7d65509e5d3ad982e829",
        "_min_layover_minutes": "334a2adbf312e3a90fff48d7ac830900efedd271e83db62a5b07820240dd04fe",
        "match_time_preference": "ce0bbaf1024f6d98e16271b3580a5d7b55c8b0ebda1fcbdb636e9163f5f4d5ce",
        "conditional_percentile": "ae63ab43033c209f0c7cd43996373cc62b9b21ebf28a85bbbfd90e4c47df234b",
    },
    ("sources/juhe_source.py", None): {
        "_is_success_response": "098fc398630a606e0786e66582023cc83519525e1c10a0fa6734ba0c4435239a",
        "_is_invalid_date_response": "065c96168e5da5e28f887fa2f3a8d6822b36d5e042c7f3c4736dfc2f9d03fd13",
        "_is_quota_response": "77177698c4fe1c7949464b2be72a397e73362d7a27f1e2f391c78d194e4a315b",
    },
    ("sources/juhe_source.py", "JuheSource"): {
        "preflight_skip": "38be6d3ddab4c5dd191dfce4712afadac2528d2c2e123323e6ae6c396cf03ed6",
        "build_request_params": "4ed241005454422e02afc5deac674fa7a32803acd739e8de610a1594200006a5",
        "parse": "56d0005b54c4ffffc2e070650a3c613235cbc75267f6d516b51263a381e3fbe5",
    },
}


def clock(instant, local_zone):
    class Fixed(datetime):
        tick = instant

        @classmethod
        def now(cls, tz=None):
            value = cls.tick.replace(tzinfo=local_zone)
            return value.astimezone(tz) if tz is not None else value.replace(tzinfo=None)
    return Fixed


def oneway(n, target=30):
    depart = DAY + timedelta(days=target)
    def point(lead, hour, price):
        ts = datetime.combine(depart - timedelta(days=lead), datetime.min.time(), SH)
        return ts.replace(hour=hour).timestamp(), price
    data = [point(60, i, 800) for i in range(6)]
    leads = [37, 34, 30, 29, 25, 23] if target else [0] * 6
    data.extend(point(lead, i * 4, 1350 + i) for i, lead in enumerate(leads[:n]))
    with patch.object(analyzer, "shanghai_today", return_value=DAY), patch.object(
        analyzer, "datetime", clock(datetime(2026, 9, 1, 12), SH)
    ):
        return analyzer.calculate_price_references(2000, data, [], target, [])


class TestTrack1OnewayConditionalMinWindow(unittest.TestCase):
    def test_g01_oneway_conditional_min_uses_lead_days_before_departure_not_record_age(self):
        refs = oneway(6)
        self.assertEqual(refs["absolute_min"]["price"], 800)
        match = refs.get("conditional_min") or {}
        self.assertEqual((match.get("price"), match.get("sample_size"), match.get("window")),
                         (1350, 6, ["2026-08-25", "2026-09-08"]))

    def test_g02_oneway_conditional_min_omitted_when_only_2d_samples_exist_or_under_five_in_window(self):
        refs = oneway(4)
        self.assertEqual(refs["absolute_min"]["sample_size"], 10)
        self.assertNotIn("conditional_min", refs)
        current = oneway(5, 0).get("conditional_min") or {}
        self.assertEqual((current.get("sample_size"), current.get("window")), (5, ["2026-09-01"] * 2))


class TestTrack2RoundtripConditionalMinWindow(unittest.TestCase):
    def test_g03_roundtrip_conditional_min_filters_d_plus_minus_7_and_separates_from_absolute_min(self):
        rows = [{"date": "2026-" + day, "total": price, "outbound": 1000, "return": price - 1000}
                for day, price in (("08-26", 1500), ("09-10", 1600), ("09-13", 2100),
                                   ("09-20", 2200), ("09-27", 2150))]
        out = rt.analysis([rt.leg("MU411", 1000)])
        back = rt.analysis([rt.leg("MU412", 1150, True)])
        out["days_to_dept"] = 10
        with patch.object(analyzer, "shanghai_today", return_value=date(2026, 9, 20)):
            direct = analyzer.analyze_roundtrip_prices(rows, 2150, 1000, 1150, days_to_dept=10)
            trip = rt.select(out, back, history=rows)
        for analysis in (direct, trip["price_analysis"]):
            refs = analysis["references"]
            self.assertEqual(refs["absolute_min"]["price"], 1500)
            cond = refs.get("conditional_min") or {}
            self.assertEqual((cond.get("price"), cond.get("sample_size"), cond.get("window")),
                             (2100, 3, ["2026-09-13", "2026-09-27"]))
        route = {"round_trip": True, "origin": "PVG", "destination": "KIX", "depart_date": "2026-09-30",
                 "return_date": "2026-10-06", "route_type": "international"}
        with tempfile.TemporaryDirectory() as tmp, ExitStack() as stack:
            stack.enter_context(patch.object(config_loader, "RUNTIME_CONFIG_PATH", Path(tmp) / "missing.yaml"))
            stack.enter_context(patch.object(plan_tracker, "DEFAULT_DATA_DIR", Path(tmp)))
            for name in ("get_last_push_snapshot", "get_last_push_price", "track_plan_status"):
                stack.enter_context(patch.object(notifier, name, return_value=None))
            payload = notifier.build_notification_payload(
                dict(out, round_trip_analysis=trip), route_info=route, subscription={"id": "test-r50"},
                price_history=[], source_stats={"juhe": {"count": 1, "status": "success"}})
        refs = payload["price_references"]
        self.assertEqual(refs["absolute_min"]["price"], 1500)
        cond = refs["conditional_min"]
        window = ["2026-09-13", "2026-09-27"]
        self.assertEqual((cond["price"], cond["sample_size"], cond["window"]), (2100, 3, window))
        evidence = cond["provenance"]
        self.assertEqual((evidence["sample_n"], evidence["window"]), (3, window))
        self.assertIn("提前购买天数=10±7", evidence["bucket"])

        lines = []
        notifier._append_roundtrip_price_reference(lines, trip, route)
        fragment = "\n".join(lines)
        self.assertIn("历史往返最低：¥1,500", fragment)
        self.assertIn("同条件往返最低（提前10天±7天）：¥2,100", fragment)
        self.assertIn("基于3个往返价格点", fragment)

    def test_g04_roundtrip_conditional_min_omitted_when_all_rows_outside_window_and_respects_explicit_row_lead_days(self):
        rows = [{"date": "2026-08-26", "total": 1500}]
        def refs():
            return analyzer.analyze_roundtrip_prices(rows, rows[-1]["total"], None, None,
                                                    days_to_dept=10)["references"]
        with patch.object(analyzer, "shanghai_today", return_value=date(2026, 9, 20)):
            self.assertNotIn("conditional_min", refs())
            rows.extend([{"date": "2026-08-15", "days_to_dept": 10, "total": 1800},
                         {"date": "2026-08-21", "depart_date": "2026-08-31", "total": 1900},
                         {"date": "2026-09-19", "days_to_departure": 40, "total": 900},
                         {"date": "2026-09-20", "depart_date": "2026-09-01", "total": 800}])
            cond = refs()["conditional_min"]
            self.assertEqual((cond["price"], cond["sample_size"], cond["window"]),
                             (1800, 2, ["2026-08-15", "2026-08-21"]))


class TestTrack3JuheInternalCacheTimestampAndObservationTruth(unittest.TestCase):
    def setUp(self):
        self.stack = self.enterContext(ExitStack())
        self.root = Path(self.stack.enter_context(tempfile.TemporaryDirectory()))
        self.outer = self.root / "outer"
        request_cache.reset_for_tests(self.outer)
        self.addCleanup(request_cache.reset_for_tests, None)
        self.stack.enter_context(patch.dict(os.environ, {"JUHE_FLIGHT_KEY": "offline-fixture"}))
        self.stack.enter_context(patch.object(juhe_source, "_cache_dir", return_value=self.root))
        self.stack.enter_context(patch.object(juhe_source, "shanghai_today", return_value=DAY))
        self.stack.enter_context(patch.object(config_loader, "RUNTIME_CONFIG_PATH", self.root / "missing.yaml"))
        for module in (juhe_source, request_cache, aggregator):
            self.stack.enter_context(patch.object(module, "safe_log"))
        self.raw = {"error_code": 0, "result": {"flightInfo": [{
            "flightNo": "MU5111", "airline": "MU", "departure": "PVG", "arrival": "PEK",
            "departureDate": ARGS[2], "arrivalDate": ARGS[2], "departureTime": "10:30",
            "arrivalTime": "12:30", "ticketPrice": 1100, "duration": "02h00m"}]}}
        self.http = self.stack.enter_context(patch("requests.get", return_value=Mock(
            status_code=200, text="offline", json=lambda: self.raw)))
        self.source = juhe_source.JuheSource()

    def setup_clock(self, hour=10, zone=SH):
        t0 = datetime(2026, 9, 1, hour)
        fixed = clock(t0, zone)
        for module in (juhe_source, request_cache, observations_store, observation_time, aggregator):
            self.stack.enter_context(patch.object(module, "datetime", fixed))
        return t0, fixed

    def cached(self, **opts):
        return request_cache.cached_fetch(self.source, *ARGS, include_cache_details=True, **opts)

    def test_g05_juhe_internal_cache_hit_preserves_original_fetched_at_and_read_cache_returns_raw_dict(self):
        t0, fixed = self.setup_clock()
        fresh = self.source.fetch(*ARGS)
        self.assertEqual((fresh["source_status"], fresh["collected_at"]), ("success", t0.isoformat()))
        fixed.tick += timedelta(minutes=12)
        self.assertEqual(self.source._read_cache(*ARGS, "economy"), self.raw)
        result = self.source.fetch(*ARGS)
        self.http.assert_called_once()
        self.assertEqual((result["source_status"], result["collected_at"]), ("cache", t0.isoformat()))
        self.assertEqual([f["collected_at"] for f in result["flights"]], [t0.isoformat()])

    def test_g06_cached_fetch_force_fresh_over_juhe_cache_marks_cache_reused_and_does_not_extend_ttl(self):
        t0, fixed = self.setup_clock()
        self.assertTrue(self.source._write_cache(*ARGS, "economy", self.raw))
        fixed.tick += timedelta(minutes=12)
        result, status, kind = self.cached(force_fresh=True)
        self.assertEqual((status, kind), ("cache", "internal_source_cache"))
        for row in [result, *result["flights"]]:
            self.assertEqual((row["collection_state"], row["collection_label"]), ("cache_reused", "缓存复用10:00"))
        key = request_cache.cache_key(self.source, *ARGS)
        entry = request_cache._request_cache[key]
        self.assertEqual((entry["fetched_at"], entry["cache_status"]), (t0.isoformat(), "cache"))
        path, = self.outer.glob("api_juhe_*.json")
        self.assertEqual(json.loads(path.read_text(encoding="utf-8"))["fetched_at"], t0.isoformat())
        self.http.assert_not_called()
        fixed.tick += timedelta(minutes=4)
        with patch.object(request_cache, "panel_reuse_result", return_value=None):
            refreshed, status, _ = self.cached()
        self.http.assert_called_once()
        self.assertEqual((status, refreshed["collected_at"]), ("fresh", fixed.tick.isoformat()))

    def test_g07_observations_store_records_original_juhe_cache_timestamp_across_shanghai_and_utc_runners(self):
        for hour, zone in ((9, SH), (1, timezone.utc)):
            db = self.root / f"round-{hour}.sqlite3"
            tokens = observations_store.set_current_round(f"r50-{hour}", db)
            self.stack.callback(observations_store.reset_current_round, tokens)
            _, fixed = self.setup_clock(hour, zone)
            self.assertTrue(self.source._write_cache(*ARGS, "economy", self.raw))
            fixed.tick += timedelta(minutes=11)
            result, _, _ = self.cached(force_fresh=True)
            with closing(sqlite3.connect(db)) as conn:
                rows = conn.execute("SELECT observed_at, observed_at_utc FROM observations").fetchall()
            self.assertEqual(rows, [("2026-09-01T09:00:00+08:00", "2026-09-01T01:00:00+00:00")])
            self.assertIs(result.get("evidence_qualified"), False)
            self.assertEqual(result.get("evidence_unqualified_reason"), "internal_source_cache_unverified")
        self.http.assert_not_called()

    def test_g08_aggregator_and_notifier_render_juhe_internal_cache_as_cache_reuse_not_realtime(self):
        t0, fixed = self.setup_clock()
        self.assertTrue(self.source._write_cache(*ARGS, "economy", self.raw))
        fixed.tick += timedelta(minutes=12)
        data = aggregator.FlightAggregator([self.source], []).collect(
            *ARGS, cabin_classes=["economy"], force_fresh=True)
        row = data["collection_freshness"][0]
        self.assertEqual((row["state"], row["collected_at"]), ("cache_reused", t0.isoformat()))
        self.assertEqual(notifier._freshness_state_text(row), "缓存复用10:00")
        self.http.assert_not_called()


class TestTrack4AstLocksHandoffAndFrozenCarriers(unittest.TestCase):
    def test_g09_ast_anchors_and_single_line_r41_handoff_are_exact(self):
        for (rel, cls), expected in LOCKS.items():
            src = (ROOT / rel).read_text(encoding="utf-8")
            body = ast.parse(src).body
            if cls:
                owner, = [n for n in body if isinstance(n, ast.ClassDef) and n.name == cls]
                body = owner.body
            actual = {n.name: hashlib.sha256(ast.get_source_segment(src, n).encode("utf-8")).hexdigest()
                      for n in body if isinstance(n, ast.FunctionDef) and n.name in expected}
            self.assertEqual(actual, expected)
    def test_g10_frozen_three_carriers_and_normal_roundtrip_baseline_remain_exact(self):
        fixture = ROOT / "tests/fixtures/frozen_email/economy_payload.json"
        with tempfile.TemporaryDirectory() as tmp, patch.object(config_loader, "RUNTIME_CONFIG_PATH",
                                                               Path(tmp) / "missing.yaml"):
            products = frozen.carriers(json.loads(fixture.read_text(encoding="utf-8")))
        expected = [(53307, "1dd353d2b7bcafc02087957ccccb6661478e20472a22fb3e15dae6a56473ee6b"),
                    (5552, "2ac0e80d9f82dbe7ebccf524c0832f09d42d916212b6178316b0b7bcd60574ff"),
                    (54248, "e31b8728b8d0bc05c9bbd4bd99c299aaf6dca7b66674b6c560ec089b1681ffa6")]
        self.assertEqual([(len(b), hashlib.sha256(b).hexdigest()) for b in
                          (s.encode("utf-8") for s in products)], expected)
        result = unittest.TestResult()
        unittest.TestSuite([normal.ReturnPhaseProcessingTest("test_normal_roundtrip_baseline")]).run(result)
        self.assertTrue(result.wasSuccessful(), result.errors + result.failures)
        self.assertEqual(result.testsRun, 1)
