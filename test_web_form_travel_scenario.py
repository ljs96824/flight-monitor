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
        jsonify=lambda *a, **k: {},
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
                    "monitor_mode": "precise",
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

    def test_precise_passenger_counts_replace_companion_radio_and_purposes_drive_scenarios(self):
        sub = build_subscription(
            _Form(
                {
                    "origin_select": "\u4e0a\u6d77",
                    "destination": "\u5927\u962a",
                    "round_trip": "false",
                    "depart_date": "2026-10-01",
                    "budget_strategy": "auto_judge",
                    "transfer_policy": "reasonable",
                    "baggage": "required",
                    "primary_goal": "buy_timing",
                    "monitor_mode": "precise",
                    "travel_scenario": ["tourism", "family"],
                    "passenger_adult": "2",
                    "passenger_child": "1",
                    "passenger_elderly": "2",
                    "passenger_infant": "0",
                    "notification_method": "pushplus",
                    "notification_frequency": "important_only",
                }
            )
        )

        self.assertEqual(sub["basic"]["passenger_count"], 5)
        self.assertEqual(sub["preferences"]["passengers"], {"adult": 2, "child": 1, "elderly": 2, "infant": 0})
        self.assertEqual(sub["preferences"]["travel_scenarios"], ["tourism", "family"])
        self.assertEqual(sub["soft_preferences"]["travel_scenarios"], ["tourism", "family"])
        self.assertEqual(sub["soft_preferences"]["travelers"], "with_elderly_child")

    def test_precise_passenger_counts_accept_canonical_count_field_names(self):
        sub = build_subscription(
            _Form(
                {
                    "origin_select": "\u4e0a\u6d77",
                    "destination": "\u5927\u962a",
                    "round_trip": "false",
                    "depart_date": "2026-10-01",
                    "budget_strategy": "auto_judge",
                    "transfer_policy": "reasonable",
                    "baggage": "required",
                    "primary_goal": "buy_timing",
                    "monitor_mode": "precise",
                    "travel_purpose": ["tourism", "family"],
                    "adult_count": "2",
                    "child_count": "1",
                    "elderly_count": "2",
                    "infant_count": "0",
                    "notification_method": "pushplus",
                    "notification_frequency": "important_only",
                }
            )
        )

        self.assertEqual(sub["basic"]["passenger_count"], 5)
        self.assertEqual(
            sub["preferences"]["passengers"],
            {"adult": 2, "child": 1, "elderly": 2, "infant": 0},
        )

    def test_quick_mode_ignores_hidden_precise_fields_and_derives_legacy_scene_fields(self):
        sub = build_subscription(
            _Form(
                {
                    "origin_select": "\u4e0a\u6d77",
                    "destination": "\u5927\u962a",
                    "round_trip": "false",
                    "depart_date": "2026-10-01",
                    "budget_strategy": "auto_judge",
                    "transfer_policy": "reasonable",
                    "baggage": "required",
                    "primary_goal": "buy_timing",
                    "monitor_mode": "quick",
                    "travel_scenario": ["tourism", "family"],
                    "travel_purpose": ["business"],
                    "trip_type": "business_meeting",
                    "passenger_count": "3",
                    "adult_count": "2",
                    "child_count": "1",
                    "elderly_count": "2",
                    "outbound_set_off": "14:00",
                    "return_set_off": "13:00",
                    "user_transport_min": "90",
                    "transport_margin_mode": "loose",
                    "redundancy_min": "40",
                    "notification_method": "pushplus",
                    "notification_frequency": "important_only",
                }
            )
        )

        self.assertEqual(sub["basic"]["passenger_count"], 3)
        self.assertEqual(sub["preferences"]["passengers"], {"adult": 3, "child": 0, "elderly": 0, "infant": 0})
        self.assertEqual(sub["soft_preferences"]["travel_scenarios"], ["tourism", "family"])
        self.assertEqual(sub["soft_preferences"]["travel_purposes"], ["tourism", "family"])
        self.assertEqual(sub["soft_preferences"]["trip_type"], "tourism")
        self.assertEqual(sub["preferences"]["travel_type"], "tourism")
        self.assertEqual(sub["constraints"]["outbound_set_off"], "")
        self.assertEqual(sub["constraints"]["return_set_off"], "")
        self.assertIsNone(sub["constraints"]["user_transport_min"])
        self.assertEqual(sub["constraints"]["transport_margin_mode"], "standard")
        self.assertEqual(sub["constraints"]["redundancy_min"], 25)


if __name__ == "__main__":
    unittest.main()
