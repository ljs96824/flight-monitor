import os
import sys
import types
import unittest
from datetime import date
from unittest.mock import patch


class HasDataRetirementTest(unittest.TestCase):
    def test_basket_ignores_retired_source_in_legacy_route_allowlist(self):
        from basket_collect import _build_route_aggregator

        class Source:
            name = "juhe"

        captured = {}

        def source_builder(origin, dest, route_type=None):
            return [Source()], []

        def aggregator_factory(search_sources, enrichment_sources, route_type=None):
            captured["search_sources"] = [source.name for source in search_sources]
            captured["enrichment_sources"] = enrichment_sources
            captured["route_type"] = route_type
            return object()

        _build_route_aggregator(
            {
                "origin": "PVG",
                "dest": "KIX",
                "route_type": "international",
                "sources": ("hasdata", "juhe"),
            },
            source_builder,
            aggregator_factory,
        )

        self.assertEqual(captured["search_sources"], ["juhe"])
        self.assertEqual(captured["enrichment_sources"], [])
        self.assertEqual(captured["route_type"], "international")

    def test_international_profile_retires_hasdata_but_keeps_metadata(self):
        from source_profiles import get_source_profile

        profile = get_source_profile("international")

        self.assertEqual(
            [item["name"] for item in profile["sources"]],
            ["juhe", "duffel"],
        )
        self.assertEqual(profile["sources"][0]["role"], "primary")
        self.assertEqual(
            profile["retired_sources"],
            [
                {
                    "name": "hasdata",
                    "role": "primary",
                    "weight": 1.0,
                    "retired_on": "2026-08-14",
                    "reason": "403/订阅终止",
                }
            ],
        )

    def test_expected_sources_are_date_aware_across_retirement(self):
        from source_profiles import expected_listing_sources

        self.assertEqual(
            expected_listing_sources("international", observed_day="2026-08-13"),
            {"hasdata", "juhe"},
        )
        self.assertEqual(
            expected_listing_sources("international", observed_day="2026-08-14"),
            {"juhe"},
        )
        self.assertEqual(expected_listing_sources("international"), {"juhe"})

    def test_collection_plan_for_international_route_has_no_hasdata_request(self):
        from collection_plan import build_collection_plan
        from sources.aggregator import build_default_sources

        env = {
            "JUHE_FLIGHT_KEY": "juhe-key",
            "HASDATA_KEY": "hasdata-key",
            "DUFFEL_TOKEN": "duffel-token",
        }
        fake_modules = {
            "serpapi": types.SimpleNamespace(GoogleSearch=object),
            "httpx": types.SimpleNamespace(get=lambda *a, **k: None, post=lambda *a, **k: None),
        }
        with patch.dict(sys.modules, fake_modules):
            with patch.dict(os.environ, env, clear=True):
                plan = build_collection_plan(
                    subscriptions=[
                        {
                            "_index": 72,
                            "origin_airports_active": ["PVG"],
                            "destination_airports_active": ["KIX"],
                            "depart_date": "2026-10-01",
                            "return_date": "2026-10-06",
                            "round_trip": True,
                            "route_type": "international",
                        }
                    ],
                    source_builder=build_default_sources,
                    include_calendars=False,
                )

        self.assertNotIn("hasdata", plan.source_counts)
        self.assertEqual(plan.source_counts, {"juhe": 2, "duffel": 2})

    def test_retirement_notice_appears_once_and_is_not_a_fault(self):
        from notifier import (
            _build_source_degradation_context,
            _build_source_retirement_context,
            _email_source_body,
        )

        first = _build_source_retirement_context(
            "international",
            {"source_set": ["hasdata", "juhe"]},
        )
        second = _build_source_retirement_context(
            "international",
            {"source_set": ["juhe"]},
        )
        self.assertTrue(first["active"])
        self.assertTrue(first["first_occurrence"])
        self.assertIn("Google源(HasData)已于2026-08-14停用", first["notice"])
        self.assertTrue(second["active"])
        self.assertFalse(second["first_occurrence"])
        self.assertEqual(second["notice"], "")

        degradation = _build_source_degradation_context(
            source_stats={"hasdata": {"status": "empty", "count": 0}},
            last_snapshot={"source_set": ["hasdata", "juhe"]},
            source_errors=[],
            retired_sources={"hasdata"},
        )
        self.assertFalse(degradation["active"])

        first_body = _email_source_body(
            {
                "route_type": "international",
                "source_stats": {
                    "juhe": {"count": 18, "status": "成功"},
                    "duffel": {"count": 2, "status": "成功"},
                },
                "source_retirement": first,
            }
        )
        second_body = _email_source_body(
            {
                "route_type": "international",
                "source_stats": {"juhe": {"count": 18, "status": "成功"}},
                "source_retirement": second,
            }
        )
        self.assertIn("主源:聚合数据(OTA)—18个方案", first_body)
        self.assertIn(first["notice"], first_body)
        self.assertNotIn("本轮Google数据源不可用", first_body + second_body)
        self.assertNotIn(first["notice"], second_body)

    def test_tcurve_keeps_pre_retirement_marker_and_accepts_new_juhe_only_cell(self):
        from tcurve import fold_tcurve_daily_cells

        rows = [
            {
                "observed_at": "2026-08-13T09:00:00+08:00",
                "route_type": "international",
                "origin_airport": "PVG",
                "dest_airport": "KIX",
                "depart_date": "2026-10-01",
                "days_to_departure": 49,
                "source": "juhe",
                "price_cny": 900,
            },
            {
                "observed_at": "2026-08-14T09:00:00+08:00",
                "route_type": "international",
                "origin_airport": "PVG",
                "dest_airport": "KIX",
                "depart_date": "2026-10-01",
                "days_to_departure": 48,
                "source": "juhe",
                "price_cny": 880,
            },
        ]

        cells = fold_tcurve_daily_cells(rows)

        self.assertTrue(cells[0]["degraded"])
        self.assertEqual(cells[0]["expected_sources"], ["hasdata", "juhe"])
        self.assertFalse(cells[1]["degraded"])
        self.assertEqual(cells[1]["expected_sources"], ["juhe"])

    def test_dual_source_agreement_naturally_decays_out_of_window(self):
        from provenance import _agreement_from_rows

        rows = [
            {
                "observed_at": "2026-08-13T09:00:00+08:00",
                "origin_airport": "PVG",
                "dest_airport": "KIX",
                "depart_date": "2026-10-01",
                "flight_combo": "MU225",
                "cabin_class": "economy",
                "source": "hasdata",
                "price_cny": 1000,
            },
            {
                "observed_at": "2026-08-13T09:00:00+08:00",
                "origin_airport": "PVG",
                "dest_airport": "KIX",
                "depart_date": "2026-10-01",
                "flight_combo": "MU225",
                "cabin_class": "economy",
                "source": "juhe",
                "price_cny": 900,
            },
        ]

        result = _agreement_from_rows(
            rows,
            start=date(2026, 8, 14),
            end=date(2026, 9, 12),
            min_pairs=1,
            computed_at="2026-09-12T10:00:00+08:00",
        )

        self.assertEqual(result["sample_n"], 0)
        self.assertEqual(result["status"], "insufficient")


if __name__ == "__main__":
    unittest.main()
