import copy
import unittest

from analyzer import transfer_risk


class TransferRiskCharacterizationTest(unittest.TestCase):
    def assert_risk(self, flight, expected):
        original = copy.deepcopy(flight)

        result = transfer_risk(flight)

        self.assertEqual(result, expected)
        self.assertEqual(list(result), list(expected))
        self.assertIsInstance(result["level"], str)
        self.assertIsInstance(result["label"], str)
        self.assertIsInstance(result["score"], int)
        self.assertIsInstance(result["factors"], list)
        self.assertNotIn("reason_codes", result)
        self.assertEqual(flight, original)

    def test_direct_and_reasonable_transfer_exact_outputs(self):
        self.assert_risk(
            {
                "flight_combo": "MU225",
                "stops": 0,
                "layovers": [{"wait_minutes": 45, "airport": "NRT"}],
                "segments": [{"airline": "MU"}, {"airline": "JL"}],
            },
            {"level": "none", "label": "直飞", "score": 0, "factors": []},
        )
        self.assert_risk(
            {
                "flight_combo": "MU225+JL891",
                "stops": 1,
                "layovers": [
                    {"wait_minutes": 180, "airport": "PVG", "city": "上海"}
                ],
                "segments": [{"airline": "MU"}, {"airline": "MU"}],
            },
            {"level": "low", "label": "低风险", "score": 0, "factors": []},
        )

    def test_short_long_and_threshold_boundaries(self):
        cases = [
            (
                89,
                {
                    "level": "medium",
                    "label": "中风险",
                    "score": 40,
                    "factors": ["中转时间仅89分钟，可能赶不上"],
                },
            ),
            (
                90,
                {
                    "level": "low",
                    "label": "低风险",
                    "score": 15,
                    "factors": ["中转时间90分钟，较紧张"],
                },
            ),
            (
                119,
                {
                    "level": "low",
                    "label": "低风险",
                    "score": 15,
                    "factors": ["中转时间119分钟，较紧张"],
                },
            ),
            (120, {"level": "low", "label": "低风险", "score": 0, "factors": []}),
            (480, {"level": "low", "label": "低风险", "score": 0, "factors": []}),
            (
                481,
                {
                    "level": "low",
                    "label": "低风险",
                    "score": 10,
                    "factors": ["中转等待8小时，较长"],
                },
            ),
        ]
        for wait_minutes, expected in cases:
            with self.subTest(wait_minutes=wait_minutes):
                self.assert_risk(
                    {
                        "stops": 1,
                        "layovers": [
                            {"wait_minutes": wait_minutes, "airport": "PVG"}
                        ],
                        "segments": [{"airline": "MU"}, {"airline": "MU"}],
                    },
                    expected,
                )

    def test_non_through_is_inferred_only_from_cross_airline_factor(self):
        self.assert_risk(
            {
                "stops": 1,
                "self_transfer": True,
                "separate_tickets": True,
                "is_through_ticket": False,
                "airlines": ["MU"],
                "segments": [{"airline": "JL"}, {"airline": "MU"}],
                "layovers": [{"wait_minutes": 180, "airport": "PVG"}],
            },
            {
                "level": "medium",
                "label": "中风险",
                "score": 25,
                "factors": ["跨航司（JL/MU），可能非联程"],
            },
        )
        self.assert_risk(
            {
                "stops": 1,
                "self_transfer": True,
                "separate_tickets": True,
                "is_through_ticket": False,
                "segments": [{"airline": "MU"}, {"airline": "MU"}],
                "layovers": [{"wait_minutes": 180, "airport": "PVG"}],
            },
            {"level": "low", "label": "低风险", "score": 0, "factors": []},
        )

    def test_airport_change_and_overnight_flags_do_not_add_separate_factors(self):
        self.assert_risk(
            {
                "stops": 1,
                "airport_change": True,
                "change_airport": True,
                "layovers": [
                    {
                        "wait_minutes": 180,
                        "airport": "PVG",
                        "airport_change": True,
                    }
                ],
                "segments": [{"airline": "MU"}, {"airline": "MU"}],
            },
            {"level": "low", "label": "低风险", "score": 0, "factors": []},
        )
        self.assert_risk(
            {
                "stops": 1,
                "overnight_transfer": True,
                "layovers": [{"wait_minutes": 720, "airport": "PVG"}],
                "segments": [{"airline": "MU"}, {"airline": "MU"}],
            },
            {
                "level": "low",
                "label": "低风险",
                "score": 10,
                "factors": ["中转等待12小时，较长"],
            },
        )

    def test_multiple_stops_transit_airport_and_factor_order(self):
        self.assert_risk(
            {
                "stops": "2",
                "airlines": ["MU"],
                "segments": [None, {"airline": "JL"}, {"airline": "MU"}],
                "layovers": [
                    {"wait_minutes": 60, "airport": "NRT"},
                    {"wait_minutes": 600, "airport": "HKG"},
                ],
            },
            {
                "level": "high",
                "label": "高风险",
                "score": 135,
                "factors": [
                    "多次中转",
                    "中转时间仅60分钟，可能赶不上",
                    "中转等待10小时，较长",
                    "跨航司（JL/MU），可能非联程",
                    "经东京成田中转，请确认是否需要过境签",
                    "经香港中转，请确认是否需要过境签",
                ],
            },
        )

    def test_missing_information_and_invalid_inputs(self):
        self.assert_risk(
            {"flight_combo": "UNKNOWN", "stops": 1},
            {"level": "low", "label": "低风险", "score": 0, "factors": []},
        )
        self.assert_risk(
            {},
            {"level": "none", "label": "直飞", "score": 0, "factors": []},
        )
        with self.assertRaisesRegex(AttributeError, "get"):
            transfer_risk(None)
        with self.assertRaisesRegex(AttributeError, "get"):
            transfer_risk({"stops": 1, "layovers": [None]})

    def test_elderly_child_scenario_does_not_change_transfer_result(self):
        base = {
            "stops": 1,
            "layovers": [{"wait_minutes": 60, "airport": "NRT"}],
            "segments": [{"airline": "MU"}, {"airline": "JL"}],
        }
        expected = {
            "level": "high",
            "label": "高风险",
            "score": 80,
            "factors": [
                "中转时间仅60分钟，可能赶不上",
                "跨航司（JL/MU），可能非联程",
                "经东京成田中转，请确认是否需要过境签",
            ],
        }
        self.assert_risk(base, expected)
        self.assert_risk(
            {
                **base,
                "companions": "with_elderly_child",
                "travel_scenarios": ["family", "with_elderly"],
                "passengers": {"adult": 2, "child": 1, "elderly": 2},
            },
            expected,
        )


if __name__ == "__main__":
    unittest.main()
