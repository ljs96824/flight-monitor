"""R7：发送函数被调用后抛错时，渠道记录写明"已尝试、结果无法确认"，与"发送前就中断"区分开。

取值沿用推送渠道日志：None=未尝试；False=已尝试但未被受理；True=已被受理。
发送函数(send_email / send)被调用后抛错 → 该渠道记 False，并追加 <渠道>_error=异常类型；
渲染等发送前步骤抛错 → 该渠道仍为 None，不追加 *_error。请求可能已发出，本轮不重发；
返回值(任一渠道受理即 True)、存档次数与 [推送失败] 行均不变。
"""

import builtins
import io
import json
import unittest
from contextlib import redirect_stdout
from unittest.mock import patch

import main


PREFIX = "[推送渠道] "
SUBSCRIPTION_ID = "00000000-0000-4000-8000-000000000091"
EMAIL = "offline@example.invalid"


class SyntheticReadTimeout(Exception):
    """模拟请求已发出、等待响应时超时：对端是否收到无法确认。"""


def _effect(value):
    return {"side_effect": value} if isinstance(value, BaseException) else {"return_value": value}


def _deliver(method="both", *, email_result=True, push_result=True, push_render_error=None,
             raw_method=False, failing_print=None):
    subscription = {"id": SUBSCRIPTION_ID, "notification_goals": {"method": method, "email": EMAIL}}
    payload = {"push_type": "测试提醒", "route": "上海 → 大阪"}
    stdout = io.StringIO()

    def fake_print(*args, **kwargs):
        if failing_print and args and args[0] == failing_print:
            raise UnicodeEncodeError("gbk", "", 0, 1, "synthetic")
        builtins.print(*args, **kwargs)

    render_push = {"side_effect": push_render_error} if push_render_error else {"return_value": object()}
    with (
        patch.object(main, "build_notification_payload", return_value=payload),
        patch.object(main, "feedback_acknowledgement", return_value=None),
        patch.object(main, "delivery_payload_with_detail_token", side_effect=lambda item: item),
        patch.object(main, "render_email", return_value=("主题", "<p>正文</p>", {})),
        patch.object(main, "render_detail_html", return_value="<p>详情</p>"),
        patch.object(main, "_save_result_for_page", return_value=True),
        patch.object(main, "render_pushplus_sections", **render_push),
        patch.object(main, "send_email", **_effect(email_result)) as send_email,
        patch.object(main, "send", **_effect(push_result)) as send_push,
        patch.object(main, "persist_notification_payload") as persist,
        patch.object(main, "normalize_notification_goals",
                     side_effect=(lambda goals: dict(goals)) if raw_method else main.normalize_notification_goals),
        patch.object(main, "print", fake_print, create=True),
        redirect_stdout(stdout),
    ):
        result = main._deliver_notification(subscription, "SHA-OSA", {"route_info": {}})
    lines = stdout.getvalue().splitlines()
    records = [json.loads(line[len(PREFIX):]) for line in lines if line.startswith(PREFIX)]
    return result, records, (send_email.call_count, send_push.call_count), persist.call_count


def _error_fields(record):
    return {key: value for key, value in record.items() if key.endswith("_error")}


class SendExceptionStateTest(unittest.TestCase):
    TIMEOUT = SyntheticReadTimeout("read timed out")
    CASES = (
        # 名称, 参数, 期望返回, 期望(email_sent, pushplus_sent), 期望 *_error, 期望(邮件调用, 推送调用), 期望存档次数
        ("pushplus_send_raises", {"method": "pushplus", "push_result": TIMEOUT},
         False, (None, False), {"pushplus_error": "SyntheticReadTimeout"}, (0, 1), 0),
        ("push_send_raises_after_email_accepted", {"push_result": TIMEOUT},
         True, (True, False), {"pushplus_error": "SyntheticReadTimeout"}, (1, 1), 1),
        ("email_send_raises", {"email_result": OSError("mime build failed")},
         False, (False, None), {"email_error": "OSError"}, (1, 0), 0),
        ("email_only_send_raises", {"method": "email", "email_result": ValueError("bad header")},
         False, (False, None), {"email_error": "ValueError"}, (1, 0), 0),
        ("fallback_send_raises", {"method": "legacy", "raw_method": True, "push_result": TIMEOUT},
         False, (None, False), {"pushplus_error": "SyntheticReadTimeout"}, (0, 1), 0),
        # 以下为对照：异常不在发送函数内，不得归到任何渠道
        ("push_render_raises_after_email_accepted", {"push_render_error": RuntimeError("render failed")},
         True, (True, None), {}, (1, 0), 1),
        ("push_render_raises_pushplus_only", {"method": "pushplus", "push_render_error": RuntimeError("render failed")},
         False, (None, None), {}, (0, 0), 0),
        ("email_finish_line_raises", {"failing_print": "[推送] 邮件发送完成: email_sent=True"},
         True, (True, None), {}, (1, 0), 1),
        ("push_finish_line_raises", {"method": "pushplus", "failing_print": "[推送] PushPlus发送完成: pushplus_sent=True"},
         True, (None, True), {}, (0, 1), 1),
    )

    def test_exception_state_matrix(self):
        for name, kwargs, expected, channels, errors, sends, persists in self.CASES:
            with self.subTest(case=name):
                result, records, calls, persisted = _deliver(**kwargs)
                self.assertIs(result, expected, "return_policy_unchanged")
                self.assertEqual(calls, sends, "no_resend_after_exception")
                self.assertEqual(persisted, persists, "persist_policy_unchanged")
                self.assertEqual(len(records), 1, "one_channel_record")
                record = records[0]
                self.assertTrue(record.get("aborted"), "abort_recorded")
                self.assertEqual((record["email_sent"], record["pushplus_sent"]), channels, "channel_state")
                self.assertEqual(_error_fields(record), errors, "error_channel_attribution")
                for value in errors.values():
                    self.assertEqual(value, record["aborted"])
                self.assertIs(record["any_sent"], result, "any_sent_matches_return")

    def test_unaborted_records_have_no_error_fields(self):
        for method, raw_method in (("both", False), ("email", False), ("pushplus", False), ("legacy", True)):
            for accepted in (True, False):
                with self.subTest(method=method, accepted=accepted):
                    _result, records, _calls, _persisted = _deliver(
                        method, email_result=accepted, push_result=accepted, raw_method=raw_method)
                    self.assertEqual(len(records), 1)
                    self.assertNotIn("aborted", records[0])
                    self.assertEqual(_error_fields(records[0]), {})


class DeliveryChannelLogHelperTest(unittest.TestCase):
    def _record(self, **kwargs):
        stdout = io.StringIO()
        with redirect_stdout(stdout):
            main._log_delivery_channels({"id": SUBSCRIPTION_ID}, "SHA-OSA", "both", True, False, **kwargs)
        lines = [line for line in stdout.getvalue().splitlines() if line.startswith(PREFIX)]
        self.assertEqual(len(lines), 1)
        return json.loads(lines[0][len(PREFIX):])

    def test_error_channel_field_only_with_abort(self):
        self.assertEqual(self._record(aborted="SyntheticReadTimeout", error_channel="pushplus")["pushplus_error"],
                         "SyntheticReadTimeout")
        self.assertEqual(self._record(aborted="OSError", error_channel="email")["email_error"], "OSError",
                         "email_error_not_redacted")
        self.assertEqual(_error_fields(self._record(aborted="RuntimeError")), {})
        self.assertEqual(_error_fields(self._record(error_channel="pushplus")), {})


if __name__ == "__main__":
    unittest.main()
