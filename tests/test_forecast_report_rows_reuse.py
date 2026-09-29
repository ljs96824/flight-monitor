"""Snapshot contracts for reusing route observations within one report."""
import ast
import copy
import hashlib
import inspect
from pathlib import Path
import sqlite3
import tempfile
import unittest
from unittest.mock import Mock, patch

import patterns
from scripts import forecast_report as report
from tests.test_forecast_report_lineage_once import AS_OF, ROUTE, make_snapshot


CASES = (("complete", None), ("complete", AS_OF), ("complete", "2026-08-11"),
         ("partial", AS_OF), ("degraded", AS_OF), ("empty", AS_OF),
         ("complete", "2026-08-01"), ("all_degraded", AS_OF))
EARLY_CASES = (("empty", AS_OF), ("complete", "2026-08-01"))


def _compile(tree, namespace):
    ast.fix_missing_locations(tree)
    scope = dict(namespace)
    exec(compile(tree, "<forecast-rows-contract>", "exec"), scope)
    return scope[tree.body[0].name]


def baseline_tree():
    # Remove only the new keyword; the omitted-rows path retains the old read.
    tree = ast.parse(inspect.getsource(report.generate_report))
    calls = [n for n in ast.walk(tree) if isinstance(n, ast.Call)
             and isinstance(n.func, ast.Name) and n.func.id == "build_route_patterns"]
    if len(calls) != 1:
        raise AssertionError("BASELINE_PATTERN_CALL_MATCH")
    calls[0].keywords = [k for k in calls[0].keywords if k.arg != "rows"]
    return tree


def baseline_report():
    return _compile(baseline_tree(), vars(report))


def baseline_patterns_tree():
    tree = ast.parse(inspect.getsource(patterns.build_route_patterns))
    function = tree.body[0]
    indexes = [i for i, arg in enumerate(function.args.kwonlyargs) if arg.arg == "rows"]
    if indexes:
        if len(indexes) != 1:
            raise AssertionError("BASELINE_ROWS_PARAMETER_MATCH")
        index = indexes[0]
        del function.args.kwonlyargs[index]
        del function.args.kw_defaults[index]
        guards = [n for n in function.body if isinstance(n, ast.If)
                  and ast.unparse(n.test) == "rows is None"]
        if len(guards) != 1 or len(guards[0].body) != 1:
            raise AssertionError("BASELINE_ROWS_GUARD_MATCH")
        index = function.body.index(guards[0])
        function.body[index:index + 1] = guards[0].body
    return tree


class ForecastReportRowsReuseTest(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.root = Path(temp.name)
        self.snapshots = {kind: make_snapshot(self.root / kind, kind)
                          for kind in sorted({kind for kind, _ in CASES})}

    def invoke(self, function, kind, as_of):
        snapshot = self.snapshots[kind]
        before = {p.name: p.read_bytes() for p in snapshot.iterdir()}
        connect = sqlite3.connect
        reads = []

        def readonly(*args, **kwargs):
            self.assertTrue(kwargs.get("uri"), "SQLITE_MUST_BE_READONLY")
            self.assertIn("?mode=ro", str(args[0]), "SQLITE_MUST_BE_READONLY")
            reads.append(args[0])
            return connect(*args, **kwargs)

        with patch.object(sqlite3, "connect", side_effect=readonly):
            value = function(db_path=snapshot, route=ROUTE, airport_pair="PVG-KIX", as_of_day=as_of)
        self.assertTrue(reads, "REAL_SNAPSHOT_READ_REQUIRED")
        self.assertEqual({p.name: p.read_bytes() for p in snapshot.iterdir()}, before, "SNAPSHOT_CHANGED")
        return value

    def assert_equivalent(self, kind, as_of, pattern_function=None):
        expected = self.invoke(baseline_report(), kind, as_of)
        with patch.object(report, "build_route_patterns", pattern_function or patterns.build_route_patterns):
            actual = self.invoke(report.generate_report, kind, as_of)
        self.assertEqual(actual[0].encode("utf-8"), expected[0].encode("utf-8"), "REPORT_TEXT_BYTES_CHANGED")
        self.assertEqual(actual[1], expected[1], "REPORT_STRUCTURE_CHANGED")

    def assert_count(self, function, kind, as_of, expected, pattern_function=None):
        implementation = pattern_function or patterns.build_route_patterns
        first = Mock(wraps=report.load_route_observations)
        second = Mock(wraps=patterns.load_route_observations)
        with patch.dict(function.__globals__, {"load_route_observations": first,
                                              "build_route_patterns": implementation}), \
                patch.dict(implementation.__globals__, {"load_route_observations": second}):
            self.invoke(function, kind, as_of)
        self.assertEqual(first.call_count + second.call_count, expected, "ROUTE_READ_TOTAL")
        self.assertEqual(first.call_count, int(expected > 0), "REPORT_BINDING_COUNT")
        self.assertEqual(second.call_count, max(0, expected - 1), "PATTERNS_BINDING_COUNT")

    def assert_empty_rows(self, function):
        db = self.snapshots["complete"] / "observations.sqlite3"
        with patch.dict(function.__globals__, {"load_route_observations": Mock(return_value=[])}):
            expected = function(db, route=ROUTE, as_of_day=AS_OF)
        spy = Mock(wraps=patterns.load_route_observations)
        rows = []
        with patch.dict(function.__globals__, {"load_route_observations": spy}):
            actual = function(db, route=ROUTE, as_of_day=AS_OF, rows=rows)
        self.assertEqual(spy.call_count, 0, "EMPTY_ROWS_READ")
        self.assertEqual(actual, expected, "EMPTY_ROWS_RESULT")
        self.assertEqual(rows, [])

    def test_report_bytes_and_structure(self):
        for kind, as_of in CASES:
            with self.subTest(kind=kind, as_of=as_of):
                self.assert_equivalent(kind, as_of)

    def test_nonempty_report_reads_once_across_both_bindings(self):
        for kind, as_of in CASES[:5]:
            with self.subTest(kind=kind, as_of=as_of):
                self.assert_count(baseline_report(), kind, as_of, 2)
                self.assert_count(report.generate_report, kind, as_of, 1)

    def test_early_returns_read_neither_binding(self):
        for kind, as_of in EARLY_CASES:
            with self.subTest(kind=kind, as_of=as_of):
                self.assert_count(baseline_report(), kind, as_of, 0)
                self.assert_count(report.generate_report, kind, as_of, 0)

    def test_input_rows_unchanged_through_report_and_patterns(self):
        loaded = []
        seen = []
        loader = report.load_route_observations
        route_codes = report._route_codes
        builder = patterns.build_patterns
        route_builder = patterns.build_route_patterns

        def load(*args, **kwargs):
            rows = loader(*args, **kwargs)
            loaded.append((rows, copy.deepcopy(rows)))
            return rows

        def codes(rows):
            before = copy.deepcopy(rows)
            result = route_codes(rows)
            self.assertEqual(rows, before, "ROUTE_CODES_MUTATED_ROWS")
            return result

        def build(rows, **kwargs):
            before = copy.deepcopy(rows)
            result = builder(rows, **kwargs)
            self.assertEqual(rows, before, "BUILD_PATTERNS_MUTATED_ROWS")
            seen.append(before)
            return result

        def boundary(*args, **kwargs):
            rows, before = loaded[-1]
            self.assertEqual(rows, before, "ROWS_CHANGED_BEFORE_PATTERNS")
            if "rows" in kwargs:
                self.assertIs(kwargs["rows"], rows, "ROWS_NOT_REUSED_BY_IDENTITY")
            return route_builder(*args, **kwargs)

        with patch.object(report, "load_route_observations", side_effect=load), \
                patch.object(report, "_route_codes", side_effect=codes), \
                patch.object(report, "build_route_patterns", side_effect=boundary), \
                patch.object(patterns, "build_patterns", side_effect=build):
            for kind, as_of in CASES[:5]:
                self.invoke(report.generate_report, kind, as_of)
        self.assertEqual(len(loaded), 5)
        self.assertEqual(len(seen), 5)
        for rows, before in loaded:
            self.assertEqual(rows, before, "ROWS_CHANGED_AFTER_REPORT")

    def test_omitted_rows_matches_baseline(self):
        old = _compile(baseline_patterns_tree(), vars(patterns))
        for kind, as_of in CASES:
            with self.subTest(kind=kind, as_of=as_of):
                args = dict(route=ROUTE, airport_pair="PVG-KIX", as_of_day=as_of)
                db = self.snapshots[kind] / "observations.sqlite3"
                self.assertEqual(patterns.build_route_patterns(db, **args), old(db, **args))

    def test_empty_rows_never_reads(self):
        self.assert_empty_rows(patterns.build_route_patterns)

    def test_snapshot_has_boundary_and_future_observations(self):
        rows = patterns.load_route_observations(self.snapshots["complete"] / "observations.sqlite3", route=ROUTE)
        days = {row["observed_at"][:10] for row in rows}
        self.assertIn(AS_OF, days)
        self.assertTrue(any(day > AS_OF for day in days))
        self.assertTrue(any(day < AS_OF for day in days))

    def test_replay_report_bytes_and_hashes(self):
        from runtime_backup import build_replay_reports
        for kind in self.snapshots:
            with self.subTest(kind=kind):
                before_dir, after_dir = self.root / (kind + "-before"), self.root / (kind + "-after")
                with patch.object(report, "generate_report", baseline_report()):
                    before = build_replay_reports(self.snapshots[kind], before_dir, ROUTE, "PVG-KIX")
                after = build_replay_reports(self.snapshots[kind], after_dir, ROUTE, "PVG-KIX")
                self.assertEqual(set(after), {"tcurve_source.txt", "forecast_source.txt"})
                self.assertEqual(after, before, "REPLAY_HASHES_CHANGED")
                for name in after:
                    old, new = (before_dir / name).read_bytes(), (after_dir / name).read_bytes()
                    self.assertEqual(new, old, "REPLAY_BYTES_CHANGED")
                    self.assertEqual(hashlib.sha256(new).hexdigest(), after[name])

    def mutant(self, kind):
        tree = ast.parse(inspect.getsource(patterns.build_route_patterns))
        body = tree.body[0].body
        guards = [node for node in body if isinstance(node, ast.If) and ast.unparse(node.test) == "rows is None"]
        filters = [node for node in body if isinstance(node, ast.If) and ast.unparse(node.test) == "as_of_day is not None"]
        self.assertEqual(len(guards), 1, "MUTATION_GUARD_MATCH")
        self.assertEqual(len(filters), 1, "MUTATION_FILTER_MATCH")
        before = ast.dump(tree, include_attributes=False)
        if kind == "skip_filter":
            original_filter = filters[0]
            body.remove(original_filter)
            guards[0].body.append(original_filter)
        elif kind == "reload":
            index = body.index(guards[0])
            body[index:index + 1] = guards[0].body
        elif kind == "strict_cutoff":
            compares = [n for n in ast.walk(filters[0]) if isinstance(n, ast.Compare)
                        and len(n.ops) == 1 and isinstance(n.ops[0], ast.LtE)]
            self.assertEqual(len(compares), 1, "MUTATION_COMPARISON_MATCH")
            compares[0].ops[0] = ast.Lt()
        elif kind == "falsey_rows":
            guards[0].test = ast.parse("not rows", mode="eval").body
        else:
            self.fail("UNKNOWN_MUTATION")
        self.assertNotEqual(ast.dump(tree, include_attributes=False), before, "MUTATION_SOURCE_UNCHANGED")
        return _compile(tree, vars(patterns))

    def test_mutation_skip_filter_changes_report(self):
        changed = self.mutant("skip_filter")
        with self.assertRaisesRegex(AssertionError, "REPORT_TEXT_BYTES_CHANGED"):
            self.assert_equivalent("complete", AS_OF, changed)

    def test_mutation_reload_repeats_read(self):
        changed = self.mutant("reload")
        with self.assertRaisesRegex(AssertionError, "ROUTE_READ_TOTAL"):
            self.assert_count(report.generate_report, "complete", AS_OF, 1, changed)

    def test_mutation_strict_cutoff_changes_report(self):
        changed = self.mutant("strict_cutoff")
        with self.assertRaisesRegex(AssertionError, "REPORT_TEXT_BYTES_CHANGED"):
            self.assert_equivalent("complete", AS_OF, changed)

    def test_mutation_falsey_rows_reads_empty(self):
        changed = self.mutant("falsey_rows")
        with self.assertRaisesRegex(AssertionError, "EMPTY_ROWS_READ"):
            self.assert_empty_rows(changed)


if __name__ == "__main__":
    unittest.main()
