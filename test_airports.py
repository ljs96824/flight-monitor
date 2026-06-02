import unittest

from airports import resolve_location


class ResolveLocationTest(unittest.TestCase):
    def test_resolve_location_known_city(self):
        self.assertEqual(
            resolve_location("大阪"),
            {
                "value": "大阪",
                "type": "city",
                "airports": ["KIX", "ITM"],
            },
        )

    def test_resolve_location_iata_code(self):
        self.assertEqual(
            resolve_location("kix"),
            {
                "value": "KIX",
                "type": "airport",
                "airports": ["KIX"],
            },
        )

    def test_resolve_location_unknown_chinese_city(self):
        self.assertEqual(
            resolve_location("重庆"),
            {
                "value": "重庆",
                "type": "unknown",
                "airports": [],
            },
        )


if __name__ == "__main__":
    unittest.main()
