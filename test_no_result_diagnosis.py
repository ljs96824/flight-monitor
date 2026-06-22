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


if __name__ == "__main__":
    unittest.main()
