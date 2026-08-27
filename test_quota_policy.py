import unittest
from datetime import date


class QuotaPolicyTest(unittest.TestCase):
    def setUp(self):
        self.purchased = {
            "kind": "purchased_packs",
            "packs": [
                {"id": "pack-2026-07", "added": 550, "added_at": "2026-07-22"},
                {"id": "pack-2026-08-27", "added": 550, "added_at": "2026-08-27"},
            ],
            "reconciliation": {
                "checked_at": "2026-08-27",
                "console_used": 447,
                "console_remaining": 653,
                "local_ledger_used": 428,
                "unrecorded_usage_adjustment": 19,
            },
            "reserve": {
                "kind": "monitoring_p90",
                "window_days": 7,
                "target_date": "2026-10-01",
                "safety_multiplier": 1.2,
                "emergency_calls": 20,
                "fallback_daily_p90": 11.5,
            },
        }

    def test_purchased_packs_and_legacy_integer_share_one_policy_api(self):
        import quota_policy

        snapshot = {
            "today": {"juhe": 9},
            "month": {"juhe": 219},
            "cumulative": {"juhe": 428},
        }

        self.assertEqual(quota_policy.total_limit(self.purchased), 1100)
        self.assertEqual(quota_policy.used(self.purchased, snapshot, "juhe"), 447)
        self.assertEqual(quota_policy.remaining(self.purchased, snapshot, "juhe"), 653)
        self.assertEqual(quota_policy.total_limit(550), 550)
        self.assertEqual(quota_policy.used(550, snapshot, "juhe"), 428)
        self.assertEqual(quota_policy.remaining(550, snapshot, "juhe"), 122)

    def test_dynamic_monitoring_reserve_uses_recent_non_research_p90(self):
        import quota_policy

        usage = {
            "dates": {
                "2026-08-21": {"juhe": 8},
                "2026-08-22": {"juhe": 9},
                "2026-08-23": {"juhe": 10},
                "2026-08-24": {"juhe": 11},
                "2026-08-25": {"juhe": 12},
                "2026-08-26": {"juhe": 13},
                "2026-08-27": {"juhe": 20},
            },
            "entries": [
                {
                    "day": "2026-08-27",
                    "round_id": "basket_research",
                    "counts": {"juhe": 6},
                }
            ],
        }
        # Excluding the research round leaves 8,9,10,11,12,13,14. Nearest-rank
        # P90 is 14; ceil(14 * 35 * 1.2 + 20) == 608.
        value = quota_policy.reserve(
            self.purchased,
            usage_payload=usage,
            source="juhe",
            as_of=date(2026, 8, 27),
            research_round_ids={"basket_research"},
        )

        self.assertEqual(value, 608)

    def test_dynamic_reserve_falls_back_only_when_window_has_no_usage(self):
        import quota_policy

        value = quota_policy.reserve(
            self.purchased,
            usage_payload={"dates": {}, "entries": []},
            source="juhe",
            as_of=date(2026, 8, 27),
        )

        self.assertEqual(value, 503)

    def test_research_available_is_remaining_less_dynamic_reserve(self):
        import quota_policy

        snapshot = {"today": {}, "month": {}, "cumulative": {"juhe": 428}}
        usage = {"dates": {}, "entries": []}

        self.assertEqual(
            quota_policy.research_available(
                self.purchased,
                snapshot,
                "juhe",
                usage_payload=usage,
                as_of=date(2026, 8, 27),
            ),
            150,
        )


    def test_quota_overview_uses_epoch_terms_and_console_reconcilable_values(self):
        from api_usage import format_quota_overview

        payload = {
            "dates": {"2026-08-27": {"juhe": 428}},
            "entries": [],
        }
        budgets = {
            "juhe": self.purchased,
            "serpapi": {"monthly": 250, "reserve": 30},
        }

        text = format_quota_overview(payload, budgets, day="2026-08-27")

        self.assertIn("juhe 本epoch已用=447/预算1100", text)
        self.assertIn("余量估算=653", text)
        self.assertIn("以聚合数据控制台为准", text)


if __name__ == "__main__":
    unittest.main()
