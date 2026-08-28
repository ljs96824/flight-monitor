import ast
import copy
import inspect
import unittest
from pathlib import Path
from unittest.mock import patch

import email_notifier
import notifier


SAMPLE_INSUFFICIENT = "历史价格样本不足，暂时无法估算后续下降比例。"


class GenerateNeutralSummaryF821Test(unittest.TestCase):
    def _summary(self, position, recent="平稳"):
        return notifier.generate_neutral_summary(
            {"price_range": [1000, 1500]},
            {
                "avg_price": 1200,
                "recent_trend": recent,
                "current_position": position,
            },
        )

    def test_signature_and_historical_list_return_contract(self):
        self.assertEqual(
            str(inspect.signature(notifier.generate_neutral_summary)),
            "(analysis, trend, price_insights=None)",
        )
        result = self._summary("🟢 较低区间")
        self.assertIsInstance(result, list)
        self.assertTrue(all(isinstance(line, str) for line in result))

    def test_removed_helper_is_not_reintroduced(self):
        tree = ast.parse(Path(notifier.__file__).read_text(encoding="utf-8"))
        definitions = {
            node.name
            for node in tree.body
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef))
        }
        self.assertNotIn("_plain_price_position", definitions)

    def test_price_position_markers_are_removed_inline(self):
        cases = {
            "🟢 较低区间": "较低区间",
            "🟢🟡 中间区间": "中间区间",
            "较低🟢区间": "较低区间",
            "  🔴 较高区间  ": "较高区间",
        }
        for raw, cleaned in cases.items():
            with self.subTest(raw=raw):
                self.assertEqual(
                    self._summary(raw),
                    [
                        "当前最低价¥1,000，低于近60天平均价¥1,200。",
                        f"当前价格处于近60天的{cleaned}。",
                        "近期价格较为平稳。",
                        SAMPLE_INSUFFICIENT,
                    ],
                )

    def test_empty_cleaned_position_does_not_create_an_empty_sentence(self):
        for position in ("🟢", None, ""):
            with self.subTest(position=position):
                result = self._summary(position)
                self.assertEqual(
                    result,
                    [
                        "当前最低价¥1,000，低于近60天平均价¥1,200。",
                        "近期价格较为平稳。",
                        SAMPLE_INSUFFICIENT,
                    ],
                )
                self.assertNotIn("当前价格处于近60天的。", result)

    def test_trend_and_price_comparison_wording_is_unchanged(self):
        cases = (
            (
                {"price_range": [1000, 1500]},
                {"avg_price": 1200, "recent_trend": "上涨", "current_position": ""},
                [
                    "当前最低价¥1,000，低于近60天平均价¥1,200。",
                    "近期价格呈上涨趋势。",
                    SAMPLE_INSUFFICIENT,
                ],
            ),
            (
                {"price_range": [1500, 1600]},
                {"avg_price": 1200, "recent_trend": "下降", "current_position": ""},
                [
                    "当前最低价¥1,500，高于近60天平均价¥1,200。",
                    "近期价格在下降。",
                    SAMPLE_INSUFFICIENT,
                ],
            ),
            (
                {"price_range": [1000, 1500]},
                {"avg_price": 1200, "recent_trend": "平稳", "current_position": ""},
                [
                    "当前最低价¥1,000，低于近60天平均价¥1,200。",
                    "近期价格较为平稳。",
                    SAMPLE_INSUFFICIENT,
                ],
            ),
            (
                {"price_range": [1000, 1500]},
                None,
                ["近期价格较为平稳。", SAMPLE_INSUFFICIENT],
            ),
        )
        for analysis, trend, expected in cases:
            with self.subTest(analysis=analysis, trend=trend):
                self.assertEqual(
                    notifier.generate_neutral_summary(analysis, trend),
                    expected,
                )

    def test_summary_has_no_network_delivery_or_disk_side_effects(self):
        analysis = {"price_range": [1000, 1500]}
        trend = {
            "avg_price": 1200,
            "recent_trend": "下降",
            "current_position": "🟢 较低区间",
        }
        price_insights = {"price_history": []}
        original = copy.deepcopy((analysis, trend, price_insights))

        side_effects = (
            patch.object(notifier.httpx, "post"),
            patch.object(notifier, "_post_pushplus"),
            patch.object(notifier, "send"),
            patch.object(email_notifier, "send_email"),
            patch.object(notifier, "save_last_push_price"),
            patch.object(notifier, "save_push_snapshot"),
            patch.object(notifier, "save_pushed_plans"),
            patch.object(Path, "write_text"),
            patch.object(Path, "write_bytes"),
        )
        mocks = []
        with side_effects[0] as http_post, side_effects[1] as push_post, side_effects[2] as send:
            with side_effects[3] as send_email, side_effects[4] as save_price:
                with side_effects[5] as save_snapshot, side_effects[6] as save_plans:
                    with side_effects[7] as write_text, side_effects[8] as write_bytes:
                        mocks.extend(
                            [
                                http_post,
                                push_post,
                                send,
                                send_email,
                                save_price,
                                save_snapshot,
                                save_plans,
                                write_text,
                                write_bytes,
                            ]
                        )
                        result = notifier.generate_neutral_summary(
                            analysis,
                            trend,
                            price_insights,
                        )

        for mocked in mocks:
            mocked.assert_not_called()
        self.assertEqual((analysis, trend, price_insights), original)
        self.assertIsInstance(result, list)
        self.assertTrue(all(isinstance(line, str) for line in result))
        joined = "".join(result)
        for instruction in ("建议购买", "立即购买", "建议等待", "立即下单"):
            self.assertNotIn(instruction, joined)


if __name__ == "__main__":
    unittest.main()
