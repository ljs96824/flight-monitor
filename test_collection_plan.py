import tempfile
import unittest
from contextlib import redirect_stdout
from io import StringIO
from pathlib import Path


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
