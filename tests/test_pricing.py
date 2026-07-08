import unittest

from pricing import (
    budget_to_pp,
    caliber_label,
    itinerary_price_pp,
    passenger_rate_sum,
    price_in_scope,
)


class PricingPureFunctionsTest(unittest.TestCase):
    def test_itinerary_price_pp_roundtrip_sums_outbound_and_return(self):
        self.assertEqual(itinerary_price_pp(800, return_per_person_oneway=900), 1700)
        self.assertEqual(itinerary_price_pp(800, round_trip=True), 1600)

    def test_passenger_rate_sum_reuses_existing_per_type_rates(self):
        passengers = {"adult": 2, "child": 1, "elderly": 1, "infant": 1}

        self.assertEqual(passenger_rate_sum(passengers, route_type="domestic"), 3.6)
        self.assertEqual(passenger_rate_sum(passengers, route_type="international"), 3.85)

    def test_price_in_scope_converts_from_single_storage_unit(self):
        passengers = {"adult": 2, "child": 1, "elderly": 0, "infant": 0}

        self.assertEqual(
            price_in_scope(1000, passengers, scope="all_passengers_roundtrip", route_type="domestic", round_trip=True),
            5000,
        )
        self.assertEqual(
            price_in_scope(1000, passengers, scope="per_person_roundtrip", route_type="domestic", round_trip=True),
            2000,
        )

    def test_budget_to_pp_and_price_in_scope_are_inverse(self):
        passengers = {"adult": 2, "child": 1, "elderly": 0, "infant": 0}
        per_person_oneway = 1234
        scoped = price_in_scope(
            per_person_oneway,
            passengers,
            scope="all_passengers_roundtrip",
            route_type="domestic",
            round_trip=True,
        )

        self.assertEqual(
            budget_to_pp(scoped, passengers, scope="all_passengers_roundtrip", route_type="domestic", round_trip=True),
            per_person_oneway,
        )

    def test_caliber_label_names_scope_and_passenger_factor(self):
        label = caliber_label(
            "all_passengers_roundtrip",
            {"adult": 2, "child": 1, "elderly": 0, "infant": 0},
            route_type="domestic",
        )

        self.assertIn("全员往返", label)
        self.assertIn("2.5", label)


if __name__ == "__main__":
    unittest.main()
