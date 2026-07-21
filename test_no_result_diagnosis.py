import unittest


class NoResultDiagnosisTest(unittest.TestCase):
    def test_diagnosis_reports_largest_bottleneck_from_stage_counts(self):
        from analyzer import build_no_result_diagnosis

        flights = [{"flight_no": f"MU{i}", "price": 800 + i} for i in range(10)]

        result = build_no_result_diagnosis(
            flights,
            [],
            {"business_start": "13:00", "business_end": "17:00", "max_budget": 2000},
            {
                "total_candidates": 10,
                "valid_price_count": 10,
                "after_basic_filter": 8,
                "after_meeting_window": 2,
                "after_budget": 0,
            },
        )

        counts = result["counts"]
        self.assertEqual(counts["reason_counts"]["direct"], 2)
        self.assertEqual(counts["reason_counts"]["meeting"], 6)
        self.assertEqual(counts["reason_counts"]["budget"], 2)
        self.assertEqual(counts["max_bottleneck"]["key"], "meeting")
        self.assertEqual(counts["max_bottleneck"]["count"], 6)
        self.assertIn("10", result["reason"])
        self.assertIn("6", result["reason"])
        self.assertIn("最大卡点", result["reason"])



    def test_same_day_combo_zero_reports_no_full_match(self):
        from analyzer import build_no_result_diagnosis

        flights = [{"flight_no": f"MU{i}", "price": 800 + i} for i in range(10)]

        result = build_no_result_diagnosis(
            flights,
            [],
            {"business_start": "10:30", "business_end": "17:00", "max_budget": 1700},
            {
                "total_candidates": 10,
                "valid_price_count": 10,
                "outbound_collected": 5,
                "return_collected": 5,
                "after_meeting_outbound": 0,
                "after_meeting_return": 4,
                "after_meeting_window": 4,
                "same_day_combos": 0,
            },
        )

        self.assertEqual(result["counts"]["after_meeting_window"], 0)
        self.assertEqual(result["counts"]["reason_counts"]["meeting"], 10)
        self.assertEqual(result["counts"]["primary_cause"], "outbound_time")
        self.assertEqual(result["counts"]["max_bottleneck"]["pool_scope"], "去程池")
        self.assertEqual(result["counts"]["max_bottleneck"]["count"], 5)
        self.assertEqual(result["counts"]["max_bottleneck"]["ratio"], 100.0)
        self.assertIn("本次无方案主因是【去程时间】", result["reason"])
        self.assertIn("返程有4个可选,非阻塞", result["reason"])
        self.assertNotIn("时间窗口排除最多", result["reason"])
        self.assertIn("剩余0个完全匹配", result["reason"])
        self.assertNotIn("剩余4个完全匹配", result["reason"])


    def test_return_lowerbound_count_overrides_gate_zero_for_no_result_cause(self):
        from analyzer import build_no_result_diagnosis

        result = build_no_result_diagnosis(
            [],
            [],
            {"business_start": "10:30", "business_end": "17:00", "max_budget": 1700},
            {
                "total_candidates": 10,
                "valid_price_count": 10,
                "after_meeting_outbound": 0,
                "after_meeting_return": 0,
                "return_after_lowerbound": 4,
                "same_day_combos": 0,
            },
        )

        self.assertEqual(result["counts"]["primary_cause"], "outbound_time")
        self.assertIn("\u8fd4\u7a0b\u67094\u4e2a\u53ef\u9009,\u975e\u963b\u585e", result["reason"])
        self.assertNotIn("\u8fd4\u7a0b\u6682\u65e0\u7b26\u5408\u822a\u73ed", result["reason"])


    def test_same_day_budget_primary_uses_lowest_excluded_combo_same_scope(self):
        from analyzer import build_no_result_diagnosis

        result = build_no_result_diagnosis(
            [],
            [],
            {
                "max_budget": 1700,
                "target_price": 1200,
                "budget_scope": "per_person",
                "max_budget_scope": "per_person",
                "passengers": {"adult": 3, "child": 0, "elderly": 0, "infant": 0},
                "route_type": "domestic",
            },
            {
                "total_candidates": 4,
                "valid_price_count": 4,
                "after_meeting_outbound": 2,
                "after_meeting_return": 2,
                "after_meeting_window": 2,
                "same_day_combos": 2,
                "after_budget": 0,
                "budget_excluded_candidates": [
                    {
                        "outbound_flight": "MU5099",
                        "return_flight": "MU5170",
                        "outbound_price": 831,
                        "return_price": 1720,
                    },
                    {
                        "outbound_flight": "MU5121",
                        "return_flight": "MU5170",
                        "outbound_price": 980,
                        "return_price": 1720,
                    },
                ],
            },
        )

        self.assertEqual(result["counts"]["primary_cause"], "budget")
        self.assertEqual(result["price_summary"]["lowest"], 2551)
        self.assertEqual(result["price_summary"]["price_scope"], "per_person_roundtrip")
        self.assertIn("最低候选 ¥2,551 单人往返 vs 预算 ¥1,700 单人往返,超出 ¥851", result["reason"])
        self.assertNotIn("¥1,200", result["reason"])

if __name__ == "__main__":
    unittest.main()



