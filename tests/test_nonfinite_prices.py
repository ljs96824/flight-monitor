"""非有限价格：报价出现 inf / NaN / 超大数时，整轮不得失败，展示不得抛异常或出现 inf、nan。

有效价格口径沿用 2026-09-18 日历模块先例 price_calendar._valid_price：
float 转换失败(含 OverflowError)、非有限或不大于 0 → 无效。
"""

import unittest
from datetime import date
from decimal import Decimal

import main
import notifier
import price_calendar
import price_estimator
from sources import aggregator
from tests import test_return_phase_processing as rp


NONFINITE = (
    float("inf"), float("-inf"), float("nan"), "inf", "Infinity", "NaN", "1e400",
    Decimal("1e400"), Decimal("Infinity"), 10**400,
)
FINITE = ((1200, "¥1,200"), ("1200", "¥1,200"), (500.25, "¥500"), (Decimal("500.5"), "¥501"))
OTHER = (0, -1, None, "", "¥500", [500], True, False)


class ScalarContractTest(unittest.TestCase):
    def test_validity_matches_calendar_precedent(self):
        for value in NONFINITE + tuple(item for item, _text in FINITE) + OTHER:
            with self.subTest(value=repr(value)[:24]):
                expected = price_calendar._valid_price(value)
                self.assertIs(main._valid_price(value), expected)
                self.assertIs(aggregator._valid_price(value), expected)
                self.assertIs(notifier._has_valid_price(value), expected)
                self.assertIs(notifier._valid_price_float(value) is not None, expected)

    def test_display_never_raises_or_leaks(self):
        for value in NONFINITE:
            with self.subTest(value=repr(value)[:24]):
                self.assertEqual(notifier.format_price(value), "暂无报价")
                self.assertEqual(notifier._price_text(value), "暂无报价")
                self.assertIsNone(notifier._to_float(value))
        for value, text in FINITE:
            with self.subTest(value=repr(value)):
                self.assertEqual(notifier.format_price(value), text)
        for value in (float("inf"), float("-inf"), float("nan"), Decimal("Infinity"), "NaN"):
            with self.subTest(rounding=repr(value)):
                self.assertIsNone(price_estimator.round_display_price(value))
        self.assertEqual(price_estimator.round_display_price(500.5), 501)


class NonFiniteQuoteEndToEndTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        rp.ReturnPhaseProcessingTest.setUpClass()

    def setUp(self):
        self.harness = rp.ReturnPhaseProcessingTest("test_return_new_collection")
        self.harness.setUp()
        self.addCleanup(self.harness.doCleanups)

    @staticmethod
    def quote(outbound, price):
        item = rp.flight(outbound, 1200)
        combo = "MU105" if outbound else "MU106"
        item.update(price=price, flight_combo=combo)
        for segment in item["segments"]:
            segment["flight_no"] = combo
        return item

    def assert_clean_delivery(self, result, bad_combo):
        self.assertTrue(result["ok"], result["stdout"][-1500:])
        self.assertEqual(len(result["pushes"]), 1)
        for text in (result["emails"][0][2], result["pages"][0][1], result["pushes"][0][1]):
            self.assertFalse(bad_combo in text, "nonfinite_quote_excluded")
            for token in ("¥inf", "¥nan", "¥-inf", "¥Infinity"):
                self.assertFalse(token in text, token)

    def test_roundtrip_round_survives_nonfinite_quote(self):
        for price in (float("inf"), "inf", "1e400", 10**400, float("nan")):
            with self.subTest(price=repr(price)[:12]):
                result = self.harness.run_case(today=date(2026, 9, 30),
                                               extra_flights={"out": [self.quote(True, price)]})
                self.assert_clean_delivery(result, "MU105")
                plans = result["payloads"][0]["recommended_plans"]
                self.assertTrue(plans)
                self.assertEqual({(plan.get("outbound_flight") or {}).get("flight_combo") for plan in plans}, {"MU101"})

    def test_return_phase_survives_nonfinite_quote(self):
        for price in (float("inf"), 10**400):
            with self.subTest(price=repr(price)[:12]):
                result = self.harness.run_case(today=date(2026, 10, 2),
                                               extra_flights={"ret": [self.quote(False, price)]})
                self.assert_clean_delivery(result, "MU106")
                plans = result["payloads"][0]["recommended_plans"]
                self.assertEqual([plan["main_flight"]["flight_combo"] for plan in plans], ["MU102"])


if __name__ == "__main__":
    unittest.main()
