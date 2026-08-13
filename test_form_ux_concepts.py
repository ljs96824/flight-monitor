import re
import unittest
from copy import deepcopy

import form_structure
import web_form
from werkzeug.datastructures import MultiDict


RAW_TIME_INPUTS = (
    "departure_slots",
    "arrival_slots",
    "outbound_departure_slots",
    "outbound_arrival_slots",
    "return_departure_slots",
    "return_arrival_slots",
    "departure_time_policy",
    "no_late_arrival",
    "prefer_daytime_arrival",
)


class FormUxConceptRegistryTest(unittest.TestCase):
    def test_every_declared_form_field_has_exactly_one_concept(self):
        self.assertTrue(hasattr(form_structure, "CONCEPTS"))
        result = form_structure.validate_concepts()
        self.assertEqual(result["missing"], [])
        self.assertEqual(result["duplicates"], [])
        self.assertEqual(result["wrong_station"], [])

    def test_concept_guard_rejects_missing_and_duplicate_ownership(self):
        concepts = deepcopy(form_structure.CONCEPTS)
        first_name = next(iter(concepts))
        missing_field = concepts[first_name]["fields"][0]
        concepts[first_name]["fields"] = tuple(concepts[first_name]["fields"][1:])
        with self.assertRaisesRegex(ValueError, "未归属"):
            form_structure.validate_concepts(concepts)

        concepts = deepcopy(form_structure.CONCEPTS)
        concept_names = list(concepts)
        duplicate_field = concepts[concept_names[0]]["fields"][0]
        concepts[concept_names[1]]["fields"] = (
            *concepts[concept_names[1]]["fields"],
            duplicate_field,
        )
        with self.assertRaisesRegex(ValueError, "重复归属"):
            form_structure.validate_concepts(concepts)

    def test_time_concept_derives_legacy_aliases_for_shared_and_split_modes(self):
        shared = form_structure.derive_time_concept_fields(
            {
                "time_preference": "unlimited",
                "allow_redeye": "false",
                "arrival_preference": "no_late",
                "separate_direction_times": "false",
            },
            round_trip=True,
        )
        self.assertEqual(shared["time_preference"], "no_redeye")
        self.assertNotIn("redeye", shared["departure_slots"])
        self.assertEqual(shared["outbound_departure_slots"], shared["departure_slots"])
        self.assertEqual(shared["return_departure_slots"], shared["departure_slots"])
        self.assertEqual(shared["no_late_arrival"], "true")
        self.assertEqual(shared["departure_time_policy"], "no_redeye")
        self.assertEqual(shared["arrival_time_policy"], "no_midnight")

        daytime = form_structure.derive_time_concept_fields(
            {
                "time_preference": "daytime",
                "allow_redeye": "false",
                "arrival_preference": "daytime",
            },
            round_trip=False,
        )
        self.assertEqual(daytime["departure_time_policy"], "daytime")
        self.assertEqual(daytime["arrival_time_policy"], "daytime_only")

        unrestricted = form_structure.derive_time_concept_fields(
            {
                "time_preference": "unlimited",
                "allow_redeye": "true",
                "arrival_preference": "any",
            },
            round_trip=False,
        )
        self.assertEqual(unrestricted["departure_time_policy"], "any")
        self.assertEqual(unrestricted["arrival_time_policy"], "any")

        split = form_structure.derive_time_concept_fields(
            {
                "separate_direction_times": "true",
                "outbound_time_preference": "daytime",
                "outbound_allow_redeye": "false",
                "outbound_arrival_preference": "daytime",
                "return_time_preference": "unlimited",
                "return_allow_redeye": "true",
                "return_arrival_preference": "any",
            },
            round_trip=True,
        )
        self.assertEqual(split["time_preference"], "custom")
        self.assertNotIn("night", split["outbound_departure_slots"])
        self.assertIn("redeye", split["return_departure_slots"])
        self.assertNotEqual(
            split["outbound_departure_slots"],
            split["return_departure_slots"],
        )

        custom = form_structure.derive_time_concept_fields(
            {
                "time_preference": "custom",
                "allow_redeye": "false",
                "arrival_preference": "custom",
                "departure_time_start": "08:30",
                "departure_time_end": "11:15",
                "arrival_time_start": "10:30",
                "arrival_time_end": "14:00",
            },
            round_trip=False,
        )
        self.assertEqual(custom["departure_time_windows"], [["08:30", "11:15"]])
        self.assertEqual(custom["arrival_time_windows"], [["10:30", "14:00"]])

    def test_split_direction_visibility_and_edit_expansion_follow_canonical_controls(self):
        shared = form_structure.visible_field_names(
            {"round_trip": "true", "separate_direction_times": "false"}
        )
        self.assertIn("separate_direction_times", shared)
        self.assertNotIn("outbound_time_preference", shared)

        split = form_structure.visible_field_names(
            {"round_trip": "true", "separate_direction_times": "true"}
        )
        self.assertIn("outbound_time_preference", split)
        self.assertIn("return_arrival_time_end", split)

        expanded = form_structure.edit_expanded_sections(
            {
                "monitor_mode": "precise",
                "ux2_concept_form": "true",
                "outbound_departure_time_start": "08:30",
            },
            editing=True,
        )
        self.assertIn("flight_preferences", expanded)

    def test_ux2_submission_uses_the_same_derived_time_policy_family(self):
        base = {
            "origin_select": "PVG",
            "destination": "KIX",
            "depart_date": "2026-10-01",
            "route_type": "international",
            "round_trip": "false",
            "monitor_mode": "precise",
            "ux2_concept_form": "true",
            "time_preference": "daytime",
            "allow_redeye": "false",
            "arrival_preference": "daytime",
        }
        subscription = web_form.build_subscription(MultiDict(base))
        hard = subscription["hard_constraints"]
        self.assertEqual(hard["time_preference"], "daytime")
        self.assertEqual(hard["departure_time_policy"], "daytime")
        self.assertEqual(hard["arrival_time_policy"], "daytime_only")


class FormUxConceptRenderingTest(unittest.TestCase):
    def setUp(self):
        web_form.app.config.update(TESTING=True)
        self.html = web_form.app.test_client().get("/").get_data(as_text=True)

    def rendered_form_tags(self):
        return re.findall(r"<(?:input|select|textarea)\b[^>]*>", self.html, re.I)

    def rendered_names(self):
        names = []
        for tag in self.rendered_form_tags():
            match = re.search(r'\bname="([^"]+)"', tag, re.I)
            if match:
                names.append(match.group(1))
        return names

    def test_real_duplicate_surfaces_are_removed(self):
        names = self.rendered_names()
        self.assertEqual(names.count("business_start"), 1)
        self.assertEqual(names.count("business_end"), 1)
        for name in RAW_TIME_INPUTS:
            self.assertNotIn(name, names)

        for name in (
            "time_preference",
            "allow_redeye",
            "arrival_preference",
            "separate_direction_times",
        ):
            self.assertIn(name, names)

    def test_every_rendered_field_is_owned_by_the_concept_registry(self):
        self.assertEqual(
            sorted(set(self.rendered_names()) - set(form_structure.FIELD_OWNERS)),
            [],
        )

    def test_each_non_option_input_name_occurs_once_and_option_values_are_unique(self):
        tags = self.rendered_form_tags()
        seen = set()
        duplicates = []
        for tag in tags:
            name_match = re.search(r'\bname="([^"]+)"', tag, re.I)
            if not name_match:
                continue
            name = name_match.group(1)
            type_match = re.search(r'\btype="([^"]+)"', tag, re.I)
            input_type = (type_match.group(1).lower() if type_match else "")
            if input_type in {"radio", "checkbox"}:
                value_match = re.search(r'\bvalue="([^"]*)"', tag, re.I)
                key = (name, value_match.group(1) if value_match else "")
            else:
                key = (name, "__single_surface__")
            if key in seen:
                duplicates.append(key)
            seen.add(key)
        self.assertEqual(duplicates, [])

    def test_six_stations_are_a_single_open_breadcrumb_wizard(self):
        self.assertIn('id="station-breadcrumbs"', self.html)
        self.assertEqual(self.html.count('data-breadcrumb-station="'), 6)
        self.assertIn('data-wizard-state="current"', self.html)
        self.assertIn("panel.hidden = !isActive", self.html)
        self.assertIn("function openWizardStation", self.html)
        self.assertIn("完成✓", self.html)
        self.assertIn("当前▶", self.html)
        self.assertIn("未到○", self.html)

    def test_chip_wall_reuses_canonical_controls_instead_of_parallel_buttons(self):
        self.assertIn('id="canonical-preference-chips"', self.html)
        self.assertIn('id="preference-chip-home"', self.html)
        self.assertIn("mountCanonicalPreferenceChips", self.html)
        function_body = self.html.split(
            "function renderDefaultChips", 1
        )[1].split("function renderStationSummaries", 1)[0]
        self.assertNotIn("document.createElement('button')", function_body)
        self.assertIn("preset-active", function_body)

    def test_confirmation_edit_links_target_the_owning_station(self):
        self.assertIn("link.dataset.summaryStation", self.html)
        self.assertIn("link.dataset.summaryAnchor", self.html)
        self.assertIn("link.href = '#station-'", self.html)
        self.assertIn("openWizardStation(config.step)", self.html)


if __name__ == "__main__":
    unittest.main()
