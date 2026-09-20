"""Synthetic fixed-clock contracts for the rendered calendar minimum."""

import ast
import copy
import inspect
import textwrap
import unittest
from datetime import datetime
from unittest.mock import patch

import price_calendar as calendar
from project_time import SHANGHAI_TZ


NOW = datetime(2026, 9, 20, 0, 15, tzinfo=SHANGHAI_TZ)
YESTERDAY, TODAY, TOMORROW = "2026-09-19", "2026-09-20", "2026-09-21"
SUCCESS = "2026-09-19T23:45:00+08:00"
DEADLINE = "2026-09-20T05:45:00+08:00"


def fresh(price, **changes):
    result = dict(status="success", min_price=price, airline="XX", count=3,
                  sources=["synthetic"], last_success_at=SUCCESS,
                  last_attempt_at=SUCCESS, stale_after=DEADLINE, round_id="synthetic-round")
    result.update(changes)
    return result


def crossing_calendar():
    # Deliberately not date-sorted; the hidden cheapest record is still fresh.
    return {"route": "PVG-PEK", "dates": {
        TOMORROW: fresh(600), YESTERDAY: fresh(100), TODAY: fresh(500),
    }}


def expected_row(day, price, lowest):
    weekday = {TODAY: "\u5468\u65e5", TOMORROW: "\u5468\u4e00"}[day]
    return dict(date=day, weekday=weekday, min_price=float(price), airline="XX", sample_n=3,
                sources=["synthetic"], observed_at=SUCCESS, last_attempt_at=SUCCESS,
                last_success_at=SUCCESS, stale_after=DEADLINE, round_id="synthetic-round",
                error_type=None, status="success", status_text="",
                eligible_for_recommendation=True, historical_reference=False,
                selected=day == TOMORROW, lowest=lowest, scope="oneway",
                label=f"{day[5:]} {weekday}", value=float(price))


class CalendarLowestScopeTest(unittest.TestCase):
    def setUp(self):
        today = patch.object(calendar, "shanghai_today", return_value=NOW.date())
        now = patch.object(calendar, "_shanghai_now", return_value=NOW)
        self.today_clock = today.start()
        self.now_clock = now.start()
        self.addCleanup(today.stop)
        self.addCleanup(now.stop)

    def assert_crossing(self, function):
        raw = crossing_calendar()
        self.assertTrue(calendar.calendar_record_is_eligible(raw["dates"][YESTERDAY]))
        rows = function(raw, TOMORROW)
        self.assertEqual([r["date"] for r in rows], [TODAY, TOMORROW], "visible_dates")
        self.assertEqual([r["lowest"] for r in rows], [True, False], "hidden_date_set_minimum")
        return rows

    def assert_clock_scope(self, function):
        self.today_clock.reset_mock()
        self.now_clock.reset_mock()
        with patch.object(calendar, "calendar_record_is_eligible",
                          wraps=calendar.calendar_record_is_eligible) as eligible:
            with patch.dict(function.__globals__, calendar_record_is_eligible=eligible):
                rows = function(crossing_calendar(), TOMORROW)
        # One row-normalization per input and one eligibility check per visible row.
        self.assertEqual(eligible.call_count, len(rows), "eligibility_rechecked")
        self.assertEqual(self.now_clock.call_count, 3 + len(rows), "clock_rechecked")
        self.assertEqual(self.today_clock.call_count, 3, "date_clock_rechecked")
        self.assertEqual([r["lowest"] for r in rows], [True, False])

    def assert_mixed(self, function, kind):
        changes = {
            "stale": dict(stale_after="2026-09-20T00:00:00+08:00"),
            "failed": dict(status="failed", error_type="SyntheticFailure"),
            "empty": dict(status="empty"),
            "time_anomaly": dict(stale_after="invalid-deadline"),
        }[kind]
        raw = {"dates": {TODAY: fresh(100, **changes),
                         TOMORROW: fresh(500), "2026-09-22": fresh(600)}}
        rows = function(raw, TOMORROW)
        self.assertEqual([r["date"] for r in rows], [TODAY, TOMORROW, "2026-09-22"])
        self.assertFalse(rows[0]["eligible_for_recommendation"])
        self.assertTrue(rows[1]["eligible_for_recommendation"])
        self.assertTrue(rows[1]["lowest"], f"ineligible_price_in_min:{kind}")
        self.assertFalse(rows[0]["lowest"])
        self.assertFalse(rows[2]["lowest"])

    def assert_ties(self, function):
        rows = function({"dates": {TODAY: fresh(500), TOMORROW: fresh(500)}}, TOMORROW)
        self.assertEqual([r["lowest"] for r in rows], [True, True], "tied_minimum_lost")

    def test_hidden_fresh_date_cannot_set_lowest(self):
        self.assert_crossing(calendar.calendar_rows)

    def test_fixed_clock_has_no_second_qualification(self):
        self.assert_clock_scope(calendar.calendar_rows)

    def test_mixed_eligibility_excludes_cheaper_row(self):
        for kind in ("stale", "failed", "empty", "time_anomaly"):
            with self.subTest(kind=kind):
                self.assert_mixed(calendar.calendar_rows, kind)

    def test_all_visible_lowest(self):
        rows = calendar.calendar_rows({"dates": {TODAY: fresh(500), TOMORROW: fresh(400)}}, TOMORROW)
        self.assertEqual([r["lowest"] for r in rows], [False, True])

    def test_tied_minima(self):
        self.assert_ties(calendar.calendar_rows)

    def test_no_visible_rows(self):
        for raw in ({"dates": {}}, {"dates": {YESTERDAY: fresh(100)}}):
            with self.subTest(hidden=bool(raw["dates"])):
                self.assertEqual(calendar.calendar_rows(raw, TOMORROW), [])

    def test_visible_without_valid_prices(self):
        for price in (None, 0, float("inf"), float("nan")):
            with self.subTest(price=str(price)):
                rows = calendar.calendar_rows({"dates": {TODAY: fresh(price, status="empty")}}, TOMORROW)
                self.assertEqual(len(rows), 1)
                self.assertIsNone(rows[0]["min_price"])
                self.assertFalse(rows[0]["lowest"])

    def test_complete_rows_and_input_unchanged(self):
        raw = crossing_calendar()
        before = copy.deepcopy(raw)
        actual = calendar.calendar_rows(raw, TOMORROW)
        expected = [expected_row(TODAY, 500, True), expected_row(TOMORROW, 600, False)]
        self.assertEqual([{k:v for k,v in r.items() if k != "lowest"} for r in actual],
                         [{k:v for k,v in r.items() if k != "lowest"} for r in expected],
                         "non_lowest_fields_changed")
        self.assertEqual(raw, before)
        self.assertEqual(actual, expected, "complete_rows_marker")

    def test_roundtrip_complete_result(self):
        expected = [expected_row(TODAY, 500, True), expected_row(TOMORROW, 600, False)]
        for row, outbound, combined in zip(expected, (500, 600), (700, 800)):
            row.update(outbound_min_price=float(outbound), return_min_price=200.0,
                       return_date="2026-09-25", min_price=float(combined), value=float(combined),
                       scope="roundtrip", sources=["return", "synthetic"], sample_n=5,
                       observed_at="2026-09-19", observation_window=["2026-09-19", "2026-09-19"],
                       breakdown=f"\u53bb\u00a5{outbound}+\u8fd4\u00a5200")
        actual = calendar.roundtrip_calendar_rows(
            crossing_calendar(), TOMORROW, return_low=200, return_date="2026-09-25",
            return_sources=["return"], return_observed_at=SUCCESS, return_sample_n=2,
        )
        self.assertEqual(actual, expected, "roundtrip_full_result_changed")

    def test_raw_calendar_to_unmodified_renderer(self):
        from notifier import _email_price_calendar_body

        raw = crossing_calendar()
        rows = calendar.calendar_rows(raw, TOMORROW)
        payload = {"price_calendar": {"scope": "oneway", "rows": rows}}
        before = copy.deepcopy(rows)
        body = _email_price_calendar_body(payload)
        self.assertIn("color:#16a34a;font-weight:600;'>\u00a5500(\u5355\u7a0b)</td>",
                      body, "rendered_lowest_style_missing")
        self.assertIn("color:#666;'>\u6700\u4f4e</td>", body, "rendered_lowest_tag_missing")
        self.assertIn("color:#2563eb;font-weight:600;'>\u00a5600(\u5355\u7a0b)</td>", body)
        self.assertNotIn("09-19 ", body)
        self.assertEqual(rows, before)

    def mutated(self, kind):
        tree = ast.parse(textwrap.dedent(inspect.getsource(calendar.calendar_rows)))
        original = ast.dump(tree, include_attributes=False)
        self.assertEqual(len(tree.body), 1, "mutation_function_scope")
        self.assertEqual(tree.body[0].name, "calendar_rows")
        matches = 0

        class Change(ast.NodeTransformer):
            def visit_Assign(self, node):
                nonlocal matches
                if len(node.targets) != 1 or not isinstance(node.targets[0], ast.Name):
                    return self.generic_visit(node)
                name = node.targets[0].id
                if kind == "all_dates" and name == "lowest":
                    matches += 1
                    node.value = ast.parse(
                        'min((float(info["min_price"]) for info in (calendar.get("dates") or {}).values() '
                        'if calendar_record_is_eligible(info)), default=None)', mode="eval").body
                if kind in {"include_ineligible", "reevaluate"} and name == "eligible_rows":
                    self_outer.assertIsInstance(node.value, ast.ListComp)
                    self_outer.assertEqual(ast.unparse(node.value.generators[0].ifs[0]),
                                           "row['eligible_for_recommendation'] and _valid_price(row['min_price'])")
                    matches += 1
                    expression = ('_valid_price(row["min_price"])' if kind == "include_ineligible" else
                                  'calendar_record_is_eligible(row, now=_shanghai_now()) '
                                  'and _valid_price(row["min_price"])')
                    node.value.generators[0].ifs = [ast.parse(expression, mode="eval").body]
                return self.generic_visit(node)

            def visit_For(self, node):
                nonlocal matches
                if kind == "first_tie_only" and ast.unparse(node.iter) == "eligible_rows":
                    matches += 1
                    node.iter = ast.parse("eligible_rows[:1]", mode="eval").body
                return self.generic_visit(node)

        self_outer = self
        tree = Change().visit(tree)
        self.assertEqual(matches, 1, "mutation_exact_match")
        self.assertNotEqual(ast.dump(tree, include_attributes=False), original, "mutation_changed")
        compiled = compile(ast.fix_missing_locations(tree), "<calendar-lowest-mutation>", "exec")
        namespace = dict(calendar.calendar_rows.__globals__)
        exec(compiled, namespace)
        return namespace["calendar_rows"]

    def test_mutation_all_dates_is_rejected(self):
        changed = self.mutated("all_dates")
        with self.assertRaisesRegex(AssertionError, "hidden_date_set_minimum"):
            self.assert_crossing(changed)

    def test_mutation_first_tie_only_is_rejected(self):
        changed = self.mutated("first_tie_only")
        with self.assertRaisesRegex(AssertionError, "tied_minimum_lost"):
            self.assert_ties(changed)

    def test_mutation_ineligible_minimum_is_rejected(self):
        changed = self.mutated("include_ineligible")
        for kind in ("stale", "failed", "empty", "time_anomaly"):
            with self.subTest(kind=kind):
                with self.assertRaisesRegex(AssertionError, f"ineligible_price_in_min:{kind}"):
                    self.assert_mixed(changed, kind)

    def test_mutation_refill_requalification_is_rejected(self):
        changed = self.mutated("reevaluate")
        with self.assertRaisesRegex(AssertionError, "eligibility_rechecked"):
            self.assert_clock_scope(changed)


if __name__ == "__main__":
    unittest.main()
