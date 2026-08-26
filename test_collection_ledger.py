import sqlite3
import tempfile
import unittest
from contextlib import closing, redirect_stdout
from io import StringIO
from pathlib import Path
from unittest.mock import patch


class FakeListingSource:
    name = "juhe"
    route_type = "international"

    def __init__(self, flights=None, *, status="success", error=None):
        self.flights = list(flights or [])
        self.status = status
        self.error = error
        self.calls = []

    def fetch(self, origin, dest, date_str, cabin_class="economy"):
        self.calls.append((origin, dest, date_str, cabin_class))
        result = {
            "source": self.name,
            "source_status": self.status,
            "flights": [dict(item) for item in self.flights],
        }
        if self.error:
            result["error"] = self.error
        return result


class CollectionLedgerTest(unittest.TestCase):
    def setUp(self):
        from request_cache import reset_for_tests

        self.temp_dir = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        self.root = Path(self.temp_dir.name)
        self.cache_dir = self.root / "cache"
        self.db_path = self.root / "observations.sqlite3"
        reset_for_tests(self.cache_dir)
        self.addCleanup(reset_for_tests, None)
        self.addCleanup(self.temp_dir.cleanup)

    def test_plan_prewrites_and_finishes_one_success_cell(self):
        from collection_plan import CollectionPlan
        from observations_store import reset_current_round, set_current_round
        from request_cache import start_request_cache_round

        source = FakeListingSource(
            [{"flight_combo": "MU225", "price": 4883, "cabin_class": "economy"}]
        )
        tokens = set_current_round("round-ledger", self.db_path)
        self.addCleanup(reset_current_round, tokens)
        start_request_cache_round("round-ledger")
        plan = CollectionPlan(subscription_count=1)
        plan.add_request(
            source,
            "PVG",
            "KIX",
            "2026-10-01",
            persist=False,
            route_type="international",
            sample_role="user_monitor",
            cohort_id="subscription-cohort",
        )

        plan.execute()

        with closing(sqlite3.connect(self.db_path)) as connection:
            row = connection.execute(
                """
                SELECT round_id, execution_status, raw_result_count,
                       valid_result_count, written_count, sample_role,
                       cohort_id, cache_status, method_version
                FROM collection_cells
                """
            ).fetchone()
        self.assertEqual(
            row,
            (
                "round-ledger",
                "success",
                1,
                1,
                1,
                "user_monitor",
                "subscription-cohort",
                "fresh",
                "collection_ledger_v1",
            ),
        )

    def test_five_daily_cell_states_are_total_and_deterministic(self):
        from collection_ledger import derive_daily_cell_state

        success = {
            "source": "juhe",
            "execution_status": "success",
            "valid_result_count": 2,
        }
        self.assertEqual(derive_daily_cell_state([], {"juhe"}), "missing")
        self.assertEqual(
            derive_daily_cell_state(
                [{**success, "valid_result_count": 0}], {"juhe"}
            ),
            "empty",
        )
        self.assertEqual(
            derive_daily_cell_state(
                [
                    {
                        "source": "juhe",
                        "execution_status": "failed",
                        "valid_result_count": 0,
                    }
                ],
                {"juhe"},
            ),
            "failed",
        )
        self.assertEqual(derive_daily_cell_state([success], {"juhe"}), "valid")
        self.assertEqual(
            derive_daily_cell_state([success], {"juhe", "hasdata"}),
            "degraded",
        )

    def test_unfinished_rows_become_interrupted(self):
        from collection_ledger import CollectionLedgerSession
        from collection_plan import PlannedRequest

        source = FakeListingSource([])
        request = PlannedRequest(
            source,
            "PVG",
            "KIX",
            "2026-10-01",
            route_type="international",
        )
        session = CollectionLedgerSession(
            round_id="round-interrupted",
            db_path=self.db_path,
        )
        session.plan([request])
        session.start(request)
        session.finalize()

        with closing(sqlite3.connect(self.db_path)) as connection:
            row = connection.execute(
                "SELECT execution_status, error_type FROM collection_cells"
            ).fetchone()
        self.assertEqual(row, ("interrupted", "ProcessInterrupted"))

    def test_same_round_prefetch_is_recorded_as_in_round_cache_reuse(self):
        from collection_plan import CollectionPlan
        from observations_store import reset_current_round, set_current_round
        from request_cache import cached_fetch, start_request_cache_round

        source = FakeListingSource([{"flight_combo": "MU225", "price": 4883}])
        tokens = set_current_round("round-reuse", self.db_path)
        self.addCleanup(reset_current_round, tokens)
        start_request_cache_round("round-reuse")
        cached_fetch(source, "PVG", "KIX", "2026-10-01", persist=False)
        plan = CollectionPlan(subscription_count=1)
        plan.add_request(
            source,
            "PVG",
            "KIX",
            "2026-10-01",
            persist=False,
            route_type="international",
            sample_role="user_monitor",
        )

        plan.execute()

        with closing(sqlite3.connect(self.db_path)) as connection:
            row = connection.execute(
                "SELECT execution_status, reuse_kind FROM collection_cells"
            ).fetchone()
        self.assertEqual(row, ("reused", "in_round_cache"))
        self.assertEqual(len(source.calls), 1)

    def test_ledger_failure_uses_round_evidence_and_does_not_stop_collection(self):
        from collection_plan import CollectionPlan
        from observations_store import reset_current_round, set_current_round
        from request_cache import start_request_cache_round

        source = FakeListingSource([{"flight_combo": "MU225", "price": 4883}])
        tokens = set_current_round("round-ledger-failed", self.db_path)
        self.addCleanup(reset_current_round, tokens)
        start_request_cache_round("round-ledger-failed")
        plan = CollectionPlan(subscription_count=1)
        plan.add_request(source, "PVG", "KIX", "2026-10-01", persist=False)

        output = StringIO()
        with patch(
            "collection_ledger.init_collection_ledger",
            side_effect=PermissionError("ledger locked"),
        ), patch(
            "collection_ledger.append_round_evidence",
            side_effect=PermissionError("archive locked"),
        ), redirect_stdout(output):
            report = plan.execute()

        self.assertEqual(report.actual_requests, 1)
        self.assertTrue(report.ledger_degraded)
        self.assertEqual(len(source.calls), 1)
        self.assertIn("[采集台账降级]", output.getvalue())
        self.assertIn("[采集台账证据失败]", output.getvalue())

    def test_empty_and_conditional_skip_are_both_preserved(self):
        from collection_plan import CollectionPlan
        from observations_store import reset_current_round, set_current_round
        from request_cache import start_request_cache_round

        search = FakeListingSource([])
        enrichment = FakeListingSource([{"flight_combo": "MU225", "price": 1}])
        enrichment.name = "duffel"
        tokens = set_current_round("round-empty", self.db_path)
        self.addCleanup(reset_current_round, tokens)
        start_request_cache_round("round-empty")
        plan = CollectionPlan(subscription_count=1)
        plan.add_request(
            search,
            "PVG",
            "KIX",
            "2026-10-01",
            group="main",
            persist=False,
            route_type="international",
        )
        plan.add_request(
            enrichment,
            "PVG",
            "KIX",
            "2026-10-01",
            group="main",
            conditional="search_has_candidates",
            persist=False,
            route_type="international",
        )

        plan.execute()

        with closing(sqlite3.connect(self.db_path)) as connection:
            rows = connection.execute(
                """
                SELECT source, execution_status, skip_reason_code
                FROM collection_cells ORDER BY source
                """
            ).fetchall()
        self.assertEqual(
            rows,
            [("duffel", "skipped", "conditional"), ("juhe", "empty", None)],
        )

    def test_daily_state_reader_uses_route_profile_expected_sources(self):
        from collection_ledger import CollectionLedgerSession, load_daily_collection_state
        from collection_plan import PlannedRequest

        source = FakeListingSource([])
        request = PlannedRequest(
            source,
            "PVG",
            "KIX",
            "2026-10-01",
            route_type="international",
        )
        session = CollectionLedgerSession(round_id="round-state", db_path=self.db_path)
        session.plan([request])
        session.start(request)
        session.finish(
            request,
            {"flights": [{"price": 100}], "source_status": "success"},
            cache_status="fresh",
        )
        state = load_daily_collection_state(
            round_id="round-state",
            origin_airport="PVG",
            dest_airport="KIX",
            depart_date="2026-10-01",
            cabin_class="economy",
            route_type="international",
            observed_day_shanghai="2026-08-26",
            db_path=self.db_path,
        )
        self.assertEqual(state["expected_sources"], ["juhe"])
        self.assertEqual(state["state"], "valid")

    def test_quota_response_is_failed_not_a_planned_skip(self):
        from collection_ledger import CollectionLedgerSession
        from collection_plan import PlannedRequest

        source = FakeListingSource([])
        request = PlannedRequest(
            source,
            "PVG",
            "KIX",
            "2026-10-01",
            route_type="international",
        )
        session = CollectionLedgerSession(round_id="round-quota", db_path=self.db_path)
        session.plan([request])
        session.start(request)
        session.finish(
            request,
            {
                "flights": [],
                "source_status": "failed_quota",
                "error": "配额不足",
                "quota_code": "112",
            },
            cache_status="fresh",
        )

        with closing(sqlite3.connect(self.db_path)) as connection:
            row = connection.execute(
                """
                SELECT execution_status, skip_reason_code, quota_status,
                       error_code
                FROM collection_cells
                """
            ).fetchone()
        self.assertEqual(row, ("failed", None, "exhausted", "112"))


if __name__ == "__main__":
    unittest.main()
