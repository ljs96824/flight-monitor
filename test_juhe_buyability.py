import sys
import types
import unittest

sys.modules.setdefault(
    "httpx",
    types.SimpleNamespace(get=lambda *a, **k: None, post=lambda *a, **k: None),
)

from analyzer import classify_buyability
from notifier import _price_estimate_summary_lines
from price_estimator import calc_transaction_price


class JuheBuyabilityTest(unittest.TestCase):
    def test_juhe_quote_without_seat_status_needs_payment_page_verification(self):
        result = classify_buyability(
            {
                "data_source": "juhe",
                "availability": {"age_minutes": 3},
                "price": 527,
            }
        )

        self.assertEqual(result["status"], "need_verify")
        self.assertIn("支付页", result["note"])

    def test_juhe_sold_out_status_still_supported_if_returned(self):
        result = classify_buyability(
            {
                "data_source": "juhe",
                "seat_status": "售罄",
                "availability": {"age_minutes": 3},
                "price": 527,
            }
        )

        self.assertEqual(result["status"], "sold_out")

    def test_juhe_price_estimate_shows_ticket_price_copy(self):
        flight = {
            "data_source": "juhe",
            "price": 527,
            "ticket_price": 527,
            "price_note": "票面价，实付以支付页为准",
            "price_includes": "票面价，不含机建燃油拆分",
            "airline": "KN",
        }

        estimate = calc_transaction_price(flight, {})
        flight["price_estimate"] = estimate
        lines = _price_estimate_summary_lines(flight)

        self.assertEqual(estimate["display_price"], 527)
        self.assertEqual(estimate["transaction_price"], 527)
        self.assertIn("票面价", lines[0])
        self.assertTrue(any("支付页" in line for line in lines))


if __name__ == "__main__":
    unittest.main()
