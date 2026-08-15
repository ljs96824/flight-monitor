import json
import unittest
from pathlib import Path
from unittest.mock import patch


BASE_DIR = Path(__file__).resolve().parent
FIXTURE_PATH = BASE_DIR / "tests" / "fixtures" / "notification_no_plan_mixed_v1.json"


def _fixture_payload():
    return json.loads(FIXTURE_PATH.read_text(encoding="utf-8"))["payload"]


def _segment(flight_no, origin, destination, departure, arrival, price):
    return {
        "flight_combo": flight_no,
        "price": price,
        "segments": [
            {
                "flight_no": flight_no,
                "dep_airport": origin,
                "arr_airport": destination,
                "dep_time": departure,
                "arr_time": arrival,
                "aircraft": "32N",
            }
        ],
    }


class NotificationSectionContractTest(unittest.TestCase):
    def test_canonical_no_match_replaces_primary_with_alternatives(self):
        from notification_sections import canonical_sections

        standard = canonical_sections("standard", mixed_cabin=False)
        no_match = canonical_sections("no_match", mixed_cabin=True)

        self.assertIn("primary_plan", standard)
        self.assertNotIn("alternative_plans", standard)
        self.assertNotIn("primary_plan", no_match)
        self.assertIn("alternative_plans", no_match)
        for section in (
            "excluded_plans",
            "price_trend",
            "price_signal",
            "data_source",
            "data_freshness",
            "quota_overview",
            "provenance",
            "mixed_cabin",
        ):
            self.assertIn(section, no_match)

    def test_no_match_mixed_bundle_contains_every_canonical_section(self):
        from notification_sections import missing_notification_sections
        from notifier import render_detail_html, render_email

        payload = _fixture_payload()
        with patch("notifier._quota_overview_text", return_value="[配额总览] 测试台账"):
            _subject, email_html = render_email(payload)

        self.assertEqual(
            missing_notification_sections(
                email_html,
                "",
                trigger_type="no_match",
                mixed_cabin=True,
            ),
            [],
        )
        self.assertIn("9C6565:9C(operating)", email_html)
        self.assertIn("命中你设置的排除廉航条件", email_html)
        self.assertIn("同条件样本重新积累", email_html)
        self.assertIn("各舱最低单程拼算参考(不同航班,非可订组合)", email_html)
        self.assertIn("¥19,348", email_html)
        self.assertNotIn("团队合计", email_html)
        self.assertNotIn("¥10,089", email_html)

    def test_economy_no_match_bundle_also_has_complete_sections(self):
        from notification_sections import missing_notification_sections
        from notifier import render_detail_html, render_email

        payload = _fixture_payload()
        payload.pop("mixed_cabin")
        payload["cabin_policy_summary"] = {}
        with patch("notifier._quota_overview_text", return_value="[配额总览] 测试台账"):
            _subject, email_html = render_email(payload)

        self.assertEqual(
            missing_notification_sections(
                email_html,
                "",
                trigger_type="no_match",
                mixed_cabin=False,
            ),
            [],
        )
        self.assertNotIn("经济舱 / 商务舱并列参考", email_html)

    def test_missing_section_is_reported(self):
        from notification_sections import missing_notification_sections

        missing = missing_notification_sections(
            "行动面板 价格走势 价格信号 数据来源 采集时间",
            "数据依据 [配额总览]",
            trigger_type="no_match",
            mixed_cabin=False,
        )
        self.assertIn("alternative_plans", missing)
        self.assertIn("excluded_plans", missing)

    def test_no_match_renderer_backfills_every_missing_canonical_section(self):
        from notification_sections import missing_notification_sections
        from notifier import _ensure_no_match_notification_sections

        cards = ["<section>行动面板</section>"]
        rendered = "".join(
            _ensure_no_match_notification_sections(
                cards,
                {"cabin_policy_summary": {"cabin_arrangement": "mixed"}},
            )
        )

        self.assertEqual(
            missing_notification_sections(
                rendered, "", trigger_type="no_match", mixed_cabin=True
            ),
            [],
        )


class NoMatchExcludedRoundtripTest(unittest.TestCase):
    def test_no_recommendation_still_builds_complete_filtered_roundtrip_cards(self):
        from analyzer import build_excluded_roundtrip_combos

        outbound = {
            "all_flights": [
                _segment("MU225", "PVG", "KIX", "2026-10-01 09:15", "2026-10-01 12:00", 3363)
            ],
            "excluded_flights": [
                {
                    "flight": _segment("9C6565", "PVG", "KIX", "2026-10-01 08:30", "2026-10-01 11:45", 2800),
                    "price": 2800,
                    "reason": "命中你设置的排除廉航条件",
                    "filter_reason_code": "lcc_excluded",
                    "filter_reason_value": "9C6565:9C(operating)",
                }
            ],
        }
        return_analysis = {
            "all_flights": [
                _segment("MU516", "KIX", "PVG", "2026-10-06 14:20", "2026-10-06 16:10", 3363)
            ]
        }

        combos = build_excluded_roundtrip_combos(
            outbound,
            return_analysis,
            None,
            max_show=3,
            constraints={"lcc_policy": "exclude_lcc"},
            passengers={"adult": 1, "child": 0, "elderly": 0, "infant": 0},
            route_type="international",
            emit_diagnostics=False,
            include_without_reference=True,
        )

        self.assertEqual(len(combos), 1)
        self.assertEqual(combos[0]["outbound"]["flight_combo"], "9C6565")
        self.assertEqual(combos[0]["return"]["flight_combo"], "MU516")
        self.assertEqual(combos[0]["filter_reasons"][0]["code"], "lcc_excluded")
        self.assertEqual(combos[0]["filter_reasons"][0]["value"], "9C6565:9C(operating)")
        self.assertIsNone(combos[0]["diff"])


class MixedCabinNoMatchReferenceTest(unittest.TestCase):
    PASSENGERS = {"adult": 2, "child": 1, "elderly": 2, "infant": 0}
    ALLOCATION = {
        "business": {"adult": 0, "child": 0, "elderly": 2, "infant": 0},
        "economy": {"adult": 2, "child": 1, "elderly": 0, "infant": 0},
    }

    def test_synthetic_reference_uses_amount_tree_and_labels_non_orderable_oneway(self):
        from notifier import _build_mixed_cabin_reference_price

        result = _build_mixed_cabin_reference_price(
            primary_plan={},
            mixed_matching={"business_reference": {"raw_price": 5050}},
            cabin_summary={"economy_unit_price": 3363},
            cabin_allocation=self.ALLOCATION,
            passengers=self.PASSENGERS,
            route_type="international",
        )

        self.assertEqual(result["kind"], "synthetic_oneway")
        self.assertEqual(result["amount"], 19348)
        self.assertEqual(result["display_tree"]["outbound"]["component_sum"], 19348)
        self.assertEqual(
            result["label"],
            "各舱最低单程拼算参考(不同航班,非可订组合)",
        )

    def test_real_full_match_takes_precedence_over_synthetic_reference(self):
        from notifier import _build_mixed_cabin_reference_price

        result = _build_mixed_cabin_reference_price(
            primary_plan={
                "mixed_cabin": True,
                "mixed_cabin_pricing": {
                    "mixed_cabin": True,
                    "total": 40416,
                    "raw_total": 40415.5,
                },
            },
            mixed_matching={"business_reference": {"raw_price": 5050}},
            cabin_summary={"economy_unit_price": 3363},
            cabin_allocation=self.ALLOCATION,
            passengers=self.PASSENGERS,
            route_type="international",
        )

        self.assertEqual(result["kind"], "matched_roundtrip")
        self.assertEqual(result["amount"], 40416)
        self.assertEqual(result["scope"], "all_passengers_roundtrip")

    def test_real_full_match_in_matching_pool_takes_precedence_without_primary(self):
        from notifier import _build_mixed_cabin_reference_price

        result = _build_mixed_cabin_reference_price(
            primary_plan={},
            mixed_matching={
                "priceable": [
                    {
                        "mixed_cabin": True,
                        "mixed_cabin_pricing": {
                            "total": 41000,
                            "raw_total": 40999.5,
                        },
                    },
                    {
                        "mixed_cabin": True,
                        "mixed_cabin_pricing": {
                            "total": 40416,
                            "raw_total": 40415.5,
                        },
                    },
                ],
                "business_reference": {"raw_price": 5050},
            },
            cabin_summary={"economy_unit_price": 3363},
            cabin_allocation=self.ALLOCATION,
            passengers=self.PASSENGERS,
            route_type="international",
        )

        self.assertEqual(result["kind"], "matched_roundtrip")
        self.assertEqual(result["amount"], 40416)


if __name__ == "__main__":
    unittest.main()
