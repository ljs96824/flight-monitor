import unittest
import logging
import sys
import types


sys.modules.setdefault("dotenv", types.SimpleNamespace(load_dotenv=lambda *a, **k: None))
sys.modules.setdefault("httpx", types.SimpleNamespace(get=lambda *a, **k: None, post=lambda *a, **k: None))
logging.basicConfig = lambda *a, **k: None


class BusinessCabinPolicyTest(unittest.TestCase):
    def test_determine_cabins_uses_level_and_team_business_seats(self):
        from analyzer import determine_cabins

        self.assertEqual(
            determine_cabins({"cabin_policy": "economy_only", "user_level": "vp"}),
            ["economy"],
        )
        self.assertEqual(
            determine_cabins({"cabin_policy": "level_based", "user_level": "staff"}),
            ["economy"],
        )
        self.assertEqual(
            determine_cabins({"cabin_policy": "level_based", "user_level": "director"}),
            ["economy", "business"],
        )
        self.assertEqual(
            determine_cabins(
                {"cabin_policy": "level_based", "user_level": "manager", "business_seats": 1}
            ),
            ["economy", "business"],
        )
        self.assertEqual(
            determine_cabins({"cabin_arrangement": "economy_all", "business_seats": 3}),
            ["economy"],
        )
        self.assertEqual(
            determine_cabins({"cabin_arrangement": "business_all"}),
            ["economy", "business"],
        )
        self.assertEqual(
            determine_cabins({"cabin_arrangement": "mixed", "business_seats": 2, "economy_seats": 6}),
            ["economy", "business"],
        )

    def test_build_cabin_policy_summary_calculates_team_cost_and_reimburse(self):
        from analyzer import build_cabin_policy_summary

        summary = build_cabin_policy_summary(
            {
                "trip_nature": "business_meeting",
                "cabin_policy": "level_based",
                "user_level": "director",
                "business_seats": 2,
                "economy_seats": 4,
                "reimburse_per_person": 2000,
            },
            [
                {"cabin_class": "economy", "price": 680, "flight_no": "MU5101"},
                {"cabin_class": "business", "price": 2180, "flight_no": "MU5101"},
            ],
        )

        self.assertEqual(summary["cabins"], ["economy", "business"])
        self.assertEqual(summary["business_unit_price"], 2180)
        self.assertEqual(summary["economy_unit_price"], 680)
        self.assertEqual(summary["team_total"], 7080)
        self.assertIn("超出报销上限", summary["business_reimburse_note"])
        self.assertIn("2商务+4经济", summary["team_cost_note"])

    def test_normalized_subscription_sets_cabin_classes_from_policy(self):
        from main import normalize_subscription

        normalized = normalize_subscription(
            {
                "origin": "PVG",
                "destination": "PEK",
                "depart_date": "2026-06-10",
                "constraints": {
                    "cabin_policy": "level_based",
                    "user_level": "director",
                    "business_seats": 1,
                },
                "basic": {"passenger_count": 2},
            }
        )

        self.assertEqual(normalized["cabin_classes"], ["economy", "business"])

    def test_build_cabin_policy_summary_supports_trip_natures_and_arrangement(self):
        from analyzer import build_cabin_policy_summary

        summary = build_cabin_policy_summary(
            {
                "trip_natures": ["meeting", "team_building"],
                "cabin_arrangement": "mixed",
                "business_seats": 2,
                "economy_seats": 6,
                "passenger_count": 8,
            },
            [
                {"cabin_class": "economy", "price": 680},
                {"cabin_class": "business", "price": 2180},
            ],
        )

        self.assertEqual(summary["trip_natures"], ["meeting", "team_building"])
        self.assertEqual(summary["cabin_arrangement"], "mixed")
        self.assertEqual(summary["cabins"], ["economy", "business"])
        self.assertEqual(summary["team_total"], 8440)

    def test_email_renders_cabin_policy_summary(self):
        from notifier import render_email

        payload = {
            "push_type": "价格提醒",
            "route": "上海 → 北京",
            "recommendation": "值得验证",
            "confidence": "中高",
            "display_price": 680,
            "transaction_price": 680,
            "verify_price": 900,
            "recommended_plans": [
                {
                    "label": "方案A",
                    "tier": "首选推荐",
                    "is_roundtrip": False,
                    "price": 680,
                    "estimated_price": 680,
                    "summary": "MU5101 东方航空",
                    "links": {},
                }
            ],
            "trigger_reason": [],
            "price_history": [],
            "action_range": {"ranges": []},
            "cabin_policy_summary": {
                "trip_nature": "business_meeting",
                "cabin_policy": "level_based",
                "cabins": ["economy", "business"],
                "economy_unit_price": 680,
                "business_unit_price": 2180,
                "business_seats": 1,
                "economy_seats": 4,
                "team_cost_note": "1商务+4经济合计参考¥4,900",
                "business_reimburse_note": "商务舱¥2,180在报销上限¥5,000内",
            },
            "detail_url": "https://example.com/detail",
            "form_url": "https://example.com/",
            "feedback_url": "https://example.com/feedback",
        }

        _, html = render_email(payload)

        self.assertIn("经济舱 / 商务舱并列参考", html)
        self.assertIn("部分职级可商务舱", html)
        self.assertIn("商务舱¥2,180在报销上限¥5,000内", html)
        self.assertIn("系统仅客观展示", html)

    def test_email_renders_multi_nature_mixed_cabin_summary(self):
        from notifier import render_email

        payload = {
            "push_type": "价格提醒",
            "route": "上海 → 北京",
            "recommendation": "值得验证",
            "confidence": "中高",
            "display_price": 680,
            "transaction_price": 680,
            "verify_price": 720,
            "ideal_price": 800,
            "max_price": 1000,
            "recommended_plans": [{"label": "方案A", "price": 680, "estimated_price": 680}],
            "trigger_reason": [],
            "buy_risk": [],
            "wait_risk": [],
            "excluded_plans": [],
            "checklist": [],
            "source_summary": {},
            "confidence_dimensions": {},
            "price_history": [],
            "action_range": {"ranges": []},
            "cabin_policy_summary": {
                "trip_natures": ["meeting", "team_building"],
                "cabin_arrangement": "mixed",
                "cabin_policy": "level_based",
                "cabins": ["economy", "business"],
                "economy_unit_price": 680,
                "business_unit_price": 2180,
                "business_seats": 2,
                "economy_seats": 6,
                "team_cost_note": "2商务+6经济合计参考¥8,440",
            },
        }

        _, html = render_email(payload)

        self.assertIn("商务会议 + 公司团建", html)
        self.assertIn("8人", html)
        self.assertIn("混合舱位", html)
        self.assertIn("商务舱2人，经济舱6人", html)
        self.assertIn("参考单人价 ¥2,180 × 2 = ¥4,360", html)
        self.assertIn("参考单人价 ¥680 × 6 = ¥4,080", html)
        self.assertIn("2商务+6经济合计参考¥8,440", html)


if __name__ == "__main__":
    unittest.main()
