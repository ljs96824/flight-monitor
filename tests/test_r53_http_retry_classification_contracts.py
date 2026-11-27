"""Offline retry classification, quota accounting and exception privacy contracts."""

import ast
from contextlib import redirect_stdout
from copy import deepcopy
from datetime import date
from hashlib import sha256
import inspect
import io
import json
import os
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch
from urllib.error import HTTPError as UrllibHTTPError, URLError

import requests
from requests import exceptions as errors

import analyzer
import api_usage
import collection_plan
import config_loader
import notifier
import request_cache as cache
from sources import aggregator, juhe_source
from tests import test_return_phase_processing as scenario


ROOT = Path(__file__).resolve().parents[1]
ROUTE = ("PVG", "KIX", "2026-12-01")
ENDPOINT = "https://apis.juhe.cn/flight/query"
FILES = {
    "analyzer.py": (562067, "9f27b97eeb60cd477b825b8a0bc36cc7ef0f3292a52d7652665ae49c929bd6d7"),
    "notifier.py": (663094, "fb1211339bbfc75789eb40ae0ae0a99807c2d19008e41394735446b8ed090702"),
    "main.py": (175530, "878a6df6d84276cde4f092f42d1c0453d08c5a8ceafdc638a68dc0561c4c13ef"),
    "airport_logistics.py": (8211, "62c5915d88783dd4ecaec5a5e285897208f497f5382ba16f6e5917c09bf25109"),
    "constraint_fingerprint.py": (11459, "b396aef5431cc976754bab2a5a2474641b78d38002844a808521b2f228c415a9"),
    "constraint_summary.py": (3595, "ab7a1505ff953891c7959e7e2412bc3ce7f93f93732e48c7557f5d43ebb3752b"),
    "web_form.py": (125145, "47f45bc97aa879bfb48e996482f13a504f1d68bc85fc0d893e885ea5b0f9ab1d"),
    "form_concepts.py": (37206, "cf535034c4242c291b6cf1f66c7335497e0fb2e4ecbf238492662b6621e1e9b6"),
    "form_structure.py": (53116, "5b9160c3cec816afeb92bf13dc868f1405c8db1eda3b6bb41479bc399b063882"),
    "plan_tracker.py": (47076, "b139465d01cf1f6e430f805a28aefb86b1085f0915825844c4409d16556f8f11"),
    "storage.py": (47792, "551a7a08964f49dc93e60eb25a6c9b78519b1244805d4408ae47b9a88673f4c3"),
    "observations_store.py": (19506, "64ed00efd4072e8cb458d28f5f8380ea0579151b87a8eea1605da050df3f8ae6"),
    "sources/juhe_source.py": (21312, "1969a55d15947c31edac73134735e18727bdb3a89b06f8aa9797beaa5b6eff3d"),
    "sources/aggregator.py": (65083, "01fcf0d1d10b125f16ed635b86e4fc2cdec4e6a92e53463b15878e68083bce82"),
    "tests/fixtures/form_normalization_baseline_v1.json": (180179, "88172833002fbe747b289848341a85d4c23470070339a4775ce2f61f183a2fd9"),
    "tests/fixtures/frozen_email/economy_payload.json": (168149, "85ee48c5539020e3cec3e2ec444d9a7f9403c4c259213c7840750c7788eb275b"),
    "test_same_day_business_mode.py": (77107, "066ae51ff5c0c7500d5a8a992558bb037fa03e0b337e9ddbaa81c5349e8a2dcd"),
    "tests/test_r40_same_day_combo_contracts.py": (29469, "57e93660bb27ea80644f261f85b5900826bb0dc4e81c6e563bf6808b53133744"),
    "tests/test_r41_legitimate_empty_response_contracts.py": (44990, "7a39484501d88d1e632156154ffa4a6d4da3c7c2ece88338cfaf3e7fd4ad7331"),
    "tests/test_r49_baggage_and_source_truth_contracts.py": (13764, "1c6db27b2d1484cd3c0fd1197e1f5ae0687fa177370b5ac92128b37aabc20b5e"),
    "tests/test_r50_history_window_and_juhe_timestamp_contracts.py": (16634, "382ffdd05d93b55e96a801dbaca3989bfffea32000d61d9eb55db36719d0e8c6"),
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


def response(code, key="synthetic"):
    reply = requests.Response()
    reply.status_code = code
    reply.url = f"{ENDPOINT}?departure=PVG&arrival=KIX&key={key}"
    reply.reason = "synthetic failure"
    reply._content = b"synthetic"
    return reply


class ScriptedSource:
    name = "synthetic"
    role = "search"

    def __init__(self, *outcomes):
        self.calls = 0
        self.outcomes = outcomes

    def fetch(self, origin, dest, date_str, cabin_class):
        result = self.outcomes[self.calls]
        self.calls += 1
        if isinstance(result, BaseException):
            raise result
        return deepcopy(result)


class OfflineCase(unittest.TestCase):
    def setUp(self):
        self.root = Path(self.enterContext(TemporaryDirectory()))
        self.addCleanup(cache.reset_for_tests, None)
        cache.reset_for_tests(self.root / "cache")
        self.enterContext(patch.dict(os.environ, {"NO_LIVE_API": "1"}))
        self.enterContext(patch.object(requests.Session, "send", side_effect=AssertionError("network disabled")))
        self.enterContext(patch.object(config_loader, "RUNTIME_CONFIG_PATH", self.root / "absent.yaml"))
        self.enterContext(patch.object(juhe_source, "_cache_dir", return_value=self.root / "juhe"))
        self.enterContext(patch.object(juhe_source, "shanghai_today", return_value=date(2026, 11, 28)))
        self.logs = []
        for module in (cache, juhe_source, collection_plan, aggregator):
            self.enterContext(patch.object(module, "safe_log", side_effect=self.logs.append))
        self.enterContext(patch.object(cache, "append_round_evidence"))
        self.sleep = self.enterContext(patch.object(cache.time, "sleep"))

    def attempt(self, *outcomes):
        cache.reset_request_cache()
        cache.start_request_cache_round("offline-r53")
        self.sleep.reset_mock()
        source = ScriptedSource(*outcomes)
        result = cache.cached_fetch(source, *ROUTE, persist=False, force_fresh=True)
        return source.calls, result, cache.get_request_cache_stats()

    def single_failure(self, exc):
        calls, result, stats = self.attempt(exc, exc)
        self.assertEqual(calls, 1, type(exc).__name__)
        self.sleep.assert_not_called()
        self.assertEqual((result["source_status"], result["retry_count"]), ("failed", 0))
        self.assertEqual((stats["actual"], stats["retries"]), (1, 0))
        return result


class TestTrack1ClassifierAndQuota(OfflineCase):
    def test_g01_retry_classifier_separates_transient_transport_and_local_io_from_http_and_deterministic_errors(self):
        self.assertTrue(hasattr(cache, "_is_retryable_source_io_error"))
        classify = cache._is_retryable_source_io_error
        for code in (400, 401, 403, 404, 422, 429, 500, 502, 503, 504):
            self.assertIs(classify(errors.HTTPError(response=response(code))), False, code)
        for exc in (errors.HTTPError(), UrllibHTTPError(ENDPOINT, 503, "test", None, None),
                    errors.SSLError(), errors.JSONDecodeError("bad", "", 0), errors.InvalidJSONError(),
                    errors.InvalidURL(), errors.InvalidSchema(), errors.MissingSchema(), errors.InvalidHeader(),
                    errors.InvalidProxyURL(), errors.URLRequired(), errors.TooManyRedirects(),
                    errors.ContentDecodingError(), errors.StreamConsumedError(), errors.UnrewindableBodyError(),
                    errors.RetryError(), errors.RequestException(), ValueError(), RuntimeError()):
            self.assertIs(classify(exc), False, type(exc).__name__)
        for exc in (errors.ConnectionError(), errors.ProxyError(), errors.ConnectTimeout(), errors.ReadTimeout(),
                    errors.Timeout(), errors.ChunkedEncodingError(), PermissionError(), FileNotFoundError(),
                    ConnectionResetError(), TimeoutError(), BrokenPipeError(), OSError(), URLError("offline")):
            self.assertIs(classify(exc), True, type(exc).__name__)

    def test_g02_requests_http_errors_4xx_and_5xx_do_not_retry_or_sleep_and_record_single_attempt(self):
        for code in (400, 401, 403, 404, 422, 429, 500, 502, 503):
            self.single_failure(errors.HTTPError(str(code), response=response(code)))

    def test_g03_juhe_http_error_records_single_api_usage_attempt_and_redacts_key_query_param(self):
        key = "SECRET_JUHE_999"
        for code in (429, 500):
            ledger = self.root / str(code) / "api_usage.json"
            api_usage.initialize_usage_ledger(ledger)
            cache.start_request_cache_round(f"offline-{code}", track_usage=True, usage_path=ledger)
            self.logs.clear()
            with patch.dict(os.environ, {"JUHE_FLIGHT_KEY": key}), patch.object(
                requests, "get", return_value=response(code, key)
            ) as get:
                result = cache.cached_fetch(juhe_source.JuheSource(), *ROUTE, force_fresh=True, persist=False)
            self.assertEqual(get.call_count, 1)
            self.assertEqual(get.call_args.kwargs["params"]["key"], key)
            self.assertEqual((result["error_type"], result["retry_count"]), ("HTTPError", 0))
            usage = api_usage.load_usage_strict(ledger)
            self.assertEqual(list(usage["dates"].values()), [{"juhe": 1}])
            self.assertEqual(len(usage["entries"]), 1)
            for text in (result["error"], "\n".join(self.logs)):
                self.assertIn("key=***", text)
                self.assertNotIn(key, text)
        self.assertEqual(cache._redact_error_text(f"cache_key=PVG_KIX&key={key}"), "cache_key=PVG_KIX&key=***")


class TestTrack2RetryBoundaries(OfflineCase):
    def test_g04_deterministic_request_exceptions_ssl_json_url_and_redirects_never_retry(self):
        for exc in (errors.SSLError(), errors.JSONDecodeError("bad", "", 0), errors.InvalidURL(),
                    errors.MissingSchema(), errors.InvalidHeader(), errors.TooManyRedirects(),
                    errors.ContentDecodingError(), errors.RetryError(), errors.RequestException()):
            self.single_failure(exc)

    def test_g05_urllib_http_error_does_not_retry_redacts_url_secrets_and_keeps_metadata_schema(self):
        exc = UrllibHTTPError(f"{ENDPOINT}?key=U_SEC&token=T_SEC", 503, "Unavailable", None, None)
        result = self.single_failure(exc)
        self.assertIsNone(result["path"])
        self.assertEqual(set(cache._source_exception_metadata(exc)), {"error", "error_type", "errno", "path"})
        self.assertNotIn("U_SEC", result["error"])
        self.assertNotIn("T_SEC", result["error"])

    def test_g06_transient_transport_and_local_io_still_retry_once_while_http_error_stops_immediately(self):
        ok = {"source_status": "success", "flights": [], "raw": {}}
        for exc in (errors.ConnectTimeout(), errors.ReadTimeout(), errors.ConnectionError(),
                    errors.ChunkedEncodingError(), PermissionError()):
            calls, result, stats = self.attempt(exc, ok)
            self.assertEqual((calls, result["retry_count"], result["source_status"]), (2, 1, "success"))
            self.assertEqual((self.sleep.call_count, stats["actual"], stats["retries"]), (1, 2, 1))
        http = errors.HTTPError("500", response=response(500))
        calls, result, _ = self.attempt(errors.ConnectionError(), http)
        self.assertEqual((calls, result["retry_count"], result["source_status"]), (2, 1, "failed"))
        calls, result, _ = self.attempt(http, ok)
        self.assertEqual((calls, result.get("retry_count"), result["source_status"]), (1, 0, "failed"))


class TestTrack3Consumers(OfflineCase):
    def test_g07_collection_plan_and_same_round_pool_reuse_non_retried_http_failure_without_second_call(self):
        cache.start_request_cache_round("r53-plan")
        exc = errors.HTTPError("429", response=response(429))
        source = ScriptedSource(exc, exc)
        plan = collection_plan.CollectionPlan(subscription_count=1)
        plan.add_request(source, *ROUTE, persist=False)
        cache.activate_collection_plan(plan.request_keys)
        self.addCleanup(cache.deactivate_collection_plan)
        report = plan.execute()
        self.assertEqual((report.actual_requests, report.retries, source.calls), (1, 0, 1))
        self.assertTrue(any("计划恒等式=True" in line for line in self.logs))
        result, status, reuse = cache.cached_fetch(source, *ROUTE, persist=False, include_cache_details=True)
        self.assertEqual((source.calls, cache.get_request_cache_stats()["hits"]), (1, 1))
        self.assertEqual((status, reuse, result["retry_count"]), ("round_failed", "in_round_cache", 0))

    def test_g08_aggregator_and_source_degradation_handle_single_shot_http_failure_honestly(self):
        cache.start_request_cache_round("r53-aggregate")
        agg = aggregator.FlightAggregator([juhe_source.JuheSource()], [])
        with patch.dict(os.environ, {"JUHE_FLIGHT_KEY": "RAW_KEY"}), patch.object(
            requests, "get", return_value=response(502, "RAW_KEY")
        ) as get:
            agg.collect(*ROUTE, cabin_classes=["economy"], force_fresh=True)
        self.assertEqual(get.call_count, 1)
        self.assertEqual(len(agg.last_source_errors), 1)
        row = agg.last_source_errors[0]
        self.assertEqual((row["source"], row["error_type"], row["retry_count"]), ("juhe", "HTTPError", 0))
        self.assertNotIn("RAW_KEY", json.dumps(row))
        self.assertEqual([u["source_status"] for u in agg.last_fetch_evidence_units], ["failed"])
        ctx = notifier._build_source_degradation_context(
            source_stats=agg.last_source_stats, source_errors=agg.last_source_errors,
            last_snapshot={"channels": '["juhe"]'},
        )
        self.assertIs(ctx["active"], True)


def carriers(payload):
    _, email = notifier.render_email(deepcopy(payload))
    push = notifier.prepare_push_render(notifier.render_pushplus_sections(deepcopy(payload))).content
    return email, push, notifier.render_detail_html(deepcopy(payload))


class TestTrack4Guards(unittest.TestCase):
    def test_g09_scoped_ast_locks_protected_files_and_reversible_r51_r52_handoffs_are_exact(self):
        with TemporaryDirectory() as tmp, patch.object(config_loader, "RUNTIME_CONFIG_PATH", Path(tmp)/"absent.yaml"):
            for path, expected in FILES.items():
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
            src = (ROOT/"request_cache.py").read_text(encoding="utf-8")
            self.assertEqual(len(src) - len(src.rstrip("\n")), 1)
            old_cache = '    "request_cache.py": (52429, "806b2d9328b6ddddfb80c1e08a12e3779ca90c2450c1f4afda897c903075daea"),\n'
            old_r51 = '    "tests/test_r51_cross_midnight_window_contracts.py": (18827, "c65a408f9df56c3763eddd116bb5e2e50aadc5ee4fd589185812209381859e82"),\n'
            for path, insertions, expected in (
                ("tests/test_r51_cross_midnight_window_contracts.py", [(38, old_cache)],
                 (18827, "c65a408f9df56c3763eddd116bb5e2e50aadc5ee4fd589185812209381859e82")),
                ("tests/test_r52_unverified_layover_comfort_contracts.py", [(26, old_r51), (38, old_cache)],
                 (19185, "caab9761dff9d7d5565f8c8f382f2072bbc2be9b28053b9c531788a10cf02f8f")),
            ):
                lines = (ROOT/path).read_text(encoding="utf-8").splitlines(keepends=True)
                for idx, line in insertions:
                    self.assertNotIn(line, lines)
                    lines.insert(idx, line)
                restored = "".join(lines).encode("utf-8")
                self.assertEqual((len(restored), sha256(restored).hexdigest()), expected, path)

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
