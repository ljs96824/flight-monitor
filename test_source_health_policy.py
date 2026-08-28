import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch


class SourceHealthPolicyTest(unittest.TestCase):
    def _check(
        self,
        source_stats,
        *,
        route_type,
        cabin_class="economy",
        observed_day="2026-08-28",
    ):
        import health_check

        with tempfile.TemporaryDirectory() as tmpdir:
            path = Path(tmpdir) / "source_health.json"
            with (
                patch.object(health_check, "SOURCE_HEALTH_PATH", path),
                patch.object(health_check, "safe_log") as log_mock,
            ):
                result = health_check.system_health_check(
                    source_stats={**source_stats, "after_dedup": 6},
                    flights=[{"price": 100}, {"price": 120}],
                    route_type=route_type,
                    cabin_class=cabin_class,
                    observed_day=observed_day,
                )
        return result, [str(call.args[0]) for call in log_mock.call_args_list]

    def assertCoverage(
        self,
        result,
        *,
        complete,
        expected,
        successful,
        missing,
        diversity,
        cross_check,
    ):
        self.assertIs(result["coverage_complete"], complete)
        self.assertEqual(result["expected_sources"], sorted(expected))
        self.assertEqual(result["successful_sources"], sorted(successful))
        self.assertEqual(result["missing_sources"], sorted(missing))
        self.assertEqual(result["source_diversity_n"], diversity)
        self.assertEqual(result["active_sources"], diversity)
        self.assertEqual(result["cross_check_status"], cross_check)

    def test_domestic_economy_juhe_is_complete_single_source_policy(self):
        result, logs = self._check(
            {"juhe": {"status": "成功", "count": 6}},
            route_type="domestic",
        )

        self.assertCoverage(
            result,
            complete=True,
            expected={"juhe"},
            successful={"juhe"},
            missing=set(),
            diversity=1,
            cross_check="not_performed",
        )
        self.assertNotIn("数据覆盖不足", result["warnings"])
        self.assertIn(
            "覆盖完整,当前为单源正式报价,未进行额外交叉验证",
            logs,
        )

    def test_international_economy_juhe_is_complete_without_cross_check(self):
        result, _logs = self._check(
            {"juhe": {"status": "成功", "count": 6}},
            route_type="international",
        )

        self.assertCoverage(
            result,
            complete=True,
            expected={"juhe"},
            successful={"juhe"},
            missing=set(),
            diversity=1,
            cross_check="not_performed",
        )

    def test_international_business_serpapi_is_complete(self):
        result, _logs = self._check(
            {"serpapi": {"status": "success", "count": 4}},
            route_type="international",
            cabin_class="business",
        )

        self.assertCoverage(
            result,
            complete=True,
            expected={"serpapi"},
            successful={"serpapi"},
            missing=set(),
            diversity=1,
            cross_check="not_performed",
        )

    def test_pre_retirement_expectation_keeps_hasdata_in_historical_policy(self):
        result, _logs = self._check(
            {"juhe": {"status": "成功", "count": 6}},
            route_type="international",
            observed_day="2026-08-13",
        )

        self.assertCoverage(
            result,
            complete=False,
            expected={"juhe", "hasdata"},
            successful={"juhe"},
            missing={"hasdata"},
            diversity=1,
            cross_check="not_performed",
        )
        self.assertIn("数据覆盖不足", result["warnings"])

    def test_enrichment_success_does_not_hide_missing_listing_source(self):
        result, _logs = self._check(
            {
                "juhe": {"status": "failed", "count": 0},
                "duffel": {"status": "success", "count": 3},
            },
            route_type="domestic",
        )

        self.assertCoverage(
            result,
            complete=False,
            expected={"juhe"},
            successful=set(),
            missing={"juhe"},
            diversity=0,
            cross_check="not_performed",
        )

    def test_two_listing_sources_are_complete_and_cross_checked(self):
        result, _logs = self._check(
            {
                "juhe": {"status": "成功", "count": 6},
                "hasdata": {"status": "success", "count": 4},
            },
            route_type="international",
            observed_day="2026-08-13",
        )

        self.assertCoverage(
            result,
            complete=True,
            expected={"juhe", "hasdata"},
            successful={"juhe", "hasdata"},
            missing=set(),
            diversity=2,
            cross_check="performed",
        )
        self.assertNotIn("数据覆盖不足", result["warnings"])


if __name__ == "__main__":
    unittest.main()
