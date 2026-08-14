import json
import tempfile
import unittest
from collections import Counter
from html.parser import HTMLParser
from pathlib import Path

import main
import web_form
from form_pages import FORM_PAGE_TEMPLATE
from werkzeug.datastructures import MultiDict
from unittest.mock import patch


FIXTURE = Path(__file__).parent / "tests" / "fixtures" / "form_normalization_baseline_v1.json"
SECTION_IDS = (
    "section-where",
    "section-when",
    "section-who",
    "section-budget",
    "section-flight-preferences",
    "section-notifications",
)
REMOVED_MECHANICS = (
    "station-breadcrumbs",
    "mobile-stepper",
    "scenario-preset-chips",
    "canonical-preference-chips",
    "optional-settings-toggle",
    "openWizardStation",
    "goToStep",
    "mountCanonicalPreferenceChips",
    "renderDefaultChips",
    "data-wizard-state",
    "data-default-collapsed",
)


class _Dom(HTMLParser):
    def __init__(self):
        super().__init__()
        self.tags = []

    def handle_starttag(self, tag, attrs):
        self.tags.append((tag, dict(attrs)))

    def attributes(self, *, tag=None, name=None):
        result = []
        for current_tag, attrs in self.tags:
            if tag is not None and current_tag != tag:
                continue
            if name is not None and name not in attrs:
                continue
            result.append(attrs)
        return result


class _FormSubmissionParser(HTMLParser):
    def __init__(self):
        super().__init__()
        self.items = []
        self._select = None

    def handle_starttag(self, tag, attrs):
        values = dict(attrs)
        if tag == "input" and values.get("name"):
            if values.get("type") in {"checkbox", "radio"} and "checked" not in values:
                return
            self.items.append((values["name"], values.get("value", "")))
        elif tag == "select" and values.get("name"):
            self._select = {
                "name": values["name"],
                "multiple": "multiple" in values,
                "options": [],
            }
        elif tag == "option" and self._select is not None:
            self._select["options"].append(
                (values.get("value", ""), "selected" in values)
            )

    def handle_endtag(self, tag):
        if tag != "select" or self._select is None:
            return
        selected = [value for value, active in self._select["options"] if active]
        if not selected and not self._select["multiple"] and self._select["options"]:
            selected = [self._select["options"][0][0]]
        if not self._select["multiple"]:
            selected = selected[:1]
        self.items.extend((self._select["name"], value) for value in selected)
        self._select = None


class FormUx3TwoPagesTest(unittest.TestCase):
    def setUp(self):
        web_form.app.config.update(TESTING=True)
        self.client = web_form.app.test_client()

    def _page(self, path):
        response = self.client.get(path)
        self.assertEqual(response.status_code, 200, path)
        return response.get_data(as_text=True)

    def test_quick_page_is_static_bounded_and_links_to_full_settings(self):
        html = self._page("/")
        dom = _Dom()
        dom.feed(html)

        self.assertIn('data-page-mode="quick"', html)
        controls = dom.attributes(name="data-ux-control")
        self.assertLessEqual(len({item["data-ux-control"] for item in controls}), 12)
        self.assertIn('href="/settings"', html)
        self.assertIn("创建监控", html)
        self.assertIn("时间/航司/行李/提醒等已按场景预设", html)
        self.assertIn(".quick-shell .field-grid { grid-template-columns:1fr; }", html)
        self.assertIn('name="origin_select" type="text"', html)
        self.assertNotIn('name="origin_manual"', html)
        self.assertNotIn('name="date_flexibility"', html)
        self.assertNotIn('name="return_date_flexibility"', html)
        for removed in REMOVED_MECHANICS:
            self.assertNotIn(removed, html)

    def test_full_page_has_six_visible_sections_and_native_secondary_groups(self):
        html = self._page("/settings")
        dom = _Dom()
        dom.feed(html)

        self.assertIn('data-page-mode="full"', html)
        section_attrs = {
            attrs.get("id"): attrs
            for attrs in dom.attributes(tag="section")
            if attrs.get("id") in SECTION_IDS
        }
        self.assertEqual(set(section_attrs), set(SECTION_IDS))
        for section_id, attrs in section_attrs.items():
            self.assertNotIn("hidden", attrs, section_id)
            self.assertNotEqual(attrs.get("style"), "display:none", section_id)
            self.assertIn(f'href="#{section_id}"', html)
        self.assertEqual(html.count("<details"), 4)
        self.assertEqual(html.count('data-secondary-group="'), 2)
        self.assertEqual(html.count('data-time-window-group="'), 2)
        self.assertIn('data-secondary-group="business-travel"', html)
        self.assertIn('data-secondary-group="feasibility"', html)
        self.assertIn('href="#group-business-travel"', html)
        self.assertIn('href="#group-feasibility"', html)
        for removed in REMOVED_MECHANICS:
            self.assertNotIn(removed, html)

    def test_full_page_named_fields_have_one_dom_element_each(self):
        html = self._page("/settings")
        dom = _Dom()
        dom.feed(html)
        controls_by_name = {}
        for tag in ("input", "select", "textarea"):
            for attrs in dom.attributes(tag=tag, name="name"):
                controls_by_name.setdefault(attrs["name"], []).append((tag, attrs))
        invalid = {}
        for name, controls in controls_by_name.items():
            if len(controls) == 1:
                continue
            choice_group = all(
                tag == "input" and attrs.get("type") in {"radio", "checkbox"}
                for tag, attrs in controls
            )
            values = [attrs.get("value") for _, attrs in controls]
            if not choice_group or len(values) != len(set(values)):
                invalid[name] = len(controls)
        self.assertEqual(invalid, {})

    def test_conditional_visibility_is_limited_to_three_whitelisted_contracts(self):
        quick = self._page("/")
        full = self._page("/settings")
        dom = _Dom()
        dom.feed(quick + full)
        contracts = {
            attrs["data-visibility-contract"]
            for attrs in dom.attributes(name="data-visibility-contract")
        }
        self.assertEqual(contracts, {"passenger-profile", "notification-email", "business-scenario"})
        for html in (quick, full):
            self.assertNotIn("data-show-if", html)
            self.assertNotIn("data-advanced-depth", html)

    def test_full_confirmation_rows_link_back_to_section_anchors(self):
        html = self._page("/settings")
        self.assertIn('id="confirmation-map"', html)
        for section_id in SECTION_IDS:
            self.assertIn(f'data-confirm-edit="{section_id}"', html)
            self.assertIn(f'href="#{section_id}"', html)

    def test_edit_compatibility_entry_redirects_to_full_page_and_keeps_saved_mode(self):
        fixture = json.loads(FIXTURE.read_text(encoding="utf-8"))
        saved = fixture["scenarios"]["solo_minimal"]["normalized_subscription"]
        original = web_form.SUBSCRIPTIONS_PATH
        with tempfile.TemporaryDirectory() as tmpdir:
            path = Path(tmpdir) / "subscriptions.json"
            path.write_text(json.dumps([saved], ensure_ascii=False), encoding="utf-8")
            web_form.SUBSCRIPTIONS_PATH = path
            try:
                redirect_response = self.client.get("/?edit=0")
                self.assertEqual(redirect_response.status_code, 302)
                self.assertTrue(redirect_response.headers["Location"].endswith("/settings?edit=0"))
                html = self._page("/settings?edit=0")
            finally:
                web_form.SUBSCRIPTIONS_PATH = original

        self.assertIn('name="monitor_mode" value="quick"', html)
        self.assertIn('name="subscription_index" value="0"', html)

    def test_new_page_modes_are_server_owned_hidden_values(self):
        quick = self._page("/")
        full = self._page("/settings")
        self.assertIn('name="monitor_mode" value="quick"', quick)
        self.assertIn('name="monitor_mode" value="precise"', full)
        self.assertNotIn('name="monitor_mode" type="radio"', quick + full)


    def test_quick_page_passenger_count_is_a_visible_control(self):
        html = self._page("/")
        self.assertIn('name="passenger_count" type="number"', html)
        self.assertNotIn('id="field-passenger-count" name="passenger_count" type="hidden"', html)

    def test_interaction_script_only_toggles_the_three_whitelisted_contracts(self):
        self.assertEqual(FORM_PAGE_TEMPLATE.count("element.hidden ="), 3)
        self.assertIn('data-visibility-contract="passenger-profile"', FORM_PAGE_TEMPLATE)
        self.assertIn('data-visibility-contract="notification-email"', FORM_PAGE_TEMPLATE)
        self.assertIn('data-visibility-contract="business-scenario"', FORM_PAGE_TEMPLATE)
        self.assertNotIn("classList.toggle('open'", FORM_PAGE_TEMPLATE)

    def test_page_marker_does_not_change_eight_fixture_normalization(self):
        fixture = json.loads(FIXTURE.read_text(encoding="utf-8"))["scenarios"]
        for name, case in fixture.items():
            source_items = []
            for key, value in case["form_input"].items():
                if isinstance(value, list):
                    source_items.extend((key, str(item)) for item in value)
                else:
                    source_items.append((key, str(value)))
            for page_mode in ("quick", "full"):
                items = [*source_items, ("form_page", page_mode)]
                rebuilt = web_form.build_subscription(MultiDict(items))
                self.assertEqual(
                    main.normalize_subscription(rebuilt),
                    case["normalized_subscription"],
                    f"{name}:{page_mode}",
                )

    def test_full_page_html_edit_roundtrip_is_idempotent_for_eight_scenarios(self):
        fixture = json.loads(FIXTURE.read_text(encoding="utf-8"))["scenarios"]
        original = web_form.SUBSCRIPTIONS_PATH
        with tempfile.TemporaryDirectory() as tmpdir:
            path = Path(tmpdir) / "subscriptions.json"
            web_form.SUBSCRIPTIONS_PATH = path
            try:
                with patch.object(web_form, "start_background_collection"):
                    for name, case in fixture.items():
                        expected = case["normalized_subscription"]
                        path.write_text(
                            json.dumps([expected], ensure_ascii=False),
                            encoding="utf-8",
                        )
                        html = self._page("/settings?edit=0")
                        parser = _FormSubmissionParser()
                        parser.feed(html)
                        response = self.client.post(
                            "/subscribe",
                            data=MultiDict(parser.items),
                            follow_redirects=False,
                        )
                        self.assertEqual(response.status_code, 302, name)
                        saved = json.loads(path.read_text(encoding="utf-8"))[0]
                        self.assertEqual(
                            main.normalize_subscription(saved),
                            expected,
                            name,
                        )
            finally:
                web_form.SUBSCRIPTIONS_PATH = original

    def test_ui_smoke_is_local_edge_contract_and_explicitly_skipped_in_ci(self):
        root = Path(__file__).parent
        smoke = (root / "scripts" / "ui_smoke.py").read_text(encoding="utf-8")
        driver = (root / "scripts" / "ui_smoke_driver.mjs").read_text(encoding="utf-8")
        workflow = (root / ".github" / "workflows" / "tests.yml").read_text(encoding="utf-8")
        self.assertIn("--remote-debugging-port", smoke)
        self.assertIn("Runtime.exceptionThrown", driver)
        self.assertIn("visibleControlCount", driver)
        self.assertIn("UI smoke (local only, requires Microsoft Edge)", workflow)
        self.assertIn("if: ${{ false }}", workflow)


if __name__ == "__main__":
    unittest.main()
