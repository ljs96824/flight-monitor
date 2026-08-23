import unittest


class NoResultDiagnosisTest(unittest.TestCase):
    def test_candidate_summary_uses_its_own_rejection_reason(self):
        from analyzer import build_no_result_diagnosis

        candidates = [
            {"flight_no": "9C6581", "flight_combo": "9C6581", "price": 2885},
            {"flight_no": "HO1337", "flight_combo": "HO1337", "price": 3761},
        ]
        excluded = [
            {
                "flight": {
                    "flight_no": "BR705|BR182",
                    "flight_combo": "BR705+BR182",
                    "price": 2500,
                },
                "price": 2500,
                "reason": "需要中转，但你设置了必须直飞",
                "filter_reason_code": "direct_only",
            },
            {
                "flight": candidates[0],
                "price": 2885,
                "reason": "命中你设置的排除廉航条件",
                "filter_reason_code": "lcc_excluded",
                "filter_reason_value": "9C6581:9C(operating)",
            },
        ]

        result = build_no_result_diagnosis(
            candidates,
            excluded,
            stage_counts={"total_candidates": 2, "valid_price_count": 2},
            fallback_reason="返程采集失败，无法组成完整往返",
        )

        self.assertEqual(result["price_summary"]["lowest"], 2885)
        self.assertEqual(
            result["price_summary"]["reason"],
            "命中你设置的排除廉航条件",
        )
        self.assertNotEqual(result["counts"]["max_bottleneck"].get("key"), "direct")

    def test_unmatched_candidate_uses_pairing_failure_not_unrelated_bucket(self):
        from analyzer import build_no_result_diagnosis

        result = build_no_result_diagnosis(
            [{"flight_no": "9C6581", "flight_combo": "9C6581", "price": 2885}],
            [
                {
                    "flight": {
                        "flight_no": "BR705|BR182",
                        "flight_combo": "BR705+BR182",
                        "price": 2500,
                    },
                    "reason": "需要中转，但你设置了必须直飞",
                    "filter_reason_code": "direct_only",
                }
            ],
            stage_counts={"total_candidates": 1, "valid_price_count": 1},
            fallback_reason="返程采集失败，无法组成完整往返",
        )

        self.assertEqual(result["price_summary"]["lowest"], 2885)
        self.assertEqual(
            result["price_summary"]["reason"],
            "返程采集失败，无法组成完整往返",
        )
        self.assertNotIn("直飞要求不符", result["price_summary"]["reason"])
        self.assertIn("返程采集失败", result["reason"])
        self.assertNotIn("剩余1个完全匹配", result["reason"])
        self.assertEqual(result["counts"]["max_bottleneck"]["key"], "roundtrip_pairing")

    def test_candidate_without_exact_reason_never_uses_stage_bucket(self):
        from analyzer import build_no_result_diagnosis

        result = build_no_result_diagnosis(
            [{"flight_no": "9C6581", "flight_combo": "9C6581", "price": 2885}],
            [],
            stage_counts={
                "total_candidates": 1,
                "valid_price_count": 1,
                "after_basic_filter": 0,
            },
        )

        self.assertEqual(
            result["price_summary"]["reason"],
            "该候选的逐航班拒因未保留",
        )
        self.assertEqual(result["price_summary"]["source"], "reason_unavailable")
        self.assertNotIn("直飞要求不符", result["price_summary"]["reason"])

    def test_alternative_carries_explicit_unmet_reason(self):
        from analyzer import build_no_result_alternatives

        alternatives = build_no_result_alternatives(
            [
                {
                    "flight_no": "MM080",
                    "flight_combo": "MM80",
                    "price": 4905,
                    "arrival_time": "2026-10-01 09:35",
                }
            ],
            [],
            default_reason="返程采集失败，无法组成完整往返",
        )

        self.assertEqual(len(alternatives), 1)
        self.assertEqual(
            alternatives[0]["unmet_reason"],
            "返程采集失败，无法组成完整往返",
        )
        self.assertEqual(alternatives[0]["tradeoff"], alternatives[0]["unmet_reason"])
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



