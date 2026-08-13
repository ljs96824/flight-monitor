import json
import unittest
from collections import Counter
from copy import deepcopy
from html.parser import HTMLParser
from pathlib import Path

import form_concepts
import form_structure
import web_form
from scripts.capture_form_normalization_baseline import SCENARIOS
from werkzeug.datastructures import MultiDict


FIXTURE = Path(__file__).parent / "tests" / "fixtures" / "form_normalization_baseline_v1.json"

BUSINESS_CONCEPTS = frozenset(
    {
        "business_nature",
        "business_level",
        "team_arrangement",
        "reimbursement",
        "invoice",
        "same_day_round_trip",
        "meeting_window",
        "meeting_location",
        "meeting_importance",
        "same_day_execution",
    }
)

CANONICAL_WINDOW_INPUTS = (
    "shared_departure_window_start",
    "shared_departure_window_end",
    "shared_arrival_window_start",
    "shared_arrival_window_end",
    "outbound_departure_window_start",
    "outbound_departure_window_end",
    "outbound_arrival_window_start",
    "outbound_arrival_window_end",
    "return_departure_window_start",
    "return_departure_window_end",
    "return_arrival_window_start",
    "return_arrival_window_end",
)

LEGACY_WINDOW_INPUTS = (
    "departure_time_start",
    "departure_time_end",
    "arrival_time_start",
    "arrival_time_end",
    "outbound_departure_time_start",
    "outbound_departure_time_end",
    "outbound_arrival_time_start",
    "outbound_arrival_time_end",
    "return_departure_time_start",
    "return_departure_time_end",
    "return_arrival_time_start",
    "return_arrival_time_end",
)


class _FormDom(HTMLParser):
    def __init__(self):
        super().__init__()
        self.names = []
        self.inputs = []
        self.group_by_name = {}
        self._group_stack = []
        self.time_groups = []

    def handle_starttag(self, tag, attrs):
        values = dict(attrs)
        if tag == "details":
            group = values.get("data-secondary-group")
            self._group_stack.append(group)
            if values.get("data-time-window-group"):
                self.time_groups.append(values["data-time-window-group"])
        if tag in {"input", "select", "textarea"} and values.get("name"):
            name = values["name"]
            self.names.append(name)
            self.inputs.append((tag, values))
            if self._group_stack and self._group_stack[-1]:
                self.group_by_name[name] = self._group_stack[-1]

    def handle_endtag(self, tag):
        if tag == "details" and self._group_stack:
            self._group_stack.pop()


def _multidict(mapping):
    items = []
    for key, value in mapping.items():
        if isinstance(value, list):
            items.extend((key, str(item)) for item in value)
        else:
            items.append((key, str(value)))
    return MultiDict(items)


class FormUx33ScenarioScopeTest(unittest.TestCase):
    def setUp(self):
        web_form.app.config.update(TESTING=True)
        self.html = web_form.app.test_client().get("/settings").get_data(as_text=True)
        self.dom = _FormDom()
        self.dom.feed(self.html)

    def test_scenario_scope_registry_is_complete_and_frozen(self):
        self.assertEqual(form_concepts.VALID_SCENARIO_SCOPES, {"common", "business", "tourism"})
        self.assertEqual(
            {
                name
                for name, concept in form_concepts.CONCEPTS.items()
                if concept["scenario_scope"] == "business"
            },
            BUSINESS_CONCEPTS,
        )
        self.assertTrue(
            all(
                concept.get("scenario_scope") in form_concepts.VALID_SCENARIO_SCOPES
                for concept in form_concepts.CONCEPTS.values()
            )
        )
        self.assertFalse(
            any(
                concept["scenario_scope"] == "tourism"
                for concept in form_concepts.CONCEPTS.values()
            )
        )

        invalid = deepcopy(form_concepts.CONCEPTS)
        invalid["invoice"].pop("scenario_scope")
        with self.assertRaisesRegex(ValueError, "场景范围"):
            form_structure.validate_concepts(invalid)

    def test_business_branch_follows_parent_and_has_no_business_field_leaks(self):
        parent_at = self.html.index('data-form-concept="travel_context"')
        business_at = self.html.index('data-secondary-group="business-travel"')
        next_common_at = self.html.index('data-form-concept="companion_mode"')
        self.assertLess(parent_at, business_at)
        self.assertLess(business_at, next_common_at)

        self.assertNotEqual(self.dom.group_by_name.get("travel_scenario"), "business-travel")
        for concept_name in BUSINESS_CONCEPTS:
            for name in form_concepts.CONCEPTS[concept_name]["canonical_input_names"]:
                self.assertEqual(self.dom.group_by_name.get(name), "business-travel", name)

    def test_editing_business_scenario_opens_business_branch(self):
        context = web_form.build_form_page_context(
            "full",
            {"travel_scenario": ["business"]},
            edit_index=72,
        )
        groups = {item["id"]: item for item in context["secondary_groups"]}
        self.assertTrue(groups["business-travel"]["open"])


class FormUx33TimeDerivationTest(unittest.TestCase):
    def test_shared_custom_windows_override_top_level_preference(self):
        result = form_structure.derive_time_concept_fields(
            {
                "time_preference": "daytime",
                "allow_redeye": "false",
                "arrival_preference": "daytime",
                "shared_departure_window_start": "08:30",
                "shared_departure_window_end": "11:15",
                "shared_arrival_window_start": "10:30",
                "shared_arrival_window_end": "14:00",
            },
            round_trip=True,
        )
        self.assertEqual(result["departure_time_windows"], [["08:30", "11:15"]])
        self.assertEqual(result["arrival_time_windows"], [["10:30", "14:00"]])
        self.assertEqual(result["outbound_departure_time_windows"], [["08:30", "11:15"]])
        self.assertEqual(result["return_arrival_time_windows"], [["10:30", "14:00"]])

    def test_direction_windows_override_shared_windows_independently(self):
        result = form_structure.derive_time_concept_fields(
            {
                "time_preference": "unlimited",
                "allow_redeye": "false",
                "arrival_preference": "any",
                "shared_departure_window_start": "08:00",
                "shared_departure_window_end": "12:00",
                "shared_arrival_window_start": "10:00",
                "shared_arrival_window_end": "15:00",
                "outbound_departure_window_start": "06:30",
                "outbound_departure_window_end": "08:30",
                "outbound_arrival_window_start": "09:00",
                "outbound_arrival_window_end": "11:00",
                "return_departure_window_start": "18:00",
                "return_departure_window_end": "21:00",
                "return_arrival_window_start": "20:00",
                "return_arrival_window_end": "23:00",
            },
            round_trip=True,
        )
        self.assertEqual(result["departure_time_windows"], [["08:00", "12:00"]])
        self.assertEqual(result["outbound_departure_time_windows"], [["06:30", "08:30"]])
        self.assertEqual(result["outbound_arrival_time_windows"], [["09:00", "11:00"]])
        self.assertEqual(result["return_departure_time_windows"], [["18:00", "21:00"]])
        self.assertEqual(result["return_arrival_time_windows"], [["20:00", "23:00"]])

    def test_incomplete_direction_window_falls_back_to_shared_pair(self):
        result = form_structure.derive_time_concept_fields(
            {
                "time_preference": "unlimited",
                "shared_departure_window_start": "08:00",
                "shared_departure_window_end": "12:00",
                "outbound_departure_window_start": "06:30",
            },
            round_trip=True,
        )
        self.assertEqual(result["outbound_departure_time_windows"], [["08:00", "12:00"]])
        self.assertEqual(result["return_departure_time_windows"], [["08:00", "12:00"]])

    def test_new_controls_write_existing_normalized_schema(self):
        source = {
            "origin_select": "PVG",
            "destination": "KIX",
            "route_type": "international",
            "round_trip": "true",
            "depart_date": "2026-12-01",
            "return_date": "2026-12-06",
            "monitor_mode": "precise",
            "ux2_concept_form": "true",
            "ux2_time_touched": "true",
            "time_preference": "unlimited",
            "allow_redeye": "false",
            "arrival_preference": "any",
            "shared_departure_window_start": "08:00",
            "shared_departure_window_end": "12:00",
            "outbound_departure_window_start": "06:30",
            "outbound_departure_window_end": "08:30",
            "return_departure_window_start": "18:00",
            "return_departure_window_end": "21:00",
        }
        subscription = web_form.build_subscription(_multidict(source))
        hard = subscription["hard_constraints"]
        self.assertEqual(hard["departure_time_windows"], [["08:00", "12:00"]])
        self.assertEqual(hard["outbound_departure_time_windows"], [["06:30", "08:30"]])
        self.assertEqual(hard["return_departure_time_windows"], [["18:00", "21:00"]])


class FormUx33RenderingTest(unittest.TestCase):
    def setUp(self):
        web_form.app.config.update(TESTING=True)
        self.html = web_form.app.test_client().get("/settings").get_data(as_text=True)
        self.dom = _FormDom()
        self.dom.feed(self.html)
        self.counts = Counter(self.dom.names)

    def test_time_surface_uses_new_controls_and_never_renders_legacy_schema_inputs(self):
        for name in CANONICAL_WINDOW_INPUTS:
            self.assertEqual(self.counts[name], 1, name)
        for name in LEGACY_WINDOW_INPUTS:
            self.assertEqual(self.counts[name], 0, name)
        self.assertEqual(self.dom.time_groups, ["custom", "directional"])
        self.assertIn("分方向完整时间窗 > 通用完整时间窗 > 时段偏好", self.html)

    def test_time_preference_is_a_radio_group_with_unique_values(self):
        radios = [
            attrs
            for tag, attrs in self.dom.inputs
            if tag == "input"
            and attrs.get("name") == "time_preference"
            and attrs.get("type") == "radio"
        ]
        self.assertEqual({item.get("value") for item in radios}, {"daytime", "unlimited"})

    def test_cabin_stays_in_flight_preferences_with_same_cabin_note(self):
        self.assertIn("当前按全员同舱监控", self.html)
        self.assertIn("混舱（如成人商务+儿童经济）为规划中特性", self.html)
        self.assertNotEqual(self.dom.group_by_name.get("cabin_policy"), "business-travel")

    def test_no_new_custom_javascript_controls_native_details(self):
        for forbidden in (
            "details.open =",
            "setAttribute('open'",
            "classList.toggle('open'",
            "data-scenario-toggle",
            "data-time-window-toggle",
        ):
            self.assertNotIn(forbidden, web_form.FORM_PAGE_TEMPLATE)


class FormUx33FixtureAndSmokeTest(unittest.TestCase):
    def test_ninth_directional_time_fixture_is_frozen(self):
        fixture = json.loads(FIXTURE.read_text(encoding="utf-8"))["scenarios"]
        self.assertEqual(len(fixture), 9)
        self.assertIn("directional_time_windows", fixture)
        self.assertIn("directional_time_windows", SCENARIOS)
        normalized = fixture["directional_time_windows"]["normalized_subscription"]
        hard = normalized["hard_constraints"]
        self.assertEqual(hard["departure_time_windows"], [["08:00", "12:00"]])
        self.assertEqual(hard["outbound_departure_time_windows"], [["06:30", "08:30"]])
        self.assertEqual(hard["return_departure_time_windows"], [["18:00", "21:00"]])

    def test_ui_smoke_covers_business_and_nested_time_details(self):
        driver = (Path(__file__).parent / "scripts" / "ui_smoke_driver.mjs").read_text(
            encoding="utf-8"
        )
        self.assertIn("商务场景分支=PASS", driver)
        self.assertIn("分层时间窗=PASS", driver)
        self.assertIn("outbound_departure_window_start", driver)
        self.assertIn("return_departure_window_end", driver)


if __name__ == "__main__":
    unittest.main()
