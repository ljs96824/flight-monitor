"""R25：JSON 存储把超长整数与过深嵌套按读取失败处理(JsonStoreReadError)，写入同样不超过嵌套上限。

基线(R24b 合并后的 main，3fc6c66c，tree 6c32c17a)：atomic_json_store.read_json 只把 OSError、UnicodeError 与
JSONDecodeError 转成 JsonStoreReadError。数字超过整数位数上限(CPython 缺省 4300 位)时 json.load 抛普通 ValueError，
解析时嵌套过深(沙箱 2 万层以上)抛 RecursionError，二者原样外抛；能解析、嵌套约 500 层以上的内容(沙箱测得)由 read_json
原样交出，update_json 随后的 deepcopy 抛 RecursionError。按 JsonStoreReadError 降级的调用方(如源健康记录)因此直接失败。
口径(用户 2026-11-02"开工atomic_json_store.read_json"，按建议口径)：read_json 捕获 (OSError, ValueError, RecursionError)，
并在解析后不用递归地检查嵌套层数(对象与数组各算一层，标量不算)，超过 MAX_JSON_NESTING(100)层按 ValueError 处理；
三种情况都记一行"[JSON存储] 读取失败 …"并抛 JsonStoreReadError，与无法解析的 JSON 同口径，update_json 不写入。
update_json 写入前同样检查 mutator 返回的内容，超过上限时记一行"[JSON存储] 写入失败 …"、抛 ValueError、原文件不变
(独立复核后补：否则会写出自己读不回的文件)。其他异常(如 TypeError、MemoryError)照旧原样外抛。
代价：嵌套 101 层到约 490 层的内容以前能正常读写，本笔起一律拒绝；网页 /subscribe 遇超长整数由显示原始报错的 400 表单页
变为与无法解析的 JSON 相同的服务器错误(测试模式下异常外抛)。项目里各 JSON 状态文件实际只有个位数层。
整数位数上限在测试内固定为 CPython 缺省 4300(不受 PYTHONINTMAXSTRDIGITS 影响)。
"""

from contextlib import contextmanager
from copy import deepcopy
from datetime import datetime
import json
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

import atomic_json_store
from atomic_json_store import JsonStoreReadError, read_json, update_json
import health_check
import sync_subscriptions as sync
import web_form
from web_test_utils import enable_csrf


READ_FAILURE = "[JSON存储] 读取失败 path="
WRITE_FAILURE = "[JSON存储] 写入失败 path="
TOO_DEEP = "JSON 嵌套超过 100 层"


@contextmanager
def default_int_digit_limit():
    previous = sys.get_int_max_str_digits()
    sys.set_int_max_str_digits(4300)
    try:
        yield
    finally:
        sys.set_int_max_str_digits(previous)


def nested_lists(depth: int) -> bytes:
    return b"[" * depth + b"]" * depth


def nested_objects(depth: int) -> bytes:
    return b'{"a":' * (depth - 1) + b"{}" + b"}" * (depth - 1)


def big_int(digits: int) -> bytes:
    return b'{"n": ' + b"9" * digits + b"}"


def deep_list(depth: int) -> list:
    node: list = []
    for _ in range(depth - 1):
        node = [node]
    return node


class ReadLimitsTest(unittest.TestCase):
    def setUp(self):
        self.tmpdir = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmpdir.cleanup)
        self.dir = Path(self.tmpdir.name)
        self.path = self.dir / "state.json"
        self.logs = []
        patcher = patch.object(atomic_json_store, "safe_log", side_effect=self.logs.append)
        patcher.start()
        self.addCleanup(patcher.stop)

    def assert_read_error(self, raw, cause, fragment):
        self.path.write_bytes(raw)
        self.logs.clear()
        with self.assertRaises(JsonStoreReadError) as caught:
            read_json(self.path)
        self.assertIsInstance(caught.exception.__cause__, cause)
        self.assertIn(fragment, str(caught.exception))
        self.assertEqual(len(self.logs), 1, self.logs)
        self.assertTrue(self.logs[0].startswith(READ_FAILURE + str(self.path)), self.logs[0])
        self.assertIn(fragment, self.logs[0])

    def test_integer_over_the_digit_limit_is_a_read_error(self):
        with default_int_digit_limit():
            self.path.write_bytes(big_int(4300))
            self.assertEqual(read_json(self.path), {"n": int("9" * 4300)})
            self.assert_read_error(big_int(4301), ValueError, "4300")

    def test_parser_recursion_is_a_read_error(self):
        self.assert_read_error(nested_lists(200000), RecursionError, "RecursionError")

    def test_nesting_over_the_limit_is_a_read_error(self):
        self.assertEqual(atomic_json_store.MAX_JSON_NESTING, 100)
        for name, build in (("lists", nested_lists), ("objects", nested_objects)):
            with self.subTest(shape=name, depth=100):
                self.path.write_bytes(build(100))
                self.assertEqual(json.dumps(read_json(self.path), separators=(",", ":")).encode(), build(100))
            for depth in (101, 600):
                with self.subTest(shape=name, depth=depth):
                    self.assert_read_error(build(depth), ValueError, TOO_DEEP)

    def test_deep_branch_is_found_among_siblings(self):
        deep = nested_lists(100)
        cases = (
            ("first_item", b"[" + deep + b", 1, 2]"),
            ("middle_item", b"[1, " + deep + b", 2]"),
            ("last_item", b"[1, 2, " + deep + b"]"),
            ("object_first_key", b'{"x": ' + deep + b', "status": "active"}'),
            ("object_last_key", b'{"status": "active", "x": ' + deep + b"}"),
            ("record_in_list", b'[{"x": ' + nested_lists(599) + b', "status": "active"}]'),
        )
        for name, raw in cases:
            with self.subTest(case=name):
                self.assert_read_error(raw, ValueError, TOO_DEEP)

    def test_scalars_do_not_count_as_a_level(self):
        innermost_scalar = b"[" * 100 + b'1, "x", null' + b"]" * 100
        self.path.write_bytes(innermost_scalar)
        self.assertIsNotNone(read_json(self.path))
        self.path.write_text(json.dumps(list(range(50000))), encoding="utf-8")
        self.assertEqual(len(read_json(self.path)), 50000)
        self.assertEqual(self.logs, [])
        self.assert_read_error(b"[" * 100 + b"{}" + b"]" * 100, ValueError, TOO_DEEP)

    def test_mixed_nesting_counts_every_container(self):
        payload = {"a": [{"b": [[{"c": 1}]]}]}
        self.path.write_text(json.dumps(payload), encoding="utf-8")
        self.assertEqual(read_json(self.path), payload)
        self.assert_read_error(b'{"x": ' + nested_lists(100) + b"}", ValueError, TOO_DEEP)

    def test_wide_shallow_content_still_reads(self):
        payload = [{"id": index, "tags": ["a", "b"], "meta": {"n": index}} for index in range(20000)]
        self.path.write_text(json.dumps(payload), encoding="utf-8")
        self.assertEqual(read_json(self.path), payload)
        self.assertEqual(self.logs, [])

    def test_existing_read_errors_are_unchanged(self):
        cases = (
            ("broken_syntax", b"{broken", "JSONDecodeError"),
            ("bad_utf8", b'{"a": "\xff"}', "UnicodeDecodeError"),
        )
        for name, raw, fragment in cases:
            with self.subTest(case=name):
                self.assert_read_error(raw, ValueError, fragment)
        with self.assertRaises(JsonStoreReadError) as caught:
            read_json(self.dir / "missing.json")
        self.assertIsInstance(caught.exception.__cause__, OSError)

    def test_other_exceptions_are_not_wrapped(self):
        self.path.write_text("[]", encoding="utf-8")
        for error in (TypeError("not a read failure"), MemoryError()):
            with self.subTest(error=type(error).__name__):
                self.logs.clear()
                with patch.object(atomic_json_store.json, "load", side_effect=error):
                    with self.assertRaises(type(error)):
                        read_json(self.path)
                self.assertEqual(self.logs, [])

    def test_update_json_refuses_unreadable_contents_without_writing(self):
        cases = (
            ("big_int", big_int(4301)),
            ("parser_recursion", nested_lists(200000)),
            ("deep_but_parseable", nested_lists(600)),
        )
        with default_int_digit_limit():
            for name, raw in cases:
                with self.subTest(case=name):
                    self.path.write_bytes(raw)
                    calls = []
                    with self.assertRaises(JsonStoreReadError):
                        update_json(self.path, lambda current: calls.append(current) or {"rewritten": True})
                    self.assertEqual(calls, [])
                    self.assertEqual(self.path.read_bytes(), raw)

    def test_update_json_refuses_to_write_output_over_the_limit(self):
        self.path.write_text("[]", encoding="utf-8")
        before = self.path.read_bytes()
        for label, existing in (("existing_file", True), ("missing_file", False)):
            with self.subTest(file=label):
                target = self.path if existing else self.dir / "new.json"
                self.logs.clear()
                with self.assertRaises(ValueError) as caught:
                    update_json(target, lambda _current: deep_list(101), missing=[])
                self.assertEqual(str(caught.exception), TOO_DEEP)
                self.assertEqual(len(self.logs), 1, self.logs)
                self.assertTrue(self.logs[0].startswith(WRITE_FAILURE + str(target)), self.logs[0])
                if existing:
                    self.assertEqual(self.path.read_bytes(), before)
                else:
                    self.assertFalse(target.exists())
                self.assertEqual(list(self.dir.glob("*.tmp")), [])
        update_json(self.path, lambda _current: deep_list(100))
        self.assertEqual(read_json(self.path), deep_list(100))


REMOTE_RECORD = {"subscription_id": "123e4567-e89b-12d3-a456-426614174251", "origin": "上海", "destination": "大阪",
                 "depart_date": "2026-12-01"}


class SyncWriteLimitTest(unittest.TestCase):
    """本机同步摄入一条嵌套过深的远端记录时拒绝写入，本地文件保持可读(独立复核发现的场景)。"""

    def test_sync_refuses_a_remote_record_it_could_not_read_back(self):
        remote = [dict(REMOTE_RECORD, extra=deep_list(150))]
        with tempfile.TemporaryDirectory() as tmp:
            local = Path(tmp) / "subscriptions.json"
            local.write_text("[]", encoding="utf-8")
            before = local.read_bytes()
            with (
                patch.object(sync, "LOCAL_SUBSCRIPTIONS", local),
                patch.object(sync, "download_remote_subscriptions", side_effect=lambda: deepcopy(remote)),
                patch.object(sync, "safe_log"),
                patch.object(atomic_json_store, "safe_log"),
            ):
                with self.assertRaises(ValueError) as caught:
                    sync.sync_subscriptions()
            self.assertEqual(str(caught.exception), TOO_DEEP)
            self.assertEqual(local.read_bytes(), before)
            self.assertEqual(read_json(local), [])


class _FixedDateTime:
    @classmethod
    def now(cls):
        return datetime(2026, 11, 2, 12, 0, 0)


class SourceHealthDegradesTest(unittest.TestCase):
    """调用方按 JsonStoreReadError 的既有降级处理：源健康记录不再因这些内容失败，原字节不变。"""

    def setUp(self):
        self.tmpdir = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmpdir.cleanup)
        self.path = Path(self.tmpdir.name) / "source_health.json"
        self.logs = []
        for patcher in (
            patch.object(health_check, "SOURCE_HEALTH_PATH", self.path),
            patch.object(health_check, "datetime", _FixedDateTime),
            patch.object(health_check, "safe_log", side_effect=self.logs.append),
            patch.object(atomic_json_store, "safe_log", side_effect=self.logs.append),
        ):
            patcher.start()
            self.addCleanup(patcher.stop)

    def test_round_degrades_instead_of_failing(self):
        cases = (
            ("big_int", b'{"juhe": {"consecutive_failures": ' + b"9" * 5000 + b"}}"),
            ("parser_recursion", nested_lists(200000)),
            ("deep_but_parseable", b'{"juhe": ' + nested_lists(600) + b"}"),
        )
        stats = {"juhe": {"status": "failed", "count": 0}}
        with default_int_digit_limit():
            for name, raw in cases:
                for label, source_stats in (("with_stats", stats), ("without_stats", {"after_dedup": 0})):
                    with self.subTest(case=name, round=label):
                        self.path.write_bytes(raw)
                        self.logs.clear()
                        history, _warnings = health_check._update_source_history(source_stats)
                        expected = (
                            {"juhe": {"consecutive_failures": 1, "last_status": "failed", "last_count": 0,
                                      "updated_at": "2026-11-02T12:00:00"}}
                            if label == "with_stats" else {}
                        )
                        self.assertEqual(history, expected)
                        self.assertEqual(self.path.read_bytes(), raw)
                        self.assertEqual(sum(READ_FAILURE in str(line) for line in self.logs), 1, self.logs)
                        self.assertEqual(sum("source_health_state_degraded" in str(line) for line in self.logs), 1)


class WebSubscribeUnreadableFileTest(unittest.TestCase):
    """网页 /subscribe 遇读不了的订阅文件：超长整数与无法解析的 JSON 同样按服务器错误处理(测试模式下异常外抛)。"""

    def setUp(self):
        self.tmpdir = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmpdir.cleanup)
        self.path = Path(self.tmpdir.name) / "subscriptions.json"
        self.old_path = web_form.SUBSCRIPTIONS_PATH
        web_form.SUBSCRIPTIONS_PATH = self.path
        self.addCleanup(setattr, web_form, "SUBSCRIPTIONS_PATH", self.old_path)
        web_form.app.config.update(TESTING=True)
        self.client = web_form.app.test_client()
        enable_csrf(self.client)

    def test_create_and_numeric_edit_raise_the_read_error(self):
        contents = (("big_int", b"[" + big_int(4301) + b"]"), ("broken_syntax", b"{broken"))
        forms = (("create", {"form_page": "full"}), ("numeric_edit", {"form_page": "full", "subscription_index": "0"}))
        with default_int_digit_limit():
            for content_name, raw in contents:
                for form_name, form in forms:
                    with self.subTest(content=content_name, form=form_name):
                        self.path.write_bytes(raw)
                        with (
                            patch.object(web_form, "build_subscription", return_value=dict(REMOTE_RECORD)),
                            patch.object(web_form, "start_background_collection") as start_mock,
                            patch.object(web_form, "_remember_startup_handshake"),
                            patch.object(atomic_json_store, "safe_log"),
                        ):
                            with self.assertRaises(JsonStoreReadError):
                                self.client.post("/subscribe", data=form)
                        start_mock.assert_not_called()
                        self.assertEqual(self.path.read_bytes(), raw)


if __name__ == "__main__":
    unittest.main()
