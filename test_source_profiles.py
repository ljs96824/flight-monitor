import os
import sys
import types
import unittest
from unittest.mock import patch

from source_profiles import normalize_route_type
from sources.aggregator import (
    FlightAggregator,
    _instantiate_source,
    build_default_sources,
    classify_route,
    classify_route_with_rule,
    route_type_for_with_rule,
)


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

    def test_serpapi_factory_accepts_each_supported_key_alias(self):
        aliases = ("SERPAPI_KEY", "SERPAPI_API_KEY", "SERP_API_KEY")
        for alias in aliases:
            with self.subTest(alias=alias), patch.dict(
                sys.modules, self.fake_modules
            ), patch.dict(os.environ, {alias: "serpapi-key"}, clear=True):
                source = _instantiate_source("serpapi")

            self.assertIsNotNone(source)
            self.assertEqual(source.name, "serpapi")

    def test_classify_route_distinguishes_domestic_greater_china_and_international(self):
        self.assertEqual(classify_route("PVG", "PEK"), "domestic")
        self.assertEqual(classify_route("PVG", "HKG"), "greater_china")
        self.assertEqual(classify_route("TPE", "HKG"), "greater_china")
        self.assertEqual(classify_route("PVG", "KIX"), "international")

    def test_domestic_profile_uses_only_juhe_search_and_duffel_enrichment(self):
        with patch.dict(sys.modules, self.fake_modules):
            with patch.dict(os.environ, self.env, clear=True):
                search_sources, enrichment_sources = build_default_sources(
                    "PVG", "PEK", route_type="domestic"
                )

        self.assertEqual([source.name for source in search_sources], ["juhe"])
        self.assertEqual([source.role for source in search_sources], ["primary"])
        self.assertEqual([source.weight for source in search_sources], [1.0])
        self.assertEqual([source.name for source in enrichment_sources], ["duffel"])
        self.assertEqual(enrichment_sources[0].role, "enrichment")
        self.assertEqual(search_sources[0].query_overrides["stops"], "nonstop_preferred")

    def test_international_profile_uses_juhe_search_and_duffel_enrichment(self):
        with patch.dict(sys.modules, self.fake_modules):
            with patch.dict(os.environ, self.env, clear=True):
                search_sources, enrichment_sources = build_default_sources(
                    "PVG", "KIX", route_type="international"
                )

        self.assertEqual([source.name for source in search_sources], ["juhe"])
        self.assertEqual([source.role for source in search_sources], ["primary"])
        self.assertEqual([source.name for source in enrichment_sources], ["duffel"])
        self.assertEqual(search_sources[0].query_overrides["stops"], "two_stops_or_fewer")

    def test_greater_china_profile_uses_hasdata_then_juhe_search_sources(self):
        with patch.dict(sys.modules, self.fake_modules):
            with patch.dict(os.environ, self.env, clear=True):
                search_sources, enrichment_sources = build_default_sources(
                    "PVG", "HKG", route_type="greater_china"
                )

        self.assertEqual([source.name for source in search_sources], ["juhe", "hasdata"])
        self.assertEqual([source.role for source in search_sources], ["primary", "cross_check"])
        self.assertEqual([source.name for source in enrichment_sources], ["duffel"])
        self.assertEqual(search_sources[0].query_overrides["stops"], "nonstop_preferred")

    def test_hasdata_parser_import_does_not_require_serpapi_sdk(self):
        with patch.dict(sys.modules, {"serpapi": None}):
            sys.modules.pop("sources.serpapi_source", None)
            sys.modules.pop("sources.hasdata_source", None)
            from sources.hasdata_source import HasDataSource

        self.assertEqual(HasDataSource.name, "hasdata")

    def test_route_type_aliases_and_classification_rule_are_explicit(self):
        self.assertEqual(normalize_route_type("hk_mo_tw"), "greater_china")
        self.assertEqual(classify_route_with_rule("PVG", "HKG"), ("greater_china", "mainland_to_hk_mo_tw"))
        self.assertEqual(classify_route_with_rule("PVG", "KIX"), ("international", "default_international"))

    def test_explicit_route_type_cannot_override_iata_classification(self):
        cases = [
            ("PVG", "KIX", "domestic", "international", "default_international"),
            ("KIX", "NRT", "domestic", "international", "default_international"),
            ("PVG", "HKG", "domestic", "greater_china", "mainland_to_hk_mo_tw"),
        ]

        for origin, dest, explicit, expected_type, expected_rule in cases:
            with self.subTest(origin=origin, dest=dest, explicit=explicit):
                self.assertEqual(
                    route_type_for_with_rule(origin, dest, explicit),
                    (expected_type, expected_rule),
                )

    def test_stale_domestic_value_cannot_disable_international_sources(self):
        with patch.dict(sys.modules, self.fake_modules):
            with patch.dict(os.environ, self.env, clear=True):
                search_sources, enrichment_sources = build_default_sources(
                    "PVG", "KIX", route_type="domestic"
                )

        self.assertEqual([source.name for source in search_sources], ["juhe"])
        self.assertEqual([source.name for source in enrichment_sources], ["duffel"])

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
        greater_china = aggregator._ordered_search_sources("PVG", "HKG", route_type="greater_china")

        self.assertEqual([source.name for source in domestic], ["juhe"])
        self.assertEqual([source.name for source in international], ["juhe"])
        self.assertEqual([source.name for source in greater_china], ["juhe", "hasdata"])


if __name__ == "__main__":
    unittest.main()
