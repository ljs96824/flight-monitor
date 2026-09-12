"""Missing wait data is one conservative surcharge, not an asserted zero wait."""

import ast
import copy
import importlib
import inspect
import textwrap
import unittest

from tests import test_layover_timezone as offline_fixture


MISSING_MESSAGE = "中转等待时间资料不完整，无法核实衔接时间，请核对航段详情。"
ZERO_MESSAGE = "中转时间仅0分钟，可能赶不上"
POLICY_CASES = (
    ("direct", {"stops": 0, "layovers": []}, "none", "直飞", 0, []),
    ("no_layovers", {"stops": 1}, "medium", "中风险", 40, [MISSING_MESSAGE]),
    ("empty", {"stops": 1, "layovers": []}, "medium", "中风险", 40, [MISSING_MESSAGE]),
    ("missing_key", {"stops": 1, "layovers": [{}]}, "medium", "中风险", 40, [MISSING_MESSAGE]),
    ("none", {"stops": 1, "layovers": [{"wait_minutes": None}]}, "medium", "中风险", 40, [MISSING_MESSAGE]),
    ("explicit_zero", {"stops": 1, "layovers": [{"wait_minutes": 0}]}, "medium", "中风险", 40, [ZERO_MESSAGE]),
    ("two_missing", {"stops": 2, "layovers": [{}, {}]}, "high", "高风险", 70, ["多次中转", MISSING_MESSAGE]),
    ("short_missing", {"stops": 2, "layovers": [{"wait_minutes": 60}, {}]}, "high", "高风险", 110,
     ["多次中转", "中转时间仅60分钟，可能赶不上", MISSING_MESSAGE]),
    ("missing_zero", {"stops": 2, "layovers": [{}, {"wait_minutes": 0}]}, "high", "高风险", 110,
     ["多次中转", ZERO_MESSAGE, MISSING_MESSAGE]),
    ("zero_missing", {"stops": 2, "layovers": [{"wait_minutes": 0}, {}]}, "high", "高风险", 110,
     ["多次中转", ZERO_MESSAGE, MISSING_MESSAGE]),
    ("partial_count_known", {"stops": 2, "layovers": [{"wait_minutes": 120}]}, "medium", "中风险", 30, ["多次中转"]),
)


class MissingLayoverRiskTest(unittest.TestCase):
    def setUp(self):
        self.offline = offline_fixture.LayoverTimezoneTest()
        self.addCleanup(self.offline.doCleanups)
        self.offline.setUp()
        self.addCleanup(self.offline._assert_no_io)
        self.a = importlib.import_module("analyzer")

    def _assert_case(self, function, case):
        name, flight, level, label, score, factors = case
        actual = function(copy.deepcopy(flight))
        self.assertEqual(actual["score"], score, name + ":score")
        self.assertEqual(actual, {"level": level, "label": label, "score": score, "factors": factors},
                         name + ":result_and_factors")
        self.assertIs(type(actual["score"]), int, name)

    def test_policy_matrix(self):
        for case in POLICY_CASES:
            for function in (self.a.calc_transfer_risk, self.a.transfer_risk):
                with self.subTest(case=case[0], consumer=function.__name__):
                    self._assert_case(function, case)

    def _assert_combined(self, function):
        flight = {"stops": 2, "segments": [{"airline": "MU"}, {"airline": "JL"}],
                  "layovers": [{"airport": "HKG"}, {"airport": "HKG", "wait_minutes": None}]}
        actual = function(flight)
        self.assertEqual(actual, {"level": "high", "label": "高风险", "score": 125,
                                 "factors": ["多次中转", MISSING_MESSAGE, "跨航司（JL/MU），可能非联程",
                                             "经香港中转，请确认是否需要过境签", "经香港中转，请确认是否需要过境签"]},
                         "combined_wait_airline_airport")

    def test_missing_keeps_airlines_airports_and_duplicate_factors(self):
        self._assert_combined(self.a.calc_transfer_risk)

    def test_input_unchanged_and_repeatable(self):
        for case in POLICY_CASES:
            with self.subTest(case=case[0]):
                flight = copy.deepcopy(case[1])
                flight["segments"] = [{"airline": "MU", "metadata": {"original": True}}]
                before = copy.deepcopy(flight)
                first = self.a.calc_transfer_risk(flight)
                self.assertEqual(flight, before)
                self.assertEqual(self.a.calc_transfer_risk(flight), first)
                self.assertEqual(flight, before)

    def _assert_invalid_record(self, function):
        with self.assertRaisesRegex(AttributeError, "get", msg="invalid_record_must_raise_AttributeError"):
            function({"stops": 1, "layovers": [None]})

    def test_invalid_record_and_direct_boundaries(self):
        for function in (self.a.calc_transfer_risk, self.a.transfer_risk):
            self._assert_invalid_record(function)
            with self.assertRaisesRegex(AttributeError, "get"):
                function(None)
            self.assertEqual(function({}), {"level": "none", "label": "直飞", "score": 0, "factors": []})
            self.assertEqual(function({"stops": 0, "layovers": [None]}),
                             {"level": "none", "label": "直飞", "score": 0, "factors": []})

    def test_valid_falsy_and_numeric_values_keep_legacy_factors(self):
        for value, text in ((0, "0"), (0.0, "0"), (False, "0"), ("", "0"), (-5, "-5"), (0.5, "0.5")):
            with self.subTest(value=value):
                actual = self.a.calc_transfer_risk({"stops": 1, "layovers": [{"wait_minutes": value}]})
                self.assertEqual(actual, {"level": "medium", "label": "中风险", "score": 40,
                                         "factors": [f"中转时间仅{text}分钟，可能赶不上"]})
        for value in ("0", "120", "bad"):
            with self.subTest(invalid_value=value):
                with self.assertRaises(TypeError):
                    self.a.calc_transfer_risk({"stops": 1, "layovers": [{"wait_minutes": value}]})

    def test_fresh_execution_risk_and_candidate_order(self):
        candidate = offline_fixture._flight("2026-03-01 01:30", "2026-03-01 03:30")
        candidate.update(layovers=[], route_summary="missing", total_duration_min=240,
                         availability={"age_minutes": 0, "source_count": 2}, preference_score=5)
        control = copy.deepcopy(candidate)
        control.update(layovers=[{"wait_minutes": 120}], route_summary="known", price=850)
        for flight in (candidate, control):
            self.assertNotIn("execution_risk", flight)
            flight["transfer_risk"] = self.a.calc_transfer_risk(flight)
            self.a.calc_execution_risk(flight)
        self.assertEqual(candidate["execution_risk"]["score"], 12, "empty_execution_score")
        self.assertEqual(control["execution_risk"]["score"], 0)
        profile = {"score_weights": {"price": 0.2, "time": 0.2, "comfort": 0.2,
                                     "risk": 0.2, "baggage": 0.1, "refund": 0.1}}
        for flight in (candidate, control):
            self.a.calc_final_score(flight, target_price=800, profile=profile)
        self.assertLess(candidate["final_score"], control["final_score"])
        selected, business = self.a.select_recommendations([candidate, control], [])
        self.assertEqual([f["route_summary"] for f in selected], ["known", "missing"])
        self.assertIsNone(business)
        self.assertEqual([f["price"] for f in (candidate, control)], [800, 850])
        candidate["availability"]["age_minutes"] = None
        self.a.calc_execution_risk(candidate)
        self.assertEqual((candidate["execution_risk"]["score"], candidate["execution_risk"]["level"]), (27, "medium"))
        # Raw transfer scores 70 and 110 both remain high: this downstream reader adds 25 for either.
        for transfer_score in (70, 110):
            flight = {"transfer_risk": {"level": "high", "score": transfer_score},
                      "availability": {"age_minutes": 0, "source_count": 2}}
            self.assertEqual(self.a.calc_execution_risk(flight)["score"], 25)

    def _mutated_risk(self, mutation):
        function = self.a.calc_transfer_risk
        tree = ast.parse(textwrap.dedent(inspect.getsource(function)))
        before = ast.unparse(tree)
        predicates = [n for n in ast.walk(tree) if isinstance(n, ast.If)
                      and any(isinstance(child, ast.Assign)
                              and any(isinstance(target, ast.Name) and target.id == "missing_wait"
                                      for target in child.targets) for child in n.body)]
        charges = [n for n in ast.walk(tree) if isinstance(n, ast.If)
                   and isinstance(n.test, ast.Name) and n.test.id == "missing_wait"]
        self.assertEqual(len(predicates), 1, mutation + ":predicate_match")
        self.assertEqual(len(charges), 1, mutation + ":charge_match")
        predicate, charge = predicates[0], charges[0]
        if mutation == "m1_zero_is_missing":
            predicate.test = ast.parse('not layover.get("wait_minutes", 0)', mode="eval").body
        elif mutation == "m2_per_record_charge":
            predicate.body = copy.deepcopy(charge.body) + [ast.Continue()]
        elif mutation == "m3_missing_early_return":
            charge.body += ast.parse(
                'return {"level": "medium", "label": "中风险", "score": risk_score, "factors": risk_factors}'
            ).body
        elif mutation == "m4_falsey_record_rescued":
            # A faulty falsey-record shortcut returns a result instead of preserving AttributeError.
            loops = [n for n in ast.walk(tree) if isinstance(n, ast.For) and predicate in n.body]
            self.assertEqual(len(loops), 1, mutation + ":loop_match")
            rescue = ast.parse('if not layover:\n    return {}').body[0]
            rescue.body[0].value = ast.parse(repr({"level": "medium", "label": "中风险", "score": 40,
                                                  "factors": [MISSING_MESSAGE]}), mode="eval").body
            loops[0].body.insert(0, rescue)
        else:
            self.fail("unknown mutation: " + mutation)
        ast.fix_missing_locations(tree)
        self.assertNotEqual(ast.unparse(tree), before, mutation + ":source_changed")
        compiled = compile(tree, "<missing-wait-" + mutation + ">", "exec")
        namespace = dict(function.__globals__)
        exec(compiled, namespace)
        return namespace[function.__name__]

    def test_mutation_zero_misclassified_is_rejected_by_factors(self):
        mutant = self._mutated_risk("m1_zero_is_missing")
        case = next(c for c in POLICY_CASES if c[0] == "explicit_zero")
        actual = mutant(copy.deepcopy(case[1]))
        self.assertEqual((actual["score"], actual["level"]), (40, "medium"))
        with self.assertRaisesRegex(AssertionError, "explicit_zero:result_and_factors"):
            self._assert_case(mutant, case)

    def test_mutation_repeated_missing_charge_is_rejected(self):
        mutant = self._mutated_risk("m2_per_record_charge")
        case = next(c for c in POLICY_CASES if c[0] == "two_missing")
        self.assertEqual(mutant(copy.deepcopy(case[1]))["score"], 110)
        with self.assertRaisesRegex(AssertionError, "two_missing:score"):
            self._assert_case(mutant, case)

    def test_mutation_early_return_cannot_skip_other_risks(self):
        mutant = self._mutated_risk("m3_missing_early_return")
        with self.assertRaisesRegex(AssertionError, "combined_wait_airline_airport"):
            self._assert_combined(mutant)

    def test_mutation_falsey_record_rescue_is_rejected(self):
        mutant = self._mutated_risk("m4_falsey_record_rescued")
        self.assertEqual(mutant({"stops": 1, "layovers": [None]}),
                         {"level": "medium", "label": "中风险", "score": 40, "factors": [MISSING_MESSAGE]})
        with self.assertRaisesRegex(AssertionError, "invalid_record_must_raise_AttributeError"):
            self._assert_invalid_record(mutant)


if __name__ == "__main__":
    unittest.main()
