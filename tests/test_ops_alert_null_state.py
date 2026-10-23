"""R16：运维告警去重状态区分"文件不存在"与"现有内容为 JSON null"。

基线(R15 合并后的 main)：atomic_json_store.update_json 在文件锁内读取状态，文件不存在时给 mutator 传 None
(atomic_json_store.py:106)；已有文件内容是 JSON null 时，read_json 的解析结果同样是 None。ops_alert._claim_today 的 mutator
把 None 一律当作首次初始化(ops_alert.py:42–43)，于是现有 null 被改写成新状态并记 dedup=normal，与模块口径
"去重状态不可读或格式不对时降级，不修复、不覆盖损坏的状态文件"不符；[] 与坏 JSON 已正确降级。
口径(用户裁定 2026-10-22，采纳建议)：update_json 增加只能按关键字传入的 missing(文件不存在时 mutator 收到它的深拷贝，
缺省 None，其余调用方行为不变)；_claim_today 传 missing={}，只有文件确实不存在时才初始化；现有 null 与其他非对象值一样
进入 dedup_unavailable，本次调用最多尝试一次，原字节不动。区分在既有文件锁内完成，不在锁外判断文件是否存在。
测试一律使用假发送器，不调用真实 PushPlus。
"""

from contextlib import ExitStack
from datetime import datetime, timedelta, timezone
import json
import os
from pathlib import Path
import tempfile
import threading
import time
import unittest
from unittest.mock import Mock, patch

import atomic_json_store
import ops_alert


TOKEN_CANARY = "TEST_ONLY_OPS_PUSHPLUS_TOKEN_R16"
NOW = datetime(2026, 10, 22, 20, 5, tzinfo=timezone(timedelta(hours=8)))
LIVE_ENV = {"PUSHPLUS_TOKEN": TOKEN_CANARY}
NULL_VARIANTS = (b"null", b"null\n", b" null \r\n")


def alert(root: Path, *, sender, now=NOW):
    logs = []
    with ExitStack() as stack:
        stack.enter_context(patch.dict(os.environ, LIVE_ENV, clear=True))
        stack.enter_context(patch.object(ops_alert, "safe_log", side_effect=logs.append))
        result = ops_alert.alert_subscription_input_missing(
            root / "subscriptions.json", now=now, sender=sender, state_path=root / "ops_alert_state.json")
    return result, logs


class UpdateJsonMissingValueTest(unittest.TestCase):
    def test_default_still_passes_none_for_a_missing_file(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "state.json"
            seen = []
            result = atomic_json_store.update_json(path, lambda value: seen.append(value) or {"a": 1})
            stored = json.loads(path.read_text(encoding="utf-8"))
        self.assertEqual((seen, result, stored), ([None], {"a": 1}, {"a": 1}))

    def test_missing_value_is_a_fresh_copy_and_only_for_a_missing_file(self):
        default = {"items": []}

        def append(value):
            value["items"].append(1)
            return value

        with tempfile.TemporaryDirectory() as tmp:
            first, second = Path(tmp) / "first.json", Path(tmp) / "second.json"
            atomic_json_store.update_json(first, append, missing=default)
            atomic_json_store.update_json(second, append, missing=default)
            existing = Path(tmp) / "existing.json"
            existing.write_text('{"items": [7]}', encoding="utf-8")
            atomic_json_store.update_json(existing, append, missing=default)
            stored = [json.loads(p.read_text(encoding="utf-8")) for p in (first, second, existing)]
        self.assertEqual(default, {"items": []}, "DEFAULT_NOT_MUTATED")
        self.assertEqual(stored, [{"items": [1]}, {"items": [1]}, {"items": [7, 1]}])

    def test_existing_null_reaches_the_mutator_as_none_and_a_refusal_keeps_bytes(self):
        for options in ({}, {"missing": {}}):
            for data in NULL_VARIANTS:
                with self.subTest(missing="missing" in options, data=data), tempfile.TemporaryDirectory() as tmp:
                    path = Path(tmp) / "state.json"
                    path.write_bytes(data)
                    seen = []

                    def refuse(value):
                        seen.append(value)
                        raise ValueError("not an object")

                    with self.assertRaises(ValueError):
                        atomic_json_store.update_json(path, refuse, **options)
                    self.assertEqual(seen, [None])
                    self.assertEqual(path.read_bytes(), data, "NULL_BYTES_KEPT")
                    self.assertEqual([p.name for p in Path(tmp).iterdir() if p.name.endswith(".tmp")], [],
                                     "NO_TEMP_LEFT")


class OpsAlertNullStateTest(unittest.TestCase):
    def test_existing_null_degrades_to_one_attempt_per_call_without_overwriting(self):
        for data in NULL_VARIANTS:
            with self.subTest(data=data), tempfile.TemporaryDirectory() as tmp:
                root = Path(tmp)
                state_path = root / "ops_alert_state.json"
                state_path.write_bytes(data)
                sender = Mock(return_value=True)
                first, logs = alert(root, sender=sender)
                second, _ = alert(root, sender=sender)
                self.assertEqual(first, {"status": "sent", "dedup": "dedup_unavailable"})
                self.assertEqual(second, {"status": "sent", "dedup": "dedup_unavailable"})
                self.assertEqual(sender.call_count, 2, "ONE_ATTEMPT_PER_CALL")
                self.assertEqual(state_path.read_bytes(), data, "NULL_STATE_NOT_OVERWRITTEN")
                self.assertEqual(logs[0], "[运维告警] 去重状态不可用，本次调用最多尝试一次 原因=ValueError")
                self.assertEqual(logs[-1], "[运维告警] 订阅文件缺失 结果=sent 去重=dedup_unavailable")
                self.assertNotIn(TOKEN_CANARY, "".join(logs))

    def test_missing_file_and_empty_object_still_dedupe_normally(self):
        for name, data in (("missing", None), ("empty_object", b"{}")):
            with self.subTest(case=name), tempfile.TemporaryDirectory() as tmp:
                root = Path(tmp)
                state_path = root / "ops_alert_state.json"
                if data is not None:
                    state_path.write_bytes(data)
                sender = Mock(return_value=True)
                first, _ = alert(root, sender=sender)
                second, _ = alert(root, sender=sender, now=NOW + timedelta(hours=1))
                state = json.loads(state_path.read_text(encoding="utf-8"))
                self.assertEqual(first, {"status": "sent", "dedup": "normal"})
                self.assertEqual(second, {"status": "skipped", "dedup": "already_attempted_today"})
                self.assertEqual(sender.call_count, 1)
                key = f"subscriptions_missing|{(root / 'subscriptions.json').resolve()}"
                self.assertEqual(state, {key: {"date": "2026-10-22", "status": "sent",
                                               "attempted_at": "2026-10-22T20:05:00+08:00"}})

    def test_claim_and_record_never_write_over_null(self):
        with tempfile.TemporaryDirectory() as tmp:
            state_path = Path(tmp) / "ops_alert_state.json"
            state_path.write_bytes(b"null")
            with patch.object(atomic_json_store, "safe_log"):
                with self.assertRaises(ValueError):
                    ops_alert._claim_today(state_path, "subscriptions_missing|x", "2026-10-22", "2026-10-22T20:05:00+08:00")
                with self.assertRaises(ValueError):
                    ops_alert._record_result(state_path, "subscriptions_missing|x", "2026-10-22", "sent")
            self.assertEqual(state_path.read_bytes(), b"null")

    def test_concurrent_callers_on_null_state_each_attempt_once_and_keep_bytes(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            state_path = root / "ops_alert_state.json"
            state_path.write_bytes(b"null")
            started = threading.Barrier(2)
            sent, results = [], []

            def slow_sender(content, title):
                sent.append(title)
                time.sleep(0.2)
                return True

            def worker():
                started.wait(5)
                results.append(ops_alert.alert_subscription_input_missing(
                    root / "subscriptions.json", now=NOW, sender=slow_sender, state_path=state_path))

            with patch.dict(os.environ, LIVE_ENV, clear=True), patch.object(ops_alert, "safe_log"):
                threads = [threading.Thread(target=worker, name=f"ops-alert-null-{i}") for i in range(2)]
                for thread in threads:
                    thread.start()
                for thread in threads:
                    thread.join(30)
            kept = state_path.read_bytes()
        self.assertEqual(len(sent), 2, "EACH_CALL_AT_MOST_ONE_ATTEMPT")
        self.assertEqual([item["dedup"] for item in results], ["dedup_unavailable", "dedup_unavailable"])
        self.assertEqual(kept, b"null")


if __name__ == "__main__":
    unittest.main()
