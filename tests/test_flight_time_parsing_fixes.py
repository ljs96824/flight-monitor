"""Explicit dates project to their own local clock, never to UTC."""

import ast
from datetime import datetime
import inspect
import textwrap
import unittest

from tests import test_parse_flight_time_characterization as legacy


EXPLICIT_CASES = (
    ("colon_zero", "2026-03-02T01:45:00+00:00"),
    ("colon_eight", "2026-03-02T01:45:00+08:00"),
    ("zulu", "2026-03-02T01:45:00Z"),
    ("compact_eight", "2026-03-02T01:45:00+0800"),
    ("hour_offset", "2026-03-02T01:45:00+08"),
)
DAY_MARKER_SEPARATOR_CASES = (
    ("unseparated", "01:45+1"),
    ("single_space", "01:45 +1"),
    ("multiple_spaces", "01:45   +1"),
    ("tab", "01:45\t+1"),
    ("trailing_whitespace", "01:45+1 \t"),
)
REJECTED_CASES = (
    ("date_only", "2026-03-02"),
    ("hour_only", "2026-03-02T01"),
    ("invalid_date", "2026-02-30T01:45:00+08:00"),
    ("invalid_offset", "2026-03-02T01:45:00+25:00"),
    ("invalid_offset_minutes", "2026-03-02T01:45:00+08:60"),
    ("invalid_negative_offset", "2026-03-02T01:45:00-24:00"),
    ("dated_day_suffix", "2026-03-02T01:45:00 +1"),
    ("dated_unspaced_suffix", "2026-03-02T01:45:00+1"),
    ("long_day_suffix", "01:45+100"),
    ("invalid_hour", "24:45+1"),
    ("invalid_minute", "01:60+1"),
    ("invalid_explicit_time", "2026-03-02T24:45:00+08:00"),
    ("residual_suffix", "2026-03-02T01:45:00+08:00 junk"),
    ("non_clock_day_marker", "flight at 01:45 +1"),
)
LEGACY_SEPARATOR_CASES = tuple(
    (marker, precision, f"2026-03-02{separator}{clock}", expected)
    for marker, separator in (
        ("double_space", "  "), ("tab", "\t"), ("double_t", "TT"),
        ("mixed_whitespace", " \t "), ("t_space", "T "),
    )
    for precision, clock, expected in (
        ("minute", "01:45", datetime(2026, 3, 2, 1, 45)),
        ("second", "01:45:09", datetime(2026, 3, 2, 1, 45, 9)),
    )
)


class FlightTimeParsingFixesTest(unittest.TestCase):
    def setUp(self):
        self.fixture = legacy.ParseFlightTimeCharacterizationTest()
        self.addCleanup(self.fixture.doCleanups)
        self.fixture.setUp()
        self.addCleanup(self.fixture._assert_no_io)
        self.analyzer = self.fixture.analyzer

    def test_explicit_dates_keep_their_local_clock(self):
        for marker, value in EXPLICIT_CASES:
            for default in (legacy.DEFAULT_DATE, None, "invalid-default-is-not-used"):
                with self.subTest(case=marker, default=default):
                    legacy._assert_current_result(self, self.analyzer.parse_flight_time, value,
                                                  default, datetime(2026, 3, 2, 1, 45), marker)

    def test_pure_clock_day_markers_are_explicit_compatibility(self):
        cases = (
            ("spaced_one", "01:45 +1", legacy.DEFAULT_DATE, datetime(2026, 3, 2, 1, 45)),
            ("unspaced_one", "01:45+1", legacy.DEFAULT_DATE, datetime(2026, 3, 2, 1, 45)),
            ("two_digit_days", "01:45+08", legacy.DEFAULT_DATE, datetime(2026, 3, 9, 1, 45)),
            ("maximum_days", "01:45+99", legacy.DEFAULT_DATE, datetime(2026, 6, 8, 1, 45)),
            ("zero_days", "01:45+0", legacy.DEFAULT_DATE, legacy.DEFAULT_CLOCK),
            ("no_date", "01:45+1", None, None),
            ("invalid_default", "01:45+1", "2026-02-30", None),
            ("date_overflow", "01:45+1", "9999-12-31", None),
        )
        for marker, value, default, expected in cases:
            with self.subTest(case=marker):
                legacy._assert_current_result(self, self.analyzer.parse_flight_time,
                                              value, default, expected, marker)

    def test_incomplete_or_malformed_inputs_do_not_fall_back(self):
        for marker, value in REJECTED_CASES:
            for default in (legacy.DEFAULT_DATE, None):
                with self.subTest(case=marker, default=default):
                    legacy._assert_current_result(self, self.analyzer.parse_flight_time,
                                                  value, default, None, marker)

    def test_day_marker_separator_tolerance(self):
        for marker, value in DAY_MARKER_SEPARATOR_CASES:
            with self.subTest(case=marker):
                legacy._assert_current_result(self, self.analyzer.parse_flight_time,
                                              value, legacy.DEFAULT_DATE,
                                              datetime(2026, 3, 2, 1, 45), marker)

    def test_confirmed_legacy_format_tolerance_is_preserved(self):
        cases = (
            ("2026-3-2 1:5", datetime(2026, 3, 2, 1, 5)),
            ("2026-03-02 01:45:09", datetime(2026, 3, 2, 1, 45, 9)),
            ("1:45", legacy.DEFAULT_CLOCK),
            ("flight at 01:45", legacy.DEFAULT_CLOCK),
        )
        for value, expected in cases:
            with self.subTest(value=value):
                legacy._assert_current_result(self, self.analyzer.parse_flight_time,
                                              value, legacy.DEFAULT_DATE, expected, "legacy_tolerance")

    def test_fold_offsets_preserve_clock_not_elapsed_time(self):
        cases = (("2026-11-01T01:30:00-04:00", datetime(2026, 11, 1, 1, 30)),
                 ("2026-11-01T01:45:00-05:00", datetime(2026, 11, 1, 1, 45)))
        results = []
        for value, expected in cases:
            results.append(self.analyzer.parse_flight_time(value, legacy.DEFAULT_DATE))
            with self.subTest(value=value):
                legacy._assert_current_result(self, self.analyzer.parse_flight_time,
                                              value, legacy.DEFAULT_DATE, expected, "local_fold_clock")
        self.assertEqual((results[1] - results[0]).total_seconds(), 900)

    def test_legacy_datetime_separator_tolerance(self):
        for marker, precision, value, expected in LEGACY_SEPARATOR_CASES:
            for default in (legacy.DEFAULT_DATE, None, "invalid-default-is-not-used"):
                with self.subTest(case=marker, precision=precision, default=default):
                    legacy._assert_current_result(self, self.analyzer.parse_flight_time,
                                                  value, default, expected, marker)

    def test_separator_recovery_preserves_arrival_window_rejection(self):
        cases = (
            ("within_window", "2026-03-02  01:45", datetime(2026, 3, 2, 1, 45), True),
            ("after_window", "2026-03-02 02:01", datetime(2026, 3, 2, 2, 1), False),
            ("next_day", "2026-03-03 01:45", datetime(2026, 3, 3, 1, 45), False),
        )
        for marker, arrival, expected, admitted in cases:
            with self.subTest(case=marker):
                flight = {"segments": [{"arr_time": arrival}]}
                self.assertEqual(self.analyzer._flight_arrival_datetime(flight, "2026-03-02"),
                                 expected, marker)
                self.assertIs(self.analyzer._same_day_outbound_passes_window(
                    flight, {"outbound_arrive_by_minutes": 120}, "2026-03-02"), admitted, marker)

    def test_offset_separator_support_remains_asymmetric(self):
        cases = (
            ("single_space", "2026-03-02 01:45", datetime(2026, 3, 2, 1, 45)),
            ("single_t", "2026-03-02T01:45", datetime(2026, 3, 2, 1, 45)),
            ("single_tab_offset", "2026-03-02\t01:45+08:00", datetime(2026, 3, 2, 1, 45)),
            ("double_space_offset", "2026-03-02  01:45+08:00", None),
            ("double_t_offset", "2026-03-02TT01:45+08:00", None),
        )
        for marker, value, expected in cases:
            with self.subTest(case=marker):
                legacy._assert_current_result(self, self.analyzer.parse_flight_time,
                                              value, legacy.DEFAULT_DATE, expected, marker)

    def test_correct_dates_change_filters_and_time_tiebreak_without_changing_prices(self):
        a = self.analyzer
        shifted = {"id": "shifted", "price": 520, "segments": [{"dep_time": EXPLICIT_CASES[0][1],
                                                                "arr_time": EXPLICIT_CASES[0][1]}]}
        same_day = {"id": "same_day", "price": 520, "segments": [{"dep_time": "2026-03-01 12:00"}]}
        windows = {"outbound_arrive_by_minutes": 120, "return_depart_after_minutes": 60}
        self.assertFalse(a._same_day_outbound_passes_window(shifted, windows, legacy.DEFAULT_DATE))
        self.assertFalse(a._same_day_return_passes_window(shifted, windows, legacy.DEFAULT_DATE))
        for key in (a._same_day_return_sort_key, a._same_day_return_price_sort_key):
            self.assertEqual([f["id"] for f in sorted([shifted, same_day], key=lambda f: key(f, legacy.DEFAULT_DATE))],
                             ["same_day", "shifted"])
        self.assertEqual((shifted["price"], same_day["price"]), (520, 520))
        self.assertEqual(a._hour_from_time(EXPLICIT_CASES[0][1]), 1)

    def _mutated_parser(self, mutation):
        original = self.analyzer.parse_flight_time
        tree = ast.parse(textwrap.dedent(inspect.getsource(original)))
        before = ast.dump(tree)
        body = tree.body[0].body
        matched = 0
        if mutation == "separator_gate_narrowed":
            patterns = [node.args[0] for node in ast.walk(tree)
                        if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)
                        and node.func.attr == "fullmatch" and node.args
                        and isinstance(node.args[0], ast.Constant)
                        and isinstance(node.args[0].value, str)
                        and node.args[0].value.startswith(r"\d{4}-\d{1,2}-\d{1,2}")]
            self.assertEqual(len(patterns), 1, "full datetime gate must be identified exactly once")
            self.assertEqual(patterns[0].value.count(r"[T\s]+"), 1, "separator mutation must not be a no-op")
            patterns[0].value = patterns[0].value.replace(r"[T\s]+", "[T ]", 1)
            matched = 1
        elif mutation == "offset_path_disabled":
            blocks = [node for node in ast.walk(tree) if isinstance(node, ast.Try) and any(
                isinstance(stmt, ast.Assign) and isinstance(stmt.value, ast.Call)
                and isinstance(stmt.value.func, ast.Attribute) and stmt.value.func.attr == "fromisoformat"
                for stmt in node.body)]
            self.assertEqual(len(blocks), 1, "offset parse path must be identified")
            blocks[0].body = [ast.Return(value=ast.Constant(None))]
            matched = 1
        elif mutation == "day_marker_separator_narrowed":
            assignments = [node for node in body if isinstance(node, ast.Assign)
                           and isinstance(node.targets[0], ast.Name) and node.targets[0].id == "day_match"]
            self.assertEqual(len(assignments), 1, "the pure-clock day match must be identified")
            call = assignments[0].value
            self.assertIsInstance(call, ast.Call)
            self.assertIsInstance(call.func, ast.Attribute)
            self.assertEqual(call.func.attr, "fullmatch")
            pattern = call.args[0]
            self.assertIsInstance(pattern, ast.Constant)
            self.assertEqual(pattern.value.count(r"\s*"), 1, "one whitespace separator must change")
            pattern.value = pattern.value.replace(r"\s*", " ?", 1)
            matched = 1
        elif mutation == "historical_broad_day_extraction":
            positions = [i for i, node in enumerate(body) if isinstance(node, ast.Assign)
                         and isinstance(node.targets[0], ast.Name) and node.targets[0].id == "date_match"]
            self.assertEqual(len(positions), 1, "explicit-date entry must be identified")
            prefix = ast.parse('''legacy_day_offset = 0
legacy_offset_match = re.search(r"\\+(\\d+)\\s*$", text)
if legacy_offset_match:
    legacy_day_offset = int(legacy_offset_match.group(1))
    text = text[:legacy_offset_match.start()].strip()
''').body
            body[positions[0]:positions[0]] = prefix
            returns = [node for node in ast.walk(tree) if isinstance(node, ast.Return)
                       and isinstance(node.value, ast.Name) and node.value.id == "parsed"]
            self.assertEqual(len(returns), 2, "full-format and clock returns must both change")
            for node in returns:
                node.value = ast.parse("parsed + timedelta(days=legacy_day_offset)", mode="eval").body
            matched = 3
        elif mutation == "default_overwrites_explicit_date":
            blocks = [node for node in body if isinstance(node, ast.If)
                      and isinstance(node.test, ast.Name) and node.test.id == "date_match"]
            self.assertEqual(len(blocks), 1, "explicit-date branch must be identified")

            class OverrideDate(ast.NodeTransformer):
                count = 0

                def visit_Return(self, node):
                    if any(isinstance(child, ast.Name) and child.id == "parsed" for child in ast.walk(node)):
                        self.count += 1
                        overwrite = ast.parse('''if date_str:
    parsed = datetime.combine(datetime.strptime(date_str, "%Y-%m-%d").date(), parsed.time())
''').body[0]
                        return [overwrite, node]
                    return node

            override = OverrideDate()
            override.visit(blocks[0])
            self.assertEqual(override.count, 2, "both full parse returns must change")
            matched = override.count
        else:
            self.fail("unknown mutation")
        return legacy._compile_mutation(self, original, tree, before, mutation, matched)

    def test_mutation_separator_gate_narrowed_is_rejected(self):
        mutant = self._mutated_parser("separator_gate_narrowed")
        for marker, precision, value, expected in LEGACY_SEPARATOR_CASES:
            for default in (legacy.DEFAULT_DATE, None, "invalid-default-is-not-used"):
                with self.subTest(case=marker, precision=precision, default=default):
                    with self.assertRaisesRegex(AssertionError, marker):
                        legacy._assert_current_result(self, mutant, value, default, expected, marker)

    def test_mutation_offset_path_disabled_is_rejected(self):
        mutant = self._mutated_parser("offset_path_disabled")
        for marker, value in EXPLICIT_CASES:
            with self.subTest(case=marker), self.assertRaisesRegex(AssertionError, marker):
                legacy._assert_current_result(self, mutant, value, legacy.DEFAULT_DATE,
                                              datetime(2026, 3, 2, 1, 45), marker)

    def test_mutation_narrowed_day_marker_separator_is_rejected(self):
        mutant = self._mutated_parser("day_marker_separator_narrowed")
        for marker, value in DAY_MARKER_SEPARATOR_CASES:
            with self.subTest(case=marker):
                if marker in {"multiple_spaces", "tab"}:
                    with self.assertRaisesRegex(AssertionError, marker):
                        legacy._assert_current_result(self, mutant, value, legacy.DEFAULT_DATE,
                                                      datetime(2026, 3, 2, 1, 45), marker)
                else:
                    legacy._assert_current_result(self, mutant, value, legacy.DEFAULT_DATE,
                                                  datetime(2026, 3, 2, 1, 45), marker)

    def test_mutation_historical_broad_day_extraction_is_rejected(self):
        mutant = self._mutated_parser("historical_broad_day_extraction")
        cases = (("broad_compact_800", EXPLICIT_CASES[3][1], datetime(2026, 3, 2, 1, 45)),
                 ("broad_compact_8", EXPLICIT_CASES[4][1], datetime(2026, 3, 2, 1, 45)),
                 ("broad_long_suffix", "01:45+100", None))
        for marker, value, expected in cases:
            with self.subTest(case=marker), self.assertRaisesRegex(AssertionError, marker):
                legacy._assert_current_result(self, mutant, value, legacy.DEFAULT_DATE, expected, marker)

    def test_mutation_default_overwrites_explicit_date_is_rejected(self):
        mutant = self._mutated_parser("default_overwrites_explicit_date")
        for marker, value in EXPLICIT_CASES:
            with self.subTest(case=marker), self.assertRaisesRegex(AssertionError, marker):
                legacy._assert_current_result(self, mutant, value, legacy.DEFAULT_DATE,
                                              datetime(2026, 3, 2, 1, 45), marker)


if __name__ == "__main__":
    unittest.main()
