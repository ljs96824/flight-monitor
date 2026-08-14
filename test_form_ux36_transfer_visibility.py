import unittest
from pathlib import Path

import web_form
from form_pages import VISIBILITY_CONTRACTS, build_form_page_context
from werkzeug.datastructures import MultiDict


def _transfer_concept(page):
    for section in page["sections"]:
        for concept in section["concepts"]:
            if concept["name"] == "transfer":
                return concept
    raise AssertionError("完整页缺少中转概念")


def _base_form(**overrides):
    values = {
        "form_page": "full",
        "monitor_mode": "precise",
        "origin_select": "上海",
        "origin_airports_active": "PVG,SHA",
        "destination": "北京",
        "destination_airports_active": "PEK,PKX",
        "depart_date": "2026-10-01",
        "round_trip": "true",
        "return_date": "2026-10-06",
        "adult_count": "1",
        "child_count": "0",
        "elderly_count": "0",
        "infant_count": "0",
        "max_budget": "8000",
        "max_budget_scope": "per_person",
        "target_price": "6000",
        "target_price_scope": "per_person",
        "travel_scenario": "tourism",
        "notification_method": "both",
    }
    values.update(overrides)
    return MultiDict(values)


class FormUx36TransferVisibilityTest(unittest.TestCase):
    def test_transfer_is_fourth_visibility_contract_and_children_are_grouped(self):
        self.assertEqual(
            VISIBILITY_CONTRACTS,
            frozenset(
                {
                    "passenger-profile",
                    "notification-email",
                    "business-scenario",
                    "transfer-details",
                }
            ),
        )
        concept = _transfer_concept(build_form_page_context("full"))
        self.assertEqual([field["name"] for field in concept["fields"]], ["transfer_policy"])
        self.assertEqual(
            [field["name"] for field in concept["transfer_detail_fields"]],
            [
                "short_transfer_limit",
                "accept_overnight_transfer",
                "accept_self_transfer",
            ],
        )
        self.assertTrue(concept["transfer_details_initial_visible"])

    def test_direct_only_hides_defaults_but_edit_preserves_nondefault_details(self):
        direct = _transfer_concept(
            build_form_page_context(
                "full",
                {
                    "transfer_policy": "direct_only",
                    "short_transfer_limit": "extra_6",
                    "accept_overnight_transfer": "false",
                    "accept_self_transfer": "false",
                },
            )
        )
        self.assertFalse(direct["transfer_details_initial_visible"])

        legacy_edit = _transfer_concept(
            build_form_page_context(
                "full",
                {
                    "transfer_policy": "direct_only",
                    "short_transfer_limit": "total_18",
                    "accept_overnight_transfer": "true",
                },
                edit_index=72,
            )
        )
        self.assertTrue(legacy_edit["transfer_details_initial_visible"])

    def test_hidden_direct_only_submission_uses_server_defaults(self):
        subscription = web_form.build_subscription(
            _base_form(transfer_policy="direct_only")
        )
        hard = subscription["hard_constraints"]
        transfer = subscription["advanced_rules"]["transfer"]
        self.assertIsNone(hard["max_extra_duration_hours"])
        self.assertIsNone(hard["max_total_duration_hours"])
        self.assertFalse(hard["accept_overnight_transfer"])
        self.assertFalse(hard["accept_self_transfer"])

        self.assertIsNone(transfer["max_extra_duration_hours"])
        self.assertIsNone(transfer["max_total_duration"])
        self.assertFalse(transfer["overnight_transfer"])
        self.assertFalse(transfer["self_transfer"])

    def test_visible_transfer_details_are_in_success_confirmation(self):
        subscription = web_form.build_subscription(
            _base_form(
                transfer_policy="price_first",
                short_transfer_limit="total_18",
                accept_overnight_transfer="true",
                accept_self_transfer="true",
            )
        )
        summary = web_form.build_success_summary(subscription)
        self.assertEqual(
            summary["transfer_text"],
            "中转设置：价格优先 · 总时长不超18小时 · 接受过夜中转 · 接受非联程自行中转",
        )

    def test_ui_smoke_covers_transfer_visibility_and_both_submission_paths(self):
        script = (
            Path(__file__).parent / "scripts" / "ui_smoke_driver.mjs"
        ).read_text(encoding="utf-8")
        self.assertIn("[UI smoke] 中转族显隐=PASS", script)
        self.assertIn("[UI smoke] 中转隐藏提交默认=PASS", script)
        self.assertIn("[UI smoke] 中转细节回读=PASS", script)


if __name__ == "__main__":
    unittest.main()
