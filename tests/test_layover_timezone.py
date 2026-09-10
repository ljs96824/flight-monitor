"""Offset-aware layover contracts; synthetic inputs and no live dependencies."""

import ast
import copy
from datetime import datetime, timezone
import importlib
import inspect
import os
import textwrap
import unittest
from contextlib import ExitStack
from unittest.mock import patch


OFFSET_CASES = (
    ("positive_75", "2026-03-01T01:30:00+01:00", "2026-03-01T01:45:00+00:00", 75),
    ("negative_clamped", "2026-03-01T01:30:00+00:00", "2026-03-01T01:45:00+01:00", 0),
    ("overestimate_30", "2026-03-01T01:30:00+00:00", "2026-03-01T03:00:00+01:00", 30),
    ("explicit_fallback_75", "2026-11-01T01:30:00-04:00", "2026-11-01T01:45:00-05:00", 75),
)


def _assert_minutes(test, calculate, case):
    name, arrival, departure, expected = case
    actual = calculate(arrival, departure)
    test.assertIs(type(actual), int, name)
    test.assertEqual(actual, expected, name)


def _flight(arrival=OFFSET_CASES[0][1], departure=OFFSET_CASES[0][2]):
    return {
        "price": 800,
        "stops": 1,
        "airlines": ["Air China"],
        "segments": [
            {"flight_no": "CA100", "airline": "Air China", "duration_min": 60,
             "dep_airport": "SHA", "dep_time": "2026-03-01T00:30:00+01:00",
             "arr_airport": "HGH", "arr_city": "Synthetic City", "arr_time": arrival},
            {"flight_no": "CA200", "airline": "Air China", "duration_min": 60,
             "dep_airport": "HGH", "dep_time": departure,
             "arr_airport": "PEK", "arr_time": "2026-03-01T02:45:00+00:00"},
        ],
    }


class LayoverTimezoneTest(unittest.TestCase):
    def setUp(self):
        stack = ExitStack()
        self.addCleanup(stack.close)
        system = {key: value for key, value in os.environ.items() if key.upper() in {
            "SYSTEMROOT", "WINDIR", "PATH", "TEMP", "TMP", "PYTHONPATH"}}
        system.update(NO_LIVE_API="1", PYTHON_DOTENV_DISABLED="1", MPLBACKEND="Agg")
        stack.enter_context(patch.dict(os.environ, system, clear=True))
        stack.enter_context(patch("dotenv.load_dotenv", return_value=False))
        stack.enter_context(patch("dotenv.dotenv_values", return_value={}, create=True))
        self.io_spies = [stack.enter_context(patch(target, side_effect=AssertionError("offline only")))
                         for target in ("socket.create_connection", "socket.socket.connect",
                                        "socket.getaddrinfo", "smtplib.SMTP", "smtplib.SMTP_SSL",
                                        "sqlite3.connect")]
        self.addCleanup(self._assert_no_io)
        self.collector = importlib.import_module("collector")

    def _assert_no_io(self):
        for spy in self.io_spies:
            spy.assert_not_called()

    def test_explicit_offsets(self):
        for case in OFFSET_CASES:
            with self.subTest(case=case[0]):
                _assert_minutes(self, self.collector.calc_layover_minutes, case)

    def test_legacy_formats_and_equivalent_offsets(self):
        cases = (
            ("same_offset", "2026-03-01T01:30:00+08:00", "2026-03-01T03:00:00+08:00", 90),
            ("space_minutes", "2026-03-01 01:30", "2026-03-01 03:00", 90),
            ("iso_seconds", "2026-03-01T01:30:00", "2026-03-01T03:00:00", 90),
            ("iso_minutes", "2026-03-01T01:30", "2026-03-01T03:00", 90),
            ("cross_day", "2026-03-01T23:30:00+08:00", "2026-03-02T01:00:00+08:00", 90),
            ("z_to_offset", "2026-03-01T01:30:00Z", "2026-03-01T03:00:00+00:00", 90),
            ("offset_to_z", "2026-03-01T01:30:00+00:00", "2026-03-01T03:00:00Z", 90),
        )
        for case in cases:
            with self.subTest(case=case[0]):
                _assert_minutes(self, self.collector.calc_layover_minutes, case)

    def test_mixed_awareness(self):
        for case in (
            ("aware_naive", "2026-03-01T01:30:00+08:00", "2026-03-01T03:00:00", 0),
            ("naive_aware", "2026-03-01T01:30:00", "2026-03-01T03:00:00+08:00", 0),
        ):
            with self.subTest(case=case[0]):
                _assert_minutes(self, self.collector.calc_layover_minutes, case)

    def test_invalid_negative_zero_and_seconds_truncation(self):
        for case in (
            ("empty_arrival", None, "2026-03-01T03:00:00", 0),
            ("empty_departure", "2026-03-01T01:30:00", "", 0),
            ("whitespace", "   ", "2026-03-01T03:00:00", 0),
            ("malformed", "not-a-date", "2026-03-01T03:00:00", 0),
            ("malformed_departure", "2026-03-01T03:00:00", "2026-02-30T01:00:00", 0),
            ("negative_naive", "2026-03-01T03:00:00", "2026-03-01T01:30:00", 0),
            ("negative_aware", "2026-03-01T03:00:00+08:00", "2026-03-01T01:30:00+08:00", 0),
            ("zero", "2026-03-01T01:30:00Z", "2026-03-01T01:30:00+00:00", 0),
            ("seconds", "2026-03-01T01:30:00Z", "2026-03-01T01:31:59Z", 1),
            ("fractional_seconds", "2026-03-01T01:30:00.500", "2026-03-01T01:31:00.499", 0),
        ):
            with self.subTest(case=case[0]):
                _assert_minutes(self, self.collector.calc_layover_minutes, case)

    def test_independent_utc_reference(self):
        # UTC instants are hand-converted from the supplied offsets, not parsed by collector.
        instants = (
            ((2026, 3, 1, 0, 30), (2026, 3, 1, 1, 45), 75),
            ((2026, 3, 1, 1, 30), (2026, 3, 1, 0, 45), -45),
            ((2026, 3, 1, 1, 30), (2026, 3, 1, 2, 0), 30),
            ((2026, 11, 1, 5, 30), (2026, 11, 1, 6, 45), 75),
        )
        for case, (arrival, departure, signed_minutes) in zip(OFFSET_CASES, instants):
            with self.subTest(case=case[0]):
                delta = datetime(*departure, tzinfo=timezone.utc) - datetime(*arrival, tzinfo=timezone.utc)
                self.assertEqual(delta.total_seconds() / 60, signed_minutes)
                self.assertEqual(case[3], max(0, signed_minutes))

    def test_parse_flight_detail_forwards_offset_strings(self):
        flight = _flight()
        raw = {"price": 800, "total_duration": 195, "flights": [
            {"flight_number": seg["flight_no"], "airline": seg["airline"], "duration": seg["duration_min"],
             "departure_airport": {"id": seg["dep_airport"], "time": seg["dep_time"]},
             "arrival_airport": {"id": seg["arr_airport"], "time": seg["arr_time"]}}
            for seg in flight["segments"]]}
        with patch.object(self.collector, "calc_layover_minutes", wraps=self.collector.calc_layover_minutes) as spy:
            result = self.collector.parse_flight_detail(raw, "synthetic")
        spy.assert_called_once_with(OFFSET_CASES[0][1], OFFSET_CASES[0][2])
        self.assertEqual(result["segments"][0]["arr_time"], OFFSET_CASES[0][1])
        self.assertEqual(result["segments"][1]["dep_time"], OFFSET_CASES[0][2])
        self.assertEqual(result["total_duration_min"], 195)
        self.assertEqual(result["layovers"][0]["wait_minutes"], 75)

    def test_missing_layovers_generated_with_offsets(self):
        with patch.object(self.collector, "calc_layover_minutes", wraps=self.collector.calc_layover_minutes) as spy:
            result = self.collector._normalize_detail_flight(_flight(), "synthetic")
        spy.assert_called_once_with(OFFSET_CASES[0][1], OFFSET_CASES[0][2])
        self.assertEqual(result["layovers"][0]["wait_minutes"], 75)
        self.assertEqual(result["total_duration_min"], 195)
        self.assertEqual(result["total_hours"], 3.2)

    def test_zero_wait_recalculated_with_offsets(self):
        flight = _flight()
        flight["layovers"] = [{"airport": "HGH", "wait_minutes": 0}]
        with patch.object(self.collector, "calc_layover_minutes", wraps=self.collector.calc_layover_minutes) as spy:
            result = self.collector._normalize_detail_flight(flight, "synthetic")
        spy.assert_called_once_with(OFFSET_CASES[0][1], OFFSET_CASES[0][2])
        self.assertEqual(result["layovers"][0]["wait_minutes"], 75)
        self.assertEqual(result["total_duration_min"], 195)

    def test_positive_wait_is_not_recalculated(self):
        flight = _flight()
        flight["layovers"] = [{"airport": "HGH", "wait_minutes": 42}]
        with patch.object(self.collector, "calc_layover_minutes", wraps=self.collector.calc_layover_minutes) as spy:
            result = self.collector._normalize_detail_flight(flight, "synthetic")
        spy.assert_not_called()
        self.assertEqual(result["layovers"][0]["wait_minutes"], 42)
        self.assertEqual(result["total_duration_min"], 162)

    def test_mixed_normalization_keeps_integer_fallback(self):
        for case, arrival, departure in (
            ("aware_naive", "2026-03-01T01:30:00+08:00", "2026-03-01T03:00:00"),
            ("naive_aware", "2026-03-01T01:30:00", "2026-03-01T03:00:00+08:00"),
        ):
            with self.subTest(case=case):
                result = self.collector._normalize_detail_flight(_flight(arrival, departure), "synthetic")
                self.assertIs(type(result["layovers"][0]["wait_minutes"]), int)
                self.assertEqual(result["layovers"][0]["wait_minutes"], 0)
                self.assertEqual(result["total_duration_min"], 120)

    def test_existing_total_duration_is_preserved(self):
        flight = _flight()
        flight.update(total_duration_min=240, total_hours=4.0,
                      layovers=[{"airport": "HGH", "wait_minutes": 42}])
        result = self.collector._normalize_detail_flight(flight, "synthetic")
        self.assertEqual(result["total_duration_min"], 240)
        self.assertEqual(result["total_hours"], 4.0)

    def test_15_and_75_score_and_risk_characterization(self):
        analyzer = importlib.import_module("analyzer")
        results = []
        for wait in (15, 75):
            flight = _flight()
            flight["layovers"] = [{"airport": "HGH", "wait_minutes": wait}]
            flight = self.collector._normalize_detail_flight(flight, "synthetic")
            results.append((analyzer.overall_score(flight, [800], [135, 195]),
                            analyzer.calc_transfer_risk(flight)))
        self.assertEqual([item[0]["price_score"] for item in results], [7, 7])
        self.assertEqual([item[0]["stops_score"] for item in results], [8, 8])
        self.assertEqual([item[0]["layover_score"] for item in results], [8, 10])
        self.assertEqual([item[0]["duration_score"] for item in results], [10, 0])
        self.assertEqual([item[0]["total"] for item in results], [8.1, 6.0])
        self.assertEqual([item[1]["score"] for item in results], [40, 40])
        self.assertEqual([item[1]["level"] for item in results], ["medium", "medium"])
        self.assertNotEqual(results[0][1]["factors"], results[1][1]["factors"])

    def test_offset_discard_mutation_is_rejected(self):
        tree = ast.parse(textwrap.dedent(inspect.getsource(self.collector.calc_layover_minutes)))
        inner = next(node for node in tree.body[0].body if isinstance(node, ast.FunctionDef))
        iso_try = next(node for node in inner.body if isinstance(node, ast.Try))
        returned = next(node for node in iso_try.body if isinstance(node, ast.Return))
        returned.value = ast.Call(func=ast.Attribute(value=ast.Name(id="dt", ctx=ast.Load()),
                                                    attr="replace", ctx=ast.Load()),
                                  args=[], keywords=[ast.keyword(arg="tzinfo", value=ast.Constant(None))])
        namespace = {"datetime": datetime}
        exec(compile(ast.fix_missing_locations(copy.deepcopy(tree)), "<offset-discard-mutation>", "exec"), namespace)
        mutant = namespace["calc_layover_minutes"]
        for case in OFFSET_CASES:
            with self.subTest(case=case[0]):
                with self.assertRaisesRegex(AssertionError, case[0]):
                    _assert_minutes(self, mutant, case)


if __name__ == "__main__":
    unittest.main()
