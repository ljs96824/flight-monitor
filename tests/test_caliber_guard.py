import sys
import types
import unittest

sys.modules.setdefault("httpx", types.SimpleNamespace(get=lambda *a, **k: None, post=lambda *a, **k: None))

from notifier import _calendar_selected_level, _scoped_price_text_from_pp
from pricing import assert_same_caliber


class CaliberGuardTest(unittest.TestCase):
    def test_mixed_caliber_comparison_fails(self):
        with self.assertRaises(AssertionError):
            assert_same_caliber("per_person_roundtrip", "all_passengers_roundtrip")

    def test_same_caliber_comparison_passes(self):
        self.assertTrue(assert_same_caliber("per_person_roundtrip", "roundtrip"))

    def test_calendar_and_selected_price_same_caliber_passes(self):
        rows = [
            {
                "date": "2026-06-24",
                "unit_price": 1200,
                "price": 3600,
                "price_scope": "\u5168\u5458\u5f80\u8fd4",
            },
            {
                "date": "2026-06-26",
                "unit_price": 1479,
                "price": 4437,
                "price_scope": "\u5168\u5458\u5f80\u8fd4",
                "selected": True,
            },
        ]

        level = _calendar_selected_level(rows, rows[1], 4437, passenger_factor=3)

        self.assertIn(level, {"\u8f83\u4fbf\u5b9c", "\u4e2d\u7b49\u6c34\u5e73", "\u504f\u8d35"})

    def test_scoped_price_text_uses_price_in_scope_and_label(self):
        text = _scoped_price_text_from_pp(
            699,
            passengers={"adult": 3, "child": 0, "elderly": 0, "infant": 0},
            scope="per_person_roundtrip",
            route_type="domestic",
            round_trip=True,
        )

        self.assertEqual(text, "\u00a51,398 \u5355\u4eba\u5f80\u8fd4")


if __name__ == "__main__":
    unittest.main()
