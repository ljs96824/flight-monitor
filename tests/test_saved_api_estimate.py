"""R22：前置跳过日志里的"省API"估算按舱位与机场的个数计数，不再对字符串按字符计数。

基线(R13b 合并后的 main，262f84b3，tree dcd0d488)：main._estimated_saved_api_calls 对舱位与机场字段直接取 len——
cabin_classes 写成字符串(R18 照旧接受，规范化后仍是字符串)时按字符计数，"economy" 让估算放大 7 倍、"business" 放大 8 倍；
机场字段写成字符串时同样按字符计数，"PVG"×"KIX" 让机场组合变成 9；列表里重复的舱位与机场重复计数，空项算独立舱位；
机场只读顶层字段，basic 里的机场被忽略，启用机场也不限于全部机场。本轮实际采集用 _subscription_airports 取机场
(collect_for_airport_matrix 的入参)，聚合器把单个字符串当一个舱位，采集计划与请求缓存键把空舱位当经济舱。
这个数只出现在"[订阅前置校验] … 结果=跳过 … 省API=…"日志行里，不影响采集、计划与配额。
口径(用户 2026-11-01"开工"，按建议口径)：机场改用 _subscription_airports(与本轮实际采集同一函数：单个字符串算一个机场、
不按逗号拆分、兼容 basic 字段与 dest_airports 别名、启用机场限于全部机场)，重复机场只算一次；舱位单个字符串算一个、
同一舱位只算一次、空值与空项按经济舱；启用外部源的计数(Juhe 不计、带密钥变量的源要配置了才计)与
"源 × 机场组合 × 舱位"的乘法不变。舱位计数辅助函数对任何写法都不抛错；整个估算对加载器放行的写法
(R18 保证 basic 是对象、机场与舱位只会是字符串、字符串列表或空值)不抛错。外部源配置与相关环境变量在测试内固定。
"""

from datetime import date
import os
import unittest
from unittest.mock import patch

import main


ONE_SOURCE = {"sources": [{"name": "juhe"}, {"name": "serpapi"}]}
TWO_SOURCES = {"sources": [{"name": "juhe"}, {"name": "serpapi"}, {"name": "duffel"}]}


def estimate(sub, profile=ONE_SOURCE, *, set_env=None, unset_env=()):
    with (
        patch.object(main, "get_source_profile", return_value=profile),
        patch.dict(os.environ, set_env or {}),
    ):
        for key in unset_env:
            os.environ.pop(key, None)
        return main._estimated_saved_api_calls(sub)


class SavedApiEstimateTest(unittest.TestCase):
    def test_string_fields_count_as_one_item(self):
        cases = (
            ("cabin_string_economy", {"origin": "PVG", "destination": "KIX", "cabin_classes": "economy"}, ONE_SOURCE, 1),
            ("cabin_string_business", {"origin": "PVG", "destination": "KIX", "cabin_classes": "business"}, ONE_SOURCE, 1),
            ("airport_strings", {"origin_airports": "PVG", "destination_airports": "KIX", "cabin_classes": ["economy"]},
             ONE_SOURCE, 1),
            ("active_airport_strings", {"origin_airports_active": "SHA", "destination_airports_active": "ITM"}, ONE_SOURCE, 1),
            ("comma_string_not_split", {"origin_airports": "PVG,SHA", "destination": "KIX"}, ONE_SOURCE, 1),
            ("two_sources_cabin_string", {"origin": "PVG", "destination": "KIX", "cabin_classes": "economy"}, TWO_SOURCES, 2),
        )
        for name, sub, profile, expected in cases:
            with self.subTest(case=name):
                self.assertEqual(estimate(sub, profile, set_env={"DUFFEL_TOKEN": "configured"}), expected)

    def test_duplicates_and_blank_cabins_count_once(self):
        cases = (
            ("duplicate_cabins", {"origin": "PVG", "destination": "KIX", "cabin_classes": ["economy", "economy"]}, 1),
            ("duplicate_mixed_cabins", {"origin": "PVG", "destination": "KIX",
                                        "cabin_classes": ["economy", "business", "economy"]}, 2),
            ("blank_item_is_economy", {"origin": "PVG", "destination": "KIX", "cabin_classes": ["", "economy"]}, 1),
            ("duplicate_airports", {"origin_airports": ["PVG", "PVG"], "destination_airports": ["KIX"]}, 1),
        )
        for name, sub, expected in cases:
            with self.subTest(case=name):
                self.assertEqual(estimate(sub), expected)

    def test_airports_follow_the_collection_inputs(self):
        cases = (
            ("basic_only", {"origin": "上海", "destination": "大阪",
                            "basic": {"origin_airports": ["PVG", "SHA"], "destination_airports": ["KIX"]}}, 2),
            ("basic_dest_airports_alias", {"origin": "PVG", "basic": {"dest_airports": ["KIX", "ITM"]}}, 2),
            ("active_limited_to_all", {"origin_airports": ["PVG", "SHA"], "origin_airports_active": ["SHA", "HGH"],
                                       "destination_airports": ["KIX"]}, 1),
            ("active_subset_of_all", {"origin_airports": ["PVG", "SHA"], "origin_airports_active": ["SHA"],
                                      "destination_airports": ["KIX"]}, 1),
            ("active_outside_all_kept", {"origin_airports": ["PVG"], "origin_airports_active": ["SHA", "HGH"],
                                         "destination_airports": ["KIX"]}, 2),
        )
        for name, sub, expected in cases:
            with self.subTest(case=name):
                self.assertEqual(estimate(sub), expected)

    def test_list_fields_and_source_counting_unchanged(self):
        lists = {"origin_airports": ["PVG", "SHA"], "destination_airports": ["KIX", "ITM"], "cabin_classes": ["economy", "business"]}
        one_cabin = {"origin": "PVG", "destination": "KIX", "cabin_classes": ["economy"]}
        self.assertEqual(estimate(lists), 8)
        self.assertEqual(estimate(one_cabin, TWO_SOURCES, set_env={"DUFFEL_TOKEN": "configured"}), 2)
        self.assertEqual(estimate(one_cabin, TWO_SOURCES, unset_env=("DUFFEL_TOKEN",)), 1, "KEYED_SOURCE_NEEDS_ITS_KEY")
        self.assertEqual(estimate(one_cabin, ONE_SOURCE, set_env={"JUHE_FLIGHT_KEY": "configured"}), 1, "JUHE_NOT_COUNTED")

    def test_defaults_and_loader_shapes_never_raise(self):
        cases = (
            ("no_cabins", {"origin": "PVG", "destination": "KIX"}, 1),
            ("empty_cabin_list", {"origin": "PVG", "destination": "KIX", "cabin_classes": []}, 1),
            ("no_airports", {}, 1),
            ("tuple_cabins", {"origin": "PVG", "destination": "KIX", "cabin_classes": ("economy", "business")}, 2),
            ("empty_basic", {"origin": "PVG", "destination": "KIX", "basic": {}}, 1),
        )
        for name, sub, expected in cases:
            with self.subTest(case=name):
                self.assertEqual(estimate(sub), expected)


class PreflightSkipLineTest(unittest.TestCase):
    """前置跳过日志行里的数字随之更正(两种跳过原因的行都用同一估算)。"""

    def skip_line(self, sub, preflight):
        lines = []
        with (
            patch.object(main, "get_source_profile", return_value=ONE_SOURCE),
            patch.object(main, "safe_log", side_effect=lines.append),
        ):
            main._log_preflight_skip(sub, preflight)
        self.assertEqual(len(lines), 1)
        return lines[0]

    def test_expired_line_counts_items(self):
        line = self.skip_line({"id": "s1", "origin": "PVG", "destination": "KIX", "cabin_classes": "economy"},
                              {"skip": True, "reason_code": "expired", "latest_date": date(2026, 1, 1)})
        self.assertIn("原因=全部采集日期已过期(最晚=2026-01-01)", line)
        self.assertTrue(line.endswith(" 省API=1"), line)

    def test_other_skip_line_counts_items(self):
        line = self.skip_line({"id": "s2", "origin_airports": "PVG", "destination_airports": "KIX",
                               "cabin_classes": "business"},
                              {"skip": True, "reason_code": "invalid_location", "reason": "地点无法解析"})
        self.assertIn("原因=地点无法解析", line)
        self.assertTrue(line.endswith(" 省API=1"), line)


if __name__ == "__main__":
    unittest.main()
