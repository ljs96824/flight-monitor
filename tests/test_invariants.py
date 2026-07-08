import sys
import types
import unittest


sys.modules.setdefault("httpx", types.SimpleNamespace(get=lambda *a, **k: None, post=lambda *a, **k: None))

from analyzer import (
    _same_day_outbound_passes_window,
    _same_day_return_passes_window,
    assert_price_budget_same_passenger_scope,
)
from notifier import _calendar_selected_level


class InvariantTest(unittest.TestCase):
    def test_next_day_arrival_does_not_satisfy_same_day_window(self):
        windows = {
            "outbound_arrive_by_minutes": 8 * 60 + 55,
            "return_depart_after_minutes": 20 * 60,
        }
        next_day_arrival = {
            "flight_no": "MU5185",
            "departure_time": "2026-06-26 22:30",
            "arrival_time": "2026-06-27 00:05",
            "departure_airport": "SHA",
            "arrival_airport": "PKX",
            "price": 1050,
        }
        same_day_return = {
            "flight_no": "MU5170",
            "departure_time": "2026-06-26 21:00",
            "arrival_time": "2026-06-26 23:10",
            "departure_airport": "PKX",
            "arrival_airport": "SHA",
            "price": 1720,
        }

        self.assertFalse(_same_day_outbound_passes_window(next_day_arrival, windows, "2026-06-26"))
        self.assertTrue(_same_day_return_passes_window(same_day_return, windows, "2026-06-26"))

    def test_calendar_selected_level_requires_same_passenger_scope(self):
        rows = [
            {
                "date": "2026-06-23",
                "weekday": "Tue",
                "min_price": 1104,
                "unit_roundtrip_price": 1104,
                "scope": "roundtrip",
                "lowest": True,
            },
            {
                "date": "2026-06-26",
                "weekday": "Fri",
                "min_price": 1479,
                "unit_roundtrip_price": 1479,
                "scope": "roundtrip",
                "selected": True,
            },
        ]
        selected = rows[1]

        level = _calendar_selected_level(rows, selected, selected_price=4437, passenger_factor=3)
        self.assertIn(level, {"偏贵", "中等水平", "较便宜"})

        with self.assertRaises(AssertionError):
            _calendar_selected_level(rows, selected, selected_price=1479, passenger_factor=3)

    def test_price_budget_scope_invariant_rejects_mixed_scope(self):
        self.assertTrue(assert_price_budget_same_passenger_scope("全员往返", "全员往返 vs 总上限1600"))
        with self.assertRaises(AssertionError):
            assert_price_budget_same_passenger_scope("单人往返", "全员往返 vs 总上限1600")


if __name__ == "__main__":
    unittest.main()
