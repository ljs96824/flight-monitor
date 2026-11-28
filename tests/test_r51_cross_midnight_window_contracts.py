"""Contracts for meeting-window clocks relative to the requested travel date."""

import ast
from copy import deepcopy
from datetime import datetime
from hashlib import sha256
import inspect
import io
import json
from contextlib import redirect_stdout
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch

import analyzer
import config_loader
import notifier
from tests import test_r40_same_day_combo_contracts as r40
from tests import test_return_phase_processing as scenario


ROOT = Path(__file__).resolve().parents[1]
DAY = r40.DAY
OB = "outbound_arrive_by"
RT = "return_depart_after"
FILES = {
    "notifier.py": (663094, "fb1211339bbfc75789eb40ae0ae0a99807c2d19008e41394735446b8ed090702"),
    "main.py": (175530, "878a6df6d84276cde4f092f42d1c0453d08c5a8ceafdc638a68dc0561c4c13ef"),
    "airport_logistics.py": (8211, "62c5915d88783dd4ecaec5a5e285897208f497f5382ba16f6e5917c09bf25109"),
    "constraint_fingerprint.py": (11459, "b396aef5431cc976754bab2a5a2474641b78d38002844a808521b2f228c415a9"),
    "constraint_summary.py": (3595, "ab7a1505ff953891c7959e7e2412bc3ce7f93f93732e48c7557f5d43ebb3752b"),
    "web_form.py": (125145, "47f45bc97aa879bfb48e996482f13a504f1d68bc85fc0d893e885ea5b0f9ab1d"),
    "form_concepts.py": (37206, "cf535034c4242c291b6cf1f66c7335497e0fb2e4ecbf238492662b6621e1e9b6"),
    "form_structure.py": (53116, "5b9160c3cec816afeb92bf13dc868f1405c8db1eda3b6bb41479bc399b063882"),
    "plan_tracker.py": (47076, "b139465d01cf1f6e430f805a28aefb86b1085f0915825844c4409d16556f8f11"),
    "storage.py": (47792, "551a7a08964f49dc93e60eb25a6c9b78519b1244805d4408ae47b9a88673f4c3"),
    "observations_store.py": (19506, "64ed00efd4072e8cb458d28f5f8380ea0579151b87a8eea1605da050df3f8ae6"),
    "sources/juhe_source.py": (21312, "1969a55d15947c31edac73134735e18727bdb3a89b06f8aa9797beaa5b6eff3d"),
    "sources/aggregator.py": (65083, "01fcf0d1d10b125f16ed635b86e4fc2cdec4e6a92e53463b15878e68083bce82"),
    "tests/fixtures/form_normalization_baseline_v1.json": (180179, "88172833002fbe747b289848341a85d4c23470070339a4775ce2f61f183a2fd9"),
    "tests/fixtures/frozen_email/economy_payload.json": (168149, "85ee48c5539020e3cec3e2ec444d9a7f9403c4c259213c7840750c7788eb275b"),
    "test_same_day_business_mode.py": (77107, "066ae51ff5c0c7500d5a8a992558bb037fa03e0b337e9ddbaa81c5349e8a2dcd"),
    "tests/test_r40_same_day_combo_contracts.py": (29469, "57e93660bb27ea80644f261f85b5900826bb0dc4e81c6e563bf6808b53133744"),
    "tests/test_r49_baggage_and_source_truth_contracts.py": (13764, "1c6db27b2d1484cd3c0fd1197e1f5ae0687fa177370b5ac92128b37aabc20b5e"),
}
SCOPES = {
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
        "calculate_price_references": "2bed9302d3906bde5056ac12f301b32313adbe4776ec0665757a7f5e970fcc83",
        "analyze_roundtrip_prices": "dc560ba48f9920a2d49eaae3c3e93b652afa58db50943555e667f36a31484c1f",
        "_minutes_datetime": "5db57eb07bab030345e56b18e39187bb3bb3798fd160893890329f5d4353388a",
        "_assert_dated_datetime": "2894d2ad899d49e7734fd9e94c0507b2996c8f7f8a7d323c8d85fe35b0d1600e",
        "_same_day_outbound_passes_window": "021bf32d509b8d4290189bfeb344e5679dd4b773b87d3d035e16d58e79c94d16",
        "_same_day_return_passes_window": "cd498b9394640c67b82989c5ec3caa6b369ba083481dcc570ea4c951b8250eda",
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
INSPECT = {
    "parse_flight_time": "9a55d7e7d427adf2469b269546262ebc3d3264ec7f7cae4b7df537f1a4177297",
    "match_time_preference": "2e21c6f1cb34a5300b194c59b167b52753e321f2d7e696685c153f64c98af93f",
}


def carriers(payload):
    _, email = notifier.render_email(deepcopy(payload))
    detail = notifier.render_detail_html(deepcopy(payload))
    push = notifier.prepare_push_render(notifier.render_pushplus_sections(deepcopy(payload))).content
    return email, push, detail


class TestTrack1DayOffsets(r40.ContractBase):
    def test_g01_minutes_to_text_preserves_same_day_and_formats_day_offsets(self):
        for n in range(1440):
            self.assertEqual(analyzer._minutes_to_text(n), f"{n // 60:02d}:{n % 60:02d}")
        for n, text in (
            (-0.4, "00:00"), (-1, "前一日23:59"), (-60, "前一日23:00"),
            (-1440, "前一日00:00"), (-1441, "前2日23:59"), (-2880, "前2日00:00"),
            (1439.6, "次日00:00"), (1440, "次日00:00"), (1500, "次日01:00"),
            (2879, "次日23:59"), (2880, "+2日00:00"), (2945, "+2日01:05"),
            (None, ""), (True, ""), (False, ""), (float("nan"), ""),
            (float("inf"), ""), (float("-inf"), ""), ("09:00", ""),
        ):
            self.assertEqual(analyzer._minutes_to_text(n), text, n)

    def test_g02_compute_same_day_windows_and_reserve_text_keep_cross_midnight_offsets_across_all_models(self):
        for model, opts in (
            ("meeting_fixed", dict(meeting_importance="important", destination_transport_min=110)),
            ("airport_split", dict(user_transport_min=110)),
            ("legacy", dict(buffer_hours=3)),
        ):
            for airport in ("PKX", "PEK"):
                windows = analyzer.compute_same_day_windows(
                    dict(opts, business_start="01:00", business_end="22:30"), None, airport
                )
                self.assertEqual(windows.get("buffer_model", "legacy"), model)
                self.assertEqual((windows["business_start"], windows["business_end"]), ("01:00", "22:30"))
                breakdown = windows["reserve_breakdown"]["windows"]
                for key, short, prefix in ((OB, "arrive_by", "前一日"), (RT, "depart_after", "次日")):
                    self.assertTrue(windows[key].startswith(prefix), (model, airport, key, windows[key]))
                    self.assertEqual(windows[key+"_by_airport"][airport], windows[key])
                    self.assertEqual(breakdown[short], windows[key])
                if model == "meeting_fixed":
                    for k in ("latest_landing_without_preparation", "business_safety_arrive_by"):
                        self.assertTrue(windows[k].startswith("前一日"))
                        self.assertEqual(windows[k], breakdown[k])
                summary = notifier._same_day_reserve_text(windows)
                self.assertIn("去程需前一日", summary)
                self.assertIn("返程次日", summary)

    def test_g03_window_text_roundtrips_to_identical_minutes_and_dated_datetimes(self):
        self.assertTrue(hasattr(analyzer, "_parse_window_text_minutes"))
        for m in (-99*1440, -2880, -1441, -1440, -90, -1, 0, 480, 1439, 1440, 1500, 2879, 2880, 99*1440):
            text = analyzer._minutes_to_text(m)
            self.assertEqual(analyzer._parse_window_text_minutes(text), m)
            self.assertEqual(analyzer._window_datetime_from_minutes_or_text({OB: text}, DAY, OB+"_minutes", OB),
                             analyzer._minutes_datetime(DAY, m))


class TestTrack2WindowFilters(r40.ContractBase):
    def test_g04_text_only_cross_midnight_windows_enforce_dated_bounds_without_stripping_prefixes(self):
        late = r40.flight("20:00", "22:00")
        early = r40.flight("06:30", "08:30")
        ret = r40.flight("18:00", "20:00", back=True)
        night_ret = r40.flight("23:15", "23:55", back=True)
        for upper in ("前一日23:00", "前一晚23:00", "前2日22:30", "23:00-1", "2026-11-30 23:00"):
            w = {OB: upper, RT: "18:00"}
            self.assertFalse(analyzer._same_day_outbound_passes_window(late, w, DAY), upper)
            self.assertEqual(analyzer.build_same_day_combos([late], [night_ret], w, DAY), [])
            by_airport = {OB+"_by_airport": {"PKX": upper}, RT+"_by_airport": {"PKX": "18:00"}}
            self.assertEqual(analyzer.build_same_day_combos([late], [night_ret], by_airport, DAY), [])
        for lower in ("次日01:00", "+2日00:30", "01:00+1", "2026-12-02 01:00"):
            w = {OB: "09:00", RT: lower}
            self.assertFalse(analyzer._same_day_return_passes_window(ret, w, DAY), lower)
            self.assertEqual(analyzer.build_same_day_combos([early], [ret], w, DAY), [])
            by_airport = {OB+"_by_airport": {"PKX": "09:00"}, RT+"_by_airport": {"PKX": lower}}
            self.assertEqual(analyzer.build_same_day_combos([early], [ret], by_airport, DAY), [])
        for upper, lower in (("09:00", "18:00"), ("09:00+0", "18:00+0"),
                             ("2026-12-01 09:00", "2026-12-01 18:00")):
            self.assertTrue(analyzer.build_same_day_combos([early], [ret], {OB:upper, RT:lower}, DAY))
        explicit = {RT: "2026-12-02 01:00", RT+"_minutes": 0}
        self.assertEqual(analyzer._window_datetime_from_minutes_or_text(explicit, None, RT+"_minutes", RT),
                         datetime(2026, 12, 2, 1))

    def test_g05_malformed_window_texts_and_invalid_minutes_fail_closed(self):
        ob = r40.flight("06:00", "07:30")
        ret = r40.flight("19:00", "21:00", back=True)
        for v in ("前0日08:00", "+0日08:00", "前100日08:00", "+100日08:00", "前一日24:00",
                  "次日01:60", "次日", "前一日bad", "10:00a", "flight at 08:00", "08:00-00",
                  "08:00-0", "次日08:00+1", " 8:00 extra ", True, False, float("nan"), float("inf"), float("-inf")):
            for target, row, passes in ((OB, ob, analyzer._same_day_outbound_passes_window),
                                       (RT, ret, analyzer._same_day_return_passes_window)):
                w = {OB: "09:00", RT: "18:00"}
                w[target if isinstance(v, str) else target+"_minutes"] = v
                self.assertIsNone(analyzer._window_datetime_from_minutes_or_text(w, DAY, target+"_minutes", target), v)
                self.assertFalse(passes(row, w, DAY), v)
                self.assertEqual(analyzer.build_same_day_combos([ob], [ret], w, DAY), [], v)

    def test_g06_debug_rows_closest_outbound_and_dateless_fallbacks_respect_cross_midnight_and_zero_minute(self):
        ret = {"departure_time": "21:00", "arrival_time": "23:00", "price": 800}
        w = {RT: "次日01:00"}
        debug, = analyzer._same_day_return_window_debug_rows([ret], w, DAY)
        self.assertEqual(debug["return_depart_after_datetime"], "2026-12-02 01:00:00")
        self.assertIs(debug["passed"], False)
        self.assertEqual(analyzer._same_day_window_return_candidates([ret], w, None), [])
        self.assertTrue(analyzer._same_day_window_return_candidates([ret], {RT: "19:00"}, None))
        for w, delay, text in (({OB: "前一日23:00"}, 510, "晚8小时30分钟"),
                               ({OB+"_minutes": 0}, 450, "晚7小时30分钟")):
            row, = analyzer._closest_same_day_outbound_options([{"arrival_time": "07:30"}], w, None)
            self.assertEqual((row["meeting_arrival_delay_minutes"], row["meeting_arrival_note"]), (delay, text))


class TestTrack3DiagnosticsAndDelivery(r40.ContractBase):
    def payload(self, prefs, out, ret, next_ret=()):
        legs = r40.leg_analyses(out, ret, prefs, next_returns=next_ret)
        result = analyzer.analyze_round_trip(*legs, emit_diagnostics=False)
        self.assertEqual(result["top_combinations"], [])
        route = dict(prefs, origin="PVG", destination="PKX", route_type="domestic", return_date=DAY, round_trip=True)
        return notifier.build_notification_payload(dict(result, round_trip_analysis=result), *legs, route_info=route)

    def test_g07_early_morning_cross_midnight_outbound_note_alternatives_and_three_carriers(self):
        prefs = dict(r40.BASE_PREFS, business_start="01:00", destination_transport_min=110)
        ret = r40.flight("19:30", "21:30", back=True)
        red_eye = r40.flight("22:15", "00:20", arrival_day=r40.NEXT)
        note = analyzer._same_day_no_feasible_note([red_eye], [ret], prefs)
        self.assertIn("需前一日", note)
        self.assertNotIn("当日前一日", note)
        out = r40.flight("05:30", "07:30")
        note = analyzer._same_day_no_feasible_note([out], [ret], prefs)
        self.assertIn("需前一日", note)
        payload = self.payload(prefs, [out], [ret])
        card, = [row for row in payload["same_day_alternatives"] if row["category"] == "same_day_earliest"]
        for key in ("unmet_reason", "note"):
            self.assertIn("超出最晚落地窗口(前一日", card[key])
        for text in carriers(payload):
            self.assertIn("需前一日", text)
            self.assertIn("超出最晚落地窗口(前一日", text)
            self.assertNotIn("当日前一日", text)
        late = r40.flight("21:00", "23:50")
        self.assertIn("≥次日", analyzer._same_day_no_feasible_note([late], [ret], prefs))
        late_card = next(c for c in analyzer.build_same_day_alternatives(
            [late], [ret], analyzer.compute_same_day_windows(prefs, None, "PKX"), DAY, constraints=prefs
        ) if c["category"] == "same_day_earliest")
        self.assertTrue(late_card["meeting_arrival_time"].startswith("次日"))

    def test_g08_late_evening_cross_midnight_return_floor_multi_airport_and_three_carriers(self):
        prefs = dict(same_day_round_trip=True, depart_date=DAY, business_start="15:00", business_end="22:30",
                     meeting_location="大兴区", meeting_importance="important")
        out = r40.flight("06:00", "07:30")
        ret = [r40.flight("21:00", "23:00", back=True, airport=a, number=n)
               for a, n in (("PKX", "MU102"), ("PEK", "MU104"))]
        next_ret = [r40.flight("07:00", "09:00", day=r40.NEXT, back=True, number="MU106")]
        note = analyzer._same_day_no_feasible_note([out], ret, prefs)
        for airport in ("PKX", "PEK"):
            self.assertIn(airport+" 次日", note)
        payload = self.payload(prefs, [out], ret, next_ret)
        self.assertIn("next_morning_return", [c["category"] for c in payload["same_day_alternatives"]])
        for text in carriers(payload):
            self.assertIn("【返程时间】", text)
            self.assertIn("次日", text)
            self.assertIn("PKX 次日", text)
            self.assertIn("PEK 次日", text)


class TestTrack4Guards(unittest.TestCase):
    def test_g09_scoped_ast_locks_inspect_hashes_and_protected_files_stay_exact(self):
        with TemporaryDirectory() as tmp, patch.object(config_loader, "RUNTIME_CONFIG_PATH", Path(tmp)/"absent.yaml"):
            for path, expected in FILES.items():
                b = (ROOT/path).read_text(encoding="utf-8").encode("utf-8")
                self.assertEqual((len(b), sha256(b).hexdigest()), expected, path)
            for (path, cls), locks in SCOPES.items():
                src = (ROOT/path).read_text(encoding="utf-8")
                nodes = ast.parse(src).body
                if cls:
                    nodes = next(n for n in nodes if isinstance(n, ast.ClassDef) and n.name == cls).body
                for name, digest in locks.items():
                    node, = [n for n in nodes if isinstance(n, ast.FunctionDef) and n.name == name]
                    b = ast.get_source_segment(src, node).encode("utf-8")
                    self.assertEqual(sha256(b).hexdigest(), digest, name)
            for name, digest in INSPECT.items():
                b = inspect.getsource(getattr(analyzer, name)).encode("utf-8")
                self.assertEqual(sha256(b).hexdigest(), digest, name)
            src = (ROOT/"analyzer.py").read_text(encoding="utf-8")
            self.assertEqual(len(src) - len(src.rstrip("\n")), 7)

    def test_g10_frozen_three_carriers_and_normal_roundtrip_baseline_remain_exact(self):
        with TemporaryDirectory() as tmp, patch.object(config_loader, "RUNTIME_CONFIG_PATH", Path(tmp)/"absent.yaml"):
            payload = json.loads((ROOT/"tests/fixtures/frozen_email/economy_payload.json").read_text(encoding="utf-8"))
            for text, expected in zip(carriers(payload), (
                (53307, "1dd353d2b7bcafc02087957ccccb6661478e20472a22fb3e15dae6a56473ee6b"),
                (5552, "2ac0e80d9f82dbe7ebccf524c0832f09d42d916212b6178316b0b7bcd60574ff"),
                (54248, "e31b8728b8d0bc05c9bbd4bd99c299aaf6dca7b66674b6c560ec089b1681ffa6"),
            ), strict=True):
                b = text.encode("utf-8")
                self.assertEqual((len(b), sha256(b).hexdigest()), expected)
        cls = scenario.ReturnPhaseProcessingTest
        with redirect_stdout(io.StringIO()):
            cls.setUpClass()
            try:
                result = cls("test_normal_roundtrip_baseline").run()
                self.assertEqual(result.testsRun, 1)
                self.assertTrue(result.wasSuccessful(), result.errors + result.failures)
            finally:
                cls.tearDownClass()
