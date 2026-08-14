import unittest
from collections import Counter
from html.parser import HTMLParser

import web_form
from form_concepts import BUSINESS_SCENARIO_CONCEPTS, CONCEPTS
from form_pages import FORM_PAGE_TEMPLATE, OPTIONS, build_form_page_context


BUSINESS_CONCEPTS = set(BUSINESS_SCENARIO_CONCEPTS)
FEASIBILITY_CONCEPTS = {
    "set_off_times",
    "transport_estimates",
    "transport_margin",
    "reserve_overrides",
}


class _DetailsDom(HTMLParser):
    def __init__(self):
        super().__init__()
        self.details = {}
        self.names = []
        self.group_by_name = {}
        self._details_stack = []

    def handle_starttag(self, tag, attrs):
        values = dict(attrs)
        if tag == "details":
            group_id = values.get("data-secondary-group")
            self._details_stack.append(group_id)
            if group_id:
                self.details[group_id] = values
        if tag in {"input", "select", "textarea"} and values.get("name"):
            name = values["name"]
            self.names.append(name)
            if self._details_stack and self._details_stack[-1]:
                self.group_by_name[name] = self._details_stack[-1]

    def handle_endtag(self, tag):
        if tag == "details" and self._details_stack:
            self._details_stack.pop()


class FormUx32NativeDetailsTest(unittest.TestCase):
    def setUp(self):
        web_form.app.config.update(TESTING=True)
        self.client = web_form.app.test_client()

    def _full_page(self):
        response = self.client.get("/settings")
        self.assertEqual(response.status_code, 200)
        return response.get_data(as_text=True)

    def test_full_page_uses_exactly_two_closed_native_details_groups(self):
        html = self._full_page()
        dom = _DetailsDom()
        dom.feed(html)

        self.assertEqual(set(dom.details), {"business-travel", "feasibility"})
        self.assertNotIn("open", dom.details["business-travel"])
        self.assertNotIn("open", dom.details["feasibility"])
        self.assertIn("商务出行", html)
        self.assertIn("可行性参数", html)
        self.assertIn('href="#group-business-travel"', html)
        self.assertIn('href="#group-feasibility"', html)

    def test_grouped_concepts_render_once_under_their_native_details(self):
        html = self._full_page()
        dom = _DetailsDom()
        dom.feed(html)
        counts = Counter(dom.names)

        for concept_name in BUSINESS_CONCEPTS:
            for name in CONCEPTS[concept_name]["canonical_input_names"]:
                self.assertEqual(counts[name], 1, name)
                self.assertEqual(dom.group_by_name.get(name), "business-travel", name)
        for concept_name in FEASIBILITY_CONCEPTS:
            for name in CONCEPTS[concept_name]["canonical_input_names"]:
                self.assertEqual(counts[name], 1, name)
                self.assertEqual(dom.group_by_name.get(name), "feasibility", name)

        for name in CONCEPTS["cabin"]["canonical_input_names"]:
            expected_count = (
                len(OPTIONS[name]) if name == "cabin_business_types" else 1
            )
            self.assertEqual(counts[name], expected_count, name)
            self.assertNotIn(name, dom.group_by_name, name)

    def test_edit_context_opens_only_groups_with_nondefault_values(self):
        business = build_form_page_context(
            "full",
            {"same_day_round_trip": "true"},
            edit_index=72,
        )
        self.assertIn("secondary_groups", business)
        business_groups = {item["id"]: item for item in business["secondary_groups"]}
        self.assertTrue(business_groups["business-travel"]["open"])
        self.assertFalse(business_groups["feasibility"]["open"])

        feasibility = build_form_page_context(
            "full",
            {"outbound_set_off": "06:30"},
            edit_index=72,
        )
        self.assertIn("secondary_groups", feasibility)
        feasibility_groups = {item["id"]: item for item in feasibility["secondary_groups"]}
        self.assertFalse(feasibility_groups["business-travel"]["open"])
        self.assertTrue(feasibility_groups["feasibility"]["open"])

    def test_server_render_adds_open_only_for_nondefault_edit_values(self):
        with web_form.app.test_request_context("/settings"):
            html = web_form._render_form_page(
                "full",
                {"invoice_needed": "true", "origin_transport_min": "45"},
                edit_index=72,
            )
        dom = _DetailsDom()
        dom.feed(html)
        self.assertIn("business-travel", dom.details)
        self.assertIn("feasibility", dom.details)
        self.assertIn("open", dom.details["business-travel"])
        self.assertIn("open", dom.details["feasibility"])

    def test_smoke_contract_covers_native_toggle_edit_open_and_find(self):
        with open("scripts/ui_smoke_driver.mjs", encoding="utf-8") as stream:
            driver = stream.read()
        self.assertIn("原生details开合=PASS", driver)
        self.assertIn("编辑态details自动展开=PASS", driver)
        self.assertIn("details内文查找可达=PASS", driver)
        self.assertNotIn("details.open =", driver)
        self.assertNotIn("setAttribute('open'", driver)
        self.assertNotIn("details.open =", FORM_PAGE_TEMPLATE)
        self.assertNotIn("setAttribute('open'", FORM_PAGE_TEMPLATE)
        self.assertNotIn("classList.toggle('open'", FORM_PAGE_TEMPLATE)


if __name__ == "__main__":
    unittest.main()
