"""F11c：已删除的不可达旧代码不得重新出现。"""

from __future__ import annotations

import unittest

import analyzer
import notifier

RETIRED_NOTIFIER_NAMES = (
    "_round_trip_price_estimate_line",
    "_price_discrepancy_notice",
    "_min_date",
    "_service_info_lines",
    "_freshness_label",
    "_round_trip_combo_flight_line",
    "_append_round_trip_combo_lines",
    "_append_push_reason_section",
)

RETIRED_ANALYZER_NAMES = (
    "timing_analysis",
    "weekday_analysis",
    "airline_competition_analysis",
    "detect_anomaly",
    "nearby_dates_comparison",
    "TIME_SLOT_LABELS",
)


class RetiredDeadCodeTest(unittest.TestCase):
    def test_retired_notifier_names_are_absent(self):
        self.assertEqual([name for name in RETIRED_NOTIFIER_NAMES if hasattr(notifier, name)], [])

    def test_retired_analyzer_names_are_absent(self):
        self.assertEqual([name for name in RETIRED_ANALYZER_NAMES if hasattr(analyzer, name)], [])


if __name__ == "__main__":
    unittest.main()
