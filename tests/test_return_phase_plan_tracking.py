"""Offline return-phase tracking contracts; candidate evidence is not a recommendation."""

import ast
import copy
import importlib
import io
import json
import tempfile
import types
import unittest
from contextlib import ExitStack, redirect_stdout
from datetime import date, datetime
from pathlib import Path
from unittest.mock import patch


SUB_ID = "00000000-0000-4000-8000-000000000049"
TODAY = date(2026, 10, 2)
EXPECTED_RED_TEST_IDS = (
    "test_c2_all_and_reference_quotes_choose_lowest",
    "test_c3_excluded_quote_keeps_its_reason",
    "test_c4_missing_is_limited_to_candidates",
    "test_c10_renderers_show_candidate_tracking",
)


class ScenarioDatetime(datetime):
    @classmethod
    def now(cls, tz=None):
        value = cls(2026, 10, 2, 16, 0, 0)
        return value if tz is None else value.replace(tzinfo=tz)


def flight(number="CA858", price=1500):
    return {
        "flight_combo": number, "price": price, "currency": "CNY",
        "source": "juhe", "data_source": "juhe", "cabin_class": "economy",
        "stops": 0, "total_duration_min": 150,
        "departure_airport": "KIX", "arrival_airport": "PVG",
        "departure_date": "2026-10-06", "departure_time": "13:00", "arrival_time": "15:30",
        "segments": [{"flight_no": number, "dep_airport": "KIX", "arr_airport": "PVG",
                      "dep_time": "2026-10-06 13:00", "arr_time": "2026-10-06 15:30"}],
        "layovers": [], "collected_at": "2026-10-02T16:00:00",
    }


def encoded(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True).encode("utf-8")


def baseline_builder(module):
    """Undo only the return tracking call; no version-sensitive function pin."""
    source = Path(module.__file__).read_text(encoding="utf-8")
    function, = [node for node in ast.parse(source).body
                 if isinstance(node, ast.FunctionDef) and node.name == "build_notification_payload"]
    tree = ast.Module(body=[function], type_ignores=[])
    matches = 0
    for node in ast.walk(tree):
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Name) and node.func.id == "_track_return_phase_plan":
            node.func.id = "track_plan_status"
            assert ast.unparse(node.args.pop()) == "analysis_result"
            matches += 1
    assert matches <= 1, "BASELINE_REBUILD_AMBIGUOUS"
    scope = dict(vars(module))
    exec(compile(ast.fix_missing_locations(tree), "<baseline-tracking-call>", "exec"), scope)
    compiled = scope["build_notification_payload"]
    return types.FunctionType(compiled.__code__, vars(module), compiled.__name__, compiled.__defaults__)


MUTATION_TARGETS = {
    "m1": (("test_c2_all_and_reference_quotes_choose_lowest", "CANDIDATE_QUOTE_STATUS"),
           ("test_c3_excluded_quote_keeps_its_reason", "EXCLUDED_QUOTE_STATUS"),
           ("test_c4_missing_is_limited_to_candidates", "CANDIDATE_ONLY_MISSING")),
    "m2": (("test_c3_excluded_quote_keeps_its_reason", "EXCLUDED_REASON"),),
    "m3": (("test_c4_missing_is_limited_to_candidates", "CANDIDATE_ONLY_MISSING"),),
    "m4": (("test_c7_recommendations_never_expand", "RECOMMENDATIONS_UNCHANGED"),),
    "m5": (("test_c8_codeshare_is_not_the_previous_identity", "CODESHARE_NOT_EQUAL"),),
    "m6": (("test_c2_all_and_reference_quotes_choose_lowest", "CANDIDATE_QUOTE_STATUS"),),
}


def mutated_tracking(module, kind):
    name = "build_notification_payload" if kind in {"m1", "m4", "m6"} else "_track_return_phase_plan"
    source = Path(module.__file__).read_text(encoding="utf-8")
    function, = [node for node in ast.parse(source).body if isinstance(node, ast.FunctionDef) and node.name == name]
    tree = ast.Module(body=[function], type_ignores=[])
    before = ast.dump(tree, include_attributes=False)
    matches = 0
    if kind in {"m1", "m6"}:
        for node in ast.walk(tree):
            if isinstance(node, ast.Call) and isinstance(node.func, ast.Name) and node.func.id == "_track_return_phase_plan":
                node.func.id = "track_plan_status"
                assert ast.unparse(node.args.pop()) == "analysis_result"
                matches += 1
    elif kind == "m2":
        for node in ast.walk(tree):
            if isinstance(node, ast.IfExp) and isinstance(node.body, ast.Constant) and node.body.value == "未入选本轮推荐":
                node.orelse = ast.Constant(value="未入选本轮推荐")
                matches += 1
    elif kind == "m3":
        for node in ast.walk(tree):
            if isinstance(node, ast.Constant) and isinstance(node.value, str) and "本轮候选中未取得报价" in node.value:
                node.value = "本轮未获取到报价,可能已售罄或停飞"
                matches += 1
    elif kind == "m4":
        for node in ast.walk(tree):
            if not isinstance(node, ast.If) or ast.unparse(node.test) != "route_info.get('processing_phase') == 'return_only'":
                continue
            index, = [i for i, statement in enumerate(node.body) if isinstance(statement, ast.Assign)
                      and any(isinstance(target, ast.Name) and target.id == "tracking" for target in statement.targets)]
            node.body.insert(index + 1, ast.parse(
                'if analysis_result.get("all_flights"):\n'
                '    plans.insert(0, _payload_single_plan(analysis_result["all_flights"][0], route_info, analysis_result, 0, "推荐"))'
            ).body[0])
            matches += 1
    elif kind == "m5":
        for node in ast.walk(tree):
            if isinstance(node, ast.Call) and isinstance(node.func, ast.Name) and node.func.id == "find_flight" and isinstance(node.args[0], ast.List):
                node.args[0] = ast.parse(
                    '[{**flight, "flight_combo": flight["flight_combo"].split("(")[-1].rstrip(")")}]', mode="eval"
                ).body
                matches += 1
    else:
        raise AssertionError("UNKNOWN_MUTATION")
    assert matches == 1, (kind, "MUTATION_NODE_COUNT", matches)
    assert ast.dump(tree, include_attributes=False) != before, "MUTATION_UNCHANGED"
    namespace = dict(vars(module))
    exec(compile(ast.fix_missing_locations(tree), "<return-tracking-mutant>", "exec"), namespace)
    compiled = namespace[name]
    mutant = types.FunctionType(compiled.__code__, vars(module), name, compiled.__defaults__)
    mutant.__kwdefaults__ = compiled.__kwdefaults__
    return name, mutant


class ReturnPhasePlanTrackingTest(unittest.TestCase):
    def setUp(self):
        self.stack = ExitStack()
        self.addCleanup(self.stack.close)
        self.root = Path(self.stack.enter_context(tempfile.TemporaryDirectory()))
        self.notifier = importlib.import_module("notifier")
        self.tracker = importlib.import_module("plan_tracker")
        self.stack.enter_context(redirect_stdout(io.StringIO()))
        self.stack.enter_context(patch.object(self.tracker, "DEFAULT_DATA_DIR", self.root))
        self.stack.enter_context(patch.object(self.tracker, "DEFAULT_FEEDBACK_PATH", self.root / "feedback.json"))
        for name in ("notifier", "plan_tracker", "price_calendar"):
            module = importlib.import_module(name)
            self.stack.enter_context(patch.object(module, "datetime", ScenarioDatetime))
        for name in ("notifier", "price_calendar", "analyzer"):
            self.stack.enter_context(patch.object(importlib.import_module(name), "shanghai_today", return_value=TODAY))
        self.stack.enter_context(patch.object(self.notifier, "get_last_push_price", return_value=None))
        self.snapshot = self.stack.enter_context(patch.object(self.notifier, "get_last_push_snapshot", return_value=None))
        self.stack.enter_context(patch.object(self.notifier, "_quota_overview_text", return_value="synthetic quota"))
        self.baseline = baseline_builder(self.notifier)

    def seed(self, *, directory="return_only", number="JL891"):
        self.tracker.save_pushed_plans(
            SUB_ID, [{"label": "方案A", "price": 2000, "main_flight": flight(number, 2000)}],
            data_dir=self.root / directory if directory else self.root,
        )

    def payload(self, *, pool=None, reference=None, excluded=None, recommended=None,
                degraded=False, builder=None, phase=True, roundtrip=False):
        recommendation = recommended if recommended is not None else [flight(), flight("MU730", 1800), flight("FM810", 1900)]
        analysis = {
            "economy_recommendations": [{"flight": item} for item in recommendation],
            "recommendations": [{"flight": item} for item in recommendation],
            "all_flights": pool or [], "reference_flights": reference or [],
            "excluded_flights": excluded or [], "collected_at": "2026-10-02T16:00:00",
        }
        route = {
            "origin": "KIX", "destination": "PVG", "origin_city": "大阪", "destination_city": "上海",
            "origin_airports_active": ["KIX"], "destination_airports_active": ["PVG"],
            "depart_date": "2026-10-06", "route_type": "international", "subscription_id": SUB_ID,
            "round_trip": roundtrip, "target_price": 2000, "max_budget": 4000,
        }
        if phase:
            route.update(processing_phase="return_only", phase_label="返程(原去程计划出发日 10-01 已过)",
                         original_outbound_date="2026-10-01")
        if roundtrip:
            route["return_date"] = "2026-10-10"
        sub = {"id": SUB_ID, "route_type": "international", "hard_constraints": {}, "constraints": {}}
        stats = {"juhe": {"count": 0 if degraded else len(recommendation)}}
        self.snapshot.return_value = {"source_set": ["juhe"]} if degraded else None
        before = copy.deepcopy((analysis, route, sub, stats))
        result = (builder or self.notifier.build_notification_payload)(
            analysis, route_info=route, subscription=sub, source_stats=stats,
        )
        self.assertEqual((analysis, route, sub, stats), before, "INPUT_EVIDENCE_UNCHANGED")
        return result

    def test_c1_recommended_preserves_baseline(self):
        self.seed()
        for price in (1700, 2000, 2300):
            with self.subTest(price=price):
                args = {"recommended": [flight("JL891", price)], "pool": [flight("JL891", 1000)]}
                actual, old = self.payload(**args), self.payload(**args, builder=self.baseline)
                self.assertEqual(encoded(actual["plan_status_change"]), encoded(old["plan_status_change"]), "RECOMMENDED_BASELINE")

    def test_c2_all_and_reference_quotes_choose_lowest(self):
        self.seed()
        for pool, reference in (([flight("JL891", 1900), flight("JL891", 1700)], []),
                                ([], [flight("JL891", 1700)]),
                                ([flight("JL891", 1900)], [flight("JL891", 1700)])):
            with self.subTest(pool=bool(pool), reference=bool(reference)):
                result = self.payload(pool=pool, reference=reference,
                                      excluded=[{"flight": flight("JL891", 900), "reason": "excluded"}])
                tracking = result["plan_status_change"]
                self.assertEqual(tracking["status"], "quoted_not_recommended", "CANDIDATE_QUOTE_STATUS")
                self.assertEqual(tracking["current_price"], 1700, "CANDIDATE_LOWEST_PRICE")
                self.assertEqual(tracking["msg"], "上次推荐的JL891本轮有报价¥1,700,未入选本轮推荐", "CANDIDATE_QUOTE_MESSAGE")
                for text in ("未获取到报价", "售罄", "停飞"):
                    self.assertNotIn(text, tracking["msg"])

    def test_c3_excluded_quote_keeps_its_reason(self):
        self.seed()
        result = self.payload(excluded=[
            {"flight_combo": "SUMMARY-ONLY", "flight": flight("JL891", 1900), "reason": "较晚"},
            {"flight_combo": "SUMMARY-ONLY", "flight": flight("JL891", 1600), "reason": "到达时间不符合要求"},
        ])
        tracking = result["plan_status_change"]
        self.assertEqual(tracking["status"], "quoted_excluded", "EXCLUDED_QUOTE_STATUS")
        self.assertEqual(tracking["current_price"], 1600, "EXCLUDED_LOWEST_PRICE")
        self.assertEqual(tracking["msg"], "上次推荐的JL891本轮有报价¥1,600,当前不符合约束:到达时间不符合要求", "EXCLUDED_REASON")

    def test_c4_missing_is_limited_to_candidates(self):
        self.seed()
        tracking = self.payload()["plan_status_change"]
        self.assertEqual(tracking["status"], "unavailable")
        self.assertEqual(tracking["msg"], "上次推荐的JL891本轮候选中未取得报价,建议在渠道核实", "CANDIDATE_ONLY_MISSING")
        for text in ("售罄", "停飞"):
            self.assertNotIn(text, tracking["msg"], "NO_AVAILABILITY_INFERENCE")

    def test_c5_degradation_preserves_baseline(self):
        self.seed()
        result = self.payload(degraded=True)
        self.assertTrue(result["source_degradation"]["active"], "DEGRADATION_REACHED")
        tracking = result["plan_status_change"]
        old = self.payload(degraded=True, builder=self.baseline)["plan_status_change"]
        self.assertEqual(encoded(tracking), encoded(old), "DEGRADED_BASELINE")
        self.assertEqual(tracking["status"], "source_unavailable")
        for text in ("售罄", "停飞"):
            self.assertNotIn(text, tracking["msg"])

    def test_c6_first_push_scope_change(self):
        actual, old = self.payload(), self.payload(builder=self.baseline)
        self.assertEqual(encoded(actual["plan_status_change"]), encoded(old["plan_status_change"]))
        self.assertEqual(actual["plan_status_change"]["msg"], "口径切换(往返→返程)")
        for key in ("recommended_plans", "alternative_plans"):
            self.assertEqual(encoded(actual[key]), encoded(old[key]), "FIRST_RECOMMENDATIONS_UNCHANGED")

    def test_c7_recommendations_never_expand(self):
        self.seed()
        for options in ({}, {"pool": [flight("JL891", 900)]}, {"reference": [flight("JL891", 900)]},
                        {"excluded": [{"flight": flight("JL891", 900), "reason": "constraint"}]},
                        {"recommended": [flight("JL891", 1700)]}, {"degraded": True},
                        {"pool": [flight("FM1012(JL891)", 900)]}):
            with self.subTest(options=options):
                actual, old = self.payload(**options), self.payload(**options, builder=self.baseline)
                for key in ("recommended_plans", "alternative_plans"):
                    self.assertEqual(encoded(actual[key]), encoded(old[key]), "RECOMMENDATIONS_UNCHANGED")

    def test_c8_codeshare_is_not_the_previous_identity(self):
        self.seed()
        tracking = self.payload(pool=[flight("FM1012(JL891)", 900)])["plan_status_change"]
        self.assertEqual(tracking["status"], "unavailable", "CODESHARE_NOT_EQUAL")
        self.assertNotIn("current_price", tracking, "CODESHARE_NO_QUOTE")

    def test_c9_ordinary_paths_preserve_tracking(self):
        self.seed(directory="")
        for roundtrip in (False, True):
            with self.subTest(roundtrip=roundtrip):
                options = {"phase": False, "roundtrip": roundtrip, "recommended": [flight("JL891", 1700)]}
                actual, old = self.payload(**options), self.payload(**options, builder=self.baseline)
                self.assertIsNotNone(actual["plan_status_change"], "ORDINARY_TRACKING_REACHED")
                self.assertEqual(encoded(actual["plan_status_change"]), encoded(old["plan_status_change"]), "ORDINARY_BASELINE")

    def test_c10_renderers_show_candidate_tracking(self):
        from pushplus_sections import prepare_push_render

        self.seed()
        for options, expected in (
            ({"pool": [flight("JL891", 1700)]}, "上次推荐的JL891本轮有报价¥1,700,未入选本轮推荐"),
            ({"excluded": [{"flight": flight("JL891", 1600), "reason": "到达时间不符合要求"}]},
             "上次推荐的JL891本轮有报价¥1,600,当前不符合约束:到达时间不符合要求"),
            ({}, "上次推荐的JL891本轮候选中未取得报价,建议在渠道核实"),
        ):
            with self.subTest(expected=expected):
                result = self.payload(**options)
                email = self.notifier.render_email(result)[1]
                push = prepare_push_render(self.notifier.render_pushplus_sections(result)).content
                self.assertIn(expected, email, "EMAIL_TRACKING_TEXT")
                self.assertIn(expected, push, "PUSHPLUS_TRACKING_TEXT")


class ReturnPhasePlanTrackingMutationTest(unittest.TestCase):
    def assert_killed(self, kind):
        notifier = importlib.import_module("notifier")
        name, mutant = mutated_tracking(notifier, kind)
        for method, marker in MUTATION_TARGETS[kind]:
            with self.subTest(mutation=kind, target=method), patch.object(notifier, name, mutant):
                result = unittest.TestResult()
                ReturnPhasePlanTrackingTest(method).run(result)
                self.assertEqual(result.testsRun, 1)
                self.assertEqual(result.errors, [], "NO_UNRELATED_MUTATION_ERRORS")
                self.assertTrue(result.failures, "TARGET_ASSERTION_MUST_KILL")
                for test, trace in result.failures:
                    self.assertIn(method, test.id())
                    self.assertIn(marker, trace, "TARGET_ASSERTION_MUST_KILL")

    def test_m1_recommended_only(self):
        self.assert_killed("m1")

    def test_m2_excluded_is_distinct(self):
        self.assert_killed("m2")

    def test_m3_no_sold_out_inference(self):
        self.assert_killed("m3")

    def test_m4_tracking_cannot_expand_recommendations(self):
        self.assert_killed("m4")

    def test_m5_matching_stays_exact(self):
        self.assert_killed("m5")

    def test_m6_dispatch_and_retirement_contract(self):
        import hashlib
        import test_legacy_notification_renderer_retirement as retirement

        self.assert_killed("m6")
        tree, source = retirement._notifier_tree_and_source()
        function = retirement._module_function(tree, "build_notification_payload")
        call, = [n for n in ast.walk(function) if isinstance(n, ast.Call)
                 and isinstance(n.func, ast.Name) and n.func.id == "_track_return_phase_plan"]
        segment = ast.get_source_segment(source, call)
        call.func.id = "track_plan_status"
        call.args.pop()
        reverted = source.replace(segment, ast.unparse(call), 1)
        reverted_tree = ast.parse(reverted)
        for name, expected in retirement.EXPECTED_MAIN_CHAIN_SHA256.items():
            node = retirement._module_function(reverted_tree, name)
            actual = hashlib.sha256(ast.get_source_segment(reverted, node).encode()).hexdigest()
            if name == "build_notification_payload":
                self.assertNotEqual(actual, expected, "REVERTED_DISPATCH_DIGEST")
            else:
                self.assertEqual(actual, expected, "OTHER_DIGEST_UNCHANGED")
        case = retirement.LegacyNotificationRendererRetirementTest("test_current_notification_main_chain_source_is_unchanged")
        result = unittest.TestResult()
        with patch.object(retirement, "_notifier_tree_and_source", return_value=(reverted_tree, reverted)):
            case.run(result)
        self.assertEqual(result.errors, [])
        self.assertEqual(len(result.failures), 1)
        self.assertIn("self.assertEqual(actual, EXPECTED_MAIN_CHAIN_SHA256)", result.failures[0][1])


if __name__ == "__main__":
    unittest.main()
