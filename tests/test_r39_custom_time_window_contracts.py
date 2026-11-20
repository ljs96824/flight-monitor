"""R39: 22 semantic RED groups and two isolated compatibility guards."""

import ast
import copy
from contextlib import ExitStack, redirect_stdout
from datetime import date, datetime, timedelta, timezone
import hashlib
import inspect
import io
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from werkzeug.datastructures import MultiDict
from web_test_utils import enable_csrf

import analyzer
import config_loader
import constraint_fingerprint
import form_concepts
import form_structure
import main
import notifier
import storage
import web_form
from tests.form_fixture_contract import without_storage_identity


ROOT = Path(__file__).resolve().parents[1]
TZ8 = timezone(timedelta(hours=8))
AXES = ("departure", "arrival", "outbound_departure", "outbound_arrival", "return_departure", "return_arrival")
FORM = {
    "origin_select": "PVG", "destination": "KIX", "route_type": "international",
    "depart_date": "2026-12-01", "return_date": "2026-12-06", "round_trip": "true",
    "monitor_mode": "precise", "ux2_concept_form": "true", "ux2_time_touched": "true",
    "time_preference": "unlimited", "arrival_preference": "any", "allow_redeye": "false",
    "price_strategy": "explicit", "max_budget": "8000", "target_price": "6000",
    "max_budget_scope": "per_person", "target_price_scope": "per_person",
    "baggage": "not_needed", "lcc_policy": "any", "transfer_policy": "reasonable",
    "adult_count": "1", "child_count": "0", "elderly_count": "0", "infant_count": "0",
    "travel_scenario": ["personal"], "primary_goal": "buy_timing", "notification_method": "page_only",
}


def multidict(values):
    items = []
    for key, value in values.items():
        if isinstance(value, (list, tuple)):
            items.extend((key, str(item)) for item in value)
        elif value is True:
            items.append((key, "true"))
        elif value not in (False, None):
            items.append((key, str(value)))
    return MultiDict(items)


def flight(departure="08:45", arrival="12:00", number="NH100"):
    return {
        "flight_combo": number, "price": 800, "cabin_class": "economy", "stops": 0,
        "total_duration_min": 180, "airlines": ["ANA"], "source": "juhe", "data_source": "juhe",
        "collected_at": "2026-11-17T09:00:00+08:00", "route_summary": "PVG-KIX",
        "departure_time": departure, "arrival_time": arrival,
        "segments": [{"flight_no": number, "airline": "ANA", "dep_airport": "PVG",
                      "arr_airport": "KIX", "dep_time": departure, "arr_time": arrival, "duration_min": 180}],
        "layovers": [],
    }


def custom(departure=None, arrival=None, **updates):
    return {"time_preference_mode": "custom", "departure_time_windows": departure or [],
            "arrival_time_windows": arrival or [], "need_baggage": "not_needed",
            "lcc_policy": "any", "transfer_policy": "reasonable", **updates}


class FixedDatetime(datetime):
    @classmethod
    def now(cls, tz=None):
        instant = cls(2026, 11, 17, 9, 0, 0, tzinfo=TZ8)
        return instant.replace(tzinfo=None) if tz is None else instant.astimezone(tz)


class ContractBase(unittest.TestCase):
    def setUp(self):
        self.stack = ExitStack()
        self.addCleanup(self.stack.close)
        self.root = Path(self.stack.enter_context(tempfile.TemporaryDirectory()))
        self.path = self.root / "subscriptions.json"
        self.path.write_text("[]", encoding="utf-8")
        self.stack.enter_context(patch.dict(os.environ, {"NO_LIVE_API": "1", "PYTHON_DOTENV_DISABLED": "1"}))
        self.stack.enter_context(patch.dict(web_form.app.config, {"TESTING": True}))
        for module, name, value in (
            (config_loader, "RUNTIME_CONFIG_PATH", self.root / "absent_runtime.yaml"),
            (storage, "DB_PATH", self.root / "prices.db"),
            (main, "SUBSCRIPTIONS_PATH", self.path), (web_form, "SUBSCRIPTIONS_PATH", self.path),
            (main, "ANALYSIS_LOG", self.root / "analysis.jsonl"),
            (analyzer, "datetime", FixedDatetime), (main, "datetime", FixedDatetime),
            (web_form, "datetime", FixedDatetime),
        ):
            self.stack.enter_context(patch.object(module, name, value))
        for module in (main, analyzer, storage, web_form):
            self.stack.enter_context(patch.object(module, "safe_log"))
        self.stack.enter_context(patch.object(main, "log_signal", return_value={}))
        self.stack.enter_context(patch.object(web_form, "start_background_collection"))
        self.stack.enter_context(patch.object(analyzer, "shanghai_today", return_value=date(2026, 11, 17)))
        self.stack.enter_context(patch.object(analyzer, "get_target_history", return_value=[]))
        self.stack.enter_context(patch.object(analyzer, "get_latest_alternatives", return_value=[]))
        self.stack.enter_context(patch.object(main, "_departure_reference_now", return_value=datetime(2026, 11, 17, 9, tzinfo=TZ8)))
        for target in ("socket.create_connection", "socket.socket.connect", "smtplib.SMTP", "smtplib.SMTP_SSL"):
            self.stack.enter_context(patch(target, side_effect=AssertionError("R39 must remain offline")))
        self.client = web_form.app.test_client()
        enable_csrf(self.client)

    def submitted(self, **updates):
        response = self.client.post("/subscribe", data=multidict({**FORM, **updates}))
        self.assertEqual(response.status_code, 302, response.get_data(as_text=True)[:300])
        return json.loads(self.path.read_text(encoding="utf-8"))[-1]

    def loaded(self, row):
        self.path.write_text(json.dumps([row], ensure_ascii=False), encoding="utf-8")
        with redirect_stdout(io.StringIO()):
            result = main.load_file_subscriptions()
        self.assertEqual(len(result), 1)
        return result[0]

    def filtered(self, row, prefs):
        with redirect_stdout(io.StringIO()):
            return analyzer._apply_user_preferences([copy.deepcopy(row)], prefs)

    def analyzed(self, rows, prefs):
        with redirect_stdout(io.StringIO()):
            return analyzer.analyze_all_flights(copy.deepcopy(rows), user_preferences=prefs)

    def api(self, name):
        self.assertTrue(hasattr(analyzer, name), "R39 contract API: " + name)
        return getattr(analyzer, name)


class TestTrack1MinutePrecisionAndProvenance(ContractBase):
    def test_g01_minute_lower_and_closed_upper(self):
        prefs = custom([["06:30", "08:30"]])
        for value, expected in (("06:30", True), ("06:45", True), ("08:30", True),
                                ("06:29", False), ("08:31", False), ("08:55", False)):
            self.assertEqual(analyzer.match_time_preference(flight(value), prefs)[0], expected, value)

    def test_g02_seconds_are_truncated_and_upper_is_closed(self):
        prefs = custom([["08:30", "11:15"]])
        for value, expected in (("11:15", True), ("11:15:59", True), ("11:16", False), ("11:50", False)):
            self.assertEqual(analyzer.match_time_preference(flight(value), prefs)[0], expected, value)
        self.assertTrue(analyzer.match_time_preference(flight("23:59"), custom([["06:00", "23:59"]]))[0])

    def test_g03_narrow_and_single_minute_windows(self):
        for start, end, cases in (("08:15", "08:50", (("08:15", True), ("08:30", True), ("08:50", True), ("08:14", False), ("08:51", False))),
                                  ("09:00", "09:00", (("09:00", True), ("08:59", False), ("09:01", False)))):
            for value, expected in cases:
                self.assertEqual(analyzer.match_time_preference(flight(value), custom([[start, end]]))[0], expected)

    def test_g04_cross_midnight_and_union(self):
        prefs = custom([["22:30", "01:30"]])
        for value, expected in (("22:30", True), ("23:59", True), ("00:00", True), ("01:30", True),
                                ("22:29", False), ("01:31", False), ("12:00", False)):
            self.assertEqual(analyzer.match_time_preference(flight(value), prefs)[0], expected, value)
        self.assertTrue(analyzer.match_time_preference(flight("12:15"), custom([["22:30", "01:30"], ["12:00", "12:30"]]))[0])

    def test_g05_unfilled_axes_keep_soft_and_hard_policies(self):
        prefs = custom([["08:00", "10:00"]], [["06:00", "20:00"]],
                       time_window_sources={"departure": "explicit", "arrival": "derived_daytime"})
        self.assertEqual(analyzer.match_time_preference(flight("08:30", "20:30"), prefs), (True, "非白天，排序降权"))
        kept, excluded, _ = self.filtered(flight("08:30", "20:30"), prefs)
        self.assertEqual(len(kept), 1)
        self.assertEqual(excluded, [])
        self.assertEqual(kept[0]["preference_penalty"], 1)
        prefs["time_window_sources"]["arrival"] = "derived_no_redeye"
        self.assertFalse(analyzer.match_time_preference(flight("08:30", "23:00"), prefs)[0])
        prefs.update(same_day_round_trip=True, direction="return")
        self.assertIn("当天往返", analyzer.match_time_preference(flight("08:30", "23:00"), prefs)[1])
        prefs["time_window_sources"]["arrival"] = "derived_no_late"
        self.assertFalse(analyzer.match_time_preference(flight("08:30", "00:30"), prefs)[0])
        self.assertTrue(analyzer.match_time_preference(flight("08:30", "23:15"), prefs)[0])

    def test_g06_colliding_values_survive_persistence_and_edit(self):
        outcomes = []
        for explicit_arrival in (True, False):
            saved = self.submitted(round_trip="false", return_date="", shared_departure_window_start="06:00",
                                   shared_departure_window_end="23:00", shared_arrival_window_start="06:00" if explicit_arrival else "",
                                   shared_arrival_window_end="23:00" if explicit_arrival else "")
            self.assertIn("time_window_sources", saved["hard_constraints"])
            normalized = self.loaded(saved)
            before = analyzer.match_time_preference(flight("08:00", "23:00"), main.subscription_preferences(normalized))[0]
            response = self.client.get("/settings?edit=0")
            self.assertEqual(response.status_code, 200)
            values = form_structure.subscription_to_form_values(saved)
            self.assertEqual(values["shared_arrival_window_start"], "06:00" if explicit_arrival else "")
            values["subscription_id"] = saved.get("subscription_id", "")
            response = self.client.post("/subscribe", data=multidict(values))
            self.assertEqual(response.status_code, 302)
            rebuilt = json.loads(self.path.read_text(encoding="utf-8"))[-1]
            after = analyzer.match_time_preference(flight("08:00", "23:00"), main.subscription_preferences(self.loaded(rebuilt)))[0]
            self.assertEqual(before, after)
            self.assertEqual(without_storage_identity(main.normalize_subscription(saved)), without_storage_identity(main.normalize_subscription(rebuilt)))
            outcomes.append(after)
        self.assertEqual(outcomes, [True, False])

    def test_g07_legacy_records_are_not_promoted(self):
        raw = web_form.build_subscription(multidict({**FORM, "shared_departure_window_start": "06:00", "shared_departure_window_end": "23:00"}))
        for key in ("hard_constraints", "soft_preferences"):
            raw[key].pop("time_window_sources", None)
            raw[key].pop("red_eye_policy_source", None)
        original = copy.deepcopy(raw)
        normalized = main.normalize_subscription(raw)
        self.assertIn("time_window_sources", normalized["hard_constraints"])
        self.assertEqual(raw, original)
        self.assertEqual(normalized["hard_constraints"]["red_eye_policy_source"], "legacy_unverified")
        prefs = main.subscription_preferences(normalized)
        self.assertFalse(analyzer.match_time_preference(flight("23:00"), prefs)[0])
        self.assertTrue(analyzer.match_time_preference(flight("23:00"), custom([["06:00", "23:00"]]))[0])
        prefs["departure_time_windows"] = [["05:00", "08:00"]]
        self.assertEqual(self.filtered(flight("05:45"), prefs)[0], [])


class TestTrack2StrictParsingAndEndpointFallback(ContractBase):
    def test_g08_window_bounds_and_end_of_day_sentinel(self):
        parse = self.api("_parse_window_bound_minutes")
        self.assertEqual(parse("24:00", allow_end_24=True), 1440)
        for value in ("24:00", "24:01", "08:99", "A320 08:30", True, 510, {}, "08:30:00"):
            self.assertIsNone(parse(value), repr(value))
        self.assertTrue(analyzer.match_time_preference(flight("23:59"), custom([["06:00", "24:00"]]))[0])

    def test_g09_range_strings_and_pairs_agree(self):
        strings = custom(["13:00-16:00"])
        pairs = custom([["13:00", "16:00"]])
        self.assertTrue(analyzer.match_time_preference(flight("16:00"), strings)[0])
        for value in ("12:59", "13:00", "14:15", "16:00", "16:01"):
            self.assertEqual(analyzer.match_time_preference(flight(value), strings), analyzer.match_time_preference(flight(value), pairs))
        self.assertTrue(analyzer._matches_time_windows(13, ["13:00-16:00"]))
        self.assertFalse(analyzer._matches_time_windows(16, ["13:00-16:00"]))

    def test_g10_flight_clock_is_strict_and_local(self):
        self.assertIsNone(analyzer._hour_from_time("08:99"))
        parse = self.api("_parse_flight_clock_minutes")
        self.assertEqual(parse("2026-12-01T11:15:59+09:00"), 675)
        self.assertEqual(parse("11:15:59 +1"), 675)
        self.assertEqual(analyzer._hour_from_time("2026-03-02\t01:45+08:00"), 1)
        for value in ("08:99", "A320 08:30", {"t": "08:30"}, "24:00", False, "11:15:99"):
            self.assertIsNone(parse(value), repr(value))

    def test_g11_only_endpoints_and_reliable_top_level_fallback(self):
        row = flight()
        row["segments"] = [{}, {"dep_time": "01:00", "arr_time": "02:00"}, {}]
        self.assertEqual(analyzer._first_departure_hour(row), 8)
        self.assertEqual(analyzer._last_arrival_hour(row), 12)
        row["segments"][0]["dep_time"] = "07:00"
        row["segments"][-1]["arr_time"] = "13:00"
        self.assertEqual(analyzer._first_departure_hour(row), 7)
        self.assertEqual(analyzer._last_arrival_hour(row), 13)
        row["segments"][0]["dep_time"] = "bad"
        self.assertEqual(analyzer._first_departure_hour(row), 8)
        row.pop("departure_time")
        self.assertIsNone(analyzer._first_departure_hour(row))

    def test_g12_constrained_missing_clocks_fail_closed(self):
        self.assertEqual(analyzer.match_time_preference({}, custom([["08:30", "11:15"]])), (False, "无法核实出发时刻是否满足可接受时段"))
        self.assertEqual(analyzer.match_time_preference({}, custom(arrival=[["08:30", "11:15"]])), (False, "无法核实到达时刻是否满足可接受时段"))

    def test_g13_unconstrained_missing_clock_is_not_rejected(self):
        row = flight()
        row.pop("arrival_time")
        row["segments"][0].pop("arr_time")
        self.assertTrue(analyzer.match_time_preference(row, custom([["08:30", "11:15"]]))[0])

    def test_g14_invalid_configuration_and_ux33_inheritance(self):
        for windows in ([["08:99", "11:00"]], [["24:00", "08:00"]], {"start": "08:30"}):
            ok, reason = analyzer.match_time_preference(flight(), custom(windows))
            self.assertEqual(reason, "自定义可接受时段配置格式无效")
            self.assertFalse(ok)
            self.assertEqual(analyzer._filter_constraint_detail({"exclude_reason": reason}, {})[0], "invalid_time_window_config")
        result = form_concepts.derive_time_concept_fields(multidict({**FORM, "shared_departure_window_start": "08:30", "shared_departure_window_end": "11:15", "return_departure_window_start": "09:00"}), round_trip=True)
        self.assertEqual(result["return_departure_time_windows"], [["08:30", "11:15"]])
        self.assertIn("time_window_sources", result)
        self.assertEqual(result["time_window_sources"]["return_departure"], "inherited_explicit")


class TestTrack3EndToEndPlumbingAndRedEyeIsolation(ContractBase):
    def test_g15_arrival_only_in_all_four_paths(self):
        for path in ("oneway", "outbound", "return", "return_only"):
            saved = self.submitted(round_trip="false" if path == "oneway" else "true", shared_arrival_window_start="10:30", shared_arrival_window_end="14:15")
            sub = self.loaded(saved)
            prefs = main.subscription_preferences(sub)
            self.assertEqual(prefs.get("time_preference_mode"), "custom")
            if path in {"return", "return_only"}:
                prefs["direction"] = "return"
            if path == "return_only":
                context = main._return_phase_context(sub, date(2026, 12, 2))
                self.assertIsNotNone(context)
                prefs = main._return_phase_preferences(sub, context)["preferences"]
                self.assertIsNone(prefs.get("max_budget"))
                self.assertIsNone(prefs.get("target_price"))
            self.assertTrue(analyzer.match_time_preference(flight("08:00", "11:15"), prefs)[0])
            self.assertFalse(analyzer.match_time_preference(flight("08:00", "14:16"), prefs)[0])
            self.assertFalse(analyzer.match_time_preference(flight("02:00", "11:15"), prefs)[0])

    def test_g16_symmetric_copy_and_direction_precedence(self):
        sub = {"origin": "PVG", "destination": "KIX", "depart_date": "2026-12-01", "return_date": "2026-12-06", "round_trip": True,
               "soft_preferences": custom([["08:30", "11:15"]], return_departure_time_windows=[["18:30", "20:15"]],
                                           time_window_sources={axis: "explicit" for axis in AXES}, red_eye_policy_source="default_from_form")}
        before = copy.deepcopy(sub)
        prefs = main.subscription_preferences(sub)
        self.assertIn("time_window_sources", prefs)
        self.assertEqual(prefs["departure_time_windows"], [["08:30", "11:15"]])
        context = main._return_phase_context(sub, date(2026, 12, 2))
        self.assertIsNotNone(context)
        returned = main._return_phase_preferences(sub, context)
        self.assertEqual(returned["preferences"]["time_window_sources"], prefs["time_window_sources"])
        self.assertEqual(returned["constraints"]["red_eye_policy_source"], "default_from_form")
        self.assertTrue(analyzer.match_time_preference(flight("20:15"), returned["preferences"])[0])
        self.assertFalse(analyzer.match_time_preference(flight("08:45"), returned["preferences"])[0])
        prefs["time_window_sources"]["departure"] = "changed"
        self.assertEqual(sub, before)
        sub["hard_constraints"] = {"departure_time_windows": [["09:00", "10:00"]]}
        self.assertEqual(main.subscription_preferences(sub)["departure_time_windows"], [["08:30", "11:15"]])
        returned = main._return_phase_preferences(sub, context)
        self.assertEqual(returned["preferences"]["departure_time_windows"], [["08:30", "11:15"]])
        self.assertEqual(returned["constraints"]["departure_time_windows"], [["09:00", "10:00"]])

    def test_g17_early_departure_exempts_only_that_axis(self):
        prefs = custom([["05:30", "08:30"]], red_eye="reject", red_eye_policy_source="default_from_form")
        self.assertEqual(len(self.filtered(flight("05:45", "08:10"), prefs)[0]), 1)
        self.assertEqual(self.filtered(flight("05:45", "01:20 +1"), prefs)[0], [])

    def test_g18_late_arrival_is_axis_and_direction_specific(self):
        prefs = custom(arrival=[["22:30", "00:30"]], red_eye="reject", red_eye_policy_source="default_from_form")
        self.assertEqual(len(self.filtered(flight("21:00", "23:30"), prefs)[0]), 1)
        self.assertEqual(self.filtered(flight("02:00", "23:30"), prefs)[0], [])
        prefs.update(time_window_sources={"arrival": "derived_no_redeye", "return_arrival": "explicit"},
                     return_arrival_time_windows=[["22:30", "00:30"]])
        self.assertFalse(analyzer.match_time_preference(flight("21:00", "23:30"), prefs)[0])
        prefs["direction"] = "return"
        self.assertEqual(len(self.filtered(flight("21:00", "23:30"), prefs)[0]), 1)

    def test_g19_independent_hard_constraints_and_family_safety_win(self):
        for overrides in ({"red_eye": "reject"}, {"no_redeye_strict": True}, {"companion_constraints": ["no_redeye"]}, {"red_eye_policy": "not_allowed"}):
            prefs = custom([["05:30", "08:30"]], **overrides)
            kept, excluded, _ = self.filtered(flight("05:45", "08:10"), prefs)
            self.assertEqual(kept, [])
            self.assertEqual(len(excluded), 1)
            self.assertIn("独立禁红眼硬约束未放开", excluded[0]["exclude_reason"])
        prefs = custom([["05:30", "08:30"]], red_eye_policy_source="default_from_form",
                       passengers={"adult": 1, "child": 1, "elderly": 0, "infant": 0})
        kept, excluded, _ = self.filtered(flight("05:45", "08:10"), prefs)
        self.assertEqual(kept, [])
        self.assertIn("老人/儿童", excluded[0]["exclude_reason"])
        prefs = custom([["12:00", "13:00"]], same_day_round_trip=True, business_start="10:30", business_end="17:00")
        self.assertEqual(len(self.filtered(flight("05:45", "08:10"), prefs)[0]), 1)

    def test_g20_reasons_share_time_preference_but_keep_clock_evidence(self):
        for reason, clock_missing in (("不在你设置的可接受时段内", False), ("无法核实到达时刻是否满足可接受时段", True)):
            code, detail = analyzer._filter_constraint_detail({**flight(), "exclude_reason": reason}, {})
            self.assertEqual(code, "time_preference")
            self.assertEqual("clock_unverified" in detail, clock_missing)
            lines = notifier._exact_filter_reason_lines({"filter_reason_code": code, "filter_reason_value": detail})
            self.assertIn("起降时间不符合设置", "".join(lines))

    def test_g21_minute_edits_change_fingerprint_and_visible_summary(self):
        values = {**FORM, "shared_departure_window_start": "08:45", "shared_departure_window_end": "11:15"}
        self.assertIn("08:45", form_structure._preference_summary(multidict(values)))
        first = web_form.build_subscription(multidict({**values, "shared_departure_window_start": "08:30"}))
        second = web_form.build_subscription(multidict(values))
        self.assertNotEqual(constraint_fingerprint.constraint_fingerprint(first), constraint_fingerprint.constraint_fingerprint(second))
        arrived = web_form.build_subscription(multidict({**FORM, "round_trip": "false", "shared_arrival_window_start": "10:30", "shared_arrival_window_end": "14:15"}))
        self.assertEqual(web_form._success_time_window_text(arrived["hard_constraints"]), "时间窗：到达10:30-14:15")
        self.assertIn("到达10:30-14:15", form_structure._preference_summary(multidict({**FORM, "shared_arrival_window_start": "10:30", "shared_arrival_window_end": "14:15"})))
        self.assertNotEqual(constraint_fingerprint.constraint_fingerprint(arrived), constraint_fingerprint.constraint_fingerprint(web_form.build_subscription(multidict(FORM))))

    def test_g22_post_loader_analysis_and_empty_result_preserve_evidence(self):
        saved = self.submitted(shared_departure_window_start="06:00", shared_departure_window_end="12:00",
                               shared_arrival_window_start="09:00", shared_arrival_window_end="15:00",
                               outbound_departure_window_start="06:30", outbound_departure_window_end="08:30",
                               outbound_arrival_window_start="09:00", outbound_arrival_window_end="11:00",
                               return_departure_window_start="18:00", return_departure_window_end="21:00",
                               return_arrival_window_start="20:00", return_arrival_window_end="23:00")
        sub = self.loaded(saved)
        prefs = main.subscription_preferences(sub)
        rows = [flight(value, "10:00", "NH" + str(index)) for index, value in enumerate(("06:45", "08:30", "08:55"))]
        result = self.analyzed(rows, prefs)
        self.assertEqual({row["flight_combo"] for row in result["roundtrip_candidate_flights"]}, {"NH0", "NH1"})
        prefs["direction"] = "return"
        self.assertEqual(len(self.analyzed([flight("19:00", "23:00")], prefs)["roundtrip_candidate_flights"]), 1)
        prefs["direction"] = "outbound"
        empty = self.analyzed([flight("08:55", "10:00")], prefs)
        self.assertEqual(empty["roundtrip_candidate_flights"], [])
        self.assertEqual(len(empty["excluded_flights"]), 1)
        self.assertIn("low_price_alert_state", empty)


class TestTrack4FrozenAndAstGuards(unittest.TestCase):
    def test_g23_source_files_ast_and_ten_non_custom_scenarios_are_locked(self):
        self.assertEqual(len(constraint_fingerprint.EXPECTED_CONSTRAINT_FINGERPRINT_FIELDS), 54)
        self.assertEqual(set(constraint_fingerprint.CONSTRAINT_FINGERPRINT_FIELDS), constraint_fingerprint.EXPECTED_CONSTRAINT_FINGERPRINT_FIELDS)
        for name, size, digest in (("constraint_fingerprint.py", 11459, "b396aef5431cc976754bab2a5a2474641b78d38002844a808521b2f228c415a9"),
                                   ("constraint_summary.py", 3595, "ab7a1505ff953891c7959e7e2412bc3ce7f93f93732e48c7557f5d43ebb3752b")):
            raw = (ROOT / name).read_text(encoding="utf-8").encode("utf-8")
            self.assertEqual((len(raw), hashlib.sha256(raw).hexdigest()), (size, digest))
        source = (ROOT / "analyzer.py").read_text(encoding="utf-8")
        nodes = {node.name: node for node in ast.parse(source).body if isinstance(node, ast.FunctionDef)}
        for name, digest in (("parse_flight_time", "9a55d7e7d427adf2469b269546262ebc3d3264ec7f7cae4b7df537f1a4177297"),):
            self.assertEqual(hashlib.sha256(inspect.getsource(getattr(analyzer, name)).encode("utf-8")).hexdigest(), digest)
        import test_legacy_notification_renderer_retirement as locked
        source = (ROOT / "notifier.py").read_text(encoding="utf-8")
        nodes = {node.name: node for node in ast.parse(source).body if isinstance(node, ast.FunctionDef)}
        for name, digest in locked.EXPECTED_MAIN_CHAIN_SHA256.items():
            self.assertEqual(hashlib.sha256(ast.get_source_segment(source, nodes[name]).encode()).hexdigest(), digest)
        fixture = json.loads((ROOT / "tests/fixtures/form_normalization_baseline_v1.json").read_text(encoding="utf-8"))
        other = {key: value for key, value in fixture["scenarios"].items() if key != "directional_time_windows"}
        self.assertEqual(len(other), 10)
        self.assertEqual(hashlib.sha256(json.dumps(other, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode()).hexdigest(), "19ff8f028e544955178a0946a5ed2449588a7cad1910e8fc8bf3d7088b7334a0")

    def test_g24_three_frozen_carriers_and_normal_roundtrip_stay_exact(self):
        payload = json.loads((ROOT / "tests/fixtures/frozen_email/economy_payload.json").read_text(encoding="utf-8"))
        with tempfile.TemporaryDirectory() as tmp, patch.object(config_loader, "RUNTIME_CONFIG_PATH", Path(tmp) / "absent.yaml"), redirect_stdout(io.StringIO()):
            _, email = notifier.render_email(copy.deepcopy(payload))
            push = notifier.prepare_push_render(notifier.render_pushplus_sections(copy.deepcopy(payload))).content
            detail = notifier.render_detail_html(copy.deepcopy(payload))
        for content, size, digest in ((email, 53307, "1dd353d2b7bcafc02087957ccccb6661478e20472a22fb3e15dae6a56473ee6b"),
                                      (push, 5552, "2ac0e80d9f82dbe7ebccf524c0832f09d42d916212b6178316b0b7bcd60574ff"),
                                      (detail, 54248, "e31b8728b8d0bc05c9bbd4bd99c299aaf6dca7b66674b6c560ec089b1681ffa6")):
            raw = content.encode()
            self.assertEqual((len(raw), hashlib.sha256(raw).hexdigest()), (size, digest))
        from tests import test_return_phase_processing as scenario
        cls = scenario.ReturnPhaseProcessingTest
        cls.setUpClass()
        case = cls("test_normal_roundtrip_baseline")
        try:
            case.setUp()
            try:
                case.test_normal_roundtrip_baseline()
            finally:
                case.tearDown()
                case.doCleanups()
        finally:
            cls.tearDownClass()


if __name__ == "__main__":
    unittest.main()
