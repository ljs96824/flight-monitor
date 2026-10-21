"""分渠道发送日志：每个渠道只报告自己的受理结果，另写一行结构化汇总。

取值：None=本轮未尝试该渠道；False=已尝试但未被受理(含未填邮箱、发送函数返回 False)；True=已被受理。
any_sent 与 _deliver_notification 的返回值同义；中途异常时只要已有渠道受理，仍返回 True 并存档(DL-2)。
"""

import io
import json
import unittest
from contextlib import redirect_stdout
from unittest.mock import patch

import main


PREFIX = "[推送渠道] "


def _deliver(method, *, email="offline@example.invalid", email_ok=True, push_ok=True, push_render_error=None,
             raw_method=False):
    subscription = {"id": "00000000-0000-4000-8000-000000000077", "notification_goals": {"method": method, "email": email}}
    payload = {"push_type": "测试提醒", "route": "上海 → 大阪"}
    render_kwargs = {"side_effect": push_render_error} if push_render_error else {"return_value": object()}
    stdout = io.StringIO()
    with (
        patch.object(main, "build_notification_payload", return_value=payload),
        patch.object(main, "feedback_acknowledgement", return_value=None),
        patch.object(main, "render_email", return_value=("主题", "<p>正文</p>", {})),
        patch.object(main, "render_detail_html", return_value="<p>详情</p>"),
        patch.object(main, "_save_result_for_page", return_value={"target_reached": True}),
        patch.object(main, "render_pushplus_sections", **render_kwargs),
        patch.object(main, "send_email", return_value=email_ok) as send_email,
        patch.object(main, "send", return_value=push_ok) as send_push,
        patch.object(main, "persist_notification_payload") as persist,
        patch.object(main, "normalize_notification_goals",
                     side_effect=(lambda goals: dict(goals)) if raw_method else main.normalize_notification_goals),
        redirect_stdout(stdout),
    ):
        result = main._deliver_notification(subscription, "SHA-OSA", {"route_info": {}})
    lines = stdout.getvalue().splitlines()
    records = [json.loads(line[len(PREFIX):]) for line in lines if line.startswith(PREFIX)]
    return result, records, lines, send_email.call_count, send_push.call_count, persist.call_count


class DeliveryChannelLogTest(unittest.TestCase):
    def assert_record(self, record, method, email_sent, pushplus_sent):
        self.assertEqual(record["subscription"], "00000000-0000-4000-8000-000000000077")
        self.assertEqual(record["route"], "SHA-OSA")
        self.assertEqual(record["method"], method)
        self.assertIs(record["email_sent"], email_sent)
        self.assertIs(record["pushplus_sent"], pushplus_sent)
        self.assertIs(record["any_sent"], bool(email_sent or pushplus_sent))

    def test_channel_matrix(self):
        cases = [
            # method, email, email_ok, push_ok, expected email_sent, pushplus_sent, calls(email, push)
            ("both", "offline@example.invalid", True, False, True, False, (1, 1)),
            ("both", "offline@example.invalid", False, True, False, True, (1, 1)),
            ("both", "offline@example.invalid", False, False, False, False, (1, 1)),
            ("both", "", True, True, False, True, (0, 1)),
            ("email", "offline@example.invalid", True, True, True, None, (1, 0)),
            ("email", "offline@example.invalid", False, True, False, None, (1, 0)),
            ("pushplus", "offline@example.invalid", True, False, None, False, (0, 1)),
            ("wechat", "offline@example.invalid", True, False, True, False, (1, 1)),
        ]
        for method, email, email_ok, push_ok, email_sent, pushplus_sent, calls in cases:
            with self.subTest(method=method, email=bool(email), email_ok=email_ok, push_ok=push_ok):
                result, records, lines, email_calls, push_calls, persist_calls = _deliver(
                    method, email=email, email_ok=email_ok, push_ok=push_ok)
                any_sent = bool(email_sent or pushplus_sent)
                self.assertIs(result, any_sent)
                self.assertEqual((email_calls, push_calls), calls)
                self.assertEqual(persist_calls, 1 if any_sent else 0)
                self.assertEqual(len(records), 1)
                self.assert_record(records[0], "both" if method == "wechat" else method, email_sent, pushplus_sent)
                self.assertNotIn("aborted", records[0])

    def test_each_channel_line_reports_only_its_own_result(self):
        _result, _records, lines, *_ = _deliver("both", email_ok=True, push_ok=False)
        self.assertIn("[推送] 邮件发送完成: email_sent=True", lines)
        self.assertIn("[推送] PushPlus发送完成: pushplus_sent=False", lines)
        self.assertFalse(any("发送完成: sent=" in line for line in lines), "cumulative_flag_removed")

    def test_unrecognized_method_fallback_reports_pushplus(self):
        # 归一化会把未知方式改为 both；兜底分支只在绕过归一化时可达，此处直接验证其日志。
        result, records, lines, email_calls, push_calls, _persist = _deliver("legacy", push_ok=False, raw_method=True)
        self.assertIs(result, False)
        self.assertEqual((email_calls, push_calls), (0, 1))
        self.assertIn("[推送] 兜底PushPlus发送完成: pushplus_sent=False", lines)
        self.assert_record(records[0], "legacy", None, False)

    def test_abort_after_email_keeps_channel_evidence(self):
        result, records, lines, email_calls, push_calls, persist_calls = _deliver(
            "both", email_ok=True, push_render_error=RuntimeError("render failed"))
        self.assertIs(result, True, "partial_send_counts_as_sent")
        self.assertEqual((email_calls, push_calls, persist_calls), (1, 0, 1))
        self.assertEqual(len(records), 1)
        self.assert_record(records[0], "both", True, None)
        self.assertEqual(records[0]["aborted"], "RuntimeError")

    def test_page_only_has_no_channel_record(self):
        result, records, _lines, email_calls, push_calls, persist_calls = _deliver("page_only")
        self.assertIs(result, True)
        self.assertEqual((email_calls, push_calls, persist_calls), (0, 0, 0))
        self.assertEqual(records, [])

    def test_log_helper_never_raises(self):
        stdout = io.StringIO()
        with patch.object(main, "safe_log_json", side_effect=ValueError("boom")), redirect_stdout(stdout):
            main._log_delivery_channels({}, "SHA-OSA", "both", True, False)
        self.assertIn("[推送渠道] 记录失败: ValueError", stdout.getvalue())


if __name__ == "__main__":
    unittest.main()
