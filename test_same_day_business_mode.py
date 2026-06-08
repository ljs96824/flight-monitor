import unittest


class SameDayBusinessModeTest(unittest.TestCase):
    def test_build_same_day_combos_keeps_feasible_business_day(self):
        from analyzer import build_same_day_combos

        outbound = [
            {
                "flight_no": "MU5101",
                "price": 680,
                "departure_time": "08:10",
                "arrival_time": "10:25",
                "stops": 0,
            }
        ]
        returns = [
            {
                "flight_no": "MU5108",
                "price": 840,
                "departure_time": "18:40",
                "arrival_time": "20:55",
                "stops": 0,
            }
        ]

        combos = build_same_day_combos(outbound, returns, "2026-06-10")

        self.assertEqual(len(combos), 1)
        self.assertEqual(combos[0]["total_price"], 1520)
        self.assertGreaterEqual(combos[0]["stay_hours"], 4)
        self.assertEqual(combos[0]["tag"], "当天往返可行")

    def test_build_same_day_combos_filters_short_stay_and_late_outbound(self):
        from analyzer import build_same_day_combos

        outbound = [
            {"flight_no": "MU5101", "price": 680, "departure_time": "11:10", "arrival_time": "13:25"},
            {"flight_no": "MU5102", "price": 690, "departure_time": "08:00", "arrival_time": "14:30"},
        ]
        returns = [
            {"flight_no": "MU5108", "price": 840, "departure_time": "17:00", "arrival_time": "19:00"}
        ]

        combos = build_same_day_combos(outbound, returns, "2026-06-10")

        self.assertEqual(combos, [])

    def test_compute_same_day_windows_uses_business_time_transport_and_buffer(self):
        from analyzer import compute_same_day_windows

        windows = compute_same_day_windows(
            {
                "constraints": {
                    "business_start": "10:00",
                    "business_end": "16:00",
                    "buffer_hours": 2.5,
                    "transport_mode": "taxi",
                }
            },
            "PVG",
            "PKX",
        )

        self.assertEqual(windows["outbound_arrive_by"], "06:20")
        self.assertEqual(windows["return_depart_after"], "19:40")
        self.assertEqual(windows["transport_min"], 70)
        self.assertEqual(windows["buffer_h"], 2.5)

    def test_build_same_day_combos_uses_computed_business_window(self):
        from analyzer import build_same_day_combos, compute_same_day_windows

        windows = compute_same_day_windows(
            {
                "constraints": {
                    "business_start": "10:00",
                    "business_end": "16:00",
                    "buffer_hours": 2.5,
                    "transport_mode": "taxi",
                }
            },
            "PVG",
            "PKX",
        )
        outbound = [
            {"flight_no": "MU5101", "price": 680, "arrival_airport": "PKX", "departure_time": "04:00", "arrival_time": "06:10"},
            {"flight_no": "MU5102", "price": 500, "arrival_airport": "PKX", "departure_time": "07:00", "arrival_time": "08:30"},
        ]
        returns = [
            {"flight_no": "MU5108", "price": 840, "departure_airport": "PKX", "departure_time": "19:50", "arrival_time": "22:05"},
            {"flight_no": "MU5109", "price": 700, "departure_airport": "PKX", "departure_time": "18:20", "arrival_time": "20:35"},
        ]

        combos = build_same_day_combos(outbound, returns, windows, "2026-06-10")

        self.assertEqual(len(combos), 1)
        self.assertEqual(combos[0]["outbound"]["flight_no"], "MU5101")
        self.assertEqual(combos[0]["return"]["flight_no"], "MU5108")
        self.assertIn("06:10", combos[0]["schedule_note"])
        self.assertIn("19:50", combos[0]["schedule_note"])

    def test_same_day_no_feasible_note_explains_tight_schedule(self):
        from analyzer import _same_day_no_feasible_note

        note = _same_day_no_feasible_note(
            [{"flight_no": "MU5102", "arrival_airport": "PKX", "arrival_time": "08:30"}],
            [{"flight_no": "MU5109", "departure_airport": "PKX", "departure_time": "18:20"}],
            {
                "constraints": {
                    "business_start": "10:00",
                    "business_end": "16:00",
                    "buffer_hours": 2.5,
                    "transport_mode": "taxi",
                }
            },
        )

        self.assertIn("当天往返时间较紧", note)
        self.assertIn("要求不晚于06:20", note)
        self.assertIn("要求不早于19:40", note)

    def test_same_day_defaults_upgrade_business_profile(self):
        from analyzer import apply_default_rules

        sub = {
            "constraints": {"same_day_round_trip": True, "transfer_policy": "reasonable"},
            "preferences": {"travel_scenarios": ["tourism"]},
            "notification_goals": {},
        }

        normalized = apply_default_rules(sub)

        self.assertTrue(normalized["hard_constraints"]["same_day_round_trip"])
        self.assertIn("business", normalized["soft_preferences"]["travel_scenarios"])
        self.assertEqual(normalized["round_trip"], True)


if __name__ == "__main__":
    unittest.main()
