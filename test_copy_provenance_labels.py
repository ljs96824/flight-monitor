import unittest

import analyzer
import notifier


def _leg(*, primary_source="hasdata", price_source="hasdata"):
    return {
        "flight_no": "MU225",
        "flight_combo": "MU225",
        "price": 5000,
        "primary_source": primary_source,
        "price_source": price_source,
        "data_source": "hasdata+juhe",
    }


class PlanSourceProvenanceTest(unittest.TestCase):
    def test_all_hasdata_marks_structure_and_pool_source_together(self):
        plan = {
            "outbound_flight": _leg(price_source="hasdata"),
            "return_flight": _leg(price_source="hasdata"),
        }

        self.assertEqual(
            notifier._plan_source_label(plan),
            "结构与入池:Google Flights",
        )

    def test_all_ota_prices_separate_structure_from_pool_source(self):
        plan = {
            "outbound_flight": _leg(price_source="juhe"),
            "return_flight": _leg(price_source="juhe"),
        }

        self.assertEqual(
            notifier._plan_source_label(plan),
            "航班结构:Google Flights / 入池价:OTA(聚合)",
        )

    def test_mixed_pool_sources_are_labeled_per_leg(self):
        plan = {
            "outbound_flight": _leg(price_source="juhe"),
            "return_flight": _leg(price_source="hasdata"),
        }

        self.assertEqual(
            notifier._plan_source_label(plan),
            "航班结构:Google Flights / 去程入池:OTA(聚合) / 返程入池:Google Flights",
        )


class PriceReferenceTierTextTest(unittest.TestCase):
    def test_price_signal_degrades_when_history_sample_is_one(self):
        signal = analyzer.build_price_signal(
            900,
            target_price=800,
            price_history=[1000],
        )

        self.assertEqual(
            signal["summary"],
            "同条件样本不足（当前n=1），继续积累中，暂不给出价格位置判断（近1次同条件采集）",
        )
        self.assertEqual(signal["label"], "待积累")

    def test_trigger_reason_degrades_when_history_sample_is_one(self):
        result = analyzer.determine_push_type(
            1200,
            target_price=1000,
            max_budget=1500,
            price_history=[900],
            analysis_result={},
        )

        self.assertIn(
            "搜索参考价距离理想入手价还差¥200（你的设置）",
            result["reasons"],
        )
        self.assertIn(
            "同条件样本不足（当前n=1），继续积累中，暂不给出价格位置判断（近1次同条件采集）",
            result["reasons"],
        )

    def test_history_sample_gate_opens_at_five(self):
        prices = [1000, 1050, 1100, 1150, 1200]

        signal = analyzer.build_price_signal(
            900,
            target_price=800,
            price_history=prices,
        )
        result = analyzer.determine_push_type(
            1300,
            target_price=1000,
            max_budget=1500,
            price_history=prices,
            analysis_result={},
        )

        self.assertEqual(
            signal["summary"],
            "搜索参考价处于近期低位（近5次同条件采集）",
        )
        self.assertIn(
            "当前搜索价高于大多数相似历史样本（近5次同条件采集）",
            result["reasons"],
        )

    def test_history_sample_gate_keeps_normal_copy_at_twenty(self):
        prices = list(range(1000, 1200, 10))

        signal = analyzer.build_price_signal(
            900,
            target_price=800,
            price_history=prices,
        )
        result = analyzer.determine_push_type(
            1300,
            target_price=1000,
            max_budget=1500,
            price_history=prices,
            analysis_result={},
        )

        self.assertEqual(
            signal["summary"],
            "搜索参考价处于近期低位（近20次同条件采集）",
        )
        self.assertIn(
            "当前搜索价高于大多数相似历史样本（近20次同条件采集）",
            result["reasons"],
        )

    def test_action_ranges_name_user_settings_tier(self):
        row = {"text": "¥6,000-¥6,300", "label": "值得购买"}

        self.assertEqual(
            notifier._action_range_display_text(row),
            "¥6,000-¥6,300：值得购买（你的设置）",
        )


if __name__ == "__main__":
    unittest.main()
