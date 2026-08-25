import copy
import unittest

from analyzer import calc_confidence


def _flight(
    *,
    age=30,
    source_count=3,
    data_source="juhe+serpapi+duffel",
    route_type="international",
    fare_level="full",
    availability_status="likely_available",
):
    return {
        "route_type": route_type,
        "data_source": data_source,
        "availability": {
            "age_minutes": age,
            "source_count": source_count,
            "status": availability_status,
            "label": "库存待支付页确认",
        },
        "fare_verification": {"level": fare_level},
    }


class CalcConfidenceCharacterizationTest(unittest.TestCase):
    def assert_result_shape(self, result, expected):
        self.assertEqual(result, expected)
        self.assertEqual(list(result), ["overall", "dimensions", "details"])
        self.assertIsInstance(result["overall"], str)
        self.assertIsInstance(result["dimensions"], dict)
        self.assertIsInstance(result["details"], dict)
        self.assertEqual(
            list(result["dimensions"]),
            ["价格新鲜度", "历史样本量", "渠道一致性", "票规完整度", "可购买性"],
        )
        self.assertEqual(list(result["details"]), list(result["dimensions"]))
        self.assertNotIn("reason_codes", result)

    def test_all_high_factors_lock_complete_return_and_no_mutation(self):
        flight = _flight()
        source_stats = {"ignored": {"status": "失败"}}
        history = list(range(14))
        originals = copy.deepcopy((flight, source_stats, history))

        result = calc_confidence(flight, source_stats, history)

        self.assert_result_shape(
            result,
            {
                "overall": "高",
                "dimensions": {
                    "价格新鲜度": "高",
                    "历史样本量": "高",
                    "渠道一致性": "高",
                    "票规完整度": "高",
                    "可购买性": "中高",
                },
                "details": {
                    "价格新鲜度": "30分钟前采集",
                    "历史样本量": "近期14次采集",
                    "渠道一致性": "Google Flights多源交叉验证",
                    "票规完整度": "票规已确认",
                    "可购买性": "有多个渠道可验证，但最终价格和票规以支付页为准",
                },
            },
        )
        self.assertEqual((flight, source_stats, history), originals)

    def test_freshness_sample_and_source_threshold_boundaries(self):
        for value, expected in ((30, "高"), (31, "中"), (120, "中"), (121, "低"), ("bad", "低"), (None, "低")):
            with self.subTest(age=value):
                result = calc_confidence(_flight(age=value), {}, [])
                self.assertEqual(result["dimensions"]["价格新鲜度"], expected)
                self.assertEqual(
                    result["details"]["价格新鲜度"],
                    f"{value}分钟前采集" if isinstance(value, int) else "采集时间未知",
                )

        for count, expected in ((4, "低"), (5, "中"), (13, "中"), (14, "高")):
            with self.subTest(history_count=count):
                result = calc_confidence(_flight(), {}, [None] * count)
                self.assertEqual(result["dimensions"]["历史样本量"], expected)
                self.assertEqual(result["details"]["历史样本量"], f"近期{count}次采集")

        for count, expected in ((1, "低"), (2, "中"), (3, "高")):
            with self.subTest(source_count=count):
                result = calc_confidence(
                    _flight(source_count=count, data_source="juhe", route_type=""),
                    {},
                    [],
                )
                self.assertEqual(result["dimensions"]["渠道一致性"], expected)
                self.assertEqual(result["details"]["渠道一致性"], f"{count}个数据源可交叉验证")

    def test_source_count_fallback_order_and_success_status_matching(self):
        from_data_source = _flight(
            source_count=0,
            data_source="juhe+serpapi",
            route_type="",
        )
        result = calc_confidence(from_data_source, {"x": {"status": "成功"}}, [])
        self.assertEqual(result["dimensions"]["渠道一致性"], "高")
        self.assertEqual(result["details"]["渠道一致性"], "Google Flights多源交叉验证")

        from_stats = _flight(source_count=0, data_source="", route_type="")
        source_stats = {
            "juhe": {"status": "成功，返回12个", "route_type": "domestic"},
            "duffel": {"status": "成功"},
            "serpapi": {"status": "失败"},
            "metadata": "成功",
        }
        result = calc_confidence(from_stats, source_stats, [])
        self.assertEqual(result["dimensions"]["渠道一致性"], "中")
        self.assertEqual(result["details"]["渠道一致性"], "2个数据源可交叉验证")

    def test_domestic_juhe_override_locks_dimensions_details_and_low_buyability(self):
        flight = _flight(
            age=31,
            source_count=1,
            data_source="juhe",
            route_type="domestic",
            fare_level="partial",
            availability_status="collection_failed",
        )
        result = calc_confidence(flight, {}, [None] * 5)

        self.assert_result_shape(
            result,
            {
                "overall": "中高",
                "dimensions": {
                    "价格新鲜度": "中",
                    "历史样本量": "中",
                    "渠道一致性": "高",
                    "票规完整度": "中",
                    "可购买性": "低",
                },
                "details": {
                    "价格新鲜度": "31分钟前采集",
                    "历史样本量": "近期5次采集",
                    "渠道一致性": "聚合数据为国内主源，Google Flights用于交叉验证",
                    "票规完整度": "行李/退改签仍需支付页确认",
                    "可购买性": "聚合数据国内报价为主，最终库存和票规以支付页为准",
                },
            },
        )

    def test_domestic_google_and_international_google_overrides(self):
        domestic = _flight(
            source_count=1,
            data_source="serpapi",
            route_type="domestic",
            availability_status="possibly_available",
        )
        domestic_result = calc_confidence(domestic, {}, [])
        self.assertEqual(domestic_result["dimensions"]["渠道一致性"], "中")
        self.assertEqual(
            domestic_result["details"]["渠道一致性"],
            "国内航线仅有Google参考，建议重点确认支付页",
        )
        self.assertEqual(domestic_result["dimensions"]["可购买性"], "中")
        self.assertEqual(
            domestic_result["details"]["可购买性"],
            "仅Google参考，最终价格和库存需支付页确认",
        )

        international = _flight(source_count=2, data_source="hasdata+juhe")
        international_result = calc_confidence(international, {}, [])
        self.assertEqual(international_result["dimensions"]["渠道一致性"], "高")
        self.assertEqual(
            international_result["details"]["渠道一致性"],
            "Google Flights多源交叉验证",
        )

    def test_fare_and_buyability_downgrade_reasons_are_details_only(self):
        cases = (
            ("partial", "possibly_available", "中", "中", "需要到支付页确认最终价、库存和票规"),
            ("missing", "unknown", "低", "低", "购买链路尚未验证"),
        )
        for fare_level, status, fare_expected, buy_expected, buy_detail in cases:
            with self.subTest(fare_level=fare_level, status=status):
                result = calc_confidence(
                    _flight(
                        fare_level=fare_level,
                        availability_status=status,
                        data_source="juhe",
                        route_type="international",
                        source_count=1,
                    ),
                    {},
                    [],
                )
                self.assertEqual(result["dimensions"]["票规完整度"], fare_expected)
                self.assertEqual(result["dimensions"]["可购买性"], buy_expected)
                self.assertEqual(
                    result["details"]["票规完整度"],
                    "行李/退改签仍需支付页确认",
                )
                self.assertEqual(result["details"]["可购买性"], buy_detail)
                self.assertNotIn("reason_codes", result)

    def test_collection_failure_and_degraded_flags_have_no_dedicated_component(self):
        flight = {
            "route_type": "international",
            "degraded": True,
            "source_degraded": True,
            "availability": {
                "status": "collection_failed",
                "degraded": True,
                "age_minutes": None,
                "source_count": 0,
            },
            "fare_verification": {"level": "missing"},
        }
        source_stats = {
            "juhe": {"status": "失败", "degraded": True},
            "serpapi": {"status": "skipped", "source_error": "quota"},
        }
        originals = copy.deepcopy((flight, source_stats))

        result = calc_confidence(flight, source_stats, [None] * 14)

        self.assert_result_shape(
            result,
            {
                "overall": "中",
                "dimensions": {
                    "价格新鲜度": "低",
                    "历史样本量": "高",
                    "渠道一致性": "低",
                    "票规完整度": "低",
                    "可购买性": "低",
                },
                "details": {
                    "价格新鲜度": "采集时间未知",
                    "历史样本量": "近期14次采集",
                    "渠道一致性": "数据源不足",
                    "票规完整度": "行李/退改签仍需支付页确认",
                    "可购买性": "购买链路尚未验证",
                },
            },
        )
        self.assertEqual((flight, source_stats), originals)

    def test_overall_thresholds_and_invalid_input_contract(self):
        high = calc_confidence(_flight(), {}, [None] * 14)
        medium_high = calc_confidence(
            _flight(age=31, source_count=2, fare_level="partial"),
            {},
            [None] * 5,
        )
        medium = calc_confidence(
            _flight(
                age=None,
                source_count=1,
                data_source="juhe",
                route_type="international",
                fare_level="missing",
                availability_status="unknown",
            ),
            {},
            [],
        )
        self.assertEqual((high["overall"], medium_high["overall"], medium["overall"]), ("高", "中高", "中"))

        with self.assertRaisesRegex(AttributeError, "get"):
            calc_confidence("not-a-flight", {}, [])
        with self.assertRaises(TypeError):
            calc_confidence({}, {}, 1)


if __name__ == "__main__":
    unittest.main()
