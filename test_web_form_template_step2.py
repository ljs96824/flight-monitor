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

    def test_same_day_round_trip_field_exists(self):
        self.assertIn('name="same_day_round_trip"', FORM_TEMPLATE)
        self.assertIn('name="business_start"', FORM_TEMPLATE)
        self.assertIn('name="business_end"', FORM_TEMPLATE)
        self.assertIn('name="buffer_hours"', FORM_TEMPLATE)
        self.assertIn('name="transport_mode"', FORM_TEMPLATE)
        self.assertIn('data-show-if="same_day_round_trip=true"', FORM_TEMPLATE)
        self.assertIn("syncSameDayRoundTrip", FORM_TEMPLATE)

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
                "same_day_round_trip": "true",
                "business_start": "10:00",
                "business_end": "16:00",
                "buffer_hours": "2.5",
                "transport_mode": "taxi",
                "origin_select": "PVG",
                "destination": "PEK",
                "depart_date": "2026-06-10",
                "price_strategy": "auto_judge",
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
        self.assertEqual(subscription["constraints"]["buffer_hours"], 2.5)
        self.assertEqual(subscription["constraints"]["transport_mode"], "taxi")
        self.assertEqual(subscription["basic"]["return_date"], "2026-06-10")


if __name__ == "__main__":
    unittest.main()
