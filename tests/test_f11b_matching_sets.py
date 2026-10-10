"""F11b：判定集合中的乱码项处置合同。

航司名单：删除乱码项，保持现有排序零变化；按航司代码匹配（B2）另行裁决，裁决前名单只含英文名。
城市名单：_route_is_domestic 的中文城市名按 CP936 逆映射还原。
"""

from __future__ import annotations

import unittest

import analyzer
from notifier import _route_is_domestic

EXPECTED_LCC_AIRLINES = [
    "Spirit",
    "Frontier",
    "Ryanair",
    "EasyJet",
    "AirAsia",
    "Scoot",
    "Peach",
    "Cebu Pacific",
    "IndiGo",
    "VietJet",
]

EXPECTED_FULL_SERVICE_AIRLINES = [
    "Air China",
    "China Eastern",
    "China Southern",
    "United",
    "Delta",
    "American",
    "Air Canada",
    "Lufthansa",
    "ANA",
    "Japan Airlines",
    "Singapore Airlines",
    "Cathay Pacific",
]

CN_CITY_NAMES = (
    "上海", "北京", "广州", "深圳", "成都", "杭州", "南京", "厦门", "福州",
    "武汉", "西安", "重庆", "昆明", "青岛", "长沙", "郑州", "天津",
)


class AirlineNameListContractTest(unittest.TestCase):
    def test_lcc_airlines_list_is_exact_and_ascii_only(self):
        self.assertEqual(analyzer.LCC_AIRLINES, EXPECTED_LCC_AIRLINES)
        self.assertTrue(all(name.isascii() for name in analyzer.LCC_AIRLINES))

    def test_full_service_airlines_list_is_exact_and_ascii_only(self):
        self.assertEqual(analyzer.FULL_SERVICE_AIRLINES, EXPECTED_FULL_SERVICE_AIRLINES)
        self.assertTrue(all(name.isascii() for name in analyzer.FULL_SERVICE_AIRLINES))


class RouteIsDomesticCityNameTest(unittest.TestCase):
    def test_every_restored_city_name_counts_as_domestic(self):
        missed = [
            name
            for name in CN_CITY_NAMES
            if not _route_is_domestic({"origin": name, "destination": "北京"})
        ]
        self.assertEqual(missed, [])

    def test_foreign_city_name_is_not_domestic(self):
        self.assertFalse(_route_is_domestic({"origin": "上海", "destination": "大阪"}))

    def test_airport_code_paths_are_unchanged(self):
        self.assertTrue(
            _route_is_domestic({"origin_airports": ["PVG", "SHA"], "destination_airports": ["HGH"]})
        )
        self.assertFalse(
            _route_is_domestic({"origin_airports": ["PVG"], "destination_airports": ["KIX"]})
        )
        self.assertFalse(_route_is_domestic({}))


if __name__ == "__main__":
    unittest.main()
