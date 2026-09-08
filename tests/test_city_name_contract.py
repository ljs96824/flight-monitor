"""Lock airport display-name distinctions without changing legacy name levels."""
import unittest

from airports import AIRPORTS
from analyzer import IATA_CITY_NAMES, city_name


KNOWN_CITY_NAME_DIVERGENCES = {
    ("KIX", "大阪关西", "关西国际机场"): {
        "reason": "历史显示名称与主表name表达不同,本笔保持兼容",
        "todo": "统一名称来源时审计调用方需要的名称层级",
    },
    ("BKK", "曼谷", "曼谷素万那普"): {
        "reason": "历史城市级简称与主表机场名不同,本笔保持兼容",
        "todo": "裁决该场景显示城市名还是机场名",
    },
    ("SIN", "新加坡", "新加坡樟宜"): {
        "reason": "历史城市级简称与主表机场名不同,本笔保持兼容",
        "todo": "裁决该场景显示城市名还是机场名",
    },
}


class CityNameContractTest(unittest.TestCase):
    def test_ctu_tfu_match_master_names_and_are_distinct(self):
        self.assertEqual(city_name("CTU"), AIRPORTS["CTU"]["name"])
        self.assertEqual(city_name("TFU"), AIRPORTS["TFU"]["name"])
        self.assertNotEqual(city_name("CTU"), city_name("TFU"))

    def test_city_name_divergences_match_exact_registry(self):
        missing_codes = set(IATA_CITY_NAMES) - set(AIRPORTS)
        self.assertEqual(missing_codes, set())
        actual = {
            (code, label, AIRPORTS[code]["name"])
            for code, label in IATA_CITY_NAMES.items()
            if label != AIRPORTS[code]["name"]
        }
        self.assertEqual(actual, set(KNOWN_CITY_NAME_DIVERGENCES))


if __name__ == "__main__":
    unittest.main()
