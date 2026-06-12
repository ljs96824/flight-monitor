import sys
import types
import unittest


class _DummyFlask:
    def __init__(self, *args, **kwargs):
        pass

    def route(self, *args, **kwargs):
        return lambda func: func

    def get(self, *args, **kwargs):
        return lambda func: func

    def post(self, *args, **kwargs):
        return lambda func: func

    def run(self, *args, **kwargs):
        return None


sys.modules.setdefault("dotenv", types.SimpleNamespace(load_dotenv=lambda *a, **k: None))
sys.modules.setdefault(
    "flask",
    types.SimpleNamespace(
        Flask=_DummyFlask,
        redirect=lambda *a, **k: None,
        render_template_string=lambda *a, **k: "",
        request=types.SimpleNamespace(form={}),
        url_for=lambda *a, **k: "",
    ),
)

from analyzer import apply_default_rules
from web_form import build_subscription


class _Form(dict):
    def getlist(self, key):
        value = self.get(key, [])
        if isinstance(value, list):
            return value
        if value in (None, ""):
            return []
        return [value]


def _base_form(**overrides):
    data = {
        "origin_select": "PVG",
        "destination": "KIX",
        "round_trip": "false",
        "depart_date": "2026-10-01",
        "date_flexibility": "0",
        "transfer_policy": "reasonable",
        "baggage": "required",
        "primary_goal": "buy_timing",
        "travel_scenario": ["personal"],
        "companions": "solo",
        "notification_method": "pushplus",
        "notification_frequency": "important_only",
    }
    data.update(overrides)
    return _Form(data)


class FormPriceStrategyAlertsTest(unittest.TestCase):
    def test_price_strategy_alias_auto_judge_hides_budget_values_and_autofills_alerts(self):
        sub = build_subscription(
            _base_form(
                price_strategy="auto_judge",
                primary_goal="buy_timing",
            )
        )

        self.assertEqual(sub["constraints"]["budget_strategy"], "auto_judge")
        self.assertIsNone(sub["constraints"]["max_price"])
        self.assertIsNone(sub["constraints"]["ideal_price"])
        self.assertIsNone(sub["hard_constraints"]["max_budget"])
        self.assertIsNone(sub["hard_constraints"]["target_price"])
        self.assertEqual(
            sub["notification_goals"]["secondary"],
            ["price_risk_alert", "better_same_day"],
        )
        self.assertEqual(
            sub["advanced_rules"]["alerts"]["types"],
            ["price_risk_alert", "better_same_day"],
        )

    def test_apply_default_rules_uses_primary_goal_when_secondary_missing(self):
        sub = apply_default_rules(
            {
                "monitor_mode": "quick",
                "soft_preferences": {},
                "hard_constraints": {},
                "notification_goals": {"primary": "best_overall"},
            }
        )

        self.assertEqual(sub["notification_goals"]["secondary"], ["better_same_day"])

    def test_quick_mode_uses_main_frequency_and_default_secondary_alerts(self):
        sub = build_subscription(
            _base_form(
                monitor_mode="quick",
                primary_goal="buy_timing",
                notification_frequency="daily_digest",
                notification_frequency_rule="price_change",
                secondary_goals=["cheaper_date"],
                accept_overnight_transfer="true",
                accept_self_transfer="true",
                invoice_needed="true",
                trip_natures=["meeting"],
                team_passenger_count="9",
            )
        )

        self.assertEqual(sub["notification_goals"]["frequency"], "daily_digest")
        self.assertEqual(sub["advanced_rules"]["alerts"]["frequency"], "daily_digest")
        self.assertEqual(sub["notification_goals"]["secondary"], ["price_risk_alert", "better_same_day"])
        self.assertEqual(sub["advanced_rules"]["alerts"]["types"], ["price_risk_alert", "better_same_day"])
        self.assertFalse(sub["hard_constraints"]["accept_overnight_transfer"])
        self.assertFalse(sub["hard_constraints"]["accept_self_transfer"])
        self.assertFalse(sub["advanced_rules"]["transfer"]["overnight_transfer"])
        self.assertFalse(sub["advanced_rules"]["transfer"]["self_transfer"])
        self.assertEqual(sub["constraints"]["trip_natures"], [])
        self.assertFalse(sub["preferences"]["invoice_needed"])

    def test_precise_non_business_scene_ignores_business_rule_residuals(self):
        sub = build_subscription(
            _base_form(
                monitor_mode="precise",
                travel_scenario=["tourism"],
                invoice_needed="true",
                invoice_special_vat="true",
                invoice_cabin_limit="true",
                team_passenger_count="9",
                reimburse_per_person="5000",
            )
        )

        self.assertEqual(sub["constraints"]["trip_natures"], [])
        self.assertFalse(sub["preferences"]["invoice_needed"])
        self.assertFalse(sub["preferences"]["invoice_special_vat"])
        self.assertFalse(sub["preferences"]["invoice_cabin_limit"])
        self.assertIsNone(sub["constraints"]["team_passenger_count"])
        self.assertIsNone(sub["constraints"]["reimburse_per_person"])


if __name__ == "__main__":
    unittest.main()
