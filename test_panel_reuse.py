import tempfile
import unittest
from datetime import date, datetime, timedelta
from pathlib import Path
from unittest.mock import patch


ANCHOR_TODAY = date(2026, 8, 30)


class PanelSource:
    route_type = "international"

    def __init__(self, name="hasdata"):
        self.name = name
        self.calls = []

    def fetch(self, origin, dest, date_str, cabin_class="economy"):
        self.calls.append((origin, dest, date_str, cabin_class))
        return {
            "source": self.name,
            "source_status": "success",
            "flights": [
                {
                    "flight_combo": "MU225",
                    "flight_no": "MU225",
                    "price": 1000,
                    "departure_airport": origin,
                    "arrival_airport": dest,
                    "departure_time": f"{date_str}T08:00:00",
                    "arrival_time": f"{date_str}T10:00:00",
                    "segments": [
                        {
                            "flight_no": "MU225",
                            "departure_airport": origin,
                            "arrival_airport": dest,
                            "departure_time": f"{date_str}T08:00:00",
                            "arrival_time": f"{date_str}T10:00:00",
                        }
                    ],
                }
            ],
        }


class PanelReuseTest(unittest.TestCase):
    def setUp(self):
        from request_cache import reset_for_tests

        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.cache_dir = self.root / "cache"
        self.db_path = self.root / "observations.sqlite3"
        reset_for_tests(self.cache_dir)
        self.addCleanup(reset_for_tests, None)

    def _seed_fresh_panel_and_cache(self, source, *, date_str="2026-10-01"):
        from observations_store import set_current_round
        from request_cache import cached_fetch, start_request_cache_round

        set_current_round("basket_seed", self.db_path)
        start_request_cache_round("basket_seed")
        cached_fetch(
            source,
            "PVG",
            "KIX",
            date_str,
            {"adult": 1},
            "economy",
            force_fresh=True,
            persist=True,
        )

    def _start_subscription_round(self, source, *, date_str="2026-10-01", panel_only=False):
        from observations_store import set_current_round
        from request_cache import (
            activate_collection_plan,
            cache_key,
            reset_request_cache,
            start_request_cache_round,
        )

        reset_request_cache(clear_memory=True, reset_stats=True)
        set_current_round("subscription_round", self.db_path)
        start_request_cache_round("subscription_round")
        key = cache_key(
            source,
            "PVG",
            "KIX",
            date_str,
            {"adult": 1},
            "economy",
        )
        activate_collection_plan(
            {key},
            panel_only_keys={key} if panel_only else set(),
            freshness_hours=6,
            fresh_scope="primary_only",
        )
        return key

    def test_recent_panel_snapshot_reuses_full_cache_without_request(self):
        from request_cache import cached_fetch, get_request_cache_stats

        source = PanelSource()
        self._seed_fresh_panel_and_cache(source)
        panel_before = self.db_path.read_bytes()
        self._start_subscription_round(source)

        result, status = cached_fetch(
            source,
            "PVG",
            "KIX",
            "2026-10-01",
            {"adult": 2, "child": 1},
            "economy",
            include_cache_status=True,
        )
        stats = get_request_cache_stats()

        self.assertEqual(status, "panel")
        self.assertEqual(len(source.calls), 1)
        self.assertEqual(stats["actual"], 0)
        self.assertEqual(stats["panel_reused"], 1)
        self.assertEqual(stats["by_source"]["hasdata"]["panel_reused"], 1)
        self.assertEqual(result["collection_state"], "panel_reused")
        self.assertEqual(result["flights"][0]["collection_state"], "panel_reused")
        self.assertTrue(result["flights"][0]["collected_at"])
        self.assertEqual(self.db_path.read_bytes(), panel_before)

    def test_plan_predicts_and_accounts_for_panel_reuse(self):
        import collection_plan

        from collection_plan import build_collection_plan
        from observations_store import set_current_round
        from request_cache import (
            activate_collection_plan,
            start_request_cache_round,
        )

        source = PanelSource()
        self._seed_fresh_panel_and_cache(source)
        self._start_subscription_round(source)

        def source_builder(origin, dest, route_type=None):
            return [source], []

        plan = build_collection_plan(
            subscriptions=[
                {
                    "_index": 1,
                    "origin_airports_active": ["PVG"],
                    "destination_airports_active": ["KIX"],
                    "depart_date": "2026-10-01",
                    "route_type": "international",
                }
            ],
            source_builder=source_builder,
            include_calendars=False,
            freshness_hours=6,
            fresh_scope="primary_only",
        )
        logs = []
        original_safe_log = collection_plan.safe_log
        collection_plan.safe_log = logs.append
        self.addCleanup(setattr, collection_plan, "safe_log", original_safe_log)

        set_current_round("subscription_round", self.db_path)
        start_request_cache_round("subscription_round")
        activate_collection_plan(
            plan.request_keys,
            panel_only_keys=plan.panel_only_keys,
            freshness_hours=6,
            fresh_scope="primary_only",
        )
        plan.log_summary()
        report = plan.execute()

        self.assertEqual(report.actual_requests, 0)
        self.assertEqual(report.panel_reused, 1)
        self.assertEqual(report.cache_hits, 0)
        self.assertEqual(report.source_skips, 0)
        self.assertEqual(report.conditional_skipped, 0)
        self.assertEqual(len(source.calls), 1)
        self.assertTrue(any("预计面板复用=1" in line for line in logs))
        self.assertTrue(any("计划恒等式=True" in line for line in logs))

    def test_seven_hour_old_panel_snapshot_falls_back_to_real_fetch(self):
        import sqlite3

        from request_cache import cached_fetch, get_request_cache_stats

        source = PanelSource()
        self._seed_fresh_panel_and_cache(source)
        stale = (datetime.now() - timedelta(hours=7)).isoformat(timespec="seconds")
        connection = sqlite3.connect(self.db_path)
        try:
            connection.execute("UPDATE observations SET observed_at=?", (stale,))
            connection.commit()
        finally:
            connection.close()
        self._start_subscription_round(source)

        _result, status = cached_fetch(
            source,
            "PVG",
            "KIX",
            "2026-10-01",
            {"adult": 1},
            "economy",
            ttl_seconds=0,
            include_cache_status=True,
        )

        self.assertEqual(status, "fresh")
        self.assertEqual(len(source.calls), 2)
        self.assertEqual(get_request_cache_stats()["panel_reused"], 0)

    def test_same_second_observations_only_return_latest_round(self):
        import sqlite3

        from observations_store import load_fresh_observation_snapshot

        source = PanelSource()
        self._seed_fresh_panel_and_cache(source)
        connection = sqlite3.connect(self.db_path)
        try:
            observed_at = connection.execute(
                "SELECT observed_at FROM observations LIMIT 1"
            ).fetchone()[0]
            connection.execute(
                """
                INSERT INTO observations (
                    observed_at, round_id, route_type, origin_airport,
                    dest_airport, depart_date, days_to_departure,
                    cabin_class, source, flight_combo, airline, stops,
                    price_cny, method_version, duration_min
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    observed_at,
                    "basket_second",
                    "international",
                    "PVG",
                    "KIX",
                    "2026-10-01",
                    70,
                    "economy",
                    "hasdata",
                    "MU999",
                    "MU",
                    0,
                    999,
                    "v1",
                    120,
                ),
            )
            connection.commit()
        finally:
            connection.close()

        snapshot = load_fresh_observation_snapshot(
            source="hasdata",
            origin_airport="PVG",
            dest_airport="KIX",
            depart_date="2026-10-01",
            cabin_class="economy",
            freshness_hours=6,
            db_path=self.db_path,
        )

        self.assertEqual(snapshot["round_id"], "basket_second")
        self.assertEqual(
            [row["flight_combo"] for row in snapshot["rows"]],
            ["MU999"],
        )

    def test_basket_force_fresh_never_uses_panel(self):
        from request_cache import cached_fetch, get_request_cache_stats

        source = PanelSource()
        self._seed_fresh_panel_and_cache(source)
        self._start_subscription_round(source)

        _result, status = cached_fetch(
            source,
            "PVG",
            "KIX",
            "2026-10-01",
            {"adult": 1},
            "economy",
            force_fresh=True,
            include_cache_status=True,
        )

        self.assertEqual(status, "fresh")
        self.assertEqual(len(source.calls), 2)
        self.assertEqual(get_request_cache_stats()["panel_reused"], 0)

    def test_primary_only_skips_unobserved_flex_dates_without_request(self):
        from collection_plan import build_collection_plan
        from request_cache import activate_collection_plan, get_request_cache_stats

        source = PanelSource()

        def source_builder(origin, dest, route_type=None):
            return [source], []

        subscription = {
            "_index": 1,
            "origin_airports_active": ["PVG"],
            "destination_airports_active": ["KIX"],
            "depart_date": (ANCHOR_TODAY + timedelta(days=32)).isoformat(),
            "date_flexibility": 1,
            "route_type": "international",
        }
        with patch("collection_plan.shanghai_today", return_value=ANCHOR_TODAY):
            plan = build_collection_plan(
                subscriptions=[subscription],
                source_builder=source_builder,
                include_calendars=False,
                fresh_scope="primary_only",
            )
        flex_requests = [
            request for request in plan._requests.values() if "弹性日期" in request.reasons
        ]
        self.assertEqual(len(flex_requests), 2)
        self.assertTrue(all(request.panel_only for request in flex_requests))

        from observations_store import set_current_round
        from request_cache import start_request_cache_round

        set_current_round("subscription_round", self.db_path)
        start_request_cache_round("subscription_round")
        activate_collection_plan(
            plan.request_keys,
            panel_only_keys=plan.panel_only_keys,
            freshness_hours=6,
            fresh_scope="primary_only",
        )
        report = plan.execute()

        self.assertEqual(len(source.calls), 1)
        self.assertEqual(report.actual_requests, 1)
        self.assertEqual(report.panel_reused, 0)
        self.assertEqual(report.source_skips, 2)
        self.assertEqual(get_request_cache_stats()["actual"], 1)

    def test_all_scope_keeps_flex_date_collection_behavior(self):
        from collection_plan import build_collection_plan
        from observations_store import set_current_round
        from request_cache import activate_collection_plan, start_request_cache_round

        source = PanelSource()

        def source_builder(origin, dest, route_type=None):
            return [source], []

        subscription = {
            "_index": 1,
            "origin_airports_active": ["PVG"],
            "destination_airports_active": ["KIX"],
            "depart_date": (ANCHOR_TODAY + timedelta(days=32)).isoformat(),
            "date_flexibility": 1,
            "route_type": "international",
        }
        with patch("collection_plan.shanghai_today", return_value=ANCHOR_TODAY):
            plan = build_collection_plan(
                subscriptions=[subscription],
                source_builder=source_builder,
                include_calendars=False,
                fresh_scope="all",
            )
        self.assertFalse(any(request.panel_only for request in plan._requests.values()))

        set_current_round("subscription_round", self.db_path)
        start_request_cache_round("subscription_round")
        activate_collection_plan(
            plan.request_keys,
            panel_only_keys=plan.panel_only_keys,
            freshness_hours=6,
            fresh_scope="all",
        )
        report = plan.execute()

        self.assertEqual(report.actual_requests, 3)
        self.assertEqual(len(source.calls), 3)

    def test_email_discloses_panel_reuse_time_and_oldest_header_time(self):
        import notifier

        payload = {
            "source_stats": {
                "hasdata": {"count": 10, "status": "成功", "route_type": "international"}
            },
            "route_type": "international",
            "data_freshness": {
                "legs": [
                    {
                        "direction": "去程",
                        "source": "hasdata",
                        "state": "panel_reused",
                        "collected_at": "2026-07-23T10:35:00+08:00",
                    },
                    {
                        "direction": "返程",
                        "source": "hasdata",
                        "state": "fresh",
                        "collected_at": "2026-07-23T15:30:00+08:00",
                    },
                ]
            },
        }

        source_body = notifier._email_source_body(payload)
        header = notifier._data_freshness_headline(payload)

        self.assertIn("去程:HasData 面板复用10:35", source_body)
        self.assertIn("返程:HasData 实时采集15:30", source_body)
        self.assertEqual(
            header,
            "数据时点:含面板复用，最旧为2026-07-23 10:35",
        )

    def test_unobserved_flex_dates_are_disclosed_without_fake_prices(self):
        import notifier

        payload = {
            "price_calendar": {
                "rows": [],
                "uncollected_rows": [
                    {"date": "2026-09-30", "status": "今日未采"},
                    {"date": "2026-10-02", "status": "今日未采"},
                ],
            },
            "recommended_plans": [],
        }

        email = notifier._email_price_calendar_body(payload)
        push_lines = notifier._pushplus_calendar_summary_lines(payload)
        push_text = "\n".join(push_lines)

        self.assertIn("2026-09-30", email)
        self.assertIn("2026-10-02", email)
        self.assertIn("今日未采", email)
        self.assertIn("09-30、10-02 今日未采", push_text)
        self.assertNotIn("¥", email)
        self.assertNotIn("CNY", email)

    def test_primary_only_evaluates_every_configured_flex_date(self):
        from unittest.mock import patch

        with patch("logging.basicConfig"):
            import main

        class DummyAggregator:
            search_sources = []

        original_collect = main.collect_for_airport_matrix
        main.collect_for_airport_matrix = lambda *args, **kwargs: None
        self.addCleanup(
            setattr,
            main,
            "collect_for_airport_matrix",
            original_collect,
        )
        rows = main.collect_nearby_dates(
            DummyAggregator(),
            {
                "origin": "PVG",
                "destination": "KIX",
                "origin_airports_active": ["PVG"],
                "destination_airports_active": ["KIX"],
                "depart_date": "2026-10-01",
                "date_flexibility": 3,
                "route_type": "international",
            },
            target_min_price=1000,
            fresh_scope="primary_only",
        )

        self.assertEqual(len(rows), 7)
        self.assertEqual(
            sorted(row["offset"] for row in rows),
            [-3, -2, -1, 0, 1, 2, 3],
        )
        self.assertEqual(
            sum(bool(row.get("today_uncollected")) for row in rows),
            6,
        )

    def test_existing_calendar_keeps_nearby_uncollected_markers(self):
        import notifier

        calendar = notifier._payload_price_calendar(
            {
                "origin": "上海",
                "destination": "大阪",
                "price_calendar": {
                    "rows": [
                        {
                            "date": "2026-10-01",
                            "min_price": 1000,
                            "selected": True,
                        }
                    ],
                },
                "nearby_dates": [
                    {
                        "date": "2026-10-02",
                        "min_price": None,
                        "today_uncollected": True,
                    }
                ],
            },
            {},
        )

        self.assertEqual(
            calendar["uncollected_rows"],
            [
                {
                    "date": "2026-10-02",
                    "selected": False,
                    "status": "今日未采",
                    "sources": [],
                    "sample_n": 0,
                    "observed_at": None,
                }
            ],
        )


if __name__ == "__main__":
    unittest.main()
