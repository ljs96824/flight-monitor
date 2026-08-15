import json
import unittest
from pathlib import Path
from unittest.mock import patch


BASE_DIR = Path(__file__).resolve().parent
FIXTURE_PATH = BASE_DIR / "tests" / "fixtures" / "notification_no_plan_mixed_v1.json"


class StandardNotificationSectionContractTest(unittest.TestCase):
    def test_standard_bundle_contains_every_canonical_section(self):
        from notification_sections import missing_notification_sections
        from notifier import render_detail_html, render_email

        payload = json.loads(FIXTURE_PATH.read_text(encoding="utf-8"))["payload"]
        plan = dict(payload["excluded_plans"][0])
        plan.update(
            {
                "label": "方案A",
                "tier": "首选推荐",
                "price": plan["total_price"],
                "estimated_price": plan["total_price"],
                "buy_condition": "以支付页最终价为准",
                "reason": "满足当前约束",
                "reasons": ["满足当前约束"],
                "filter_reasons": [],
            }
        )
        payload.update(
            {
                "push_type": "价格提醒",
                "recommended_plans": [plan],
                "current_price": plan["total_price"],
                "display_price": plan["total_price"],
                "transaction_price": plan["total_price"],
                "budget_compare_price": plan["total_price"],
                "verify_price": plan["total_price"],
                "buy_condition": "以支付页最终价为准",
                "price_policy_reason": "满足当前约束",
                "recommendation": "可验证",
            }
        )
        payload.pop("mixed_cabin")
        payload["cabin_policy_summary"] = {}

        with patch("notifier._quota_overview_text", return_value="[配额总览] 测试台账"):
            _subject, email_html = render_email(payload)
            detail_html = render_detail_html(payload)

        self.assertEqual(
            missing_notification_sections(
                email_html,
                detail_html,
                trigger_type="standard",
                mixed_cabin=False,
            ),
            [],
        )


if __name__ == "__main__":
    unittest.main()
