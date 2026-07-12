import contextlib
import io
import sys
import types
import unittest
from unittest.mock import patch


sys.modules.setdefault("httpx", types.SimpleNamespace(get=lambda *a, **k: None, post=lambda *a, **k: None))

from analyzer import analyze_all_flights
from notifier import build_notification_payload, render_email


class NotificationContradictionsTest(unittest.TestCase):
    @staticmethod
    def _roundtrip_advice_payload(max_budget):
        analysis = {
            "round_trip_analysis": {
                "top_combinations": [
                    {
                        "outbound": {
                            "flight_no": "MU225",
                            "price": 4902,
                            "stops": 0,
                            "execution_grade": "A",
                        },
                        "return": {
                            "flight_no": "JL891",
                            "price": 4169,
                            "stops": 0,
                            "execution_grade": "A",
                        },
                        "outbound_price": 4902,
                        "return_price": 4169,
                        "total_price": 9071,
                    }
                ],
                "total_min": 9071,
            },
            "decision": {"conclusion": "可以观察", "confidence": "中"},
        }
        route_info = {
            "round_trip": True,
            "origin": "PVG",
            "destination": "KIX",
            "depart_date": "2026-10-01",
            "return_date": "2026-10-06",
            "target_price": 6000,
            "max_budget": max_budget,
            "route_type": "international",
        }
        subscription = {
            "id": f"advice-scope-{max_budget}",
            "basic": {"route_type": "international", "passenger_count": 1},
            "preferences": {
                "passengers": {"adult": 1, "child": 0, "elderly": 0, "infant": 0}
            },
            "constraints": {
                "budget_scope": "per_person",
                "max_budget_scope": "per_person",
                "target_price_scope": "per_person",
                "target_price": 6000,
                "max_budget": max_budget,
            },
        }
        output = io.StringIO()
        with patch("notifier.get_last_push_price", return_value=None), patch(
            "notifier.get_last_push_snapshot", return_value=None
        ), patch("notifier.track_plan_status", return_value=None), contextlib.redirect_stdout(output):
            payload = build_notification_payload(
                analysis,
                route_info=route_info,
                subscription=subscription,
            )
        return payload, output.getvalue()

    def test_roundtrip_over_budget_advice_matches_headline_and_diagnosis(self):
        payload, log = self._roundtrip_advice_payload(8000)

        self.assertTrue(payload["budget_gap"]["is_over_budget"])
        self.assertEqual(
            payload["recommended_plans"][0]["buy_condition"],
            payload["recommendation"],
        )
        self.assertNotIn("强烈建议购买", payload["recommended_plans"][0]["buy_condition"])
        self.assertNotIn("达标", payload["recommended_plans"][0]["buy_condition"])
        self.assertIn(
            "[购买建议] unit_roundtrip=9071 max_budget=8000 判定=over_budget 与排除诊断一致=True",
            log,
        )

    def test_roundtrip_budget_raise_switches_headline_and_card_together(self):
        payload, log = self._roundtrip_advice_payload(10000)

        self.assertFalse(payload["budget_gap"]["is_over_budget"])
        self.assertEqual(
            payload["recommended_plans"][0]["buy_condition"],
            payload["recommendation"],
        )
        self.assertIn("购买前验证", payload["recommendation"])
        self.assertIn(
            "[购买建议] unit_roundtrip=9071 max_budget=10000 判定=within_budget 与排除诊断一致=True",
            log,
        )

    def test_roundtrip_analysis_does_not_attach_leg_budget_advice(self):
        flight = {
            "price": 4902,
            "flight_combo": "MU225",
            "airline_summary": "MU",
            "route_summary": "PVG → KIX",
            "total_duration_min": 210,
            "total_hours": 3.5,
            "stops": 0,
            "segments": [
                {
                    "flight_no": "MU225",
                    "airline": "MU",
                    "dep_airport": "PVG",
                    "dep_time": "2026-10-01 09:50",
                    "arr_airport": "KIX",
                    "arr_time": "2026-10-01 13:20",
                    "duration_min": 210,
                }
            ],
            "layovers": [],
            "data_source": "hasdata",
            "cabin_class": "economy",
        }

        result = analyze_all_flights(
            [flight],
            user_preferences={
                "round_trip": True,
                "target_price": 6000,
                "max_budget": 8000,
            },
        )

        self.assertNotIn("price_advice", result["all_flights"][0])
        self.assertIsNone(result["price_band"])

    def test_roundtrip_payload_ignores_unscoped_last_push_without_plan_tracking(self):
        analysis = {
            "round_trip_analysis": {
                "top_combinations": [
                    {
                        "outbound": {"flight_no": "MU225", "price": 6000, "stops": 0},
                        "return": {"flight_no": "JL891", "price": 6426, "stops": 0},
                        "outbound_price": 6000,
                        "return_price": 6426,
                        "total_price": 12426,
                    }
                ]
            }
        }
        with patch("notifier.get_last_push_price", return_value={"price": 33591}), patch(
            "notifier.get_last_push_snapshot", return_value=None
        ), patch("notifier.track_plan_status", return_value=None):
            payload = build_notification_payload(
                analysis,
                route_info={
                    "round_trip": True,
                    "origin": "PVG",
                    "destination": "KIX",
                    "depart_date": "2026-10-01",
                    "return_date": "2026-10-06",
                },
                subscription={
                    "id": "roundtrip-unscoped-history",
                    "basic": {"route_type": "international"},
                    "preferences": {
                        "passengers": {"adult": 2, "child": 1, "elderly": 2, "infant": 0}
                    },
                },
            )

        self.assertIsNone(payload["diff_from_last"]["diff"])
        self.assertFalse(
            any("较上次提醒" in str(reason) for reason in payload["trigger_reason"])
        )

    def test_plan_tracking_change_replaces_mixed_scope_push_difference(self):
        from notifier import _apply_plan_tracking_change

        push_meta = {
            "type": "价格下降",
            "price_change": {
                "last": 33591,
                "current": 12426,
                "diff": -21165,
                "direction": "down",
            },
            "reasons": ["较上次提醒：下降¥21,165", "当前价格仍需观察"],
        }
        tracking = {
            "status": "price_up",
            "previous_price": 12215,
            "current_price": 12426,
            "price_diff": 211,
            "scope": "per_person_roundtrip",
            "msg": "同组合单人往返上涨¥211",
        }

        result = _apply_plan_tracking_change(push_meta, tracking, is_roundtrip=True)

        self.assertEqual(result["type"], "涨价风险")
        self.assertEqual(result["price_change"]["last"], 12215)
        self.assertEqual(result["price_change"]["current"], 12426)
        self.assertEqual(result["price_change"]["diff"], 211)
        self.assertEqual(result["price_change"]["scope"], "per_person_roundtrip")
        self.assertIn("同组合单人往返上涨¥211", result["reasons"])
        self.assertNotIn("较上次提醒：下降¥21,165", result["reasons"])

    def test_skipped_roundtrip_comparison_removes_mixed_scope_push_difference(self):
        from notifier import _apply_plan_tracking_change

        result = _apply_plan_tracking_change(
            {
                "type": "价格下降",
                "price_change": {"last": 33591, "current": 12426, "diff": -21165},
                "reasons": ["较上次提醒：下降¥21,165", "当前价格仍需观察"],
            },
            {
                "status": "comparison_skipped",
                "scope": "per_person_roundtrip",
                "msg": "历史记录无单人口径，本次不展示涨跌。",
            },
            is_roundtrip=True,
        )

        self.assertIsNone(result.get("price_change"))
        self.assertIn("历史记录无单人口径，本次不展示涨跌。", result["reasons"])
        self.assertNotIn("较上次提醒：下降¥21,165", result["reasons"])

    def test_stable_unit_roundtrip_change_clears_false_price_drop_type(self):
        from notifier import _apply_plan_tracking_change

        result = _apply_plan_tracking_change(
            {
                "type": "价格下降",
                "price_change": {"last": 33591, "current": 12426, "diff": -21165},
                "reasons": ["较上次提醒：下降¥21,165"],
            },
            {
                "status": "stable",
                "previous_price": 12215,
                "current_price": 12240,
                "price_diff": 25,
                "scope": "per_person_roundtrip",
                "msg": "同组合单人往返价格稳定",
            },
            is_roundtrip=True,
        )

        self.assertEqual(result["type"], "价格稳定")
        self.assertEqual(result["price_change"]["diff"], 25)

    def test_partial_roundtrip_quote_clears_stale_mixed_scope_difference(self):
        from notifier import _apply_plan_tracking_change

        result = _apply_plan_tracking_change(
            {
                "type": "价格下降",
                "price_change": {"last": 33591, "current": 12426, "diff": -21165},
                "reasons": ["较上次提醒：下降¥21,165"],
            },
            {
                "status": "partial_unavailable",
                "previous_price": 12215,
                "current_price": None,
                "price_diff": None,
                "scope": "per_person_roundtrip",
                "msg": "返程本次未获取到报价，无法计算完整单人往返价。",
            },
            is_roundtrip=True,
        )

        self.assertIsNone(result.get("price_change"))
        self.assertNotEqual(result["type"], "价格下降")
        self.assertIn("无法计算完整单人往返价", result["reasons"][0])

    def test_rising_over_budget_caps_verify_price_and_waits(self):
        analysis = {
            "recommendations": [
                {
                    "flight_no": "MU5099",
                    "price": 2560,
                    "stops": 0,
                    "execution_grade": "A",
                    "price_estimate": {"transaction_price": 2560},
                },
                {
                    "flight_no": "MU5128",
                    "price": 2710,
                    "stops": 0,
                    "execution_grade": "A",
                    "price_estimate": {"transaction_price": 2710},
                },
            ],
            "days_to_dept": 10,
            "waiting_risk": {"up_probability": 70, "down_probability": 20},
        }

        with patch("notifier.get_last_push_price", return_value={"price": 1640}), patch(
            "notifier.get_last_push_snapshot", return_value=None
        ), patch("notifier.track_plan_status", return_value=None):
            payload = build_notification_payload(
                analysis,
                route_info={
                    "origin": "上海",
                    "destination": "北京",
                    "depart_date": "2026-06-19",
                    "target_price": 1600,
                    "max_budget": 2000,
                },
                subscription={"id": "rise-over-budget"},
            )

        self.assertEqual(payload["push_type"], "涨价风险")
        self.assertEqual(payload["verify_price"], 2000)
        self.assertIn("继续", payload["recommendation"])
        self.assertNotIn("可以购买前验证", payload["recommendation"])
        tiers = [plan.get("tier") for plan in payload["recommended_plans"]]
        self.assertEqual(tiers[0], "首选推荐")
        self.assertEqual(tiers[1], "次选方案")
        self.assertEqual(tiers.count("首选推荐"), 1)

    def test_email_normalizes_duplicate_primary_tiers_and_dedupes_bottom_booking_links(self):
        payload = {
            "push_type": "涨价风险",
            "route": "上海 → 北京",
            "recommendation": "建议继续监控",
            "display_price": 2560,
            "transaction_price": 2560,
            "verify_price": 2000,
            "ideal_price": 1600,
            "max_price": 2000,
            "buy_condition": "当前搜索价¥2,560已超过最高可接受价¥2,000，建议继续监控",
            "recommended_plans": [
                {
                    "label": "方案A",
                    "tier": "首选推荐",
                    "is_roundtrip": False,
                    "price": 2560,
                    "estimated_price": 2560,
                    "main_flight": {"flight_no": "MU5099", "price": 2560, "stops": 0},
                    "links": {"main": '<a href="https://example.com/a">携程</a>'},
                },
                {
                    "label": "方案B",
                    "tier": "首选推荐",
                    "is_roundtrip": False,
                    "price": 2710,
                    "estimated_price": 2710,
                    "main_flight": {"flight_no": "MU5128", "price": 2710, "stops": 0},
                    "links": {"main": '<a href="https://example.com/b">携程</a>'},
                },
            ],
            "trigger_reason": [],
            "price_history": [],
            "detail_url": "https://example.com/detail",
            "form_url": "https://example.com/",
            "feedback_url": "https://example.com/feedback",
        }

        _, html = render_email(payload)

        self.assertIn("方案A ｜ 首选推荐", html)
        self.assertIn("方案B ｜ 次选方案", html)
        self.assertNotIn("方案B ｜ 首选推荐", html)
        self.assertEqual(html.count("快速验证首选方案A"), 1)

    def test_single_airport_combo_hides_airport_comparison(self):
        payload = {
            "push_type": "值得验证",
            "route": "上海 → 北京",
            "recommendation": "可以观察",
            "display_price": 680,
            "verify_price": 720,
            "route_airports": {"origins": ["SHA"], "destinations": ["PEK"]},
            "airport_cost_comparison": {
                "rows": [
                    {"airport": "PEK", "ticket_price": 680, "effective_cost": 910},
                ]
            },
            "recommended_plans": [
                {
                    "label": "方案A",
                    "tier": "首选推荐",
                    "is_roundtrip": False,
                    "price": 680,
                    "main_flight": {"flight_no": "MU5101", "price": 680},
                    "links": {},
                }
            ],
            "trigger_reason": [],
            "price_history": [],
        }

        _, html = render_email(payload)

        self.assertNotIn("机场选择对比", html)


if __name__ == "__main__":
    unittest.main()
