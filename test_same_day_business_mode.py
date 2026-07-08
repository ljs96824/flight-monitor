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

    def test_default_same_day_picks_reasonable_business_window_not_extremes(self):
        from analyzer import build_same_day_combos

        outbound = [
            {"flight_no": "EXTREME_EARLY", "price": 100, "departure_time": "05:20", "arrival_time": "07:10"},
            {"flight_no": "GOOD_OB", "price": 500, "departure_time": "07:20", "arrival_time": "09:30"},
            {"flight_no": "LATE_OB", "price": 300, "departure_time": "10:10", "arrival_time": "12:30"},
        ]
        returns = [
            {"flight_no": "EARLY_RT", "price": 100, "departure_time": "15:00", "arrival_time": "17:10"},
            {"flight_no": "GOOD_RT", "price": 600, "departure_time": "17:30", "arrival_time": "19:40"},
            {"flight_no": "EXTREME_LATE", "price": 200, "departure_time": "22:40", "arrival_time": "23:55"},
        ]

        combos = build_same_day_combos(
            outbound,
            returns,
            "2026-06-10",
            constraints={"day_trip_period": "morning"},
        )

        self.assertGreaterEqual(combos[0]["stay_hours"], 5)
        self.assertEqual(combos[0]["outbound"]["flight_no"], "GOOD_OB")
        self.assertEqual(combos[0]["return"]["flight_no"], "GOOD_RT")
        self.assertIn("默认方案", combos[0]["schedule_note"])

    def test_default_same_day_period_changes_return_preference(self):
        from analyzer import build_same_day_combos

        outbound = [
            {"flight_no": "OB", "price": 500, "departure_time": "08:20", "arrival_time": "10:30"},
        ]
        returns = [
            {"flight_no": "AFTERNOON_RT", "price": 800, "departure_time": "16:30", "arrival_time": "18:30"},
            {"flight_no": "EVENING_RT", "price": 850, "departure_time": "19:30", "arrival_time": "21:30"},
        ]

        morning = build_same_day_combos(
            outbound,
            returns,
            "2026-06-10",
            constraints={"day_trip_period": "morning"},
        )
        afternoon = build_same_day_combos(
            outbound,
            returns,
            "2026-06-10",
            constraints={"day_trip_period": "afternoon"},
        )

        self.assertEqual(morning[0]["return"]["flight_no"], "AFTERNOON_RT")
        self.assertEqual(afternoon[0]["return"]["flight_no"], "EVENING_RT")

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

    def test_meeting_location_near_pkx_allows_normal_morning_arrival(self):
        from analyzer import build_same_day_combos, compute_same_day_windows

        constraints = {
            "same_day_round_trip": True,
            "business_start": "10:30",
            "business_end": "16:30",
            "meeting_location": "\u5317\u4eac\u5e02\u5927\u5174\u533a",
            "meeting_importance": "normal",
            "route_type": "domestic",
        }
        windows = compute_same_day_windows({"constraints": constraints}, "SHA", "PKX")

        self.assertEqual(windows["buffer_model"], "meeting_fixed")
        self.assertLessEqual(windows["destination_transport_min"], 30)
        self.assertEqual(windows["outbound_arrive_by"], "09:25")

        outbound = [
            {
                "flight_no": "MU5099",
                "price": 795,
                "departure_airport": "SHA",
                "arrival_airport": "PKX",
                "departure_time": "07:00",
                "arrival_time": "08:20",
                "departure_date": "2026-06-19",
                "arrival_date": "2026-06-19",
            }
        ]
        returns = [
            {
                "flight_no": "CA1589",
                "price": 1350,
                "departure_airport": "PKX",
                "arrival_airport": "SHA",
                "departure_time": "20:30",
                "arrival_time": "22:40",
                "departure_date": "2026-06-19",
                "arrival_date": "2026-06-19",
            }
        ]
        combos = build_same_day_combos(
            outbound,
            returns,
            windows,
            "2026-06-19",
            constraints=constraints,
        )

        self.assertEqual(len(combos), 1)
        self.assertEqual(combos[0]["outbound"]["flight_no"], "MU5099")
        self.assertEqual(combos[0]["return"]["flight_no"], "CA1589")

    def test_meeting_fixed_breakdown_items_sum_to_total(self):
        from analyzer import compute_same_day_windows

        constraints = {
            "same_day_round_trip": True,
            "business_start": "10:30",
            "business_end": "17:00",
            "meeting_location": "\u5317\u4eac\u5e02\u5927\u5174\u533a",
            "meeting_importance": "important",
            "checked_baggage_required": True,
            "route_type": "domestic",
        }
        windows = compute_same_day_windows({"constraints": constraints}, "SHA", "PKX")
        outbound = windows["reserve_breakdown"]["outbound"]
        parts = [
            outbound["arrival_exit_min"],
            outbound["destination_transport_min"],
            outbound["destination_transport_margin_min"],
            outbound["delay_buffer_min"],
            outbound["pre_meeting_buffer_min"],
            outbound["safety_min"],
            outbound["custom_redundancy_min"],
        ]

        self.assertEqual(sum(parts), outbound["total_min"])
        self.assertEqual(outbound["itemized_total_min"], outbound["total_min"])
        self.assertEqual(outbound["arrival_exit_min"], 45)
        self.assertEqual(outbound["destination_transport_margin_min"], 15)
        self.assertEqual(outbound["delay_buffer_min"], 20)
        self.assertEqual(outbound["pre_meeting_buffer_min"], 30)
        self.assertEqual(outbound["total_min"], 150)
        self.assertEqual(windows["outbound_arrive_by"], "08:00")

    def test_unknown_meeting_location_uses_labeled_conservative_default(self):
        from analyzer import compute_same_day_windows

        constraints = {
            "same_day_round_trip": True,
            "business_start": "10:30",
            "business_end": "17:00",
            "meeting_importance": "normal",
            "checked_baggage_required": True,
            "route_type": "domestic",
        }
        windows = compute_same_day_windows({"constraints": constraints}, "SHA", "PKX")
        outbound = windows["reserve_breakdown"]["outbound"]

        self.assertEqual(outbound["destination_transport_source"], "\u672a\u586b\u4f1a\u8bae\u5730\u70b9,\u6309\u4fdd\u5b88\u4f30\u7b97")
        self.assertEqual(outbound["location_confidence"], "unknown")
        self.assertGreaterEqual(windows["outbound_arrive_by_minutes"], 9 * 60 + 15)

    def test_same_day_combos_use_per_airport_windows_and_prefer_closer_airport(self):
        from analyzer import build_same_day_combos, compute_same_day_windows

        constraints = {
            "same_day_round_trip": True,
            "business_start": "10:30",
            "business_end": "17:00",
            "meeting_location": "\u5317\u4eac\u5e02\u5927\u5174\u533a",
            "meeting_importance": "normal",
            "route_type": "domestic",
        }
        sample_windows = compute_same_day_windows({"constraints": constraints}, "SHA", "PEK")
        outbound = [
            {
                "flight_no": "PEK_EARLIER",
                "price": 800,
                "departure_airport": "SHA",
                "arrival_airport": "PEK",
                "departure_time": "05:30",
                "arrival_time": "07:49",
                "departure_date": "2026-06-19",
                "arrival_date": "2026-06-19",
            },
            {
                "flight_no": "PKX_SAME_TIME",
                "price": 800,
                "departure_airport": "SHA",
                "arrival_airport": "PKX",
                "departure_time": "06:30",
                "arrival_time": "08:00",
                "departure_date": "2026-06-19",
                "arrival_date": "2026-06-19",
            },
        ]
        returns = [
            {
                "flight_no": "PEK_RETURN",
                "price": 900,
                "departure_airport": "PEK",
                "arrival_airport": "SHA",
                "departure_time": "20:30",
                "arrival_time": "23:00",
                "departure_date": "2026-06-19",
                "arrival_date": "2026-06-19",
            },
            {
                "flight_no": "PKX_RETURN",
                "price": 900,
                "departure_airport": "PKX",
                "arrival_airport": "SHA",
                "departure_time": "20:30",
                "arrival_time": "23:00",
                "departure_date": "2026-06-19",
                "arrival_date": "2026-06-19",
            },
        ]

        combos = build_same_day_combos(
            outbound,
            returns,
            sample_windows,
            "2026-06-19",
            constraints=constraints,
        )

        self.assertGreaterEqual(len(combos), 1)
        self.assertEqual(combos[0]["outbound"]["arrival_airport"], "PKX")
        self.assertEqual(combos[0]["return"]["departure_airport"], "PKX")
        self.assertLess(
            combos[0]["outbound_destination_transport_min"],
            next(
                item["outbound_destination_transport_min"]
                for item in combos
                if item["outbound"]["arrival_airport"] == "PEK"
            ),
        )

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

    def test_compute_same_day_windows_uses_airport_buffer_transport_and_redundancy(self):
        from analyzer import compute_same_day_windows

        windows = compute_same_day_windows(
            {
                "constraints": {
                    "business_start": "10:00",
                    "business_end": "16:00",
                    "user_transport_min": 60,
                    "redundancy_min": 25,
                }
            },
            "SHA",
            "PKX",
        )

        self.assertEqual(windows["buffer_model"], "airport_split")
        self.assertEqual(windows["arrival_buffer_min"], 120)
        self.assertEqual(windows["checkin_buffer_min"], 110)
        self.assertEqual(windows["transport_min"], 60)
        self.assertEqual(windows["redundancy_min"], 25)
        self.assertEqual(windows["outbound_transport_margin_min"], 24)
        self.assertTrue(windows["outbound_transport_rush"])
        self.assertEqual(windows["return_transport_margin_min"], 18)
        self.assertFalse(windows["return_transport_rush"])
        self.assertEqual(windows["outbound_reserve_minutes"], 229)
        self.assertEqual(windows["return_reserve_minutes"], 213)
        self.assertEqual(windows["outbound_arrive_by"], "06:11")
        self.assertEqual(windows["return_depart_after"], "19:33")
        self.assertEqual(windows["reserve_breakdown"]["outbound"]["total_min"], windows["outbound_reserve_minutes"])
        self.assertEqual(windows["reserve_breakdown"]["return"]["total_min"], windows["return_reserve_minutes"])
        self.assertEqual(windows["reserve_breakdown"]["outbound"]["transport_source"], "用户填写")
        self.assertEqual(windows["reserve_breakdown"]["windows"]["arrive_by"], windows["outbound_arrive_by"])

    def test_same_day_windows_legacy_buffer_has_single_breakdown_source(self):
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

        self.assertTrue(windows["reserve_breakdown"]["legacy"])
        self.assertEqual(windows["reserve_breakdown"]["outbound"]["total_min"], windows["reserve_minutes"])
        self.assertEqual(windows["reserve_breakdown"]["return"]["total_min"], windows["reserve_minutes"])

    def test_transport_margin_uses_ratio_rush_hour_and_minimum(self):
        from analyzer import calc_transport_margin

        margin, ratio, rush = calc_transport_margin(60, "standard")
        self.assertEqual(margin, 18)
        self.assertEqual(ratio, 0.30)
        self.assertFalse(rush)

        rush_margin, rush_ratio, rush = calc_transport_margin(60, "standard", travel_hour=8)
        self.assertEqual(rush_margin, 24)
        self.assertEqual(rush_ratio, 0.40)
        self.assertTrue(rush)

        small_margin, _, _ = calc_transport_margin(20, "standard")
        self.assertEqual(small_margin, 15)

    def test_route_type_airport_buffers_include_border_processing(self):
        from airport_logistics import get_arrival_buffer, get_departure_buffer

        self.assertEqual(get_departure_buffer("PVG", "domestic"), 110)
        self.assertEqual(get_departure_buffer("PVG", "international"), 180)
        self.assertEqual(get_departure_buffer("HKG", "greater_china"), 120)
        self.assertEqual(get_arrival_buffer("PVG", "domestic"), 120)
        self.assertEqual(get_arrival_buffer("PVG", "international"), 170)
        self.assertEqual(get_arrival_buffer("HKG", "greater_china"), 100)

    def test_analyze_departure_feasibility_labels_feasible_tight_and_impossible(self):
        from analyzer import analyze_departure_feasibility

        flight = {
            "flight_no": "CA987",
            "departure_airport": "PVG",
            "departure_time": "2026-06-10 19:30",
        }

        feasible = analyze_departure_feasibility("14:00", flight, "international", 60, "standard", "2026-06-10")
        self.assertEqual(feasible["level"], "可行")
        self.assertEqual(feasible["margin_min"], 47)
        self.assertEqual(feasible["total_reserve"], 283)
        self.assertIn("出境边检海关", feasible["buffer_label"])

        tight = analyze_departure_feasibility("14:00", flight, "international", 90, "standard", "2026-06-10")
        self.assertEqual(tight["level"], "紧张")
        self.assertEqual(tight["margin_min"], 8)

        impossible = analyze_departure_feasibility("15:00", flight, "international", 90, "standard", "2026-06-10")
        self.assertEqual(impossible["level"], "不可行")
        self.assertEqual(impossible["short_min"], 52)
        self.assertEqual(impossible["need_set_off"], "14:08")

    def test_compute_same_day_windows_adds_transport_margin(self):
        from analyzer import compute_same_day_windows

        windows = compute_same_day_windows(
            {
                "constraints": {
                    "business_start": "09:00",
                    "business_end": "16:00",
                    "user_transport_min": 60,
                    "redundancy_min": 25,
                    "transport_margin_mode": "standard",
                }
            },
            "SHA",
            "PKX",
        )

        self.assertEqual(windows["outbound_transport_margin_min"], 24)
        self.assertEqual(windows["outbound_transport_margin_ratio"], 0.40)
        self.assertTrue(windows["outbound_transport_rush"])
        self.assertEqual(windows["return_transport_margin_min"], 18)
        self.assertFalse(windows["return_transport_rush"])
        self.assertEqual(windows["outbound_reserve_minutes"], 229)
        self.assertEqual(windows["outbound_arrive_by"], "05:11")

    def test_city_airport_uses_shorter_standard_buffer(self):
        from analyzer import compute_same_day_windows

        sha = compute_same_day_windows(
            {
                "constraints": {
                    "business_start": "10:00",
                    "business_end": "16:00",
                    "user_transport_min": 60,
                    "redundancy_min": 25,
                }
            },
            "PVG",
            "SHA",
        )
        pvg = compute_same_day_windows(
            {
                "constraints": {
                    "business_start": "10:00",
                    "business_end": "16:00",
                    "user_transport_min": 60,
                    "redundancy_min": 25,
                }
            },
            "SHA",
            "PVG",
        )

        self.assertLess(sha["arrival_buffer_min"], pvg["arrival_buffer_min"])
        self.assertGreater(sha["outbound_arrive_by_minutes"], pvg["outbound_arrive_by_minutes"])

    def test_build_same_day_alternatives_for_time_conflict(self):
        from analyzer import build_same_day_alternatives, compute_same_day_windows

        windows = compute_same_day_windows(
            {
                "constraints": {
                    "business_start": "10:00",
                    "business_end": "16:00",
                    "user_transport_min": 60,
                    "redundancy_min": 25,
                }
            },
            "SHA",
            "PEK",
        )
        current_day = [
            {
                "flight_no": "MU5099",
                "price": 894,
                "departure_airport": "SHA",
                "arrival_airport": "PEK",
                "departure_date": "2026-06-19",
                "arrival_date": "2026-06-19",
                "departure_time": "07:00",
                "arrival_time": "09:15",
            }
        ]
        previous_day = [
            {
                "flight_no": "MU5137",
                "price": 620,
                "departure_airport": "SHA",
                "arrival_airport": "PEK",
                "departure_date": "2026-06-18",
                "arrival_date": "2026-06-18",
                "departure_time": "19:00",
                "arrival_time": "21:15",
            },
            {
                "flight_no": "HU7610",
                "price": 520,
                "departure_airport": "SHA",
                "arrival_airport": "PEK",
                "departure_date": "2026-06-18",
                "arrival_date": "2026-06-19",
                "departure_time": "22:30",
                "arrival_time": "00:40",
            },
        ]

        alternatives = build_same_day_alternatives(
            current_day,
            [],
            windows,
            "2026-06-19",
            previous_day_outbound=previous_day,
        )

        categories = [item["category"] for item in alternatives]
        self.assertIn("previous_evening", categories)
        self.assertIn("previous_redeye", categories)
        self.assertIn("same_day_earliest", categories)
        self.assertEqual(
            next(item for item in alternatives if item["category"] == "same_day_earliest")["flight"]["flight_no"],
            "MU5099",
        )

    def test_build_same_day_alternatives_keep_roundtrip_return_and_price(self):
        from analyzer import build_same_day_alternatives, compute_same_day_windows

        windows = compute_same_day_windows(
            {
                "constraints": {
                    "same_day_round_trip": True,
                    "business_start": "10:00",
                    "business_end": "17:00",
                    "meeting_importance": "important",
                    "destination_transport_min": 50,
                }
            },
            "SHA",
            "PEK",
        )
        current_day = [
            {
                "flight_no": "MU5099",
                "price": 895,
                "departure_airport": "SHA",
                "arrival_airport": "PEK",
                "departure_date": "2026-06-19",
                "arrival_date": "2026-06-19",
                "departure_time": "07:00",
                "arrival_time": "09:15",
            }
        ]
        returns = [
            {
                "flight_no": "CA1589",
                "price": 1350,
                "departure_airport": "PEK",
                "arrival_airport": "SHA",
                "departure_date": "2026-06-19",
                "arrival_date": "2026-06-19",
                "departure_time": "21:30",
                "arrival_time": "23:30",
            }
        ]
        previous_day = [
            {
                "flight_no": "MU5137",
                "price": 620,
                "departure_airport": "SHA",
                "arrival_airport": "PEK",
                "departure_date": "2026-06-18",
                "arrival_date": "2026-06-18",
                "departure_time": "19:00",
                "arrival_time": "21:15",
            }
        ]

        alternatives = build_same_day_alternatives(
            current_day,
            returns,
            windows,
            "2026-06-19",
            previous_day_outbound=previous_day,
        )

        previous = next(item for item in alternatives if item["category"] == "previous_evening")
        earliest = next(item for item in alternatives if item["category"] == "same_day_earliest")
        self.assertEqual(previous["outbound"]["flight_no"], "MU5137")
        self.assertEqual(previous["return"]["flight_no"], "CA1589")
        self.assertEqual(previous["roundtrip_price"], 1970)
        self.assertEqual(previous["price"], 1970)
        self.assertEqual(earliest["outbound"]["flight_no"], "MU5099")
        self.assertEqual(earliest["return"]["flight_no"], "CA1589")
        self.assertEqual(earliest["roundtrip_price"], 2245)

    def test_same_day_alternatives_choose_cheapest_return_within_window_and_mark_budget_overage(self):
        from analyzer import build_same_day_alternatives, compute_same_day_windows

        windows = compute_same_day_windows(
            {
                "constraints": {
                    "same_day_round_trip": True,
                    "business_start": "10:30",
                    "business_end": "17:00",
                    "meeting_importance": "normal",
                    "destination_transport_min": 25,
                }
            },
            "SHA",
            "PKX",
        )
        current_day = [
            {
                "flight_no": "MU5099",
                "price": 831,
                "departure_airport": "SHA",
                "arrival_airport": "PKX",
                "departure_date": "2026-06-26",
                "arrival_date": "2026-06-26",
                "departure_time": "07:00",
                "arrival_time": "09:15",
            }
        ]
        returns = [
            {
                "flight_no": "MU5128",
                "price": 1921,
                "departure_airport": "PKX",
                "arrival_airport": "SHA",
                "departure_date": "2026-06-26",
                "arrival_date": "2026-06-26",
                "departure_time": "20:10",
                "arrival_time": "22:20",
            },
            {
                "flight_no": "MU5170",
                "price": 1720,
                "departure_airport": "PKX",
                "arrival_airport": "SHA",
                "departure_date": "2026-06-26",
                "arrival_date": "2026-06-26",
                "departure_time": "21:00",
                "arrival_time": "23:10",
            },
        ]

        alternatives = build_same_day_alternatives(
            current_day,
            returns,
            windows,
            "2026-06-26",
            max_budget=1600,
        )

        earliest = next(item for item in alternatives if item["category"] == "same_day_earliest")
        self.assertEqual(earliest["return"]["flight_no"], "MU5170")
        self.assertEqual(earliest["return_price"], 1720)
        self.assertLessEqual(earliest["return_price"], 1921)
        self.assertEqual(earliest["adult_roundtrip_price"], 2551)
        self.assertTrue(earliest["over_budget"])
        self.assertEqual(earliest["budget_overage"], 951)
        self.assertEqual(earliest["budget_scope_label"], "单人往返 vs 上限1600")
    def test_build_same_day_combos_uses_computed_business_window(self):
        from analyzer import build_same_day_combos, compute_same_day_windows

        windows = compute_same_day_windows(
            {
                "constraints": {
                    "business_start": "10:00",
                    "business_end": "16:00",
                    "depart_date": "2026-06-19",
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

    def test_meeting_fixed_combos_sort_by_business_safety_before_price(self):
        from analyzer import build_same_day_combos, compute_same_day_windows

        windows = compute_same_day_windows(
            {
                "constraints": {
                    "business_start": "10:00",
                    "business_end": "17:00",
                    "meeting_importance": "important",
                    "destination_transport_min": 50,
                }
            },
            "SHA",
            "PEK",
        )
        outbound = [
            {"flight_no": "TIGHT_OB", "price": 100, "arrival_airport": "PEK", "departure_time": "06:30", "arrival_time": "06:50"},
            {"flight_no": "SAFE_OB", "price": 500, "arrival_airport": "PEK", "departure_time": "04:30", "arrival_time": "05:00"},
        ]
        returns = [
            {"flight_no": "RT", "price": 700, "departure_airport": "PEK", "departure_time": "22:00", "arrival_time": "23:55"},
        ]

        combos = build_same_day_combos(outbound, returns, windows, "2026-06-10")

        self.assertEqual(combos[0]["outbound"]["flight_no"], "SAFE_OB")
        self.assertEqual(combos[0]["business_feasibility"]["outbound"]["level"], "稳妥可行")
        self.assertEqual(combos[1]["business_feasibility"]["outbound"]["level"], "高风险卡点")
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
            [{"flight_no": "MU5102", "arrival_airport": "PKX", "arrival_time": "08:30", "departure_time": "06:30"}],
            [{"flight_no": "MU5109", "departure_airport": "PKX", "departure_time": "18:20"}],
            {
                "constraints": {
                    "business_start": "10:00",
                    "business_end": "16:00",
                    "depart_date": "2026-06-19",
                    "buffer_hours": 2.5,
                    "transport_mode": "taxi",
                }
            },
        )

        self.assertTrue(note.startswith("本次无方案主因是【去程时间】"))
        self.assertIn("最早MU5102 08:30到", note)
        self.assertIn("需07:30前落地", note)
        self.assertIn("晚1h0m", note)

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

    def test_same_day_no_feasible_note_names_empty_return_window(self):
        from analyzer import _same_day_no_feasible_note

        note = _same_day_no_feasible_note(
            [{"flight_no": "MU5099", "arrival_airport": "PEK", "departure_time": "05:00", "arrival_time": "06:10"}],
            [{"flight_no": "CA1507", "departure_airport": "PEK", "departure_time": "18:00"}],
            {
                "same_day_round_trip": True,
                "depart_date": "2026-06-19",
                "business_start": "10:00",
                "business_end": "17:00",
                "meeting_importance": "important",
                "destination_transport_min": 50,
            },
        )

        self.assertTrue(note.startswith("本次无方案主因是【返程时间】"))
        self.assertIn("去程可赶到", note)
        self.assertIn("当天没有符合返程窗口", note)
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
                    "depart_date": "2026-06-19",
                    "buffer_hours": 2.5,
                    "transport_mode": "taxi",
                }
            },
        )

        self.assertTrue(note.startswith("本次无方案主因是【去程时间】"))
        self.assertIn("最早CA1521 08:00到", note)
        self.assertIn("需07:30前落地", note)
        self.assertIn("晚30分钟", note)

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
            "depart_date": "2026-06-19",
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
        self.assertIn("\u672c\u6b21\u65e0\u65b9\u6848\u4e3b\u56e0\u662f\u3010\u8fd4\u7a0b\u65f6\u95f4\u3011", result["same_day_no_feasible_note"])


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

    def test_same_day_earliest_alternative_uses_raw_valid_pool_not_price_display_pool(self):
        from analyzer import analyze_round_trip

        constraints = {
            "same_day_round_trip": True,
            "business_start": "10:00",
            "business_end": "16:00",
            "buffer_hours": 2.5,
            "transport_mode": "taxi",
        }
        afternoon_price_pick = {
            "flight_no": "CA1510",
            "flight_combo": "CA1510",
            "price": 300,
            "arrival_airport": "PEK",
            "departure_date": "2026-06-19",
            "arrival_date": "2026-06-19",
            "departure_time": "14:30",
            "arrival_time": "16:55",
        }
        real_earliest = {
            "flight_no": "MU5099",
            "flight_combo": "MU5099",
            "price": 900,
            "arrival_airport": "PEK",
            "departure_date": "2026-06-19",
            "arrival_date": "2026-06-19",
            "departure_time": "07:00",
            "arrival_time": "09:15",
        }
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
                "all_flights": [afternoon_price_pick],
                "same_day_base_flights": [afternoon_price_pick, real_earliest],
                "hard_constraints": constraints,
                "depart_date": "2026-06-19",
            },
            {
                "all_flights": returns,
                "same_day_base_flights": returns,
                "hard_constraints": constraints,
                "depart_date": "2026-06-19",
            },
        )

        earliest = next(
            item for item in result["same_day_alternatives"] if item["category"] == "same_day_earliest"
        )
        self.assertEqual(earliest["flight"]["flight_no"], "MU5099")
        self.assertNotEqual(earliest["flight"]["flight_no"], "CA1510")

    def test_pick_earliest_same_day_sorts_by_parsed_datetime_not_raw_string(self):
        from analyzer import pick_earliest_same_day

        picked = pick_earliest_same_day(
            [
                {
                    "flight_no": "PM999",
                    "departure_date": "2026-06-19",
                    "arrival_date": "2026-06-19",
                    "departure_time": "2026-06-19 14:30",
                    "arrival_time": "2026-06-19 16:55",
                    "price": 300,
                },
                {
                    "flight_no": "AM001",
                    "departure_date": "2026-06-19",
                    "arrival_date": "2026-06-19",
                    "departure_time": "07:00",
                    "arrival_time": "09:15",
                    "price": 900,
                },
            ],
            "2026-06-19",
        )

        self.assertEqual(picked["flight_no"], "AM001")

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
                    "depart_date": "2026-06-19",
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


    def test_same_day_roundtrip_analysis_uses_actual_combo_total_and_roundtrip_budget(self):
        from analyzer import analyze_round_trip

        outbound = [
            {
                "flight_no": "OB_CHEAP_BUT_LATE",
                "flight_combo": "OB_CHEAP_BUT_LATE",
                "price": 100,
                "departure_airport": "SHA",
                "arrival_airport": "PEK",
                "departure_time": "2026-06-18 13:00",
                "arrival_time": "2026-06-18 15:00",
                "departure_date": "2026-06-18",
                "arrival_date": "2026-06-18",
                "stops": 0,
            },
            {
                "flight_no": "OB_OK",
                "flight_combo": "OB_OK",
                "price": 700,
                "departure_airport": "SHA",
                "arrival_airport": "PEK",
                "departure_time": "2026-06-18 06:30",
                "arrival_time": "2026-06-18 08:00",
                "departure_date": "2026-06-18",
                "arrival_date": "2026-06-18",
                "stops": 0,
            },
        ]
        returns = [
            {
                "flight_no": "RT_CHEAP_BUT_EARLY",
                "flight_combo": "RT_CHEAP_BUT_EARLY",
                "price": 100,
                "departure_airport": "PEK",
                "arrival_airport": "SHA",
                "departure_time": "2026-06-18 12:00",
                "arrival_time": "2026-06-18 14:00",
                "departure_date": "2026-06-18",
                "arrival_date": "2026-06-18",
                "stops": 0,
            },
            {
                "flight_no": "RT_OK",
                "flight_combo": "RT_OK",
                "price": 900,
                "departure_airport": "PEK",
                "arrival_airport": "SHA",
                "departure_time": "2026-06-18 19:00",
                "arrival_time": "2026-06-18 21:00",
                "departure_date": "2026-06-18",
                "arrival_date": "2026-06-18",
                "stops": 0,
            },
        ]
        constraints = {
            "same_day_round_trip": True,
            "business_start": "10:00",
            "business_end": "16:00",
            "buffer_hours": 1,
        }

        result = analyze_round_trip(
            {
                "all_flights": outbound,
                "hard_constraints": constraints,
                "depart_date": "2026-06-18",
            },
            {
                "all_flights": returns,
                "hard_constraints": constraints,
                "depart_date": "2026-06-18",
            },
            target_price=1600,
            max_budget=2200,
        )

        self.assertFalse(result["same_day_time_conflict"])
        self.assertEqual(result["top_combinations"][0]["outbound"]["flight_no"], "OB_OK")
        self.assertEqual(result["top_combinations"][0]["return"]["flight_no"], "RT_OK")
        self.assertEqual(result["total_min"], 1600)
        self.assertNotIn("4,400", str(result["decision_summary"]))
        self.assertNotIn("4,400", result["advice"])


    def test_important_meeting_fixed_buffer_model_has_single_breakdown_source(self):
        from analyzer import compute_same_day_windows

        windows = compute_same_day_windows(
            {
                "constraints": {
                    "same_day_round_trip": True,
                    "business_start": "10:00",
                    "business_end": "17:00",
                    "meeting_importance": "important",
                    "destination_transport_min": 50,
                    "checked_baggage_required": False,
                }
            },
            "SHA",
            "PEK",
        )

        outbound = windows["reserve_breakdown"]["outbound"]
        self.assertEqual(windows["buffer_model"], "meeting_fixed")
        self.assertEqual(outbound["arrival_exit_min"], 35)
        self.assertEqual(outbound["destination_transport_min"], 50)
        self.assertEqual(outbound["destination_transport_margin_min"], 20)
        self.assertEqual(outbound["delay_buffer_min"], 20)
        self.assertEqual(outbound["pre_meeting_buffer_min"], 30)
        self.assertEqual(outbound["safety_min"], 15)
        self.assertEqual(outbound["total_min"], 170)
        self.assertEqual(windows["outbound_reserve_minutes"], 170)
        self.assertEqual(windows["outbound_arrive_by"], "07:10")
        self.assertEqual(windows["business_safety_arrive_by"], "07:10")
        self.assertEqual(windows["reserve_breakdown"]["windows"]["arrive_by"], windows["outbound_arrive_by"])

    def test_relaxed_same_day_advice_counts_each_arrival_airport_window(self):
        from analyzer import _same_day_no_feasible_note

        note = _same_day_no_feasible_note(
            [
                {
                    "flight_no": "PEK_EARLY_FAR",
                    "departure_airport": "SHA",
                    "arrival_airport": "PEK",
                    "departure_date": "2026-06-26",
                    "arrival_date": "2026-06-26",
                    "departure_time": "06:10",
                    "arrival_time": "08:20",
                },
                {
                    "flight_no": "PKX_NEAR",
                    "departure_airport": "SHA",
                    "arrival_airport": "PKX",
                    "departure_date": "2026-06-26",
                    "arrival_date": "2026-06-26",
                    "departure_time": "06:10",
                    "arrival_time": "08:05",
                },
            ],
            [
                {
                    "flight_no": "RT",
                    "departure_airport": "PKX",
                    "departure_date": "2026-06-26",
                    "departure_time": "21:00",
                }
            ],
            {
                "same_day_round_trip": True,
                "depart_date": "2026-06-26",
                "business_start": "10:30",
                "business_end": "17:00",
                "meeting_location": "\u5927\u5174\u533a",
                "meeting_importance": "important",
            },
        )

        self.assertTrue(note.startswith("\u672c\u6b21\u65e0\u65b9\u6848\u4e3b\u56e0\u662f\u3010\u65f6\u95f4\u7a97\u53e3\u3011"))
        self.assertIn("\u4f1a\u8bae\u91cd\u8981\u7a0b\u5ea6\u6539\u4e3a\u666e\u901a\u5546\u52a1\u540e", note)
        self.assertIn("2\u4e2a\u53bb\u7a0b\u53ef\u9009", note)

    def test_critical_meeting_fixed_defaults_and_checked_baggage_extra(self):
        from analyzer import compute_same_day_windows

        windows = compute_same_day_windows(
            {
                "constraints": {
                    "same_day_round_trip": True,
                    "business_start": "10:00",
                    "business_end": "17:00",
                    "meeting_importance": "critical",
                    "destination_transport_min": 20,
                    "checked_baggage_required": True,
                }
            },
            "SHA",
            "PEK",
        )

        outbound = windows["reserve_breakdown"]["outbound"]
        ret = windows["reserve_breakdown"]["return"]
        self.assertEqual(outbound["destination_transport_margin_min"], 35)
        self.assertEqual(outbound["arrival_exit_min"], 45)
        self.assertEqual(outbound["delay_buffer_min"], 90)
        self.assertEqual(outbound["pre_meeting_buffer_min"], 90)
        self.assertEqual(ret["departure_airport_process_min"], 120)
        self.assertEqual(ret["post_meeting_buffer_min"], 30)

    def test_business_time_margin_uses_four_levels(self):
        from analyzer import classify_business_time_margin

        self.assertEqual(classify_business_time_margin(75)["level"], "稳妥可行")
        self.assertEqual(classify_business_time_margin(45)["level"], "可行但偏紧")
        self.assertEqual(classify_business_time_margin(18)["level"], "高风险卡点")
        self.assertEqual(classify_business_time_margin(-1)["level"], "不可行")


    def test_same_day_round_trip_with_meeting_time_forces_arrival_model(self):
        from analyzer import compute_same_day_windows

        windows = compute_same_day_windows(
            {
                "constraints": {
                    "same_day_round_trip": True,
                    "business_start": "10:30",
                    "business_end": "16:30",
                    "user_transport_min": 70,
                    "redundancy_min": 25,
                    "route_type": "domestic",
                }
            },
            "SHA",
            "PKX",
        )

        self.assertEqual(windows["buffer_model"], "meeting_fixed")
        self.assertLess(windows["arrival_buffer_min"], 120)
        self.assertNotEqual(windows["outbound_arrive_by"], "06:27")
        self.assertGreaterEqual(windows["outbound_arrive_by_minutes"], 7 * 60 + 45)

    def test_total_passengers_defaults_quick_mode_count_to_adults(self):
        from analyzer import get_total_passengers

        total, passengers = get_total_passengers({"basic": {"passenger_count": 3}})

        self.assertEqual(total, 3)
        self.assertEqual(passengers, {"adult": 3, "child": 0, "elderly": 0, "infant": 0})

    def test_same_day_window_rejects_inferred_next_day_arrival_without_arrival_date(self):
        from analyzer import build_same_day_combos

        windows = {
            "outbound_arrive_by_minutes": 8 * 60 + 55,
            "return_depart_after_minutes": 18 * 60,
            "outbound_arrive_by": "08:55",
            "return_depart_after": "18:00",
        }
        outbound = [
            {
                "flight_no": "MU5185",
                "price": 831,
                "departure_date": "2026-06-26",
                "departure_time": "22:50",
                "arrival_time": "00:05",
            },
            {
                "flight_no": "CA1566",
                "price": 850,
                "departure_date": "2026-06-26",
                "departure_time": "23:40",
                "arrival_time": "00:55",
            },
            {
                "flight_no": "MU5099",
                "price": 900,
                "departure_date": "2026-06-26",
                "departure_time": "07:00",
                "arrival_time": "09:15",
            },
        ]
        returns = [
            {
                "flight_no": "CA1589",
                "price": 700,
                "departure_date": "2026-06-26",
                "departure_time": "20:30",
                "arrival_time": "22:40",
            }
        ]

        combos = build_same_day_combos(outbound, returns, windows, "2026-06-26")

        self.assertEqual(combos, [])

    def test_build_same_day_combos_exports_return_floor_by_airport_for_debug_rows(self):
        from analyzer import (
            _same_day_return_window_debug_rows,
            build_same_day_combos,
            compute_same_day_windows,
        )

        constraints = {
            'same_day_round_trip': True,
            'business_start': '10:30',
            'business_end': '17:00',
            'meeting_location': '大兴区',
            'meeting_importance': 'important',
            'transport_mode': 'taxi',
            'route_type': 'domestic',
        }
        windows = compute_same_day_windows({'constraints': constraints}, None, 'PEK')
        outbound = [
            {
                'flight_no': 'PKX_OB',
                'arrival_airport': 'PKX',
                'departure_date': '2026-07-01',
                'departure_time': '07:00',
                'arrival_time': '08:00',
                'price': 800,
            }
        ]
        returns = [
            {
                'flight_no': 'MU5128',
                'departure_airport': 'PKX',
                'departure_date': '2026-07-01',
                'departure_time': '21:00',
                'arrival_time': '23:10',
                'price': 900,
            }
        ]

        build_same_day_combos(outbound, returns, windows, '2026-07-01', constraints=constraints)
        rows = _same_day_return_window_debug_rows(returns, windows, '2026-07-01', constraints={})

        self.assertEqual(windows['return_depart_after_by_airport']['PKX'], '19:55')
        self.assertEqual(rows[0]['return_depart_after_datetime'], '2026-07-01 19:55:00')
        self.assertTrue(rows[0]['passed'])
    def test_same_day_outbound_window_uses_flight_date_when_boundary_is_text(self):
        from analyzer import _same_day_outbound_passes_window

        flight = {
            'flight_no': 'MU5099',
            'departure_date': '2026-07-01',
            'departure_time': '07:00',
            'arrival_date': '2026-07-01',
            'arrival_time': '09:15',
        }

        self.assertTrue(
            _same_day_outbound_passes_window(
                flight,
                {'outbound_arrive_by': '09:52'},
                None,
            )
        )

    def test_same_day_return_window_debug_uses_flight_date_when_airport_floor_is_text(self):
        from analyzer import _same_day_return_window_debug_rows

        rows = _same_day_return_window_debug_rows(
            [
                {
                    'flight_no': 'MU9192',
                    'departure_airport': 'PKX',
                    'departure_date': '2026-07-01',
                    'departure_time': '20:45',
                },
                {
                    'flight_no': 'CA1589',
                    'departure_airport': 'PEK',
                    'departure_date': '2026-07-01',
                    'departure_time': '20:30',
                },
            ],
            {'return_depart_after_by_airport': {'PKX': '19:55', 'PEK': '20:39'}},
            None,
        )

        self.assertEqual(rows[0]['return_depart_after_datetime'], '2026-07-01 19:55:00')
        self.assertTrue(rows[0]['passed'])
        self.assertEqual(rows[1]['return_depart_after_datetime'], '2026-07-01 20:39:00')
        self.assertFalse(rows[1]['passed'])
    def test_build_same_day_combos_uses_flight_dates_when_window_text_has_no_global_date(self):
        from analyzer import build_same_day_combos

        windows = {
            'outbound_arrive_by_by_airport': {'PKX': '09:52'},
            'return_depart_after_by_airport': {'PKX': '19:55', 'PEK': '20:39'},
        }
        outbound = [
            {
                'flight_no': 'MU5099',
                'departure_airport': 'SHA',
                'arrival_airport': 'PKX',
                'departure_date': '2026-07-01',
                'arrival_date': '2026-07-01',
                'departure_time': '07:00',
                'arrival_time': '09:15',
                'price': 831,
            }
        ]
        returns = [
            {
                'flight_no': 'MU9192',
                'departure_airport': 'PKX',
                'arrival_airport': 'SHA',
                'departure_date': '2026-07-01',
                'departure_time': '20:45',
                'arrival_time': '22:55',
                'price': 1600,
            },
            {
                'flight_no': 'CA1589',
                'departure_airport': 'PEK',
                'arrival_airport': 'PVG',
                'departure_date': '2026-07-01',
                'departure_time': '20:30',
                'arrival_time': '22:40',
                'price': 1820,
            },
        ]

        combos = build_same_day_combos(outbound, returns, windows, None)

        self.assertEqual([(c['outbound']['flight_no'], c['return']['flight_no']) for c in combos], [('MU5099', 'MU9192')])
    def test_same_day_return_window_debug_rows_are_independent_from_outbound_matches(self):
        from analyzer import _same_day_return_window_debug_rows

        windows = {
            "outbound_arrive_by_minutes": 8 * 60 + 55,
            "return_depart_after_minutes": 20 * 60 + 25,
            "outbound_arrive_by": "08:55",
            "return_depart_after": "20:25",
        }
        returns = [
            {
                "flight_no": "RT_EARLY",
                "price": 900,
                "departure_date": "2026-06-26",
                "departure_time": "20:10",
                "arrival_time": "22:20",
            },
            {
                "flight_no": "RT_OK",
                "price": 1000,
                "departure_date": "2026-06-26",
                "departure_time": "20:30",
                "arrival_time": "22:40",
            },
            {
                "flight_no": "RT_NEXT_DAY",
                "price": 800,
                "departure_date": "2026-06-27",
                "departure_time": "00:30",
                "arrival_time": "02:40",
            },
        ]

        rows = _same_day_return_window_debug_rows(returns, windows, "2026-06-26")

        self.assertEqual([row["flight_no"] for row in rows], ["RT_EARLY", "RT_OK", "RT_NEXT_DAY"])
        self.assertEqual([row["passed"] for row in rows], [False, True, False])
        self.assertEqual(rows[1]["departure_datetime"], "2026-06-26 20:30:00")
        self.assertEqual(rows[1]["return_depart_after_datetime"], "2026-06-26 20:25:00")

    def test_same_day_return_window_debug_parses_string_lowerbound_with_date(self):
        from analyzer import _same_day_return_window_debug_rows

        rows = _same_day_return_window_debug_rows(
            [
                {
                    "flight_no": "RT_OK",
                    "price": 1000,
                    "departure_date": "2026-06-26",
                    "departure_time": "20:30",
                    "arrival_time": "22:40",
                }
            ],
            {"return_depart_after": "20:25"},
            "2026-06-26",
        )

        self.assertEqual(rows[0]["return_depart_after_datetime"], "2026-06-26 20:25:00")
        self.assertTrue(rows[0]["passed"])

    def test_same_day_return_window_debug_uses_per_airport_lowerbound_map(self):
        from analyzer import _same_day_return_window_debug_rows

        rows = _same_day_return_window_debug_rows(
            [
                {
                    "flight_no": "PEK_TOO_EARLY",
                    "departure_airport": "PEK",
                    "departure_date": "2026-06-26",
                    "departure_time": "20:30",
                    "arrival_time": "22:40",
                },
                {
                    "flight_no": "PEK_OK",
                    "departure_airport": "PEK",
                    "departure_date": "2026-06-26",
                    "departure_time": "21:30",
                    "arrival_time": "23:40",
                },
                {
                    "flight_no": "PKX_OK",
                    "departure_airport": "PKX",
                    "departure_date": "2026-06-26",
                    "departure_time": "20:10",
                    "arrival_time": "22:10",
                },
            ],
            {
                "return_depart_after_by_airport": {"PEK": "20:39", "PKX": "19:55"},
                "return_depart_after_minutes_by_airport": {"PEK": 1239, "PKX": 1195},
            },
            "2026-06-26",
        )

        self.assertEqual(
            [(row["flight_no"], row["return_depart_after_datetime"], row["passed"]) for row in rows],
            [
                ("PEK_TOO_EARLY", "2026-06-26 20:39:00", False),
                ("PEK_OK", "2026-06-26 20:39:00", True),
                ("PKX_OK", "2026-06-26 19:55:00", True),
            ],
        )

    def test_same_day_return_window_debug_soft_fallback_when_lowerbound_missing(self):
        from analyzer import _same_day_return_window_debug_rows

        returns = [
            {
                "flight_no": "RT_OK",
                "price": 1000,
                "departure_date": "2026-06-26",
                "departure_time": "20:30",
                "arrival_time": "22:40",
            }
        ]
        windows = {"outbound_arrive_by_minutes": 8 * 60 + 55}

        rows = _same_day_return_window_debug_rows(returns, windows, "2026-06-26")

        self.assertEqual(rows[0]["return_depart_after_datetime"], None)
        self.assertFalse(rows[0]["passed"])
        self.assertIn("返程下限缺失", rows[0]["warning"])

    def test_same_day_return_window_debug_calculates_lowerbound_from_constraints(self):
        from analyzer import _same_day_return_window_debug_rows

        rows = _same_day_return_window_debug_rows(
            [
                {
                    "flight_no": "PEK_2030",
                    "departure_airport": "PEK",
                    "departure_date": "2026-06-26",
                    "departure_time": "20:30",
                    "arrival_time": "22:40",
                },
                {
                    "flight_no": "PKX_2010",
                    "departure_airport": "PKX",
                    "departure_date": "2026-06-26",
                    "departure_time": "20:10",
                    "arrival_time": "22:10",
                },
            ],
            {"outbound_arrive_by_minutes": 8 * 60},
            "2026-06-26",
            constraints={
                "same_day_round_trip": True,
                "business_start": "10:30",
                "business_end": "17:00",
                "meeting_location": "大兴区",
                "meeting_importance": "important",
                "transport_mode": "taxi",
                "route_type": "domestic",
            },
        )

        self.assertEqual(rows[0]["return_depart_after_datetime"], "2026-06-26 20:39:00")
        self.assertFalse(rows[0]["passed"])
        self.assertEqual(rows[1]["return_depart_after_datetime"], "2026-06-26 19:55:00")
        self.assertTrue(rows[1]["passed"])

    def test_same_day_return_window_debug_calculates_lowerbound_from_window_context(self):
        from analyzer import _same_day_return_window_debug_rows

        rows = _same_day_return_window_debug_rows(
            [
                {
                    "flight_no": "MU5128",
                    "departure_airport": "PKX",
                    "departure_date": "2026-07-01",
                    "departure_time": "21:00",
                    "arrival_time": "23:10",
                },
                {
                    "flight_no": "MU5166",
                    "departure_airport": "PEK",
                    "departure_date": "2026-07-01",
                    "departure_time": "21:30",
                    "arrival_time": "23:25",
                },
                {
                    "flight_no": "MU9192",
                    "departure_airport": "PKX",
                    "departure_date": "2026-07-01",
                    "departure_time": "20:45",
                    "arrival_time": "22:55",
                },
            ],
            {
                "business_start": "10:30",
                "business_end": "17:00",
                "meeting_location": "大兴区",
                "meeting_importance": "important",
                "transport_mode": "taxi",
                "route_type": "domestic",
                "destination_transport_min": 60,
                "transport_min": 60,
                "return_depart_after": "20:39",
                "return_depart_after_minutes": 20 * 60 + 39,
            },
            "2026-07-01",
            constraints={},
        )

        self.assertEqual(rows[0]["return_depart_after_datetime"], "2026-07-01 19:55:00")
        self.assertTrue(rows[0]["passed"])
        self.assertEqual(rows[1]["return_depart_after_datetime"], "2026-07-01 20:39:00")
        self.assertTrue(rows[1]["passed"])
        self.assertEqual(rows[2]["return_depart_after_datetime"], "2026-07-01 19:55:00")
        self.assertTrue(rows[2]["passed"])

    def test_closest_same_day_outbound_uses_full_date_for_cross_midnight(self):
        from analyzer import _closest_same_day_outbound_options

        windows = {
            "outbound_arrive_by_minutes": 8 * 60 + 55,
            "outbound_arrive_by": "08:55",
        }
        outbound = [
            {
                "flight_no": "MU5185",
                "price": 831,
                "departure_date": "2026-06-26",
                "departure_time": "22:50",
                "arrival_time": "00:05",
            },
            {
                "flight_no": "MU5099",
                "price": 900,
                "departure_date": "2026-06-26",
                "departure_time": "07:00",
                "arrival_time": "09:15",
            },
        ]

        options = _closest_same_day_outbound_options(outbound, windows, "2026-06-26", limit=1)

        self.assertEqual(options[0]["flight_no"], "MU5099")
    def test_closest_same_day_outbound_uses_each_arrival_airport_transport(self):
        from analyzer import _closest_same_day_outbound_options, compute_same_day_windows

        constraints = {
            "same_day_round_trip": True,
            "business_start": "10:30",
            "business_end": "17:00",
            "meeting_location": "\u5317\u4eac\u5e02\u5927\u5174\u533a",
            "meeting_importance": "normal",
            "route_type": "domestic",
        }
        windows = compute_same_day_windows({"constraints": constraints}, "SHA", "PEK")
        outbound = [
            {
                "flight_no": "PEK_EARLY_BUT_FAR",
                "price": 800,
                "departure_airport": "SHA",
                "arrival_airport": "PEK",
                "departure_time": "06:30",
                "arrival_time": "08:40",
                "departure_date": "2026-06-19",
                "arrival_date": "2026-06-19",
            },
            {
                "flight_no": "PKX_LATER_BUT_NEAR",
                "price": 800,
                "departure_airport": "SHA",
                "arrival_airport": "PKX",
                "departure_time": "06:50",
                "arrival_time": "09:00",
                "departure_date": "2026-06-19",
                "arrival_date": "2026-06-19",
            },
        ]

        options = _closest_same_day_outbound_options(
            outbound,
            windows,
            "2026-06-19",
            limit=2,
            constraints=constraints,
        )

        self.assertEqual(options[0]["flight_no"], "PKX_LATER_BUT_NEAR")
        self.assertEqual(options[0]["destination_transport_min"], 25)
        self.assertLess(
            options[0]["destination_transport_min"],
            options[1]["destination_transport_min"],
        )
    def test_same_day_no_feasible_note_leads_with_outbound_time_bottleneck(self):
        from analyzer import _same_day_no_feasible_note

        note = _same_day_no_feasible_note(
            [
                {"flight_no": "MU5185", "arrival_airport": "PKX", "departure_date": "2026-06-26", "departure_time": "22:30", "arrival_date": "2026-06-27", "arrival_time": "00:05"},
                {"flight_no": "MU5099", "arrival_airport": "PKX", "departure_date": "2026-06-26", "departure_time": "07:00", "arrival_date": "2026-06-26", "arrival_time": "09:15"},
            ],
            [{"flight_no": "MU5170", "departure_airport": "PKX", "departure_date": "2026-06-26", "departure_time": "21:00"}],
            {
                "same_day_round_trip": True,
                "depart_date": "2026-06-26",
                "business_start": "10:30",
                "business_end": "17:00",
                "meeting_location": "\u5927\u5174\u533a",
                "meeting_importance": "important",
                "checked_baggage_required": True,
            },
        )

        self.assertTrue(note.startswith("\u672c\u6b21\u65e0\u65b9\u6848\u4e3b\u56e0\u662f\u3010\u53bb\u7a0b\u65f6\u95f4\u3011"))
        self.assertIn("\u6700\u65e9MU5099 09:15\u5230", note)
        self.assertIn("\u970008:00\u524d\u843d\u5730", note)
        self.assertIn("\u665a1h15m", note)
        self.assertIn("\u8fd4\u7a0b\u67091\u4e2a\u53ef\u9009,\u975e\u963b\u585e", note)
        self.assertIn("\u524d\u4e00\u665a\u5230\u8fbe", note)
        self.assertIn("\u8c03\u4f4e\u9884\u7559/\u4f1a\u8bae\u91cd\u8981\u5ea6", note)
        self.assertNotIn("MU5185", note)
        self.assertNotIn("\u8fd4\u7a0b\u5f53\u5929\u65e0\u7b26\u5408\u822a\u73ed", note)
        self.assertNotIn("\u65f6\u95f4\u7a97\u53e3100%", note)

    def test_same_day_no_feasible_note_marks_physical_impossibility(self):
        from analyzer import _same_day_no_feasible_note

        note = _same_day_no_feasible_note(
            [
                {
                    "flight_no": "MU5099",
                    "arrival_airport": "PKX",
                    "departure_date": "2026-06-26",
                    "departure_time": "07:00",
                    "arrival_date": "2026-06-26",
                    "arrival_time": "09:15",
                }
            ],
            [{"flight_no": "MU5170", "departure_airport": "PKX", "departure_date": "2026-06-26", "departure_time": "21:00"}],
            {
                "same_day_round_trip": True,
                "depart_date": "2026-06-26",
                "business_start": "10:00",
                "business_end": "17:00",
                "meeting_location": "\u5927\u5174\u533a",
                "meeting_importance": "important",
                "checked_baggage_required": True,
            },
        )

        self.assertIn("\u6700\u65e9\u5230\u8fbe 09:15", note)
        self.assertIn("\u843d\u5730\u5230\u4f1a\u573a\u6700\u5c11\u970070\u5206\u949f", note)
        self.assertIn("\u8be5\u822a\u7ebf\u5f53\u5929\u65e0\u6cd5\u6ee1\u8db3 10:00 \u4f1a\u8bae", note)
        self.assertIn("\u5c06\u4f1a\u8bae\u63a8\u8fdf\u81f3 \u226510:25", note)

    def test_same_day_airport_window_cache_prints_reserve_once_per_airport(self):
        import contextlib
        import io

        from analyzer import (
            _clear_same_day_window_cache_for_tests,
            _same_day_windows_for_airport,
            compute_same_day_windows,
        )

        constraints = {
            "same_day_round_trip": True,
            "depart_date": "2026-06-26",
            "business_start": "10:30",
            "business_end": "17:00",
            "meeting_location": "\u5927\u5174\u533a",
            "meeting_importance": "important",
            "route_type": "domestic",
            "checked_baggage_required": True,
        }

        _clear_same_day_window_cache_for_tests()
        base_windows = compute_same_day_windows({"constraints": constraints}, "SHA", "PEK")

        stream = io.StringIO()
        with contextlib.redirect_stdout(stream):
            for _ in range(3):
                _same_day_windows_for_airport(base_windows, constraints, "PKX")

        self.assertEqual(stream.getvalue().count("[\u53bb\u7a0b\u5230\u4f1a-\u65b0]"), 1)
if __name__ == "__main__":
    unittest.main()




