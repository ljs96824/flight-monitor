"""R26：反馈写入遇 null 不覆盖；三处自行解析 JSON 的读取方沿用 read_json 的读取口径(R25)。

基线(R25 合并后的 main，25a90cf5，tree 9efdd545)：
- web_form.save_feedback 的 mutator 把 None 当空数组，而 update_json 对"文件不存在"与"现有内容为 JSON null"都交来 None，
  于是内容为 null 的 feedback.json 被改写为只含这一条的数组；网页 /feedback 提交照常 200 并调用作者邮件通知(配置了
  FEEDBACK_NOTIFY_EMAIL 才发信)。load_feedback 对 null 按格式错误拒收。
- web_form._load_payload_result、plan_tracker.load_pushed_plans 与 runtime_backup._strict_json/_strict_jsonl 自行解析 JSON，
  只接住语法错误、OSError 与(前两处之一与后两处)解码错误：超过 4300 位的整数(ValueError)与解析时嵌套过深(RecursionError)
  原样外抛，超过 100 层的内容照常交出；load_pushed_plans 还不接非法 UTF-8，且把 null、数组等非对象内容原样交给按对象
  取 last_pushed 的调用方(track_plan_status 与通知生成)，在那里抛 AttributeError。
口径(用户 2026-11-03"开工web_form.save_feedback和read_json…"，按建议口径)：
- save_feedback 改传 missing=[]：文件不存在照旧新建；现有内容为 null 与其他非数组同属格式错误，抛 ValueError、不写入，
  网页 /feedback 因此与 feedback.json 无法解析时一样以服务器错误结束(测试模式下异常外抛)，这条反馈不保存、不发邮件。
- atomic_json_store 公开 JSON_READ_ERRORS(OSError、ValueError、RecursionError)与 parse_json_text(只接受 str，解析后检查
  嵌套层数，失败原样外抛、不记日志)；三处读取方改用二者，读不了的内容各按原有退路处理：详情载荷返回 None，已推送方案返回
  {}(非对象内容同样返回 {})，备份校验抛 RuntimeStateValidationError。其他异常照旧原样外抛。带 UTF-8 BOM 的文件与 read_json
  一样按读不了处理(基线已是如此，此处钉住，防止某一处改成 utf-8-sig 后口径分叉)。整数位数上限在测试内固定为 4300。
"""

from contextlib import contextmanager
import json
from pathlib import Path
import socket
import sys
import tempfile
import unittest
from unittest.mock import patch

import atomic_json_store
import plan_tracker
import runtime_backup
import web_form
from web_test_utils import enable_csrf


FEEDBACK_FORMAT_ERROR = "feedback.json 格式错误，应为反馈数组"
PAYLOAD_ID = "123e4567-e89b-12d3-a456-426614174260"
BOM = b"\xef\xbb\xbf"
BIG_INT = b'{"n": ' + b"9" * 4301 + b"}"
DEEP = b'{"x": ' + b"[" * 100 + b"]" * 100 + b"}"
PARSER_RECURSION = b"[" * 200000 + b"]" * 200000
UNREADABLE = (("big_int", BIG_INT, "ValueError"), ("too_deep", DEEP, "ValueError"),
              ("parser_recursion", PARSER_RECURSION, "RecursionError"))


@contextmanager
def default_int_digit_limit():
    previous = sys.get_int_max_str_digits()
    sys.set_int_max_str_digits(4300)
    try:
        yield
    finally:
        sys.set_int_max_str_digits(previous)


class TempDirTest(unittest.TestCase):
    def setUp(self):
        self.tmpdir = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmpdir.cleanup)
        self.dir = Path(self.tmpdir.name)
        limit = default_int_digit_limit()
        limit.__enter__()
        self.addCleanup(limit.__exit__, None, None, None)


class SyntheticParseFailure(RuntimeError):
    pass


class SharedReadPolicyTest(TempDirTest):
    def test_parse_json_text_follows_the_read_json_policy(self):
        self.assertEqual(atomic_json_store.JSON_READ_ERRORS, (OSError, ValueError, RecursionError))
        parse = atomic_json_store.parse_json_text
        self.assertEqual(parse('{"a": [1, {"b": null}]}'), {"a": [1, {"b": None}]})
        nested = parse("[" * 100 + "]" * 100)
        for _ in range(99):
            nested = nested[0]
        self.assertEqual(nested, [])
        with patch.object(atomic_json_store, "safe_log") as log:
            with self.assertRaisesRegex(ValueError, "JSON 嵌套超过 100 层"):
                parse("[" * 101 + "]" * 101)
            for name, raw, error in UNREADABLE:
                with self.subTest(case=name):
                    with self.assertRaises(atomic_json_store.JSON_READ_ERRORS) as caught:
                        parse(raw.decode("utf-8"))
                    self.assertEqual(type(caught.exception).__name__, error)
            with self.assertRaises(json.JSONDecodeError):
                parse(BOM.decode("utf-8") + "{}")
            for value in (None, b"{}", BOM + b"{}", bytearray(b"[]")):
                with self.subTest(non_text=type(value).__name__):
                    with self.assertRaises(TypeError):
                        parse(value)
        log.assert_not_called()

    def test_utf8_bom_is_unreadable_for_every_reader(self):
        state_path = self.dir / "state.json"
        state_path.write_bytes(BOM + b"{}")
        with patch.object(atomic_json_store, "safe_log"):
            with self.assertRaises(atomic_json_store.JsonStoreReadError) as caught:
                atomic_json_store.read_json(state_path)
        self.assertIsInstance(caught.exception.__cause__, json.JSONDecodeError)
        with patch.object(web_form, "PAGE_PAYLOADS_DIR", self.dir):
            (self.dir / f"{PAYLOAD_ID}.json").write_bytes(BOM + b'{"payload": {}}')
            self.assertIsNone(web_form._load_payload_result(PAYLOAD_ID))
        plans_path = plan_tracker._storage_path("sub-r26", self.dir)
        plans_path.parent.mkdir(parents=True, exist_ok=True)
        plans_path.write_bytes(BOM + b'{"last_pushed": {}}')
        self.assertEqual(plan_tracker.load_pushed_plans("sub-r26", self.dir), {})
        usage_path = self.dir / "api_usage.json"
        usage_path.write_bytes(BOM + b"{}")
        with self.assertRaises(runtime_backup.RuntimeStateValidationError) as caught:
            runtime_backup._strict_json(usage_path)
        self.assertEqual(str(caught.exception), "JSON解析失败: api_usage.json: JSONDecodeError")
        lines_path = self.dir / "signals_history.jsonl"
        lines_path.write_bytes(BOM + b'{"ok": 1}\n{"ok": 2}\n')
        with self.assertRaises(runtime_backup.RuntimeStateValidationError) as caught:
            runtime_backup._strict_jsonl(lines_path)
        self.assertEqual(str(caught.exception), "JSONL解析失败: signals_history.jsonl:1: JSONDecodeError")

    def test_unlisted_parse_exceptions_still_propagate_from_the_readers(self):
        payload_path = self.dir / f"{PAYLOAD_ID}.json"
        payload_path.write_text("{}", encoding="utf-8")
        plans_path = plan_tracker._storage_path("sub-r26", self.dir)
        plans_path.parent.mkdir(parents=True, exist_ok=True)
        plans_path.write_text("{}", encoding="utf-8")
        state_path = self.dir / "api_usage.json"
        state_path.write_text("{}", encoding="utf-8")
        lines_path = self.dir / "signals_history.jsonl"
        lines_path.write_text("{}\n", encoding="utf-8")
        readers = (
            ("detail_payload", lambda: web_form._load_payload_result(PAYLOAD_ID)),
            ("pushed_plans", lambda: plan_tracker.load_pushed_plans("sub-r26", self.dir)),
            ("strict_json", lambda: runtime_backup._strict_json(state_path)),
            ("strict_jsonl", lambda: runtime_backup._strict_jsonl(lines_path)),
        )
        with patch.object(web_form, "PAGE_PAYLOADS_DIR", self.dir):
            for name, read in readers:
                with self.subTest(reader=name):
                    with patch.object(json, "loads", side_effect=SyntheticParseFailure("synthetic")):
                        with self.assertRaises(SyntheticParseFailure):
                            read()

    def test_read_json_still_wraps_the_same_errors(self):
        path = self.dir / "state.json"
        with patch.object(atomic_json_store, "safe_log"):
            for name, raw, error in UNREADABLE:
                with self.subTest(case=name):
                    path.write_bytes(raw)
                    with self.assertRaises(atomic_json_store.JsonStoreReadError) as caught:
                        atomic_json_store.read_json(path)
                    self.assertEqual(type(caught.exception.__cause__).__name__, error)


@contextmanager
def _lock_then(action):
    real_lock = atomic_json_store.file_lock

    @contextmanager
    def lock(target, *args, **kwargs):
        with real_lock(target, *args, **kwargs):
            action(Path(target))
            yield

    with patch.object(atomic_json_store, "file_lock", lock):
        yield


class SaveFeedbackNullTest(TempDirTest):
    def setUp(self):
        super().setUp()
        self.path = self.dir / "feedback.json"
        patcher = patch.object(web_form, "FEEDBACK_PATH", self.path)
        patcher.start()
        self.addCleanup(patcher.stop)

    def saved(self):
        return json.loads(self.path.read_text(encoding="utf-8"))

    def assert_refused(self, raw):
        self.path.write_bytes(raw)
        with self.assertRaises(ValueError) as caught:
            web_form.save_feedback({"comment": "new"})
        self.assertEqual(str(caught.exception), FEEDBACK_FORMAT_ERROR)
        self.assertEqual(self.path.read_bytes(), raw)

    def test_null_is_refused_without_writing(self):
        for raw in (b"null", b"null\n", b" \r\n null\t\r\n"):
            with self.subTest(raw=raw):
                self.assert_refused(raw)

    def test_other_non_arrays_are_still_refused(self):
        for raw in (b"{}", b'"[]"', b"0", b"false"):
            with self.subTest(raw=raw):
                self.assert_refused(raw)

    def test_missing_file_is_created_and_arrays_append(self):
        web_form.save_feedback({"comment": "first"})
        self.assertEqual(self.saved(), [{"comment": "first"}])
        web_form.save_feedback({"comment": "second"})
        self.assertEqual(self.saved(), [{"comment": "first"}, {"comment": "second"}])

    def test_missing_versus_null_is_decided_inside_the_lock(self):
        with _lock_then(lambda target: target.write_bytes(b"null\n")):
            with self.assertRaises(ValueError):
                web_form.save_feedback({"comment": "new"})
        self.assertEqual(self.path.read_bytes(), b"null\n")
        self.path.write_text("[]", encoding="utf-8")
        with _lock_then(lambda target: target.unlink()):
            web_form.save_feedback({"comment": "new"})
        self.assertEqual(self.saved(), [{"comment": "new"}])

    def test_feedback_page_over_null_neither_saves_nor_notifies(self):
        no_network = patch.object(socket.socket, "connect", side_effect=AssertionError("feedback route attempted network access"))
        with patch.dict(web_form.app.config, {"TESTING": True}), no_network, \
                patch.object(web_form, "notify_feedback_author", return_value=False) as notify:
            client = web_form.app.test_client()
            enable_csrf(client, path="/feedback")
            self.path.write_bytes(b"null\n")
            with self.assertRaises(ValueError) as caught:
                client.post("/feedback", data={"subscription_id": PAYLOAD_ID, "feedback_type": "unavailable"})
            self.assertEqual(str(caught.exception), FEEDBACK_FORMAT_ERROR)
            notify.assert_not_called()
            self.assertEqual(self.path.read_bytes(), b"null\n")
            self.path.write_text("[]", encoding="utf-8")
            response = client.post("/feedback", data={"subscription_id": PAYLOAD_ID, "feedback_type": "unavailable"})
            self.assertEqual(response.status_code, 200)
            self.assertEqual(notify.call_count, 1)
            self.assertEqual([item["subscription_id"] for item in self.saved()], [PAYLOAD_ID])


class DetailPayloadReadTest(TempDirTest):
    def test_unreadable_payloads_are_treated_as_missing(self):
        with patch.object(web_form, "PAGE_PAYLOADS_DIR", self.dir):
            path = self.dir / f"{PAYLOAD_ID}.json"
            for name, raw, _error in UNREADABLE:
                with self.subTest(case=name):
                    path.write_bytes(raw)
                    self.assertIsNone(web_form._load_payload_result(PAYLOAD_ID))
                    self.assertEqual(path.read_bytes(), raw)
            path.write_text(json.dumps({"payload": {"x": [1, 2]}}), encoding="utf-8")
            self.assertEqual(web_form._load_payload_result(PAYLOAD_ID), {"payload": {"x": [1, 2]}})


class PushedPlansReadTest(TempDirTest):
    def write(self, raw):
        path = plan_tracker._storage_path("sub-r26", self.dir)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(raw)
        return path

    def test_unreadable_and_non_object_contents_load_as_no_record(self):
        cases = (*((name, raw) for name, raw, _error in UNREADABLE),
                 ("invalid_utf8", b'{"last_pushed": "\xff"}'), ("null", b"null"), ("array", b"[1]"), ("text", b'"x"'))
        for name, raw in cases:
            with self.subTest(case=name):
                path = self.write(raw)
                self.assertEqual(plan_tracker.load_pushed_plans("sub-r26", self.dir), {})
                self.assertIsNone(plan_tracker.track_plan_status("sub-r26", [], self.dir))
                self.assertEqual(path.read_bytes(), raw)

    def test_saved_plans_still_round_trip(self):
        payload = {"subscription_id": "sub-r26", "last_pushed": {"plan_a": {"flight_no": "MU515"}}}
        self.write(json.dumps(payload).encode("utf-8"))
        self.assertEqual(plan_tracker.load_pushed_plans("sub-r26", self.dir), payload)
        self.assertEqual(plan_tracker.load_pushed_plans("sub-missing", self.dir), {})


class BackupValidationReadTest(TempDirTest):
    def test_strict_json_reports_unreadable_contents_as_validation_errors(self):
        path = self.dir / "api_usage.json"
        for name, raw, error in UNREADABLE:
            with self.subTest(case=name):
                path.write_bytes(raw)
                with self.assertRaises(runtime_backup.RuntimeStateValidationError) as caught:
                    runtime_backup._strict_json(path)
                self.assertEqual(str(caught.exception), f"JSON解析失败: api_usage.json: {error}")
        path.write_text('{"ok": [1]}', encoding="utf-8")
        self.assertEqual(runtime_backup._strict_json(path), {"ok": [1]})

    def test_strict_jsonl_reports_the_offending_line(self):
        path = self.dir / "signals_history.jsonl"
        for name, raw, error in UNREADABLE:
            with self.subTest(case=name):
                path.write_bytes(b'{"ok": 1}\n\n' + raw + b"\n")
                with self.assertRaises(runtime_backup.RuntimeStateValidationError) as caught:
                    runtime_backup._strict_jsonl(path)
                self.assertEqual(str(caught.exception), f"JSONL解析失败: signals_history.jsonl:3: {error}")
        path.write_text('{"ok": 1}\n\n{"ok": [2]}\n', encoding="utf-8")
        self.assertEqual(runtime_backup._strict_jsonl(path), 2)


if __name__ == "__main__":
    unittest.main()
