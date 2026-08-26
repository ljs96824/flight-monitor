import copy
import io
import json
import os
import sqlite3
import sys
import tempfile
import types
import unittest
from contextlib import closing, redirect_stdout
from datetime import date
from pathlib import Path
from unittest.mock import patch


FIXTURE_PATH = (
    Path(__file__).parent
    / "tests"
    / "fixtures"
    / "serpapi_business_response_redacted.json"
)


def _load_fixture():
    return json.loads(FIXTURE_PATH.read_text(encoding="utf-8"))


def _without_volatile_values(value):
    if isinstance(value, dict):
        return {
            key: _without_volatile_values(item)
            for key, item in value.items()
            if key not in {"computed_at"}
        }
    if isinstance(value, list):
        return [_without_volatile_values(item) for item in value]
    return value


class SerpApiProductionAdapterTest(unittest.TestCase):
    def test_redacted_fixture_parses_all_thirteen_offers_and_layovers(self):
        from airlines import classify_segment
        from sources.serpapi_source import parse_google_flights

        fixture = _load_fixture()
        flights = parse_google_flights(
            fixture,
            "serpapi",
            "business",
            "2026-10-01",
            marketing_fallback=True,
            price_note="SerpAPI展示价,税费构成未拆分,以支付页为准",
        )

        self.assertEqual(len(fixture["best_flights"]), 5)
        self.assertEqual(len(fixture["other_flights"]), 8)
        self.assertEqual(len(flights), 13)
        self.assertEqual(flights[0]["airline"], "中国东方航空")
        self.assertEqual(flights[0]["segments"][0]["marketing_airline_code"], "MU")
        self.assertEqual(
            classify_segment(flights[0]["segments"][0])["basis"],
            "marketing_fallback",
        )
        connection = next(item for item in flights if item["flight_nos"] == ["MU 521", "JL 121"])
        self.assertEqual(connection["stops"], 1)
        self.assertEqual(connection["layovers"][0]["airport"], "HND")
        self.assertEqual(connection["layovers"][0]["wait_minutes"], 90)
        lcc = next(item for item in flights if item["flight_nos"] == ["MM 80"])
        lcc_classification = classify_segment(lcc["segments"][0])
        self.assertTrue(lcc_classification["is_lcc"])
        self.assertEqual(lcc_classification["carrier_code"], "MM")
        self.assertEqual(lcc_classification["basis"], "marketing_fallback")
        self.assertTrue(
            all(
                item["price_note"]
                == "SerpAPI展示价,税费构成未拆分,以支付页为准"
                for item in flights
            )
        )

    def test_fetch_uses_travel_class_and_never_selected_cabins(self):
        from sources.serpapi_source import SerpAPISource

        captured = []

        class FakeGoogleSearch:
            def __init__(self, params):
                captured.append(dict(params))

            def get_dict(self):
                return _load_fixture()

        with patch.dict(
            sys.modules,
            {"serpapi": types.SimpleNamespace(GoogleSearch=FakeGoogleSearch)},
        ), patch.dict(os.environ, {"SERPAPI_KEY": "redacted"}, clear=True):
            result = SerpAPISource().fetch("PVG", "KIX", "2026-10-01", "business")

        self.assertEqual(captured[0]["engine"], "google_flights")
        self.assertEqual(captured[0]["type"], "2")
        self.assertEqual(captured[0]["travel_class"], 3)
        self.assertEqual(captured[0]["currency"], "CNY")
        self.assertEqual(captured[0]["hl"], "zh-cn")
        self.assertEqual(captured[0]["gl"], "cn")
        self.assertNotIn("selected_cabins", captured[0])
        self.assertEqual(len(result["flights"]), 13)
        self.assertEqual(result["raw_result_count"], 13)

    def test_missing_key_is_a_graceful_source_skip(self):
        from sources.serpapi_source import SerpAPISource

        with patch.dict(os.environ, {}, clear=True):
            result = SerpAPISource().preflight_skip(
                "PVG",
                "KIX",
                "2026-10-01",
                "business",
            )

        self.assertEqual(result["source_status"], "not_configured")
        self.assertEqual(result["source"], "serpapi")
        self.assertIn("缺少 SerpAPI 密钥", result["skipped_reason"])


class CabinAwareSourcePolicyTest(unittest.TestCase):
    def test_expected_listing_sources_are_derived_per_cabin(self):
        from source_profiles import expected_listing_sources

        for route_type in ("international", "greater_china"):
            with self.subTest(route_type=route_type):
                self.assertEqual(
                    expected_listing_sources(route_type, cabin_class="economy"),
                    {"juhe"},
                )
                self.assertEqual(
                    expected_listing_sources(route_type, cabin_class="business"),
                    {"serpapi"},
                )

    def test_plan_routes_economy_to_juhe_and_business_to_serpapi(self):
        from collection_plan import build_collection_plan

        class FakeSource:
            def __init__(self, name, cabins):
                self.name = name
                self.supported_cabins = frozenset(cabins)

        sources = {
            "juhe": FakeSource("juhe", {"economy"}),
            "serpapi": FakeSource("serpapi", {"business"}),
            "duffel": FakeSource("duffel", {"economy", "business"}),
        }

        def source_builder(_origin, _dest, route_type=None):
            self.assertEqual(route_type, "international")
            return [sources["juhe"], sources["serpapi"]], [sources["duffel"]]

        plan = build_collection_plan(
            subscriptions=[
                {
                    "_index": 1,
                    "origin_airports_active": ["PVG"],
                    "destination_airports_active": ["KIX"],
                    "depart_date": "2026-10-01",
                    "route_type": "international",
                    "cabin_classes": ["economy", "business"],
                }
            ],
            basket_requests=[],
            source_builder=source_builder,
            include_calendars=False,
        )

        requests = {
            (key[0], key[5])
            for key in plan.request_keys
        }
        self.assertEqual(
            requests,
            {
                ("juhe", "economy"),
                ("serpapi", "business"),
                ("duffel", "economy"),
                ("duffel", "business"),
            },
        )


class SerpApiPanelReuseTest(unittest.TestCase):
    def setUp(self):
        from request_cache import reset_for_tests

        self._tmp = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        self.root = Path(self._tmp.name)
        self.cache_dir = self.root / "cache"
        self.db_path = self.root / "observations.sqlite3"
        reset_for_tests(self.cache_dir)
        self.addCleanup(self._cleanup)

    def _cleanup(self):
        from request_cache import reset_for_tests

        reset_for_tests(None)
        self._tmp.cleanup()

    def test_business_result_is_stored_and_reused_from_panel_by_cabin(self):
        from observations_store import set_current_round
        from request_cache import (
            activate_collection_plan,
            cache_key,
            cached_fetch,
            get_request_cache_stats,
            reset_request_cache,
            start_request_cache_round,
        )

        class Source:
            name = "serpapi"
            route_type = "international"

            def __init__(self):
                self.calls = 0

            def fetch(self, origin, dest, date_str, cabin_class="economy"):
                self.calls += 1
                return {
                    "source": self.name,
                    "source_status": "success",
                    "flights": [
                        {
                            "flight_combo": "MU225",
                            "flight_no": "MU225",
                            "price": 9000,
                            "cabin_class": cabin_class,
                            "departure_airport": origin,
                            "arrival_airport": dest,
                            "departure_time": f"{date_str}T09:00:00",
                            "arrival_time": f"{date_str}T12:00:00",
                        }
                    ],
                }

        source = Source()
        set_current_round("serpapi-seed", self.db_path)
        start_request_cache_round("serpapi-seed")
        cached_fetch(
            source,
            "PVG",
            "KIX",
            "2026-10-01",
            cabin_class="business",
            force_fresh=True,
            persist=True,
        )

        with closing(sqlite3.connect(self.db_path)) as connection:
            stored = connection.execute(
                "SELECT source, cabin_class, COUNT(*) FROM observations GROUP BY source, cabin_class"
            ).fetchone()
        self.assertEqual(stored, ("serpapi", "business", 1))

        reset_request_cache(clear_memory=True, reset_stats=True)
        set_current_round("serpapi-sub", self.db_path)
        start_request_cache_round("serpapi-sub")
        key = cache_key(source, "PVG", "KIX", "2026-10-01", None, "business")
        activate_collection_plan({key}, freshness_hours=6, fresh_scope="primary_only")
        result, status = cached_fetch(
            source,
            "PVG",
            "KIX",
            "2026-10-01",
            cabin_class="business",
            include_cache_status=True,
        )

        self.assertEqual(status, "panel")
        self.assertEqual(source.calls, 1)
        self.assertEqual(result["flights"][0]["cabin_class"], "business")
        self.assertEqual(get_request_cache_stats()["panel_reused"], 1)


class SerpApiMonthlyQuotaTest(unittest.TestCase):
    def setUp(self):
        from request_cache import reset_for_tests

        self._tmp = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        reset_for_tests(Path(self._tmp.name) / "cache")
        self.addCleanup(self._cleanup)

    def _cleanup(self):
        from request_cache import reset_for_tests

        reset_for_tests(None)
        self._tmp.cleanup()

    def _plan(self):
        from collection_plan import CollectionPlan

        class FakeSource:
            name = "serpapi"

            def __init__(self):
                self.calls = []

            def fetch(self, origin, dest, date_str, cabin_class="economy"):
                self.calls.append((origin, dest, date_str, cabin_class))
                return {
                    "source": self.name,
                    "source_status": "success",
                    "flights": [{"flight_combo": "MU225", "price": 9000}],
                }

        source = FakeSource()
        plan = CollectionPlan(subscription_count=1)
        plan.add_request(
            source,
            "PVG",
            "KIX",
            "2026-10-01",
            cabin_class="business",
            persist=False,
        )
        return source, plan

    def test_monthly_budget_allows_request_at_reserve_boundary(self):
        source, plan = self._plan()
        plan.log_summary(
            quota_budgets={"serpapi": {"monthly": 250, "reserve": 30}},
            usage_snapshot={"today": {}, "month": {"serpapi": 219}, "cumulative": {}},
        )

        report = plan.execute()

        self.assertEqual(len(source.calls), 1)
        self.assertEqual(report.actual_requests, 1)

    def test_monthly_budget_protects_all_planned_requests_above_reserve(self):
        source, plan = self._plan()
        output = io.StringIO()
        with redirect_stdout(output):
            plan.log_summary(
                quota_budgets={"serpapi": {"monthly": 250, "reserve": 30}},
                usage_snapshot={"today": {}, "month": {"serpapi": 220}, "cumulative": {}},
            )
            report = plan.execute()

        self.assertEqual(source.calls, [])
        self.assertEqual(report.actual_requests, 0)
        self.assertEqual(report.source_skips, 1)
        self.assertIn("[配额保护] 源=serpapi", output.getvalue())
        self.assertIn("本月已用=220", output.getvalue())

    def test_usage_snapshot_uses_calendar_month(self):
        from api_usage import usage_snapshot

        snapshot = usage_snapshot(
            {
                "dates": {
                    "2026-08-31": {"serpapi": 7},
                    "2026-09-01": {"serpapi": 2},
                    "2026-09-15": {"serpapi": 3},
                }
            },
            day="2026-09-15",
        )

        self.assertEqual(snapshot["today"]["serpapi"], 3)
        self.assertEqual(snapshot["month"]["serpapi"], 5)
        self.assertEqual(snapshot["cumulative"]["serpapi"], 12)


class EconomyAnalyticsIsolationTest(unittest.TestCase):
    def setUp(self):
        from observations_store import init_observations_db

        self._tmp = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        self.db_path = Path(self._tmp.name) / "observations.sqlite3"
        init_observations_db(self.db_path)
        self.addCleanup(self._tmp.cleanup)
        for offset, observed_day in enumerate(
            ("2026-08-20", "2026-08-21", "2026-08-22", "2026-08-23", "2026-08-24")
        ):
            self._append(
                cabin_class="economy",
                source="juhe",
                observed_day=observed_day,
                round_id=f"economy-{offset}",
                combo=f"MU{225 + offset}",
                price=1000 + offset * 20,
            )

    def _append(self, *, cabin_class, source, observed_day, round_id, combo, price):
        from observations_store import append_observations

        append_observations(
            [
                {
                    "flight_combo": combo,
                    "price": price,
                    "cabin_class": cabin_class,
                    "airline": "MU",
                    "stops": 0,
                    "duration_min": 180,
                }
            ],
            round_id=round_id,
            route_type="international",
            origin_airport="PVG",
            dest_airport="KIX",
            depart_date="2026-09-20",
            cabin_class=cabin_class,
            source=source,
            observed_at=f"{observed_day}T09:00:00",
            db_path=self.db_path,
        )

    def _analytics(self):
        from analytics.report_lib import load_observations as load_report_observations
        from forecast import build_notification_forecast
        from patterns import build_route_patterns
        from provenance import build_panel_report_payload, load_route_observations
        from tcurve import build_tcurve

        route_info = {
            "origin_airports_active": ["PVG"],
            "destination_airports_active": ["KIX"],
            "depart_date": "2026-09-20",
        }
        return _without_volatile_values(
            {
                "rows": load_route_observations(self.db_path, route="上海-大阪"),
                "report_rows": load_report_observations(self.db_path),
                "reference_calendar_signal": build_panel_report_payload(
                    self.db_path,
                    route="上海-大阪",
                    as_of_date=date(2026, 8, 24),
                    min_pairs=1,
                    min_tcurve_sample=1,
                ),
                "tcurve": build_tcurve(
                    self.db_path,
                    route="上海-大阪",
                    min_sample=1,
                    include_degraded=True,
                ),
                "forecast": build_notification_forecast(
                    route_info,
                    db_path=self.db_path,
                    as_of_day="2026-08-24",
                ),
                "patterns": build_route_patterns(
                    self.db_path,
                    route="上海-大阪",
                    min_n=1,
                ),
            }
        )

    def test_business_rows_do_not_change_any_existing_economy_analysis(self):
        before = self._analytics()
        for offset, observed_day in enumerate(
            ("2026-08-20", "2026-08-21", "2026-08-22", "2026-08-23", "2026-08-24")
        ):
            self._append(
                cabin_class="business",
                source="serpapi",
                observed_day=observed_day,
                round_id=f"business-{offset}",
                combo=f"JL{80 + offset}",
                price=100 + offset,
            )
        after = self._analytics()

        self.assertEqual(after, before)
        self.assertTrue(
            all(row["cabin_class"] == "economy" for row in after["rows"])
        )


if __name__ == "__main__":
    unittest.main()
