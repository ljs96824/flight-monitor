"""Finite calendar inputs; synthetic data only, not supplier incident evidence."""

import ast
import copy
import inspect
import tempfile
import textwrap
import unittest
from datetime import datetime, timedelta
from decimal import Decimal
from pathlib import Path
from unittest.mock import patch

import price_calendar as calendar
import request_cache
from project_time import SHANGHAI_TZ


NOW = datetime(2030, 3, 1, 12, tzinfo=SHANGHAI_TZ)
TARGET = "2030-03-03"
CANDIDATE = "2030-03-04"
NONFINITE_CASES = (
    ("inf", float("inf")), ("negative_inf", float("-inf")),
    ("nan", float("nan")), ("exponent_string", "1e400"),
    ("huge_decimal", Decimal("1e400")), ("huge_integer", 10**400),
)
SCALAR_CASES = tuple((label, value, False) for label, value in NONFINITE_CASES) + (
    ("integer", 500, True), ("numeric_string", "500", True),
    ("fraction", 500.25, True), ("finite_decimal", Decimal("500.25"), True),
    ("true_compatibility", True, True), ("false_compatibility", False, False),
    ("zero", 0, False), ("negative", -1, False), ("none", None, False),
    ("empty", "", False), ("currency_text", "\u00a5500", False),
    ("list", [500], False),
)


class SyntheticSource:
    name = "synthetic_calendar"

    def __init__(self, prices):
        self.prices = prices
        self.calls = []

    def fetch(self, origin, dest, date_str, cabin_class="economy"):
        self.calls.append((origin, dest, date_str, cabin_class))
        return {"flights": [{"price": price, "flight_no": "XX100"}
                            for price in self.prices]}


class CalendarPriceFinitenessTest(unittest.TestCase):
    def setUp(self):
        for name, value in (("_shanghai_now", NOW), ("shanghai_today", NOW.date())):
            mocked = patch.object(calendar, name, return_value=value)
            mocked.start()
            self.addCleanup(mocked.stop)
        self.record = {
            "status": "success", "min_price": 500,
            "last_success_at": NOW.isoformat(),
            "stale_after": (NOW + timedelta(hours=6)).isoformat(),
        }
        self.candidate_calendar = {"route": "PVG-PEK", "dates": {CANDIDATE: self.record}}

    def assert_scalar(self, function, value, expected, label):
        try:
            actual = function(value)
        except (TypeError, ValueError, OverflowError) as exc:
            self.fail(f"scalar_exception:{label}:{type(exc).__name__}")
        self.assertIs(actual, expected, f"scalar_result:{label}")

    def assert_current(self, function, value, expected_saves, label, threshold=100):
        self.assertNotEqual(TARGET, CANDIDATE)
        self.assertGreaterEqual(calendar.parse_date(CANDIDATE), NOW.date())
        self.assertTrue(calendar.calendar_record_is_eligible(self.record))
        try:
            rows = function(self.candidate_calendar, TARGET, value, threshold=threshold)
        except (TypeError, ValueError, OverflowError) as exc:
            self.fail(f"current_exception:{label}:{type(exc).__name__}")
        self.assertEqual([row["save"] for row in rows], expected_saves,
                         f"current_result:{label}")
        if rows:
            self.assertEqual(rows[0]["date"], CANDIDATE)
            self.assertEqual(rows[0]["price"], 500)

    def test_scalar_contract(self):
        for label, value, expected in SCALAR_CASES:
            with self.subTest(case=label):
                self.assert_scalar(calendar._valid_price, value, expected, label)

    def test_current_positive_control(self):
        self.assert_current(calendar.analyze_date_savings, 900, [400], "positive")

    def test_current_rejects_nonfinite_and_overflow(self):
        for label, value in NONFINITE_CASES:
            with self.subTest(case=label):
                self.assert_current(calendar.analyze_date_savings, value, [], label)

    def test_current_zero_and_negative_compatibility(self):
        # Preserve the old numeric entry contract, not a negative-price policy.
        for value, expected in ((0, -500), (-1, -501)):
            with self.subTest(current=value):
                self.assert_current(calendar.analyze_date_savings, value, [expected],
                                    str(value), threshold=-1000)

    def test_ineligible_raw_price_is_not_sanitized(self):
        raw = dict(self.record, min_price=float("inf"))
        normalized = calendar._normalized_calendar_record(raw)
        self.assertEqual(normalized["min_price"], float("inf"))
        self.assertFalse(calendar.calendar_record_is_eligible(normalized),
                         "raw_nonfinite_admitted")
        self.assertEqual(raw, dict(self.record, min_price=float("inf")))

    def refresh(self, prices, history=None):
        with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as tmp:
            root = Path(tmp)
            data_dir = root / "calendar"
            # Separate calendar storage, request cache and empty round context.
            request_cache.reset_for_tests(root / "request_cache")
            try:
                self.assertIsNone(request_cache._current_stats_round_id)
                self.assertFalse(request_cache._resolved_request_keys_this_round)
                if history is not None:
                    self.assertFalse(calendar.calendar_record_is_eligible(history))
                    calendar.save_calendar("PVG-PEK", {
                        "dates": {CANDIDATE: copy.deepcopy(history)},
                    }, data_dir)
                source = SyntheticSource(prices)
                with patch.object(calendar, "_query_dates",
                                  return_value=[calendar.parse_date(CANDIDATE)]):
                    result = calendar.update_calendar(
                        "PVG-PEK", "PVG", "PEK", TARGET, source,
                        data_dir=data_dir, sleep_seconds=0,
                    )
                self.assertEqual(source.calls, [("PVG", "PEK", CANDIDATE, "economy")],
                                 "synthetic_source_not_called")
                self.assertTrue(calendar.calendar_path("PVG-PEK", data_dir).is_file())
                return result["dates"][CANDIDATE]
            finally:
                request_cache.reset_for_tests(None)

    def test_update_mixed_counts_only_finite(self):
        record = self.refresh([500, float("inf")])
        self.assertEqual(record["status"], "success")
        self.assertEqual(record["min_price"], 500)
        self.assertEqual(record["count"], 1, "mixed_count_includes_inf")
        self.assertTrue(calendar.calendar_record_is_eligible(record))

    def test_update_only_inf_without_history(self):
        record = self.refresh([float("inf")])
        self.assertEqual(record["status"], "empty", "all_inf_admitted")
        self.assertNotIn("min_price", record, "invented_empty_price")
        self.assertIsNone(record["last_success_at"])
        self.assertEqual(record["last_attempt_at"], NOW.isoformat(timespec="seconds"))
        self.assertFalse(calendar.calendar_record_is_eligible(record))

    def test_update_only_inf_preserves_expired_history(self):
        history = dict(self.record, min_price=650,
                       last_success_at=(NOW - timedelta(hours=8)).isoformat(),
                       last_attempt_at=(NOW - timedelta(hours=8)).isoformat(),
                       stale_after=(NOW - timedelta(hours=2)).isoformat())
        before = copy.deepcopy(history)
        record = self.refresh([float("inf")], history)
        self.assertEqual(record["status"], "empty", "history_inf_admitted")
        self.assertEqual(record["min_price"], 650)
        self.assertEqual(record["last_success_at"], history["last_success_at"])
        self.assertEqual(record["last_attempt_at"], NOW.isoformat(timespec="seconds"))
        self.assertEqual(record["updated_at"], NOW.isoformat(timespec="seconds"))
        self.assertFalse(calendar.calendar_record_is_eligible(record))
        self.assertEqual(history, before)

    def mutated(self, function, mutation):
        tree = ast.parse(textwrap.dedent(inspect.getsource(function)))
        self.assertEqual(len(tree.body), 1, "mutation_function_scope")
        self.assertIsInstance(tree.body[0], ast.FunctionDef)
        original = ast.dump(tree, include_attributes=False)
        matched = 0

        class Change(ast.NodeTransformer):
            def visit_Return(self, node):
                nonlocal matched
                if (mutation == "remove_scalar_finite" and isinstance(node.value, ast.BoolOp)
                        and isinstance(node.value.op, ast.And)
                        and ast.unparse(node.value.values[0]) == "math.isfinite(price)"):
                    matched += 1
                    node.value = node.value.values[1]
                return self.generic_visit(node)

            def visit_ExceptHandler(self, node):
                nonlocal matched
                if (mutation == "remove_overflow" and isinstance(node.type, ast.Tuple)
                        and ast.unparse(node.type) == "(TypeError, ValueError, OverflowError)"):
                    matched += 1
                    node.type.elts = [item for item in node.type.elts
                                      if not (isinstance(item, ast.Name) and item.id == "OverflowError")]
                return self.generic_visit(node)

            def visit_Call(self, node):
                nonlocal matched
                if mutation == "finite_on_raw" and ast.unparse(node) == "math.isfinite(price)":
                    matched += 1
                    node.args = [ast.Name(id="value", ctx=ast.Load())]
                return self.generic_visit(node)

            def visit_If(self, node):
                nonlocal matched
                if (mutation in {"remove_current_finite", "current_positive_gate"}
                        and ast.unparse(node.test) == "not math.isfinite(current)"):
                    matched += 1
                    if mutation == "remove_current_finite":
                        return None
                    node.test = ast.parse("not _valid_price(current_price)", mode="eval").body
                return self.generic_visit(node)

        tree = Change().visit(tree)
        self.assertEqual(matched, 1, "mutation_exact_match")
        self.assertNotEqual(ast.dump(tree, include_attributes=False), original, "mutation_changed")
        compiled = compile(ast.fix_missing_locations(tree), "<calendar-price-mutation>", "exec")
        namespace = dict(function.__globals__)
        exec(compiled, namespace)
        return namespace[function.__name__]

    def test_mutation_scalar_finite_removed(self):
        changed = self.mutated(calendar._valid_price, "remove_scalar_finite")
        with self.assertRaisesRegex(AssertionError, "scalar_result:inf"):
            self.assert_scalar(changed, float("inf"), False, "inf")

    def test_mutation_overflow_catch_removed(self):
        changed = self.mutated(calendar._valid_price, "remove_overflow")
        with self.assertRaisesRegex(AssertionError, "scalar_exception:huge_integer:OverflowError"):
            self.assert_scalar(changed, 10**400, False, "huge_integer")

    def test_mutation_current_finite_removed(self):
        changed = self.mutated(calendar.analyze_date_savings, "remove_current_finite")
        for label, value, exception in (("inf", float("inf"), "OverflowError"),
                                        ("nan", float("nan"), "ValueError")):
            with self.subTest(case=label):
                with self.assertRaisesRegex(AssertionError, f"current_exception:{label}:{exception}"):
                    self.assert_current(changed, value, [], label)

    def test_mutation_finite_on_raw_value(self):
        changed = self.mutated(calendar._valid_price, "finite_on_raw")
        with self.assertRaisesRegex(AssertionError, "scalar_exception:numeric_string:TypeError"):
            self.assert_scalar(changed, "500", True, "numeric_string")

    def test_mutation_current_positive_gate(self):
        changed = self.mutated(calendar.analyze_date_savings, "current_positive_gate")
        for value, expected in ((0, -500), (-1, -501)):
            with self.subTest(current=value):
                with self.assertRaisesRegex(AssertionError, f"current_result:{value}"):
                    self.assert_current(changed, value, [expected], str(value), threshold=-1000)


if __name__ == "__main__":
    unittest.main()
