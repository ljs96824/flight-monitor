"""R34：低价提醒四态（triggered/not_triggered/unknown/not_applicable）与一次性能力提示。

合同（2026-11-09 维护者裁定）：
- R35：没有本地同口径历史的往返低价策略为 unknown，
  不用去程单程阈值触发往返低价提醒。
- 非低价策略与返程阶段显式 not_applicable；返程阶段不重启低价判断。
- unknown 的旧布尔 low_price_alert_triggered 必须为 False，不得改成 True。
- 能力提示按（持久化 subscription_id, 规范化配置哈希）去重；哈希不含报价、
  样本数、采集时间、round_id、发送结果或密钥；配置变更重新提示一次。
- 发送失败不记录、下轮重试；身份缺失不使用共同空键去重、不发送、不记录。
"""
import json
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch

import analyzer
from analyzer import analyze_all_flights
import main


def make_flight(combo, price):
    return {
        "flight_combo": combo,
        "cabin_class": "economy",
        "price": price,
        "departure_time": "2026-12-01 08:15",
        "arrival_time": "2026-12-01 10:25",
        "total_duration_min": 130,
        "stops": 0,
        "layovers": [],
        "segments": [
            {
                "flight_no": combo,
                "dep_airport": "PVG",
                "arr_airport": "KIX",
                "dep_time": "2026-12-01 08:15",
                "arr_time": "2026-12-01 10:25",
            }
        ],
    }


def make_insights(points, low=600.0):
    history = [[f"2026-10-{10 + index:02d}", low + index * 50] for index in range(points)]
    return {"price_history": history, "lowest_price": low}


LOW_PRICE_HARD = {"budget_strategy": "low_price_alert"}


def run_analysis(trip_type, points=6, lowest=550.0, hard=None, preferences=None):
    flights = [make_flight("MU100", lowest), make_flight("CA200", lowest + 200)]
    return analyze_all_flights(
        flights,
        make_insights(points),
        mode="balanced",
        user_preferences=preferences or {},
        hard_constraints=hard if hard is not None else LOW_PRICE_HARD,
        low_price_trip_type=trip_type,
    )


class AnalyzerStateTest(unittest.TestCase):
    def test_roundtrip_unknown_despite_sufficient_history_and_low_price(self):
        result = run_analysis("round_trip", points=6, lowest=550.0)
        self.assertEqual(result["low_price_alert_state"], "unknown")
        self.assertEqual(result["low_price_alert_reason"], "history_unavailable")
        self.assertFalse(result["low_price_alert_triggered"])

    def test_one_way_triggered_unchanged(self):
        result = run_analysis("one_way", points=6, lowest=550.0)
        analyzer.apply_low_price_evaluation(result, {"state": "triggered", "reason": "entered_low_zone",
                                                     "threshold_cny": 600, "prior_n": 5})
        self.assertEqual(result["low_price_alert_state"], "triggered")
        self.assertEqual(result["low_price_alert_reason"], "entered_low_zone")
        self.assertTrue(result["low_price_alert_triggered"])

    def test_one_way_not_triggered(self):
        result = run_analysis("one_way", points=6, lowest=5000.0)
        analyzer.apply_low_price_evaluation(result, {"state": "not_triggered", "reason": "above_threshold",
                                                     "threshold_cny": 600, "prior_n": 5})
        self.assertEqual(result["low_price_alert_state"], "not_triggered")
        self.assertEqual(result["low_price_alert_reason"], "above_threshold")
        self.assertFalse(result["low_price_alert_triggered"])

    def test_history_unavailable(self):
        result = run_analysis("one_way", points=0)
        self.assertEqual(result["low_price_alert_state"], "unknown")
        self.assertEqual(result["low_price_alert_reason"], "history_unavailable")
        self.assertFalse(result["low_price_alert_triggered"])

    def test_history_insufficient(self):
        result = run_analysis("one_way", points=3)
        analyzer.apply_low_price_evaluation(result, {"state": "unknown", "reason": "history_insufficient",
                                                     "threshold_cny": None, "prior_n": 3})
        self.assertEqual(result["low_price_alert_state"], "unknown")
        self.assertEqual(result["low_price_alert_reason"], "history_insufficient")
        self.assertFalse(result["low_price_alert_triggered"])

    def test_return_only_not_applicable(self):
        result = run_analysis("return_only", points=6, lowest=550.0)
        self.assertEqual(result["low_price_alert_state"], "not_applicable")
        self.assertIsNone(result["low_price_alert_reason"])

    def test_non_low_price_strategy_not_applicable(self):
        result = run_analysis("one_way", hard={"budget_strategy": "explicit"})
        self.assertEqual(result["low_price_alert_state"], "not_applicable")
        self.assertTrue(result["low_price_alert_triggered"])

    def test_trip_context_unknown_when_unprovable(self):
        result = run_analysis(None)
        self.assertEqual(result["low_price_alert_state"], "unknown")
        self.assertEqual(result["low_price_alert_reason"], "trip_context_unknown")

    def test_compat_resolution_round_trip_from_constraints(self):
        result = run_analysis(None, hard={
            "budget_strategy": "low_price_alert",
            "round_trip": True,
        })
        self.assertEqual(result["low_price_alert_state"], "unknown")
        self.assertEqual(result["low_price_alert_reason"], "history_unavailable")

    def test_invalid_trip_type_raises(self):
        with self.assertRaises(ValueError):
            run_analysis("business_trip")

    def test_early_error_branch_carries_explicit_state(self):
        result = analyze_all_flights(
            [], None, hard_constraints=dict(LOW_PRICE_HARD), low_price_trip_type="one_way"
        )
        self.assertEqual(result["error"], "no_flights")
        self.assertEqual(result["low_price_alert_state"], "unknown")
        self.assertEqual(result["low_price_alert_reason"], "no_valid_offers")


def make_sub(**overrides):
    sub = {
        "subscription_id": "11111111-2222-3333-4444-555555555555",
        "origin": "PVG",
        "destination": "KIX",
        "depart_date": "2026-12-01",
        "mode": "balanced",
        "notification_goals": {"method": "email", "email": "user@example.com"},
        "hard_constraints": {"budget_strategy": "low_price_alert"},
    }
    sub.update(overrides)
    return sub


HASH_INPUTS = {
    "active_origins": ["PVG"],
    "active_dests": ["KIX"],
    "constraint_fp": "fp-test",
    "round_trip": False,
    "return_date": None,
    "passengers": {"adult": 1},
    "search_sources": [],
}


class CapabilityNoticeTest(unittest.TestCase):
    def setUp(self):
        self.tmp = TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.state_path = Path(self.tmp.name) / "low_price_notice_state.json"
        self.addCleanup(patch.stopall)
        patch.object(main, "LOW_PRICE_NOTICE_STATE_PATH", self.state_path).start()
        self.sent = []

        def fake_send(method, email, title, content):
            self.sent.append({"title": title, "content": content})
            return {"email_sent": True, "pushplus_sent": None}, {}

        patch.object(main, "_send_alert_channels", side_effect=fake_send).start()

    def analysis(self, reason="history_unavailable"):
        return {"low_price_alert_state": "unknown", "low_price_alert_reason": reason}

    def test_notice_sent_once_per_config(self):
        sub = make_sub()
        main._handle_low_price_unknown(sub, self.analysis(), **HASH_INPUTS)
        main._handle_low_price_unknown(sub, self.analysis(), **HASH_INPUTS)
        self.assertEqual(len(self.sent), 1)
        self.assertIn("不是低价提醒", self.sent[0]["content"])
        state = json.loads(self.state_path.read_text(encoding="utf-8"))
        self.assertEqual(len(state), 1)

    def test_runtime_evidence_does_not_change_hash(self):
        sub = make_sub()
        main._handle_low_price_unknown(sub, self.analysis(), **HASH_INPUTS)
        changed = dict(HASH_INPUTS, passengers={"adult": 1})
        analysis2 = self.analysis("history_insufficient")
        main._handle_low_price_unknown(sub, analysis2, **changed)
        self.assertEqual(len(self.sent), 1)

    def test_config_change_renotifies(self):
        sub = make_sub()
        main._handle_low_price_unknown(sub, self.analysis(), **HASH_INPUTS)
        main._handle_low_price_unknown(
            make_sub(depart_date="2026-12-08"), self.analysis(), **HASH_INPUTS
        )
        self.assertEqual(len(self.sent), 2)

    def test_roundtrip_reason_uses_roundtrip_text(self):
        main._handle_low_price_unknown(
            make_sub(round_trip=True, return_date="2026-12-05"),
            self.analysis("roundtrip_history_not_supported"),
            **dict(HASH_INPUTS, round_trip=True, return_date="2026-12-05"),
        )
        self.assertEqual(len(self.sent), 1)
        self.assertIn("整个往返行程", self.sent[0]["content"])

    def test_send_failure_not_recorded_and_retried(self):
        patch.object(
            main, "_send_alert_channels",
            side_effect=lambda *args: ({"email_sent": False, "pushplus_sent": None}, {}),
        ).start()
        sub = make_sub()
        main._handle_low_price_unknown(sub, self.analysis(), **HASH_INPUTS)
        self.assertFalse(self.state_path.exists())
        patch.object(
            main, "_send_alert_channels",
            side_effect=lambda *args: ({"email_sent": True, "pushplus_sent": None}, {}),
        ).start()
        main._handle_low_price_unknown(sub, self.analysis(), **HASH_INPUTS)
        self.assertTrue(self.state_path.exists())

    def test_missing_identity_no_send_no_record(self):
        sub = make_sub()
        del sub["subscription_id"]
        main._handle_low_price_unknown(sub, self.analysis(), **HASH_INPUTS)
        self.assertEqual(len(self.sent), 0)
        self.assertFalse(self.state_path.exists())


class ResolverContractTest(unittest.TestCase):
    def test_states_are_four(self):
        states = getattr(analyzer, "LOW_PRICE_ALERT_STATES", None)
        self.assertIsNotNone(states, "analyzer 缺少 LOW_PRICE_ALERT_STATES")
        self.assertEqual(
            set(states),
            {"triggered", "not_triggered", "unknown", "not_applicable"},
        )

    def test_unknown_never_reports_triggered_bool(self):
        resolver = getattr(analyzer, "resolve_low_price_alert_state", None)
        self.assertIsNotNone(resolver, "analyzer 缺少 resolve_low_price_alert_state")
        state, _reason = resolver("low_price_alert", None, 500.0, None, "one_way")
        self.assertEqual(state, "unknown")


if __name__ == "__main__":
    unittest.main()
