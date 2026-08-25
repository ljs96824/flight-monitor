import copy
import unittest

from analyzer import travel_profile_explanation


class TravelProfileExplanationCharacterizationTest(unittest.TestCase):
    def test_none_uses_the_complete_personal_default_profile(self):
        result = travel_profile_explanation(None)

        self.assertEqual(
            result,
            {
                "scenario": "personal",
                "scenarios": ["personal"],
                "scenario_label": "个人出行",
                "basis": "个人出行按价格和便利性均衡处理。",
                "tradeoff": "",
                "dimensions": {
                    "价格敏感度": "高",
                    "时间刚性": "中",
                    "舒适度需求": "中",
                    "执行风险厌恶": "中",
                    "行李票规重要性": "中",
                },
                "stock_check": None,
            },
        )
        self.assertEqual(
            list(result),
            [
                "scenario",
                "scenarios",
                "scenario_label",
                "basis",
                "tradeoff",
                "dimensions",
                "stock_check",
            ],
        )
        self.assertIsInstance(result["scenario"], str)
        self.assertIsInstance(result["scenarios"], list)
        self.assertIsInstance(result["dimensions"], dict)

    def test_single_scenario_matrix_preserves_current_labels_and_punctuation(self):
        cases = {
            "personal": ("个人出行", "个人出行按价格和便利性均衡处理。"),
            "business": (
                "商务/会议",
                "商务/会议提高到达时间稳定、直飞/低风险和可改签权重。",
            ),
            "tourism": ("旅游", "旅游保留价格敏感和日期弹性。"),
            "family": (
                "家庭/亲子",
                "家庭/亲子提高白天直飞、行李明确和低中转风险权重。",
            ),
            "elderly": (
                "有老人同行",
                "老人同行提高白天到达、全服务航司和低转机风险权重。",
            ),
            "price_first": (
                "价格优先",
                "价格优先保留低价敏感度，但仍会提示执行风险。",
            ),
        }

        for scenario, (label, basis) in cases.items():
            with self.subTest(scenario=scenario):
                self.assertEqual(
                    travel_profile_explanation({"scenario": scenario}),
                    {
                        "scenario": scenario,
                        "scenarios": [scenario],
                        "scenario_label": label,
                        "basis": basis,
                        "tradeoff": "",
                        "dimensions": {},
                        "stock_check": None,
                    },
                )

    def test_multi_scenario_order_dimensions_stock_and_input_are_exact(self):
        profile = {
            "scenario": "personal",
            "scenarios": ["tourism", "family", "price_first"],
            "price": "high",
            "time": "medium",
            "comfort": "high",
            "risk_averse": "high",
            "baggage": "high",
            "stock_check": {"required": True},
        }
        before = copy.deepcopy(profile)

        result = travel_profile_explanation(profile)

        self.assertEqual(
            result,
            {
                "scenario": "tourism",
                "scenarios": ["tourism", "family", "price_first"],
                "scenario_label": "旅游 + 家庭/亲子 + 价格优先",
                "basis": (
                    "系统合并了多个场景的需求：旅游保留价格敏感和日期弹性。；"
                    "家庭/亲子提高白天直飞、行李明确和低中转风险权重。；"
                    "价格优先保留低价敏感度，但仍会提示执行风险。"
                ),
                "tradeoff": "旅游保留价格敏感，但家庭/亲子的安全舒适要求会优先于纯低价。",
                "dimensions": {
                    "价格敏感度": "高",
                    "时间刚性": "中",
                    "舒适度需求": "高",
                    "执行风险厌恶": "高",
                    "行李票规重要性": "高",
                },
                "stock_check": {"required": True},
            },
        )
        self.assertEqual(profile, before)

    def test_tradeoff_branch_priority_and_comma_scenario_normalization_are_exact(self):
        important_first = travel_profile_explanation(
            {"scenarios": ["price_first", "important", "business"]}
        )
        business_price = travel_profile_explanation(
            {"scenarios": "business, price_first"}
        )
        elderly_visit = travel_profile_explanation(
            {"scenarios": ["with_elderly", "family_visit"]}
        )

        self.assertEqual(
            important_first["tradeoff"],
            "你同时选择了价格优先和重要事项，系统会先保证可靠性，再在可靠方案中选择价格更低的。",
        )
        self.assertEqual(important_first["scenario"], "price_first")
        self.assertEqual(
            important_first["scenario_label"],
            "价格优先 + 重要事项 + 商务/会议",
        )
        self.assertEqual(business_price["scenarios"], ["business", "price_first"])
        self.assertEqual(
            business_price["tradeoff"],
            "商务场景会先保证准点和低风险，再在同类稳妥方案中选择更低价格。",
        )
        self.assertEqual(
            elderly_visit["tradeoff"],
            "探亲/回家提高行李权重，老人同行进一步提高直飞、白天到达和低风险权重。",
        )

    def test_unknown_scenario_and_unknown_dimension_level_are_preserved(self):
        self.assertEqual(
            travel_profile_explanation(
                {"scenario": "mystery", "price": "odd", "stock_check": None}
            ),
            {
                "scenario": "mystery",
                "scenarios": ["mystery"],
                "scenario_label": "mystery",
                "basis": "按价格、时间、舒适度和执行风险做均衡排序。",
                "tradeoff": "",
                "dimensions": {"价格敏感度": "odd"},
                "stock_check": None,
            },
        )

    def test_falsey_non_mapping_uses_default_but_truthy_non_mapping_raises(self):
        self.assertEqual(
            travel_profile_explanation([]),
            travel_profile_explanation(None),
        )
        with self.assertRaises(AttributeError):
            travel_profile_explanation("business")


if __name__ == "__main__":
    unittest.main()
