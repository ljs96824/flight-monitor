import unittest

from analyzer import apply_default_rules


class RouteTypeAlertDefaultsTest(unittest.TestCase):
    def test_international_route_uses_cross_border_alert_defaults(self):
        sub = apply_default_rules(
            {
                "monitor_mode": "quick",
                "basic": {"route_type": "international"},
                "notification_goals": {"primary": "buy_timing"},
                "soft_preferences": {},
                "hard_constraints": {},
            }
        )

        alerts = sub["notification_goals"]["secondary"]

        self.assertIn("large_price_drop", alerts)
        self.assertIn("transfer_risk_change", alerts)
        self.assertIn("interline_risk_change", alerts)
        self.assertNotIn("better_same_day", alerts)

    def test_domestic_route_keeps_dense_domestic_alert_defaults(self):
        sub = apply_default_rules(
            {
                "monitor_mode": "quick",
                "basic": {"route_type": "domestic"},
                "notification_goals": {"primary": "buy_timing"},
                "soft_preferences": {},
                "hard_constraints": {},
            }
        )

        alerts = sub["notification_goals"]["secondary"]

        self.assertIn("price_risk_alert", alerts)
        self.assertIn("better_same_day", alerts)
        self.assertNotIn("transfer_risk_change", alerts)


if __name__ == "__main__":
    unittest.main()
