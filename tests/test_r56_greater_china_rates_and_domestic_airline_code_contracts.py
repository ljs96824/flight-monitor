"""Offline contracts for Greater China fares and evidence-based domestic carrier codes."""
import ast
from hashlib import sha256
import inspect
from pathlib import Path
import unittest
from unittest.mock import patch

import domestic_fare_rules as fares
import price_estimator as prices


ROOT = Path(__file__).resolve().parents[1]
FAMILY = {"adult": 1, "child": 1}
ALIASES = ("greater_china", "greaterchina", "greater-china", "hk_mo_tw", "hkmotw",
           "hongkong_macao_taiwan", "hong_kong_macao_taiwan")
EXTRA_SCOPES = {
    ("price_estimator.py", None): {
        "normalize_passengers_for_pricing": "e0a000241639d3d9640c4bf7482d2f0c4bf6dccf534320f52644cbe3243c5cd5",
        "passenger_fare_rates": "6c2e21856fe40e7bd94297d7969ecfa812c248a20599252f0b12ec59dbc71d6e",
        "passenger_price_factor": "94014e82ab794d91e7c68502d86c52f2fc360991f7e234aac65d2526efd05220",
        "round_display_price": "8471ed1ac555b258c1f3d3fb5f714b1c70fd5f4baea2cae8a3fda754ff58b2c8",
        "build_display_prices": "7d7db146ccf88664e660eeffc9681a90204c485baa1cd827546af1131dfbfc26",
        "build_passenger_price_breakdown": "278086c8199584dad7b2ab97362d1aed88b383db09e9da2b6a00f77cd061d5cc",
        "calc_total_price_for_passengers": "1c239e3ce7c8dec005122958f7c2b96bf3a6f0baa04f34c2c27548982dd187e7",
        "calc_total_for_passengers": "ff17953566b1e4eb8b5b586f70533aeb7079cf2c4ec80c4a43e8211d272bfd52",
        "build_price_tiers": "08ce57ea30d23adb432777853a2d5c1dc3dc01570ae17ef8a1816f0cbff969d5",
        "calc_transaction_price": "00b9d81c2731f6c85e2b6660bca3b022b9c866bed2eee888489d2f32924a08df",
        "estimate_real_price": "622ca2ae863d3d7c9482759c6af99b1eb5bced7569df5a5092a33568125cd936",
    },
    ("domestic_fare_rules.py", None): {
        "get_aircraft_name": "c486491022c545ea559547a0536983568d94dd21bc58b05816425ed2174701b1",
        "re_match_aircraft_code": "ae2e6482955aaa56ef48b0ecc21f2bb281c35671a6e5da2d0b690ef8cd739567",
        "get_domestic_baggage": "99d02738bc65a3c9634e86cb799b4ef063539568e87d1e192af3cffddfb73a58",
        "get_domestic_refund": "4ea08bff0732d00401e69e8a3d4de9ff6cd1635d4c4c297172aa4e849fc5efa4",
        "standardize_domestic_fare_rules": "37c8f74c59fbd4a7e503fab73c87f499227b280c381b085f305a817b20588fd4",
    },
}


class TestTrack1GreaterChinaPassengerFareContracts(unittest.TestCase):
    def test_g01_greater_china_and_aliases_resolve_75_percent_child_rate_and_distinct_rate_key_and_log(self):
        from source_profiles import ROUTE_TYPE_ALIASES

        self.assertEqual(set(ALIASES), {"greater_china"} | {
            k for k, v in ROUTE_TYPE_ALIASES.items() if v == "greater_china"})
        for alias in ALIASES:
            route = "  " + alias.upper() + " "
            key, rates = prices.passenger_fare_rates(route)
            self.assertEqual((key, rates["child"], rates["infant"]), ("greater_china", .75, .1), route)
            self.assertIn("港澳台", rates["note"])
            self.assertIn("75折", rates["note"])
            self.assertNotIn("国内常规5折", rates["note"])
            with patch.object(prices, "_logged_passenger_factor_keys", set()), patch.object(prices, "safe_log") as log:
                self.assertEqual(prices.passenger_price_factor(FAMILY, route), 1.75)
                log.assert_called_once()
                self.assertIn("route=greater_china", log.call_args.args[0])
                self.assertIn("PASSENGER_FARE_RATES[greater_china]", log.call_args.args[0])
        for value, expected in (("international", "international"), ("intl", "international"),
                                ("domestic", "domestic"), (None, "domestic"), ("", "domestic"),
                                ("unrecognized", "domestic")):
            self.assertEqual(prices.passenger_fare_rates(value)[0], expected)

    def test_g02_display_price_tree_breakdown_tiers_and_mixed_cabin_apply_greater_china_rates(self):
        route = "greater_china"
        self.assertEqual(prices.calc_total_price_for_passengers(1000, FAMILY, route_type=route), 1750)
        self.assertEqual(prices.calc_total_for_passengers(1000, FAMILY, route), 1750)
        self.assertEqual(prices.build_passenger_price_breakdown(1000, FAMILY, route_type=route)["total"], 1750)
        self.assertEqual(prices.build_display_prices(1000, 1000, FAMILY, route)["total"], 3500)
        self.assertEqual(prices.build_price_tiers(1000, 1000, FAMILY, route)["total_roundtrip_ref"], 3500)
        leg = prices.build_display_prices(1410, passengers=FAMILY, route_type=route)["outbound"]
        self.assertEqual(leg["parts_by_type"]["child"]["unit_price"], 1058)
        mixed = prices.build_display_prices(None, passengers=FAMILY, route_type=route,
            per_cabin_unit_prices={"outbound": {"business": 2200, "economy": 1410}},
            cabin_allocation={"business": {"adult": 1}, "economy": {"child": 1}})
        child = mixed["outbound"]["cabins"]["economy"]["parts_by_type"]["child"]
        self.assertEqual((mixed["route_rate_key"], child["ratio"], child["total"], mixed["total"]),
                         (route, .75, 1058, 3258))

    def test_g03_pricing_caliber_budget_limits_and_low_price_bucket_isolate_greater_china_from_domestic(self):
        import analyzer
        import pricing
        from tests.test_low_price_same_caliber_history import product

        route, scope = "greater_china", "all_passengers_oneway"
        self.assertEqual(pricing.price_in_scope(1000, FAMILY, scope, route), 1750)
        self.assertEqual(pricing.budget_to_pp(1750, FAMILY, scope, route), 1000)
        self.assertIn("费率合计1.75", pricing.caliber_label(scope, FAMILY, route))
        limits = analyzer.passenger_budget_limits(1750, budget_scope="all", passengers=FAMILY,
                                                  route_type=route, round_trip=False)
        self.assertEqual((limits["multiplier"], limits["max_budget_pp_oneway"]), (1.75, 1000))
        f, unit = product(price=1000)
        f["arrival_airport"] = f["segments"][0]["arr_airport"] = unit["dest_airport"] = "HKG"
        day = f["departure_date"]
        sub = dict(origin="PVG", destination="HKG", depart_date=day, budget_scope="all")
        analysis = {"outbound": {"roundtrip_candidate_flights": [f]}}
        evidence = {"outbound": {"attempted_airport_pairs": [("PVG", "HKG")], "fetch_evidence_units": [unit]}}
        buckets = []
        for profile, amount in ((route, 1750), ("domestic", 1500)):
            offer = analyzer.build_current_low_price_offer(sub, analysis, evidence, trip_type="one_way",
                route_type=profile, origins=["PVG"], destinations=["HKG"], passengers=FAMILY, constraint_fp="test")
            self.assertTrue(offer.get("can_evaluate_current"), offer)
            self.assertEqual((offer["route_rate_key"], offer["price_scope"], offer["amount_cny"]),
                             (profile, scope, amount))
            buckets.append(offer["bucket_key"])
        self.assertNotEqual(*buckets)


class TestTrack2DomesticAirlineCodePriorityAndSafety(unittest.TestCase):
    def test_g04_explicit_airline_code_fields_take_priority_over_display_name_or_conflicting_airline(self):
        code = fares.airline_code_from_flight
        for display in ("春秋航空", "CA"):
            self.assertEqual(code({"airline": display, "airline_code": "9C"}), "9C")
        self.assertEqual(code({"airline": "Spring Airlines", "carrier_code": "9C"}), "9C")
        for field in ("airlineCode", "carrierCode", "iata_code", "iataCode", "carrier"):
            self.assertEqual(code({field: " 3u ", "airline": "CA", "flight_no": "CA1501"}), "3U", field)
        for value in ("3U", "KN"):
            self.assertEqual(code({"airline": value}), value)

    def test_g05_non_code_airline_falls_through_to_flight_numbers_segments_and_unambiguous_domestic_names(self):
        code = fares.airline_code_from_flight
        self.assertEqual(code({"airline": "春秋航空", "flight_no": "9C8888"}), "9C")
        for sep in "+/|,;":
            self.assertEqual(code({"airline": "Spring Airlines", "flight_combo": "9C 8888"+sep+"MU5101"}), "9C")
        self.assertEqual(code({"airline": "中国国际航空", "segments": [{"flight_no": "CA1501"}]}), "CA")
        self.assertEqual(code({"flightNumber": "MU_5101", "segments": [{"carrierCode": "CA"}]}), "MU")
        self.assertEqual(code({"segments": [{"iataCode": "3U", "flightNo": "CA1501"}]}), "3U")
        self.assertEqual(code({"segments": [{"flightNumber": "CA-1501"}]}), "CA")
        self.assertEqual(code({"airline": "中国东方航空"}), "MU")
        self.assertEqual(code({"airline_summary": "中国联合航空 / 中联航"}), "KN")
        self.assertEqual(code({"segments": [{"airlineName": "深圳航空"}]}), "ZH")
        for row in ({"airline": "Unknown Foreign Carrier"}, {"airline_summary": "东航 / 国航"},
                    {"airline": "国航", "segments": [{"airline": "东航"}]},
                    {"flight_no": "123456"}, {"flightNo": "ＣＡ123"}, {"segments": [None]}):
            self.assertEqual(code(row), "", row)

    def test_g06_standardize_domestic_fare_rules_classifies_lcc_full_service_and_unknown_with_display_names(self):
        for name, code, number, expected in (("春秋航空", "9C", "9C8888", (False, 0, "需加购")),
                                             ("中国国际航空", "CA", "CA1501", (True, 20, "标准"))):
            for extra in ({"airline_code": code}, {"flight_no": number}):
                bag = fares.standardize_domestic_fare_rules({"airline": name, **extra})["baggage"]
                self.assertEqual((bag["included"], bag["checked_kg"], bag["level"]), expected)
        bag = fares.standardize_domestic_fare_rules({"airline": "未知包机"})["baggage"]
        self.assertEqual((bag["included"], bag["level"]), (None, "待确认"))


class TestTrack3DomesticAnalyzerAndNotifierIntegration(unittest.TestCase):
    def test_g07_analyzer_verify_fare_rules_tags_and_effective_cost_handle_chinese_airline_display_names(self):
        import analyzer
        from on_time_data import AIRLINE_PUNCTUALITY

        lcc = dict(route_type="domestic", airline="春秋航空", flight_no="9C8888", price=500,
                   departure_airport="PVG", arrival_airport="PEK", stops=0)
        issue = analyzer.verify_fare_rules(lcc, {"baggage": "required"})["issues"]
        self.assertTrue(any("不含免费托运行李" in x for x in issue), issue)
        self.assertIn("廉航低价", analyzer.make_domestic_tags(lcc, {"price": "high"}))
        self.assertEqual(analyzer.calc_effective_cost(lcc, {"baggage": "required"})["baggage_cost"], 100)
        full = dict(route_type="domestic", airline="东方航空", flight_no="MU5101", cabin_code="Y",
                    departure_airport="PVG", arrival_airport="PEK", stops=0)
        tags = analyzer.make_domestic_tags(full, {"time": "high"})
        self.assertIn("商务友好", tags)
        self.assertIn("低风险", tags)
        self.assertEqual(analyzer.enrich_travel_risk_and_cost(full)["punctuality"]["level"], AIRLINE_PUNCTUALITY["MU"])

    def test_g08_juhe_normalized_chinese_airline_row_preserves_notifier_lcc_tags_and_baggage_line(self):
        import analyzer
        import notifier
        from sources.juhe_source import JuheSource

        raw = dict(airline="春秋航空", airlineName="春秋航空", flightNo="9C8888", ticketPrice=500,
                   departure="PVG", arrival="PEK", departureDate="2026-10-01", arrivalDate="2026-10-01",
                   departureTime="10:00", arrivalTime="12:30")
        row, = JuheSource().normalize([raw], collected_at="2026-09-30T12:00:00+08:00")
        row["fare_rules"] = fares.standardize_domestic_fare_rules(row)
        row["domestic_tags"] = analyzer.make_domestic_tags(row, {"price": "high"})
        self.assertIn("廉航低价", row["domestic_tags"])
        self.assertIn("廉航低价", notifier._flight_status_tags(row))
        self.assertIn("需另购", notifier._pushplus_baggage_line_for_flight(row))


class TestTrack4Guards(unittest.TestCase):
    def test_g09_protected_files_scoped_ast_locks_and_module_constants_are_exact(self):
        import analyzer
        from tests import test_r55_cli_exit_and_telemetry_isolation_contracts as r55

        files = {**r55.PROTECTED_FILES,
                 "main.py": (178961, "ec8bb6c65fcaa374e9c52ff7753bf8425d88d0eb46fc826464f2b6928b0c9cb3"),
                 "tests/test_r55_cli_exit_and_telemetry_isolation_contracts.py":
                     (28924, "1a099f8fa8c45ca28559a78452f31ced829707a515f9080d6fadee18f3f1597a")}
        self.assertEqual(len(files), 27)
        for path, expected in files.items():
            raw = (ROOT/path).read_text(encoding="utf-8").encode("utf-8")
            self.assertEqual((len(raw), sha256(raw).hexdigest()), expected, path)
        scopes = {**r55.SCOPES, **EXTRA_SCOPES}
        self.assertEqual(sum(map(len, scopes.values())), 69)
        for (path, cls), locks in scopes.items():
            src = (ROOT/path).read_text(encoding="utf-8")
            nodes = ast.parse(src).body
            if cls:
                nodes = next(n for n in nodes if isinstance(n, ast.ClassDef) and n.name == cls).body
            for name, expected in locks.items():
                node, = [n for n in nodes if isinstance(n, ast.FunctionDef) and n.name == name]
                self.assertEqual(sha256(ast.get_source_segment(src, node).encode("utf-8")).hexdigest(), expected, name)
        self.assertEqual(len(r55.INSPECT), 2)
        for name, expected in r55.INSPECT.items():
            self.assertEqual(sha256(inspect.getsource(getattr(analyzer, name)).encode("utf-8")).hexdigest(), expected)
        for path, _ in EXTRA_SCOPES:
            src = (ROOT/path).read_text(encoding="utf-8")
            self.assertEqual(len(src)-len(src.rstrip("\n")), 1)
        self.assertEqual(fares.LCC_AIRLINES, {"9C": "春秋航空", "HO": "吉祥航空", "PN": "西部航空",
                         "KN": "中国联合航空", "BK": "奥凯航空", "JD": "首都航空", "GS": "天津航空"})
        self.assertEqual(fares.FULL_SERVICE, {"CA": "国航", "MU": "东航", "CZ": "南航", "HU": "海航",
                         "MF": "厦航", "ZH": "深航", "SC": "山航", "3U": "川航", "FM": "上航",
                         "GJ": "长龙", "EU": "成都航空"})
        for route, child, note in (("domestic", .5, "儿童票按国内常规5折估算,婴儿票按国内常规1折估算,实际以支付页为准"),
                                   ("international", .75, "儿童票按国际航线常见约75折估算,婴儿票按约1折估算,实际以支付页为准")):
            self.assertEqual(prices.PASSENGER_FARE_RATES[route],
                             dict(adult=1., elderly=1., child=child, infant=.1, note=note))

    def test_g10_frozen_three_carriers_and_normal_roundtrip_baseline_remain_exact(self):
        from tests import test_r55_cli_exit_and_telemetry_isolation_contracts as r55

        case = r55.TestTrack4Guards("test_g10_frozen_three_carriers_and_normal_roundtrip_baseline_remain_exact")
        result = case.run()
        self.assertEqual(result.testsRun, 1)
        self.assertTrue(result.wasSuccessful(), result.errors + result.failures)
