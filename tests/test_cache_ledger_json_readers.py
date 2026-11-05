"""R27：另外 5 处自行读取 JSON 文件的生产代码沿用 read_json 的读取口径(R25/R26)。

基线(R26 合并后的 main，82665132，tree 0e5e856b)：
- main._fresh_cached_flights(当天往返备选缓存)用 except Exception 接住一切，但解析后不查嵌套层数，超过 100 层的缓存照常使用。
- basket_sentinel._load_state 已接住 (OSError, ValueError, RecursionError)(R13b)，同样不查嵌套层数。
- api_usage.load_usage_strict 只接住 OSError 与 JSONDecodeError：非法 UTF-8(UnicodeDecodeError)、超过 4300 位的整数(ValueError)
  与解析时嵌套过深(RecursionError)原样外抛，不是 UsageLedgerReadError，于是 record_actual_requests 不写冲突审计、
  load_usage_for_diagnostics 不报不健康而是直接抛错；超过 100 层的台账照常读出。
- JuheSource._read_cache 不接 RecursionError；内容为 null、数组等非对象时 payload.get 抛 AttributeError；二者都从 fetch 外抛，
  不发请求；超过 100 层照常命中。
- request_cache._read_persistent_payload 不接超长整数(ValueError)与 RecursionError；超过 100 层照常命中。
口径(用户 2026-11-04"开工另外 5 处生产代码自己读 JSON 文件…"，按建议口径)：五处都改用 atomic_json_store 的 parse_json_text，
四处把接住的异常改为 JSON_READ_ERRORS(main.py 保留原有的 except Exception)，读不了的内容各按原有退路处理：当天往返缓存与
两处请求缓存按未命中，哨兵状态按今日未通知，配额台账报 UsageLedgerReadError；聚合缓存不是对象时同样按未命中。其他异常
照旧外抛(main.py 除外)。带 UTF-8 BOM 的文件各处照旧按读不了处理。整数位数上限在测试内固定为 4300。
"""

from contextlib import contextmanager, redirect_stdout
from datetime import date, datetime
import io
import json
import os
from pathlib import Path
import socket
import sys
import tempfile
import types
import unittest
from unittest.mock import patch

import api_usage
import basket_sentinel
import main
import request_cache
from sources import juhe_source


BOM = b"\xef\xbb\xbf"
DEEP = "[" * 100 + "]" * 100
BIG_INT = b'{"n": ' + b"9" * 4301 + b"}"
PARSER_RECURSION = b"[" * 200000 + b"]" * 200000
INVALID_UTF8 = b'{"note": "\xff"}'
# R25 起读不了的两类内容(超长整数、解析时嵌套过深)与非法 UTF-8；名字 → (字节, 读取或解析时抛出的异常类型名)。
# 嵌套超过 100 层的内容依各读取方的格式另行构造(too_deep)。
UNREADABLE = {
    "big_int": (BIG_INT, "ValueError"),
    "parser_recursion": (PARSER_RECURSION, "RecursionError"),
    "invalid_utf8": (INVALID_UTF8, "UnicodeDecodeError"),
}
ALREADY_UNREADABLE = {"truncated": b'{"a": ', "bom": BOM + b"{}"}
NON_OBJECT = {"null": b"null", "array": b"[1]", "text": b'"x"'}
PERSIST_KEY = ("juhe", "PVG", "KIX", "2026-12-01", "1", "economy")
NOW = datetime(2026, 11, 4, 21, 0, 0)
NOW_TEXT = NOW.isoformat(timespec="seconds")


class FixedDateTime(datetime):
    """固定本机时钟：被测模块的 datetime.now() 一律返回 NOW(测试不读宿主时钟)。"""

    @classmethod
    def now(cls, tz=None):
        return NOW if tz is None else NOW.replace(tzinfo=tz)


@contextmanager
def default_int_digit_limit():
    previous = sys.get_int_max_str_digits()
    sys.set_int_max_str_digits(4300)
    try:
        yield
    finally:
        sys.set_int_max_str_digits(previous)


class SyntheticParseFailure(RuntimeError):
    pass


class TempDirTest(unittest.TestCase):
    def setUp(self):
        self.tmpdir = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmpdir.cleanup)
        self.dir = Path(self.tmpdir.name)
        limit = default_int_digit_limit()
        limit.__enter__()
        self.addCleanup(limit.__exit__, None, None, None)

    def write(self, path: Path, raw: bytes) -> Path:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(raw)
        return path


class FreshCachedFlightsTest(TempDirTest):
    def read(self, raw: bytes):
        path = self.write(self.dir / "same_day_fallback.json", raw)
        output = io.StringIO()
        with redirect_stdout(output), patch.object(main, "datetime", FixedDateTime):
            value = main._fresh_cached_flights(path)
        self.assertEqual(path.read_bytes(), raw)
        return value, output.getvalue()

    def test_over_deep_cache_is_not_used(self):
        raw = ('{"updated_at": "%s", "flights": [{"a": %s}]}' % (NOW_TEXT, DEEP)).encode("utf-8")
        value, output = self.read(raw)
        self.assertIsNone(value)
        self.assertIn("[当天往返备选] 缓存读取失败: JSON 嵌套超过 100 层", output)

    def test_fresh_cache_and_other_unreadable_contents_are_unchanged(self):
        value, output = self.read(('{"updated_at": "%s", "flights": [{"a": [1]}]}' % NOW_TEXT).encode("utf-8"))
        self.assertEqual(value, [{"a": [1]}])
        self.assertIn("[当天往返备选] 使用缓存", output)
        for name, raw in {**{key: item[0] for key, item in UNREADABLE.items()}, **ALREADY_UNREADABLE, **NON_OBJECT}.items():
            with self.subTest(case=name):
                value, output = self.read(raw)
                self.assertIsNone(value)
                self.assertIn("[当天往返备选] 缓存读取失败", output)


class SentinelStateTest(TempDirTest):
    def load(self, raw: bytes):
        path = self.write(self.dir / "basket_sentinel_state.json", raw)
        value = basket_sentinel._load_state(path)
        self.assertEqual(path.read_bytes(), raw)
        return value

    def test_over_deep_state_counts_as_not_notified_today(self):
        raw = ('{"last_notified_day": "2026-11-04", "x": %s}' % DEEP).encode("utf-8")
        self.assertEqual(self.load(raw), {})

    def test_valid_state_and_other_unreadable_contents_are_unchanged(self):
        self.assertEqual(self.load(b'{"last_notified_day": "2026-11-04"}'), {"last_notified_day": "2026-11-04"})
        for name, raw in {**{key: item[0] for key, item in UNREADABLE.items()}, **ALREADY_UNREADABLE, **NON_OBJECT}.items():
            with self.subTest(case=name):
                self.assertEqual(self.load(raw), {})


class UsageLedgerReadTest(TempDirTest):
    def setUp(self):
        super().setUp()
        self.ledger = self.dir / "api_usage.json"
        self.conflict_log = self.dir / "api_usage_conflict.log"

    def unreadable_ledgers(self):
        deep = ('{"version": 2, "dates": {}, "entries": [], "x": %s}' % DEEP).encode("utf-8")
        return {**UNREADABLE, "too_deep": (deep, "ValueError")}

    def test_unreadable_ledger_raises_the_ledger_read_error(self):
        for name, (raw, cause) in self.unreadable_ledgers().items():
            with self.subTest(case=name):
                self.write(self.ledger, raw)
                with self.assertRaises(api_usage.UsageLedgerReadError) as caught:
                    api_usage.load_usage_strict(self.ledger)
                self.assertTrue(str(caught.exception).startswith(f"配额台账读取失败: {self.ledger}: "))
                self.assertEqual(type(caught.exception.__cause__).__name__, cause)
                self.assertEqual(self.ledger.read_bytes(), raw)

    def test_diagnostics_report_unreadable_ledger_as_unhealthy(self):
        for name, (raw, _cause) in self.unreadable_ledgers().items():
            with self.subTest(case=name):
                self.write(self.ledger, raw)
                diagnostic = api_usage.load_usage_for_diagnostics(self.ledger)
                self.assertFalse(diagnostic["healthy"])
                self.assertIsNone(diagnostic["usage"])
                self.assertEqual(diagnostic["error_type"], "UsageLedgerReadError")
                self.assertTrue(diagnostic["error"].startswith("配额台账读取失败"))

    def test_recording_into_unreadable_ledger_leaves_reconciliation_evidence(self):
        for index, (name, (raw, _cause)) in enumerate(self.unreadable_ledgers().items()):
            with self.subTest(case=name):
                self.write(self.ledger, raw)
                self.conflict_log.unlink(missing_ok=True)
                with patch.object(api_usage, "safe_log"):
                    with self.assertRaises(api_usage.UsageLedgerReadError):
                        api_usage.record_actual_requests(
                            {"juhe": 2},
                            path=self.ledger,
                            round_id=f"r27-{index}",
                            day="2026-11-04",
                            recorded_at="2026-11-04T21:00:00+08:00",
                            conflict_log_path=self.conflict_log,
                        )
                self.assertEqual(self.ledger.read_bytes(), raw)
                evidence = api_usage.list_reconciliation_evidence(self.conflict_log)
                self.assertEqual([(row["round_id"], row["counts"], row["status"]) for row in evidence],
                                 [(f"r27-{index}", {"juhe": 2}, "pending_reconciliation")])
                self.assertTrue(evidence[0]["reason"].startswith("配额台账读取失败"))

    def test_valid_ledger_and_already_unreadable_contents_are_unchanged(self):
        self.write(self.ledger, b'{"version": 2, "dates": {}, "entries": [], "x": [[1]]}')
        self.assertEqual(api_usage.load_usage_strict(self.ledger)["version"], 2)
        for name, raw in ALREADY_UNREADABLE.items():
            with self.subTest(case=name):
                self.write(self.ledger, raw)
                with self.assertRaises(api_usage.UsageLedgerReadError) as caught:
                    api_usage.load_usage_strict(self.ledger)
                self.assertIsInstance(caught.exception.__cause__, json.JSONDecodeError)
        self.write(self.ledger, b"null")
        with self.assertRaisesRegex(api_usage.UsageLedgerReadError, "配额台账根节点不是对象"):
            api_usage.load_usage_strict(self.ledger)


class JuheCacheReadTest(TempDirTest):
    def setUp(self):
        super().setUp()
        for target, value in (("_cache_dir", lambda: self.dir), ("safe_log", lambda *args, **kwargs: None),
                              ("datetime", FixedDateTime)):
            patcher = patch.object(juhe_source, target, value)
            patcher.start()
            self.addCleanup(patcher.stop)
        self.source = juhe_source.JuheSource()
        self.path = self.source._cache_path("PVG", "KIX", "2026-12-01", "economy")

    def read(self, raw: bytes):
        self.write(self.path, raw)
        value = self.source._read_cache("PVG", "KIX", "2026-12-01", "economy")
        self.assertEqual(self.path.read_bytes(), raw)
        return value

    def test_unreadable_and_non_object_caches_are_misses(self):
        deep = ('{"fetched_at": "%s", "raw": {"error_code": 0, "result": {"flightInfo": [{"flightNo": "MU1", "x": %s}]}}}'
                % (NOW_TEXT, DEEP)).encode("utf-8")
        cases = {**{key: item[0] for key, item in UNREADABLE.items()}, **ALREADY_UNREADABLE, **NON_OBJECT, "too_deep": deep}
        for name, raw in cases.items():
            with self.subTest(case=name):
                self.assertIsNone(self.read(raw))

    def test_fresh_successful_cache_is_still_a_hit(self):
        raw = {"error_code": 0, "result": {"flightInfo": [{"flightNo": "MU1"}]}}
        value = self.read(json.dumps({"fetched_at": NOW_TEXT, "raw": raw}).encode("utf-8"))
        self.assertEqual(value, raw)

    def test_fetch_over_a_non_object_cache_requests_once_instead_of_failing(self):
        class FakeResponse:
            status_code = 200
            text = '{"error_code":281801}'

            def raise_for_status(self):
                return None

            def json(self):
                return {"error_code": 281801, "reason": "行程出发日期格式不正确或为空"}

        calls = []

        def fake_get(*args, **kwargs):
            calls.append((args, kwargs))
            return FakeResponse()

        self.write(self.path, b"null")
        no_network = patch.object(socket.socket, "connect", side_effect=AssertionError("juhe test attempted network access"))
        with no_network, patch.object(juhe_source, "shanghai_today", return_value=date(2026, 11, 4)), \
                patch.dict(os.environ, {"JUHE_FLIGHT_KEY": "test-key"}, clear=True), \
                patch.dict(sys.modules, {"requests": types.SimpleNamespace(get=fake_get)}):
            result = self.source.fetch("PVG", "KIX", "2026-12-01", "economy")
        self.assertEqual(len(calls), 1)
        self.assertEqual(result["source_status"], "invalid_date")


class PersistentCacheReadTest(TempDirTest):
    def read(self, raw: bytes):
        path = self.write(request_cache._cache_path(PERSIST_KEY, self.dir), raw)
        value = request_cache._read_persistent_payload(PERSIST_KEY, self.dir)
        self.assertEqual(path.read_bytes(), raw)
        return value

    def test_unreadable_payloads_are_misses(self):
        deep = ('{"fetched_at": "%s", "result": {"flights": [], "x": %s}}' % (NOW_TEXT, DEEP)).encode("utf-8")
        cases = {**{key: item[0] for key, item in UNREADABLE.items()}, **ALREADY_UNREADABLE, **NON_OBJECT, "too_deep": deep}
        for name, raw in cases.items():
            with self.subTest(case=name):
                self.assertIsNone(self.read(raw))
        payload = {"fetched_at": NOW_TEXT, "result": {"flights": [{"segments": [[1]]}]}}
        self.assertEqual(self.read(json.dumps(payload).encode("utf-8")), payload)



class EncodingAndOsErrorTest(TempDirTest):
    """合法且新鲜的内容前加 UTF-8 BOM、或字符串里夹一个非法 UTF-8 字节，以及 read_text 抛 OSError 时，五处都按读不了处理。

    用合法内容承载干扰，才能区分"按读不了处理"与"换成 utf-8-sig 或 errors=replace 读进来、恰好又被后面的字段检查挡下"。
    """

    def setUp(self):
        super().setUp()
        self.enterContext(patch.object(main, "datetime", FixedDateTime))
        for target, value in (("_cache_dir", lambda: self.dir), ("safe_log", lambda *args, **kwargs: None),
                              ("datetime", FixedDateTime)):
            self.enterContext(patch.object(juhe_source, target, value))
        self.juhe = juhe_source.JuheSource()
        self.cases = {
            "fallback_cache": (self.dir / "same_day_fallback.json",
                               '{"updated_at": "%s", "flights": [{"note": "NOTE"}]}' % NOW_TEXT, self.read_fallback),
            "sentinel_state": (self.dir / "basket_sentinel_state.json",
                               '{"last_notified_day": "2026-11-04", "note": "NOTE"}', self.read_sentinel),
            "usage_ledger": (self.dir / "api_usage.json",
                             '{"version": 2, "dates": {}, "entries": [], "note": "NOTE"}', self.read_ledger),
            "juhe_cache": (self.juhe._cache_path("PVG", "KIX", "2026-12-01", "economy"),
                           '{"fetched_at": "%s", "raw": {"error_code": 0, "result": {"flightInfo": [{"flightNo": "MU1", "note": "NOTE"}]}}}'
                           % NOW_TEXT, self.read_juhe),
            "persistent_cache": (request_cache._cache_path(PERSIST_KEY, self.dir),
                                 '{"fetched_at": "%s", "result": {"flights": [], "note": "NOTE"}}' % NOW_TEXT, self.read_persistent),
        }

    def read_fallback(self, path):
        with redirect_stdout(io.StringIO()):
            return main._fresh_cached_flights(path)

    def read_sentinel(self, path):
        return basket_sentinel._load_state(path) or None

    def read_ledger(self, path):
        try:
            return api_usage.load_usage_strict(path)
        except api_usage.UsageLedgerReadError:
            return None

    def read_juhe(self, path):
        return self.juhe._read_cache("PVG", "KIX", "2026-12-01", "economy")

    def read_persistent(self, path):
        return request_cache._read_persistent_payload(PERSIST_KEY, self.dir)

    def outcomes(self, encode):
        results = {}
        for name, (path, template, read) in self.cases.items():
            raw = encode(template)
            self.write(path, raw)
            with self.subTest(reader=name):
                results[name] = read(path) is not None
                self.assertEqual(path.read_bytes(), raw)
        return results

    def test_clean_fresh_contents_are_read(self):
        readable = self.outcomes(lambda template: template.replace("NOTE", "ok").encode("utf-8"))
        self.assertEqual(readable, dict.fromkeys(self.cases, True))

    def test_bom_before_fresh_content_is_unreadable(self):
        readable = self.outcomes(lambda template: BOM + template.replace("NOTE", "ok").encode("utf-8"))
        self.assertEqual(readable, dict.fromkeys(self.cases, False))

    def test_invalid_utf8_inside_fresh_content_is_unreadable(self):
        readable = self.outcomes(lambda template: template.encode("utf-8").replace(b"NOTE", b"\xff"))
        self.assertEqual(readable, dict.fromkeys(self.cases, False))

    def test_os_error_while_reading_is_unreadable(self):
        for name, (path, template, read) in self.cases.items():
            self.write(path, template.replace("NOTE", "ok").encode("utf-8"))
        readable = {}
        with patch.object(Path, "read_text", side_effect=PermissionError("SYNTHETIC_DENIAL")):
            for name, (path, _template, read) in self.cases.items():
                with self.subTest(reader=name):
                    readable[name] = read(path) is not None
        self.assertEqual(readable, dict.fromkeys(self.cases, False))

class UnlistedParseExceptionTest(TempDirTest):
    def test_other_exceptions_still_propagate_except_from_the_best_effort_fallback_cache(self):
        sentinel_path = self.write(self.dir / "basket_sentinel_state.json", b"{}")
        ledger = self.write(self.dir / "api_usage.json", b'{"version": 2, "dates": {}, "entries": []}')
        persist_path = self.write(request_cache._cache_path(PERSIST_KEY, self.dir), b"{}")
        self.assertTrue(persist_path.exists())
        with patch.object(juhe_source, "_cache_dir", lambda: self.dir):
            source = juhe_source.JuheSource()
            self.write(source._cache_path("PVG", "KIX", "2026-12-01", "economy"), b"{}")
            readers = (
                ("sentinel_state", lambda: basket_sentinel._load_state(sentinel_path)),
                ("usage_ledger", lambda: api_usage.load_usage_strict(ledger)),
                ("juhe_cache", lambda: source._read_cache("PVG", "KIX", "2026-12-01", "economy")),
                ("persistent_cache", lambda: request_cache._read_persistent_payload(PERSIST_KEY, self.dir)),
            )
            for name, read in readers:
                with self.subTest(reader=name):
                    with patch.object(json, "loads", side_effect=SyntheticParseFailure("synthetic")):
                        with self.assertRaises(SyntheticParseFailure):
                            read()
        fallback = self.write(self.dir / "same_day_fallback.json", b"{}")
        with patch.object(json, "loads", side_effect=SyntheticParseFailure("synthetic")), redirect_stdout(io.StringIO()) as output:
            self.assertIsNone(main._fresh_cached_flights(fallback))
        self.assertIn("[当天往返备选] 缓存读取失败: synthetic", output.getvalue())


if __name__ == "__main__":
    unittest.main()
