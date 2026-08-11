import unittest
from unittest.mock import patch

from forecast import build_notification_forecast
from notifier import _email_forecast_body


class ForecastNotificationTest(unittest.TestCase):
    def test_market_envelope_has_no_constraint_fingerprint(self):
        cells = []
        for depart_index in range(3):
            depart = f"2026-10-{1 + depart_index:02d}"
            for offset in range(12):
                cells.append({"depart_date": depart, "observed_day": f"2026-09-{10 + offset:02d}", "days_to_departure": 21 + depart_index - offset, "min_price": 100 + offset, "degraded": False, "min_sources": ["juhe"]})
        with patch("forecast.load_tcurve_daily_cells", return_value=cells), patch("forecast.walk_forward_backtest") as mocked, patch("forecast.predict_price", return_value={"status": "ok", "median": 100, "p25": 90, "p75": 110, "p10": 80, "p90": 120}):
            mocked.return_value = {"horizons": {"3": {"n": 8, "model": {"mape": 5}, "naive": {"mape": 6}, "tcurve": {"mape": 7}, "skill_gate": {"passed": True}}}}
            result = build_notification_forecast({"origin": "PVG", "destination": "KIX", "depart_date": "2026-10-01"}, db_path="unused", as_of_day="2026-09-21")
        self.assertTrue(result["eligible"])
        self.assertIn("市场最低参考价·单人单程·与用户筛选无关", result["provenance"]["bucket"])
        self.assertNotIn("约束=", result["provenance"]["bucket"])

    def test_email_section_requires_eligible_payload(self):
        self.assertEqual(_email_forecast_body({}), "")
        payload = {"forecast": {"eligible": True, "predictions": [{"target_day": "2026-09-22", "median": 100, "p25": 90, "p75": 110, "p10": 80, "p90": 120}], "backtest": {"model": {"mape": 5}, "naive": {"mape": 7}}, "current_market_reference": {"median": 100}}, "price_tiers": {"unit_roundtrip": 300}}
        body = _email_forecast_body(payload)
        self.assertIn("价格预测", "价格预测")
        self.assertIn("与你的筛选条件无关", body)
        self.assertIn("非承诺", body)


if __name__ == "__main__":
    unittest.main()
