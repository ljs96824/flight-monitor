"""R42: preserve independent unnumbered quotes after the source boundary."""
import io
import json
import unittest
from contextlib import redirect_stdout
from copy import deepcopy
from datetime import date
from hashlib import sha256
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import Mock, patch

import analyzer
import config_loader
import main
import notifier
from sources import aggregator
from tests import test_flight_dedupe_endpoints as endpoints
from tests import test_multi_airport_full_coverage as matrix
from tests import test_r41_legitimate_empty_response_contracts as r41
from tests import test_recommendation_identity as recommendations
from tests import test_return_phase_processing as scenario


class QuoteFeed:
    """Already collected records; endpoints need not equal the searched city airport."""
    search_sources = [matrix.Source()]

    def __init__(self):
        self.calls = []

    def collect(self, origin, destination, day, cabin_classes=None, **kwargs):
        self.calls.append((origin, destination, day))
        if origin == "KIX":
            pair = ("KIX", "PVG")
            offers = [("", 900), ("", 950), ("MU515", 1000)]
        else:
            pair = ("PVG", "KIX")
            offers = [("", 800), ("", 850)] if origin == "PVG" else [("", 900)]
        flights = []
        for number, price in offers:
            f = matrix.quote(*pair, day)
            f.update(flight_combo=number, price=price)
            f["segments"][0]["flight_no"] = number
            flights.append(f)
        return dict(flights=flights, collected_at=matrix.STAMP, source_stats={})


def outbound_matrix(feed):
    return main.collect_for_airport_matrix(feed, ["PVG", "SHA"], ["KIX"], matrix.DAY,
                                           cabin_classes=["economy"])


class TestTrack1DirectDedupeIdentityContracts(unittest.TestCase):
    def test_g01_missing_empty_null_and_literal_none(self):
        flights = [{"price": 95}, {"price": 90}, {"flight_combo": "", "price": 85},
                   {"flight_combo": None, "price": 80}, {"flight_combo": None, "price": 75},
                   {"flight_combo": "NONE", "price": 70}]
        self.assertEqual(main._dedupe_flights(flights), flights[::-1])

    def test_g02_separator_only_quotes_have_no_shared_identity(self):
        combos = ["   ", "\t\n", "+", " + / ", ", ， 、", "///", " + / , ， 、 "]
        flights = [{"flight_combo": c, "price": 90, "row": i} for i, c in enumerate(combos * 2)]
        self.assertTrue(all(analyzer.flight_itinerary_identity_key(f) is None for f in flights))
        self.assertEqual(main._dedupe_flights(flights), flights)

    def test_g03_normalized_products_keep_the_original_low_quote(self):
        flights = [endpoints.flight(c, p, "PVG", "KIX") for c, p in (
            ("MU0515", 155), ("mu 515", 120), ("MU515 / FM0811", 180),
            ("MU0515+FM811", 150), ("mu515, fm811", 145))]
        before = deepcopy(flights)
        self.assertEqual(main._dedupe_flights(flights), [flights[1], flights[4]])
        self.assertEqual(flights, before)

    def test_g04_stable_order_and_first_quote_on_ties(self):
        flights = [dict(flight_combo=n, price=p, row=i) for i, (n, p) in enumerate((
            ("MU515", 150), ("", 100), ("MU0515", 100), ("", 80), ("mu 515", 100), ("", 100)))]
        self.assertEqual(main._dedupe_flights(flights), [flights[i] for i in (3, 2, 1, 5)])


class TestTrack2MatrixAndInputSafetyContracts(unittest.TestCase):
    def test_g05_matrix_counts_and_second_pair_decision(self):
        feed = QuoteFeed()
        with patch.object(main, "safe_log") as log, redirect_stdout(io.StringIO()):
            data = outbound_matrix(feed)
        self.assertEqual(feed.calls, [("PVG", "KIX", matrix.DAY), ("SHA", "KIX", matrix.DAY)])
        self.assertEqual([f["price"] for f in data["flights"]], [800, 850, 900])
        self.assertEqual((data["count"], data["total_count"], data["source_stats"]["after_dedup"]), (3, 3, 3))
        decisions = [c.args[0] for c in log.call_args_list if "[机场组合决策]" in c.args[0]]
        self.assertEqual(len(decisions), 1)
        self.assertIn("有效方案数=2 ", decisions[0])
        self.assertIn("SHA->KIX", decisions[0])

    def test_g06_matrix_quotes_reach_budget_recommendations(self):
        with patch.object(main, "safe_log"), redirect_stdout(io.StringIO()):
            data = outbound_matrix(QuoteFeed())
            analysis = analyzer.analyze_all_flights(data["flights"], mode="budget")
        for pool in ("all_flights", "economy_recommendations"):
            self.assertEqual(sorted(f["price"] for f in analysis[pool]), [800, 850, 900], pool)

    def test_g07_invalid_prices_and_malformed_records(self):
        self.assertEqual(main._dedupe_flights([{"price": True}, {"price": False}]), [])
        good = {"price": "80.5"}
        rows = [{"price": p} for p in (True, False, 0, -1, float("nan"), float("inf"), "bad")]
        try:
            result = main._dedupe_flights([None, 1, "bad", [], *rows, good])
            empty = [main._dedupe_flights(None), main._dedupe_flights([])]
        except Exception as exc:
            self.fail(f"invalid input raised {type(exc).__name__}: {exc}")
        self.assertEqual(result, [good])
        self.assertEqual(empty, [[], []])


class TestTrack3SubscriptionAndReturnPhaseContracts(r41._IsolatedCase):
    def test_g08_return_only_previous_price_requires_identity(self):
        analyze = Mock(wraps=analyzer.analyze_all_flights)
        ok, _, effects = self.run_subscription(
            QuoteFeed(), today=date(2026, 10, 2),
            extra={"get_previous_snapshot_prices": Mock(return_value={"": 666, "MU515": 1100}),
                   "analyze_all_flights": analyze, "collect_nearby_dates": Mock(return_value=[])})
        self.assertTrue(ok)
        analyze.assert_called_once()
        rows = analyze.call_args.args[0]
        self.assertEqual([(f["flight_combo"], f.get("previous_price")) for f in rows],
                         [("", None), ("", None), ("MU515", 1100)])
        self.assertTrue(all("previous_price" not in f for f in rows[:2]))
        effects["save_raw_response"].assert_called_once()

    def test_g09_one_way_subscription_archives_and_analyzes_all_quotes(self):
        sub = scenario.subscription()
        sub.update(origin_airports=["PVG", "SHA"], origin_airports_active=["PVG", "SHA"],
                   round_trip=False, return_date="", hard_constraints={})
        feed = QuoteFeed()
        analyze = Mock(wraps=analyzer.analyze_all_flights)
        ok, _, effects = self.run_subscription(feed, sub=sub, extra={
            "analyze_all_flights": analyze, "collect_nearby_dates": Mock(return_value=[])})
        self.assertTrue(ok)
        self.assertEqual(feed.calls, [("PVG", "KIX", matrix.DAY), ("SHA", "KIX", matrix.DAY)])
        effects["save_raw_response"].assert_called_once()
        raw = effects["save_raw_response"].call_args.args[2]
        self.assertEqual((raw["collection_outcome"], raw["total_count"]), ("quoted", 3))
        analyze.assert_called_once()
        for rows in (analyze.call_args.args[0], raw["flights"]):
            self.assertEqual([(r["flight_combo"], r["price"]) for r in rows], [("", 800), ("", 850), ("", 900)])


class TestTrack4EvergreenCompatibilityGuards(unittest.TestCase):
    def test_g10_raw_unnumbered_source_rows_remain_unusable(self):
        feed = aggregator.FlightAggregator([matrix.Source()], [])
        raw = r41._empty_raw(raw={"error_code": 0, "result": [1]}, flights=[{"price": 100}], raw_result_count=1)
        with patch.object(aggregator, "cached_fetch", return_value=(raw, "fresh")), \
                patch.object(aggregator, "safe_log"), redirect_stdout(io.StringIO()):
            data = main.collect_for_airport_matrix(feed, ["PVG"], ["KIX"], matrix.DAY, cabin_classes=["economy"])
        self.assertEqual(data["collection_outcome"], "data_unusable")
        self.assertEqual([u["valid_flight_count"] for u in data["fetch_evidence_units"]], [0])

    def test_g11_numbered_endpoints_cabins_and_route_text(self):
        with patch.object(main, "safe_log"):
            endpoints.MatrixDedupeTest.test_same_flight_with_different_route_text_kept_once(self)
            endpoints.MatrixDedupeTest.test_distinct_flights_stay_distinct(self)
        recommendations.ItineraryIdentityKeyTest.test_helper_distinguishes_combos_and_cabins(self)

    def test_g12_frozen_delivery_with_isolated_runtime_configuration(self):
        fixture = Path(__file__).parent / "fixtures/frozen_email/economy_payload.json"
        payload = json.loads(fixture.read_bytes())
        with TemporaryDirectory() as tmp, redirect_stdout(io.StringIO()), \
                patch.object(config_loader, "RUNTIME_CONFIG_PATH", Path(tmp) / "missing.yaml"):
            _, email = notifier.render_email(deepcopy(payload))
            push = notifier.prepare_push_render(notifier.render_pushplus_sections(deepcopy(payload))).content
            detail = notifier.render_detail_html(deepcopy(payload))
        for content, size, digest in (
            (email, 53307, "1dd353d2b7bcafc02087957ccccb6661478e20472a22fb3e15dae6a56473ee6b"),
            (push, 5552, "2ac0e80d9f82dbe7ebccf524c0832f09d42d916212b6178316b0b7bcd60574ff"),
            (detail, 54248, "e31b8728b8d0bc05c9bbd4bd99c299aaf6dca7b66674b6c560ec089b1681ffa6"),
        ):
            b = content.encode("utf-8")
            self.assertEqual((len(b), sha256(b).hexdigest()), (size, digest))
