from __future__ import annotations

import re
import unittest

from werkzeug.datastructures import MultiDict


MIXED_PASSENGERS = {"adult": 2, "child": 1, "elderly": 2, "infant": 0}
MIXED_ALLOCATION = {
    "business": {"adult": 2, "child": 0, "elderly": 0, "infant": 0},
    "economy": {"adult": 0, "child": 1, "elderly": 2, "infant": 0},
}


class QuotaOverviewContractTest(unittest.TestCase):
    def test_overview_uses_existing_usage_snapshot_and_budget_config(self):
        from api_usage import format_quota_overview

        payload = {
            "dates": {
                "2026-08-13": {"juhe": 40, "serpapi": 6},
                "2026-08-14": {"juhe": 10, "serpapi": 4},
            }
        }
        budgets = {
            "juhe": 550,
            "serpapi": {"monthly": 250, "reserve": 30},
        }

        self.assertEqual(
            format_quota_overview(payload, budgets, day="2026-08-14"),
            "[配额总览] juhe 本epoch已用=50/预算550 余量估算=500 储备=0 "
            "研究可用=500(以聚合数据控制台为准) · "
            "serpapi 本月已用=10/250 余量估算=240(reserve=30) · duffel=不限额",
        )

    def test_detail_source_section_appends_the_same_overview_line(self):
        import notifier
        from unittest.mock import patch

        marker = (
            "[配额总览] juhe 余量估算=500/550(买断) · "
            "serpapi 本月余量=240/250(reserve=30) · duffel=不限额"
        )
        with patch.object(notifier, "_quota_overview_text", return_value=marker):
            body = notifier._detail_technical_source_body(
                {"source_stats": {"juhe": {"count": 8, "status": "ok"}}}
            )

        self.assertIn(marker, body)

    def test_round_flush_logs_the_shared_overview_line(self):
        from pathlib import Path
        from tempfile import TemporaryDirectory
        from unittest.mock import patch

        import request_cache

        marker = (
            "[配额总览] juhe 本epoch已用=0/预算550 余量估算=550 储备=0 "
            "研究可用=550(以聚合数据控制台为准) · "
            "serpapi 本月已用=0/250 余量估算=250(reserve=30) · duffel=不限额"
        )
        with TemporaryDirectory(prefix="ux37-quota-") as tmpdir:
            root = Path(tmpdir)
            request_cache.reset_for_tests(root / "cache")
            request_cache.start_request_cache_round(
                "ux37-quota-test",
                track_usage=True,
                usage_path=root / "api_usage.json",
                quota_budgets={
                    "juhe": 550,
                    "serpapi": {"monthly": 250, "reserve": 30},
                },
            )
            logs = []
            try:
                with patch.object(request_cache, "safe_log", side_effect=logs.append):
                    request_cache._flush_api_usage_ledger()
            finally:
                request_cache.reset_for_tests(root / "cache")

        self.assertEqual([line for line in logs if line.startswith("[配额总览]")], [marker])


class CompanionConstraintDerivationContractTest(unittest.TestCase):
    def test_audit_covers_all_seven_legacy_values(self):
        from companion_constraints import COMPANION_CONSTRAINT_AUDIT

        self.assertEqual(
            set(COMPANION_CONSTRAINT_AUDIT),
            {
                "direct_preferred",
                "no_redeye",
                "avoid_long_layover",
                "need_baggage",
                "need_refund_change",
                "daytime_arrival",
                "limited_mobility",
            },
        )
        self.assertEqual(
            COMPANION_CONSTRAINT_AUDIT["limited_mobility"]["disposition"],
            "independent_control",
        )
        self.assertTrue(
            all(
                value["disposition"] == "derived"
                for key, value in COMPANION_CONSTRAINT_AUDIT.items()
                if key != "limited_mobility"
            )
        )

    def test_canonical_flight_preferences_derive_the_legacy_values_in_order(self):
        from companion_constraints import derive_companion_constraints

        self.assertEqual(
            derive_companion_constraints(
                {
                    "transfer_policy": "reasonable",
                    "short_transfer_limit": "extra_3",
                    "departure_time_policy": "daytime",
                    "arrival_time_policy": "daytime_only",
                    "baggage": "required",
                    "refund_flexibility": "required",
                    "mobility_limited": True,
                }
            ),
            [
                "direct_preferred",
                "no_redeye",
                "avoid_long_layover",
                "need_baggage",
                "need_refund_change",
                "daytime_arrival",
                "limited_mobility",
            ],
        )

    def test_unchecked_redeye_control_uses_the_page_default_when_the_browser_omits_it(self):
        import web_form
        from scripts.capture_form_normalization_baseline import _base_form

        form = _base_form(
            form_page="full",
            derive_companion_constraints="true",
            monitor_mode="precise",
            transfer_policy="reasonable",
            ux2_concept_form="true",
            ux2_time_touched="false",
            ux2_original_departure_time_policy="",
            ux2_original_arrival_time_policy="",
            time_preference="unlimited",
            outbound_time_preference="unlimited",
            return_time_preference="unlimited",
            outbound_allow_redeye="false",
            return_allow_redeye="false",
            shared_departure_window_start="08:00",
            shared_departure_window_end="12:00",
            outbound_departure_window_start="06:30",
            outbound_departure_window_end="08:30",
        )
        form.pop("allow_redeye", None)
        subscription = web_form.build_subscription(MultiDict(form))
        self.assertIn(
            "no_redeye",
            subscription["soft_preferences"]["companion_constraints"],
        )

    def test_derived_ui_post_matches_equivalent_legacy_post_field_for_field(self):
        import web_form
        from scripts.capture_form_normalization_baseline import _base_form

        common = _base_form(
            form_page="full",
            derive_companion_constraints="true",
            monitor_mode="precise",
            transfer_policy="reasonable",
            short_transfer_limit="extra_3",
            ux2_concept_form="true",
            ux2_time_touched="true",
            time_preference="daytime",
            allow_redeye="false",
            arrival_preference="daytime",
            baggage="required",
            refund_flexibility="required",
            mobility_limited="true",
        )
        legacy = dict(common)
        legacy["companion_constraints"] = [
            "direct_preferred",
            "no_redeye",
            "avoid_long_layover",
            "need_baggage",
            "need_refund_change",
            "daytime_arrival",
            "limited_mobility",
        ]

        derived_subscription = web_form.build_subscription(MultiDict(common))
        legacy_subscription = web_form.build_subscription(MultiDict(legacy))
        derived_subscription.pop("created_at", None)
        legacy_subscription.pop("created_at", None)
        self.assertEqual(derived_subscription, legacy_subscription)

    def test_ui_removes_legacy_group_but_keeps_independent_mobility_control(self):
        import web_form
        from form_concepts import CONCEPTS

        web_form.app.config.update(TESTING=True)
        client = web_form.app.test_client()
        self.assertNotIn('name="companion_constraints"', client.get("/").get_data(as_text=True))
        full = client.get("/settings").get_data(as_text=True)
        self.assertNotIn('name="companion_constraints"', full)
        self.assertEqual(full.count('name="mobility_limited"'), 1)
        self.assertNotIn(
            "companion_constraints",
            CONCEPTS["companion_constraints"]["canonical_input_names"],
        )

    def test_direct_legacy_post_is_still_accepted(self):
        import web_form
        from scripts.capture_form_normalization_baseline import _base_form

        form = _base_form(
            monitor_mode="precise",
            companion_constraints=["direct_preferred", "limited_mobility"],
        )
        subscription = web_form.build_subscription(MultiDict(form))
        self.assertEqual(
            subscription["soft_preferences"]["companion_constraints"],
            ["direct_preferred", "limited_mobility"],
        )


class MixedCabinTypeSelectionContractTest(unittest.TestCase):
    def test_type_checkboxes_derive_the_same_allocation_as_the_legacy_matrix(self):
        from cabin_allocation import cabin_allocation_from_form

        form = MultiDict(
            [
                ("cabin_allocation_ui", "types"),
                ("cabin_business_types", "adult"),
            ]
        )
        allocation, explicit = cabin_allocation_from_form(form, MIXED_PASSENGERS)
        self.assertTrue(explicit)
        self.assertEqual(allocation, MIXED_ALLOCATION)

    def test_confirmation_label_lists_each_cabin_by_passenger_type(self):
        from cabin_allocation import cabin_allocation_detail_label

        self.assertEqual(
            cabin_allocation_detail_label(MIXED_ALLOCATION),
            "商务:成人×2 / 经济:儿童×1+老人×2",
        )

    def test_full_page_uses_four_type_checkboxes_not_a_visible_number_matrix(self):
        import web_form

        web_form.app.config.update(TESTING=True)
        page = web_form.app.test_client().get("/settings").get_data(as_text=True)
        controls = re.findall(
            r'<input[^>]+name="cabin_business_types"[^>]+type="checkbox"[^>]*>',
            page,
        )
        self.assertEqual(len(controls), 4)
        for cabin in ("business", "economy"):
            for passenger_type in ("adult", "child", "elderly", "infant"):
                name = f"cabin_{cabin}_{passenger_type}"
                self.assertNotRegex(
                    page,
                    rf'<input[^>]+name="{name}"[^>]+type="number"',
                )
        self.assertIn("同类型拆分不同舱位暂不支持", page)

    def test_scenario_11_checkbox_path_matches_legacy_matrix_normalization(self):
        import main
        import web_form
        from scripts.capture_form_normalization_baseline import SCENARIOS

        legacy_form = dict(SCENARIOS["mixed_cabin_passenger_allocation"])
        checkbox_form = dict(legacy_form)
        for cabin in ("business", "economy"):
            for passenger_type in ("adult", "child", "elderly", "infant"):
                checkbox_form.pop(f"cabin_{cabin}_{passenger_type}", None)
        checkbox_form["cabin_allocation_ui"] = "types"
        checkbox_form["cabin_business_types"] = ["adult"]

        legacy = main.normalize_subscription(
            web_form.build_subscription(MultiDict(legacy_form))
        )
        checkbox = main.normalize_subscription(
            web_form.build_subscription(MultiDict(checkbox_form))
        )
        self.assertEqual(checkbox, legacy)


if __name__ == "__main__":
    unittest.main()
