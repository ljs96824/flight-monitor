"""R30：源结果里的孤立代理项在入口统一替换为 U+FFFD。

基线(R29 合并后的 main，c35bdbd1，tree 16d5c376)：JSON 转义(如 "\\ud83d"，半个 emoji)解析出的孤立代理项不能编码为
UTF-8——
- juhe 响应里只要有一个，写 juhe 缓存时抛 UnicodeEncodeError，这次采集记失败、不留缓存，每轮照付请求；
- 其他源(如 serpapi)的结果在 request_cache 写持久缓存时抛出：cached_fetch 向外抛，已有缓存文件被截成 0 字节，采集计划
  整体中断、后面的请求不再执行；不写持久缓存的结果(如空结果)在写轮次证据时同样抛出；观测库(SQLite)写入同样失败。
口径(用户 2026-11-06 选"入口统一替换")：atomic_json_store.replace_lone_surrogates 把经对象、数组与元组可达的字符串
(对象的键与值、数组与元组的元素)里的孤立代理项逐个替换为 U+FFFD，返回 (新值, 被替换的字符串个数)；没有时原样返回同一
对象；有时每个容器只复制一次，共享与循环引用照旧；不用递归。cached_fetch 取到结果后(成功与失败两条路)、juhe 解析响应后
先清洗再交给后续环节，有替换时各记一行。本机时钟固定；网络一律禁止；嵌套用循环构造。
"""

from collections import OrderedDict, namedtuple
from contextlib import closing
import copy
from datetime import datetime
import json
import os
from pathlib import Path
import socket
import sqlite3
import sys
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

import atomic_json_store


NOW = datetime(2026, 11, 6, 9, 30, 0)
HALF = json.loads('"\\ud83d"')  # 孤立的高代理项(半个 emoji)
LOW = json.loads('"\\ude00"')  # 孤立的低代理项
PAIR = json.loads('"\\ud83d\\ude00"')  # 成对的转义解析为一个完整字符 U+1F600，不算孤立
FFFD = chr(0xFFFD)
DEEP = 20_000  # 超过默认递归上限


class FixedDateTime(datetime):
    """固定本机时钟：被测模块的 datetime.now() 一律返回 NOW(测试不读宿主时钟)。"""

    @classmethod
    def now(cls, tz=None):
        return NOW if tz is None else NOW.replace(tzinfo=tz)


def nested(levels: int, leaf, kind=list):
    """levels 层嵌套的数组(或元组)，最里层放 leaf，循环构造：nested(1, x) == [x]，nested(2, x) == [[x]]。"""

    node = kind([leaf])
    for _ in range(levels - 1):
        node = kind([node])
    return node


def innermost(node, levels: int):
    for _ in range(levels - 1):
        node = node[0]
    return node


def replace_lone_surrogates(value):
    # 调用时再取，基线上没有这个函数时各用例照常收集、逐个失败
    return atomic_json_store.replace_lone_surrogates(value)


class ReplaceLoneSurrogatesTest(unittest.TestCase):
    def test_clean_values_are_returned_as_the_same_object(self):
        clean = {"airline_name": "东方航空", "emoji": PAIR, "items": ["x", 1, None, 2.5, True, (b"\xff", "日本")]}
        cycle = ["ok"]
        cycle.append(cycle)
        values = {"对象": clean, "字符串": "东方航空" + PAIR, "数字": 42, "None": None, "深层": nested(DEEP, "ok"), "循环": cycle}
        for label, value in values.items():
            out, replaced = replace_lone_surrogates(value)
            self.assertIs(out, value, label)
            self.assertEqual(replaced, 0, label)

    def test_lone_surrogates_in_keys_values_lists_and_tuples_are_replaced_one_by_one(self):
        value = {
            "airline_name": "东方航空" + HALF,
            "segments": [{"note": LOW + "x", "price": 1280}],
            "pair": ("ok", "MU" + HALF),
            "k" + HALF: 1,
            "split": HALF + LOW,
            "emoji": PAIR,
        }
        before = copy.deepcopy(value)
        out, replaced = replace_lone_surrogates(value)
        self.assertEqual(out, {
            "airline_name": "东方航空" + FFFD,
            "segments": [{"note": FFFD + "x", "price": 1280}],
            "pair": ("ok", "MU" + FFFD),
            "k" + FFFD: 1,
            "split": FFFD + FFFD,
            "emoji": PAIR,
        })
        self.assertEqual(replaced, 5)
        self.assertIs(type(out["pair"]), tuple)
        self.assertEqual(value, before)
        json.dumps(out, ensure_ascii=False).encode("utf-8")
        self.assertEqual(replace_lone_surrogates(HALF), (FFFD, 1))
        self.assertEqual(replace_lone_surrogates(((HALF,), [(HALF, (HALF,))])), (((FFFD,), [(FFFD, (FFFD,))]), 3))

    def test_containers_are_copied_as_builtin_types_and_later_colliding_keys_win(self):
        Pair = namedtuple("Pair", "left right")
        out, replaced = replace_lone_surrogates(OrderedDict([("a", HALF), ("b", Pair(HALF, 1))]))
        self.assertIs(type(out), dict)
        self.assertEqual((out, type(out["b"]), replaced), ({"a": FFFD, "b": (FFFD, 1)}, tuple, 2))
        self.assertEqual(replace_lone_surrogates({"a" + HALF: 1, "a" + LOW: 2}), ({"a" + FFFD: 2}, 2))
        self.assertEqual(replace_lone_surrogates({"a" + HALF: (HALF,), "a" + LOW: 2}), ({"a" + FFFD: 2}, 3))
        tuple_key = ("t" + HALF,)
        out, replaced = replace_lone_surrogates({tuple_key: 1, 1: HALF})
        self.assertEqual((out, replaced), ({tuple_key: 1, 1: FFFD}, 1))
        self.assertIs(next(iter(out)), tuple_key)

    def test_shared_references_and_cycles_stay_shared_in_the_copy(self):
        shared = [HALF]
        out, replaced = replace_lone_surrogates({"a": shared, "b": shared})
        self.assertIs(out["a"], out["b"])
        self.assertEqual((out["a"], replaced), ([FFFD], 1))
        cycle = [HALF]
        cycle.append(cycle)
        cycle.append(cycle)
        out, replaced = replace_lone_surrogates(cycle)
        self.assertEqual((out[0], replaced), (FFFD, 1))
        self.assertIs(out[1], out)
        self.assertIs(out[2], out)
        holder = []
        through_tuple = (holder, HALF)
        holder.append(through_tuple)
        out, replaced = replace_lone_surrogates(through_tuple)
        self.assertEqual((out[1], replaced), (FFFD, 1))
        self.assertIs(out[0][0], out)
        chain = (HALF,)
        for _ in range(60):
            chain = (chain, chain)
        out, replaced = replace_lone_surrogates(chain)
        self.assertEqual(replaced, 1)
        self.assertIs(out[0], out[1])
        self.assertEqual(innermost(out, 61), (FFFD,))

    def test_deep_nesting_is_cleaned_without_recursion(self):
        for kind in (list, tuple):
            out, replaced = replace_lone_surrogates(nested(DEEP, HALF, kind))
            self.assertEqual((innermost(out, DEEP), replaced), (kind([FFFD]), 1), kind.__name__)


class OfflineTest(unittest.TestCase):
    def setUp(self):
        self.enterContext(patch.dict(os.environ, {"NO_LIVE_API": "1", "PYTHON_DOTENV_DISABLED": "1"}, clear=True))
        self.enterContext(patch.dict(sys.modules, {"dotenv": SimpleNamespace(
            load_dotenv=lambda *a, **k: False, dotenv_values=lambda *a, **k: {})}))
        self.network = Mock(side_effect=AssertionError("EXTERNAL_NETWORK_FORBIDDEN"))
        self.enterContext(patch.object(socket.socket, "connect", self.network))
        self.enterContext(patch.object(socket.socket, "connect_ex", self.network))
        self.addCleanup(self.network.assert_not_called)
        import request_cache
        self.cache = request_cache
        temporary = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        request_cache.reset_for_tests(self.root / "cache")
        self.addCleanup(request_cache.reset_for_tests, None)
        self.enterContext(patch.object(request_cache, "datetime", FixedDateTime))
        self.logs = []
        self.enterContext(patch.object(request_cache, "safe_log", side_effect=self.logs.append))
        self.enterContext(patch.object(request_cache, "_persist_api_usage_attempt"))
        self.real_archive = request_cache._archive_listing_result
        self.archive = self.enterContext(patch.object(request_cache, "_archive_listing_result"))
        self.enterContext(patch.object(request_cache, "_panel_snapshot_for_key", return_value=None))

    def cleaning_lines(self):
        return [line for line in self.logs if line.startswith("[源结果清洗]")]

    def new_process(self, round_id):
        """模拟新进程：清空内存缓存与统计，持久缓存目录不变。"""

        self.cache.reset_for_tests(self.root / "cache")
        self.cache.start_request_cache_round(round_id)


class CachedFetchIngressTest(OfflineTest):
    def source(self, name, airline_name, **fetch):
        result = {"source": name, "source_status": "success",
                  "flights": [{"price": 2300, "flight_no": "JL892", "airline": "JL", "airline_name": airline_name}],
                  "raw": {"note": airline_name}}
        return SimpleNamespace(name=name, fetch=Mock(**(fetch or {"return_value": result})))

    def fetch(self, source, date_str="2099-09-01", **kwargs):
        return self.cache.cached_fetch(source, "PVG", "KIX", date_str, cabin_class="business", ttl_seconds=3600,
                                       include_cache_status=True, **kwargs)

    def test_dirty_result_is_cleaned_cached_and_reused_by_the_next_process(self):
        source = self.source("serpapi", "Japan Airlines " + HALF)
        path = self.cache._cache_path(self.cache.cache_key(source, "PVG", "KIX", "2099-09-01", None, "business"))
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text('{"old": true}', encoding="utf-8")
        self.new_process("r30-p1")
        result, status = self.fetch(source, force_fresh=True)
        self.assertEqual((status, result["flights"][0]["airline_name"], result["raw"]), (
            "fresh", "Japan Airlines " + FFFD, {"note": "Japan Airlines " + FFFD}))
        self.assertEqual(self.cleaning_lines(), [
            "[源结果清洗] 源=serpapi 航线=PVG->KIX 日期=2099-09-01 含孤立代理项的字符串 2 个，已替换为 U+FFFD"])
        stored = atomic_json_store.parse_json_text(path.read_text(encoding="utf-8"))
        self.assertEqual(stored["result"]["flights"][0]["airline_name"], "Japan Airlines " + FFFD)
        self.new_process("r30-p2")
        again, status = self.fetch(source)
        self.assertEqual((status, again["flights"][0]["airline_name"]), ("cache", "Japan Airlines " + FFFD))
        source.fetch.assert_called_once()

    def test_collection_plan_keeps_going_after_a_dirty_result(self):
        import collection_plan
        self.enterContext(patch.object(collection_plan, "safe_log"))
        dirty, clean = self.source("serpapi", "Japan Airlines " + HALF), self.source("juhe", "东方航空")
        self.new_process("r30-plan")
        plan = collection_plan.CollectionPlan(subscription_count=1)
        plan.add_request(dirty, "PVG", "KIX", "2099-09-02")
        plan.add_request(clean, "PVG", "KIX", "2099-09-03")
        plan.log_summary()
        plan.execute()
        dirty.fetch.assert_called_once()
        clean.fetch.assert_called_once()
        self.assertEqual(len(self.cleaning_lines()), 1)

    def test_observations_store_the_cleaned_text(self):
        import observations_store
        source = self.source("serpapi", "日本航空")
        source.fetch.return_value["flights"][0]["airline"] = "日本航空" + HALF
        self.new_process("r30-observe")
        database = self.root / "observations.sqlite3"
        observations_store.set_current_round("r30-observe", database)
        self.fetch(source)
        self.assertEqual([line for line in self.logs if line.startswith("[观测落库失败]")], [])
        with closing(sqlite3.connect(database)) as connection:
            rows = connection.execute("SELECT airline, flight_combo FROM observations").fetchall()
        self.assertEqual(rows, [("日本航空" + FFFD, "JL892")])

    def test_round_evidence_gets_the_cleaned_text(self):
        from log_utils import end_round_log_archive, start_round_log_archive
        self.archive.side_effect = self.real_archive
        source = SimpleNamespace(name="serpapi", fetch=Mock(return_value={
            "source": "serpapi", "source_status": "empty", "flights": [], "raw": {"note": "空结果" + HALF}}))
        self.new_process("r30-evidence")
        path = start_round_log_archive("r30-evidence", root_dir=self.root / "rounds", now=NOW)
        try:
            result, status = self.fetch(source)
        finally:
            end_round_log_archive(status="ok")
        self.assertEqual((status, result["raw"]), ("fresh", {"note": "空结果" + FFFD}))
        evidence = [line for line in path.read_text(encoding="utf-8").splitlines() if "[源响应证据]" in line]
        self.assertEqual(len(evidence), 1)
        self.assertIn("空结果" + FFFD, evidence[0])

    def test_failed_fetch_message_is_cleaned_before_pooling(self):
        source = self.source("serpapi", "", side_effect=RuntimeError("bad response " + HALF))
        self.new_process("r30-failed")
        first, status = self.fetch(source)
        self.assertEqual((status, first["source_status"], first["error"]), (
            "fresh", "failed", "RuntimeError:bad response " + FFFD))
        pooled, status = self.fetch(source)
        self.assertEqual((status, pooled["error"]), ("round_failed", "RuntimeError:bad response " + FFFD))
        source.fetch.assert_called_once()
        self.assertEqual(self.cleaning_lines(), [
            "[源结果清洗] 源=serpapi 航线=PVG->KIX 日期=2099-09-01 含孤立代理项的字符串 1 个，已替换为 U+FFFD"])

    def test_clean_result_passes_through_without_a_cleaning_line(self):
        source = self.source("serpapi", "Japan Airlines " + PAIR)
        self.new_process("r30-clean")
        result, _ = self.fetch(source)
        self.assertEqual(result["flights"][0]["airline_name"], "Japan Airlines " + PAIR)
        self.assertEqual(self.cleaning_lines(), [])


class JuheIngressTest(OfflineTest):
    RESPONSE = (
        '{"resultcode":"200","error_code":0,"result":{"flightInfo":[{"flightNo":"MU521","ticketPrice":"1280",'
        '"airline":"MU","airlineName":"东方航空\\ud83d","departure":"PVG","arrival":"KIX","departureDate":"2099-08-20",'
        '"departureTime":"09:00","arrivalDate":"2099-08-20","arrivalTime":"12:30","duration":"2h30m","transferNum":"1"}]}}'
    )
    CLEANING_LINE = "[juhe] PVG->KIX 2099-08-20 响应含孤立代理项的字符串 1 个，已替换为 U+FFFD"

    def setUp(self):
        super().setUp()
        from sources import juhe_source
        self.juhe = juhe_source
        self.juhe_logs = []
        self.enterContext(patch.object(juhe_source, "safe_log", side_effect=self.juhe_logs.append))
        self.enterContext(patch.object(juhe_source, "datetime", FixedDateTime))
        self.enterContext(patch.object(juhe_source, "_cache_dir", return_value=self.root / "juhe"))
        os.environ["JUHE_FLIGHT_KEY"] = "test-key"
        self.text = self.RESPONSE
        test = self

        class Response:
            status_code = 200

            @property
            def text(self):
                return test.text

            def raise_for_status(self):
                return None

            def json(self):
                return json.loads(test.text)

        self.get = Mock(return_value=Response())
        self.enterContext(patch.dict(sys.modules, {"requests": SimpleNamespace(get=self.get)}))

    def juhe_cleaning_lines(self):
        return [line for line in self.juhe_logs if "孤立代理项" in line]

    def test_dirty_response_is_cleaned_before_caching_and_parsing(self):
        source = self.juhe.JuheSource()
        first = source.fetch("PVG", "KIX", "2099-08-20")
        self.assertEqual((first["source_status"], first["flights"][0]["airline_name"]), ("success", "东方航空" + FFFD))
        self.assertEqual(self.juhe_cleaning_lines(), [self.CLEANING_LINE])
        cached = atomic_json_store.parse_json_text(
            (self.root / "juhe" / "juhe_PVG_KIX_2099-08-20_economy.json").read_text(encoding="utf-8"))
        self.assertEqual(cached["raw"]["result"]["flightInfo"][0]["airlineName"], "东方航空" + FFFD)
        second = source.fetch("PVG", "KIX", "2099-08-20")
        self.assertEqual((second["source_status"], second["flights"][0]["airline_name"]), ("cache", "东方航空" + FFFD))
        self.get.assert_called_once()

    def test_clean_response_logs_no_cleaning_line(self):
        self.text = self.RESPONSE.replace("\\ud83d", "")
        result = self.juhe.JuheSource().fetch("PVG", "KIX", "2099-08-20")
        self.assertEqual((result["source_status"], result["flights"][0]["airline_name"]), ("success", "东方航空"))
        self.assertEqual(self.juhe_cleaning_lines(), [])

    def test_three_processes_pay_for_one_request_and_all_succeed(self):
        outcomes = []
        for round_id in ("r30-juhe-1", "r30-juhe-2", "r30-juhe-3"):
            self.new_process(round_id)
            result = self.cache.cached_fetch(self.juhe.JuheSource(), "PVG", "KIX", "2099-08-20", ttl_seconds=3600)
            outcomes.append((result.get("source_status"), [flight.get("airline_name") for flight in result.get("flights") or []]))
        self.assertEqual(self.get.call_count, 1)
        self.assertEqual(outcomes[0], ("success", ["东方航空" + FFFD]))
        self.assertEqual([names for _, names in outcomes], [["东方航空" + FFFD]] * 3)
        self.assertEqual(self.juhe_cleaning_lines(), [self.CLEANING_LINE])
        self.assertEqual(self.cleaning_lines(), [])


if __name__ == "__main__":
    unittest.main()
