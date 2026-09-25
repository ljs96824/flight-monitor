"""Read-only snapshot contracts for report-scoped lineage evaluation."""
import ast
from contextlib import ExitStack, closing
import copy
from datetime import date
import hashlib
import inspect
import json
from pathlib import Path
import sqlite3
import tempfile
import unittest
from unittest.mock import patch

import forecast
from scripts import forecast_report as report
from test_tcurve import SCHEMA


BASE_SHA = "ba4644ea2845f86718e04bb442efbe40a21917c5"
ROUTE = "\u4e0a\u6d77-\u5927\u962a"
AS_OF = "2026-08-12"
KINDS = ("complete", "partial", "missing", "cutoff", "degraded")
EMPTY_KINDS = ("empty", "all_degraded")


def make_snapshot(root, kind):
    root.mkdir()
    db = root / "observations.sqlite3"
    with closing(sqlite3.connect(db)) as connection:
        connection.execute(SCHEMA)
        if kind != "empty":
            for depart in ("2026-09-10", "2026-09-11"):
                for day in ("2026-08-11", AS_OF, "2026-08-13"):
                    missing = (kind == "missing" or
                               kind == "partial" and depart == "2026-09-11" and day == AS_OF or
                               kind == "cutoff" and day >= AS_OF or
                               kind == "degraded" and day == AS_OF)
                    degraded = kind == "all_degraded" or kind == "degraded" and day == AS_OF
                    for source in (("hasdata",) if degraded else ("hasdata", "juhe")):
                        connection.execute(
                            "INSERT INTO observations (observed_at, round_id, route_type, "
                            "origin_airport, dest_airport, depart_date, days_to_departure, "
                            "cabin_class, source, flight_combo, airline, stops, duration_min, "
                            "price_cny, method_version) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                            (day + "T09:00:00+08:00", "" if missing else "round-" + depart + "-" + day,
                             "international", "PVG", "KIX", depart,
                             (date.fromisoformat(depart) - date.fromisoformat(day)).days,
                             "economy", source, "MU" + depart[-2:] + day[-2:],
                             "synthetic", 0, 120, 100 + int(day[-2:]), "v1"),
                        )
        connection.commit()
    with closing(sqlite3.connect(root / "prices.db")) as connection:
        connection.execute("CREATE TABLE synthetic (id INTEGER)")
        connection.commit()
    (root / "api_usage.json").write_text("{}", encoding="utf-8")
    (root / "snapshot_manifest.json").write_text(
        json.dumps({"permission_quality_cells": []}), encoding="utf-8")
    return root


def _tree():
    return ast.parse(inspect.getsource(report.generate_report))


def _hoists(tree):
    return [node for node in tree.body[0].body if isinstance(node, ast.Assign)
            and len(node.targets) == 1 and isinstance(node.targets[0], ast.Name)
            and node.targets[0].id == "lineage_complete"]


def _lineage_keyword(tree):
    matches = [node for node in ast.walk(tree) if isinstance(node, ast.keyword)
               and node.arg == "lineage_complete"]
    if len(matches) != 1:
        raise AssertionError("LINEAGE_KEYWORD_MATCH")
    return matches[0]


def _compile(tree):
    ast.fix_missing_locations(tree)
    namespace = dict(vars(report))
    exec(compile(tree, "<forecast-lineage-contract>", "exec"), namespace)
    return namespace["generate_report"]


def baseline_report():
    # Undo only the lineage hoist to compare per-departure and per-report behavior.
    # This also works in CI's shallow checkout without fetching historical code.
    tree = _tree()
    hoists = _hoists(tree)
    if hoists:
        if len(hoists) != 1:
            raise AssertionError("BASELINE_HOIST_MATCH")
        node = hoists[0]
        _lineage_keyword(tree).value = copy.deepcopy(node.value)
        tree.body[0].body.remove(node)
    return _compile(tree)


class ForecastReportLineageOnceTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.snapshots = {kind: make_snapshot(self.root / kind, kind)
                          for kind in KINDS + EMPTY_KINDS}

    def invoke(self, function, kind, as_of):
        root = self.snapshots[kind]
        before = {p.name: p.read_bytes() for p in root.iterdir()}
        connect = sqlite3.connect
        reads = []

        def readonly(*args, **kwargs):
            self.assertTrue(kwargs.get("uri"), "SQLITE_MUST_BE_READONLY")
            self.assertIn("?mode=ro", str(args[0]), "SQLITE_MUST_BE_READONLY")
            reads.append(args[0])
            return connect(*args, **kwargs)

        with patch.object(sqlite3, "connect", side_effect=readonly):
            result = function(db_path=root, route=ROUTE, airport_pair="PVG-KIX", as_of_day=as_of)
        self.assertTrue(reads, "REAL_SNAPSHOT_READ_REQUIRED")
        self.assertEqual({p.name: p.read_bytes() for p in root.iterdir()}, before, "SNAPSHOT_CHANGED")
        return result

    def assert_equivalent(self, function, kind, as_of):
        baseline = self.invoke(baseline_report(), kind, as_of)
        actual = self.invoke(function, kind, as_of)
        self.assertEqual(actual[0].encode("utf-8"), baseline[0].encode("utf-8"), "REPORT_TEXT_BYTES_CHANGED")
        self.assertEqual(actual[1], baseline[1], "REPORT_STRUCTURE_CHANGED")
        return actual

    def assert_count(self, function, kind, as_of, count):
        spy = unittest.mock.Mock(wraps=forecast.lineage_complete_for_cells)
        with patch.dict(function.__globals__, {"lineage_complete_for_cells": spy}):
            result = self.invoke(function, kind, as_of)
        marker = "EMPTY_LINEAGE_CALL_COUNT" if kind in EMPTY_KINDS else "REPORT_LINEAGE_CALL_COUNT"
        self.assertEqual(spy.call_count, count, marker)
        if count:
            self.assertGreaterEqual(len(result[1]["forecasts"]), 2, "MULTI_DEPARTURE_CONTROL")
        return result, spy

    def test_return_bytes_and_structure_match_baseline(self):
        for kind in KINDS + EMPTY_KINDS:
            for as_of in (None, AS_OF):
                with self.subTest(kind=kind, as_of=as_of):
                    self.assert_equivalent(report.generate_report, kind, as_of)

    def test_lineage_once_for_nonempty_report(self):
        for kind in KINDS:
            for as_of in (None, AS_OF):
                with self.subTest(kind=kind, as_of=as_of):
                    self.assert_count(baseline_report(), kind, as_of, 2)
                    self.assert_count(report.generate_report, kind, as_of, 1)

    def test_both_early_returns_skip_lineage(self):
        for kind in EMPTY_KINDS:
            for as_of in (None, AS_OF):
                with self.subTest(kind=kind, as_of=as_of):
                    self.assert_count(baseline_report(), kind, as_of, 0)
                    self.assert_count(report.generate_report, kind, as_of, 0)

    def test_lineage_arguments_are_included_and_report_cutoff(self):
        for kind in KINDS:
            for as_of in (None, AS_OF):
                with self.subTest(kind=kind, as_of=as_of):
                    with patch.object(report, "lineage_complete_for_cells", wraps=forecast.lineage_complete_for_cells) as spy:
                        result = self.invoke(report.generate_report, kind, as_of)
                    self.assertGreater(spy.call_count, 0)
                    cells = report.filter_forecast_cells(report.load_tcurve_daily_cells(
                        self.snapshots[kind] / "observations.sqlite3", route=ROUTE, airport_pair="PVG-KIX"))
                    cutoff = result[1]["as_of_day"]
                    included = [c for c in cells if c["observed_day"] <= cutoff and not c["degraded"]]
                    for call in spy.call_args_list:
                        self.assertEqual(call.args, (included,), "LINEAGE_INCLUDED_ARGUMENT")
                        self.assertEqual(call.kwargs, {"as_of_day": cutoff}, "LINEAGE_CUTOFF_ARGUMENT")

    def test_cutoff_and_degraded_fixtures_have_distinct_lineage(self):
        def cells(kind):
            return report.load_tcurve_daily_cells(self.snapshots[kind] / "observations.sqlite3", route=ROUTE)
        boundary = cells("cutoff")
        self.assertTrue(all(c["lineage_complete"] for c in boundary if c["observed_day"] < AS_OF))
        self.assertTrue(all(not c["lineage_complete"] for c in boundary if c["observed_day"] == AS_OF))
        self.assertTrue(any(c["observed_day"] == AS_OF for c in boundary))
        degraded = [c for c in cells("degraded") if c["observed_day"] <= AS_OF]
        self.assertTrue(any(c["degraded"] for c in degraded))
        self.assertFalse(forecast.lineage_complete_for_cells(degraded, as_of_day=AS_OF))
        self.assertTrue(forecast.lineage_complete_for_cells([c for c in degraded if not c["degraded"]], as_of_day=AS_OF))

    def test_calls_preserve_input_cells_and_arguments(self):
        observed = {}
        loaded = []
        loader = report.load_tcurve_daily_cells

        def load(*args, **kwargs):
            result = loader(*args, **kwargs)
            loaded.append((result, copy.deepcopy(result)))
            return result

        def guard(name, original):
            def wrapped(*args, **kwargs):
                before = copy.deepcopy((args, kwargs))
                value = original(*args, **kwargs)
                self.assertEqual((args, kwargs), before, "CALL_MUTATED_INPUT_" + name)
                observed[name] = observed.get(name, 0) + 1
                return value
            return wrapped

        with ExitStack() as stack:
            stack.enter_context(patch.object(report, "load_tcurve_daily_cells", side_effect=load))
            for name in ("estimate_level", "source_coverage_for_departure", "regime_departure_n",
                         "_future_shape_points", "evaluate_forecast_eligibility", "holiday_labels_for_route"):
                stack.enter_context(patch.object(report, name, side_effect=guard(name, getattr(report, name))))
            for name in ("_usable", "predict_price"):
                stack.enter_context(patch.object(forecast, name, side_effect=guard(name, getattr(forecast, name))))
            for kind in KINDS:
                self.invoke(report.generate_report, kind, AS_OF)
        self.assertEqual(set(observed), {"estimate_level", "source_coverage_for_departure", "regime_departure_n",
                         "_future_shape_points", "evaluate_forecast_eligibility", "holiday_labels_for_route", "_usable", "predict_price"})
        for actual, before in loaded:
            self.assertEqual(actual, before, "LOADED_CELLS_MUTATED")

    def test_build_replay_reports_bytes_and_hashes_match(self):
        from runtime_backup import build_replay_reports
        for kind in KINDS + EMPTY_KINDS:
            with self.subTest(kind=kind):
                baseline_dir = self.root / (kind + "-baseline")
                current_dir = self.root / (kind + "-current")
                with patch.object(report, "generate_report", baseline_report()):
                    old = build_replay_reports(self.snapshots[kind], baseline_dir, ROUTE, "PVG-KIX")
                new = build_replay_reports(self.snapshots[kind], current_dir, ROUTE, "PVG-KIX")
                self.assertEqual(set(new), {"tcurve_source.txt", "forecast_source.txt"})
                self.assertEqual(new, old, "REPLAY_HASHES_CHANGED")
                for name in new:
                    a, b = (baseline_dir / name).read_bytes(), (current_dir / name).read_bytes()
                    self.assertEqual(a, b, "REPLAY_REPORT_BYTES_CHANGED")
                    self.assertEqual(hashlib.sha256(b).hexdigest(), new[name])

    def mutant(self, kind):
        tree = _tree()
        hoists = _hoists(tree)
        self.assertEqual(len(hoists), 1, "MUTATION_HOIST_MATCH")
        assignment = hoists[0]
        self.assertEqual(ast.unparse(assignment.value), "lineage_complete_for_cells(included, as_of_day=as_of)")
        keyword = _lineage_keyword(tree)
        self.assertEqual(ast.unparse(keyword.value), "lineage_complete")
        before = ast.dump(tree, include_attributes=False)
        if kind == "earlier_cutoff":
            assignment.value.keywords[0].value = ast.parse(
                "(date.fromisoformat(as_of) - timedelta(days=1)).isoformat()", mode="eval").body
        elif kind == "includes_degraded":
            assignment.value.args[0] = ast.Name(id="cells", ctx=ast.Load())
        elif kind == "per_departure":
            keyword.value = copy.deepcopy(assignment.value)
            tree.body[0].body.remove(assignment)
        elif kind == "before_early_return":
            targets = [n for n in tree.body[0].body if isinstance(n, ast.If) and ast.unparse(n.test) == "not included"]
            self.assertEqual(len(targets), 1, "MUTATION_RETURN_MATCH")
            tree.body[0].body.remove(assignment)
            tree.body[0].body.insert(tree.body[0].body.index(targets[0]), assignment)
        else:
            self.fail("UNKNOWN_MUTATION")
        self.assertNotEqual(ast.dump(tree, include_attributes=False), before, "MUTATION_SOURCE_UNCHANGED")
        return _compile(tree)

    def test_mutation_earlier_cutoff_changes_report(self):
        changed = self.mutant("earlier_cutoff")
        with self.assertRaisesRegex(AssertionError, "REPORT_TEXT_BYTES_CHANGED"):
            self.assert_equivalent(changed, "cutoff", AS_OF)

    def test_mutation_degraded_cells_change_report(self):
        changed = self.mutant("includes_degraded")
        with self.assertRaisesRegex(AssertionError, "REPORT_TEXT_BYTES_CHANGED"):
            self.assert_equivalent(changed, "degraded", AS_OF)

    def test_mutation_per_departure_repeats_work(self):
        changed = self.mutant("per_departure")
        with self.assertRaisesRegex(AssertionError, "REPORT_LINEAGE_CALL_COUNT"):
            self.assert_count(changed, "complete", AS_OF, 1)

    def test_mutation_before_early_return_does_unused_work(self):
        changed = self.mutant("before_early_return")
        with self.assertRaisesRegex(AssertionError, "EMPTY_LINEAGE_CALL_COUNT"):
            self.assert_count(changed, "all_degraded", AS_OF, 0)


if __name__ == "__main__":
    unittest.main()
