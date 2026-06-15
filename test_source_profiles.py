import os
import sys
import types
import unittest
from unittest.mock import patch

from sources.aggregator import FlightAggregator, build_default_sources, classify_route


class DummySource:
    def __init__(self, name):
        self.name = name


class SourceProfilesTest(unittest.TestCase):
    def setUp(self):
        self.env = {
            "JUHE_FLIGHT_KEY": "juhe-key",
            "SERPAPI_KEY": "serpapi-key",
            "HASDATA_KEY": "hasdata-key",
            "SEARCHAPI_KEY": "searchapi-key",
            "TRAVELPAYOUTS_TOKEN": "tp-token",
            "RAPIDAPI_KEY": "rapid-key",
            "DUFFEL_TOKEN": "duffel-token",
        }
        self.fake_modules = {
            "serpapi": types.SimpleNamespace(GoogleSearch=object),
            "httpx": types.SimpleNamespace(get=lambda *a, **k: None, post=lambda *a, **k: None),
        }

    def test_classify_route_distinguishes_domestic_greater_china_and_international(self):
        self.assertEqual(classify_route("PVG", "PEK"), "domestic")
        self.assertEqual(classify_route("PVG", "HKG"), "greater_china")
        self.assertEqual(classify_route("TPE", "HKG"), "greater_china")
        self.assertEqual(classify_route("PVG", "KIX"), "international")

    def test_domestic_profile_uses_juhe_google_cross_check_and_duffel_only(self):
        with patch.dict(sys.modules, self.fake_modules):
            with patch.dict(os.environ, self.env, clear=True):
                search_sources, enrichment_sources = build_default_sources(
                    "PVG", "PEK", route_type="domestic"
                )

        self.assertEqual([source.name for source in search_sources], ["juhe", "serpapi"])
        self.assertEqual([source.role for source in search_sources], ["primary", "cross_check"])
        self.assertEqual([source.weight for source in search_sources], [1.0, 0.6])
        self.assertEqual([source.name for source in enrichment_sources], ["duffel"])
        self.assertEqual(enrichment_sources[0].role, "enrichment")
        self.assertEqual(search_sources[0].query_overrides["stops"], "nonstop_preferred")

    def test_international_profile_excludes_juhe_and_long_failing_sources(self):
        with patch.dict(sys.modules, self.fake_modules):
            with patch.dict(os.environ, self.env, clear=True):
                search_sources, enrichment_sources = build_default_sources(
                    "PVG", "KIX", route_type="international"
                )

        self.assertEqual([source.name for source in search_sources], ["serpapi", "hasdata"])
        self.assertEqual([source.role for source in search_sources], ["primary", "primary"])
        self.assertEqual([source.name for source in enrichment_sources], ["duffel"])
        self.assertEqual(search_sources[0].query_overrides["stops"], "two_stops_or_fewer")

    def test_greater_china_profile_uses_google_without_juhe(self):
        with patch.dict(sys.modules, self.fake_modules):
            with patch.dict(os.environ, self.env, clear=True):
                search_sources, enrichment_sources = build_default_sources(
                    "PVG", "HKG", route_type="greater_china"
                )

        self.assertEqual([source.name for source in search_sources], ["serpapi", "hasdata"])
        self.assertEqual([source.role for source in search_sources], ["primary", "cross_check"])
        self.assertEqual([source.name for source in enrichment_sources], ["duffel"])
        self.assertEqual(search_sources[0].query_overrides["stops"], "nonstop_preferred")

    def test_ordered_sources_respect_route_profile_even_with_manual_sources(self):
        aggregator = FlightAggregator(
            [
                DummySource("juhe"),
                DummySource("serpapi"),
                DummySource("hasdata"),
                DummySource("travelpayouts"),
            ],
            [],
        )

        domestic = aggregator._ordered_search_sources("PVG", "PEK", route_type="domestic")
        international = aggregator._ordered_search_sources("PVG", "KIX", route_type="international")

        self.assertEqual([source.name for source in domestic], ["juhe", "serpapi"])
        self.assertEqual([source.name for source in international], ["serpapi", "hasdata"])


if __name__ == "__main__":
    unittest.main()
