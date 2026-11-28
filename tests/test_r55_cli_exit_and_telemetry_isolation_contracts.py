"""Offline contracts for batch exit status and best-effort notification telemetry."""
import ast
from datetime import date
from decimal import Decimal
from hashlib import sha256
import inspect
import json
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

ROOT = Path(__file__).resolve().parents[1]
ROUND = "collection_synthetic"
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
    "sources/searchapi_source.py": (4263, "9ab33d45c594bc61250911ab22f9ed42489688e1e6218a54c375dd0b1bd87d0d"),
    "sources/aggregator.py": (65083, "01fcf0d1d10b125f16ed635b86e4fc2cdec4e6a92e53463b15878e68083bce82"),
    "tests/fixtures/form_normalization_baseline_v1.json": (180179, "88172833002fbe747b289848341a85d4c23470070339a4775ce2f61f183a2fd9"),
    "tests/fixtures/frozen_email/economy_payload.json": (168149, "85ee48c5539020e3cec3e2ec444d9a7f9403c4c259213c7840750c7788eb275b"),
    "test_same_day_business_mode.py": (77107, "066ae51ff5c0c7500d5a8a992558bb037fa03e0b337e9ddbaa81c5349e8a2dcd"),
    "tests/test_r40_same_day_combo_contracts.py": (29469, "57e93660bb27ea80644f261f85b5900826bb0dc4e81c6e563bf6808b53133744"),
    "tests/test_r41_legitimate_empty_response_contracts.py": (44871, "0320f4d41a9836b8167daefa5fac2fc11e3d63eb17bad366304e7a20611d88f9"),
    "tests/test_r49_baggage_and_source_truth_contracts.py": (13764, "1c6db27b2d1484cd3c0fd1197e1f5ae0687fa177370b5ac92128b37aabc20b5e"),
    "tests/test_r50_history_window_and_juhe_timestamp_contracts.py": (16011, "42551bc267b7607a9d9b1310bb7a3700fc84e08212b7a41b8632c0b1d3330afb"),
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
    ("sources/searchapi_source.py", None): {
        "_missing_searchapi_key_result": "e6001610b4ad15127c4b6b1ff9f3d7875ee294d3db9727726228bdf097ac920e",
        "_searchapi_travel_class": "b090187531a9d0b84dc10104c4cc407adc33bd460c145ac4b58ef64e96d8fd79",
        "_searchapi_stops_value": "ce5e794ef8acfbf8cc6c0f9a278196580ad60ec78c3f5346b87662cf96164bad",
        "_redact_url": "076dd2353388eafd5cbf133716f118c3ca77192f2ae1ebe7b11887eed01928ed",
    },
    ("sources/searchapi_source.py", "SearchAPISource"): {
        "preflight_skip": "193890e2070b2869b50c426ec5067250e25acd11877fcbd0ade159110d946af1",
        "fetch": "2e01c30e890c3bad87f3df510dcd4a4f24501d6c4904100a4ea3164e7973d778",
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
    ("main.py", None): {
        "_notification_tcurve": "fdf270638c3a9e566b39aaf584534881366b83dc29bdfc5e23e62cdd8978301a",
        "_notification_forecast": "c443006b7c9692459a8cb2afa72ef7aa9a075f576ce899336c9a855fbed3623f",
        "_notification_provenance_context": "0062d35fd0b7f64e6d8f2b9b5b9e48ee34fd6f1c8b29f4cb8e018f3600be4bba",
        "_deliver_notification": "f2d8c073a3271dadde15f04ec1687a6a8d8f0e6070e4eefc5811d9cfef1e55c3",
        "_process_scheduled_subscription": "62049ca63069c7146bc2705f65677e54060d94399d7969a3dc3374e275bfebb7",
        "load_file_subscriptions": "0fd28d630849bf786d7a892fd90bc3a1ff3e4a0d9a32a513d45a98a0e161f390",
    },
}
INSPECT = {
    "parse_flight_time": "9a55d7e7d427adf2469b269546262ebc3d3264ec7f7cae4b7df537f1a4177297",
    "match_time_preference": "2e21c6f1cb34a5300b194c59b167b52753e321f2d7e696685c153f64c98af93f",
}
HANDOFFS = {
    "tests/test_r51_cross_midnight_window_contracts.py": (
        (18442, "204bae3d510dc2605c903a1d86ee31bf157a5b62c39565dc5de1cc27307debb7"),
        (18349, "2d916d6f377e501b3000353d5db8ac06d976afc3767ac2dcf3d5ad0f772f1ae7"),
        [
            (28, "    \"main.py\": (175530, \"878a6df6d84276cde4f092f42d1c0453d08c5a8ceafdc638a68dc0561c4c13ef\"),\n"),
        ],
    ),
    "tests/test_r52_unverified_layover_comfort_contracts.py": (
        (18666, "a9f4c69fa16deba2a4f442c5f3acda5d5172d03d38027f745c9c3046f21952e2"),
        (18573, "0e6b727b3207884ee7a5bd29d8a17f0dea86243fb15d0ed0782d5c7743f50dc6"),
        [
            (27, "    \"main.py\": (175530, \"878a6df6d84276cde4f092f42d1c0453d08c5a8ceafdc638a68dc0561c4c13ef\"),\n"),
        ],
    ),
    "tests/test_r53_http_retry_classification_contracts.py": (
        (19263, "d846a68d0a3d110662312c66b353f07265d2b0c384086686d1a513cef8754a40"),
        (19170, "d29644b0d042bedc7ffe69135c0ca0a1f1cd4e05106434db93033112b79f8b55"),
        [
            (36, "    \"main.py\": (175530, \"878a6df6d84276cde4f092f42d1c0453d08c5a8ceafdc638a68dc0561c4c13ef\"),\n"),
        ],
    ),
    "tests/test_r54_searchapi_source_contracts.py": (
        (30234, "0163f40c96131d50fe6186b280d6b1ed25d2c0401267ebc749b230c1faea4bde"),
        (28641, "df8f5e241eeaa9d90e16bf8985235e83be971c1be44304175b330a4ef4a7d95a"),
        [
            (33, "    \"main.py\": (175530, \"878a6df6d84276cde4f092f42d1c0453d08c5a8ceafdc638a68dc0561c4c13ef\"),\n"),
            (440, "\n"),
            (441, "        restored = {}\n"),
            (442, "        for path, (before, after, insertions) in HANDOFFS.items():\n"),
            (443, "            text = (ROOT/path).read_text(encoding=\"utf-8\")\n"),
            (444, "            b = text.encode(\"utf-8\")\n"),
            (445, "            self.assertEqual((len(b), sha256(b).hexdigest()), after, path)\n"),
            (446, "            lines = text.splitlines(keepends=True)\n"),
            (447, "            for idx, line in insertions:\n"),
            (448, "                lines.insert(idx, line)\n"),
            (449, "            b = \"\".join(lines).encode(\"utf-8\")\n"),
            (450, "            self.assertEqual((len(b), sha256(b).hexdigest()), before, path)\n"),
            (451, "            restored[path] = lines\n"),
            (452, "        old_cache = '    \"request_cache.py\": (52429, \"806b2d9328b6ddddfb80c1e08a12e3779ca90c2450c1f4afda897c903075daea\"),\\n'\n"),
            (453, "        old_r51 = '    \"tests/test_r51_cross_midnight_window_contracts.py\": (18827, \"c65a408f9df56c3763eddd116bb5e2e50aadc5ee4fd589185812209381859e82\"),\\n'\n"),
            (454, "        for path, insertions, expected in (\n"),
            (455, "            (\"tests/test_r51_cross_midnight_window_contracts.py\", [(38, old_cache)],\n"),
            (456, "             (18827, \"c65a408f9df56c3763eddd116bb5e2e50aadc5ee4fd589185812209381859e82\")),\n"),
            (457, "            (\"tests/test_r52_unverified_layover_comfort_contracts.py\", [(26, old_r51), (38, old_cache)],\n"),
            (458, "             (19185, \"caab9761dff9d7d5565f8c8f382f2072bbc2be9b28053b9c531788a10cf02f8f\")),\n"),
            (459, "        ):\n"),
            (460, "            lines = restored[path]\n"),
            (461, "            for idx, line in insertions:\n"),
            (462, "                lines.insert(idx, line)\n"),
            (463, "            b = \"\".join(lines).encode(\"utf-8\")\n"),
            (464, "            self.assertEqual((len(b), sha256(b).hexdigest()), expected, path)\n"),
        ],
    ),
}


class OfflineCase(unittest.TestCase):
    def setUp(self):
        with patch("logging.basicConfig"), patch("dotenv.load_dotenv"):
            import main
        self.main = main
        self.root = Path(self.enterContext(TemporaryDirectory()))
        self.path = self.root / "subscriptions.json"
        self.path.write_text("[]", encoding="utf-8")
        self.log = self.replace("safe_log")
        self.init = self.replace("init_db")
        self.alert = self.replace("alert_subscription_input_missing")
        self.replace("SUBSCRIPTIONS_PATH", self.path)
        self.gate = Mock(acquired=True, holder={})
        self.replace("acquire_collection_singleflight", Mock(return_value=self.gate))
        self.replace("_make_collection_round_id", Mock(return_value=ROUND))
        self.enterContext(patch("collection_singleflight.safe_log"))
        self.replace("_shanghai_today", Mock(return_value=date(2026, 9, 30)))
        self.sub = {"origin": "PVG", "destination": "KIX", "depart_date": "2026-10-01",
                    "route_type": "international", "cabin_classes": ["economy"]}

    def replace(self, name, obj=None):
        return self.enterContext(patch.object(self.main, name, Mock() if obj is None else obj))

    def batch(self, subscriptions):
        for name in ("_run_basket_sentinel_for_main", "start_round_log_archive", "end_round_log_archive",
                     "activate_collection_plan", "deactivate_collection_plan", "start_request_cache_round",
                     "set_current_round", "reset_current_round", "print_request_cache_stats",
                     "log_retention_dry_run", "_log_preflight_skip", "record_subscription_attempt"):
            self.replace(name)
        self.replace("_collection_plan_log_options", Mock(return_value={}))
        self.replace("build_collection_plan", Mock(return_value=Mock(
            request_keys=set(), panel_only_keys=set(), fresh_scope="primary_only", freshness_hours=6)))
        self.preflight = self.replace("evaluate_subscription_preflight", Mock(return_value={"skip": False}))
        return self.replace("load_file_subscriptions", Mock(return_value=subscriptions))

    def telemetry(self, extra=None):
        from tests.test_return_phase_processing import flight
        self.replace("FlightAggregator", Mock(return_value=SimpleNamespace(last_source_errors=[])))
        for name, value in (
            ("build_default_sources", ([], [])), ("get_constraint_epoch_boundary", None),
            ("get_constraint_history_limit", 14), ("get_previous_snapshot_prices", {}),
            ("get_lowest_price_history", []), ("collect_nearby_dates", []), ("log_signal", {}),
            ("_notification_tcurve", {}), ("_notification_provenance_context", {}), ("_notification_forecast", {}),
        ):
            self.replace(name, Mock(return_value=value))
        self.replace("save_raw_response")
        self.replace("save_flight_details")
        self.replace("collect_for_airport_matrix", Mock(side_effect=lambda *a, **kw: {
            "flights": [flight(outbound=True)], "collected_at": "2026-09-30T10:00:00+08:00"}))
        self.replace("analyze_all_flights", Mock(side_effect=lambda *a, **kw: dict(extra or {})))
        self.analysis_log = self.root / "analysis.jsonl"
        self.replace("ANALYSIS_LOG", self.analysis_log)
        self.delivery = self.replace("_deliver_notification", Mock(return_value=True))
        self.failure = self.replace("_log_subscription_failure")
        self.health = self.replace("system_health_check", Mock(return_value={"ok": True}))

    def process(self):
        return self.main._process_subscription_locked(self.sub, ensure_db=False,
            preflight_result={"skip": False}, manage_collection_round=False, collection_round_id=ROUND)

    def break_log(self):
        dest = Mock()
        dest.open.side_effect = PermissionError("locked token=LOG_SEC_2")
        self.replace("ANALYSIS_LOG", dest)
        return dest

    def messages(self):
        return [c.args[0] for c in self.log.call_args_list]


class TestTrack1CliExitAndBatchStatus(OfflineCase):
    def test_g01_cli_exit_mapper_and_singleflight_busy_return_distinct_nonzero_code(self):
        m = self.main
        for name, code in (("EXIT_OK", 0), ("EXIT_BATCH_FAILED", 1), ("EXIT_SINGLEFLIGHT_BUSY", 6)):
            self.assertEqual(getattr(m, name, None), code)
        self.assertTrue(hasattr(m, "_cli_exit_code_from_run_result"))
        for result, code in (({"status": "ok"}, 0), (None, 0), (True, 0), (Mock(), 0),
                             ({"status": "busy"}, 6), ({"status": "failed"}, 1),
                             ({"status": "partial"}, 1), (False, 1)):
            self.assertEqual(m._cli_exit_code_from_run_result(result), code)
        self.gate.acquired = False
        self.assertEqual(m.run_cli(sync_remote=False), 6)
        self.assertIn("[计划任务] 本轮因单飞锁占用跳过 status=busy exit_code=6", self.messages())
        self.init.assert_not_called()
        self.alert.assert_not_called()

    def test_g02_run_and_run_cli_propagate_batch_ok_partial_failed_and_preflight_failures_while_run_locked_returns_none(self):
        loader = self.batch([self.sub, self.sub])
        process = self.replace("_process_scheduled_subscription")
        self.replace("_log_subscription_failure")

        def verify(status, checked, skipped, invalid, success, failed):
            result = self.main.run(sync_remote=False)
            self.assertIsInstance(result, dict)
            self.assertEqual(result, dict(status=status, round_id=ROUND, checked=checked, skipped=skipped,
                                         preflight_failed=invalid, success=success, failed=failed, exceptions=0))
            self.log.reset_mock()
            expected = 0 if status == "ok" else 1
            self.assertEqual(self.main.run_cli(sync_remote=False), expected)
            self.assertEqual([s for s in self.messages() if s.startswith("[计划任务]")],
                             [] if not expected else [f"[计划任务] 批次未全部成功 status={status} exit_code=1"])

        for outcomes, status in (([True, True], "ok"), ([True, False], "partial"), ([False, False], "failed")):
            process.side_effect = outcomes * 2
            verify(status, 2, 0, 0, outcomes.count(True), outcomes.count(False))
        self.preflight.side_effect = ValueError("synthetic preflight")
        verify("failed", 2, 0, 2, 0, 0)
        self.preflight.side_effect = None
        self.preflight.return_value = {"skip": True}
        verify("ok", 2, 2, 0, 0, 0)
        loader.return_value = []
        verify("ok", 0, 0, 0, 0, 0)
        self.assertIsNone(self.main._run_locked(sync_remote=False, round_id=ROUND))

    def test_g03_sync_subscriptions_main_propagates_run_cli_busy_and_batch_failure_codes(self):
        import sync_subscriptions as sync
        self.enterContext(patch.object(sync, "LOCAL_SUBSCRIPTIONS", self.path))
        self.enterContext(patch.object(sync, "sync_subscriptions"))
        self.enterContext(patch.object(sync, "safe_log"))
        alert = self.enterContext(patch.object(sync, "alert_subscription_input_missing"))
        self.gate.acquired = False
        self.assertEqual(sync.main(), 6)
        self.gate.acquired = True
        self.batch([self.sub, self.sub])
        process = self.replace("_process_scheduled_subscription")
        for outcomes, code in (([False, False], 1), ([True, False], 1), ([True, True], 0)):
            process.side_effect = outcomes
            self.assertEqual(sync.main(), code)
        self.path.unlink()
        self.assertEqual(sync.main(), 2)
        alert.assert_called_once_with(self.path)


class TestTrack2TelemetryAndAnalysisLogIsolation(OfflineCase):
    def test_g04_system_health_check_exception_is_isolated_redacted_and_does_not_abort_delivery(self):
        self.telemetry()
        self.health.side_effect = RuntimeError("boom api_key=HEALTH_SEC_1")
        self.assertIs(self.process(), True)
        self.delivery.assert_called_once()
        self.assertEqual(self.delivery.call_args.args[2]["analysis_result"]["system_health"], {})
        self.failure.assert_not_called()
        self.assertIn("[健康检查] 执行失败 跳过附着 原因=RuntimeError:boom api_key=***", self.messages())
        self.assertNotIn("HEALTH_SEC_1", str(self.messages()))

    def test_g05_analysis_log_serializes_non_json_types_with_default_str_and_preserves_delivery(self):
        extras = {"day": date(2026, 12, 1), "amount": Decimal("1688.50"), "path": Path("synthetic/path")}
        self.telemetry(extras)
        self.analysis_log.write_text('{"previous":true}\n', encoding="utf-8")
        self.assertIs(self.process(), True)
        previous, record = map(json.loads, self.analysis_log.read_text(encoding="utf-8").splitlines())
        self.assertEqual(previous, {"previous": True})
        self.assertEqual({k: record[k] for k in extras}, {k: str(v) for k, v in extras.items()})
        self.assertEqual(record["system_health"], {"ok": True})
        self.assertTrue(record["logged_at"])
        self.delivery.assert_called_once()
        self.failure.assert_not_called()

    def test_g06_analysis_log_io_error_is_isolated_redacted_and_does_not_abort_delivery(self):
        self.telemetry()
        destination = self.break_log()
        self.assertIs(self.process(), True)
        destination.open.assert_called_once_with("a", encoding="utf-8")
        self.delivery.assert_called_once()
        self.failure.assert_not_called()
        self.assertIn("[分析日志] 写入失败 跳过落盘 原因=PermissionError:locked token=***", self.messages())
        self.assertNotIn("LOG_SEC_2", str(self.messages()))


class TestTrack3LowPriceAndMutationCompatibility(OfflineCase):
    def test_g07_low_price_quiet_skip_path_survives_simultaneous_health_and_analysis_log_failures(self):
        self.telemetry()
        self.sub["hard_constraints"] = {"budget_strategy": "low_price_alert"}
        self.replace("build_current_low_price_offer", Mock(return_value={
            "can_evaluate_current": False, "state": "not_triggered", "reason": "above_threshold"}))
        skip = self.replace("_should_skip_low_price_alert", Mock(wraps=self.main._should_skip_low_price_alert))
        self.health.side_effect = OSError("health unavailable")
        destination = self.break_log()
        self.assertIs(self.process(), True)
        self.health.assert_called_once()
        destination.open.assert_called_once()
        skip.assert_called_once()
        self.assertIn("[推送] 按低价提醒策略静默结束（未发送完整行情通知）", self.messages())
        self.delivery.assert_not_called()
        self.failure.assert_not_called()

    def test_g08_end_to_end_run_cli_distinguishes_isolated_telemetry_fault_from_true_delivery_failure_and_keeps_mutations(self):
        from tests import test_batch_round_status as batch_guard
        self.batch([self.sub])
        self.telemetry()
        self.health.side_effect = OSError("health unavailable")
        self.break_log()
        for accepted, code in ((True, 0), (False, 1)):
            self.delivery.reset_mock()
            self.failure.reset_mock()
            self.delivery.return_value = accepted
            self.assertEqual(self.main.run_cli(sync_remote=False), code)
            self.delivery.assert_called_once()
            self.assertEqual(self.failure.call_count, int(not accepted))
        result = batch_guard.BatchRoundStatusMutationTest("test_c_mutations_hit_target_assertions").run()
        self.assertEqual(result.testsRun, 1)
        self.assertTrue(result.wasSuccessful(), result.errors + result.failures)


class TestTrack4Guards(unittest.TestCase):
    def test_g09_protected_files_scoped_ast_locks_main_invariants_and_reversible_handoffs_are_exact(self):
        import analyzer
        from tests import test_r54_searchapi_source_contracts as r54

        def digest(text):
            b = text.encode("utf-8")
            return len(b), sha256(b).hexdigest()

        self.assertEqual(len(PROTECTED_FILES), 25)
        for path, expected in PROTECTED_FILES.items():
            self.assertEqual(digest((ROOT/path).read_text(encoding="utf-8")), expected, path)
        self.assertEqual(sum(map(len, SCOPES.values())), 53)
        for (path, cls), locks in SCOPES.items():
            src = (ROOT/path).read_text(encoding="utf-8")
            nodes = ast.parse(src).body
            if cls:
                nodes = next(n for n in nodes if isinstance(n, ast.ClassDef) and n.name == cls).body
            for name, expected in locks.items():
                node, = [n for n in nodes if isinstance(n, ast.FunctionDef) and n.name == name]
                self.assertEqual(digest(ast.get_source_segment(src, node))[1], expected, name)
        for name, expected in INSPECT.items():
            self.assertEqual(digest(inspect.getsource(getattr(analyzer, name)))[1], expected, name)
        src = (ROOT/"main.py").read_text(encoding="utf-8")
        self.assertEqual(len(src) - len(src.rstrip("\n")), 1)
        tree = ast.parse(src)
        calls = [n for n in ast.walk(tree) if isinstance(n, ast.Call) and
                 isinstance(n.func, ast.Name) and n.func.id == "start_request_cache_round"]
        self.assertEqual(len(calls), 2)
        for n in calls:
            self.assertTrue({"workload_class", "entrypoint"} <= {kw.arg for kw in n.keywords})
        guard = ast.parse('if __name__ == "__main__":\n    raise SystemExit(run_cli())').body[0]
        self.assertEqual(ast.dump(tree.body[-1]), ast.dump(guard))

        rebuilt = {}
        for path, (before, after, insertions) in HANDOFFS.items():
            text = (ROOT/path).read_text(encoding="utf-8")
            self.assertEqual(digest(text), after, path)
            lines = text.splitlines(keepends=True)
            for idx, line in insertions:
                lines.insert(idx, line)
            rebuilt[path] = "".join(lines)
            self.assertEqual(digest(rebuilt[path]), before, path)

        restored = {}
        for path, (before, after, insertions) in r54.HANDOFFS.items():
            text = rebuilt[path] if path in rebuilt else (ROOT/path).read_text(encoding="utf-8")
            b = text.encode("utf-8")
            self.assertEqual((len(b), sha256(b).hexdigest()), after, path)
            lines = text.splitlines(keepends=True)
            for idx, line in insertions:
                lines.insert(idx, line)
            b = "".join(lines).encode("utf-8")
            self.assertEqual((len(b), sha256(b).hexdigest()), before, path)
            restored[path] = lines
        old_cache = '    "request_cache.py": (52429, "806b2d9328b6ddddfb80c1e08a12e3779ca90c2450c1f4afda897c903075daea"),\n'
        old_r51 = '    "tests/test_r51_cross_midnight_window_contracts.py": (18827, "c65a408f9df56c3763eddd116bb5e2e50aadc5ee4fd589185812209381859e82"),\n'
        for path, insertions, expected in (
            ("tests/test_r51_cross_midnight_window_contracts.py", [(38, old_cache)],
             (18827, "c65a408f9df56c3763eddd116bb5e2e50aadc5ee4fd589185812209381859e82")),
            ("tests/test_r52_unverified_layover_comfort_contracts.py", [(26, old_r51), (38, old_cache)],
             (19185, "caab9761dff9d7d5565f8c8f382f2072bbc2be9b28053b9c531788a10cf02f8f")),
        ):
            lines = restored[path]
            for idx, line in insertions:
                lines.insert(idx, line)
            b = "".join(lines).encode("utf-8")
            self.assertEqual((len(b), sha256(b).hexdigest()), expected, path)

    def test_g10_frozen_three_carriers_and_normal_roundtrip_baseline_remain_exact(self):
        from tests import test_r54_searchapi_source_contracts as r54
        result = r54.TestTrack4Guards("test_g10_frozen_three_carriers_and_normal_roundtrip_baseline_remain_exact").run()
        self.assertEqual(result.testsRun, 1)
        self.assertTrue(result.wasSuccessful(), result.errors + result.failures)
