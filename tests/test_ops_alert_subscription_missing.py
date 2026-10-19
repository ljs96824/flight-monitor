"""R9-2：订阅文件缺失时，不依赖订阅列表的 PushPlus 运维告警。

基线(R9-1 合并后的 main)：命令入口在订阅文件缺失时只记一行 [订阅输入缺失] 并以退出码 2 结束；系统告警只能经订阅自带的
通知渠道发出(main._notify_system_alert)，订阅为 0 时谁也通知不到。
口径(R9 定案 + Q4)：缺失时经全局 PushPlus(PUSHPLUS_TOKEN，无需收件人)尽力发一次告警；NO_LIVE_API=1 或未配置 token
时不尝试、不写去重状态，分别记 blocked_offline / not_configured。去重键为"原因 + 规范化路径"，上海自然日内最多自动尝试一次，
"检查并登记"在去重状态文件锁内完成。去重状态不可读或无法登记时降级：本次调用最多尝试一次并记 dedup_unavailable，不修复、
不覆盖损坏的状态文件。发送受理、发送失败、未配置分别记录；内容不含 token、订阅内容或异常原文；告警失败不改变退出码 2。
测试一律使用假发送器，不调用真实 PushPlus。
"""

from contextlib import ExitStack, redirect_stdout
from datetime import datetime, timedelta, timezone
import io
import json
import os
from pathlib import Path
import tempfile
import threading
import time
import unittest
from unittest.mock import Mock, patch

import main
import ops_alert
import sync_subscriptions as sync


TOKEN_CANARY = "TEST_ONLY_OPS_PUSHPLUS_TOKEN"
SECRET_CANARY = "TEST_ONLY_EXCEPTION_SECRET"
NOW = datetime(2026, 10, 19, 21, 5, tzinfo=timezone(timedelta(hours=8)))
LIVE_ENV = {"PUSHPLUS_TOKEN": TOKEN_CANARY}


def alert(root: Path, *, env=None, now=NOW, sender=None, state_path=None):
    logs = []
    with ExitStack() as stack:
        stack.enter_context(patch.dict(os.environ, env if env is not None else LIVE_ENV, clear=True))
        stack.enter_context(patch.object(ops_alert, "safe_log", side_effect=logs.append))
        result = ops_alert.alert_subscription_input_missing(
            root / "subscriptions.json", now=now, sender=sender,
            state_path=state_path if state_path is not None else root / "ops_alert_state.json")
    return result, logs


class OpsAlertGateTest(unittest.TestCase):
    def test_offline_and_unconfigured_do_not_attempt_or_write_state(self):
        cases = (
            ("no_live_api", {"NO_LIVE_API": "1", "PUSHPLUS_TOKEN": TOKEN_CANARY}, "blocked_offline"),
            ("no_token", {}, "not_configured"),
            ("blank_token", {"PUSHPLUS_TOKEN": "  "}, "not_configured"),
        )
        for name, env, expected in cases:
            with self.subTest(case=name), tempfile.TemporaryDirectory() as tmp:
                root = Path(tmp)
                sender = Mock(return_value=True)
                result, logs = alert(root, env=env, sender=sender)
                self.assertEqual(result, {"status": expected, "dedup": "not_used"})
                sender.assert_not_called()
                self.assertEqual(sorted(p.name for p in root.iterdir()), [], "no_state_written")
                self.assertEqual(logs, [f"[运维告警] 订阅文件缺失 结果={expected} 去重=not_used"])

    def test_default_sender_is_gated_pushplus_send(self):
        calls = []
        with patch("notifier.send", side_effect=lambda content, title: calls.append((title, content)) or True):
            self.assertIs(ops_alert._default_sender("内容", "标题"), True)
        self.assertEqual(calls, [("标题", "内容")])


class OpsAlertDedupTest(unittest.TestCase):
    def test_first_attempt_sends_once_and_records_result(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            sender = Mock(return_value=True)
            result, logs = alert(root, sender=sender)
            state = json.loads((root / "ops_alert_state.json").read_text(encoding="utf-8"))
        self.assertEqual(result, {"status": "sent", "dedup": "normal"})
        sender.assert_called_once()
        content, title = sender.call_args.args
        self.assertEqual(title, "[运维告警] 订阅文件缺失")
        self.assertIn("订阅文件缺失", content)
        self.assertIn(str((root / "subscriptions.json").resolve()), content)
        self.assertNotIn(TOKEN_CANARY, content + "".join(logs))
        key = f"subscriptions_missing|{(root / 'subscriptions.json').resolve()}"
        self.assertEqual(state, {key: {"date": "2026-10-19", "status": "sent", "attempted_at": "2026-10-19T21:05:00+08:00"}})
        self.assertEqual(logs[-1], "[运维告警] 订阅文件缺失 结果=sent 去重=normal")

    def test_same_day_is_skipped_next_day_sends_again(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            sender = Mock(return_value=True)
            first, _ = alert(root, sender=sender)
            second, logs = alert(root, sender=sender, now=NOW + timedelta(hours=2))
            third, _ = alert(root, sender=sender, now=NOW + timedelta(hours=3))  # 次日 00:05(上海)
        self.assertEqual([first["status"], second["status"], third["status"]], ["sent", "skipped", "sent"])
        self.assertEqual(second["dedup"], "already_attempted_today")
        self.assertEqual(sender.call_count, 2)
        self.assertEqual(logs[-1], "[运维告警] 订阅文件缺失 结果=skipped 去重=already_attempted_today")

    def test_failed_attempt_still_counts_for_the_day(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            failing = Mock(return_value=False)
            raising = Mock(side_effect=RuntimeError(SECRET_CANARY))
            first, _ = alert(root, sender=failing)
            other = Path(tmp) / "other"
            other.mkdir()
            second, logs = alert(other, sender=raising, state_path=root / "ops_alert_state.json")
            again, _ = alert(root, sender=failing)
            state = json.loads((root / "ops_alert_state.json").read_text(encoding="utf-8"))
        self.assertEqual(first, {"status": "send_failed", "dedup": "normal"})
        self.assertEqual(second, {"status": "send_failed", "dedup": "normal"})
        self.assertEqual(again["status"], "skipped")
        self.assertEqual(failing.call_count, 1)
        self.assertIn("[运维告警] 发送异常 原因=RuntimeError", logs)
        self.assertNotIn(SECRET_CANARY, "".join(logs))
        self.assertEqual(sorted(entry["status"] for entry in state.values()), ["send_failed", "send_failed"])

    def test_unusable_state_degrades_to_single_attempt_without_overwriting(self):
        for name, data in (("corrupt", b"{bad"), ("not_object", b"[]")):
            with self.subTest(case=name), tempfile.TemporaryDirectory() as tmp:
                root = Path(tmp)
                state_path = root / "ops_alert_state.json"
                state_path.write_bytes(data)
                sender = Mock(return_value=True)
                first, logs = alert(root, sender=sender)
                second, _ = alert(root, sender=sender)
                self.assertEqual(first, {"status": "sent", "dedup": "dedup_unavailable"})
                self.assertEqual(second, {"status": "sent", "dedup": "dedup_unavailable"})
                self.assertEqual(sender.call_count, 2, "one_attempt_per_call")
                self.assertEqual(state_path.read_bytes(), data, "damaged_state_not_overwritten")
                self.assertTrue(logs[0].startswith("[运维告警] 去重状态不可用，本次调用最多尝试一次 原因="))

    def test_concurrent_callers_send_only_once(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            started = threading.Barrier(2)
            sent = []

            def slow_sender(content, title):
                sent.append(title)
                time.sleep(0.2)
                return True

            results = []

            def worker():
                started.wait(5)
                results.append(ops_alert.alert_subscription_input_missing(
                    root / "subscriptions.json", now=NOW, sender=slow_sender,
                    state_path=root / "ops_alert_state.json"))

            with (
                patch.dict(os.environ, LIVE_ENV, clear=True),
                patch.object(ops_alert, "safe_log"),
            ):
                threads = [threading.Thread(target=worker, name=f"ops-alert-{i}") for i in range(2)]
                for thread in threads:
                    thread.start()
                for thread in threads:
                    thread.join(30)
        self.assertEqual(len(sent), 1, "exactly_one_attempt")
        self.assertEqual(sorted(item["status"] for item in results), ["sent", "skipped"])


class EntryIntegrationTest(unittest.TestCase):
    def test_run_cli_alerts_and_keeps_exit_code(self):
        cases = (
            ("sent", Mock(return_value={"status": "sent", "dedup": "normal"})),
            ("alert_raises", Mock(side_effect=RuntimeError(SECRET_CANARY))),
        )
        for name, alert_mock in cases:
            with self.subTest(case=name), tempfile.TemporaryDirectory() as tmp:
                path = Path(tmp) / "subscriptions.json"
                logs = []
                with (
                    patch.object(main, "SUBSCRIPTIONS_PATH", path),
                    patch.object(main, "alert_subscription_input_missing", alert_mock),
                    patch.object(main, "run") as run,
                    patch.object(main, "safe_log", side_effect=logs.append),
                ):
                    code = main.run_cli()
                self.assertEqual(code, 2)
                run.assert_not_called()
                alert_mock.assert_called_once_with(path)
                self.assertTrue(logs[0].startswith("[订阅输入缺失] "))
                self.assertNotIn(SECRET_CANARY, "".join(logs))
                if name == "alert_raises":
                    self.assertEqual(logs[-1], "[运维告警] 调用失败 原因=RuntimeError")

    def test_sync_entry_alerts_and_keeps_exit_code(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "subscriptions.json"
            alert_mock = Mock(side_effect=RuntimeError(SECRET_CANARY))
            logs = []
            with (
                patch.object(sync, "LOCAL_SUBSCRIPTIONS", path),
                patch.object(sync, "alert_subscription_input_missing", alert_mock),
                patch.object(sync, "download_remote_subscriptions") as download,
                patch.object(sync, "safe_log", side_effect=logs.append),
            ):
                code = sync.main()
        self.assertEqual(code, 2)
        download.assert_not_called()
        alert_mock.assert_called_once_with(path)
        self.assertEqual(logs[-1], "[运维告警] 调用失败 原因=RuntimeError")

    def test_real_alert_under_no_live_api_writes_nothing(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            path = root / "subscriptions.json"
            state_path = root / "ops_alert_state.json"
            with (
                patch.dict(os.environ, {"NO_LIVE_API": "1", "PUSHPLUS_TOKEN": TOKEN_CANARY}, clear=True),
                patch.object(main, "SUBSCRIPTIONS_PATH", path),
                patch.object(ops_alert, "OPS_ALERT_STATE_PATH", state_path),
                patch("notifier.send", side_effect=AssertionError("unexpected send")),
                redirect_stdout(io.StringIO()) as output,
            ):
                code = main.run_cli()
            created = sorted(p.name for p in root.iterdir())
        self.assertEqual(code, 2)
        self.assertEqual(created, [])
        self.assertIn("[运维告警] 订阅文件缺失 结果=blocked_offline 去重=not_used", output.getvalue())
        self.assertNotIn(TOKEN_CANARY, output.getvalue())


if __name__ == "__main__":
    unittest.main()
