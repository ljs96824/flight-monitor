"""F8：多机场合并去重按"航班组合 + 舱位 + 起降机场"判同一航班，路线摘要不参与判重。

路线摘要是各来源格式不一的展示文本(机场代码、城市名或缺失)：
旧键含路线摘要时，同一航班因摘要格式不同被保留两份(单程会出现两个同航班方案)；
摘要同为空时，不同机场出发的同号航班又会被合并成一份。
"""

import io
import unittest
from contextlib import redirect_stdout

import main


DAY = "2099-10-01"


def flight(combo, price, dep=None, arr=None, route=None, cabin="economy", segments=None):
    item = {"flight_combo": combo, "price": price, "cabin_class": cabin}
    if dep:
        item["departure_airport"] = dep
    if arr:
        item["arrival_airport"] = arr
    if route is not None:
        item["route_summary"] = route
    if segments:
        item["segments"] = segments
    return item


class PairAggregator:
    def __init__(self, data):
        self.data = data
        self.calls = []

    def collect(self, origin, destination, date_str, cabin_classes=None):
        self.calls.append((origin, destination))
        return {"flights": [dict(item) for item in self.data.get((origin, destination), [])], "source_stats": {}}


def collect(data, origins, destinations):
    aggregator = PairAggregator(data)
    with redirect_stdout(io.StringIO()):
        result = main.collect_for_airport_matrix(aggregator, origins, destinations, DAY)
    rows = [(item["flight_combo"], item["price"], item.get("cabin_class"),
             main._flight_airport_value(item, "departure"), main._flight_airport_value(item, "arrival"))
            for item in result["flights"]]
    return rows, result["source_stats"]["after_dedup"], aggregator.calls


class MatrixDedupeTest(unittest.TestCase):
    def test_same_flight_with_different_route_text_kept_once(self):
        for label, second_route in (("empty", ""), ("missing", None), ("city_names", "上海浦东 → 大阪关西"),
                                    ("spacing", "PVG→KIX")):
            with self.subTest(route=label):
                rows, after_dedup, calls = collect({
                    ("PVG", "KIX"): [flight("MU515", 1300, "PVG", "KIX", "PVG → KIX")],
                    ("SHA", "KIX"): [flight("MU515", 1200, "PVG", "KIX", second_route),
                                     flight("FM811", 900, "SHA", "KIX", "")],
                }, ["PVG", "SHA"], ["KIX"])
                self.assertEqual(calls, [("PVG", "KIX"), ("SHA", "KIX")])
                self.assertEqual(rows, [("FM811", 900, "economy", "SHA", "KIX"),
                                        ("MU515", 1200, "economy", "PVG", "KIX")], "cheapest_copy_kept_once")
                self.assertEqual(after_dedup, 2)

    def test_endpoints_from_segments_match_top_level_fields(self):
        segments = [{"flight_no": "MU515", "dep_airport": "PVG", "arr_airport": "KIX"}]
        rows, _after, _calls = collect({
            ("PVG", "KIX"): [flight("MU515", 1300, route="PVG → KIX", segments=segments)],
            ("SHA", "KIX"): [flight("MU515", 1250, "PVG", "KIX")],
        }, ["PVG", "SHA"], ["KIX"])
        self.assertEqual(rows, [("MU515", 1250, "economy", "PVG", "KIX")])

    def test_distinct_flights_stay_distinct(self):
        cases = (
            ("different_departure_airport_same_empty_route",
             {("PVG", "KIX"): [flight("XX1", 1000, "PVG", "KIX", "")],
              ("SHA", "KIX"): [flight("XX1", 1100, "SHA", "KIX", "")]},
             ["PVG", "SHA"], ["KIX"],
             [("XX1", 1000, "economy", "PVG", "KIX"), ("XX1", 1100, "economy", "SHA", "KIX")]),
            ("different_arrival_airport",
             {("PVG", "KIX"): [flight("XX100", 1000, "PVG", "KIX", "PVG → KIX")],
              ("PVG", "ITM"): [flight("XX100", 1500, "PVG", "ITM", "PVG → KIX → ITM")]},
             ["PVG"], ["KIX", "ITM"],
             [("XX100", 1000, "economy", "PVG", "KIX"), ("XX100", 1500, "economy", "PVG", "ITM")]),
            ("different_cabin",
             {("PVG", "KIX"): [flight("MU515", 1300, "PVG", "KIX", "PVG → KIX")],
              ("SHA", "KIX"): [flight("MU515", 5200, "PVG", "KIX", "", cabin="business")]},
             ["PVG", "SHA"], ["KIX"],
             [("MU515", 1300, "economy", "PVG", "KIX"), ("MU515", 5200, "business", "PVG", "KIX")]),
        )
        for label, data, origins, destinations, expected in cases:
            with self.subTest(case=label):
                rows, after_dedup, _calls = collect(data, origins, destinations)
                self.assertEqual(rows, expected)
                self.assertEqual(after_dedup, len(expected))


class DedupeFunctionTest(unittest.TestCase):
    def test_route_text_ignored_and_order_by_price(self):
        flights = [
            flight("MU515", 1300, "PVG", "KIX", "PVG → KIX"),
            flight("mu 515", 1250, "PVG", "KIX", "上海浦东 → 大阪关西"),
            flight("FM811", 900, "SHA", "KIX"),
            flight("NH920", "invalid", "PVG", "KIX"),
            flight("NH920", 0, "PVG", "KIX"),
        ]
        result = main._dedupe_flights(flights)
        self.assertEqual([(item["flight_combo"], item["price"]) for item in result],
                         [("FM811", 900), ("mu 515", 1250)])


if __name__ == "__main__":
    unittest.main()
