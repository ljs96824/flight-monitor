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
                    "business_end": "18:00",
                    "buffer_hours": 2.5,
                    "transport_mode": "taxi",
                }
            },
            "PVG",
            "PKX",
        )

        self.assertEqual(windows["outbound_arrive_by"], "07:30")
        self.assertEqual(windows["return_depart_after"], "20:30")
        self.assertEqual(windows["transport_min"], 70)
        self.assertEqual(windows["buffer_h"], 2.5)
        self.assertEqual(windows["reserve_minutes"], 150)

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

    def test_parse_flight_time_keeps_24_hour_clock(self):
        from analyzer import parse_flight_time

        parsed = parse_flight_time("2026-06-19 21:30")

        self.assertIsNotNone(parsed)
        self.assertEqual(parsed.hour, 21)
        self.assertEqual(parsed.minute, 30)

    def test_same_day_window_rejects_next_day_early_arrival(self):
        from analyzer import build_same_day_combos, compute_same_day_windows

        windows = compute_same_day_windows(
            {
                "constraints": {
                    "business_start": "10:00",
                    "business_end": "18:00",
                    "buffer_hours": 2.5,
                    "transport_mode": "taxi",
                }
            },
            "PVG",
            "PEK",
        )
        outbound = [
            {
                "flight_no": "CA999",
                "price": 400,
                "arrival_airport": "PEK",
                "departure_date": "2026-06-19",
                "arrival_date": "2026-06-20",
                "departure_time": "22:30",
                "arrival_time": "00:30",
            }
        ]
        returns = [
            {
                "flight_no": "CA1510",
                "price": 700,
                "departure_airport": "PEK",
                "departure_date": "2026-06-19",
                "arrival_date": "2026-06-19",
                "departure_time": "21:30",
                "arrival_time": "23:55",
            }
        ]

        combos = build_same_day_combos(outbound, returns, windows, "2026-06-19")

        self.assertEqual(combos, [])

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
        self.assertIn("要求不晚于07:30", note)
        self.assertIn("要求不早于18:30", note)

    def test_same_day_return_allows_late_evening_arrival_before_midnight(self):
        from analyzer import match_time_preference

        ok, note = match_time_preference(
            {"departure_time": "21:30", "arrival_time": "23:55"},
            {
                "time_preference_mode": "no_redeye",
                "same_day_round_trip": True,
                "direction": "return",
            },
        )

        self.assertTrue(ok)
        self.assertIn("返程晚班", note)

    def test_same_day_no_feasible_note_counts_relaxed_two_hour_candidates(self):
        from analyzer import _same_day_no_feasible_note

        note = _same_day_no_feasible_note(
            [
                {"flight_no": "CA1521", "arrival_airport": "PEK", "arrival_time": "08:00"},
                {"flight_no": "CA1523", "arrival_airport": "PEK", "arrival_time": "08:40"},
            ],
            [{"flight_no": "CA1510", "departure_airport": "PEK", "departure_time": "21:30"}],
            {
                "constraints": {
                    "business_start": "10:00",
                    "business_end": "18:00",
                    "buffer_hours": 2.5,
                    "transport_mode": "taxi",
                }
            },
        )

        self.assertIn("缩短预留至2小时", note)
        self.assertIn("有1个航班可选", note)
        self.assertIn("CA1521 08:00到，比要求晚30分钟", note)

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

    def test_analyze_round_trip_same_day_does_not_fallback_to_non_window_combo(self):
        from analyzer import analyze_round_trip

        constraints = {
            "same_day_round_trip": True,
            "business_start": "10:00",
            "business_end": "16:00",
            "buffer_hours": 2.5,
            "transport_mode": "taxi",
        }
        outbound = [
            {
                "flight_no": "MU5102",
                "flight_combo": "MU5102",
                "price": 500,
                "arrival_airport": "PKX",
                "departure_time": "07:00",
                "arrival_time": "08:30",
            }
        ]
        returns = [
            {
                "flight_no": "MU5109",
                "flight_combo": "MU5109",
                "price": 700,
                "departure_airport": "PKX",
                "departure_time": "18:20",
                "arrival_time": "20:35",
            }
        ]

        result = analyze_round_trip(
            {
                "all_flights": outbound,
                "hard_constraints": constraints,
                "days_to_dept": 10,
            },
            {"all_flights": returns, "hard_constraints": constraints},
        )

        self.assertEqual(result["top_combinations"], [])
        self.assertIn("当天往返时间较紧", result["same_day_no_feasible_note"])


    def test_analyze_round_trip_same_day_uses_all_candidates_before_recommendation(self):
        from analyzer import analyze_round_trip

        constraints = {
            "same_day_round_trip": True,
            "business_start": "10:00",
            "business_end": "16:00",
            "buffer_hours": 2.5,
            "transport_mode": "taxi",
        }
        late_price_pick = {
            "flight_no": "HU7612",
            "flight_combo": "HU7612",
            "price": 300,
            "arrival_airport": "PEK",
            "departure_date": "2026-06-19",
            "arrival_date": "2026-06-19",
            "departure_time": "21:30",
            "arrival_time": "23:55",
        }
        feasible = {
            "flight_no": "MU5099",
            "flight_combo": "MU5099",
            "price": 900,
            "arrival_airport": "PEK",
            "departure_date": "2026-06-19",
            "arrival_date": "2026-06-19",
            "departure_time": "05:00",
            "arrival_time": "07:15",
        }
        returns = [
            {
                "flight_no": "CA1510",
                "flight_combo": "CA1510",
                "price": 700,
                "departure_airport": "PEK",
                "departure_date": "2026-06-19",
                "arrival_date": "2026-06-19",
                "departure_time": "19:00",
                "arrival_time": "21:20",
            }
        ]

        result = analyze_round_trip(
            {
                "economy_recommendations": [late_price_pick],
                "all_flights": [late_price_pick, feasible],
                "hard_constraints": constraints,
                "depart_date": "2026-06-19",
            },
            {
                "all_flights": returns,
                "hard_constraints": constraints,
                "depart_date": "2026-06-19",
            },
        )

        self.assertEqual(result["top_combinations"][0]["outbound"]["flight_no"], "MU5099")

    def test_analyze_round_trip_same_day_time_conflict_lists_closest_by_arrival(self):
        from analyzer import analyze_round_trip

        constraints = {
            "same_day_round_trip": True,
            "business_start": "10:00",
            "business_end": "16:00",
            "buffer_hours": 2.5,
            "transport_mode": "taxi",
        }
        outbound = [
            {
                "flight_no": "CA1510",
                "flight_combo": "CA1510",
                "price": 300,
                "arrival_airport": "PEK",
                "departure_date": "2026-06-19",
                "arrival_date": "2026-06-19",
                "departure_time": "21:30",
                "arrival_time": "23:55",
            },
            {
                "flight_no": "MU5099",
                "flight_combo": "MU5099",
                "price": 900,
                "arrival_airport": "PEK",
                "departure_date": "2026-06-19",
                "arrival_date": "2026-06-19",
                "departure_time": "07:00",
                "arrival_time": "09:15",
            },
            {
                "flight_no": "HO1001",
                "flight_combo": "HO1001",
                "price": 800,
                "arrival_airport": "PEK",
                "departure_date": "2026-06-19",
                "arrival_date": "2026-06-19",
                "departure_time": "08:00",
                "arrival_time": "10:20",
            },
        ]
        returns = [
            {
                "flight_no": "CA1511",
                "flight_combo": "CA1511",
                "price": 700,
                "departure_airport": "PEK",
                "departure_date": "2026-06-19",
                "arrival_date": "2026-06-19",
                "departure_time": "19:00",
                "arrival_time": "21:20",
            }
        ]

        result = analyze_round_trip(
            {
                "all_flights": outbound,
                "hard_constraints": constraints,
                "depart_date": "2026-06-19",
            },
            {
                "all_flights": returns,
                "hard_constraints": constraints,
                "depart_date": "2026-06-19",
            },
        )

        self.assertTrue(result["same_day_time_conflict"])
        self.assertEqual(result["top_combinations"], [])
        self.assertEqual(result["closest_same_day_outbound_options"][0]["flight_no"], "MU5099")
        self.assertNotEqual(result["closest_same_day_outbound_options"][0]["flight_no"], "CA1510")

    def test_determine_push_type_reports_same_day_time_conflict(self):
        from analyzer import determine_push_type

        push = determine_push_type(
            1000,
            analysis_result={
                "round_trip_analysis": {
                    "same_day_time_conflict": True,
                    "top_combinations": [],
                }
            },
        )

        self.assertEqual(push["type"], "时间冲突提示")

    def test_same_day_meeting_skips_generic_redeye_time_filters(self):
        from analyzer import _apply_user_preferences

        flight = {
            "flight_no": "MU5099",
            "flight_combo": "MU5099",
            "price": 900,
            "stops": 0,
            "departure_time": "05:00",
            "arrival_time": "07:15",
            "total_duration_min": 135,
        }

        kept, excluded, _ = _apply_user_preferences(
            [flight],
            {
                "same_day_round_trip": True,
                "business_start": "10:00",
                "business_end": "16:00",
                "time_preference_mode": "no_redeye",
                "departure_time_policy": "no_redeye",
                "arrival_time_policy": "daytime_only",
                "red_eye": "reject",
                "preferred_departure_slots": ["morning"],
                "direction": "outbound",
            },
        )

        self.assertEqual([item["flight_no"] for item in kept], ["MU5099"])
        self.assertEqual(excluded, [])

    def test_same_day_meeting_marks_time_source_as_meeting_derived(self):
        from analyzer import apply_default_rules

        sub = apply_default_rules(
            {
                "monitor_mode": "precise",
                "constraints": {
                    "same_day_round_trip": True,
                    "business_start": "10:00",
                    "business_end": "16:00",
                    "buffer_hours": 2.5,
                },
                "preferences": {
                    "time_pref": "custom",
                    "time_windows": {
                        "departure": [["09:00", "12:00"]],
                        "arrival": [["09:00", "12:00"]],
                    },
                },
                "notification_goals": {},
            }
        )

        self.assertEqual(sub["constraints"]["time_source"], "meeting_derived")
        self.assertEqual(sub["hard_constraints"]["time_source"], "meeting_derived")
        self.assertTrue(
            any("会议模式接管时间设置" in item for item in sub["defaults_applied"])
        )


if __name__ == "__main__":
    unittest.main()
