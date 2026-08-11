import unittest
from datetime import date


class HolidayFactsTest(unittest.TestCase):
    def test_route_labels_include_country_holiday_and_relative_day(self):
        from holidays import holiday_labels_for_route

        labels = holiday_labels_for_route("PVG", "KIX", date(2026, 10, 1))
        self.assertIn("中国大陆·国庆节(当天)", labels)

    def test_japan_holiday_shoulder_is_labeled(self):
        from holidays import holiday_labels_for_route

        labels = holiday_labels_for_route("PVG", "KIX", date(2026, 9, 22))
        self.assertIn("日本·秋分日(节前1日)", labels)

    def test_holiday_registry_digest_is_frozen(self):
        from holidays import HOLIDAY_DATA_DIGEST, EXPECTED_HOLIDAY_DATA_DIGEST

        self.assertEqual(HOLIDAY_DATA_DIGEST, EXPECTED_HOLIDAY_DATA_DIGEST)


if __name__ == "__main__":
    unittest.main()
