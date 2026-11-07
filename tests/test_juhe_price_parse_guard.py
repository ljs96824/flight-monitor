"""R32：Juhe 无法解析的报价逐条隔离。

基线(a10725d)上 normalize() 对解析为 None 的价格执行 ``price <= 0``，抛
TypeError，整份响应里价格正常的条目一起丢失；新采路径因此不写缓存，下一轮同键重复付费；
缓存命中路径同样调 normalize()，缓存 raw 含坏价格条目时命中路径也抛错。

本文件锁定修复后的口径(裁定 2026-11-07)：
- 解析为 None(缺失/空/不可解析)的条目逐条跳过，每条记一行有界诊断；其余条目照常返回；
- 0 与负数维持静默跳过(兼容绿)，空串无有效别名属本笔修复输入(基线红)，两者分开锁定；
- _first() 选字段规则不变：ticketPrice="abc" 且 price=1000 时不新增"解析失败后再回退"；
- 新采全无效仍走既有 empty 分支不写缓存；缓存命中全无效保持 source_status="cache" 且 flights=[]；
- 缓存保存 raw 响应不清洗，raw_result_count 计数单位不变。
"""
import json
import os
import socket
import sys
import types
import unittest
from datetime import date, datetime
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch

from sources import juhe_source
from sources.juhe_source import JuheSource


NOW_TEXT = "2026-11-07T12:00:00"
REQUEST_ARGS = ("PVG", "KIX", "2026-12-01", "economy")
DIAGNOSTIC = "价格无法解析跳过"


class FixedDateTime(datetime):
    @classmethod
    def now(cls, tz=None):
        return cls.fromisoformat(NOW_TEXT)


def make_item(flight_no, **fields):
    item = {
        "flightNo": flight_no,
        "departureDate": "2026-12-01",
        "departureTime": "08:15",
        "arrivalDate": "2026-12-01",
        "arrivalTime": "10:25",
    }
    item.update(fields)
    return item


def raw_response(items):
    return {"resultcode": "200", "error_code": 0, "result": {"flightInfo": items}}


MIXED_ITEMS = [
    make_item("MU100"),                              # 无价格字段 -> None
    make_item("MU101", ticketPrice="abc"),           # 不可解析 -> None
    make_item("MU102", ticketPrice="￥4,883"),        # 4883.0
    make_item("MU103", ticketPrice=0),               # 静默跳过
    make_item("MU104", ticketPrice=1280),            # 1280.0
]


class GuardTestCase(unittest.TestCase):
    def setUp(self):
        self.tmp = TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.dir = Path(self.tmp.name)
        self.enterContext(patch.object(juhe_source, "_cache_dir", lambda: self.dir))
        self.enterContext(patch.object(juhe_source, "datetime", FixedDateTime))
        self.enterContext(patch.object(juhe_source, "shanghai_today", return_value=date(2026, 11, 4)))
        self.enterContext(patch.dict(os.environ, {"JUHE_FLIGHT_KEY": "test-key"}, clear=True))
        self.enterContext(patch.object(
            socket.socket, "connect",
            side_effect=AssertionError("juhe test attempted network access"),
        ))
        self.logs = []
        self.enterContext(patch.object(
            juhe_source, "safe_log",
            side_effect=lambda message: self.logs.append(str(message)),
        ))
        self.source = JuheSource()

    def diagnostics(self):
        return [line for line in self.logs if DIAGNOSTIC in line]

    def fetch(self, items):
        raw = raw_response(items)
        calls = []

        class FakeResponse:
            status_code = 200
            text = json.dumps(raw, ensure_ascii=False)

            def raise_for_status(self):
                return None

            def json(self):
                return raw

        def fake_get(*args, **kwargs):
            calls.append((args, kwargs))
            return FakeResponse()

        with patch.dict(sys.modules, {"requests": types.SimpleNamespace(get=fake_get)}):
            result = self.source.fetch(*REQUEST_ARGS)
        return result, calls

    def seed_cache(self, items):
        path = self.source._cache_path(*REQUEST_ARGS)
        payload = {"fetched_at": NOW_TEXT, "raw": raw_response(items)}
        path.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
        return path

    def cache_files(self):
        return [p for p in self.dir.iterdir() if p.name.startswith("juhe_") and p.suffix == ".json"]


class FreshMixedPricesTest(GuardTestCase):
    def test_valid_items_kept_and_each_bad_item_logged_once(self):
        result, calls = self.fetch(MIXED_ITEMS)

        self.assertEqual(len(calls), 1)
        self.assertEqual(result["source_status"], "success")
        self.assertEqual([f["flight_no"] for f in result["flights"]], ["MU102", "MU104"])
        self.assertEqual([f["price"] for f in result["flights"]], [4883.0, 1280.0])
        diagnostics = self.diagnostics()
        self.assertEqual(len(diagnostics), 2)
        self.assertTrue(any("MU100" in line for line in diagnostics))
        self.assertTrue(any("MU101" in line for line in diagnostics))

    def test_cache_write_keeps_raw_untouched_and_counts_parsed_items(self):
        result, _calls = self.fetch(MIXED_ITEMS)

        self.assertEqual(result["source_status"], "success")
        self.assertEqual(result["raw_result_count"], 5)
        self.assertEqual(len(result["raw"]["result"]["flightInfo"]), 5)
        files = self.cache_files()
        self.assertEqual(len(files), 1)
        stored = json.loads(files[0].read_text(encoding="utf-8"))
        self.assertEqual(len(stored["raw"]["result"]["flightInfo"]), 5)


class AliasPriorityTest(GuardTestCase):
    def normalize(self, item):
        with patch.object(juhe_source, "safe_log", side_effect=lambda message: self.logs.append(str(message))):
            return self.source.normalize([item], collected_at=NOW_TEXT)

    def test_empty_first_alias_falls_through_to_valid_price(self):
        flights = self.normalize(make_item("MU200", ticketPrice="", price="1,000"))

        self.assertEqual(len(flights), 1)
        self.assertEqual(flights[0]["price"], 1000.0)
        self.assertEqual(self.diagnostics(), [])

    def test_unparseable_first_alias_does_not_fall_back(self):
        flights = self.normalize(make_item("MU201", ticketPrice="abc", price=1000))

        self.assertEqual(flights, [])
        diagnostics = self.diagnostics()
        self.assertEqual(len(diagnostics), 1)
        self.assertIn("MU201", diagnostics[0])

    def test_all_aliases_missing_or_empty_skip_with_diagnostic(self):
        flights = self.normalize(make_item("MU202", ticketPrice="", price="", adultPrice=""))

        self.assertEqual(flights, [])
        self.assertEqual(len(self.diagnostics()), 1)


class FreshAllInvalidTest(GuardTestCase):
    def test_all_invalid_prices_yield_empty_status_without_cache_write(self):
        result, calls = self.fetch([make_item("MU300"), make_item("MU301", ticketPrice="abc")])

        self.assertEqual(len(calls), 1)
        self.assertEqual(result["source_status"], "empty")
        self.assertEqual(result["reason"], "HTTP成功但无有效方案")
        self.assertEqual(result["flights"], [])
        self.assertEqual(self.cache_files(), [])
        self.assertEqual(len(self.diagnostics()), 2)


class CacheHitTest(GuardTestCase):
    def no_http(self):
        return patch.dict(sys.modules, {"requests": types.SimpleNamespace(
            get=lambda *args, **kwargs: (_ for _ in ()).throw(AssertionError("unexpected HTTP call"))
        )})

    def test_mixed_cache_returns_valid_items_without_http(self):
        self.seed_cache(MIXED_ITEMS)
        with self.no_http():
            result = self.source.fetch(*REQUEST_ARGS)

        self.assertEqual(result["source_status"], "cache")
        self.assertEqual([f["flight_no"] for f in result["flights"]], ["MU102", "MU104"])
        self.assertEqual(len(self.diagnostics()), 2)

    def test_all_invalid_cache_keeps_cache_status_with_empty_flights(self):
        self.seed_cache([make_item("MU300"), make_item("MU301", ticketPrice="abc")])
        with self.no_http():
            result = self.source.fetch(*REQUEST_ARGS)

        self.assertEqual(result["source_status"], "cache")
        self.assertEqual(result["flights"], [])
        self.assertEqual(len(self.diagnostics()), 2)


class UnchangedValidResponseTest(GuardTestCase):
    CANONICAL = {
        "flightNo": "KN5978",
        "airline": "KN",
        "airlineName": "中国联合航空公司",
        "equipment": "73U",
        "departure": "PVG",
        "arrival": "PKX",
        "departureName": "上海浦东",
        "arrivalName": "北京大兴",
        "departureDate": "2026-06-10",
        "departureTime": "08:15",
        "arrivalDate": "2026-06-10",
        "arrivalTime": "10:25",
        "duration": "02h10m",
        "transferNum": 1,
        "ticketPrice": 527,
        "isCodeShare": False,
    }

    def test_valid_response_normalization_is_unchanged(self):
        result, _calls = self.fetch([dict(self.CANONICAL)])

        self.assertEqual(result["source_status"], "success")
        self.assertEqual(result["raw_result_count"], 1)
        self.assertEqual(self.diagnostics(), [])
        self.assertEqual(result["flights"], [{
            "flight_no": "KN5978",
            "flight_combo": "KN5978",
            "airline": "KN",
            "airline_name": "中国联合航空公司",
            "aircraft": "波音737-800",
            "aircraft_code": "73U",
            "departure_airport": "PVG",
            "arrival_airport": "PKX",
            "departure_name": "上海浦东",
            "arrival_name": "北京大兴",
            "departure_date": "2026-06-10",
            "arrival_date": "2026-06-10",
            "_source_raw_departure_time": "08:15",
            "departure_time": "2026-06-10 08:15",
            "arrival_time": "2026-06-10 10:25",
            "duration_str": "02h10m",
            "duration_min": 130,
            "total_duration_min": 130,
            "stops": 0,
            "transfer_num": 1,
            "price": 527,
            "ticket_price": 527,
            "is_codeshare": False,
            "data_source": "juhe",
            "source": "juhe",
            "source_role": "search",
            "domestic_realtime_quote": True,
            "collected_at": NOW_TEXT,
            "price_note": "票面价，实付以支付页为准",
            "price_includes": "票面价，不含机建燃油拆分",
            "segments": [{
                "flight_no": "KN5978",
                "airline": "中国联合航空公司",
                "dep_airport": "PVG",
                "arr_airport": "PKX",
                "dep_time": "2026-06-10 08:15",
                "arr_time": "2026-06-10 10:25",
                "aircraft": "波音737-800",
            }],
            "layovers": [],
            "raw": self.CANONICAL,
        }])


class ZeroNegativeSilentTest(GuardTestCase):
    def test_zero_and_negative_prices_skip_silently(self):
        result, _calls = self.fetch([make_item("MU400", ticketPrice=0), make_item("MU401", ticketPrice=-5)])

        self.assertEqual(result["source_status"], "empty")
        self.assertEqual(result["reason"], "HTTP成功但无有效方案")
        self.assertEqual(self.diagnostics(), [])


class CacheReadbackSingleHttpTest(GuardTestCase):
    def test_second_fetch_hits_cache_and_http_is_called_once(self):
        first, first_calls = self.fetch([make_item("MU500", ticketPrice="abc"), make_item("MU501", ticketPrice=990)])
        self.assertEqual(first["source_status"], "success")
        self.assertEqual(len(self.cache_files()), 1)

        second, second_calls = self.fetch([make_item("MU500", ticketPrice="abc"), make_item("MU501", ticketPrice=990)])

        self.assertEqual(second["source_status"], "cache")
        self.assertEqual([f["flight_no"] for f in second["flights"]], ["MU501"])
        self.assertEqual(len(first_calls) + len(second_calls), 1)


if __name__ == "__main__":
    unittest.main()
