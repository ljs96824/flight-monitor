import json
import tempfile
import unittest
from collections import Counter
from html.parser import HTMLParser
from pathlib import Path
from unittest.mock import patch

import main
import web_form
import form_pages
from form_concepts import CONCEPTS
from scripts.capture_form_normalization_baseline import SCENARIOS
from werkzeug.datastructures import MultiDict


FIXTURE = Path(__file__).parent / "tests" / "fixtures" / "form_normalization_baseline_v1.json"
REPEATABLE_CHECKBOX_FIELDS = frozenset({"travel_scenario", "companion_constraints"})


class _FormDom(HTMLParser):
    def __init__(self):
        super().__init__()
        self.names = []
        self.controls_by_name = {}
        self.sections_by_name = {}
        self._section_stack = []
        self.groups_by_name = {}
        self._group_stack = []

    def handle_starttag(self, tag, attrs):
        values = dict(attrs)
        if tag == "section":
            self._section_stack.append(values.get("id"))
        if tag == "details":
            self._group_stack.append(values.get("data-secondary-group"))
        if tag in {"input", "select", "textarea"} and values.get("name"):
            name = values["name"]
            self.names.append(name)
            self.controls_by_name.setdefault(name, []).append((tag, values))
            self.sections_by_name[name] = next(
                (item for item in reversed(self._section_stack) if item),
                None,
            )
            self.groups_by_name[name] = next(
                (item for item in reversed(self._group_stack) if item),
                None,
            )

    def handle_endtag(self, tag):
        if tag == "section" and self._section_stack:
            self._section_stack.pop()
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


class FormUx31RenderCompletenessTest(unittest.TestCase):
    def setUp(self):
        web_form.app.config.update(TESTING=True)
        self.client = web_form.app.test_client()

    def _page(self, path):
        response = self.client.get(path)
        self.assertEqual(response.status_code, 200, path)
        return response.get_data(as_text=True)

    def test_every_concept_declares_canonical_input_names(self):
        for concept_name, concept in CONCEPTS.items():
            self.assertIn("canonical_input_names", concept, concept_name)
            self.assertTrue(concept["canonical_input_names"], concept_name)
            self.assertTrue(
                set(concept["canonical_input_names"]).issubset(concept["fields"]),
                concept_name,
            )

    def test_full_page_renders_every_canonical_input_exactly_once(self):
        html = self._page("/settings")
        dom = _FormDom()
        dom.feed(html)
        counts = Counter(dom.names)
        for concept_name, concept in CONCEPTS.items():
            for name in concept["canonical_input_names"]:
                controls = dom.controls_by_name.get(name, [])
                self.assertTrue(controls, f"{concept_name}:{name}")
                if name in REPEATABLE_CHECKBOX_FIELDS:
                    self.assertEqual(len(controls), len(form_pages.OPTIONS[name]), name)
                    self.assertTrue(
                        all(
                            tag == "input" and attrs.get("type") == "checkbox"
                            for tag, attrs in controls
                        ),
                        f"{concept_name}:{name}",
                    )
                    values = [attrs.get("value") for _, attrs in controls]
                    self.assertEqual(len(values), len(set(values)), f"{concept_name}:{name}")
                    continue
                if counts[name] == 1:
                    continue
                self.assertTrue(
                    all(tag == "input" and attrs.get("type") == "radio" for tag, attrs in controls),
                    f"{concept_name}:{name}",
                )
                values = [attrs.get("value") for _, attrs in controls]
                self.assertEqual(len(values), len(set(values)), f"{concept_name}:{name}")

        business_fields = {
            "same_day_round_trip",
            "business_start",
            "business_end",
            "buffer_hours",
            "transport_mode",
            "user_transport_min",
            "redundancy_min",
            "invoice_needed",
            "invoice_context",
            "invoice_special_vat",
            "invoice_cabin_limit",
        }
        for name in business_fields:
            self.assertEqual(dom.groups_by_name.get(name), "business-travel", name)
        for name in ("outbound_set_off", "origin_transport_min", "transport_margin_mode"):
            self.assertEqual(dom.groups_by_name.get(name), "feasibility", name)
        for name in ("notification_method", "notification_email"):
            self.assertEqual(dom.sections_by_name.get(name), "section-notifications", name)

    def test_quick_page_renders_its_declared_inputs_exactly_once(self):
        html = self._page("/")
        dom = _FormDom()
        dom.feed(html)
        counts = Counter(dom.names)
        self.assertTrue(hasattr(form_pages, "QUICK_CANONICAL_INPUT_NAMES"))
        for name in form_pages.QUICK_CANONICAL_INPUT_NAMES:
            if name in REPEATABLE_CHECKBOX_FIELDS:
                self.assertEqual(counts[name], len(form_pages.OPTIONS[name]), name)
                self.assertTrue(
                    all(
                        tag == "input" and attrs.get("type") == "checkbox"
                        for tag, attrs in dom.controls_by_name[name]
                    ),
                    name,
                )
            else:
                self.assertEqual(counts[name], 1, name)

    def test_mode_names_and_prominent_bidirectional_links_are_visible(self):
        quick = self._page("/")
        full = self._page("/settings")
        self.assertIn("<h1>快速创建监控</h1>", quick)
        self.assertIn("需要完整控制？", quick)
        self.assertIn('data-mode-link="full"', quick)
        self.assertIn("<h1>完整设置</h1>", full)
        self.assertIn('data-mode-link="quick"', full)
        self.assertIn("返回快速创建", full)

    def test_same_day_meeting_fields_use_native_business_group_and_whitelisted_visibility(self):
        html = self._page("/settings")
        self.assertIn('data-secondary-group="business-travel"', html)
        self.assertIn("商务类型、会议、团队、报销与发票设置；非商务行程可保持关闭。", html)
        self.assertNotIn('data-static-subsection="same-day-meeting"', html)
        self.assertEqual(
            html.count('data-visibility-contract="'),
            html.count('data-visibility-contract="passenger-profile"')
            + html.count('data-visibility-contract="notification-email"')
            + html.count('data-visibility-contract="business-scenario"')
            + html.count('data-visibility-contract="transfer-details"'),
        )

    def test_same_day_execution_fields_roundtrip_without_guessing(self):
        source = dict(SCENARIOS["same_day_round_trip"])
        source.update(
            {
                "monitor_mode": "precise",
                "buffer_hours": "1.5",
                "transport_mode": "taxi",
                "user_transport_min": "25",
                "redundancy_min": "15",
            }
        )
        normalized = main.normalize_subscription(
            web_form.build_subscription(_multidict(source))
        )
        constraints = normalized["constraints"]
        self.assertEqual(constraints["buffer_hours"], 1.5)
        self.assertEqual(constraints["transport_mode"], "taxi")
        self.assertEqual(constraints["user_transport_min"], 25)
        self.assertEqual(constraints["redundancy_min"], 15)

    def test_full_submit_confirmation_echoes_email_and_meeting(self):
        source = dict(SCENARIOS["same_day_round_trip"])
        source.update(
            {
                "monitor_mode": "precise",
                "notification_method": "email",
                "notification_email": "ux31@example.com",
            }
        )
        original = web_form.SUBSCRIPTIONS_PATH
        with tempfile.TemporaryDirectory() as tmpdir:
            web_form.SUBSCRIPTIONS_PATH = Path(tmpdir) / "subscriptions.json"
            try:
                with patch.object(web_form, "start_background_collection"):
                    response = self.client.post(
                        "/subscribe",
                        data=_multidict(source),
                        follow_redirects=True,
                    )
            finally:
                web_form.SUBSCRIPTIONS_PATH = original
        html = response.get_data(as_text=True)
        self.assertIn("ux31@example.com", html)
        self.assertIn("10:30", html)
        self.assertIn("17:00", html)

    def test_normalization_baseline_contains_directional_and_parallel_scenes(self):
        fixture = json.loads(FIXTURE.read_text(encoding="utf-8"))["scenarios"]
        self.assertEqual(len(fixture), 10)
        self.assertIn("same_day_meeting_complete", fixture)
        self.assertIn("directional_time_windows", fixture)
        self.assertIn("parallel_scenarios_elderly_child", fixture)

    def test_ui_smoke_drives_bidirectional_links_email_and_meeting(self):
        driver = (
            Path(__file__).parent / "scripts" / "ui_smoke_driver.mjs"
        ).read_text(encoding="utf-8")
        self.assertIn("双页互链=PASS", driver)
        self.assertIn("渠道三态转换=PASS", driver)
        self.assertIn('chooseNotificationMethod("pushplus")', driver)
        self.assertIn('chooseNotificationMethod("email")', driver)
        self.assertIn('chooseNotificationMethod("both")', driver)
        self.assertNotIn("set('notification_method'", driver)
        self.assertIn("ux31@example.com", driver)
        self.assertIn("页2当天往返会议=PASS", driver)


if __name__ == "__main__":
    unittest.main()
