"""Current local-clock behavior, including defects; changes require adjudication."""

import ast
from contextlib import ExitStack
from datetime import datetime
import importlib
import inspect
import os
import textwrap
import unittest
from unittest.mock import patch


DEFAULT_DATE = "2026-03-01"
DEFAULT_CLOCK = datetime(2026, 3, 1, 1, 45)
COLON_AND_Z = (
    "2026-03-02T01:45:00+00:00",
    "2026-03-02T01:45:00+08:00",
    "2026-03-02T01:45:00Z",
)
# Independent calendar expectations: these pin the defects, not correct UTC parsing.
COMPACT_CASES = (
    ("compact_800_days", "2026-03-02T01:45:00+0800", datetime(2028, 5, 10, 1, 45)),
    ("compact_8_days", "2026-03-02T01:45:00+08", datetime(2026, 3, 10, 1, 45)),
)
LEGACY_CASES = (
    ("full_minutes", "2026-03-02 01:45", DEFAULT_DATE, datetime(2026, 3, 2, 1, 45)),
    ("full_seconds", "2026-03-02 01:45:00", DEFAULT_DATE, datetime(2026, 3, 2, 1, 45)),
    ("day_marker", "01:45 +1", DEFAULT_DATE, datetime(2026, 3, 2, 1, 45)),
    ("clock_only", "01:45", DEFAULT_DATE, DEFAULT_CLOCK),
)
BOUNDARY_CASES = (
    ("empty", "", DEFAULT_DATE),
    ("none", None, DEFAULT_DATE),
    ("malformed", "not-a-time", DEFAULT_DATE),
    ("date_only", "2026-03-02", DEFAULT_DATE),
    ("clock_without_date", "01:45", None),
)


def _assert_current_result(test, parse, value, date_str, expected, marker):
    actual = parse(value, date_str)
    test.assertEqual(actual, expected, marker)
    if actual is not None:
        test.assertIs(type(actual), datetime, marker)
        test.assertIsNone(actual.tzinfo, marker)


class ParseFlightTimeCharacterizationTest(unittest.TestCase):
    def setUp(self):
        stack = ExitStack()
        self.addCleanup(stack.close)
        environment = {key: value for key, value in os.environ.items() if key.upper() in {
            "SYSTEMROOT", "WINDIR", "PATH", "TEMP", "TMP", "PYTHONPATH"}}
        environment.update(NO_LIVE_API="1", PYTHON_DOTENV_DISABLED="1", MPLBACKEND="Agg")
        stack.enter_context(patch.dict(os.environ, environment, clear=True))
        stack.enter_context(patch("dotenv.load_dotenv", return_value=False))
        stack.enter_context(patch("dotenv.dotenv_values", return_value={}, create=True))
        self.io_spies = [stack.enter_context(patch(target, side_effect=AssertionError("offline only")))
                         for target in ("socket.create_connection", "socket.socket.connect",
                                        "socket.getaddrinfo", "smtplib.SMTP", "smtplib.SMTP_SSL",
                                        "sqlite3.connect")]
        self.addCleanup(self._assert_no_io)
        self.analyzer = importlib.import_module("analyzer")

    def _assert_no_io(self):
        for spy in self.io_spies:
            spy.assert_not_called()

    def test_colon_offsets_and_z_override_date_or_return_none(self):
        for value in COLON_AND_Z:
            for date_str, expected in ((DEFAULT_DATE, DEFAULT_CLOCK), (None, None)):
                with self.subTest(value=value, date_str=date_str):
                    _assert_current_result(self, self.analyzer.parse_flight_time, value,
                                           date_str, expected, "colon_or_z_current_fallback")

    def test_compact_offsets_are_currently_day_counts(self):
        for marker, value, expected in COMPACT_CASES:
            for date_str in (DEFAULT_DATE, None):
                with self.subTest(case=marker, date_str=date_str):
                    _assert_current_result(self, self.analyzer.parse_flight_time, value,
                                           date_str, expected, marker)

    def test_unambiguous_legacy_inputs(self):
        for marker, value, date_str, expected in LEGACY_CASES:
            with self.subTest(case=marker):
                _assert_current_result(self, self.analyzer.parse_flight_time, value,
                                       date_str, expected, marker)

    def test_empty_malformed_and_incomplete_inputs(self):
        for marker, value, date_str in BOUNDARY_CASES:
            with self.subTest(case=marker):
                _assert_current_result(self, self.analyzer.parse_flight_time, value,
                                       date_str, None, marker)

    def test_return_type_is_naive_datetime_or_none(self):
        cases = [(value, date_str) for value in COLON_AND_Z for date_str in (DEFAULT_DATE, None)]
        cases += [(value, DEFAULT_DATE) for _, value, _ in COMPACT_CASES]
        cases += [(value, date_str) for _, value, date_str, _ in LEGACY_CASES]
        cases += [(value, date_str) for _, value, date_str in BOUNDARY_CASES]
        for value, date_str in cases:
            with self.subTest(value=value, date_str=date_str):
                result = self.analyzer.parse_flight_time(value, date_str)
                if result is not None:
                    self.assertIs(type(result), datetime)
                    self.assertIsNone(result.tzinfo, "current_return_is_naive")

    def test_arrival_compares_with_naive_business_window(self):
        flight = {"segments": [{"arr_time": COLON_AND_Z[0]}]}
        arrival = self.analyzer._flight_arrival_datetime(flight, DEFAULT_DATE)
        window = self.analyzer._minutes_datetime(DEFAULT_DATE, 120)
        self.assertEqual(arrival, DEFAULT_CLOCK)
        self.assertEqual(window, datetime(2026, 3, 1, 2))
        self.assertIsNone(arrival.tzinfo)
        self.assertIsNone(window.tzinfo)
        self.assertLess(arrival, window)
        self.assertEqual((window - arrival).total_seconds(), 900)

    def _mutated_parser(self, mutation):
        original = self.analyzer.parse_flight_time
        tree = ast.parse(textwrap.dedent(inspect.getsource(original)))
        body = tree.body[0].body
        if mutation == "full_formats_fail":
            loop = next(node for node in body if isinstance(node, ast.For))
            self.assertEqual(loop.target.id, "fmt")
            loop.iter = ast.Tuple(elts=[], ctx=ast.Load())
        elif mutation == "day_offset_removed":
            block = next(node for node in body if isinstance(node, ast.If)
                         and isinstance(node.test, ast.Name) and node.test.id == "offset_match")
            block.test = ast.Constant(False)
        elif mutation == "clock_fallback_none":
            assignment = next(node for node in body if isinstance(node, ast.Assign)
                              and isinstance(node.targets[0], ast.Name)
                              and node.targets[0].id == "compact_time")
            body.insert(body.index(assignment), ast.Return(value=ast.Constant(None)))
        else:
            self.fail("unknown mutation")
        namespace = dict(original.__globals__)
        exec(compile(ast.fix_missing_locations(tree), "<parse-flight-time-mutation>", "exec"), namespace)
        return namespace[original.__name__]

    def test_mutation_full_formats_fail_is_rejected(self):
        mutant = self._mutated_parser("full_formats_fail")
        for marker, value, date_str, expected in LEGACY_CASES[:2]:
            with self.subTest(case=marker), self.assertRaisesRegex(AssertionError, marker):
                _assert_current_result(self, mutant, value, date_str, expected, marker)

    def test_mutation_day_offset_removed_is_rejected(self):
        mutant = self._mutated_parser("day_offset_removed")
        cases = [(marker, value, DEFAULT_DATE, expected) for marker, value, expected in COMPACT_CASES]
        cases.append(LEGACY_CASES[2])
        for marker, value, date_str, expected in cases:
            with self.subTest(case=marker), self.assertRaisesRegex(AssertionError, marker):
                _assert_current_result(self, mutant, value, date_str, expected, marker)

    def test_mutation_clock_fallback_none_is_rejected(self):
        mutant = self._mutated_parser("clock_fallback_none")
        for value in COLON_AND_Z:
            with self.subTest(value=value), self.assertRaisesRegex(AssertionError, "colon_or_z_current_fallback"):
                _assert_current_result(self, mutant, value, DEFAULT_DATE, DEFAULT_CLOCK,
                                       "colon_or_z_current_fallback")


if __name__ == "__main__":
    unittest.main()
