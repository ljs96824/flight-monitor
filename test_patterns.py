import sqlite3
import tempfile
import unittest
from pathlib import Path

from patterns import build_patterns, build_route_patterns


class PatternsTest(unittest.TestCase):
    def test_rates_keep_raw_percent_and_n(self):
        rows = []
        for index in range(10):
            day = f"2026-07-{index + 1:02d}"
            rows.append({"observed_at": day + "T09:00:00", "depart_date": "2026-10-01", "flight_combo": "MU225", "price_cny": 100 + index, "airline": "MU", "stops": 0})
            if index < 2:
                rows.append({"observed_at": day + "T09:00:00", "depart_date": "2026-10-01", "flight_combo": "JL891", "price_cny": 90, "airline": "JL", "stops": 0})
        result = build_patterns(rows, min_n=1)
        labels = {item["combo"]: item["label"] for item in result["combo_occurrence"]}
        self.assertEqual(labels["MU225"], "常驻(100%·n=10)")
        self.assertEqual(labels["JL891"], "偶发(20%·n=2)")

    def test_weekday_deduplicates_depart_dates_and_supply_has_basis(self):
        rows = [
            {"observed_at": "2026-07-01T09:00:00", "depart_date": "2026-10-01", "flight_combo": "MU225", "price_cny": 100, "airline": "MU", "stops": 0},
            {"observed_at": "2026-07-02T09:00:00", "depart_date": "2026-10-01", "flight_combo": "MU225", "price_cny": 101, "airline": "MU", "stops": 0},
            {"observed_at": "2026-07-02T09:00:00", "depart_date": "2026-10-02", "flight_combo": "MU225+JL100", "price_cny": 80, "airline": "MU", "stops": 1},
        ]
        result = build_patterns(rows, min_n=1)
        self.assertEqual(result["weekday_stability"][0]["depart_date_n"], 1)
        self.assertEqual(result["supply_mix"]["basis"], "基于组合结构")

    def test_departure_period_gap_is_explicit(self):
        result = build_patterns([], min_n=5)
        self.assertEqual(result["departure_period"]["status"], "字段不可得")
        self.assertIn("面板未存起飞时刻(obs_store v1)", result["departure_period"]["reason"])
        self.assertIn("待schema扩展后自动点亮", result["departure_period"]["reason"])


if __name__ == "__main__":
    unittest.main()
