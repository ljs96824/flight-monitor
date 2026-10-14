"""失败通知分渠道：采集失败通知与系统告警逐渠道发送、逐渠道记录。

取值与推送渠道日志一致：None=未尝试；False=已尝试但未被受理(含未填邮箱、发送抛错)；True=已被受理。
一个渠道抛错不再连带跳过另一个渠道；返回值恒为 bool，与记录里的 any_sent 同义。
"""

import io
import json
import unittest
from contextlib import redirect_stdout
from unittest.mock import patch

import main


FAILURE_PREFIX = "[失败通知渠道] "
ALERT_PREFIX = "[告警渠道] "
EMAIL = "offline@example.invalid"


def _effect(value):
    return {"side_effect": value} if isinstance(value, Exception) else {"return_value": value}


def _run(call, *, email_result=True, push_result=True):
    stdout = io.StringIO()
    with (
        patch.object(main, "send_email", **_effect(email_result)) as send_email,
        patch.object(main, "send", **_effect(push_result)) as send_push,
        redirect_stdout(stdout),
    ):
        result = call()
    lines = stdout.getvalue().splitlines()
    return result, lines, (send_email.call_count, send_push.call_count)


def _records(lines, prefix):
    return [json.loads(line[len(prefix):]) for line in lines if line.startswith(prefix)]


class SubscriptionFailureChannelTest(unittest.TestCase):
    CASES = (
        # 名称, 方式, 邮箱, 邮件结果, 推送结果, 期望返回, 期望调用(邮件, 推送), 期望(email_sent, pushplus_sent), 期望 *_error 字段
        ("email_raises_push_accepted", "both", EMAIL, OSError("smtp down"), True,
         True, (1, 1), (False, True), {"email_error": "OSError"}),
        ("email_raises_push_rejected", "both", EMAIL, OSError("smtp down"), False,
         False, (1, 1), (False, False), {"email_error": "OSError"}),
        ("email_accepted_push_raises", "both", EMAIL, True, RuntimeError("push down"),
         True, (1, 1), (True, False), {"pushplus_error": "RuntimeError"}),
        ("email_rejected_push_raises", "both", EMAIL, False, RuntimeError("push down"),
         False, (1, 1), (False, False), {"pushplus_error": "RuntimeError"}),
        ("both_accepted", "both", EMAIL, True, True, True, (1, 1), (True, True), None),
        ("both_rejected", "both", EMAIL, False, False, False, (1, 1), (False, False), None),
        ("truthy_non_bool_push", "pushplus", EMAIL, True, "accepted", True, (0, 1), (None, True), None),
        ("email_only_without_address", "email", "", True, True, False, (0, 0), (False, None), None),
        ("pushplus_only_raises", "pushplus", EMAIL, True, RuntimeError("push down"),
         False, (0, 1), (None, False), {"pushplus_error": "RuntimeError"}),
        ("page_only", "page_only", EMAIL, True, True, False, (0, 0), (None, None), None),
    )

    def test_channel_matrix(self):
        for name, method, email, email_result, push_result, expected, calls, channels, errors in self.CASES:
            with self.subTest(case=name):
                sub = {"id": "00000000-0000-4000-8000-000000000079", "origin": "PVG", "destination": "KIX",
                       "notification_goals": {"method": method, "email": email}}
                result, lines, sends = _run(
                    lambda: main._notify_subscription_failure(sub, reason="synthetic failure"),
                    email_result=email_result, push_result=push_result)
                self.assertIs(result, expected, "bool_result")
                self.assertEqual(sends, calls, "channel_calls")
                self.assertIs("last_failure" in sub, not expected, "last_failure_only_when_unsent")
                records = _records(lines, FAILURE_PREFIX)
                self.assertEqual(len(records), 1, "one_channel_record")
                record = records[0]
                self.assertEqual(record["subscription"], "00000000-0000-4000-8000-000000000079")
                self.assertEqual(record["route"], "PVG->KIX")
                self.assertEqual(record["method"], method)
                self.assertEqual((record["email_sent"], record["pushplus_sent"]), channels)
                self.assertIs(record["any_sent"], result, "any_sent_matches_return")
                self.assertEqual({key: value for key, value in record.items() if key.endswith("_error")}, errors or {})
                for field in (errors or {}):
                    channel = field[:-len("_error")]
                    self.assertTrue(any(line.startswith(f"[失败通知] 发送失败 渠道={channel}: ") for line in lines),
                                    "channel_error_line")

    def test_message_and_title_unchanged(self):
        sub = {"origin": "PVG", "destination": "KIX", "notification_goals": {"method": "both", "email": EMAIL}}
        content = "本次采集失败: PVG->KIX<br>原因: synthetic failure<br>订阅已保留,下轮自动重试。"
        stdout = io.StringIO()
        with (
            patch.object(main, "send_email", side_effect=OSError("smtp down")) as send_email,
            patch.object(main, "send", return_value=True) as send_push,
            redirect_stdout(stdout),
        ):
            main._notify_subscription_failure(sub, reason="synthetic failure")
        send_email.assert_called_once_with(EMAIL, "【航班监控采集失败】PVG->KIX", content, {})
        send_push.assert_called_once_with(content, title="【航班监控采集失败】PVG->KIX")

    def test_record_failure_never_changes_result(self):
        sub = {"origin": "PVG", "destination": "KIX", "notification_goals": {"method": "pushplus"}}
        with patch.object(main, "safe_log_json", side_effect=ValueError("boom")):
            result, lines, _sends = _run(lambda: main._notify_subscription_failure(sub, reason="x"))
        self.assertIs(result, True)
        self.assertIn("[失败通知渠道] 记录失败: ValueError", lines)


class SystemAlertChannelTest(unittest.TestCase):
    @staticmethod
    def subs(*methods):
        return [{"id": f"00000000-0000-4000-8000-00000000008{index}",
                 "notification_goals": {"method": method, "email": EMAIL}}
                for index, method in enumerate(methods)]

    def test_email_exception_no_longer_skips_pushplus(self):
        result, lines, sends = _run(lambda: main._notify_system_alert(self.subs("both", "pushplus"), "T", "C"),
                                    email_result=OSError("smtp down"), push_result=True)
        self.assertIs(result, True)
        self.assertEqual(sends, (1, 1), "first_subscription_pushplus_attempted")
        records = _records(lines, ALERT_PREFIX)
        self.assertEqual(len(records), 1, "stops_at_first_accepted")
        self.assertEqual((records[0]["email_sent"], records[0]["pushplus_sent"]), (False, True))
        self.assertEqual(records[0]["email_error"], "OSError")
        self.assertNotIn("pushplus_error", records[0])
        self.assertIn("[篮子哨兵] 通知渠道失败 渠道=email 原因=OSError:smtp down", lines)

    def test_falls_through_to_next_subscription(self):
        stdout = io.StringIO()
        with (
            patch.object(main, "send_email", side_effect=OSError("smtp down")),
            patch.object(main, "send", side_effect=[False, True]) as send_push,
            redirect_stdout(stdout),
        ):
            result = main._notify_system_alert(self.subs("both", "pushplus"), "T", "C")
        records = _records(stdout.getvalue().splitlines(), ALERT_PREFIX)
        self.assertIs(result, True)
        self.assertEqual(send_push.call_count, 2)
        self.assertEqual([(item["subscription"], item["any_sent"]) for item in records],
                         [("00000000-0000-4000-8000-000000000080", False),
                          ("00000000-0000-4000-8000-000000000081", True)])

    def test_no_channel_accepted(self):
        result, lines, sends = _run(lambda: main._notify_system_alert(self.subs("page_only", "email"), "T", "C"),
                                    email_result=False)
        self.assertIs(result, False)
        self.assertEqual(sends, (1, 0))
        self.assertEqual(len(_records(lines, ALERT_PREFIX)), 1, "page_only_skipped_without_record")
        self.assertIn("[篮子哨兵] 无可用通知渠道", lines)


if __name__ == "__main__":
    unittest.main()
