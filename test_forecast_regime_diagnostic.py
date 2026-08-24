import unittest


class ForecastRegimeDiagnosticTest(unittest.TestCase):
    def test_cross_regime_candidates_are_diagnostic_only(self):
        from scripts.forecast_report import _diagnostic_cross_regime_candidates

        shapes = {
            "holiday": {
                10: {
                    "n": 1,
                    "sufficient": False,
                    "raw": {"median": 1.2},
                }
            },
            "normal": {
                10: {
                    "n": 8,
                    "sufficient": True,
                    "median": 1.0,
                    "raw": {"median": 1.0},
                }
            },
        }

        lines = _diagnostic_cross_regime_candidates(
            shapes,
            target_regime="holiday",
            target_t_values=[10],
        )

        self.assertEqual(len(lines), 1)
        self.assertIn("regime=normal", lines[0])
        self.assertIn("原始值,不可用于判断", lines[0])
        self.assertNotIn("regime=holiday", lines[0])


if __name__ == "__main__":
    unittest.main()
