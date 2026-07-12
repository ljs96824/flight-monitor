import unittest


class DomesticAircraftMappingTest(unittest.TestCase):
    def test_get_aircraft_name_translates_latest_equipment_codes(self):
        from domestic_fare_rules import get_aircraft_name

        expected = {
            "325": "空客A320",
            "32K": "空客A320",
            "32S": "空客A320",
            "330": "空客A330",
            "73H": "波音737-800",
            "767": "波音767",
            "777": "波音777",
            "781": "波音787-10",
            "7M8": "波音737 MAX 8",
        }

        for code, name in expected.items():
            with self.subTest(code=code):
                self.assertEqual(get_aircraft_name(code), name)
                self.assertNotIn("机型代码", get_aircraft_name(code))

    def test_get_aircraft_name_translates_juhe_equipment_codes(self):
        from domestic_fare_rules import get_aircraft_name

        self.assertEqual(get_aircraft_name("33L"), "空客A330")
        self.assertEqual(get_aircraft_name("33J"), "空客A330")
        self.assertEqual(get_aircraft_name("332"), "空客A330-200")
        self.assertEqual(get_aircraft_name("789"), "波音787-9")
        self.assertEqual(get_aircraft_name("773"), "波音777-300")
        self.assertEqual(get_aircraft_name("77W"), "波音777-300ER")
        self.assertEqual(get_aircraft_name("388"), "空客A380")
        self.assertEqual(get_aircraft_name("919"), "国产C919")
        self.assertEqual(get_aircraft_name("32H"), "\u7a7a\u5ba2A321")
        self.assertEqual(get_aircraft_name("327"), "\u7a7a\u5ba2A321")
        self.assertEqual(get_aircraft_name("324"), "\u7a7a\u5ba2A320")
        self.assertEqual(get_aircraft_name("326"), "\u7a7a\u5ba2A320")
        self.assertEqual(get_aircraft_name("322"), "\u7a7a\u5ba2A321")
        self.assertEqual(get_aircraft_name("350"), "\u7a7a\u5ba2A350")
        self.assertEqual(get_aircraft_name("73E"), "\u6ce2\u97f3737-800")
        self.assertEqual(get_aircraft_name("73U"), "\u6ce2\u97f3737-800")
        self.assertEqual(get_aircraft_name("78A"), "\u6ce2\u97f3787-8")
        self.assertEqual(get_aircraft_name("ZZZ"), "机型代码ZZZ(以航司为准)")

    def test_juhe_normalize_uses_shared_aircraft_mapping(self):
        from sources.juhe_source import JuheSource

        rows = JuheSource().normalize(
            [
                {
                    "flightNo": "MU5101",
                    "airline": "MU",
                    "airlineName": "东方航空",
                    "equipment": "33L",
                    "departure": "SHA",
                    "arrival": "PEK",
                    "departureDate": "2026-06-18",
                    "arrivalDate": "2026-06-18",
                    "departureTime": "07:00",
                    "arrivalTime": "09:15",
                    "ticketPrice": 897,
                    "transferNum": 1,
                }
            ]
        )

        self.assertEqual(rows[0]["aircraft"], "空客A330")
        self.assertEqual(rows[0]["segments"][0]["aircraft"], "空客A330")


if __name__ == "__main__":
    unittest.main()
