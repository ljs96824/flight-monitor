import copy
import json
import unittest
from pathlib import Path
from unittest.mock import patch


ROOT = Path(__file__).resolve().parent
NO_MATCH_FIXTURE = ROOT / "tests" / "fixtures" / "notification_no_plan_mixed_v1.json"
FROZEN_FIXTURE = ROOT / "tests" / "fixtures" / "frozen_email" / "economy_payload.json"


def _no_match_payload():
    return json.loads(NO_MATCH_FIXTURE.read_text(encoding="utf-8"))["payload"]


def _standard_payload():
    payload = _no_match_payload()
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
    payload.pop("mixed_cabin", None)
    payload["cabin_policy_summary"] = {}
    return payload


def _incomplete_payload():
    reason = "本轮返程采集失败(原因=juhe:PermissionError(errno=5,path=<relative>/data)),结论不代表市场无票"
    return {
        "push_type": "数据不完整",
        "route": "上海 → 大阪",
        "is_roundtrip": True,
        "recommended_plans": [],
        "alternative_plans": [],
        "same_day_alternatives": [],
        "no_primary_reason": reason,
        "source_degradation": {
            "active": True,
            "data_incomplete": True,
            "push_type": "数据不完整",
            "reason": reason,
        },
        "collection_failures": [{"direction": "返程", "reason": reason}],
        "source_stats": {"juhe": {"count": 1, "status": "成功"}},
        "collected_at": "2026-08-23T21:00:31",
    }


class FormatComparisonMessageF821Test(unittest.TestCase):
    def test_direct_entry_degrades_honestly_without_leaking_name_error(self):
        from notifier import format_comparison_message

        analysis = {
            "conclusion": {"conclusion": "当前结论保持"},
            "recommendations": [
                {
                    "tag": "首选",
                    "flight": {
                        "flight_combo": "MU5101",
                        "price": 880,
                        "segments": [],
                    },
                }
            ],
            "price_range": [880, 990],
        }
        route = {
            "origin": "SHA",
            "destination": "PEK",
            "depart_date": "2026-09-01",
            "detail_url": "https://example.test/detail/uuid",
        }
        before = copy.deepcopy((analysis, route))

        with patch("notifier.safe_log") as log:
            rendered = format_comparison_message(analysis, route)

        self.assertIsInstance(rendered, str)
        self.assertIn("方案对比详情暂不可用,核心推荐不受影响", rendered)
        self.assertIn("当前结论：当前结论保持", rendered)
        self.assertIn("当前最低参考价：¥880", rendered)
        self.assertIn("首选方案概要：", rendered)
        self.assertIn("MU5101", rendered)
        self.assertIn("网页详情：https://example.test/detail/uuid", rendered)
        self.assertNotIn("价格位置", rendered)
        self.assertNotIn("建议购买", rendered)
        self.assertEqual((analysis, route), before)
        self.assertTrue(
            any("[方案对比降级]" in str(call.args[0]) for call in log.call_args_list)
        )

    def test_private_detail_builder_uses_domain_exception(self):
        from notifier import ComparisonMessageUnavailable, _format_comparison_details

        with self.assertRaisesRegex(
            ComparisonMessageUnavailable,
            "历史方案对比语义已退役",
        ):
            _format_comparison_details({}, {})

    def test_primary_notification_renderers_do_not_reach_legacy_comparison_entry(self):
        import notifier

        frozen = json.loads(FROZEN_FIXTURE.read_text(encoding="utf-8"))
        scenarios = {
            "standard": _standard_payload(),
            "no_match": _no_match_payload(),
            "data_incomplete": _incomplete_payload(),
            "frozen_replay": frozen,
        }

        for label, payload in scenarios.items():
            with self.subTest(label=label), patch(
                "notifier.format_comparison_message",
                side_effect=AssertionError("legacy comparison entry reached"),
            ) as probe, patch(
                "notifier._quota_overview_text",
                return_value="[配额总览] 测试台账",
            ):
                notifier.render_email(copy.deepcopy(payload))
                notifier.render_pushplus(copy.deepcopy(payload))
                notifier.render_detail_html(copy.deepcopy(payload))
                self.assertEqual(probe.call_count, 0)


if __name__ == "__main__":
    unittest.main()
