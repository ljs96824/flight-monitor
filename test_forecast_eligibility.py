import inspect
import unittest

import forecast
from forecast import assess_overall_reliability


def _eligible_kwargs():
    return {
        "level": {"reliable": True, "n": 5},
        "shape_points": [{"n": 5, "sufficient": True}],
        "backtest_gate": {"passed": True, "case_n": 8},
        "source_coverage": True,
        "regime_sample_n": 5,
        "lineage_complete": True,
        "regime": "normal",
    }


class ForecastEligibilityTest(unittest.TestCase):
    def test_overall_reliability_is_strict_shortest_board_for_every_component(self):
        base = {
            "level": {"reliable": True, "n": 5},
            "shape_points": [{"n": 5, "sufficient": True}],
            "backtest_gate": {"passed": True, "case_n": 8},
            "source_coverage": True,
            "regime_sample_n": 5,
        }
        mutations = (
            {"level": {"reliable": False, "n": 3}},
            {"shape_points": [{"n": 4, "sufficient": False}]},
            {"backtest_gate": {"passed": False, "case_n": 8}},
            {"source_coverage": False},
            {"regime_sample_n": 4},
        )

        self.assertTrue(assess_overall_reliability(**base)["passed"])
        for mutation in mutations:
            with self.subTest(mutation=mutation):
                result = assess_overall_reliability(**{**base, **mutation})
                self.assertFalse(result["passed"])
                self.assertEqual(result["value"], 0)

    def test_unified_decision_returns_all_six_machine_states(self):
        evaluate = forecast.evaluate_forecast_eligibility
        cases = (
            ("eligible", {}),
            ("insufficient_shape", {"shape_points": [{"n": 4, "sufficient": False}]}),
            ("skill_gate_failed", {"backtest_gate": {"passed": False, "case_n": 8}}),
            ("source_degraded", {"source_coverage": False}),
            ("regime_insufficient", {"regime_sample_n": 4, "regime": "holiday"}),
            ("lineage_incomplete", {"lineage_complete": False}),
        )

        for expected, mutation in cases:
            with self.subTest(expected=expected):
                result = evaluate(**{**_eligible_kwargs(), **mutation})
                self.assertEqual(result["status"], expected)
                self.assertIn("bottleneck", result)
                self.assertIsInstance(result["reason_codes"], list)
                self.assertTrue(result["human_text"])

    def test_high_components_never_compensate_a_low_component(self):
        result = forecast.evaluate_forecast_eligibility(
            **{**_eligible_kwargs(), "source_coverage": False}
        )

        self.assertEqual(result["status"], "source_degraded")
        self.assertEqual(result["overall_reliability"]["value"], 0)
        self.assertFalse(result["overall_reliability"]["passed"])

    def test_report_and_notification_builders_have_no_component_gate_copies(self):
        from scripts import forecast_report

        sources = {
            "build_notification_forecast": inspect.getsource(
                forecast.build_notification_forecast
            ),
            "generate_report": inspect.getsource(forecast_report.generate_report),
        }
        forbidden = (
            'gate.get("passed")',
            'level.get("reliable")',
            'reliability["passed"]',
            "components[\"shape_reliability\"][\"passed\"]",
            "components[\"backtest_skill\"][\"passed\"]",
            "components[\"source_coverage\"][\"passed\"]",
            "components[\"regime_match\"][\"passed\"]",
        )
        for name, source in sources.items():
            with self.subTest(name=name):
                for token in forbidden:
                    self.assertNotIn(token, source)
                self.assertIn("evaluate_forecast_eligibility", source)


if __name__ == "__main__":
    unittest.main()
