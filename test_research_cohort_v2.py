import json
import sqlite3
import tempfile
import unittest
from contextlib import closing, redirect_stdout
from datetime import date, timedelta
from io import StringIO
from pathlib import Path
from unittest.mock import patch


class ResearchCohortV2Test(unittest.TestCase):
    def test_six_slots_contain_two_anchors_and_four_probes(self):
        from research_cohort import prepare_research_requests

        state = {}
        schedule = prepare_research_requests(
            state,
            today=date(2026, 8, 26),
            user_monitor_dates=set(),
        )

        self.assertEqual(len(schedule.requests), 6)
        self.assertEqual(
            [item["depart_date"] for item in schedule.requests[:2]],
            ["2026-09-08", "2026-10-01"],
        )
        self.assertEqual(
            [item["depart_date"] for item in schedule.requests[2:]],
            ["2026-09-02", "2026-09-09", "2026-09-23", "2026-10-07"],
        )
        self.assertEqual(
            [item["sample_role"] for item in schedule.requests],
            ["trajectory_anchor"] * 2 + ["cross_sectional_probe"] * 4,
        )
        self.assertTrue(
            all((item["origin"], item["dest"], item["sources"]) == ("PVG", "KIX", ("juhe",)) for item in schedule.requests)
        )

    def test_only_valid_probe_cells_advance_and_five_rotate_target(self):
        from research_cohort import apply_research_round_outcomes, prepare_research_requests

        state = {}
        outcomes = (
            "failed",
            "empty",
            "degraded",
            "missing",
            "valid",
            "valid",
            "valid",
            "valid",
            "valid",
        )
        for day_offset, outcome in enumerate(outcomes):
            today = date(2026, 7, 1) + timedelta(days=day_offset)
            schedule = prepare_research_requests(state, today=today, user_monitor_dates=set())
            probe = next(item for item in schedule.requests if item["slot"] == "probe_1")
            apply_research_round_outcomes(
                state,
                requests=[probe],
                round_id=f"round-{day_offset}",
                today=today,
                db_path=Path("unused.sqlite3"),
                cell_state_loader=lambda **_kwargs: {"state": outcome},
            )

        probe_state = state["research_cohort_v2"]["probes"]["probe_1"]
        self.assertEqual(probe_state["target_index"], 1)
        self.assertEqual(probe_state["target_t"], 10)
        self.assertEqual(probe_state["probe_valid_n"], 0)

    def test_anchor_collects_t_zero_then_completes_without_renewal(self):
        from research_cohort import apply_research_round_outcomes, prepare_research_requests

        state = {}
        schedule = prepare_research_requests(
            state,
            today=date(2026, 9, 8),
            user_monitor_dates=set(),
        )
        anchor = next(item for item in schedule.requests if item["slot"] == "anchor_normal")
        self.assertEqual(anchor["depart_date"], "2026-09-08")

        apply_research_round_outcomes(
            state,
            requests=[anchor],
            round_id="round-t0",
            today=date(2026, 9, 8),
            db_path=Path("unused.sqlite3"),
            cell_state_loader=lambda **_kwargs: {"state": "failed"},
        )
        self.assertEqual(
            state["research_cohort_v2"]["anchors"]["anchor_normal"]["status"],
            "completed",
        )

        later = prepare_research_requests(
            state,
            today=date(2026, 9, 9),
            user_monitor_dates=set(),
        )
        self.assertNotIn("anchor_normal", {item["slot"] for item in later.requests})
        self.assertNotIn("2026-11-08", {item["depart_date"] for item in later.requests})

    def test_probe_collision_prefers_anchor_and_moves_to_next_t(self):
        from research_cohort import prepare_research_requests

        state = {}
        schedule = prepare_research_requests(
            state,
            today=date(2026, 9, 1),
            user_monitor_dates=set(),
        )

        requests_for_anchor_date = [
            item for item in schedule.requests if item["depart_date"] == "2026-09-08"
        ]
        self.assertEqual(len(requests_for_anchor_date), 1)
        self.assertEqual(len(schedule.requests), 6)
        self.assertEqual(requests_for_anchor_date[0]["sample_role"], "trajectory_anchor")
        probe = state["research_cohort_v2"]["probes"]["probe_1"]
        self.assertEqual((probe["target_index"], probe["target_t"]), (1, 10))
        self.assertTrue(
            any(event["kind"] == "deduped_with_anchor" for event in schedule.events)
        )

    def test_probe_collision_with_active_user_monitor_moves_without_extra_request(self):
        from research_cohort import prepare_research_requests

        state = {}
        schedule = prepare_research_requests(
            state,
            today=date(2026, 8, 26),
            user_monitor_dates={"2026-09-02"},
        )

        self.assertNotIn("2026-09-02", {item["depart_date"] for item in schedule.requests})
        self.assertEqual(
            state["research_cohort_v2"]["probes"]["probe_1"]["target_t"],
            10,
        )
        self.assertTrue(
            any(event["kind"] == "deduped_with_user_monitor" for event in schedule.events)
        )

    def test_active_user_dates_include_reverse_roundtrip_return_leg(self):
        from research_cohort import active_user_monitor_dates

        subscriptions = [
            {
                "status": "active",
                "origin_airports_active": ["PVG"],
                "destination_airports_active": ["KIX"],
                "depart_date": "2026-10-01",
                "return_date": "2026-10-06",
                "round_trip": True,
            },
            {
                "status": "active",
                "origin_airports_active": ["KIX"],
                "destination_airports_active": ["PVG"],
                "depart_date": "2026-11-01",
                "return_date": "2026-11-05",
                "round_trip": True,
            },
            {
                "status": "paused",
                "origin_airports_active": ["PVG"],
                "destination_airports_active": ["KIX"],
                "depart_date": "2026-12-01",
            },
            {
                "status": "active",
                "basic": {
                    "origin": "PVG",
                    "destination": "KIX",
                    "depart_date": "2026-12-15",
                    "round_trip": False,
                },
            },
        ]

        self.assertEqual(
            active_user_monitor_dates(subscriptions, origin="PVG", dest="KIX"),
            {"2026-10-01", "2026-11-05", "2026-12-15"},
        )

    def test_research_subscription_loader_accepts_enabled_case_insensitively(self):
        from basket_collect import _load_active_subscriptions_for_research

        subscriptions = [
            {
                "status": "Enabled",
                "origin": "PVG",
                "destination": "KIX",
                "depart_date": "2026-10-01",
            },
            {
                "status": "paused",
                "origin": "PVG",
                "destination": "KIX",
                "depart_date": "2026-10-01",
            },
        ]
        with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as tmp:
            path = Path(tmp) / "subscriptions.json"
            path.write_text(
                json.dumps(subscriptions, ensure_ascii=False),
                encoding="utf-8",
            )

            loaded = _load_active_subscriptions_for_research(
                path,
                today=date(2026, 8, 26),
            )

        self.assertEqual(loaded, [subscriptions[0]])

    def test_quota_simulation_does_not_dedupe_force_fresh_basket_across_processes(self):
        from research_cohort import simulate_research_quota

        result = simulate_research_quota(
            basket_keys={"b1", "shared"},
            subscription_keys={"shared", "s2"},
            other_scheduled_calls=2,
            quota_remaining=122,
            retries_per_request=1,
        )

        self.assertEqual(result["basket_planned_unique"], 2)
        self.assertEqual(result["basket_normal_actual"], 2)
        self.assertEqual(result["basket_retry_ceiling"], 4)
        self.assertEqual(result["subscription_planned_unique"], 2)
        self.assertEqual(result["combined_daily_expected"], 6)
        self.assertEqual(result["combined_daily_worst_case"], 8)
        self.assertEqual(result["estimated_days_remaining"], 20)

    def test_quota_simulation_matches_whole_system_ten_to_sixteen_budget(self):
        from research_cohort import simulate_research_quota

        result = simulate_research_quota(
            basket_keys={f"basket-{index}" for index in range(6)},
            subscription_keys={"subscription-main", "subscription-return"},
            other_scheduled_calls=2,
            quota_remaining=94,
            retries_per_request=1,
        )

        self.assertEqual(result["basket_planned_unique"], 6)
        self.assertEqual(result["basket_retry_ceiling"], 12)
        self.assertEqual(result["subscription_planned_unique"], 2)
        self.assertEqual(result["other_scheduled_calls"], 2)
        self.assertEqual(result["combined_daily_expected"], 10)
        self.assertEqual(result["combined_daily_worst_case"], 16)
        self.assertEqual(result["estimated_days_remaining"], 9)

    def test_hard_gate_requires_all_three_evidence_groups(self):
        from research_cohort import evaluate_research_hard_gates

        blocked = evaluate_research_hard_gates(
            off_disk_copy=False,
            quota_simulation={"complete": True},
            migration_status={"timestamp_ready": True, "lineage_ready": True, "old_data_readable": True},
        )
        ready = evaluate_research_hard_gates(
            off_disk_copy=True,
            quota_simulation={"complete": True},
            migration_status={"timestamp_ready": True, "lineage_ready": True, "old_data_readable": True},
        )

        self.assertFalse(blocked["ready"])
        self.assertIn("off_disk_copy", blocked["missing"])
        self.assertTrue(ready["ready"])
        self.assertEqual(ready["missing"], [])

    def test_runtime_quota_is_incomplete_without_explicit_other_calls(self):
        from basket_collect import _simulate_runtime_quota

        def source_builder(_origin, _dest, route_type=None):
            return [], []

        with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as tmp:
            usage_path = Path(tmp) / "api_usage.json"
            usage_path.write_text(
                '{"version":2,"dates":{},"entries":[]}',
                encoding="utf-8",
            )
            base_settings = {
                "source_quota_budget": {"juhe": 550},
                "freshness_hours": 6,
                "sub_round_fresh_scope": "primary_only",
            }
            incomplete = _simulate_runtime_quota(
                research_requests=[],
                subscriptions=[],
                settings=base_settings,
                source_builder=source_builder,
                usage_path=usage_path,
            )
            complete = _simulate_runtime_quota(
                research_requests=[],
                subscriptions=[],
                settings={
                    **base_settings,
                    "research_cohort_v2_gates": {"other_scheduled_calls": 0},
                },
                source_builder=source_builder,
                usage_path=usage_path,
            )

        self.assertFalse(incomplete["complete"])
        self.assertTrue(complete["complete"])

    def test_migration_inspection_requires_both_schema_sections_and_readable_old_rows(self):
        from research_cohort import inspect_research_migrations

        with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as tmp:
            root = Path(tmp)
            observations = root / "observations.sqlite3"
            prices = root / "prices.db"
            with closing(sqlite3.connect(observations)) as connection, connection:
                connection.executescript(
                    """
                    CREATE TABLE observations (
                      id INTEGER PRIMARY KEY,
                      observed_at_utc TEXT,
                      observed_day_shanghai TEXT,
                      legacy_time_ambiguous INTEGER NOT NULL DEFAULT 0
                    );
                    INSERT INTO observations VALUES (1, '2026-08-25T01:00:00Z', '2026-08-25', 0);
                    CREATE TABLE collection_cells (id INTEGER PRIMARY KEY);
                    """
                )
            with closing(sqlite3.connect(prices)) as connection, connection:
                for table in ("flight_details", "roundtrip_price_history", "push_snapshots"):
                    connection.execute(
                        f"CREATE TABLE {table} (id INTEGER PRIMARY KEY, round_id TEXT)"
                    )

            status = inspect_research_migrations(observations, prices)

        self.assertEqual(
            status,
            {"timestamp_ready": True, "lineage_ready": True, "old_data_readable": True},
        )

    def test_migration_inspection_keeps_timestamp_and_lineage_gates_separate(self):
        from research_cohort import inspect_research_migrations

        with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as tmp:
            root = Path(tmp)
            observations = root / "observations.sqlite3"
            prices = root / "prices.db"
            with closing(sqlite3.connect(observations)) as connection, connection:
                connection.execute(
                    """
                    CREATE TABLE observations (
                      id INTEGER PRIMARY KEY,
                      observed_at_utc TEXT,
                      observed_day_shanghai TEXT,
                      legacy_time_ambiguous INTEGER NOT NULL DEFAULT 0
                    )
                    """
                )
            with closing(sqlite3.connect(prices)) as connection, connection:
                for table in ("flight_details", "roundtrip_price_history", "push_snapshots"):
                    connection.execute(
                        f"CREATE TABLE {table} (id INTEGER PRIMARY KEY, round_id TEXT)"
                    )

            status = inspect_research_migrations(observations, prices)

        self.assertEqual(
            status,
            {"timestamp_ready": True, "lineage_ready": False, "old_data_readable": True},
        )

    def test_config_defaults_switch_off_and_preserves_paused_route_reasons(self):
        from collection_plan import load_collection_settings

        settings = load_collection_settings(Path(__file__).with_name("config.yaml"))

        self.assertFalse(settings["research_cohort_v2"])
        self.assertEqual(
            settings["research_cohort_v2_gates"]["other_scheduled_calls"],
            2,
        )
        self.assertEqual(
            {item["route"] for item in settings["paused_research_routes"]},
            {"SHA->PEK", "PVG->HKG"},
        )
        self.assertTrue(
            all(
                item["reason"] == "quota_concentration_for_pvg_kix_tcurve"
                and item["resume_when"] == "pvg_kix_core_t_points_reach_min_n"
                for item in settings["paused_research_routes"]
            )
        )

    def test_probe_target_sequences_are_frozen(self):
        from research_cohort import PROBE_TARGET_SEQUENCES

        self.assertEqual(
            PROBE_TARGET_SEQUENCES,
            {
                "probe_1": (7, 10, 3, 5),
                "probe_2": (14, 21, 17, 24),
                "probe_3": (28, 35, 49, 63),
                "probe_4": (42, 56, 70, 84),
            },
        )

    def test_enabled_basket_executes_only_six_research_juhe_slots(self):
        from basket_collect import run_basket
        from test_basket_collect import FakeAggregator, FakeSource, fake_source_builder

        settings = {
            "source_quota_budget": {"juhe": 550},
            "source_quota_low_remaining_threshold": 50,
            "freshness_hours": 6,
            "sub_round_fresh_scope": "primary_only",
            "research_cohort_v2": True,
            "research_cohort_v2_gates": {"off_disk_copy": True},
            "paused_research_routes": [
                {"route": "SHA->PEK"},
                {"route": "PVG->HKG"},
            ],
        }
        ready = {
            "timestamp_ready": True,
            "lineage_ready": True,
            "old_data_readable": True,
        }
        FakeSource.calls.clear()
        FakeAggregator.collect_calls.clear()
        with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as tmp:
            root = Path(tmp)
            output = StringIO()
            with (
                patch("basket_collect.load_collection_settings", return_value=settings),
                patch("basket_collect._load_active_subscriptions_for_research", return_value=[]),
                patch("basket_collect.inspect_research_migrations", return_value=ready),
                patch(
                    "basket_collect._simulate_runtime_quota",
                    return_value={"complete": True},
                ),
                patch("basket_collect.count_observations_for_round", return_value=6),
                redirect_stdout(output),
            ):
                summary = run_basket(
                    today=date(2026, 8, 26),
                    state_path=root / "basket_state.json",
                    db_path=root / "observations.sqlite3",
                    usage_path=root / "api_usage.json",
                    source_builder=fake_source_builder,
                    aggregator_factory=FakeAggregator,
                    singleflight_lock_path=root / "collection.lock",
                )
            state = json.loads((root / "basket_state.json").read_text(encoding="utf-8"))

        self.assertEqual((summary["queues"], summary["success"], summary["failed"]), (6, 6, 0))
        self.assertEqual(len(FakeSource.calls), 6)
        self.assertTrue(all(call[0] == "juhe" and call[1:3] == ("PVG", "KIX") for call in FakeSource.calls))
        self.assertIn("research_cohort_v2", state)
        self.assertIn("[研究采样] 已暂停 route=SHA->PEK", output.getvalue())

    def test_enabled_basket_with_missing_gate_makes_no_source_call(self):
        from basket_collect import run_basket
        from test_basket_collect import FakeAggregator, FakeSource, fake_source_builder

        settings = {
            "source_quota_budget": {"juhe": 550},
            "source_quota_low_remaining_threshold": 50,
            "freshness_hours": 6,
            "sub_round_fresh_scope": "primary_only",
            "research_cohort_v2": True,
            "research_cohort_v2_gates": {"off_disk_copy": False},
            "paused_research_routes": [],
        }
        FakeSource.calls.clear()
        with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as tmp:
            root = Path(tmp)
            with (
                patch("basket_collect.load_collection_settings", return_value=settings),
                patch("basket_collect._load_active_subscriptions_for_research", return_value=[]),
                patch(
                    "basket_collect.inspect_research_migrations",
                    return_value={
                        "timestamp_ready": True,
                        "lineage_ready": True,
                        "old_data_readable": True,
                    },
                ),
                patch(
                    "basket_collect._simulate_runtime_quota",
                    return_value={"complete": True},
                ),
                patch("basket_collect.start_request_cache_round") as start_round,
            ):
                summary = run_basket(
                    today=date(2026, 8, 26),
                    state_path=root / "basket_state.json",
                    db_path=root / "observations.sqlite3",
                    usage_path=root / "api_usage.json",
                    source_builder=fake_source_builder,
                    aggregator_factory=FakeAggregator,
                    singleflight_lock_path=root / "collection.lock",
                )

        self.assertEqual(summary["status"], "blocked")
        self.assertEqual(summary["reason"], "research_hard_gate")
        self.assertEqual(FakeSource.calls, [])
        start_round.assert_not_called()


if __name__ == "__main__":
    unittest.main()
