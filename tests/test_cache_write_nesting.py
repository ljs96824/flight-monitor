"""R28：三处缓存写入方写之前按读取口径检查嵌套层数，读回时会被拒绝的内容不写入。

基线(R27 合并后的 main，9ffdf541，tree 698a4dad)：
- request_cache._write_persistent、JuheSource._write_cache 与当天往返备选缓存的写入(main._collect_same_day_fallback_flights)
  写之前不查嵌套层数：源返回超过 100 层的结果照样写入，R27 起读回时按未命中或读取失败，文件写了也用不上。
- atomic_json_store._check_nesting 只把对象与数组算作一层，遇到元组就不再往里数(元组及其中的一切都不计层)；json 序列化
  会把元组写成数组，于是 update_json 写入前的检查可能放过自己读不回的内容。
口径(用户 2026-11-05 选"只拦截写入")：
- atomic_json_store 公开 check_json_nesting，与 update_json 写入前、read_json 与 parse_json_text 读回后同一判定；
  _check_nesting 把元组也算一层(读取侧解析结果不含元组，不受影响)。
- 三处写入方写之前调用 check_json_nesting：超过 100 层时不写、记一行(当天往返备选缓存为打印一行)，原有文件不动；持久缓存
  不停用该目录，juhe 写入返回 False；本轮照常使用这份结果。读回仍按 R27 处理：没有可用旧文件时，源若持续返回这类结果，每个
  新进程仍会对该键请求一次(与 R27 相同，只是不再留下读不回的文件)；持久缓存留着的旧文件与写入失败时一样，有效期更长的调用方
  在有效期内仍可能命中这份更早的结果。含循环引用的内容也被这道检查挡下(按嵌套超过 100 层报告)。
本机时钟固定；嵌套用循环构造，检查不靠递归。
"""

from collections import OrderedDict, namedtuple
from contextlib import redirect_stdout
from datetime import datetime
import io
import json
from pathlib import Path
import socket
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

import atomic_json_store
import main
import request_cache
from sources import juhe_source
from tests import test_return_phase_processing as rp


NOW = datetime(2026, 11, 5, 9, 30, 0)
PERSIST_KEY = ("juhe", "PVG", "KIX", "2026-12-01", "1", "economy")
TOO_DEEP = "JSON 嵌套超过 100 层"


class FixedDateTime(datetime):
    """固定本机时钟：被测模块的 datetime.now() 一律返回 NOW(测试不读宿主时钟)。"""

    @classmethod
    def now(cls, tz=None):
        return NOW if tz is None else NOW.replace(tzinfo=tz)


def nested(levels: int, container=list):
    """levels 层嵌套的空容器，循环构造：nested(1) == []，nested(3) == [[[]]]。"""

    node = container()
    for _ in range(levels - 1):
        node = container([node])
    return node


class TempDirTest(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.dir = Path(temporary.name)

    def messages(self, log):
        return [call.args[0] for call in log.call_args_list]


class NestingCheckTest(unittest.TestCase):
    def test_lists_tuples_and_objects_each_count_as_one_level(self):
        check = atomic_json_store.check_json_nesting
        with patch.object(atomic_json_store, "safe_log") as log:
            for container in (list, tuple):
                with self.subTest(container=container.__name__):
                    check(nested(100, container))
                    with self.assertRaisesRegex(ValueError, TOO_DEEP):
                        check(nested(101, container))
            check({"a": [nested(98)]})
            with self.assertRaisesRegex(ValueError, TOO_DEEP):
                check({"a": (nested(99),)})
            innermost = [5, "x", None]
            for _ in range(99):
                innermost = [innermost]
            check(innermost)
            check("text")
            innermost = [{1, 2}, frozenset({3})]
            for _ in range(99):
                innermost = [innermost]
            check(innermost)
            with self.assertRaisesRegex(ValueError, TOO_DEEP):
                check(nested(200000))
        log.assert_not_called()

    def test_container_subclasses_count_like_their_base_types(self):
        Pair = namedtuple("Pair", "left right")

        class Items(list):
            pass

        for name, wrap in (("ordered_dict", lambda node: OrderedDict(x=node)), ("namedtuple", lambda node: Pair(node, 1)),
                           ("list_subclass", lambda node: Items([node]))):
            for levels, too_deep in ((100, False), (101, True)):
                with self.subTest(container=name, levels=levels):
                    # 外层数组(1) + levels-2 层 wrap + 最内层空数组(1) = levels 层
                    node = wrap([])
                    for _ in range(levels - 3):
                        node = wrap(node)
                    node = [node]
                    if too_deep:
                        with self.assertRaisesRegex(ValueError, TOO_DEEP):
                            atomic_json_store.check_json_nesting(node)
                    else:
                        atomic_json_store.check_json_nesting(node)
                    self.assertEqual(self.reads_back(node), not too_deep)

    def reads_back(self, payload):
        try:
            atomic_json_store.parse_json_text(json.dumps(payload))
        except ValueError:
            return False
        return True

    def test_check_agrees_with_reading_back_the_serialized_text(self):
        for levels, readable in ((100, True), (101, False)):
            for container in (list, tuple):
                with self.subTest(levels=levels, container=container.__name__):
                    payload = {"x": nested(levels - 1, container)}
                    try:
                        atomic_json_store.check_json_nesting(payload)
                        checked = True
                    except ValueError:
                        checked = False
                    try:
                        atomic_json_store.parse_json_text(json.dumps(payload))
                        parsed = True
                    except ValueError:
                        parsed = False
                    self.assertEqual((checked, parsed), (readable, readable))


class UpdateJsonTupleTest(TempDirTest):
    def test_deep_tuple_output_is_refused_and_shallow_tuples_are_written(self):
        path = self.dir / "state.json"
        path.write_text("{}", encoding="utf-8")
        with patch.object(atomic_json_store, "safe_log") as log:
            with self.assertRaisesRegex(ValueError, TOO_DEEP):
                atomic_json_store.update_json(path, lambda payload: {"x": nested(100, tuple)})
        self.assertEqual(path.read_bytes(), b"{}")
        self.assertEqual(len([text for text in self.messages(log) if text.startswith("[JSON存储] 写入失败")]), 1)
        self.assertEqual(list(self.dir.glob("*.tmp")), [])
        atomic_json_store.update_json(path, lambda payload: {"x": nested(99, tuple)})
        self.assertEqual(atomic_json_store.read_json(path), {"x": nested(99)})


class PersistentCacheWriteTest(TempDirTest):
    def setUp(self):
        super().setUp()
        request_cache.reset_for_tests(self.dir / "cache")
        self.addCleanup(request_cache.reset_for_tests, None)
        self.enterContext(patch.object(request_cache, "datetime", FixedDateTime))
        self.log = self.enterContext(patch.object(request_cache, "safe_log"))
        self.path = request_cache._cache_path(PERSIST_KEY)

    def result(self, levels):
        # 写入的整份内容：外层对象(1) + result 对象(2) + x 下 levels-2 层数组 = levels 层
        return {"flights": [], "x": nested(levels - 2)}

    def test_over_deep_result_is_not_written(self):
        request_cache._write_persistent(PERSIST_KEY, self.result(101))
        self.assertFalse(self.path.exists())
        self.assertEqual(request_cache._disabled_persistent_dirs, set())
        messages = self.messages(self.log)
        self.assertEqual(len(messages), 1)
        self.assertTrue(messages[0].startswith(f"[缓存] 结果不写入持久缓存 {self.path.name}: "))
        self.assertIn(TOO_DEEP, messages[0])

    def test_existing_file_survives_a_skip_and_the_directory_stays_enabled(self):
        request_cache._write_persistent(PERSIST_KEY, self.result(50))
        before = self.path.read_bytes()
        request_cache._write_persistent(PERSIST_KEY, self.result(101))
        self.assertEqual(self.path.read_bytes(), before)
        self.assertEqual(request_cache._read_persistent_payload(PERSIST_KEY)["result"], self.result(50))
        other = ("juhe", "PVG", "KIX", "2026-12-02", "1", "economy")
        request_cache._write_persistent(other, self.result(100))
        self.assertEqual(request_cache._read_persistent_payload(other)["result"], self.result(100))


class PersistentCacheFetchTest(TempDirTest):
    def setUp(self):
        super().setUp()
        self.enterContext(patch.object(socket.socket, "connect", side_effect=AssertionError("cache test attempted network access")))
        request_cache.reset_for_tests(self.dir / "cache")
        self.addCleanup(request_cache.reset_for_tests, None)
        for name in ("safe_log", "_record_observations_after_fetch", "_archive_listing_result", "_persist_api_usage_attempt"):
            self.enterContext(patch.object(request_cache, name))
        self.enterContext(patch.object(request_cache, "_panel_snapshot_for_key", return_value=None))
        self.enterContext(patch.object(request_cache, "datetime", FixedDateTime))

    def test_over_deep_fresh_result_is_used_but_not_persisted(self):
        flight = {"price": 100, "flight_no": "MU1", "x": nested(100)}
        source = SimpleNamespace(name="duffel", fetch=Mock(return_value={"source": "duffel", "flights": [flight]}))
        for _process in range(2):
            request_cache.reset_for_tests(self.dir / "cache")
            result = request_cache.cached_fetch(source, "PVG", "KIX", "2026-12-01", ttl_seconds=3600)
            self.assertEqual([item["flight_no"] for item in result["flights"]], ["MU1"])
        self.assertEqual(source.fetch.call_count, 2)
        self.assertEqual(list((self.dir / "cache").glob("*.json")), [])


class JuheCacheWriteTest(TempDirTest):
    def setUp(self):
        super().setUp()
        for target, value in (("_cache_dir", lambda: self.dir), ("datetime", FixedDateTime)):
            self.enterContext(patch.object(juhe_source, target, value))
        self.log = self.enterContext(patch.object(juhe_source, "safe_log"))
        self.source = juhe_source.JuheSource()
        self.path = self.source._cache_path("PVG", "KIX", "2026-12-01", "economy")

    def raw(self, levels):
        # 写入的整份内容：外层对象(1) + raw(2) + result(3) + flightInfo(4) + 航班对象(5) + x 下 levels-5 层数组 = levels 层
        return {"error_code": 0, "result": {"flightInfo": [{"flightNo": "MU1", "x": nested(levels - 5)}]}}

    def write(self, levels):
        return self.source._write_cache("PVG", "KIX", "2026-12-01", "economy", self.raw(levels))

    def test_over_deep_raw_is_not_written(self):
        self.assertIs(self.write(101), False)
        self.assertFalse(self.path.exists())
        messages = self.messages(self.log)
        self.assertEqual(len(messages), 1)
        self.assertTrue(messages[0].startswith(f"[缓存不写入] 源=juhe 文件={self.path.name} 原因="))
        self.assertIn(TOO_DEEP, messages[0])

    def test_boundary_raw_is_written_and_an_existing_file_survives_a_skip(self):
        self.assertIs(self.write(100), True)
        self.assertEqual(self.source._read_cache("PVG", "KIX", "2026-12-01", "economy"), self.raw(100))
        before = self.path.read_bytes()
        self.assertIs(self.write(101), False)
        self.assertEqual(self.path.read_bytes(), before)


class FallbackCacheWriteTest(TempDirTest):
    def setUp(self):
        super().setUp()
        self.path = self.dir / "same_day_fallback.json"
        self.enterContext(patch.object(main, "datetime", FixedDateTime))
        self.enterContext(patch.object(main, "_fallback_cache_path", return_value=self.path))

    def collect(self, levels):
        # 写入的整份内容：外层对象(1) + flights(2) + 航班对象(3) + x 下 levels-3 层数组 = levels 层
        flight = dict(rp.flight(True, 1300), x=nested(levels - 3))
        output = io.StringIO()
        with patch.object(main, "collect_for_airport_matrix", return_value={"flights": [flight]}), redirect_stdout(output):
            flights = main._collect_same_day_fallback_flights(None, ["PVG"], ["KIX"], "2099-10-01")
        return flights, output.getvalue()

    def test_over_deep_flights_are_used_but_not_cached(self):
        flights, output = self.collect(101)
        self.assertEqual([item["flight_combo"] for item in flights], ["MU101"])
        self.assertFalse(self.path.exists())
        self.assertEqual(output, f"[当天往返备选] 不写入缓存 {self.path}: {TOO_DEEP}\n")

    def test_an_existing_stale_file_survives_a_skip(self):
        stale = json.dumps({"updated_at": "2026-11-04T09:30:00", "date": "2099-10-01", "flights": []}).encode("utf-8")
        self.path.write_bytes(stale)
        flights, output = self.collect(101)
        self.assertEqual([item["flight_combo"] for item in flights], ["MU101"])
        self.assertEqual(self.path.read_bytes(), stale)
        self.assertEqual(output, f"[当天往返备选] 不写入缓存 {self.path}: {TOO_DEEP}\n")

    def test_boundary_flights_are_cached_and_read_back(self):
        flights, output = self.collect(100)
        self.assertIn(f"[当天往返备选] 已缓存 1 个航班到 {self.path}", output)
        with redirect_stdout(io.StringIO()):
            cached = main._fresh_cached_flights(self.path)
        self.assertEqual([item["flight_combo"] for item in cached], ["MU101"])


if __name__ == "__main__":
    unittest.main()
