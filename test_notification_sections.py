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
    def test_existing_alternatives_are_enriched_with_specific_or_pairing_reason(self):
        from notifier import _prepare_no_result_alternatives

        mm80 = {"flight_no": "MM80", "flight_combo": "MM80", "price": 4905}
        spring = {"flight_no": "9C6575", "flight_combo": "9C6575", "price": 3367}
        existing = [
            {"flight": mm80, "tradeoff": "不满足当前约束"},
            {"flight": spring, "tradeoff": "不满足当前约束"},
        ]
        excluded = [
            {
                "flight": mm80,
                "reason": "命中你设置的排除廉航条件",
                "filter_reason_code": "lcc_excluded",
            }
        ]

        prepared = _prepare_no_result_alternatives(
            existing,
            [mm80, spring],
            excluded,
            default_reason="返程采集失败，无法组成完整往返",
            default_reason_code="return_collection_failed",
        )

        self.assertEqual(prepared[0]["unmet_reason"], "命中你设置的排除廉航条件")
        self.assertEqual(prepared[0]["filter_reason_code"], "lcc_excluded")
        self.assertEqual(prepared[1]["unmet_reason"], "返程采集失败，无法组成完整往返")
        self.assertEqual(prepared[1]["filter_reason_code"], "return_collection_failed")
    def test_no_result_candidate_pool_includes_both_directions(self):
        from notifier import _no_result_candidate_flights

        outbound = {"all_flights": [{"flight_no": "MU225", "price": 3000}]}
        returned = {"all_flights": [{"flight_no": "MU516", "price": 2800}]}

        outbound_only = _no_result_candidate_flights({}, outbound, returned, True)
        candidates = _no_result_candidate_flights(
            {}, outbound, returned, True, include_return=True
        )

        self.assertEqual(
            [(item["flight_no"], item["direction"]) for item in outbound_only],
            [("MU225", "outbound")],
        )
        self.assertEqual(
            [(item["flight_no"], item["direction"]) for item in candidates],
            [("MU225", "outbound"), ("MU516", "return")],
        )
    def test_no_combo_renders_deduplicated_single_leg_rejection_table(self):
        from notifier import _build_single_leg_rejection_rows, _email_excluded_compact_body

        candidates = [
            _segment("9C6581", "PVG", "KIX", "2026-10-01 07:10", "2026-10-01 10:20", 2885),
            _segment("MM080", "PVG", "KIX", "2026-10-01 06:15", "2026-10-01 09:35", 4905),
        ]
        excluded = [
            {
                "flight": _segment("BR705+BR182", "PVG", "KIX", "2026-10-01 08:00", "2026-10-01 16:00", 2500),
                "price": 2500,
                "direction": "outbound",
                "reason": "需要中转，但你设置了必须直飞",
                "filter_reason_code": "direct_only",
                "filter_reason_value": "stops=1",
            },
            {
                "flight": _segment("BR705+BR182", "PVG", "KIX", "2026-10-01 08:00", "2026-10-01 16:00", 2500),
                "price": 2500,
                "direction": "outbound",
                "reason": "需要中转，但你设置了必须直飞",
                "filter_reason_code": "direct_only",
                "filter_reason_value": "stops=1",
            },
        ]
        rows = _build_single_leg_rejection_rows(
            candidates,
            excluded,
            default_reason="返程采集失败，无法组成完整往返",
            default_reason_code="return_collection_failed",
        )
        payload = {
            "is_roundtrip": True,
            "no_primary_diagnosis": {"reason": "无完整往返组合"},
            "excluded_plans": [],
            "single_leg_rejections": rows,
        }

        rendered = _email_excluded_compact_body(payload)

        self.assertEqual(len(rows), 3)
        self.assertIn("逐航班拒因表", rendered)
        self.assertIn("BR705+BR182", rendered)
        self.assertIn("需要中转，但你设置了必须直飞", rendered)
        self.assertIn("9C6581", rendered)
        self.assertIn("返程采集失败，无法组成完整往返", rendered)
        self.assertLess(rendered.index("BR705+BR182"), rendered.index("9C6581"))

    def test_fallback_card_and_relaxation_preview_show_actual_reason(self):
        from notifier import _no_primary_next_step_text, _same_day_alternative_card

        alternative = {
            "title": "备选A · 最接近条件",
            "flight": _segment("MM080", "PVG", "KIX", "2026-10-01 06:15", "2026-10-01 09:35", 4905),
            "price": 4905,
            "unmet_reason": "返程采集失败，无法组成完整往返",
            "tradeoff": "返程采集失败，无法组成完整往返",
        }
        payload = {
            "depart_date": "2026-10-01",
            "same_day_alternatives": [alternative],
            "single_leg_rejections": [
                {
                    "flight": alternative["flight"],
                    "reason": alternative["unmet_reason"],
                    "filter_reason_code": "return_collection_failed",
                    "direction": "outbound",
                    "stops": 0,
                }
            ],
        }

        rendered = _same_day_alternative_card(alternative, payload)
        guidance = _no_primary_next_step_text(payload)

        self.assertIn("未达条件", rendered)
        self.assertIn("返程采集失败，无法组成完整往返", rendered)
        self.assertIn("恢复返程采集", guidance)
        self.assertIn("1个直飞去程", guidance)
    def test_20260823_archive_replay_keeps_candidate_reason_and_single_leg_table(self):
        from analyzer import build_no_result_alternatives, build_no_result_diagnosis
        from notifier import (
            _build_single_leg_rejection_rows,
            _candidate_price_summary_text,
            _email_excluded_compact_body,
            _no_primary_next_step_text,
            _same_day_alternative_card,
        )

        fixture_path = (
            BASE_DIR / "tests" / "fixtures" / "no_result_20260823_v1.json"
        )
        fixture = json.loads(fixture_path.read_text(encoding="utf-8"))
        diagnosis = build_no_result_diagnosis(
            fixture["candidates"],
            fixture["excluded"],
            stage_counts=fixture["stage_counts"],
            fallback_reason=fixture["default_reason"],
        )
        alternatives = build_no_result_alternatives(
            fixture["candidates"],
            fixture["excluded"],
            default_reason=fixture["default_reason"],
            default_reason_code="return_collection_failed",
        )
        rows = _build_single_leg_rejection_rows(
            fixture["candidates"],
            fixture["excluded"],
            default_reason=fixture["default_reason"],
            default_reason_code="return_collection_failed",
        )
        payload = {
            "is_roundtrip": True,
            "depart_date": "2026-10-01",
            "candidate_price_summary": diagnosis["price_summary"],
            "no_primary_diagnosis": diagnosis["counts"],
            "same_day_alternatives": alternatives,
            "single_leg_rejections": rows,
            "excluded_plans": [],
        }

        price_text = _candidate_price_summary_text(payload)
        cards = "".join(
            _same_day_alternative_card(item, payload) for item in alternatives
        )
        excluded_html = _email_excluded_compact_body(payload)
        guidance = _no_primary_next_step_text(payload)

        self.assertEqual(
            fixture["round_id"],
            "collection_20260823T210014035475",
        )
        self.assertIn("【完整往返】", diagnosis["reason"])
        self.assertNotIn("直飞/基础筛选排除10个", diagnosis["reason"])
        self.assertIn("¥2,885", price_text)
        self.assertIn("返程采集失败", price_text)
        self.assertNotIn("直飞要求不符", price_text)
        self.assertEqual(cards.count("未达条件"), len(alternatives))
        self.assertIn("MM80", cards)
        self.assertIn("9C6575", cards)
        self.assertIn("逐航班拒因表", excluded_html)
        self.assertIn("BR705+BR182", excluded_html)
        self.assertIn("返程采集失败", excluded_html)
        self.assertIn("恢复返程采集", guidance)
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
