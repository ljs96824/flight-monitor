import copy
import re
import tempfile
import unittest
from contextlib import redirect_stdout
from dataclasses import fields, replace
from io import StringIO
from pathlib import Path
from unittest.mock import patch


class FakeSource:
    def __init__(self, name, flights=None):
        self.name = name
        self.calls = []
        self.flights = list(flights or [])

    def fetch(self, origin, dest, date_str, cabin_class="economy"):
        self.calls.append((origin, dest, date_str, cabin_class))
        return {
            "source": self.name,
            "source_status": "success",
            "flights": [dict(item) for item in self.flights],
        }


class PreflightSkipSource(FakeSource):
    def __init__(self, name, source_status):
        super().__init__(name)
        self.source_status = source_status

    def preflight_skip(self, origin, dest, date_str, cabin_class="economy"):
        return {
            "source": self.name,
            "source_status": self.source_status,
            "flights": [],
            "skipped_reason": "fixture preflight",
        }


class CollectionPlanTest(unittest.TestCase):
    def setUp(self):
        from request_cache import reset_for_tests

        self._cache_tmp = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        self._cache_dir = Path(self._cache_tmp.name) / self._testMethodName
        reset_for_tests(self._cache_dir)
        self.addCleanup(self._cleanup_cache)

    def _cleanup_cache(self):
        from request_cache import reset_for_tests

        reset_for_tests(None)
        self._cache_tmp.cleanup()

    def _execute_plan(self, plan):
        from request_cache import activate_collection_plan, deactivate_collection_plan

        activate_collection_plan(plan.request_keys)
        try:
            return plan.execute()
        finally:
            deactivate_collection_plan()

    def test_request_outcome_result_is_borrowed_hidden_and_ignored_for_equality(self):
        from collection_plan import CollectionPlan, PlanExecutionReport, RequestOutcome

        source = FakeSource(
            "juhe",
            [
                {
                    "flight_combo": "MU1",
                    "price": 800,
                    "booking_url": "https://example.test/?token=secret",
                }
            ],
        )
        plan = CollectionPlan(subscription_count=1)
        plan.add_request(source, "SHA", "PEK", "2026-08-20", persist=False)

        report = self._execute_plan(plan)
        outcome = report.outcomes[0]
        outcome_fields = fields(RequestOutcome)
        result_field = {item.name: item for item in outcome_fields}["result"]

        self.assertEqual(
            tuple(item.name for item in outcome_fields),
            (
                "request_key",
                "source",
                "origin",
                "destination",
                "depart_date",
                "cabin_class",
                "execution_status",
                "cache_status",
                "reuse_kind",
                "skip_reason_code",
                "error_type",
                "error_code",
                "quota_status",
                "raw_result_count",
                "valid_result_count",
                "route_type",
                "cohort_id",
                "sample_role",
                "consumers",
                "groups",
                "reasons",
                "result",
            ),
        )
        self.assertFalse(result_field.repr)
        self.assertFalse(result_field.compare)
        self.assertNotIn("booking_url", repr(outcome))
        self.assertEqual(
            outcome,
            replace(outcome, result={"flights": [], "booking_url": "different"}),
        )
        self.assertIs(outcome.result, plan._results[outcome.request_key])
        self.assertEqual(
            PlanExecutionReport(1, 0, 0, 0, 0, 0).outcomes,
            (),
        )

    def test_classifier_public_alias_and_empty_values_are_preserved_verbatim(self):
        from collection_ledger import (
            _terminal_values,
            classify_collection_result,
        )
        from collection_plan import PlannedRequest, _build_request_outcome

        request = PlannedRequest(
            FakeSource("juhe"),
            "SHA",
            "PEK",
            "2026-08-20",
            route_type="domestic",
        )
        result = {
            "source_status": "success",
            "flights": [],
            "error_code": "",
        }
        classified = classify_collection_result(result, cache_status="")
        outcome = _build_request_outcome(
            request,
            result,
            cache_status="",
        )

        self.assertIs(_terminal_values, classify_collection_result)
        for name in (
            "execution_status",
            "cache_status",
            "reuse_kind",
            "skip_reason_code",
            "error_type",
            "error_code",
            "quota_status",
            "raw_result_count",
            "valid_result_count",
        ):
            with self.subTest(name=name):
                self.assertEqual(getattr(outcome, name), classified[name])
        self.assertEqual(outcome.cache_status, "")

    def test_request_outcome_status_matrix_uses_collection_ledger_classifier(self):
        from collection_plan import PlannedRequest, _build_request_outcome

        request = PlannedRequest(
            FakeSource("juhe"),
            "PVG",
            "KIX",
            "2026-10-01",
            cabin_class="economy",
            route_type="international",
        )
        cases = (
            (
                "success",
                {
                    "source_status": "success",
                    "raw_result_count": 3,
                    "flights": [{"price": 100}, {"price": 0}],
                },
                "fresh",
                None,
                None,
                ("success", None, None, 3, 1),
            ),
            (
                "empty",
                {"source_status": "success", "flights": []},
                "fresh",
                None,
                None,
                ("empty", None, None, 0, 0),
            ),
            (
                "failed",
                {
                    "source_status": "failed",
                    "flights": [],
                    "error_type": "PermissionError",
                    "error_code": 5,
                },
                "fresh",
                None,
                None,
                ("failed", None, None, 0, 0),
            ),
            (
                "quota",
                {"source_status": "skipped_quota_protection", "flights": []},
                "skipped",
                None,
                "quota",
                ("skipped", None, "quota", 0, 0),
            ),
            (
                "preflight",
                {"source_status": "skipped_past_date", "flights": []},
                "skipped",
                None,
                None,
                ("skipped", None, "preflight", 0, 0),
            ),
            (
                "source_disabled",
                {"source_status": "skipped_source_disabled", "flights": []},
                "skipped",
                None,
                None,
                ("skipped", None, "source_disabled", 0, 0),
            ),
            (
                "panel",
                {"source_status": "success", "flights": [{"price": 100}]},
                "panel",
                "panel",
                None,
                ("reused", "panel", None, 1, 1),
            ),
            (
                "persistent_cache",
                {"source_status": "success", "flights": [{"price": 100}]},
                "cache",
                "persistent_cache",
                None,
                ("reused", "persistent_cache", None, 1, 1),
            ),
            (
                "in_round_cache",
                {"source_status": "success", "flights": [{"price": 100}]},
                "cache",
                "in_round_cache",
                None,
                ("reused", "in_round_cache", None, 1, 1),
            ),
        )

        for (
            name,
            result,
            cache_status,
            reuse_kind,
            skip_reason_code,
            expected,
        ) in cases:
            with self.subTest(name=name):
                outcome = _build_request_outcome(
                    request,
                    result,
                    cache_status=cache_status,
                    reuse_kind=reuse_kind,
                    skip_reason_code=skip_reason_code,
                )
                self.assertEqual(
                    (
                        outcome.execution_status,
                        outcome.reuse_kind,
                        outcome.skip_reason_code,
                        outcome.raw_result_count,
                        outcome.valid_result_count,
                    ),
                    expected,
                )
        quota = _build_request_outcome(
            request,
            {"source_status": "skipped_quota_protection", "flights": []},
            cache_status="skipped",
            skip_reason_code="quota",
        )
        self.assertEqual(quota.quota_status, "protected")
        failed = _build_request_outcome(
            request,
            {
                "source_status": "failed",
                "flights": [],
                "error_type": "PermissionError",
                "error_code": 5,
            },
            cache_status="fresh",
        )
        self.assertEqual(failed.error_type, "PermissionError")
        self.assertEqual(failed.error_code, "5")

    def test_execute_emits_all_outcomes_without_changing_legacy_statistics(self):
        from collection_plan import CollectionPlan

        success = FakeSource("success", [{"flight_combo": "MU1", "price": 800}])
        quota = FakeSource("quota", [{"flight_combo": "MU2", "price": 900}])
        empty = FakeSource("empty", [])
        conditional = FakeSource(
            "duffel",
            [{"flight_combo": "MU1", "price": 800}],
        )
        plan = CollectionPlan(subscription_count=1)
        success_request = plan.add_request(
            success,
            "SHA",
            "PEK",
            "2026-08-20",
            persist=False,
        )
        quota_request = plan.add_request(
            quota,
            "SHA",
            "PEK",
            "2026-08-21",
            persist=False,
        )
        empty_request = plan.add_request(
            empty,
            "SHA",
            "PEK",
            "2026-08-22",
            group="empty-group",
            persist=False,
        )
        conditional_request = plan.add_request(
            conditional,
            "SHA",
            "PEK",
            "2026-08-22",
            group="empty-group",
            conditional="search_has_candidates",
            persist=False,
        )
        plan._quota_protected_keys.add(quota_request.key)
        plan._quota_protection_reasons[quota_request.key] = "fixture protected"

        report = self._execute_plan(plan)
        by_key = {outcome.request_key: outcome for outcome in report.outcomes}

        self.assertEqual(
            (
                report.actual_requests,
                report.retries,
                report.cache_hits,
                report.panel_reused,
                report.source_skips,
                report.conditional_skipped,
            ),
            (2, 0, 0, 0, 1, 1),
        )
        self.assertEqual(by_key[success_request.key].execution_status, "success")
        self.assertEqual(by_key[success_request.key].valid_result_count, 1)
        self.assertEqual(by_key[quota_request.key].execution_status, "skipped")
        self.assertEqual(by_key[quota_request.key].skip_reason_code, "quota")
        self.assertEqual(by_key[quota_request.key].quota_status, "protected")
        self.assertEqual(by_key[empty_request.key].execution_status, "empty")
        self.assertEqual(
            by_key[conditional_request.key].execution_status,
            "skipped",
        )
        self.assertEqual(
            by_key[conditional_request.key].skip_reason_code,
            "conditional",
        )
        self.assertEqual(
            plan._results[conditional_request.key]["source_status"],
            "skipped_conditional",
        )
        for outcome in report.outcomes:
            self.assertIs(outcome.result, plan._results[outcome.request_key])
        self.assertEqual(len(success.calls), 1)
        self.assertEqual(len(quota.calls), 0)
        self.assertEqual(len(empty.calls), 1)
        self.assertEqual(len(conditional.calls), 0)

    def test_outcomes_follow_plan_insertion_order_across_two_execution_passes(self):
        from collection_plan import CollectionPlan

        main_a = FakeSource("main-a", [{"flight_combo": "A", "price": 100}])
        conditional_a = FakeSource(
            "conditional-a",
            [{"flight_combo": "A", "price": 100}],
        )
        main_b = FakeSource("main-b", [{"flight_combo": "B", "price": 200}])
        conditional_b = FakeSource(
            "conditional-b",
            [{"flight_combo": "B", "price": 200}],
        )
        plan = CollectionPlan(subscription_count=1)
        plan.add_request(
            main_a,
            "SHA",
            "PEK",
            "2026-08-20",
            group="a",
            persist=False,
        )
        plan.add_request(
            conditional_a,
            "SHA",
            "PEK",
            "2026-08-20",
            group="a",
            conditional="search_has_candidates",
            persist=False,
        )
        plan.add_request(
            main_b,
            "SHA",
            "PEK",
            "2026-08-21",
            group="b",
            persist=False,
        )
        plan.add_request(
            conditional_b,
            "SHA",
            "PEK",
            "2026-08-21",
            group="b",
            conditional="search_has_candidates",
            persist=False,
        )
        expected = [request.key for request in plan._requests.values()]

        report = self._execute_plan(plan)

        self.assertEqual(
            [outcome.request_key for outcome in report.outcomes],
            expected,
        )
        self.assertEqual(
            [source.calls[0][2] for source in (main_a, main_b, conditional_a, conditional_b)],
            ["2026-08-20", "2026-08-21", "2026-08-20", "2026-08-21"],
        )

    def test_outcome_metadata_is_sorted_and_sample_role_upgrade_is_preserved(self):
        from collection_plan import CollectionPlan

        source = FakeSource("juhe", [{"flight_combo": "MU1", "price": 800}])
        plan = CollectionPlan(subscription_count=3)
        plan.add_request(
            source,
            "PVG",
            "KIX",
            "2026-10-01",
            consumer="subscription:z",
            group="group:z",
            reason="reason:z",
            sample_role="cross_sectional_probe",
            persist=False,
        )
        plan.add_request(
            source,
            "PVG",
            "KIX",
            "2026-10-01",
            consumer="subscription:a",
            group="group:a",
            reason="reason:a",
            sample_role="trajectory_anchor",
            persist=False,
        )
        plan.add_request(
            source,
            "PVG",
            "KIX",
            "2026-10-01",
            consumer="research:cohort-1",
            group="group:m",
            reason="reason:m",
            sample_role="user_monitor",
            persist=False,
        )

        outcome = self._execute_plan(plan).outcomes[0]

        self.assertEqual(
            outcome.consumers,
            ("research:cohort-1", "subscription:a", "subscription:z"),
        )
        self.assertEqual(outcome.groups, ("group:a", "group:m", "group:z"))
        self.assertEqual(outcome.reasons, ("reason:a", "reason:m", "reason:z"))
        self.assertEqual(outcome.sample_role, "trajectory_anchor")

    def test_source_preflight_skip_outcome_uses_existing_reason_codes(self):
        from collection_plan import CollectionPlan

        cases = (
            ("skipped_past_date", "preflight"),
            ("skipped_source_disabled", "source_disabled"),
        )
        for index, (source_status, reason_code) in enumerate(cases):
            with self.subTest(source_status=source_status):
                source = PreflightSkipSource(f"preflight-{index}", source_status)
                plan = CollectionPlan(subscription_count=1)
                plan.add_request(
                    source,
                    "SHA",
                    "PEK",
                    f"2026-08-{20 + index:02d}",
                    persist=False,
                )

                report = self._execute_plan(plan)

                self.assertEqual(report.source_skips, 1)
                self.assertEqual(report.outcomes[0].execution_status, "skipped")
                self.assertEqual(
                    report.outcomes[0].skip_reason_code,
                    reason_code,
                )
                self.assertEqual(source.calls, [])

    def test_execute_preserves_all_cache_reuse_kinds_in_outcomes(self):
        from collection_plan import CollectionPlan

        cases = (
            ("panel", "panel"),
            ("cache", "persistent_cache"),
            ("cache", "in_round_cache"),
        )
        for index, (cache_status, reuse_kind) in enumerate(cases):
            with self.subTest(reuse_kind=reuse_kind):
                source = FakeSource(f"reuse-{index}")
                result = {
                    "source": source.name,
                    "source_status": "success",
                    "flights": [{"flight_combo": "MU1", "price": 800}],
                }
                plan = CollectionPlan(subscription_count=1)
                request = plan.add_request(
                    source,
                    "SHA",
                    "PEK",
                    f"2026-09-{20 + index:02d}",
                    persist=False,
                )
                with patch(
                    "collection_plan.cached_fetch",
                    return_value=(result, cache_status, reuse_kind),
                ):
                    report = self._execute_plan(plan)

                outcome = report.outcomes[0]
                self.assertEqual(outcome.execution_status, "reused")
                self.assertEqual(outcome.reuse_kind, reuse_kind)
                self.assertIs(outcome.result, result)
                self.assertIs(outcome.result, plan._results[request.key])

    def test_execute_remains_fail_fast_and_does_not_return_partial_report(self):
        from collection_plan import CollectionPlan

        plan = CollectionPlan(subscription_count=1)
        plan.add_request(
            FakeSource("juhe"),
            "SHA",
            "PEK",
            "2026-08-20",
            persist=False,
        )
        report = None

        with patch(
            "collection_plan.cached_fetch",
            side_effect=RuntimeError("fixture failure"),
        ):
            with self.assertRaisesRegex(RuntimeError, "fixture failure"):
                report = self._execute_plan(plan)

        self.assertIsNone(report)
        self.assertEqual(plan._results, {})

    def test_conditional_skip_finishes_ledger_without_entering_running(self):
        from collection_plan import CollectionPlan

        events = []

        class RecordingLedger:
            degraded = False

            def __init__(self, *, round_id, db_path):
                events.append(("init", round_id, str(db_path)))

            def plan(self, requests):
                events.append(("plan", tuple(request.key for request in requests)))

            def start(self, request):
                events.append(("start", request.key))

            def finish(
                self,
                request,
                result,
                *,
                cache_status,
                reuse_kind=None,
                skip_reason_code=None,
            ):
                events.append(
                    (
                        "finish",
                        request.key,
                        cache_status,
                        reuse_kind,
                        skip_reason_code,
                    )
                )

            def fail_exception(self, request, exc):
                events.append(("fail", request.key, type(exc).__name__))

            def finalize(self):
                events.append(("finalize",))

        search = FakeSource("juhe", [])
        conditional = FakeSource("duffel", [{"flight_combo": "MU1", "price": 1}])
        plan = CollectionPlan(subscription_count=1)
        main_request = plan.add_request(
            search,
            "SHA",
            "PEK",
            "2026-08-20",
            group="main",
            persist=False,
        )
        conditional_request = plan.add_request(
            conditional,
            "SHA",
            "PEK",
            "2026-08-20",
            group="main",
            conditional="search_has_candidates",
            persist=False,
        )

        with (
            patch(
                "collection_ledger.CollectionLedgerSession",
                RecordingLedger,
            ),
            patch(
                "observations_store.get_current_round",
                return_value=("round-ledger", self._cache_dir / "observations.sqlite3"),
            ),
        ):
            report = self._execute_plan(plan)

        started_keys = [event[1] for event in events if event[0] == "start"]
        finished = [event for event in events if event[0] == "finish"]
        self.assertEqual(started_keys, [main_request.key])
        self.assertEqual(
            [event[1] for event in finished],
            [main_request.key, conditional_request.key],
        )
        self.assertEqual(finished[1][2:], ("skipped", None, "conditional"))
        self.assertEqual(report.conditional_skipped, 1)

    def test_three_subscriptions_share_one_real_search_request(self):
        from collection_plan import CollectionPlan
        from request_cache import activate_collection_plan, deactivate_collection_plan

        source = FakeSource("juhe", [{"flight_combo": "MU1", "price": 800}])
        plan = CollectionPlan(subscription_count=3)
        for index, adult_count in enumerate((1, 2, 3)):
            plan.add_request(
                source,
                "SHA",
                "PEK",
                "2026-08-20",
                passengers={"adult": adult_count},
                consumer=f"sub-{index}",
                persist=False,
            )

        self.assertEqual(plan.expanded_total, 3)
        self.assertEqual(plan.unique_count, 1)
        self.assertEqual(plan.reuse_saved, 2)

        activate_collection_plan(plan.request_keys)
        self.addCleanup(deactivate_collection_plan)
        report = plan.execute()

        self.assertEqual(len(source.calls), 1)
        self.assertEqual(report.actual_requests, 1)

    def test_second_round_hits_pool_but_basket_force_fresh_calls_once(self):
        from collection_plan import CollectionPlan
        from request_cache import (
            activate_collection_plan,
            deactivate_collection_plan,
            start_request_cache_round,
        )

        source = FakeSource("juhe", [{"flight_combo": "MU1", "price": 800}])

        start_request_cache_round("round-1")
        first = CollectionPlan(subscription_count=1)
        first.add_request(source, "SHA", "PEK", "2026-08-20", persist=False)
        activate_collection_plan(first.request_keys)
        first_report = first.execute()
        deactivate_collection_plan()

        start_request_cache_round("round-2")
        second = CollectionPlan(subscription_count=1)
        second.add_request(source, "SHA", "PEK", "2026-08-20", persist=False)
        activate_collection_plan(second.request_keys)
        second_report = second.execute()
        deactivate_collection_plan()

        start_request_cache_round("basket-round")
        basket = CollectionPlan(basket_date_count=1)
        basket.add_request(
            source,
            "SHA",
            "PEK",
            "2026-08-20",
            force_fresh=True,
            persist=False,
        )
        activate_collection_plan(basket.request_keys)
        basket_report = basket.execute()
        deactivate_collection_plan()

        self.assertEqual(first_report.actual_requests, 1)
        self.assertEqual(second_report.actual_requests, 0)
        self.assertEqual(second_report.cache_hits, 1)
        self.assertEqual(basket_report.actual_requests, 1)
        self.assertEqual(len(source.calls), 2)

    def test_empty_search_result_does_not_execute_conditional_duffel(self):
        from collection_plan import CollectionPlan
        from request_cache import activate_collection_plan, deactivate_collection_plan

        search = FakeSource("juhe", [])
        duffel = FakeSource("duffel", [{"flight_combo": "MU1", "price": 800}])
        plan = CollectionPlan(subscription_count=1)
        plan.add_request(
            search,
            "SHA",
            "PEK",
            "2026-08-20",
            group="main",
            persist=False,
        )
        plan.add_request(
            duffel,
            "SHA",
            "PEK",
            "2026-08-20",
            group="main",
            conditional="search_has_candidates",
            persist=False,
        )

        activate_collection_plan(plan.request_keys)
        self.addCleanup(deactivate_collection_plan)
        report = plan.execute()

        self.assertEqual(len(search.calls), 1)
        self.assertEqual(len(duffel.calls), 0)
        self.assertEqual(report.conditional_skipped, 1)

    def test_shared_main_request_keeps_all_conditional_dependency_groups(self):
        from collection_plan import CollectionPlan
        from request_cache import activate_collection_plan, deactivate_collection_plan

        source = FakeSource("hasdata", [{"flight_combo": "MU1", "price": 800}])
        plan = CollectionPlan(subscription_count=2)
        plan.add_request(
            source,
            "PVG",
            "KIX",
            "2026-10-01",
            group="sub:0:outbound",
            persist=False,
        )
        plan.add_request(
            source,
            "PVG",
            "KIX",
            "2026-10-01",
            group="sub:1:outbound",
            persist=False,
        )
        plan.add_request(
            source,
            "PVG",
            "KIX",
            "2026-09-30",
            group="sub:1:outbound",
            conditional="search_has_candidates",
            persist=False,
        )

        activate_collection_plan(plan.request_keys)
        self.addCleanup(deactivate_collection_plan)
        report = plan.execute()

        self.assertEqual(
            source.calls,
            [
                ("PVG", "KIX", "2026-10-01", "economy"),
                ("PVG", "KIX", "2026-09-30", "economy"),
            ],
        )
        self.assertEqual(report.conditional_skipped, 0)

    def test_airport_fallback_is_counted_as_one_outside_request(self):
        from collection_plan import CollectionPlan
        from request_cache import (
            activate_collection_plan,
            cached_fetch,
            deactivate_collection_plan,
            get_request_cache_stats,
        )

        source = FakeSource("juhe", [{"flight_combo": "MU1", "price": 800}])
        plan = CollectionPlan(subscription_count=1)
        plan.add_request(source, "SHA", "PEK", "2026-08-20", persist=False)
        activate_collection_plan(plan.request_keys)
        self.addCleanup(deactivate_collection_plan)
        plan.execute()

        output = StringIO()
        with redirect_stdout(output):
            cached_fetch(
                source,
                "PVG",
                "PEK",
                "2026-08-20",
                {"adult": 3},
                "economy",
                persist=False,
                request_reason="机场组合回退",
            )

        stats = get_request_cache_stats()
        self.assertEqual(stats["outside_unique"], 1)
        self.assertEqual(stats["outside_actual"], 1)
        self.assertEqual(stats["actual"], stats["planned_actual"] + stats["outside_actual"])
        self.assertIn("[计划外补充] 源=juhe od=PVG->PEK 日期=2026-08-20 原因=机场组合回退", output.getvalue())

    def test_low_quota_remaining_emits_warning(self):
        from collection_plan import CollectionPlan

        source = FakeSource("juhe", [{"flight_combo": "MU1", "price": 800}])
        plan = CollectionPlan(subscription_count=1)
        plan.add_request(source, "SHA", "PEK", "2026-08-20", persist=False)

        output = StringIO()
        with redirect_stdout(output):
            plan.log_summary(
                quota_budgets={"juhe": 550},
                quota_low_remaining_threshold=50,
                usage_snapshot={"today": {"juhe": 20}, "cumulative": {"juhe": 500}},
            )

        self.assertIn("[采集计划] 唯一请求=1 juhe=1", output.getvalue())
        self.assertIn("余量低于阈值50", output.getvalue())

    def test_purchased_pack_policy_is_not_treated_as_monthly_hard_skip(self):
        from collection_plan import CollectionPlan

        source = FakeSource("juhe", [{"flight_combo": "MU1", "price": 800}])
        plan = CollectionPlan(subscription_count=1)
        plan.add_request(source, "SHA", "PEK", "2026-08-20", persist=False)
        plan.log_summary(
            quota_budgets={
                "juhe": {
                    "kind": "purchased_packs",
                    "packs": [{"id": "pack-a", "added": 550}],
                    "reserve": 500,
                }
            },
            usage_snapshot={"cumulative": {"juhe": 549}},
        )

        self.assertEqual(plan._quota_protected_keys, set())
    def test_api_usage_ledger_accumulates_actual_requests(self):
        from api_usage import (
            initialize_usage_ledger,
            load_usage_strict,
            record_actual_requests,
            usage_snapshot,
        )
        from request_cache import reset_for_tests

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            reset_for_tests(root / "cache")
            try:
                path = root / "api_usage.json"
                initialize_usage_ledger(path)
                record_actual_requests(
                    {"juhe": 2, "hasdata": 1},
                    path=path,
                    day="2026-07-22",
                    round_id="round-a",
                    recorded_at="2026-07-22T10:00:00+08:00",
                )
                record_actual_requests(
                    {"juhe": 1},
                    path=path,
                    day="2026-07-23",
                    round_id="round-b",
                    recorded_at="2026-07-23T10:00:00+08:00",
                )

                raw = load_usage_strict(path)
                snapshot = usage_snapshot(raw, day="2026-07-23")
            finally:
                reset_for_tests(None)

        self.assertEqual(raw["dates"]["2026-07-22"], {"juhe": 2, "hasdata": 1})
        self.assertEqual(snapshot["today"], {"juhe": 1})
        self.assertEqual(snapshot["cumulative"], {"juhe": 3, "hasdata": 1})
        self.assertEqual(
            raw["entries"],
            [
                {
                    "recorded_at": "2026-07-22T10:00:00+08:00",
                    "round_id": "round-a",
                    "day": "2026-07-22",
                    "counts": {"juhe": 2, "hasdata": 1},
                    "workload_class": "unknown",
                    "entrypoint": "unknown",
                },
                {
                    "recorded_at": "2026-07-23T10:00:00+08:00",
                    "round_id": "round-b",
                    "day": "2026-07-23",
                    "counts": {"juhe": 1},
                    "workload_class": "unknown",
                    "entrypoint": "unknown",
                },
            ],
        )

    def test_subscription_builder_expands_roundtrip_and_deduplicates_passengers(self):
        from collection_plan import build_collection_plan

        sources = {
            "juhe": FakeSource("juhe", [{"flight_combo": "MU1", "price": 800}]),
            "duffel": FakeSource("duffel", [{"flight_combo": "MU1", "price": 800}]),
        }

        def source_builder(origin, dest, route_type=None):
            return [sources["juhe"]], [sources["duffel"]]

        subscriptions = []
        for index, adults in enumerate((1, 2, 3)):
            subscriptions.append(
                {
                    "_index": index,
                    "origin": "上海",
                    "destination": "北京",
                    "origin_airports_active": ["SHA"],
                    "destination_airports_active": ["PEK"],
                    "depart_date": "2026-08-20",
                    "return_date": "2026-08-25",
                    "round_trip": True,
                    "route_type": "domestic",
                    "cabin_classes": ["economy"],
                    "preferences": {"passengers": {"adult": adults}},
                }
            )

        plan = build_collection_plan(
            subscriptions=subscriptions,
            basket_requests=[],
            source_builder=source_builder,
            include_calendars=False,
        )

        self.assertEqual(plan.subscription_count, 3)
        self.assertEqual(plan.source_counts, {"juhe": 2, "duffel": 2})
        self.assertEqual(plan.unique_count, 4)
        self.assertEqual(plan.expanded_total, 12)
        self.assertEqual(plan.reuse_saved, 8)

    def test_subscription_consumer_ref_uses_stable_identity_without_mutation(self):
        from collection_plan import _subscription_consumer_ref

        stable = {"subscription_id": "123e4567-e89b-12d3-a456-426614174000"}
        legacy = {"_index": 72}
        legacy_without_index = {"_index": None}
        stable_before = copy.deepcopy(stable)
        legacy_before = copy.deepcopy(legacy)

        self.assertEqual(
            _subscription_consumer_ref(stable, 0),
            "subscription:123e4567-e89b-12d3-a456-426614174000",
        )
        self.assertEqual(
            _subscription_consumer_ref(legacy, 3),
            "subscription-legacy:72",
        )
        self.assertEqual(
            _subscription_consumer_ref(legacy_without_index, 4),
            "subscription-legacy:4",
        )
        self.assertEqual(stable, stable_before)
        self.assertEqual(legacy, legacy_before)
        self.assertNotIn("subscription_id", legacy)

    def test_collection_plan_consumer_refs_are_opaque_stable_and_privacy_safe(self):
        from collection_plan import basket_consumer_ref, build_collection_plan

        source = FakeSource("juhe", [{"flight_combo": "MU1", "price": 800}])

        def source_builder(origin, dest, route_type=None):
            return [source], []

        subscriptions = [
            {
                "subscription_id": "123e4567-e89b-12d3-a456-426614174000",
                "_index": 82,
                "origin_airports_active": ["PVG"],
                "destination_airports_active": ["KIX"],
                "depart_date": "2026-10-01",
                "route_type": "international",
            },
            {
                "_index": 72,
                "origin_airports_active": ["SHA"],
                "destination_airports_active": ["PEK"],
                "depart_date": "2026-09-01",
                "route_type": "domestic",
            },
        ]
        basket_requests = [
            {
                "origin": "PVG",
                "dest": "KIX",
                "depart_date": "2026-10-02",
                "route_type": "international",
                "cohort_id": "pvg-kix.core_1",
                "queue": "PVG->KIX:A",
            },
            {
                "origin": "SHA",
                "dest": "PEK",
                "depart_date": "2026-09-02",
                "route_type": "domestic",
                "queue": "SHA->PEK:A",
            },
        ]

        plan = build_collection_plan(
            subscriptions=subscriptions,
            basket_requests=basket_requests,
            source_builder=source_builder,
            include_calendars=False,
        )
        consumers = tuple(
            sorted(
                consumer
                for request in plan._requests.values()
                for consumer in request.consumers
            )
        )

        self.assertEqual(
            consumers,
            (
                "basket:legacy-1",
                "research:pvg-kix.core_1",
                "subscription-legacy:72",
                "subscription:123e4567-e89b-12d3-a456-426614174000",
            ),
        )
        pattern = re.compile(
            r"^(subscription|subscription-legacy|research|basket):"
            r"[A-Za-z0-9._:-]+$"
        )
        for consumer in consumers:
            with self.subTest(consumer=consumer):
                self.assertRegex(consumer, pattern)
                self.assertNotIn("@", consumer)
                self.assertNotRegex(consumer, r"\s")
                self.assertNotRegex(consumer.lower(), r"email|token|pushplus")
                self.assertNotRegex(consumer, r"(?<!\d)1[3-9]\d{9}(?!\d)")
        self.assertFalse(
            any("PVG->KIX:A" in consumer for consumer in consumers)
        )
        self.assertNotIn("subscription_id", subscriptions[1])

        legacy_requests = [
            {
                "origin": origin,
                "dest": dest,
                "depart_date": f"2026-09-{index + 1:02d}",
                "route_type": route_type,
            }
            for index, (origin, dest, route_type) in enumerate(
                (
                    ("SHA", "PEK", "domestic"),
                    ("SHA", "PEK", "domestic"),
                    ("PVG", "HKG", "greater_china"),
                    ("PVG", "HKG", "greater_china"),
                    ("PVG", "KIX", "international"),
                    ("PVG", "KIX", "international"),
                )
            )
        ]
        normalized = [
            {**item, "_consumer_ref": basket_consumer_ref(item, index)}
            for index, item in enumerate(legacy_requests)
        ]
        grouped = {
            route: [
                item
                for item in normalized
                if f"{item['origin']}->{item['dest']}" == route
            ]
            for route in ("SHA->PEK", "PVG->HKG", "PVG->KIX")
        }
        self.assertEqual(
            [item["_consumer_ref"] for item in normalized],
            [f"basket:legacy-{index}" for index in range(6)],
        )
        self.assertEqual(
            [
                item["_consumer_ref"]
                for route in ("SHA->PEK", "PVG->HKG", "PVG->KIX")
                for item in grouped[route]
            ],
            [f"basket:legacy-{index}" for index in range(6)],
        )

        cohort = {"cohort_id": "pvg-kix.probe-14"}
        self.assertEqual(
            basket_consumer_ref(cohort, 99),
            "research:pvg-kix.probe-14",
        )

        legacy_plan = build_collection_plan(
            basket_requests=normalized,
            source_builder=source_builder,
            include_calendars=False,
        )
        self.assertEqual(
            sorted(
                consumer
                for request in legacy_plan._requests.values()
                for consumer in request.consumers
            ),
            [f"basket:legacy-{index}" for index in range(6)],
        )

    def test_source_builder_failure_does_not_block_other_subscriptions(self):
        from collection_plan import build_collection_plan

        source = FakeSource("juhe", [{"flight_combo": "MU1", "price": 800}])

        def source_builder(origin, dest, route_type=None):
            if origin == "PVG":
                raise RuntimeError("source unavailable")
            return [source], []

        bad = {
            "_index": 1,
            "origin_airports_active": ["PVG"],
            "destination_airports_active": ["KIX"],
            "depart_date": "2026-10-01",
            "route_type": "international",
        }
        good = {
            "_index": 2,
            "origin_airports_active": ["SHA"],
            "destination_airports_active": ["PEK"],
            "depart_date": "2026-08-20",
            "route_type": "domestic",
        }

        output = StringIO()
        with redirect_stdout(output):
            plan = build_collection_plan(
                subscriptions=[bad, good],
                source_builder=source_builder,
                include_calendars=False,
            )

        self.assertEqual(plan.unique_count, 1)
        self.assertIn("[采集计划跳过] 订阅=1", output.getvalue())

    def test_basket_request_upgrades_shared_key_to_force_fresh(self):
        from collection_plan import build_collection_plan

        source = FakeSource("juhe", [{"flight_combo": "MU1", "price": 800}])

        def source_builder(origin, dest, route_type=None):
            return [source], []

        subscription = {
            "_index": 1,
            "origin": "上海",
            "destination": "北京",
            "origin_airports_active": ["SHA"],
            "destination_airports_active": ["PEK"],
            "depart_date": "2026-08-20",
            "round_trip": False,
            "route_type": "domestic",
        }
        basket_request = {
            "origin": "SHA",
            "dest": "PEK",
            "depart_date": "2026-08-20",
            "route_type": "domestic",
            "sources": ("juhe",),
            "queue": "A",
        }

        plan = build_collection_plan(
            subscriptions=[subscription],
            basket_requests=[basket_request],
            source_builder=source_builder,
            include_calendars=False,
        )

        self.assertEqual(plan.unique_count, 1)
        self.assertEqual(plan.basket_date_count, 1)
        request = next(iter(plan._requests.values()))
        self.assertTrue(request.force_fresh)

    def test_cache_key_reuses_same_http_request_across_passenger_mixes(self):
        from request_cache import cached_fetch

        source = FakeSource("hasdata", [{"flight_combo": "MU1", "price": 800}])
        cached_fetch(
            source,
            "PVG",
            "KIX",
            "2026-10-01",
            {"adult": 1},
            "economy",
            persist=False,
        )
        cached_fetch(
            source,
            "PVG",
            "KIX",
            "2026-10-01",
            {"adult": 2, "child": 1, "elderly": 2},
            "economy",
            persist=False,
        )

        self.assertEqual(len(source.calls), 1)


if __name__ == "__main__":
    unittest.main()
