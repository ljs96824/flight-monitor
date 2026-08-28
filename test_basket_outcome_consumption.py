import ast
import copy
import inspect
import sqlite3
import tempfile
import unittest
from contextlib import closing
from datetime import datetime
from pathlib import Path
from unittest.mock import patch


DUAL_SOURCE_PROFILE = {
    "sources": [
        {"name": "juhe", "role": "primary", "weight": 1.0},
        {"name": "hasdata", "role": "cross_check", "weight": 0.6},
        {"name": "duffel", "role": "enrichment", "weight": 0.0},
    ],
    "query": {},
}


class OfflineSource:
    def __init__(self, name, result):
        self.name = name
        self.result = copy.deepcopy(result)
        self.calls = []

    def fetch(self, origin, dest, date_str, cabin_class="economy"):
        self.calls.append((origin, dest, date_str, cabin_class))
        return copy.deepcopy(self.result)


class PreflightSkipSource(OfflineSource):
    def preflight_skip(self, origin, dest, date_str, cabin_class="economy"):
        return copy.deepcopy(self.result)


class FixedDateTime(datetime):
    @classmethod
    def now(cls, tz=None):
        value = cls(2026, 8, 28, 9, 30, 0)
        return value.replace(tzinfo=tz) if tz is not None else value


def make_outcome(
    source,
    result,
    *,
    execution_status="success",
    cache_status="fresh",
    reuse_kind=None,
    skip_reason_code=None,
    consumers=("basket:legacy-0",),
    origin="PVG",
    destination="HKG",
    depart_date="2026-10-01",
    cabin_class="economy",
):
    from collection_plan import RequestOutcome
    from request_cache import cache_key

    flights = list((result or {}).get("flights") or [])
    valid = [item for item in flights if float(item.get("price") or 0) > 0]
    return RequestOutcome(
        request_key=cache_key(
            source,
            origin,
            destination,
            depart_date,
            {"adult": 1, "child": 0, "elderly": 0, "infant": 0},
            cabin_class,
        ),
        source=source.name,
        origin=origin,
        destination=destination,
        depart_date=depart_date,
        cabin_class=cabin_class,
        execution_status=execution_status,
        cache_status=cache_status,
        reuse_kind=reuse_kind,
        skip_reason_code=skip_reason_code,
        error_type=(result or {}).get("error_type"),
        error_code=(result or {}).get("error_code"),
        quota_status=None,
        raw_result_count=len(flights),
        valid_result_count=len(valid),
        route_type="greater_china",
        cohort_id=None,
        sample_role="legacy",
        consumers=tuple(consumers),
        groups=("fixture",),
        reasons=("固定篮子",),
        result=result,
    )


class BasketOutcomeConsumptionTest(unittest.TestCase):
    def setUp(self):
        from request_cache import reset_for_tests

        self._cache_tmp = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        reset_for_tests(Path(self._cache_tmp.name) / self._testMethodName)
        self.addCleanup(self._cleanup_cache)

    def _cleanup_cache(self):
        from request_cache import reset_for_tests

        reset_for_tests(None)
        self._cache_tmp.cleanup()

    def test_replay_cache_status_maps_execution_state_without_reclassifying_it(self):
        from sources.aggregator import _aggregator_replay_cache_status

        source = OfflineSource("juhe", {})
        cases = (
            ("success", "fresh", None, "cache"),
            ("reused", "panel", "panel", "cache"),
            ("empty", "fresh", None, "round_empty"),
            ("failed", "fresh", None, "round_failed"),
            ("skipped", "skipped", None, "skipped"),
        )
        for execution_status, cache_status, reuse_kind, expected in cases:
            with self.subTest(execution_status=execution_status):
                outcome = make_outcome(
                    source,
                    {"source_status": "success", "flights": []},
                    execution_status=execution_status,
                    cache_status=cache_status,
                    reuse_kind=reuse_kind,
                )
                self.assertEqual(_aggregator_replay_cache_status(outcome), expected)

        with self.assertRaisesRegex(ValueError, "planned"):
            _aggregator_replay_cache_status(
                make_outcome(
                    source,
                    {"flights": []},
                    execution_status="planned",
                )
            )

    def test_non_terminal_outcome_status_propagates_from_aggregator(self):
        from sources.aggregator import FlightAggregator

        source = OfflineSource(
            "juhe",
            {
                "source_status": "success",
                "flights": [{"flight_combo": "MU225", "price": 1000}],
            },
        )
        outcome = make_outcome(
            source,
            source.result,
            execution_status="running",
        )
        aggregator = FlightAggregator([source], [], route_type="greater_china")

        with patch(
            "sources.aggregator.get_source_profile",
            return_value=DUAL_SOURCE_PROFILE,
        ):
            with self.assertRaisesRegex(ValueError, "running"):
                aggregator.collect_from_outcomes(
                    "PVG",
                    "HKG",
                    "2026-10-01",
                    [outcome],
                    cabin_classes=["economy"],
                    route_type="greater_china",
                    passengers={"adult": 1},
                )

    def test_collect_from_outcomes_requires_exact_key_and_never_fetches(self):
        from sources.aggregator import FlightAggregator, MissingRequestOutcome

        source = OfflineSource("juhe", {"source_status": "success", "flights": []})
        outcome = make_outcome(source, source.result, depart_date="2026-10-02")
        aggregator = FlightAggregator([source], [], route_type="greater_china")

        with (
            patch("sources.aggregator.cached_fetch") as cached_fetch,
            patch("sources.aggregator.get_source_profile", return_value=DUAL_SOURCE_PROFILE),
        ):
            with self.assertRaisesRegex(MissingRequestOutcome, "juhe.*2026-10-01"):
                aggregator.collect_from_outcomes(
                    "PVG",
                    "HKG",
                    "2026-10-01",
                    [outcome],
                    cabin_classes=["economy"],
                    route_type="greater_china",
                    passengers={"adult": 1},
                )

        cached_fetch.assert_not_called()
        self.assertEqual(source.calls, [])
        self.assertEqual(aggregator.last_outcome_reads, 0)

    def test_collect_from_outcomes_rejects_duplicate_request_keys(self):
        from sources.aggregator import FlightAggregator

        source = OfflineSource("juhe", {"source_status": "success", "flights": []})
        outcome = make_outcome(source, source.result)
        aggregator = FlightAggregator([source], [], route_type="greater_china")

        with self.assertRaisesRegex(ValueError, "duplicate|重复"):
            aggregator.collect_from_outcomes(
                "PVG",
                "HKG",
                "2026-10-01",
                [outcome, outcome],
                cabin_classes=["economy"],
                route_type="greater_china",
                passengers={"adult": 1},
            )

    def test_outcome_payload_is_deepcopied_before_aggregator_mutation(self):
        from sources.aggregator import FlightAggregator

        raw_result = {
            "source_status": "success",
            "collected_at": "2026-08-28T08:00:00",
            "flights": [
                {
                    "flight_combo": "MU 225",
                    "flight_no": "MU225",
                    "price": 1000,
                    "segments": [{"flight_no": "MU225"}],
                }
            ],
        }
        source = OfflineSource("juhe", raw_result)
        outcome = make_outcome(source, raw_result)
        borrowed = outcome.result
        before = copy.deepcopy(borrowed)
        aggregator = FlightAggregator([source], [], route_type="greater_china")

        with (
            patch("sources.aggregator.cached_fetch") as cached_fetch,
            patch("sources.aggregator.get_source_profile", return_value=DUAL_SOURCE_PROFILE),
            patch("sources.aggregator.datetime", FixedDateTime),
        ):
            result = aggregator.collect_from_outcomes(
                "PVG",
                "HKG",
                "2026-10-01",
                [outcome],
                cabin_classes=["economy"],
                route_type="greater_china",
                passengers={"adult": 1},
            )

        self.assertIsNotNone(result)
        self.assertEqual(outcome.result, before)
        self.assertIs(outcome.result, borrowed)
        self.assertNotIn("source_collection", outcome.result["flights"][0])
        self.assertIn("source_collection", result["flights"][0])
        cached_fetch.assert_not_called()

    def test_shared_outcome_isolated_between_consumers(self):
        from sources.aggregator import FlightAggregator

        raw_result = {
            "source_status": "success",
            "collected_at": "2026-08-28T08:00:00",
            "flights": [{"flight_combo": "MU225", "price": 1000}],
        }
        source = OfflineSource("juhe", raw_result)
        outcome = make_outcome(
            source,
            raw_result,
            consumers=("basket:legacy-0", "basket:legacy-5"),
        )
        before = copy.deepcopy(outcome.result)

        with (
            patch("sources.aggregator.get_source_profile", return_value=DUAL_SOURCE_PROFILE),
            patch("sources.aggregator.datetime", FixedDateTime),
        ):
            first = FlightAggregator([source], [], route_type="greater_china").collect_from_outcomes(
                "PVG", "HKG", "2026-10-01", [outcome],
                cabin_classes=["economy"], route_type="greater_china", passengers={"adult": 1},
            )
            first["flights"][0]["consumer_mutation"] = True
            second = FlightAggregator([source], [], route_type="greater_china").collect_from_outcomes(
                "PVG", "HKG", "2026-10-01", [outcome],
                cabin_classes=["economy"], route_type="greater_china", passengers={"adult": 1},
            )

        self.assertNotIn("consumer_mutation", second["flights"][0])
        self.assertEqual(outcome.result, before)

    def test_outcomes_mode_never_injects_a_source(self):
        from sources.aggregator import FlightAggregator

        aggregator = FlightAggregator([], [], route_type="domestic")
        with patch("sources.aggregator._instantiate_source") as instantiate:
            result = aggregator.collect_from_outcomes(
                "SHA",
                "PEK",
                "2026-10-01",
                [],
                cabin_classes=["economy"],
                route_type="domestic",
                passengers={"adult": 1},
            )
        self.assertIsNone(result)
        instantiate.assert_not_called()

    def test_old_cache_replay_and_outcome_replay_are_field_for_field_equal(self):
        from collection_plan import CollectionPlan
        from request_cache import (
            activate_collection_plan,
            deactivate_collection_plan,
            get_request_cache_stats,
            start_request_cache_round,
        )
        from observations_store import reset_current_round, set_current_round
        from sources.aggregator import FlightAggregator

        juhe = OfflineSource(
            "juhe",
            {
                "source_status": "success",
                "collected_at": "2026-08-28T08:01:00",
                "raw": {"fixture": "juhe"},
                "flights": [
                    {
                        "flight_combo": "MU 225",
                        "flight_no": "MU225",
                        "price": 1200,
                        "segments": [{"flight_no": "MU225", "aircraft": "A320"}],
                        "booking_options": [
                            {"platform": "OTA", "url": "https://example.test/ota", "price": 1200}
                        ],
                    }
                ],
            },
        )
        hasdata = OfflineSource(
            "hasdata",
            {
                "source_status": "success",
                "collected_at": "2026-08-28T08:02:00",
                "raw": {"fixture": "hasdata"},
                "flights": [
                    {
                        "flight_combo": "MU225",
                        "flight_no": "MU225",
                        "price": 1000,
                        "segments": [{"flight_no": "MU225", "aircraft": "A320neo"}],
                        "booking_options": [
                            {"platform": "Google", "url": "https://example.test/google", "price": 1000}
                        ],
                    }
                ],
            },
        )
        duffel = OfflineSource(
            "duffel",
            {
                "source_status": "success",
                "flights": [
                    {
                        "flight_combo": "MU225",
                        "price": 1000,
                        "extra": {"baggage": "1pc", "changeable": True},
                    }
                ],
            },
        )
        passengers = {"adult": 1, "child": 0, "elderly": 0, "infant": 0}
        plan = CollectionPlan(basket_date_count=1)
        for source in (juhe, hasdata):
            plan.add_request(
                source, "PVG", "HKG", "2026-10-01", passengers,
                group="fixture", consumer="basket:legacy-0", persist=False,
                route_type="greater_china", reason="固定篮子",
            )
        plan.add_request(
            duffel, "PVG", "HKG", "2026-10-01", passengers,
            group="fixture", consumer="basket:legacy-0", persist=False,
            route_type="greater_china", reason="行李退改补充",
            conditional="search_has_candidates",
        )
        start_request_cache_round("basket-equivalence")
        activate_collection_plan(plan.request_keys)
        self.addCleanup(deactivate_collection_plan)
        ledger_tmp = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        self.addCleanup(ledger_tmp.cleanup)
        ledger_path = Path(ledger_tmp.name) / "observations.sqlite3"
        round_tokens = set_current_round("basket-equivalence", ledger_path)
        self.addCleanup(reset_current_round, round_tokens)

        def ledger_rows():
            with closing(sqlite3.connect(ledger_path)) as connection:
                return connection.execute(
                    "SELECT * FROM collection_cells ORDER BY request_fingerprint"
                ).fetchall()

        report = plan.execute()
        borrowed_before = [copy.deepcopy(outcome.result) for outcome in report.outcomes]
        stats_after_execute = get_request_cache_stats()
        ledger_after_execute = ledger_rows()

        with (
            patch("sources.aggregator.get_source_profile", return_value=DUAL_SOURCE_PROFILE),
            patch("sources.aggregator.datetime", FixedDateTime),
        ):
            old_result = FlightAggregator(
                [juhe, hasdata], [duffel], route_type="greater_china"
            ).collect(
                "PVG", "HKG", "2026-10-01",
                cabin_classes=["economy"], route_type="greater_china",
                passengers=passengers, force_fresh=False,
            )
            stats_after_old = get_request_cache_stats()
            ledger_after_old = ledger_rows()
            new_aggregator = FlightAggregator(
                [juhe, hasdata], [duffel], route_type="greater_china"
            )
            new_result = new_aggregator.collect_from_outcomes(
                "PVG", "HKG", "2026-10-01", report.outcomes,
                cabin_classes=["economy"], route_type="greater_china",
                passengers=passengers,
            )
            stats_after_new = get_request_cache_stats()
            ledger_after_new = ledger_rows()

        compared = (
            "flights",
            "source",
            "source_stats",
            "source_errors",
            "dual_source_price_anomalies",
            "price_anomalies",
            "raw_by_source",
            "collection_freshness",
            "request_cache_status",
        )
        for field in compared:
            with self.subTest(field=field):
                self.assertEqual(old_result[field], new_result[field])
        old_flight = old_result["flights"][0]
        new_flight = new_result["flights"][0]
        for field in (
            "price",
            "data_source",
            "price_source",
            "source_price_details",
            "booking_options",
            "extra",
            "has_baggage_info",
        ):
            with self.subTest(flight_field=field):
                self.assertEqual(old_flight[field], new_flight[field])
        self.assertEqual(
            min(item["price"] for item in old_result["flights"]),
            min(item["price"] for item in new_result["flights"]),
        )
        self.assertEqual(stats_after_old["total"] - stats_after_execute["total"], 3)
        self.assertEqual(stats_after_old["hits"] - stats_after_execute["hits"], 3)
        for source_name in ("juhe", "hasdata", "duffel"):
            with self.subTest(cache_stats_source=source_name):
                self.assertEqual(
                    stats_after_old["by_source"][source_name]["calls"]
                    - stats_after_execute["by_source"][source_name]["calls"],
                    1,
                )
                self.assertEqual(
                    stats_after_old["by_source"][source_name]["hits"]
                    - stats_after_execute["by_source"][source_name]["hits"],
                    1,
                )
        self.assertEqual(new_aggregator.last_outcome_reads, 3)
        self.assertEqual(stats_after_new, stats_after_old)
        self.assertEqual(len(ledger_after_execute), 3)
        self.assertEqual(ledger_after_old, ledger_after_execute)
        self.assertEqual(ledger_after_new, ledger_after_execute)
        for index, outcome in enumerate(report.outcomes):
            self.assertEqual(outcome.result, borrowed_before[index])
            self.assertIs(outcome.result, plan._results[outcome.request_key])

    def test_one_source_failure_preserves_existing_aggregator_conclusion(self):
        from sources.aggregator import FlightAggregator

        juhe = OfflineSource(
            "juhe",
            {
                "source_status": "success",
                "collected_at": "2026-08-28T08:01:00",
                "flights": [{"flight_combo": "MU225", "price": 1200}],
            },
        )
        hasdata = OfflineSource(
            "hasdata",
            {
                "source_status": "failed",
                "error": "fixture unavailable",
                "error_type": "TimeoutError",
                "flights": [],
            },
        )
        outcomes = (
            make_outcome(juhe, juhe.result),
            make_outcome(
                hasdata,
                hasdata.result,
                execution_status="failed",
            ),
        )

        def replay_cached_fetch(
            source,
            origin,
            dest,
            date_str,
            passengers,
            cabin_class,
            **kwargs,
        ):
            status = "cache" if source.name == "juhe" else "round_failed"
            value = copy.deepcopy(source.result)
            if kwargs.get("include_cache_status"):
                return value, status
            return value

        with (
            patch("sources.aggregator.cached_fetch", side_effect=replay_cached_fetch) as fetch,
            patch("sources.aggregator.get_source_profile", return_value=DUAL_SOURCE_PROFILE),
            patch("sources.aggregator.datetime", FixedDateTime),
        ):
            old = FlightAggregator(
                [juhe, hasdata], [], route_type="greater_china"
            ).collect(
                "PVG", "HKG", "2026-10-01",
                cabin_classes=["economy"], route_type="greater_china",
                passengers={"adult": 1},
            )
            new = FlightAggregator(
                [juhe, hasdata], [], route_type="greater_china"
            ).collect_from_outcomes(
                "PVG", "HKG", "2026-10-01", outcomes,
                cabin_classes=["economy"], route_type="greater_china",
                passengers={"adult": 1},
            )

        self.assertEqual(fetch.call_count, 2)
        self.assertEqual(old, new)
        self.assertEqual(new["flights"][0]["price"], 1200)
        self.assertEqual(
            new["source_errors"],
            [
                {
                    "source": "hasdata",
                    "cabin_class": "economy",
                    "error": "fixture unavailable",
                    "error_type": "TimeoutError",
                }
            ],
        )

    def test_preflight_skip_keeps_plan_stat_and_drops_only_duplicate_replay_stat(self):
        from collection_plan import CollectionPlan
        from request_cache import (
            activate_collection_plan,
            deactivate_collection_plan,
            get_request_cache_stats,
            start_request_cache_round,
        )
        from sources.aggregator import FlightAggregator

        source = PreflightSkipSource(
            "juhe",
            {
                "source_status": "skipped_past_date",
                "skipped_reason": "fixture preflight",
                "flights": [],
            },
        )
        passengers = {"adult": 1, "child": 0, "elderly": 0, "infant": 0}
        plan = CollectionPlan(basket_date_count=1)
        plan.add_request(
            source,
            "PVG",
            "HKG",
            "2026-10-01",
            passengers,
            group="fixture",
            consumer="basket:legacy-0",
            persist=False,
            route_type="greater_china",
            reason="固定篮子",
        )
        start_request_cache_round("basket-preflight-equivalence")
        activate_collection_plan(plan.request_keys)
        self.addCleanup(deactivate_collection_plan)
        report = plan.execute()
        stats_after_execute = get_request_cache_stats()
        profile = {
            "sources": [{"name": "juhe", "role": "primary", "weight": 1.0}],
            "query": {},
        }

        with patch("sources.aggregator.get_source_profile", return_value=profile):
            old_result = FlightAggregator(
                [source], [], route_type="greater_china"
            ).collect(
                "PVG",
                "HKG",
                "2026-10-01",
                cabin_classes=["economy"],
                route_type="greater_china",
                passengers=passengers,
            )
            stats_after_old = get_request_cache_stats()
            new_aggregator = FlightAggregator(
                [source], [], route_type="greater_china"
            )
            new_result = new_aggregator.collect_from_outcomes(
                "PVG",
                "HKG",
                "2026-10-01",
                report.outcomes,
                cabin_classes=["economy"],
                route_type="greater_china",
                passengers=passengers,
            )
            stats_after_new = get_request_cache_stats()

        self.assertIsNone(old_result)
        self.assertIsNone(new_result)
        self.assertEqual(report.source_skips, 1)
        self.assertEqual(stats_after_old["total"] - stats_after_execute["total"], 1)
        self.assertEqual(stats_after_old["skipped"] - stats_after_execute["skipped"], 1)
        self.assertEqual(stats_after_old["hits"] - stats_after_execute["hits"], 0)
        self.assertEqual(new_aggregator.last_outcome_reads, 1)
        self.assertEqual(stats_after_new, stats_after_old)
        self.assertEqual(source.calls, [])

    def test_legacy_six_queue_cache_stats_drop_by_ten_without_api_drift(self):
        from collection_plan import CollectionPlan
        from request_cache import (
            activate_collection_plan,
            deactivate_collection_plan,
            get_request_cache_stats,
            start_request_cache_round,
        )
        from sources.aggregator import FlightAggregator

        passengers = {"adult": 1, "child": 0, "elderly": 0, "infant": 0}
        route_specs = (
            ("SHA", "PEK", "domestic", "2026-09-01", ("juhe",)),
            ("SHA", "PEK", "domestic", "2026-09-02", ("juhe",)),
            ("PVG", "HKG", "greater_china", "2026-09-03", ("juhe", "hasdata")),
            ("PVG", "HKG", "greater_china", "2026-09-04", ("juhe", "hasdata")),
            ("PVG", "KIX", "international", "2026-09-05", ("hasdata", "juhe")),
            ("PVG", "KIX", "international", "2026-09-06", ("hasdata", "juhe")),
        )
        plan = CollectionPlan(basket_date_count=6)
        queues = []
        all_sources = []
        for index, (origin, dest, route_type, depart_date, source_names) in enumerate(
            route_specs
        ):
            consumer = f"basket:legacy-{index}"
            sources = []
            for source_name in source_names:
                source = OfflineSource(
                    source_name,
                    {
                        "source_status": "success",
                        "collected_at": "2026-08-28T08:00:00",
                        "flights": [
                            {
                                "flight_combo": "TEST1",
                                "price": 100 if source_name == "juhe" else 90,
                            }
                        ],
                    },
                )
                sources.append(source)
                all_sources.append(source)
                plan.add_request(
                    source,
                    origin,
                    dest,
                    depart_date,
                    passengers,
                    group="fixture",
                    consumer=consumer,
                    persist=False,
                    route_type=route_type,
                    reason="固定篮子",
                )
            queues.append(
                (origin, dest, route_type, depart_date, consumer, tuple(sources))
            )

        start_request_cache_round("basket-six-queue-stats")
        activate_collection_plan(plan.request_keys)
        self.addCleanup(deactivate_collection_plan)
        report = plan.execute()
        stats_after_execute = get_request_cache_stats()
        replay_reads = 0
        new_results = []
        old_results = []
        with (
            patch("sources.aggregator.get_source_profile", return_value=DUAL_SOURCE_PROFILE),
            patch("sources.aggregator.datetime", FixedDateTime),
        ):
            for origin, dest, route_type, depart_date, consumer, sources in queues:
                outcomes = tuple(
                    outcome
                    for outcome in report.outcomes
                    if consumer in outcome.consumers
                )
                aggregator = FlightAggregator(list(sources), [], route_type=route_type)
                new_results.append(
                    aggregator.collect_from_outcomes(
                        origin,
                        dest,
                        depart_date,
                        outcomes,
                        cabin_classes=["economy"],
                        route_type=route_type,
                        passengers=passengers,
                    )
                )
                replay_reads += aggregator.last_outcome_reads
            stats_after_new = get_request_cache_stats()

            for origin, dest, route_type, depart_date, _consumer, sources in queues:
                old_results.append(
                    FlightAggregator(list(sources), [], route_type=route_type).collect(
                        origin,
                        dest,
                        depart_date,
                        cabin_classes=["economy"],
                        route_type=route_type,
                        passengers=passengers,
                        force_fresh=False,
                    )
                )
            stats_after_old = get_request_cache_stats()

        self.assertEqual(new_results, old_results)
        self.assertEqual(len(report.outcomes), 10)
        self.assertEqual(sum(len(source.calls) for source in all_sources), 10)
        self.assertEqual(report.actual_requests, 10)
        self.assertEqual(report.retries, 0)
        self.assertEqual(replay_reads, 10)
        self.assertEqual(stats_after_execute["total"], 10)
        self.assertEqual(stats_after_execute["hits"], 0)
        self.assertEqual(stats_after_new, stats_after_execute)
        self.assertEqual(stats_after_old["total"], 20)
        self.assertEqual(stats_after_old["hits"], 10)
        self.assertEqual(stats_after_old["actual"], stats_after_new["actual"])
        self.assertEqual(stats_after_old["retries"], stats_after_new["retries"])
        for source_name, expected_drop in (("juhe", 6), ("hasdata", 4)):
            with self.subTest(source_name=source_name):
                self.assertEqual(
                    stats_after_old["by_source"][source_name]["calls"]
                    - stats_after_new["by_source"][source_name]["calls"],
                    expected_drop,
                )
                self.assertEqual(
                    stats_after_old["by_source"][source_name]["hits"]
                    - stats_after_new["by_source"][source_name]["hits"],
                    expected_drop,
                )

    def test_empty_quota_skip_and_panel_reuse_keep_legacy_meanings(self):
        from sources.aggregator import FlightAggregator

        source = OfflineSource("juhe", {})
        cases = (
            (
                "empty",
                {
                    "source_status": "empty",
                    "flights": [],
                    "collection_state": "fresh",
                },
                "empty",
                None,
                None,
            ),
            (
                "quota",
                {
                    "source_status": "skipped_quota_protection",
                    "flights": [],
                    "collection_state": "quota_protected",
                },
                "skipped",
                None,
                None,
            ),
            (
                "panel",
                {
                    "source_status": "success",
                    "flights": [{"flight_combo": "MU225", "price": 1000}],
                    "collection_state": "panel_reused",
                    "collected_at": "2026-08-28T08:00:00",
                },
                "reused",
                "panel",
                "panel",
            ),
            (
                "persistent_cache",
                {
                    "source_status": "success",
                    "flights": [{"flight_combo": "MU225", "price": 1000}],
                    "collection_state": "cache_reused",
                    "collected_at": "2026-08-28T08:00:00",
                },
                "reused",
                "persistent_cache",
                "cache",
            ),
            (
                "in_round_cache",
                {
                    "source_status": "success",
                    "flights": [{"flight_combo": "MU225", "price": 1000}],
                    "collection_state": "cache_reused",
                    "collected_at": "2026-08-28T08:00:00",
                },
                "reused",
                "in_round_cache",
                "cache",
            ),
        )
        with (
            patch("sources.aggregator.get_source_profile", return_value=DUAL_SOURCE_PROFILE),
            patch("sources.aggregator.datetime", FixedDateTime),
        ):
            for name, raw, status, reuse_kind, expected_cache_status in cases:
                with self.subTest(name=name):
                    outcome = make_outcome(
                        source,
                        raw,
                        execution_status=status,
                        reuse_kind=reuse_kind,
                        skip_reason_code="quota" if name == "quota" else None,
                    )
                    aggregator = FlightAggregator(
                        [source], [], route_type="greater_china"
                    )
                    result = aggregator.collect_from_outcomes(
                        "PVG", "HKG", "2026-10-01", [outcome],
                        cabin_classes=["economy"], route_type="greater_china",
                        passengers={"adult": 1},
                    )
                    if name in {"panel", "persistent_cache", "in_round_cache"}:
                        self.assertIsNotNone(result)
                        self.assertEqual(
                            result["request_cache_status"],
                            expected_cache_status,
                        )
                    else:
                        self.assertIsNone(result)

    def test_conditional_skip_outcome_is_not_treated_as_an_enrichment_flight(self):
        from sources.aggregator import FlightAggregator

        search = OfflineSource("juhe", {})
        enrichment = OfflineSource("duffel", {})
        search_result = {
            "source_status": "success",
            "flights": [{"flight_combo": "MU225", "price": 1000}],
        }
        conditional_result = {
            "source_status": "skipped_conditional",
            "skipped_reason": "无列表候选",
            "flights": [],
        }
        outcomes = (
            make_outcome(search, search_result),
            make_outcome(
                enrichment,
                conditional_result,
                execution_status="skipped",
                cache_status="skipped",
                skip_reason_code="conditional",
            ),
        )

        with (
            patch("sources.aggregator.cached_fetch") as cached_fetch,
            patch("sources.aggregator.get_source_profile", return_value=DUAL_SOURCE_PROFILE),
            patch("sources.aggregator.datetime", FixedDateTime),
        ):
            result = FlightAggregator(
                [search], [enrichment], route_type="greater_china"
            ).collect_from_outcomes(
                "PVG", "HKG", "2026-10-01", outcomes,
                cabin_classes=["economy"], route_type="greater_china",
                passengers={"adult": 1},
            )

        self.assertEqual(len(result["flights"]), 1)
        self.assertEqual(result["source_stats"]["enriched_count"], 0)
        self.assertFalse(result["flights"][0]["has_baggage_info"])
        cached_fetch.assert_not_called()

    def test_basket_runtime_does_not_call_legacy_aggregator_collect(self):
        import basket_collect

        tree = ast.parse(inspect.getsource(basket_collect._run_basket_locked))
        legacy_calls = [
            node
            for node in ast.walk(tree)
            if isinstance(node, ast.Call)
            and isinstance(node.func, ast.Attribute)
            and node.func.attr == "collect"
        ]
        self.assertEqual(legacy_calls, [])
        source = inspect.getsource(basket_collect._run_basket_locked)
        self.assertNotIn("outcome.origin", source)
        self.assertNotIn("outcome.destination", source)
        self.assertNotIn("outcome.depart_date", source)


if __name__ == "__main__":
    unittest.main()
