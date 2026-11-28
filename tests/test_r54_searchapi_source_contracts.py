"""Offline SearchAPI adapter, cache, coverage and guard handoff contracts."""
import ast
from contextlib import redirect_stdout
from copy import deepcopy
from hashlib import sha256
import importlib
import inspect
import io
import json
import os
from pathlib import Path
import sys
from tempfile import TemporaryDirectory
import types
import unittest
from unittest.mock import patch

import analyzer
import config_loader
import main
import notifier
import request_cache as cache
import source_profiles
from sources import aggregator
from tests import test_return_phase_processing as scenario

ROOT = Path(__file__).resolve().parents[1]
ROUTE = ("PVG", "KIX", "2026-12-01")
KEY = "SYNTH_R54_KEY"
NOTE = "SearchAPI展示价,税费构成未拆分,以支付页为准"
PROTECTED_FILES = {
    "analyzer.py": (562067, "9f27b97eeb60cd477b825b8a0bc36cc7ef0f3292a52d7652665ae49c929bd6d7"),
    "notifier.py": (663094, "fb1211339bbfc75789eb40ae0ae0a99807c2d19008e41394735446b8ed090702"),
    "request_cache.py": (53260, "9be193cf38f23c2ee507d9e9a40a3b0d8a7f782cbcb807662b1f4423d8ece3a3"),
    "collection_plan.py": (40824, "855215c759e4d93145ef0f82dd6d4895faeedc3bd50e3806920c5aa85c897f82"),
    "airport_logistics.py": (8211, "62c5915d88783dd4ecaec5a5e285897208f497f5382ba16f6e5917c09bf25109"),
    "constraint_fingerprint.py": (11459, "b396aef5431cc976754bab2a5a2474641b78d38002844a808521b2f228c415a9"),
    "constraint_summary.py": (3595, "ab7a1505ff953891c7959e7e2412bc3ce7f93f93732e48c7557f5d43ebb3752b"),
    "web_form.py": (125145, "47f45bc97aa879bfb48e996482f13a504f1d68bc85fc0d893e885ea5b0f9ab1d"),
    "form_concepts.py": (37206, "cf535034c4242c291b6cf1f66c7335497e0fb2e4ecbf238492662b6621e1e9b6"),
    "form_structure.py": (53116, "5b9160c3cec816afeb92bf13dc868f1405c8db1eda3b6bb41479bc399b063882"),
    "plan_tracker.py": (47076, "b139465d01cf1f6e430f805a28aefb86b1085f0915825844c4409d16556f8f11"),
    "storage.py": (47792, "551a7a08964f49dc93e60eb25a6c9b78519b1244805d4408ae47b9a88673f4c3"),
    "observations_store.py": (19506, "64ed00efd4072e8cb458d28f5f8380ea0579151b87a8eea1605da050df3f8ae6"),
    "sources/hasdata_source.py": (2694, "464b81201403cf54696bdc9b639f87526e3a9fe6fab2ab95b6371ebd33c1f07c"),
    "sources/juhe_source.py": (21312, "1969a55d15947c31edac73134735e18727bdb3a89b06f8aa9797beaa5b6eff3d"),
    "sources/serpapi_source.py": (16030, "eeefaac3e4b1bdb9fce18e7d2db55adcd82e172ef59c7b4cd17aaa53dd571ad4"),
    "sources/aggregator.py": (65083, "01fcf0d1d10b125f16ed635b86e4fc2cdec4e6a92e53463b15878e68083bce82"),
    "tests/fixtures/form_normalization_baseline_v1.json": (180179, "88172833002fbe747b289848341a85d4c23470070339a4775ce2f61f183a2fd9"),
    "tests/fixtures/frozen_email/economy_payload.json": (168149, "85ee48c5539020e3cec3e2ec444d9a7f9403c4c259213c7840750c7788eb275b"),
    "test_same_day_business_mode.py": (77107, "066ae51ff5c0c7500d5a8a992558bb037fa03e0b337e9ddbaa81c5349e8a2dcd"),
    "tests/test_r40_same_day_combo_contracts.py": (29469, "57e93660bb27ea80644f261f85b5900826bb0dc4e81c6e563bf6808b53133744"),
    "tests/test_r49_baggage_and_source_truth_contracts.py": (13764, "1c6db27b2d1484cd3c0fd1197e1f5ae0687fa177370b5ac92128b37aabc20b5e"),
}
SCOPES = {
    ("notifier.py", None): {
        "build_notification_payload": "5db09e4d9d42c04def2766c544eb02f5fed5e24b2cfe0486a34b0c5d3ff14714",
        "render_pushplus_sections": "75f4f4f448dc777f9e763dc8da6f638fd3da24d7cbe64db15791e4ec97b314a6",
        "render_email": "1dda41baa9b20b21fd14b3e703cafcd3b4e1cdbeb0150d617d7024f8ab217955",
        "render_detail_html": "cefe927d905e3f0cbe32c7f032dc2f81aa988e84a241eac70587175d86a539f6",
    },
    ("analyzer.py", None): {
        "verify_fare_rules": "87ddfdf2bfdfc728057901b36751a544287568ef6f54ba1d8a14638daf43e082",
        "parse_flight_time": "dfbff457554895bf182c1c80900ac82f64c94d7aeaff28189ce547c85c31069f",
        "overall_score": "c762984d55d286c49e4f69387290c131161c5e8ebdcfaee796fdcf1f38296745",
        "calc_transfer_risk": "660cb5ea9a31f053122f705765a3299969f0bd14555c7d65509e5d3ad982e829",
        "comfort_score": "cb8973e4bd46b111364a272ec394ea3cb8f84c87fa8dc0bb3413340b96ed16f8",
        "_max_layover_minutes": "4e4c727aa34e7c9bf8223796a76c95b0d72fe6b27e9859f44d952ab07d8e2918",
        "_min_layover_minutes": "334a2adbf312e3a90fff48d7ac830900efedd271e83db62a5b07820240dd04fe",
        "evaluate_connection_safety": "a8dfd3dd3f3d4a109076352b8f9695721e0b771bb7eec6021bffdf1591715504",
        "_inspect_connection_waits": "56edd6d9dc7ba74d7e4f404938bebfb96d5e1c96ce8be7be4fc95a7ca6f2f03e",
        "_flight_comfort_score": "6afcdd19c1a7280854b52b34a706f0d96ab3d1ba04238bd29be14469f2c29a4b",
        "make_domestic_tags": "39ad7e2d0fcdd25fafa51833fd8bd23890ecfdfb9862f1857092083ab25c4dce",
        "match_time_preference": "ce0bbaf1024f6d98e16271b3580a5d7b55c8b0ebda1fcbdb636e9163f5f4d5ce",
        "conditional_percentile": "ae63ab43033c209f0c7cd43996373cc62b9b21ebf28a85bbbfd90e4c47df234b",
        "calculate_price_references": "2bed9302d3906bde5056ac12f301b32313adbe4776ec0665757a7f5e970fcc83",
        "analyze_roundtrip_prices": "dc560ba48f9920a2d49eaae3c3e93b652afa58db50943555e667f36a31484c1f",
        "_minutes_datetime": "5db57eb07bab030345e56b18e39187bb3bb3798fd160893890329f5d4353388a",
        "_assert_dated_datetime": "2894d2ad899d49e7734fd9e94c0507b2996c8f7f8a7d323c8d85fe35b0d1600e",
        "_same_day_outbound_passes_window": "021bf32d509b8d4290189bfeb344e5679dd4b773b87d3d035e16d58e79c94d16",
        "_same_day_return_passes_window": "cd498b9394640c67b82989c5ec3caa6b369ba083481dcc570ea4c951b8250eda",
        "_minutes_to_text": "4fbb12f15cc6d847e37f7200ae2279cbd50e2314b67bc67d93f160bcfa3fe87d",
        "_parse_window_text_minutes": "c7fd8a37feff2dd3aeaa8df8d23b64953bc9b404dedd4b92347c02d662908aac",
        "_window_datetime_from_minutes_or_text": "8277a3554e5a960008e261ebcf944bd3a9ac6d77ff4bc9491a43ecd32359b791",
        "_relative_time_text": "b37eec6d355968bb11594880218c1799d264fe6c5152d3a2e230886a579b4f16",
    },
    ("request_cache.py", None): {
        "_is_retryable_source_io_error": "cea99f9bfba80fd19c2ede890822330eae9d480a021b1f234f864c8e498d01d3",
        "_redact_error_text": "53269beb15a81b5e69c2dd2ef15fd1f2b0f14cb837bce7aa626ade00210044db",
        "_redact_exception_path": "8479837b3e4596414629ea444c229e617334bad768ef9b9c78f4dd478de920d3",
        "cached_fetch": "6680473b730fee826f72c937d84d148e66de060ae2dd023865c9761ffdaaa4e3",
    },
    ("sources/serpapi_source.py", None): {
        "_serpapi_travel_class": "254b103c62efef8ce03fad7b3ad2540d396d482fd9e622b2b180d63ab65ae590",
        "parse_google_flights": "38055fab5032822b5b7480b13e39931807fad1660968a5b1a2f3f5333238675d",
    },
    ("sources/serpapi_source.py", "SerpAPISource"): {
        "preflight_skip": "87ab2ea214436466b61250ae5d18b56596d17b97a810375540cf5bb19fd5380f",
        "fetch": "7a7f65209eb733f2edcc7438aaf6fd4afb63ffd5ef2f9df21b24881f0fdd0072",
    },
    ("sources/juhe_source.py", None): {
        "_is_success_response": "098fc398630a606e0786e66582023cc83519525e1c10a0fa6734ba0c4435239a",
        "_is_invalid_date_response": "065c96168e5da5e28f887fa2f3a8d6822b36d5e042c7f3c4736dfc2f9d03fd13",
        "_is_quota_response": "77177698c4fe1c7949464b2be72a397e73362d7a27f1e2f391c78d194e4a315b",
    },
    ("sources/juhe_source.py", "JuheSource"): {
        "preflight_skip": "38be6d3ddab4c5dd191dfce4712afadac2528d2c2e123323e6ae6c396cf03ed6",
        "build_request_params": "4ed241005454422e02afc5deac674fa7a32803acd739e8de610a1594200006a5",
        "parse": "56d0005b54c4ffffc2e070650a3c613235cbc75267f6d516b51263a381e3fbe5",
    },
}
INSPECT = {
    "parse_flight_time": "9a55d7e7d427adf2469b269546262ebc3d3264ec7f7cae4b7df537f1a4177297",
    "match_time_preference": "2e21c6f1cb34a5300b194c59b167b52753e321f2d7e696685c153f64c98af93f",
}
HANDOFFS = {
    "tests/test_r41_legitimate_empty_response_contracts.py": (
        (44990, "7a39484501d88d1e632156154ffa4a6d4da3c7c2ece88338cfaf3e7fd4ad7331"),
        (44871, "0320f4d41a9836b8167daefa5fac2fc11e3d63eb17bad366304e7a20611d88f9"),
        [
            (651, "            (\"sources/searchapi_source.py\", 2539, \"1de58d4ea8997b9aba555c3b74d5a45af866cbb4f3a6b1da82fcbfbf967e3317\"),\n"),
        ],
    ),
    "tests/test_r50_history_window_and_juhe_timestamp_contracts.py": (
        (16634, "382ffdd05d93b55e96a801dbaca3989bfffea32000d61d9eb55db36719d0e8c6"),
        (16011, "42551bc267b7607a9d9b1310bb7a3700fc84e08212b7a41b8632c0b1d3330afb"),
        [
            (259, "        path = ROOT / \"tests/test_r41_legitimate_empty_response_contracts.py\"\n"),
            (260, "        lines = path.read_text(encoding=\"utf-8\").splitlines(keepends=True)\n"),
            (261, "        removed = ('            (\"sources/juhe_source.py\", 20923, '\n"),
            (262, "                   '\"fbfb90acc93751fa10682d2014d1b012aba410025b3b105e4aa62f1bbf75e17a\"),\\n')\n"),
            (263, "        self.assertNotIn(removed, lines)\n"),
            (264, "        lines.insert(652, removed)\n"),
            (265, "        restored = \"\".join(lines).encode(\"utf-8\")\n"),
            (266, "        self.assertEqual((len(restored), hashlib.sha256(restored).hexdigest()),\n"),
            (267, "                         (45105, \"79f594bc4272e594c1a3158313f004beb4e68bef42a35e3f1c9dcc722c77588f\"))\n"),
            (268, "\n"),
        ],
    ),
    "tests/test_r51_cross_midnight_window_contracts.py": (
        (18726, "8cda807a48f77372d85132acfee68e9b08f8677fb2fed69b6e43474da74f564b"),
        (18442, "204bae3d510dc2605c903a1d86ee31bf157a5b62c39565dc5de1cc27307debb7"),
        [
            (44, "    \"tests/test_r41_legitimate_empty_response_contracts.py\": (44990, \"7a39484501d88d1e632156154ffa4a6d4da3c7c2ece88338cfaf3e7fd4ad7331\"),\n"),
            (46, "    \"tests/test_r50_history_window_and_juhe_timestamp_contracts.py\": (16634, \"382ffdd05d93b55e96a801dbaca3989bfffea32000d61d9eb55db36719d0e8c6\"),\n"),
        ],
    ),
    "tests/test_r52_unverified_layover_comfort_contracts.py": (
        (18950, "770ae8d505bd969250ccb098d818a493159b1246210baa56bf0a801083c32660"),
        (18666, "a9f4c69fa16deba2a4f442c5f3acda5d5172d03d38027f745c9c3046f21952e2"),
        [
            (43, "    \"tests/test_r41_legitimate_empty_response_contracts.py\": (44990, \"7a39484501d88d1e632156154ffa4a6d4da3c7c2ece88338cfaf3e7fd4ad7331\"),\n"),
            (45, "    \"tests/test_r50_history_window_and_juhe_timestamp_contracts.py\": (16634, \"382ffdd05d93b55e96a801dbaca3989bfffea32000d61d9eb55db36719d0e8c6\"),\n"),
        ],
    ),
    "tests/test_r53_http_retry_classification_contracts.py": (
        (20670, "d2a7dbd15159ec904e555e5d8d95f7d78f535950b3495fe932ce4560134df5c1"),
        (19263, "d846a68d0a3d110662312c66b353f07265d2b0c384086686d1a513cef8754a40"),
        [
            (52, "    \"tests/test_r41_legitimate_empty_response_contracts.py\": (44990, \"7a39484501d88d1e632156154ffa4a6d4da3c7c2ece88338cfaf3e7fd4ad7331\"),\n"),
            (54, "    \"tests/test_r50_history_window_and_juhe_timestamp_contracts.py\": (16634, \"382ffdd05d93b55e96a801dbaca3989bfffea32000d61d9eb55db36719d0e8c6\"),\n"),
            (299, "            old_cache = '    \"request_cache.py\": (52429, \"806b2d9328b6ddddfb80c1e08a12e3779ca90c2450c1f4afda897c903075daea\"),\\n'\n"),
            (300, "            old_r51 = '    \"tests/test_r51_cross_midnight_window_contracts.py\": (18827, \"c65a408f9df56c3763eddd116bb5e2e50aadc5ee4fd589185812209381859e82\"),\\n'\n"),
            (301, "            for path, insertions, expected in (\n"),
            (302, "                (\"tests/test_r51_cross_midnight_window_contracts.py\", [(38, old_cache)],\n"),
            (303, "                 (18827, \"c65a408f9df56c3763eddd116bb5e2e50aadc5ee4fd589185812209381859e82\")),\n"),
            (304, "                (\"tests/test_r52_unverified_layover_comfort_contracts.py\", [(26, old_r51), (38, old_cache)],\n"),
            (305, "                 (19185, \"caab9761dff9d7d5565f8c8f382f2072bbc2be9b28053b9c531788a10cf02f8f\")),\n"),
            (306, "            ):\n"),
            (307, "                lines = (ROOT/path).read_text(encoding=\"utf-8\").splitlines(keepends=True)\n"),
            (308, "                for idx, line in insertions:\n"),
            (309, "                    self.assertNotIn(line, lines)\n"),
            (310, "                    lines.insert(idx, line)\n"),
            (311, "                restored = \"\".join(lines).encode(\"utf-8\")\n"),
            (312, "                self.assertEqual((len(restored), sha256(restored).hexdigest()), expected, path)\n"),
        ],
    ),
}


_REAL_HTTPX = None


def _real_httpx():
    global _REAL_HTTPX
    if _REAL_HTTPX is not None:
        return _REAL_HTTPX
    current = sys.modules.get("httpx")
    if isinstance(current, types.ModuleType) and hasattr(current, "Client") and hasattr(current, "Response"):
        _REAL_HTTPX = current
        return current
    saved = sys.modules.pop("httpx", None)
    try:
        _REAL_HTTPX = importlib.import_module("httpx")
        return _REAL_HTTPX
    finally:
        if saved is not None:
            sys.modules["httpx"] = saved
        else:
            sys.modules.pop("httpx", None)


def row(price, number="MU501"):
    return {"price": price, "total_duration": 150, "flights": [{
        "flight_number": number, "airline": "China Eastern", "duration": 150,
        "departure_airport": {"id": "PVG", "time": "2026-12-01 09:00"},
        "arrival_airport": {"id": "KIX", "time": "2026-12-01 12:30"},
    }]}


class OfflineCase(unittest.TestCase):
    def setUp(self):
        tmp = Path(self.enterContext(TemporaryDirectory()))
        self.enterContext(patch.dict(os.environ, {"SEARCHAPI_KEY": KEY, "NO_LIVE_API": "1"}))
        self.enterContext(patch.object(config_loader, "RUNTIME_CONFIG_PATH", tmp / "absent.yaml"))
        self.httpx = _real_httpx()
        self.enterContext(patch.dict(sys.modules, {"httpx": self.httpx}))
        self.addCleanup(sys.modules.pop, "sources.searchapi_source", None)
        self.enterContext(patch.object(self.httpx.Client, "send", side_effect=AssertionError("network disabled")))
        self.enterContext(patch.object(self.httpx.AsyncClient, "send", side_effect=AssertionError("network disabled")))
        self.enterContext(patch.object(self.httpx, "get", side_effect=AssertionError("network disabled")))
        self.addCleanup(cache.reset_for_tests, None)
        cache.reset_for_tests(tmp / "cache")
        cache.start_request_cache_round("r54")
        self.enterContext(patch.object(cache, "append_round_evidence"))
        for module in (cache, aggregator, main):
            self.enterContext(patch.object(module, "safe_log"))
        self.sleep = self.enterContext(patch("time.sleep"))

    def load_searchapi(self):
        name = "sources.searchapi_source"
        sys.modules.pop(name, None)
        try:
            return importlib.import_module(name)
        except ImportError as exc:
            self.fail(f"searchapi_source import failed: {exc}")

    def searchapi_route_profile(self):
        profile = {
            "sources": [{"name": "searchapi", "role": "primary", "weight": 1.0,
                         "cabins": ["economy", "business"]}],
            "retired_sources": [],
            "query": {"currency": "CNY", "hl": "zh-cn", "gl": "cn", "stops": "two_stops_or_fewer"},
            "primary_source": "searchapi",
        }
        return patch.dict(source_profiles.ROUTE_SOURCE_PROFILES, {"international": profile})

    def responses(self, *pairs):
        queued = iter(pairs)

        def get(url, *, params, timeout, headers=None):
            status, payload = next(queued)
            req = self.httpx.Request("GET", url, params=params, headers=headers)
            return self.httpx.Response(status, request=req, json=payload)

        return patch.object(self.httpx, "get", side_effect=get)


class TestTrack1ImportCabinAndPreflight(OfflineCase):
    def test_g01_searchapi_module_imports_cleanly_and_declares_supported_cabins(self):
        cls = self.load_searchapi().SearchAPISource
        self.assertEqual(cls.name, "searchapi")
        self.assertEqual(cls.supported_cabins, frozenset({"economy", "business"}))
        self.assertEqual([source_profiles.source_supports_cabin(cls(), cabin)
                          for cabin in ("economy", "business", "first")], [True, True, False])

    def test_g02_travel_class_and_stops_overrides_send_documented_searchapi_params(self):
        mod = self.load_searchapi()
        for value, expected in (("economy", "economy"), ("BUSINESS", "business"),
                                ("premium_economy", "premium_economy"), ("first", "first_class"),
                                ("first_class", "first_class"), (None, "economy"), ("unknown", "economy")):
            self.assertEqual(mod._searchapi_travel_class(value), expected, value)
        for value, expected in (("nonstop_preferred", "nonstop"), ("nonstop", "nonstop"),
                                ("one_stop_or_fewer", "one_stop_or_fewer"),
                                ("two_stops_or_fewer", "two_stops_or_fewer"), ("", "two_stops_or_fewer"),
                                (None, "two_stops_or_fewer")):
            self.assertEqual(mod._searchapi_stops_value(value), expected, value)
        src = mod.SearchAPISource()
        self.assertIsNone(src.preflight_skip(*ROUTE, "business"))
        src.query_overrides = {"stops": "nonstop_preferred", "currency": "USD", "hl": "en", "gl": "us"}
        with self.responses((200, {"best_flights": []})) as get:
            src.fetch(*ROUTE, "business")
        self.assertEqual(get.call_args.kwargs["params"], {
            "engine": "google_flights", "departure_id": "PVG", "arrival_id": "KIX",
            "outbound_date": ROUTE[2], "flight_type": "one_way", "gl": "us", "hl": "en",
            "currency": "USD", "stops": "nonstop", "sort_by": "price",
            "travel_class": "business", "api_key": KEY,
        })
        self.assertNotIn("selected_cabins", get.call_args.kwargs["params"])

    def test_g03_missing_searchapi_key_preflight_skips_and_fetch_never_touches_network(self):
        src = self.load_searchapi().SearchAPISource()
        os.environ.pop("SEARCHAPI_KEY", None)
        with patch.object(self.httpx, "get", side_effect=AssertionError("network disabled")) as get:
            for result in (src.preflight_skip(*ROUTE, "economy"), src.fetch(*ROUTE)):
                self.assertEqual(result, {"flights": [], "source": "searchapi", "raw": {},
                                          "source_status": "not_configured",
                                          "skipped_reason": "缺少 SearchAPI 密钥（SEARCHAPI_KEY）"})
            result = cache.cached_fetch(src, *ROUTE, persist=False, force_fresh=True)
        get.assert_not_called()
        self.assertEqual(result["source_status"], "not_configured")
        stats = cache.get_request_cache_stats()
        self.assertEqual((stats["actual"], stats["skipped"]), (0, 1))


class TestTrack2ResponseSchemaAndAuthFallback(OfflineCase):
    def test_g04_priced_response_populates_source_status_success_raw_count_and_passes_empty_schema(self):
        src = self.load_searchapi().SearchAPISource()
        raw = {"best_flights": [row(1500)], "other_flights": [row(2100, "MU502")],
               "price_insights": {"lowest_price": 1500}}
        with self.responses((200, raw)):
            result = src.fetch(*ROUTE)
        self.assertEqual((result["source_status"], result["raw_result_count"], len(result["flights"])),
                         ("success", 2, 2))
        self.assertEqual((result["raw"], result["price_insights"], result["source"]),
                         (raw, raw["price_insights"], "searchapi"))
        self.assertEqual(result["flights"][0]["price_note"], NOTE)
        self.assertEqual(result["flights"][0]["segments"][0]["carrier_basis"], "marketing_fallback")
        self.assertIs(aggregator._validate_raw_empty_schema("searchapi", result, has_usable_flights=True), True)

    def test_g05_legitimate_empty_and_discarded_unpriced_rows_align_with_r41_contracts(self):
        src = self.load_searchapi().SearchAPISource()
        empty = {"best_flights": [], "other_flights": [], "search_metadata": {"status": "Success"}}
        invalid = {**empty, "best_flights": [row(None)]}
        with self.responses((200, empty), (200, invalid), (200, invalid)) as get:
            for count, valid in ((0, True), (1, False)):
                result = src.fetch(*ROUTE)
                self.assertEqual((result["flights"], result["raw_result_count"], result["source_status"]),
                                 ([], count, "empty"))
                self.assertIs(aggregator._validate_raw_empty_schema("searchapi", result, has_usable_flights=False), valid)
            with self.searchapi_route_profile():
                outcome = main.collect_for_airport_matrix(
                    aggregator.FlightAggregator([src], []), ["PVG"], ["KIX"], ROUTE[2],
                    cabin_classes=["economy"],
                )
        self.assertEqual(get.call_count, 3)
        self.assertIsInstance(outcome, dict)
        self.assertEqual(outcome["collection_outcome"], "data_unusable")

    def test_g06_header_auth_fallback_and_http_status_error_redact_api_key_without_cache_retry(self):
        src = self.load_searchapi().SearchAPISource()
        buf = io.StringIO()
        with redirect_stdout(buf), self.responses((401, {}), (200, {"best_flights": [row(1600)]})) as get:
            self.assertEqual(src.fetch(*ROUTE)["source_status"], "success")
        self.sleep.assert_called_once_with(2)
        self.assertEqual(get.call_count, 2)
        first, second = [c.kwargs for c in get.call_args_list]
        self.assertEqual(first["params"]["api_key"], KEY)
        self.assertNotIn("api_key", second["params"])
        self.assertEqual(second["headers"], {"Authorization": "Bearer " + KEY})
        self.assertIn("api_key=***", buf.getvalue())
        self.assertNotIn(KEY, buf.getvalue())
        with self.responses((403, {}), (403, {})) as denied:
            result = cache.cached_fetch(src, *ROUTE, persist=False, force_fresh=True)
        self.assertEqual((denied.call_count, cache.get_request_cache_stats()["actual"],
                          result["retry_count"], result["source_status"]), (2, 1, 0, "failed"))
        self.assertIn("api_key=***", result["error"])
        self.assertNotIn(KEY, result["error"])


class TestTrack3FactoryAndAggregatorIntegration(OfflineCase):
    def test_g07_instantiate_source_and_context_free_build_default_sources_succeed_while_profiles_stay_inactive(self):
        cls = self.load_searchapi().SearchAPISource
        for key in ("JUHE_FLIGHT_KEY", "SERPAPI_KEY", "SERPAPI_API_KEY", "SERP_API_KEY", "HASDATA_KEY",
                    "DUFFEL_TOKEN", "TRAVELPAYOUTS_TOKEN", "RAPIDAPI_KEY"):
            os.environ.pop(key, None)
        try:
            self.assertIsInstance(aggregator._instantiate_source("searchapi"), cls)
            listing, enrichment = aggregator.build_default_sources()
            self.assertEqual(([type(s) for s in listing], enrichment), ([cls], []))
            with patch.object(aggregator, "_instantiate_source", wraps=aggregator._instantiate_source) as factory:
                routed = aggregator.build_default_sources("PVG", "KIX", "international")
            self.assertNotIn("searchapi", [c.args[0] for c in factory.call_args_list])
            self.assertFalse(any(isinstance(s, cls) for group in routed for s in group))
        except ImportError as exc:
            self.fail(f"factory import failed: {exc}")

    def test_g08_flight_aggregator_collect_and_source_degradation_context_integrate_searchapi(self):
        src = self.load_searchapi().SearchAPISource()
        agg = aggregator.FlightAggregator([src], [])
        with self.searchapi_route_profile(), self.responses((200, {"best_flights": [row(2700)]})):
            data = agg.collect(*ROUTE, cabin_classes=["business"], force_fresh=True)
        self.assertIsInstance(data, dict)
        self.assertEqual(len(data["flights"]), 1)
        self.assertEqual(agg.last_source_stats["searchapi"]["count"], 1)
        self.assertEqual(agg.last_fetch_evidence_units[0]["source_status"], "success")
        cache.reset_request_cache()
        cache.start_request_cache_round("r54-failure")
        with self.searchapi_route_profile(), self.responses((429, {})) as get:
            agg.collect(*ROUTE, cabin_classes=["business"], force_fresh=True)
        self.assertEqual(get.call_count, 1)
        self.assertEqual(len(agg.last_source_errors), 1)
        error = agg.last_source_errors[0]
        self.assertEqual((error["source"], error["error_type"], error["retry_count"]),
                         ("searchapi", "HTTPStatusError", 0))
        self.assertNotIn(KEY, json.dumps(error))
        ctx = notifier._build_source_degradation_context(
            source_stats=agg.last_source_stats, source_errors=agg.last_source_errors,
            last_snapshot={"channels": '["searchapi"]'},
        )
        self.assertIs(ctx["active"], True)


def carriers(payload):
    _, email = notifier.render_email(deepcopy(payload))
    push = notifier.prepare_push_render(notifier.render_pushplus_sections(deepcopy(payload))).content
    return email, push, notifier.render_detail_html(deepcopy(payload))


class TestTrack4Guards(unittest.TestCase):
    def test_g09_protected_files_scoped_ast_locks_gateway_invariants_and_reversible_handoffs_are_exact(self):
        for path, expected in PROTECTED_FILES.items():
            b = (ROOT/path).read_text(encoding="utf-8").encode("utf-8")
            self.assertEqual((len(b), sha256(b).hexdigest()), expected, path)
        for (path, cls), locks in SCOPES.items():
            src = (ROOT/path).read_text(encoding="utf-8")
            nodes = ast.parse(src).body
            if cls:
                nodes = next(n for n in nodes if isinstance(n, ast.ClassDef) and n.name == cls).body
            for name, digest in locks.items():
                node, = [n for n in nodes if isinstance(n, ast.FunctionDef) and n.name == name]
                b = ast.get_source_segment(src, node).encode("utf-8")
                self.assertEqual(sha256(b).hexdigest(), digest, name)
        for name, digest in INSPECT.items():
            b = inspect.getsource(getattr(analyzer, name)).encode("utf-8")
            self.assertEqual(sha256(b).hexdigest(), digest, name)
        src = (ROOT/"sources/searchapi_source.py").read_text(encoding="utf-8")
        self.assertEqual(len(src)-len(src.rstrip("\n")), 1)
        tree = ast.parse(src)
        cls, = (n for n in tree.body if isinstance(n, ast.ClassDef) and n.name == "SearchAPISource")
        fetch, = (n for n in cls.body if isinstance(n, ast.FunctionDef) and n.name == "fetch")
        self.assertEqual(sum(isinstance(n, ast.Call) and ast.unparse(n.func) == "httpx.get"
                             for n in ast.walk(fetch)), 2)
        reads = [n for n in ast.walk(tree) if isinstance(n, ast.Call)
                 and ast.unparse(n.func) in {"os.environ.get", "os.getenv"}]
        self.assertTrue(reads)
        for n in reads:
            self.assertIsInstance(n.args[0], ast.Constant)
            self.assertEqual(n.args[0].value, "SEARCHAPI_KEY")

    def test_g10_frozen_three_carriers_and_normal_roundtrip_baseline_remain_exact(self):
        with TemporaryDirectory() as tmp, patch.object(config_loader, "RUNTIME_CONFIG_PATH", Path(tmp)/"absent.yaml"):
            payload = json.loads((ROOT/"tests/fixtures/frozen_email/economy_payload.json").read_text(encoding="utf-8"))
            for text, expected in zip(carriers(payload), (
                (53307, "1dd353d2b7bcafc02087957ccccb6661478e20472a22fb3e15dae6a56473ee6b"),
                (5552, "2ac0e80d9f82dbe7ebccf524c0832f09d42d916212b6178316b0b7bcd60574ff"),
                (54248, "e31b8728b8d0bc05c9bbd4bd99c299aaf6dca7b66674b6c560ec089b1681ffa6"),
            ), strict=True):
                b = text.encode("utf-8")
                self.assertEqual((len(b), sha256(b).hexdigest()), expected)
        cls = scenario.ReturnPhaseProcessingTest
        with redirect_stdout(io.StringIO()):
            cls.setUpClass()
            try:
                result = cls("test_normal_roundtrip_baseline").run()
                self.assertEqual(result.testsRun, 1)
                self.assertTrue(result.wasSuccessful(), result.errors + result.failures)
            finally:
                cls.tearDownClass()
