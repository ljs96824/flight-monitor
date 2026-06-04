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

from web_form import build_subscription


class _Form(dict):
    def getlist(self, key):
        value = self.get(key, [])
        if isinstance(value, list):
            return value
        if value in (None, ""):
            return []
        return [value]


class WebFormTravelScenarioTest(unittest.TestCase):
    def test_build_subscription_stores_travel_scenario_and_companion_details(self):
        sub = build_subscription(
            _Form(
                {
                    "origin_select": "上海",
                    "destination": "大阪",
                    "round_trip": "false",
                    "depart_date": "2026-10-01",
                    "date_flexibility": "0",
                    "budget_strategy": "explicit",
                    "max_budget_mode": "fixed",
                    "max_budget": "8000",
                    "target_price_mode": "fixed",
                    "target_price": "6000",
                    "transfer_policy": "reasonable",
                    "baggage": "required",
                    "primary_goal": "buy_timing",
                    "travel_scenario": ["tourism", "family"],
                    "companions": "with_child",
                    "companion_constraints": [
                        "direct_preferred",
                        "no_redeye",
                        "need_baggage",
                    ],
                    "solo_travel": "true",
                    "no_late_arrival": "true",
                    "notification_method": "pushplus",
                    "notification_frequency": "important_only",
                }
            )
        )

        soft = sub["soft_preferences"]
        prefs = sub["preferences"]

        self.assertEqual(soft["travel_scenario"], "tourism")
        self.assertEqual(soft["travel_scenarios"], ["tourism", "family"])
        self.assertEqual(soft["companions"], "with_child")
        self.assertEqual(soft["travelers"], "with_child")
        self.assertEqual(
            soft["companion_constraints"],
            ["direct_preferred", "no_redeye", "need_baggage"],
        )
        self.assertTrue(soft["solo_travel"])
        self.assertTrue(soft["no_late_arrival"])
        self.assertEqual(prefs["travel_scenario"], "tourism")
        self.assertEqual(prefs["travel_scenarios"], ["tourism", "family"])
        self.assertEqual(prefs["travelers"], "with_child")


if __name__ == "__main__":
    unittest.main()
