import sys
import types
import unittest


sys.modules.setdefault(
    "httpx",
    types.SimpleNamespace(get=lambda *args, **kwargs: None, post=lambda *args, **kwargs: None),
)


class AirportComparisonGuardTest(unittest.TestCase):
    def test_route_airports_string_does_not_crash_and_keeps_reference(self):
        from notifier import _active_airport_combo_count, _should_show_airport_comparison

        payload = {
            "route_airports": "SHA -> PEK",
            "airport_cost_comparison": {
                "rows": [{"airport": "PEK", "ticket_price": 680, "effective_cost": 910}]
            },
        }

        self.assertEqual(_active_airport_combo_count(payload), 0)
        self.assertTrue(_should_show_airport_comparison(payload))

    def test_active_airports_from_route_info_count_as_multiple_combos(self):
        from notifier import _active_airport_combo_count, _should_show_airport_comparison

        payload = {
            "route_info": {
                "origin_airports_active": ["PVG", "SHA"],
                "destination_airports_active": ["PEK", "PKX"],
            },
            "airport_cost_comparison": {
                "rows": [
                    {"airport": "PEK", "ticket_price": 680, "effective_cost": 910},
                    {"airport": "PKX", "ticket_price": 620, "effective_cost": 940},
                ]
            },
        }

        self.assertEqual(_active_airport_combo_count(payload), 4)
        self.assertTrue(_should_show_airport_comparison(payload))


if __name__ == "__main__":
    unittest.main()
