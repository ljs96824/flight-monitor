import re
import unittest

import web_form
from form_pages import OPTIONS
from web_form import build_subscription


class Form(dict):
    def getlist(self, key):
        value = self.get(key)
        if value is None:
            return []
        return value if isinstance(value, list) else [value]


class WebFormTemplateStep2Test(unittest.TestCase):
    def setUp(self):
        web_form.app.config.update(TESTING=True)
        client = web_form.app.test_client()
        self.quick = client.get("/").get_data(as_text=True)
        self.full = client.get("/settings").get_data(as_text=True)

    def test_quick_page_has_core_fields_and_no_wizard_progress_mechanics(self):
        for name in (
            "origin_select",
            "destination",
            "depart_date",
            "round_trip",
            "passenger_count",
            "max_budget",
            "target_price",
            "travel_scenario",
        ):
            expected = len(OPTIONS[name]) if name == "travel_scenario" else 1
            self.assertEqual(self.quick.count(f'name="{name}"'), expected, name)
        self.assertNotIn("required-missing-list", self.quick)
        self.assertNotIn("missingRequiredLabels", self.quick)

    def test_destination_candidates_and_price_summary_hooks_exist(self):
        self.assertIn('id="destination-candidates"', self.quick)
        self.assertIn('name="destination_airports_active"', self.quick)
        self.assertIn('id="destination-status"', self.quick)
        self.assertIn('id="price-hint"', self.quick)
        self.assertIn("renderCandidates", self.quick)
        self.assertIn("updatePriceHint", self.quick)

    def test_transfer_rules_are_single_full_page_controls_without_visibility_dsl(self):
        for name in (
            "transfer_policy",
            "short_transfer_limit",
            "accept_self_transfer",
            "accept_overnight_transfer",
        ):
            self.assertEqual(self.full.count(f'name="{name}"'), 1, name)
        self.assertNotIn("data-show-if", self.full)

    def test_time_business_and_reminder_fields_have_one_surface(self):
        for name in (
            "time_preference",
            "allow_redeye",
            "arrival_preference",
            "separate_direction_times",
            "travel_scenario",
            "notification_frequency",
            "notification_frequency_rule",
        ):
            tags = re.findall(
                rf'<(?:input|select|textarea)\b[^>]*\bname="{re.escape(name)}"[^>]*>',
                self.full,
                re.I,
            )
            self.assertTrue(tags, name)
            if len(tags) == 1:
                continue
            type_matches = [
                re.search(r'\btype="(radio|checkbox)"', tag, re.I) for tag in tags
            ]
            self.assertTrue(
                all(type_matches),
                name,
            )
            choice_types = {match.group(1).lower() for match in type_matches}
            self.assertEqual(len(choice_types), 1, name)
            values = [
                re.search(r'\bvalue="([^"]*)"', tag, re.I).group(1)
                for tag in tags
            ]
            self.assertEqual(len(values), len(set(values)), name)
        self.assertNotIn("syncNotificationFrequencyShadow", self.full)
        self.assertNotIn("advanced-frequency-copy", self.full)

    def test_same_day_round_trip_fields_exist_on_full_page(self):
        for name in (
            "same_day_round_trip",
            "business_start",
            "business_end",
            "user_transport_min",
            "meeting_importance",
            "origin_transport_min",
            "destination_transport_min",
            "airport_advance_min",
            "arrival_exit_min",
            "delay_buffer_min",
            "pre_meeting_buffer_min",
            "post_meeting_buffer_min",
            "custom_redundancy_min",
            "outbound_set_off",
            "return_set_off",
        ):
            self.assertEqual(self.full.count(f'name="{name}"'), 1, name)
        self.assertNotIn("syncSameDayRoundTrip", self.full)

    def test_business_cabin_policy_fields_exist_once(self):
        for name in (
            "trip_natures",
            "meeting_start",
            "meeting_end",
            "team_date_flexibility",
            "same_flight_required",
            "cabin_policy",
            "team_passenger_count",
            "cabin_arrangement",
            "user_level",
            "business_seats",
            "economy_seats",
            "reimburse_per_person",
        ):
            self.assertEqual(self.full.count(f'name="{name}"'), 1, name)

    def test_route_type_is_read_only_and_domestic_invoice_fields_exist_once(self):
        for name in (
            "route_type",
            "invoice_needed",
            "invoice_special_vat",
            "invoice_cabin_limit",
        ):
            self.assertEqual(self.full.count(f'name="{name}"'), 1, name)
        self.assertIn('type="hidden" id="field-route-type" name="route_type"', self.full)
        self.assertIn('data-route-type-badge="true"', self.full)
        self.assertIn('data-route-type-label>待识别</strong>', self.full)
        self.assertNotIn('<select id="field-route-type"', self.full)

    def test_same_day_round_trip_is_saved_as_constraint(self):
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
        self.assertEqual(subscription["basic"]["return_date"], "2026-06-10")

    def test_same_day_business_fields_belong_to_full_settings_not_quick_page(self):
        for name in (
            "same_day_round_trip",
            "business_start",
            "business_end",
            "meeting_location",
            "meeting_importance",
        ):
            self.assertNotIn(f'name="{name}"', self.quick)
            self.assertEqual(self.full.count(f'name="{name}"'), 1, name)

    def test_quick_same_day_submission_still_stores_meeting_facts(self):
        form = Form(
            {
                "round_trip": "false",
                "monitor_mode": "quick",
                "same_day_round_trip": "true",
                "business_start": "10:00",
                "business_end": "17:00",
                "meeting_location": "国贸",
                "meeting_importance": "critical",
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
        self.assertTrue(subscription["constraints"]["same_day_round_trip"])
        self.assertEqual(subscription["constraints"]["business_start"], "10:00")
        self.assertEqual(subscription["constraints"]["business_end"], "17:00")
        self.assertEqual(subscription["constraints"]["meeting_location"], "国贸")
        self.assertEqual(subscription["constraints"]["meeting_importance"], "critical")
        self.assertEqual(subscription["constraints"]["time_source"], "meeting_derived")

    def test_meeting_and_time_controls_are_static_not_handoff_panels(self):
        self.assertIn('name="meeting_start"', self.full)
        self.assertIn('name="time_preference"', self.full)
        self.assertNotIn("meeting-time-handoff-card", self.full)
        self.assertNotIn("updateMeetingTimeHandoff", self.full)

    def test_business_cabin_policy_is_saved_as_constraint(self):
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
        self.assertEqual(
            subscription["constraints"]["trip_natures"],
            ["business", "meeting", "team_building"],
        )
        self.assertEqual(subscription["constraints"]["trip_nature"], "meeting")
        self.assertEqual(subscription["constraints"]["meeting_start"], "10:00")
        self.assertEqual(subscription["constraints"]["meeting_end"], "16:00")
        self.assertEqual(subscription["constraints"]["cabin_arrangement"], "mixed")
        self.assertEqual(subscription["constraints"]["cabin_policy"], "level_based")
        self.assertEqual(subscription["constraints"]["user_level"], "director")
        self.assertEqual(subscription["constraints"]["business_seats"], 2)
        self.assertEqual(subscription["constraints"]["economy_seats"], 6)
        self.assertEqual(subscription["basic"]["passenger_count"], 8)
        self.assertEqual(subscription["constraints"]["reimburse_per_person"], 5000)

    def test_duplicate_travel_scene_fields_are_removed_from_ui(self):
        tags = re.findall(
            r'<input\b[^>]*\bname="travel_scenario"[^>]*>', self.full, re.I
        )
        self.assertEqual(len(tags), len(OPTIONS["travel_scenario"]))
        self.assertTrue(all(re.search(r'\btype="checkbox"', tag, re.I) for tag in tags))
        self.assertNotIn('name="travel_purpose"', self.full)
        self.assertNotIn('name="trip_type"', self.full)
        self.assertNotIn("precise-only", self.quick + self.full)

    def test_quick_page_uses_silent_presets_instead_of_rendered_chip_wall(self):
        self.assertIn("其他设置按场景预设", self.quick)
        self.assertIn("时间/航司/行李/提醒等已按场景预设", self.quick)
        self.assertNotIn("scenario-preset-chips", self.quick)
        self.assertNotIn("canonical-preference-chips", self.quick)

    def test_completion_summary_has_per_row_static_edit_links(self):
        self.assertIn('id="confirmation-map"', self.full)
        for section_id in (
            "section-where",
            "section-when",
            "section-who",
            "section-budget",
            "section-flight-preferences",
            "section-notifications",
        ):
            self.assertIn(f'data-confirm-edit="{section_id}"', self.full)
            self.assertIn(f'href="#{section_id}"', self.full)

    def test_visibility_contracts_are_only_the_four_approved_contracts(self):
        contracts = set(
            re.findall(r'data-visibility-contract="([^"]+)"', self.quick + self.full)
        )
        self.assertEqual(
            contracts,
            {
                "passenger-profile",
                "notification-email",
                "business-scenario",
                "transfer-details",
            },
        )
        self.assertNotIn("data-show-if", self.quick + self.full)
        self.assertNotIn("data-advanced-depth", self.quick + self.full)

    def test_route_type_and_domestic_invoice_are_saved(self):
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

    def test_route_type_is_derived_from_iata_when_form_value_conflicts(self):
        form = Form(
            {
                "monitor_mode": "precise",
                "route_type": "domestic",
                "round_trip": "true",
                "origin_select": "PVG",
                "destination": "KIX",
                "depart_date": "2026-10-01",
                "return_date": "2026-10-06",
                "price_strategy": "auto_judge",
                "travel_scenario": ["tourism"],
                "transfer_policy": "reasonable",
                "baggage": "required",
                "primary_goal": "buy_timing",
                "notification_method": "pushplus",
                "notification_frequency": "important_only",
            }
        )
        subscription = build_subscription(form)
        self.assertEqual(subscription["route_type"], "international")
        self.assertEqual(subscription["basic"]["route_type"], "international")
        self.assertEqual(subscription["constraints"]["route_type"], "international")
        self.assertEqual(subscription["hard_constraints"]["route_type"], "international")

    def test_domestic_invoice_trigger_maps_to_existing_invoice_field(self):
        form = Form(
            {
                "monitor_mode": "precise",
                "route_type": "domestic",
                "round_trip": "false",
                "origin_select": "PVG",
                "destination": "PEK",
                "depart_date": "2026-06-10",
                "price_strategy": "auto_judge",
                "travel_scenario": ["tourism"],
                "transfer_policy": "reasonable",
                "baggage": "required",
                "primary_goal": "buy_timing",
                "notification_method": "pushplus",
                "notification_frequency": "important_only",
                "invoice_context": "true",
            }
        )
        subscription = build_subscription(form)
        self.assertTrue(subscription["preferences"]["invoice_needed"])

    def test_location_price_hint_and_notification_channel_markers_exist(self):
        self.assertIn("const cityAliases =", self.quick)
        self.assertIn('id="origin-status"', self.quick)
        self.assertIn('id="destination-status"', self.quick)
        self.assertIn('id="price-hint"', self.quick)
        self.assertIn("resolveExact", self.quick)
        self.assertIn("renderCandidates", self.quick)
        self.assertIn("updatePriceHint", self.quick)
        self.assertIn('name="notification_method"', self.full)
        for value in ("email", "pushplus", "both"):
            self.assertIn(f'value="{value}"', self.full)
        self.assertIn('name="notification_email"', self.full)
        self.assertIn('data-visibility-contract="notification-email"', self.full)


if __name__ == "__main__":
    unittest.main()
