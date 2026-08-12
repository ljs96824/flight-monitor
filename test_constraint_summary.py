import unittest

from analyzer import _roundtrip_exclusion_basis
from constraint_summary import build_constraint_summary, format_constraint_summary


class ConstraintSummaryTest(unittest.TestCase):
    def test_shared_summary_matches_existing_email_basis(self):
        constraints = {
            "same_day_round_trip": True,
            "business_start": "10:30",
            "business_end": "17:00",
            "transfer_policy": "direct_only",
            "baggage": "required",
            "lcc_policy": "exclude_lcc",
            "max_budget": 8000,
            "max_budget_scope": "per_person",
        }
        passengers = {"adult": 2, "child": 1, "elderly": 2, "infant": 0}
        expected = [
            "当天往返",
            "会议10:30-17:00",
            "必须直飞",
            "必须含托运",
            "排除廉航",
            "最高可接受价¥38,000(全员,=单人¥8,000×4.75)",
        ]
        shared = build_constraint_summary(
            constraints,
            max_budget=38000,
            passengers=passengers,
            route_type="international",
        )
        self.assertEqual(shared, expected)
        self.assertEqual(
            _roundtrip_exclusion_basis(
                constraints,
                38000,
                passengers,
                "international",
            ),
            shared,
        )

    def test_shared_formatter_preserves_existing_email_text(self):
        self.assertEqual(
            format_constraint_summary(["\u5fc5\u987b\u76f4\u98de", "\u5fc5\u987b\u542b\u6258\u8fd0"]),
            "\u4f9d\u636e:\u5fc5\u987b\u76f4\u98de\u00b7\u5fc5\u987b\u542b\u6258\u8fd0",
        )

    def test_legacy_all_passenger_budget_scope_aliases_remain_all_passenger(self):
        for scope in ("overall", "all_passenger", "整单", "全员", "全部人"):
            with self.subTest(scope=scope):
                parts = build_constraint_summary(
                    {
                        "max_budget": 8000,
                        "max_budget_scope": scope,
                    },
                    max_budget=8000,
                    passengers={"adult": 2, "child": 1},
                    route_type="international",
                )
                self.assertEqual(parts[-1], "最高可接受价¥8,000(全员往返)")


if __name__ == "__main__":
    unittest.main()
