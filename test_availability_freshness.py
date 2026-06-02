import unittest
import sys
import types
from datetime import datetime, timedelta

sys.modules.setdefault("httpx", types.SimpleNamespace())

from analyzer import estimate_availability
from notifier import _payload_freshness_text


class AvailabilityFreshnessTest(unittest.TestCase):
    def test_recent_collected_at_produces_small_age(self):
        collected_at = (datetime.now() - timedelta(minutes=2)).isoformat()
        availability = estimate_availability(
            {
                "price": 4200,
                "data_source": "serpapi+hasdata",
                "collected_at": collected_at,
            }
        )

        self.assertIsNotNone(availability["age_minutes"])
        self.assertLess(availability["age_minutes"], 10)
        self.assertNotEqual(availability["age_minutes"], 9999)

    def test_missing_collected_at_stays_unknown_instead_of_9999(self):
        availability = estimate_availability(
            {
                "price": 4200,
                "data_source": "serpapi",
            }
        )

        self.assertIsNone(availability["age_minutes"])
        self.assertIn("采集时间未知", availability["label"])

    def test_payload_freshness_text_does_not_append_minutes_to_unknown(self):
        text = _payload_freshness_text({"freshness_minutes": None})

        self.assertEqual(text, "采集时间待确认")
        self.assertNotIn("分钟前", text)


if __name__ == "__main__":
    unittest.main()
