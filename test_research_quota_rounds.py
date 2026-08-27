import tempfile
import unittest
from datetime import date
from pathlib import Path
from unittest.mock import patch


class _JuheSource:
    name = "juhe"


def _source_builder(_origin, _dest, route_type=None):
    return [_JuheSource()], []


class ResearchQuotaRoundsTest(unittest.TestCase):
    def test_subscription_unique_requests_are_multiplied_by_daily_rounds(self):
        from research_cohort import simulate_research_quota

        for unique_count, expected_daily in ((1, 3), (2, 6), (4, 12), (6, 18)):
            with self.subTest(unique_count=unique_count):
                result = simulate_research_quota(
                    basket_keys=set(),
                    subscription_keys={
                        f"subscription-{index}" for index in range(unique_count)
                    },
                    scheduled_subscription_runs_per_day=3,
                    other_non_subscription_calls_per_day=0,
                    quota_remaining=180,
                    retries_per_request=1,
                )

                self.assertEqual(
                    result["subscription_daily_expected"],
                    expected_daily,
                )

    def test_basket_and_subscription_same_key_are_not_deduped_across_processes(self):
        from research_cohort import simulate_research_quota

        result = simulate_research_quota(
            basket_keys={"shared"},
            subscription_keys={"shared"},
            scheduled_subscription_runs_per_day=3,
            other_non_subscription_calls_per_day=0,
            quota_remaining=120,
            retries_per_request=1,
        )

        self.assertEqual(result["basket_planned_unique"], 1)
        self.assertEqual(result["subscription_daily_expected"], 3)
        self.assertEqual(result["combined_daily_expected"], 4)
        self.assertEqual(result["combined_daily_worst_case"], 8)

    def test_roundtrip_subscription_counts_both_legs_in_each_daily_round(self):
        from api_usage import initialize_usage_ledger
        from basket_collect import _simulate_runtime_quota

        subscription = {
            "status": "active",
            "origin": "PVG",
            "destination": "KIX",
            "depart_date": "2026-10-01",
            "return_date": "2026-10-06",
            "round_trip": True,
            "route_type": "international",
            "passengers": {"adult": 1},
        }
        settings = {
            "source_quota_budget": {"juhe": 550},
            "freshness_hours": 6,
            "sub_round_fresh_scope": "primary_only",
            "research_cohort_v2_gates": {
                "scheduled_subscription_runs_per_day": 3,
                "other_non_subscription_calls_per_day": 0,
            },
        }
        with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as tmp:
            usage_path = Path(tmp) / "api_usage.json"
            initialize_usage_ledger(usage_path)
            with patch("collection_plan._calendar_dates", return_value=[]):
                result = _simulate_runtime_quota(
                    research_requests=[],
                    subscriptions=[subscription],
                    settings=settings,
                    source_builder=_source_builder,
                    usage_path=usage_path,
                    today=date(2026, 8, 27),
                )

        self.assertEqual(result["subscription_planned_unique"], 2)
        self.assertEqual(result["subscription_daily_expected"], 6)
        self.assertEqual(result["combined_daily_expected"], 6)
        self.assertEqual(result["combined_daily_worst_case"], 12)

    def test_readiness_day_estimates_use_repeated_subscription_rounds(self):
        from research_cohort import simulate_research_quota

        result = simulate_research_quota(
            basket_keys={f"basket-{index}" for index in range(6)},
            subscription_keys={"outbound", "return"},
            scheduled_subscription_runs_per_day=3,
            other_non_subscription_calls_per_day=0,
            quota_remaining=120,
            retries_per_request=1,
        )

        self.assertEqual(result["subscription_daily_expected"], 6)
        self.assertEqual(result["subscription_daily_worst_case"], 12)
        self.assertEqual(result["combined_daily_expected"], 12)
        self.assertEqual(result["combined_daily_worst_case"], 24)
        self.assertEqual(result["expected_days_remaining"], 10)
        self.assertEqual(result["worst_case_days_remaining"], 5)


if __name__ == "__main__":
    unittest.main()
