"""Positive connection evidence for comfort scoring and passenger-facing advice."""

import ast
from contextlib import redirect_stdout
from copy import deepcopy
from hashlib import sha256
import inspect
import io
import json
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch

import analyzer
import config_loader
import notifier
from tests import test_r40_same_day_combo_contracts as r40
from tests import test_return_phase_processing as scenario


ROOT = Path(__file__).resolve().parents[1]
PENDING = "中转衔接时间待确认，请核对航段详情"
ACCEPTABLE = "合理中转可接受"
ADULT = {"red_eye": "accept", "allow_self_transfer": True}
FILES = {
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
        "comfort_score": "cb8973e4bd46b111364a272ec394ea3cb8f84c87fa8dc0bb3413340b96ed16f8",
        "_max_layover_minutes": "4e4c727aa34e7c9bf8223796a76c95b0d72fe6b27e9859f44d952ab07d8e2918",
        "evaluate_connection_safety": "a8dfd3dd3f3d4a109076352b8f9695721e0b771bb7eec6021bffdf1591715504",
        "_minutes_to_text": "4fbb12f15cc6d847e37f7200ae2279cbd50e2314b67bc67d93f160bcfa3fe87d",
        "_parse_window_text_minutes": "c7fd8a37feff2dd3aeaa8df8d23b64953bc9b404dedd4b92347c02d662908aac",
        "_window_datetime_from_minutes_or_text": "8277a3554e5a960008e261ebcf944bd3a9ac6d77ff4bc9491a43ecd32359b791",
        "_relative_time_text": "b37eec6d355968bb11594880218c1799d264fe6c5152d3a2e230886a579b4f16",
        "verify_fare_rules": "87ddfdf2bfdfc728057901b36751a544287568ef6f54ba1d8a14638daf43e082",
        "parse_flight_time": "dfbff457554895bf182c1c80900ac82f64c94d7aeaff28189ce547c85c31069f",
        "overall_score": "c762984d55d286c49e4f69387290c131161c5e8ebdcfaee796fdcf1f38296745",
        "calc_transfer_risk": "660cb5ea9a31f053122f705765a3299969f0bd14555c7d65509e5d3ad982e829",
        "_min_layover_minutes": "334a2adbf312e3a90fff48d7ac830900efedd271e83db62a5b07820240dd04fe",
        "match_time_preference": "ce0bbaf1024f6d98e16271b3580a5d7b55c8b0ebda1fcbdb636e9163f5f4d5ce",
        "conditional_percentile": "ae63ab43033c209f0c7cd43996373cc62b9b21ebf28a85bbbfd90e4c47df234b",
        "calculate_price_references": "2bed9302d3906bde5056ac12f301b32313adbe4776ec0665757a7f5e970fcc83",
        "analyze_roundtrip_prices": "dc560ba48f9920a2d49eaae3c3e93b652afa58db50943555e667f36a31484c1f",
        "_minutes_datetime": "5db57eb07bab030345e56b18e39187bb3bb3798fd160893890329f5d4353388a",
        "_assert_dated_datetime": "2894d2ad899d49e7734fd9e94c0507b2996c8f7f8a7d323c8d85fe35b0d1600e",
        "_same_day_outbound_passes_window": "021bf32d509b8d4290189bfeb344e5679dd4b773b87d3d035e16d58e79c94d16",
        "_same_day_return_passes_window": "cd498b9394640c67b82989c5ec3caa6b369ba083481dcc570ea4c951b8250eda",
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



def wait(minutes, **extra):
    return {"wait_minutes": minutes, **extra}


def flight(layovers, stops=1, number="MU501"):
    return {
        "flight_combo": number, "price": 800, "currency": "CNY", "cabin_class": "economy",
        "source": "juhe", "data_source": "juhe", "airline": "XX", "airlines": ["XX"],
        "departure_airport": "PVG", "arrival_airport": "KIX", "departure_date": "2026-12-01",
        "departure_time": "09:00", "arrival_time": "17:00", "total_duration_min": 480,
        "stops": stops, "layovers": layovers, "segments": [], "route_summary": "PVG-ICN-KIX",
        "collected_at": "2026-11-26T12:00:00+08:00",
    }


def with_segments(row):
    return dict(row, segments=[
        dict(flight_no=row["flight_combo"], airline="XX", dep_airport="PVG", arr_airport="ICN",
             dep_time="2026-12-01 09:00", arr_time="2026-12-01 11:00"),
        dict(flight_no="XX310", airline="XX", dep_airport="ICN", arr_airport="KIX",
             dep_time="2026-12-01 14:20", arr_time="2026-12-01 17:00"),
    ])


def carriers(payload):
    _, email = notifier.render_email(deepcopy(payload))
    push = notifier.prepare_push_render(notifier.render_pushplus_sections(deepcopy(payload))).content
    return email, push, notifier.render_detail_html(deepcopy(payload))


class ContractCase(r40.ContractBase):
    def checked(self, function, *args, **kwargs):
        try:
            return function(*args, **kwargs)
        except (TypeError, ValueError, OverflowError, AttributeError) as exc:
            self.fail(f"unexpected runtime error: {exc!r}")

    def apply(self, row, **prefs):
        return self.checked(analyzer._apply_user_preferences, [row], {**ADULT, **prefs})


class TestTrack1Comfort(ContractCase):
    def test_g01_direct_and_verified_comfort_preserved_while_unknown_ranks_strictly_lower(self):
        score = analyzer._flight_comfort_score
        direct = flight([], stops=0)
        self.assertEqual(score(direct), 85)
        self.assertEqual(score(dict(direct, airlines=["Air China"])), 95)
        for minutes, expected in ((60, 65), (120, 65), (180, 65), (181, 55), (240, 55),
                                  (480, 55), (481, 40), (540, 40)):
            self.assertEqual(score(flight([wait(minutes)])), expected, minutes)
        self.assertLess(score(flight([])), score(flight([wait(120)])))
        self.assertEqual(score(flight([])), score(flight([wait(240)])))
        self.assertEqual(score(with_segments(direct)), 55)

    def test_g02_all_unverified_malformed_and_nonpositive_layover_shapes_get_zero_short_wait_bonus(self):
        score = analyzer._flight_comfort_score
        missing = flight([])
        del missing["layovers"]
        self.assertEqual(score(missing), 55)
        for rows in ([], [{}], [None], *([wait(v)] for v in
                     (None, 0, -5, "bad", True, float("nan"), float("inf"), float("-inf"))),
                     [wait(120, wait_unverified_reason="timestamp_missing")],
                     [wait(120, wait_unverified_reason="segment_time_inverted")]):
            self.assertEqual(self.checked(score, flight(rows)), 55, repr(rows))
        for airport, next_airport, expected in (("ICN", "HKG", 55), ("HKG", "ICN", 55), ("icn", "ICN", 65)):
            row = with_segments(flight([wait(90, airport=airport, next_airport=next_airport)]))
            self.assertEqual(score(row), expected)
        for rows in ([wait(90)], [wait(90), {}]):
            self.assertEqual(score(flight(rows, stops=2)), 35)
        self.assertEqual(score(with_segments(flight([wait(90), wait(90)]))), 55)

    def test_g03_confirmed_long_layover_penalty_is_preserved_even_with_other_unknown_leg(self):
        for rows in ([wait(540), {}], [None, wait(600)]):
            self.assertEqual(self.checked(analyzer._flight_comfort_score, flight(rows, stops=2)), 20)
        inverted = wait(600, wait_unverified_reason="segment_time_inverted")
        for n, expected in ((1, 55), (2, 35)):
            self.assertEqual(analyzer._flight_comfort_score(flight([inverted], stops=n)), expected)


class TestTrack2Preferences(ContractCase):
    def test_g04_ordinary_adult_keeps_unverified_transfer_with_pending_note_instead_of_reasonable_claim(self):
        for policy in ("reasonable", "short_ok"):
            for rows, verified in (([], False), ([wait(0)], False),
                                   ([wait(130, wait_unverified_reason="timestamp_missing")], False),
                                   ([wait(120)], True)):
                kept, excluded, info = self.apply(flight(rows), transfer_policy=policy)
                self.assertEqual((len(kept), excluded, info["reference_flights"]), (1, [], []))
                self.assertEqual(ACCEPTABLE in kept[0]["preference_notes"], verified)
                self.assertEqual(PENDING in kept[0]["preference_notes"], not verified)

    def test_g05_ordinary_adult_long_wait_and_malformed_layovers_align_on_valid_wait_number_while_safety_gates_hold(self):
        for profile in (dict(companions="with_child", passengers={"adult": 1, "child": 1}),
                        dict(travel_scenario="business")):
            kept, excluded, info = self.apply(with_segments(flight([])), **profile)
            self.assertEqual((kept, excluded, len(info["reference_flights"])), ([], [], 1))
            self.assertEqual(info["reference_flights"][0]["filter_reason_code"], "connection_time_unverified")
            kept, excluded, info = self.apply(with_segments(flight([wait(30)])), **profile)
            self.assertEqual((kept, len(excluded), info["reference_flights"]), ([], 1, []))
            self.assertIn("中转", excluded[0]["exclude_reason"])
        kept, excluded, _ = self.apply(flight([wait(540), {}], stops=2), allow_overnight_transfer=False)
        self.assertEqual((kept, [x["exclude_reason"] for x in excluded]), ([], ["系统默认不推荐过夜中转"]))
        kept, excluded, _ = self.apply(flight([wait(300), {}], stops=2), avoid_long_layover=True)
        self.assertEqual((len(kept), excluded), (1, []))
        self.assertIn("同行约束：中转等待偏长", kept[0]["preference_penalties"])
        for rows in ([None], *([wait(v)] for v in ("bad", True, float("nan"), float("inf"), float("-inf"))),
                     [wait(600, wait_unverified_reason="segment_time_inverted")]):
            kept, excluded, info = self.apply(flight(rows), allow_overnight_transfer=False, avoid_long_layover=True)
            self.assertEqual((len(kept), excluded, info["reference_flights"]), (1, [], []))
            self.assertNotIn("同行约束：中转等待偏长", kept[0]["preference_penalties"])

    def test_g06_make_domestic_tags_requires_verified_low_risk_for_single_stop_tag(self):
        for category in ("has_child", "has_elderly"):
            profile = {"passenger_profile": {category: True}}
            for stops, rows, expected in ((0, [], True), (1, [wait(130)], True), (1, [], False),
                                         (1, [{}], False), (1, [None], False), (1, [wait(60)], False),
                                         (1, [wait(130, wait_unverified_reason="timestamp_missing")], False)):
                row = dict(flight(rows, stops=stops), departure_time="05:00", route_type="domestic",
                           departure_airport="PVG", arrival_airport="PEK",
                           route_summary="PVG-WUH-PEK" if stops else "PVG-PEK",
                           fare_rules={"baggage": {"included": False}})
                tags = self.checked(analyzer.make_domestic_tags, row, profile=profile, lowest_price=100)
                self.assertEqual("中转风险低" in tags, expected, (category, rows, tags))


class TestTrack3Delivery(ContractCase):
    def pair(self, a, b):
        result = analyzer.analyze_all_flights([a, b], user_preferences=ADULT)
        rows = {row["flight_combo"]: row for row in result["all_flights"]}
        self.assertEqual(set(rows), {"MU501", "MU502"})
        self.assertEqual(result.get("reference_flights", []), [])
        return result, rows["MU501"], rows["MU502"]

    def test_g07_analyze_all_flights_keeps_juhe_unverified_transfer_and_ranks_verified_short_transfer_ahead(self):
        _, unknown, verified = self.pair(
            with_segments(flight([])), with_segments(flight([wait(120, airport="ICN")], number="MU502")))
        self.assertEqual(verified["score_components"]["comfort"] - unknown["score_components"]["comfort"], 10)
        self.assertGreater(verified["final_score"], unknown["final_score"])
        self.assertIn(PENDING, unknown["preference_notes"])
        self.assertNotIn(ACCEPTABLE, unknown["preference_notes"])

        no_segments = flight([], number="MU503")
        self.assertEqual(no_segments["segments"], [])
        control = with_segments(flight([wait(120, airport="ICN")], number="MU502"))
        result = analyzer.analyze_all_flights([no_segments, control], user_preferences=ADULT)
        self.assertEqual([r["flight_combo"] for r in result["reference_flights"]], ["MU503"])
        self.assertEqual([r["flight_combo"] for r in result["all_flights"]], ["MU502"])

    def test_g08_end_to_end_pipeline_and_three_carriers_do_not_let_unverified_timestamp_beat_verified_medium_layover(self):
        a = with_segments(flight([wait(130, airport="ICN", wait_unverified_reason="timestamp_missing")]))
        b = with_segments(flight([wait(200, airport="ICN")], number="MU502"))
        result, unknown, verified = self.pair(a, b)
        self.assertEqual(unknown["score_components"]["comfort"], 55)
        self.assertLessEqual(unknown["final_score"], verified["final_score"])
        self.assertIn(PENDING, unknown["preference_notes"])
        payload = notifier.build_notification_payload(result, route_info={
            "origin": "PVG", "destination": "KIX", "depart_date": "2026-12-01", "round_trip": False,
        })
        for body in carriers(payload):
            self.assertIn("中转1次", body)


class TestTrack4Guards(unittest.TestCase):
    def test_g09_scoped_ast_locks_inspect_hashes_and_protected_files_stay_exact(self):
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
            src = (ROOT/"analyzer.py").read_text(encoding="utf-8")
            self.assertEqual(len(src) - len(src.rstrip("\n")), 7)

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
