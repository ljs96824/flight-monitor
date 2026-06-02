import unittest

from airports import resolve_location


class ResolveLocationTest(unittest.TestCase):
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
