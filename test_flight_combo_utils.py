import unittest

from flight_combo_utils import normalize_combo, normalize_flight_no


class FlightComboUtilsTest(unittest.TestCase):
    def test_normalize_combo_unifies_separator_case_space_and_leading_zero(self):
        self.assertEqual(normalize_combo("BR0705|BR0182"), "BR705+BR182")
        self.assertEqual(normalize_combo("SQ0825|SQ622"), "SQ825+SQ622")
        self.assertEqual(normalize_combo("SQ831|SQ0618"), "SQ831+SQ618")
        self.assertEqual(normalize_combo(" br0705 / br0182 "), "BR705+BR182")
        self.assertEqual(normalize_combo("OZ3625+OZ1165"), "OZ3625+OZ1165")
        self.assertEqual(normalize_combo("MU 225"), "MU225")

    def test_normalize_flight_no_handles_digit_prefixed_airline_codes(self):
        self.assertEqual(normalize_flight_no("9C0657"), "9C657")
        self.assertEqual(normalize_flight_no("CA0012A"), "CA12A")


if __name__ == "__main__":
    unittest.main()
