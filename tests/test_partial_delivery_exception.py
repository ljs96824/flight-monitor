"""DL-2：某个渠道已受理之后再抛异常，本次推送仍按已发送处理。

返回值与渠道日志的 any_sent 同义：只要有渠道受理就返回 True，并且推送快照恰好保存一次；
没有任何渠道受理时，结论和以前一样是 False，也不存档。
"""

import builtins
import io
import json
import unittest
from contextlib import redirect_stdout
from datetime import date
from unittest.mock import patch

import main
from tests import test_return_phase_processing as rp


PREFIX = "[推送渠道] "
SUBSCRIPTION_ID = "00000000-0000-4000-8000-000000000078"
ACCEPTED_NOTICE = "[推送] 中断前已有渠道受理，按已发送处理"


def _deliver(method="both", *, email="offline@example.invalid", email_ok=True, push_ok=True,
             email_render_error=None, email_send_error=None, push_render_error=None,
             push_send_error=None, persist_error=None, finish_print_error=None):
    subscription = {"id": SUBSCRIPTION_ID, "notification_goals": {"method": method, "email": email}}
    payload = {"push_type": "测试提醒", "route": "上海 → 大阪"}
    stdout = io.StringIO()

    def fake_print(*args, **kwargs):
        if finish_print_error and args and args[0] == "[推送] 发送完成":
            raise finish_print_error
        builtins.print(*args, **kwargs)

    def side(error, value):
        return {"side_effect": error} if error else {"return_value": value}

    with (
        patch.object(main, "build_notification_payload", return_value=payload),
        patch.object(main, "feedback_acknowledgement", return_value=None),
        patch.object(main, "render_email", **side(email_render_error, ("主题", "<p>正文</p>", {}))),
        patch.object(main, "render_detail_html", return_value="<p>详情</p>"),
        patch.object(main, "_save_result_for_page", return_value=True),
        patch.object(main, "render_pushplus_sections", **side(push_render_error, object())),
        patch.object(main, "send_email", **side(email_send_error, email_ok)) as send_email,
        patch.object(main, "send", **side(push_send_error, push_ok)) as send_push,
        patch.object(main, "persist_notification_payload", **side(persist_error, None)) as persist,
        patch.object(main, "print", fake_print, create=True),
        redirect_stdout(stdout),
    ):
        result = main._deliver_notification(subscription, "SHA-OSA", {"route_info": {}})
    text = stdout.getvalue()
    records = [json.loads(line[len(PREFIX):]) for line in text.splitlines() if line.startswith(PREFIX)]
    return result, records, text, (send_email.call_count, send_push.call_count), persist.call_count


class PartialSendExceptionTest(unittest.TestCase):
    CASES = (
        # 名称, 参数, 期望返回, 期望(邮件调用, 推送调用), 期望存档次数
        ("push_render_raises_after_email_accepted",
         {"push_render_error": RuntimeError("render failed")}, True, (1, 0), 1),
        ("push_send_raises_after_email_accepted",
         {"push_send_error": OSError("log write failed")}, True, (1, 1), 1),
        ("push_render_raises_after_email_rejected",
         {"email_ok": False, "push_render_error": RuntimeError("render failed")}, False, (1, 0), 0),
        ("push_render_raises_without_email_address",
         {"email": "", "push_render_error": RuntimeError("render failed")}, False, (0, 0), 0),
        ("email_render_raises_before_any_send",
         {"email_render_error": RuntimeError("chart failed")}, False, (0, 0), 0),
        ("email_send_raises",
         {"email_send_error": OSError("smtp failed")}, False, (1, 0), 0),
        ("finish_log_raises_after_persist",
         {"finish_print_error": UnicodeEncodeError("gbk", "", 0, 1, "synthetic")}, True, (1, 1), 1),
        ("persist_raises_in_abort_path",
         {"push_render_error": RuntimeError("render failed"), "persist_error": OSError("disk full")},
         True, (1, 0), 1),
    )

    def test_partial_send_matrix(self):
        for name, kwargs, expected, calls, persist_calls in self.CASES:
            with self.subTest(case=name):
                result, records, text, sends, persisted = _deliver(**kwargs)
                self.assertIs(result, expected, "partial_send_result")
                self.assertEqual(sends, calls, "channel_calls")
                self.assertEqual(persisted, persist_calls, "persist_exactly_once")
                self.assertTrue(records, "channel_record_written")
                self.assertTrue(records[-1].get("aborted"), "abort_recorded")
                self.assertIs(records[-1]["any_sent"], result, "any_sent_matches_return")
                self.assertIs(ACCEPTED_NOTICE in text, expected, "accepted_notice")

    def test_abort_path_persist_failure_is_logged(self):
        _result, _records, text, _sends, _persisted = _deliver(
            push_render_error=RuntimeError("render failed"), persist_error=OSError("disk full"))
        self.assertIn("[推送存档失败] OSError: disk full", text)

    def test_unaborted_paths_unchanged(self):
        for kwargs, expected, persist_calls in (({}, True, 1), ({"email_ok": False, "push_ok": False}, False, 0)):
            with self.subTest(**{key: str(value) for key, value in kwargs.items()}):
                result, records, text, sends, persisted = _deliver(**kwargs)
                self.assertIs(result, expected)
                self.assertEqual((sends, persisted), ((1, 1), persist_calls))
                self.assertEqual(len(records), 1)
                self.assertNotIn("aborted", records[0])
                self.assertNotIn(ACCEPTED_NOTICE, text)


class ProcessingPartialSendTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        rp.ReturnPhaseProcessingTest.setUpClass()

    def setUp(self):
        self.harness = rp.ReturnPhaseProcessingTest("test_return_new_collection")
        self.harness.setUp()
        self.addCleanup(self.harness.doCleanups)

    def test_round_counts_partial_send_as_delivered(self):
        for label, today in (("roundtrip", date(2026, 9, 30)), ("return_phase", date(2026, 10, 2))):
            with self.subTest(phase=label):
                with patch.object(main, "render_pushplus_sections", side_effect=RuntimeError("render failed")):
                    result = self.harness.run_case(today=today)
                self.assertIs(result["ok"], True, "subscription_round_ok")
                self.assertEqual((len(result["emails"]), len(result["pushes"])), (1, 0))
                self.assertEqual(result["save_price"].call_count, 1, "last_push_price_saved")
                self.assertEqual(result["save_snapshot"].call_count, 1, "push_snapshot_saved")
                self.assertEqual(result["plan_writer"].call_count, 1, "pushed_plans_saved")
                self.assertIn("[推送失败] RuntimeError: render failed", result["stdout"])
                self.assertNotIn("[订阅处理失败]", result["stdout"], "no_false_failure")


if __name__ == "__main__":
    unittest.main()
