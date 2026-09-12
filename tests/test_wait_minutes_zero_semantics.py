"""Characterization, not approval of the current zero/unknown-time policy."""

import ast
import copy
from contextlib import ExitStack, redirect_stdout
import importlib
import inspect
import io
import json
from pathlib import Path
import sys
import textwrap
import unittest
from unittest.mock import patch

from tests import test_layover_timezone as legacy
from tests import test_layover_parsing_unification as source_fixture


# Values are passed unchanged; expected results were independently reviewed after a baseline probe.
INPUTS = {
    "direct": {"stops": 0, "layovers": []},
    "transfer_missing": {"stops": 1},
    "transfer_empty": {"stops": 1, "layovers": []},
    "missing_key": {"stops": 1, "layovers": [{}]},
    "none": {"stops": 1, "layovers": [{"wait_minutes": None}]},
    "zero": {"stops": 1, "layovers": [{"wait_minutes": 0}]},
    "positive": {"stops": 1, "layovers": [{"wait_minutes": 120}]},
    "negative": {"stops": 1, "layovers": [{"wait_minutes": -5}]},
    "string_zero": {"stops": 1, "layovers": [{"wait_minutes": "0"}]},
    "string_numeric": {"stops": 1, "layovers": [{"wait_minutes": "120"}]},
    "invalid_string": {"stops": 1, "layovers": [{"wait_minutes": "bad"}]},
    "float_zero": {"stops": 1, "layovers": [{"wait_minutes": 0.0}]},
    "float_half": {"stops": 1, "layovers": [{"wait_minutes": 0.5}]},
    "zero_120": {"stops": 1, "layovers": [{"wait_minutes": 0}, {"wait_minutes": 120}]},
    "missing_120": {"stops": 1, "layovers": [{}, {"wait_minutes": 120}]},
    "only_120": {"stops": 1, "layovers": [{"wait_minutes": 120}]},
}


def _flight(name, identity="candidate"):
    flight = legacy._flight("2026-03-01 01:30", "2026-03-01 03:30")
    flight.update(total_duration_min=240, total_hours=4, route_summary=identity,
                  flight_combo=identity, cabin_class="economy", date="2026-03-01")
    flight.update(copy.deepcopy(INPUTS[name]))
    return flight


class WaitMinutesZeroSemanticsTest(unittest.TestCase):
    def setUp(self):
        self.offline = legacy.LayoverTimezoneTest()
        self.addCleanup(self.offline.doCleanups)
        self.offline.setUp()
        self.addCleanup(self.offline._assert_no_io)
        self.a = importlib.import_module("analyzer")
        self.collector = self.offline.collector
        self.duffel = importlib.import_module("sources.duffel_source")

    def test_comfort_score_values_and_text(self):
        scores = {"direct": 10, "transfer_missing": 9.0, "transfer_empty": 9.0,
                  "missing_key": 7.5, "none": 7.5, "zero": 7.5, "positive": 9.0,
                  "negative": 7.5, "string_zero": 7.5, "string_numeric": 9.0,
                  "float_zero": 7.5, "float_half": 7.5, "zero_120": 7.5,
                  "missing_120": 7.5, "only_120": 9.0}
        for name, score in scores.items():
            with self.subTest(case=name):
                actual = self.a.comfort_score(_flight(name))
                self.assertEqual(actual["score"], score)
                self.assertIs(type(actual["score"]), type(score))
                if score == 7.5:
                    number = "-5" if name == "negative" else "0"
                    self.assertEqual(actual["penalties"], [f"在中转地转机时间仅{number}分钟，较紧张"])
                else:
                    self.assertEqual(actual["penalties"], [])

    def _assert_overall_zero(self):
        try:
            actual = self.a.overall_score(_flight("zero"), [800], [240])
        except TypeError as error:
            self.fail("W04_unexpected_TypeError_at_wait_comparison: " + str(error))
        self.assertEqual(actual, {"total": 7.4, "price_score": 7, "duration_score": 7,
                                  "stops_score": 8, "layover_score": 8}, "W04_zero_score")

    def test_overall_score_preserves_current_penalties(self):
        self._assert_overall_zero()
        for name, (total, layover, stops) in {
            "direct": (8.2, 10, 10), "transfer_missing": (7.8, 10, 8),
            "transfer_empty": (7.8, 10, 8), "missing_key": (7.4, 8, 8),
            "none": (7.4, 8, 8), "positive": (7.8, 10, 8), "negative": (7.4, 8, 8),
            "float_zero": (7.4, 8, 8), "float_half": (7.4, 8, 8),
            "zero_120": (7.4, 8, 8), "missing_120": (7.4, 8, 8), "only_120": (7.8, 10, 8),
        }.items():
            with self.subTest(case=name):
                self.assertEqual(self.a.overall_score(_flight(name), [800], [240]),
                                 {"total": total, "price_score": 7, "duration_score": 7,
                                  "stops_score": stops, "layover_score": layover})

    def test_transfer_risk_real_signature_and_result(self):
        for name in INPUTS.keys() - {"string_zero", "string_numeric", "invalid_string"}:
            with self.subTest(case=name):
                actual = self.a.calc_transfer_risk(_flight(name))
                self.assertIs(type(actual), dict)
                if name == "direct":
                    expected = {"level": "none", "label": "直飞", "score": 0, "factors": []}
                elif name in {"transfer_missing", "transfer_empty", "missing_key", "none", "missing_120"}:
                    expected = {"level": "medium", "label": "中风险", "score": 40,
                                "factors": ["中转等待时间资料不完整，无法核实衔接时间，请核对航段详情。"]}
                elif name in {"positive", "only_120"}:
                    expected = {"level": "low", "label": "低风险", "score": 0, "factors": []}
                else:
                    number = {"negative": "-5", "float_half": "0.5"}.get(name, "0")
                    expected = {"level": "medium", "label": "中风险", "score": 40,
                                "factors": [f"中转时间仅{number}分钟，可能赶不上"]}
                self.assertEqual(actual, expected)
                self.assertEqual(self.a.transfer_risk(_flight(name)), expected)

    def test_comparison_raw_waits_on_both_operands(self):
        for name in INPUTS.keys() - {"none", "string_zero", "string_numeric", "invalid_string"}:
            for side in ("a", "b"):
                with self.subTest(case=name, side=side):
                    candidate, control = _flight(name), _flight("zero", "control")
                    args = (candidate, control) if side == "a" else (control, candidate)
                    self.assertEqual(self.a.compare_flights(*copy.deepcopy(args)), {
                        "a_pros": ["少转1次机"] if name == "direct" and side == "a" else [],
                        "a_cons": ["多转1次机"] if name == "direct" and side == "b" else [],
                        "price_diff": 0, "time_diff_min": 0})

    def _assert_minimum(self, name, expected):
        value = self.a._min_layover_minutes(_flight(name))
        self.assertEqual(value, expected, "strict_positive_minimum")
        self.assertIs(type(value), type(expected))

    def test_minimum_and_maximum_list_semantics(self):
        expected = {"direct": (0, None), "transfer_missing": (0, None), "transfer_empty": (0, None),
                    "missing_key": (0, None), "none": (0, None), "zero": (0, None),
                    "positive": (120, 120), "negative": (-5, None), "string_zero": (0, None),
                    "string_numeric": (120, 120), "float_zero": (0, None), "float_half": (0, None),
                    "zero_120": (120, 120), "missing_120": (120, 120), "only_120": (120, 120)}
        for name, (maximum, minimum) in expected.items():
            with self.subTest(case=name):
                actual = self.a._max_layover_minutes(_flight(name))
                self.assertEqual(actual, maximum)
                self.assertIs(type(actual), int)
                self._assert_minimum(name, minimum)

    def _nested_analysis(self, name, force_fallback=False):
        observed = []
        candidate = _flight(name)
        flights = [candidate]
        if force_fallback:
            candidate["total_duration_min"] = 480
            control = _flight("transfer_empty", "control")
            control["price"] = 900
            flights.append(control)
        def trace(frame, event, value):
            if frame.f_code.co_name == "comfortable_layovers" and event == "return":
                observed.append(value)
            return trace
        previous = sys.gettrace()
        try:
            with ExitStack() as stack, redirect_stdout(io.StringIO()):
                # These earlier independent consumers must not prevent reaching W09/W10.
                stack.enter_context(patch.object(self.a, "_apply_user_preferences", side_effect=lambda fs, _: (fs, [], {})))
                stack.enter_context(patch.object(self.a, "overall_score", return_value={"total": 7}))
                stack.enter_context(patch.object(self.a, "transfer_risk", return_value={}))
                stack.enter_context(patch.object(self.a, "enrich_travel_risk_and_cost"))
                stack.enter_context(patch.object(self.a, "calc_execution_grade"))
                stack.enter_context(patch.object(self.a, "calc_final_score"))
                stack.enter_context(patch.object(self.a, "select_recommendations", return_value=([], None)))
                sys.settrace(trace)
                result = self.a.analyze_all_flights(copy.deepcopy(flights))
        finally:
            sys.settrace(previous)
            self.last_comfortable_predicates = observed
        return result["recommendations"][2]["flight"]["route_summary"]

    def test_nested_comfort_predicate_and_fallback_are_real(self):
        for name in INPUTS.keys() - {"none", "string_zero", "invalid_string"}:
            with self.subTest(case=name):
                self.assertEqual(self._nested_analysis(name), "candidate")
                self.assertEqual(self.last_comfortable_predicates,
                                 [name in {"positive", "string_numeric", "only_120"}])
                self.assertIs(type(self.last_comfortable_predicates[0]), bool)
        for name in ("zero_120", "missing_120", "only_120"):
            with self.subTest(case=name, branch="forced_fallback"):
                self.assertEqual(self._nested_analysis(name, True), "control")

    def test_select_recommendations_real_comfort_sort(self):
        for name in INPUTS.keys() - {"invalid_string"}:
            with self.subTest(case=name):
                candidate, control = _flight(name), _flight("zero", "control")
                control["layovers"] = [{"wait_minutes": 60}]
                result, business = self.a.select_recommendations(copy.deepcopy([candidate, control]), [], "comfort")
                expected = ["control", "candidate"] if name in {
                    "positive", "string_numeric", "zero_120", "missing_120", "only_120"} else ["candidate", "control"]
                self.assertEqual([item["route_summary"] for item in result], expected)
                self.assertIsNone(business)

    def test_exception_types_and_actual_stages(self):
        cases = []
        for name in ("string_zero", "string_numeric", "invalid_string"):
            cases.extend([(name, TypeError, self.a.overall_score, (_flight(name), [800], [240]), "overall_score"),
                          (name, TypeError, self.a.calc_transfer_risk, (_flight(name),), "calc_transfer_risk")])
        for name in ("none", "string_zero", "string_numeric", "invalid_string"):
            cases.extend([(name + "_a", TypeError, self.a.compare_flights, (_flight(name), _flight("zero")), "compare_flights"),
                          (name + "_b", TypeError, self.a.compare_flights, (_flight("zero"), _flight(name)), "compare_flights")])
        for function in (self.a.comfort_score, self.a._max_layover_minutes, self.a._min_layover_minutes):
            cases.append(("invalid_string", ValueError, function, (_flight("invalid_string"),), function.__name__))
        cases.append(("invalid_string", ValueError, self.a.select_recommendations,
                      ([_flight("invalid_string")], [], "comfort"), "select_recommendations"))
        for name, error_type, function, args, scope in cases:
            with self.subTest(case=name, consumer=scope):
                events = []
                def trace(frame, event, value):
                    if event == "exception" and frame.f_code.co_filename == self.a.__file__:
                        events.append((frame.f_code.co_qualname, value[0]))
                    return trace
                previous = sys.gettrace()
                try:
                    sys.settrace(trace)
                    with self.assertRaises(error_type):
                        function(*copy.deepcopy(args))
                finally:
                    sys.settrace(previous)
                self.assertTrue(any(s == scope or s.startswith(scope + ".<locals>.") for s, e in events if e is error_type),
                                "exception_stage_" + scope)
        for name, error_type, stage in (("none", TypeError, "fallback"), ("string_zero", TypeError, "fallback"),
                                        ("string_numeric", TypeError, "forced_fallback"), ("invalid_string", ValueError, "predicate")):
            with self.subTest(case=name, stage=stage):
                with self.assertRaises(error_type):
                    self._nested_analysis(name, stage == "forced_fallback")
                if stage != "predicate":
                    self.assertIs(self.last_comfortable_predicates[0], stage == "forced_fallback")

    def test_real_filters_merge_unknown_and_zero_but_not_short_positive(self):
        profile = {"transfer_policy": "reasonable", "travel_scenario": "business", "red_eye": "accept",
                   "allow_self_transfer": True, "allow_overnight_transfer": True}
        for name in ("transfer_missing", "missing_key", "none", "zero", "negative", "float_half", "zero_120", "only_120"):
            with self.subTest(case=name):
                with redirect_stdout(io.StringIO()):
                    kept, _, _ = self.a._apply_user_preferences([copy.deepcopy(_flight(name))], copy.deepcopy(profile))
                self.assertEqual(len(kept), 1)
        short = _flight("zero")
        short["layovers"] = [{"wait_minutes": 30}]
        with redirect_stdout(io.StringIO()):
            kept, excluded, _ = self.a._apply_user_preferences([copy.deepcopy(short)], copy.deepcopy(profile))
        self.assertEqual(kept, [])
        self.assertEqual(excluded[0]["exclude_reason"], "出行风险画像：中转时间低于90分钟")

    def _assert_marker_consumption(self, arrival, departure):
        parser = self.duffel.DuffelSource.__new__(self.duffel.DuffelSource)
        raw = source_fixture._duffel_offer(arrival, departure)
        source = parser._parse_offer(copy.deepcopy(raw))
        self.assertEqual(source["layovers"][0]["wait_minutes"], 0, "producer_zero")
        self.assertIs(source["layovers"][0]["_wait_computed"], True, "producer_marker_existed")
        normalizer = self.collector._normalize_detail_flight
        with patch.object(self.collector, "calc_layover_minutes", wraps=self.collector.calc_layover_minutes) as spy:
            with patch.dict(normalizer.__globals__, {"calc_layover_minutes": spy}):
                result = normalizer(copy.deepcopy(source))
        self.assertEqual(result["layovers"][0]["wait_minutes"], 0, "zero_unchanged")
        self.assertEqual(spy.call_count, 0, "no_recomputation")
        self.assertNotIn("_wait_computed", result["layovers"][0], "marker_leaked")
        return result

    def test_marker_is_not_proof_of_zero_signed_elapsed_time(self):
        for name, arrival, departure in (
            ("same_instant", "2026-03-01T01:30:00Z", "2026-03-01T01:30:00Z"),
            ("positive_twenty_seconds", "2026-03-01T01:30:00+08:00:10", "2026-03-01T01:31:00+08:00:50"),
            ("negative_2700_seconds", "2026-03-01T01:30:00+00:00", "2026-03-01T01:45:00+01:00"),
            ("minute_quantization", "2026-03-01T01:30:10", "2026-03-01T01:30:50"),
        ):
            with self.subTest(case=name):
                self._assert_marker_consumption(arrival, departure)

    def test_unmarked_zero_can_still_have_been_computed(self):
        parser = self.duffel.DuffelSource.__new__(self.duffel.DuffelSource)
        for name, arrival, departure in (("invalid", "bad", "2026-03-01 03:00"),
                                         ("mixed", "2026-03-01T01:30:00Z", "2026-03-01 03:00"),
                                         ("missing", None, "2026-03-01 03:00")):
            with self.subTest(case=name):
                source = parser._parse_offer(copy.deepcopy(source_fixture._duffel_offer(arrival, departure)))
                self.assertEqual(source["layovers"][0]["wait_minutes"], 0)
                self.assertNotIn("_wait_computed", source["layovers"][0])
                with patch.object(self.collector, "calc_layover_minutes", wraps=self.collector.calc_layover_minutes) as spy:
                    result = self.collector._normalize_detail_flight(copy.deepcopy(source))
                self.assertEqual(spy.call_count, 1)
                self.assertEqual(result["layovers"][0]["wait_minutes"], 0)
        serpapi = importlib.import_module("sources.serpapi_source")
        raw = source_fixture._serpapi_input("2026-03-01T01:30:00Z", "2026-03-01T01:30:00Z")
        result = serpapi.parse_google_flights(copy.deepcopy(raw), "synthetic", date_str="2026-03-01")[0]
        self.assertEqual(result["layovers"][0]["wait_minutes"], 0)
        self.assertNotIn("_wait_computed", result["layovers"][0])
        result = self.collector._normalize_detail_flight(legacy._flight("2026-03-01 01:30", "2026-03-01 01:30"))
        self.assertEqual(result["layovers"][0]["wait_minutes"], 0)
        self.assertNotIn("_wait_computed", result["layovers"][0])

    def test_frozen_fixture_does_not_contain_control_marker(self):
        payload = json.loads((Path(__file__).parent / "fixtures" / "frozen_email" / "economy_payload.json").read_text(encoding="utf-8"))
        self.assertNotIn("_wait_computed", json.dumps(payload))

    def test_marker_cleanup_is_limited_to_aligned_computed_zero(self):
        # Deliberately non-producer shapes: do not claim arbitrary markers are sanitized.
        positive = _flight("positive")
        positive["layovers"][0]["_wait_computed"] = True
        result = self.collector._normalize_detail_flight(copy.deepcopy(positive))
        self.assertIs(result["layovers"][0]["_wait_computed"], True)
        extra = _flight("positive")
        extra["layovers"].append({"wait_minutes": 0, "_wait_computed": True})
        result = self.collector._normalize_detail_flight(copy.deepcopy(extra))
        self.assertIs(result["layovers"][1]["_wait_computed"], True)

    def _mutant(self, function, kind):
        tree = ast.parse(textwrap.dedent(inspect.getsource(function)))
        before = ast.dump(tree)
        if kind == "W04_or_none":
            matches = [n for n in ast.walk(tree) if isinstance(n, ast.Assign)
                       and any(isinstance(t, ast.Name) and t.id == "wait" for t in n.targets)
                       and isinstance(n.value, ast.BoolOp) and isinstance(n.value.op, ast.Or)]
            self.assertEqual(len(matches), 1, "W04_wait_assignment_node")
            self.assertEqual(ast.dump(matches[0].value.values[-1]), ast.dump(ast.Constant(0)))
            matches[0].value.values[-1] = ast.Constant(None)
        elif kind == "collector_pop_to_get":
            matches = [n for n in ast.walk(tree) if isinstance(n, ast.Call) and isinstance(n.func, ast.Attribute)
                       and n.func.attr == "pop" and n.args and isinstance(n.args[0], ast.Constant)
                       and n.args[0].value == "_wait_computed"]
            self.assertEqual(len(matches), 1, "collector_marker_pop_node")
            matches[0].func.attr = "get"
        elif kind == "minimum_includes_zero":
            matches = [n for n in ast.walk(tree) if isinstance(n, ast.Compare)
                       and len(n.ops) == 1 and isinstance(n.ops[0], ast.Gt)
                       and isinstance(n.comparators[0], ast.Constant) and n.comparators[0].value == 0]
            self.assertEqual(len(matches), 1, "minimum_strict_positive_node")
            matches[0].ops[0] = ast.GtE()
        else:
            self.fail("unknown mutation")
        self.assertNotEqual(ast.dump(tree), before, "source_changed")
        code = compile(ast.fix_missing_locations(tree), "<wait-zero-mutation>", "exec")
        namespace = dict(function.__globals__)
        exec(code, namespace)
        value = namespace[function.__name__]
        value.mutation_evidence = {"kind": kind, "matched_nodes": len(matches), "source_changed": True, "compiled": True}
        return value

    def test_mutation_W04_or_none_is_rejected(self):
        mutant = self._mutant(self.a.overall_score, "W04_or_none")
        with patch.object(self.a, "overall_score", mutant):
            with self.assertRaisesRegex(AssertionError, "W04_unexpected_TypeError_at_wait_comparison"):
                self._assert_overall_zero()

    def test_mutation_marker_get_leaks_without_recomputation(self):
        mutant = self._mutant(self.collector._normalize_detail_flight, "collector_pop_to_get")
        with patch.object(self.collector, "_normalize_detail_flight", mutant):
            with self.assertRaisesRegex(AssertionError, "marker_leaked"):
                self._assert_marker_consumption("2026-03-01T01:30:00Z", "2026-03-01T01:30:00Z")

    def test_mutation_minimum_including_zero_is_rejected(self):
        mutant = self._mutant(self.a._min_layover_minutes, "minimum_includes_zero")
        with patch.object(self.a, "_min_layover_minutes", mutant):
            with self.assertRaisesRegex(AssertionError, "strict_positive_minimum"):
                self._assert_minimum("zero_120", 120)
            self._assert_minimum("only_120", 120)


if __name__ == "__main__":
    unittest.main()
