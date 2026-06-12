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
try:
    import flask  # noqa: F401
except ModuleNotFoundError:
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

from web_form import FORM_TEMPLATE
from web_form import build_subscription


class WebFormTemplateStep2Test(unittest.TestCase):
    def test_missing_required_items_render_as_list(self):
        self.assertIn('id="required-missing-list"', FORM_TEMPLATE)
        self.assertIn("\u57fa\u7840\u9879\u5df2\u5b8c\u6210", FORM_TEMPLATE)
        self.assertIn("missingRequiredLabels(currentStep)", FORM_TEMPLATE)

    def test_destination_airport_tags_and_summary_hooks_exist(self):
        self.assertIn('id="destination-airport-tags"', FORM_TEMPLATE)
        self.assertIn('name="destination_airports_active"', FORM_TEMPLATE)
        self.assertIn("\u662f\u5426\u53ea\u641c\u7d22\u67d0\u4e2a\u673a\u573a", FORM_TEMPLATE)
        self.assertIn("\u5373\u5c06\u521b\u5efa\u7684\u76d1\u63a7", FORM_TEMPLATE)
        self.assertIn("\u786e\u8ba4\u5e76\u5f00\u59cb\u76d1\u63a7", FORM_TEMPLATE)

    def test_transfer_rules_use_declarative_visibility(self):
        self.assertIn('data-show-if="transfer_policy=reasonable|price_first"', FORM_TEMPLATE)
        self.assertIn('data-show-if="transfer_policy=price_first"', FORM_TEMPLATE)

    def test_time_business_and_reminder_reduction_markers_exist(self):
        self.assertIn('id="custom-time-options" data-show-if="time_preference=custom"', FORM_TEMPLATE)
        self.assertIn('id="precise-time-toggle"', FORM_TEMPLATE)
        self.assertIn('data-show-if="time_preference=custom"', FORM_TEMPLATE)
        self.assertIn('id="business-rules-module" data-show-if="business_context=true"', FORM_TEMPLATE)
        self.assertIn('id="notification_frequency_rule_shadow"', FORM_TEMPLATE)
        self.assertIn("syncNotificationFrequencyShadow", FORM_TEMPLATE)
        self.assertNotIn('id="advanced-frequency-copy"', FORM_TEMPLATE)

    def test_same_day_round_trip_field_exists(self):
        self.assertIn('name="same_day_round_trip"', FORM_TEMPLATE)
        self.assertIn('name="business_start"', FORM_TEMPLATE)
        self.assertIn('name="business_end"', FORM_TEMPLATE)
        self.assertIn('name="user_transport_min"', FORM_TEMPLATE)
        self.assertIn('name="redundancy_min"', FORM_TEMPLATE)
        self.assertIn('name="transport_margin_mode"', FORM_TEMPLATE)
        self.assertIn('name="outbound_set_off"', FORM_TEMPLATE)
        self.assertIn('name="return_set_off"', FORM_TEMPLATE)
        self.assertIn("行程可行性分析", FORM_TEMPLATE)
        self.assertIn('id="airport-buffer-preview"', FORM_TEMPLATE)
        self.assertIn('data-show-if="same_day_round_trip=true"', FORM_TEMPLATE)
        self.assertIn("syncSameDayRoundTrip", FORM_TEMPLATE)

    def test_business_cabin_policy_fields_exist(self):
        self.assertIn('name="trip_natures"', FORM_TEMPLATE)
        self.assertIn('value="business"', FORM_TEMPLATE)
        self.assertIn('value="meeting"', FORM_TEMPLATE)
        self.assertIn('value="team_building"', FORM_TEMPLATE)
        self.assertIn('data-show-if="trip_natures=business|meeting|team_building"', FORM_TEMPLATE)
        self.assertIn('data-show-if="trip_natures=meeting"', FORM_TEMPLATE)
        self.assertIn('name="meeting_start"', FORM_TEMPLATE)
        self.assertIn('name="meeting_end"', FORM_TEMPLATE)
        self.assertIn('data-show-if="trip_natures=team_building"', FORM_TEMPLATE)
        self.assertIn('name="team_date_flexibility"', FORM_TEMPLATE)
        self.assertIn('name="same_flight_required"', FORM_TEMPLATE)
        self.assertIn('data-show-if="trip_natures=business"', FORM_TEMPLATE)
        self.assertIn('name="cabin_policy"', FORM_TEMPLATE)
        self.assertIn('name="team_passenger_count"', FORM_TEMPLATE)
        self.assertIn('name="cabin_arrangement"', FORM_TEMPLATE)
        self.assertIn('value="economy_all"', FORM_TEMPLATE)
        self.assertIn('value="business_all"', FORM_TEMPLATE)
        self.assertIn('value="mixed"', FORM_TEMPLATE)
        self.assertIn('data-show-if="cabin_arrangement=mixed"', FORM_TEMPLATE)
        self.assertIn("validateCabinArrangement", FORM_TEMPLATE)
        self.assertIn('name="user_level"', FORM_TEMPLATE)
        self.assertIn('name="business_seats"', FORM_TEMPLATE)
        self.assertIn('name="economy_seats"', FORM_TEMPLATE)
        self.assertIn('name="reimburse_per_person"', FORM_TEMPLATE)
        self.assertIn('data-show-if="cabin_policy=level_based"', FORM_TEMPLATE)

    def test_route_type_selector_and_domestic_invoice_fields_exist(self):
        self.assertIn('name="route_type"', FORM_TEMPLATE)
        self.assertIn('value="domestic"', FORM_TEMPLATE)
        self.assertIn('value="international"', FORM_TEMPLATE)
        self.assertIn('value="greater_china"', FORM_TEMPLATE)
        self.assertIn('data-show-if="route_type=domestic|greater_china"', FORM_TEMPLATE)
        self.assertIn('data-show-if="route_type=international|greater_china"', FORM_TEMPLATE)
        self.assertIn('name="invoice_needed"', FORM_TEMPLATE)
        self.assertIn('name="invoice_special_vat"', FORM_TEMPLATE)
        self.assertIn('name="invoice_cabin_limit"', FORM_TEMPLATE)
        self.assertIn("autoDetectRouteType", FORM_TEMPLATE)
        self.assertIn("港澳通行证/台湾通行证", FORM_TEMPLATE)
        self.assertIn("国内OTA", FORM_TEMPLATE)
        self.assertIn('data-show-if="route_type=international">国际航线会把行李规则、时区时差、过境签', FORM_TEMPLATE)

    def test_same_day_round_trip_is_saved_as_constraint(self):
        class Form(dict):
            def getlist(self, key):
                value = self.get(key)
                if value is None:
                    return []
                return value if isinstance(value, list) else [value]

        form = Form(
            {
                "round_trip": "false",
                "monitor_mode": "precise",
                "same_day_round_trip": "true",
                "business_start": "10:00",
                "business_end": "16:00",
                "user_transport_min": "60",
                "redundancy_min": "25",
                "transport_margin_mode": "loose",
                "outbound_set_off": "14:00",
                "return_set_off": "12:30",
                "origin_select": "PVG",
                "destination": "PEK",
                "depart_date": "2026-06-10",
                "price_strategy": "auto_judge",
                "travel_scenario": ["business"],
                "transfer_policy": "reasonable",
                "baggage": "required",
                "primary_goal": "buy_timing",
                "notification_method": "pushplus",
                "notification_frequency": "important_only",
            }
        )

        subscription = build_subscription(form)

        self.assertTrue(subscription["round_trip"])
        self.assertTrue(subscription["same_day_round_trip"])
        self.assertTrue(subscription["constraints"]["same_day_round_trip"])
        self.assertEqual(subscription["constraints"]["business_start"], "10:00")
        self.assertEqual(subscription["constraints"]["business_end"], "16:00")
        self.assertEqual(subscription["constraints"]["user_transport_min"], 60)
        self.assertEqual(subscription["constraints"]["redundancy_min"], 25)
        self.assertEqual(subscription["constraints"]["transport_margin_mode"], "loose")
        self.assertEqual(subscription["constraints"]["outbound_set_off"], "14:00")
        self.assertEqual(subscription["constraints"]["return_set_off"], "12:30")
        self.assertEqual(subscription["constraints"]["time_source"], "meeting_derived")
        self.assertEqual(subscription["hard_constraints"]["time_source"], "meeting_derived")
        self.assertEqual(subscription["basic"]["return_date"], "2026-06-10")

    def test_same_day_business_time_fields_are_available_in_quick_mode(self):
        self.assertIn('id="same-day-business-fields" data-show-if="same_day_round_trip=true"', FORM_TEMPLATE)
        self.assertNotIn('id="same-day-business-fields" class="precise-only"', FORM_TEMPLATE)
        self.assertIn("会议/办事开始时间", FORM_TEMPLATE)
        self.assertIn("快速模式会按机场等级、车程估算和25分钟冗余自动预留。", FORM_TEMPLATE)

    def test_precise_meeting_mode_takes_over_time_preferences(self):
        self.assertIn('id="meeting-time-handoff-card"', FORM_TEMPLATE)
        self.assertIn('id="time-preference-controls"', FORM_TEMPLATE)
        self.assertIn("时间安排已由会议模式接管", FORM_TEMPLATE)
        self.assertIn("updateMeetingTimeHandoff", FORM_TEMPLATE)
        self.assertIn("会议模式将接管时间设置", FORM_TEMPLATE)

    def test_business_cabin_policy_is_saved_as_constraint(self):
        class Form(dict):
            def getlist(self, key):
                value = self.get(key)
                if value is None:
                    return []
                return value if isinstance(value, list) else [value]

        form = Form(
            {
                "monitor_mode": "precise",
                "round_trip": "false",
                "origin_select": "PVG",
                "destination": "PEK",
                "depart_date": "2026-06-10",
                "price_strategy": "auto_judge",
                "travel_scenario": ["business"],
                "transfer_policy": "reasonable",
                "baggage": "required",
                "primary_goal": "buy_timing",
                "notification_method": "pushplus",
                "notification_frequency": "important_only",
                "adult_count": "2",
                "child_count": "0",
                "elderly_count": "0",
                "infant_count": "0",
                "trip_natures": ["business", "meeting", "team_building"],
                "meeting_start": "10:00",
                "meeting_end": "16:00",
                "team_date_flexibility": "flexible",
                "same_flight_required": "true",
                "team_passenger_count": "8",
                "cabin_arrangement": "mixed",
                "cabin_policy": "level_based",
                "user_level": "director",
                "business_seats": "2",
                "economy_seats": "6",
                "reimburse_per_person": "5000",
            }
        )

        subscription = build_subscription(form)

        self.assertEqual(subscription["constraints"]["trip_natures"], ["business", "meeting", "team_building"])
        self.assertEqual(subscription["constraints"]["trip_nature"], "meeting")
        self.assertEqual(subscription["constraints"]["meeting_start"], "10:00")
        self.assertEqual(subscription["constraints"]["meeting_end"], "16:00")
        self.assertEqual(subscription["constraints"]["business_start"], "10:00")
        self.assertEqual(subscription["constraints"]["business_end"], "16:00")
        self.assertEqual(subscription["constraints"]["team_date_flexibility"], "flexible")
        self.assertTrue(subscription["constraints"]["same_flight_required"])
        self.assertEqual(subscription["constraints"]["cabin_arrangement"], "mixed")
        self.assertEqual(subscription["constraints"]["cabin_policy"], "level_based")
        self.assertEqual(subscription["constraints"]["user_level"], "director")
        self.assertEqual(subscription["constraints"]["business_seats"], 2)
        self.assertEqual(subscription["constraints"]["economy_seats"], 6)
        self.assertEqual(subscription["basic"]["passenger_count"], 8)
        self.assertEqual(subscription["constraints"]["reimburse_per_person"], 5000)
        self.assertEqual(subscription["hard_constraints"]["cabin_policy"], "level_based")

    def test_duplicate_travel_scene_fields_are_removed_from_ui(self):
        self.assertIn('name="travel_scenario"', FORM_TEMPLATE)
        self.assertNotIn('name="travel_purpose"', FORM_TEMPLATE)
        self.assertNotIn('name="trip_type"', FORM_TEMPLATE)
        self.assertIn('class="smart-panel precise-only"', FORM_TEMPLATE)
        self.assertIn('disablePreciseOnlyFields', FORM_TEMPLATE)

    def test_route_type_and_domestic_invoice_are_saved(self):
        class Form(dict):
            def getlist(self, key):
                value = self.get(key)
                if value is None:
                    return []
                return value if isinstance(value, list) else [value]

        form = Form(
            {
                "monitor_mode": "precise",
                "route_type": "domestic",
                "round_trip": "false",
                "origin_select": "PVG",
                "destination": "PEK",
                "depart_date": "2026-06-10",
                "price_strategy": "auto_judge",
                "travel_scenario": ["business"],
                "transfer_policy": "reasonable",
                "baggage": "required",
                "primary_goal": "buy_timing",
                "notification_method": "pushplus",
                "notification_frequency": "important_only",
                "invoice_needed": "true",
                "invoice_special_vat": "true",
                "invoice_cabin_limit": "true",
            }
        )

        subscription = build_subscription(form)

        self.assertEqual(subscription["basic"]["route_type"], "domestic")
        self.assertTrue(subscription["preferences"]["invoice_needed"])
        self.assertTrue(subscription["preferences"]["invoice_special_vat"])
        self.assertTrue(subscription["preferences"]["invoice_cabin_limit"])


if __name__ == "__main__":
    unittest.main()
