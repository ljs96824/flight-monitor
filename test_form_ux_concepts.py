import re
import unittest
from copy import deepcopy

import form_structure
import web_form
from form_concepts import CANONICAL_TIME_WINDOW_FIELDS, LEGACY_RAW_TIME_WINDOW_FIELDS
from form_pages import FORM_PAGE_TEMPLATE
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
SECTION_IDS = (
    "section-where",
    "section-when",
    "section-who",
    "section-budget",
    "section-flight-preferences",
    "section-notifications",
)


class FormUxConceptRegistryTest(unittest.TestCase):
    def test_every_declared_form_field_has_exactly_one_concept(self):
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

    def test_split_direction_visibility_follows_canonical_controls(self):
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
        self.html = web_form.app.test_client().get("/settings").get_data(as_text=True)

    def rendered_form_tags(self):
        return re.findall(r"<(?:input|select|textarea)\b[^>]*>", self.html, re.I)

    def rendered_names(self):
        names = []
        for tag in self.rendered_form_tags():
            match = re.search(r'\bname="([^"]+)"', tag, re.I)
            if match:
                names.append(match.group(1))
        return names

    def tag_for_name(self, name):
        return next(
            tag for tag in self.rendered_form_tags()
            if re.search(fr'\bname="{re.escape(name)}"', tag, re.I)
        )

    def test_legacy_time_aliases_are_derived_and_not_rendered(self):
        names = self.rendered_names()
        self.assertEqual(names.count("business_start"), 1)
        self.assertEqual(names.count("business_end"), 1)
        for name in (*RAW_TIME_INPUTS, *LEGACY_RAW_TIME_WINDOW_FIELDS):
            self.assertEqual(names.count(name), 0, name)
        for name in CANONICAL_TIME_WINDOW_FIELDS:
            self.assertEqual(names.count(name), 1, name)
            self.assertNotIn('type="hidden"', self.tag_for_name(name))
        self.assertEqual(names.count("time_preference"), 2)
        for name in ("allow_redeye", "arrival_preference"):
            self.assertEqual(names.count(name), 1, name)
            self.assertNotIn('type="hidden"', self.tag_for_name(name))
        self.assertIn('type="hidden"', self.tag_for_name("separate_direction_times"))
    def test_every_rendered_business_field_is_owned_by_the_concept_registry(self):
        allowed_page_fields = {"form_page"}
        self.assertEqual(
            sorted(
                set(self.rendered_names())
                - set(form_structure.FIELD_OWNERS)
                - allowed_page_fields
            ),
            [],
        )

    def test_each_named_control_is_unique_or_a_valid_radio_group(self):
        for name in set(self.rendered_names()):
            tags = [
                tag
                for tag in self.rendered_form_tags()
                if re.search(fr'\bname="{re.escape(name)}"', tag, re.I)
            ]
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
    def test_six_primary_sections_and_native_groups_use_plain_anchor_navigation(self):
        for section_id in SECTION_IDS:
            self.assertIn(f'id="{section_id}"', self.html)
            self.assertIn(f'href="#{section_id}"', self.html)
        self.assertEqual(self.html.count("<details"), 4)
        self.assertEqual(self.html.count('data-secondary-group="'), 2)
        self.assertEqual(self.html.count('data-time-window-group="'), 2)
        for group_id in ("business-travel", "feasibility"):
            self.assertIn(f'id="group-{group_id}"', self.html)
            self.assertIn(f'href="#group-{group_id}"', self.html)
        self.assertNotIn("data-wizard-state", self.html)
        self.assertNotIn("openWizardStation", self.html)

    def test_chip_and_breadcrumb_surfaces_are_removed(self):
        for marker in (
            "station-breadcrumbs",
            "scenario-preset-chips",
            "canonical-preference-chips",
            "mountCanonicalPreferenceChips",
            "renderDefaultChips",
        ):
            self.assertNotIn(marker, self.html)
            self.assertNotIn(marker, FORM_PAGE_TEMPLATE)

    def test_confirmation_edit_links_target_static_section_anchors(self):
        self.assertIn('id="confirmation-map"', self.html)
        for section_id in SECTION_IDS:
            self.assertIn(f'data-confirm-edit="{section_id}"', self.html)
            self.assertIn(f'href="#{section_id}"', self.html)


if __name__ == "__main__":
    unittest.main()
