"""R1：邮件专用渲染(折线图PNG/邮件HTML)只在选择了邮件的方式下执行。

pushplus、page_only 与未识别方式的 PushPlus 兜底不再调用 render_email：既省掉折线图与邮件HTML的生成，
邮件渲染出错时也不再连带阻断这些方式。email、both 的渲染次数、发送参数与结论不变；
详情渲染、页面保存、PushPlus 渲染与发送均照旧。
邮件渲染自带的诊断日志([口径校验][邮件]、[渲染统计][邮件])随之只在邮件方式下出现，详情诊断照旧。
"""

import io
import json
import unittest
from contextlib import redirect_stdout
from unittest.mock import patch

import main
import test_log_quality as log_quality


PREFIX = "[推送渠道] "
SUBSCRIPTION_ID = "00000000-0000-4000-8000-000000000090"
EMAIL = "offline@example.invalid"
ENABLED = "[推送] 邮件方式已启用，开始生成折线图PNG/邮件HTML"
SKIPPED = "[推送] 未选择邮件方式，跳过折线图PNG/邮件HTML"
RENDERED = ("主题", "<p>正文</p>", {"trendchart": b"png"})
PAYLOAD = {"push_type": "测试提醒", "route": "上海 → 大阪"}
PUSH_TITLE = "【测试提醒】上海 → 大阪"


def _deliver(method, *, render_error=None, raw_method=False, page_saved=True):
    subscription = {"id": SUBSCRIPTION_ID, "notification_goals": {"method": method, "email": EMAIL}}
    payload = dict(PAYLOAD)
    push_render = object()
    render_kwargs = {"side_effect": render_error} if render_error else {"return_value": RENDERED}
    stdout = io.StringIO()
    with (
        patch.object(main, "build_notification_payload", return_value=payload),
        patch.object(main, "feedback_acknowledgement", return_value=None),
        patch.object(main, "delivery_payload_with_detail_token", side_effect=lambda item: item),
        patch.object(main, "render_email", **render_kwargs) as render_email,
        patch.object(main, "render_detail_html", return_value="<p>详情</p>") as render_detail,
        patch.object(main, "_save_result_for_page", return_value=page_saved) as save_page,
        patch.object(main, "render_pushplus_sections", return_value=push_render) as render_push,
        patch.object(main, "send_email", return_value=True) as send_email,
        patch.object(main, "send", return_value=True) as send_push,
        patch.object(main, "persist_notification_payload") as persist,
        patch.object(main, "normalize_notification_goals",
                     side_effect=(lambda goals: dict(goals)) if raw_method else main.normalize_notification_goals),
        redirect_stdout(stdout),
    ):
        result = main._deliver_notification(subscription, "SHA-OSA", {"route_info": {}})
    lines = stdout.getvalue().splitlines()
    return {
        "result": result,
        "lines": lines,
        "records": [json.loads(line[len(PREFIX):]) for line in lines if line.startswith(PREFIX)],
        "payload": payload,
        "push_render": push_render,
        "render_email": render_email,
        "render_detail": render_detail,
        "save_page": save_page,
        "render_push": render_push,
        "send_email": send_email,
        "send_push": send_push,
        "persist": persist,
    }


class EmailRenderByChannelTest(unittest.TestCase):
    CASES = (
        # 名称, 方式, 绕过归一化, 期望邮件渲染次数, 期望(邮件发送, 推送发送), 期望存档次数
        ("email", "email", False, 1, (1, 0), 1),
        ("both", "both", False, 1, (1, 1), 1),
        ("normalized_to_both", "wechat", False, 1, (1, 1), 1),
        ("pushplus", "pushplus", False, 0, (0, 1), 1),
        ("page_only", "page_only", False, 0, (0, 0), 0),
        ("unrecognized_fallback", "legacy", True, 0, (0, 1), 1),
    )

    def test_render_count_and_outputs_by_method(self):
        for name, method, raw_method, renders, sends, persists in self.CASES:
            with self.subTest(case=name):
                run = _deliver(method, raw_method=raw_method)
                self.assertIs(run["result"], True)
                self.assertEqual(run["render_email"].call_count, renders, "email_render_calls")
                self.assertEqual((run["send_email"].call_count, run["send_push"].call_count), sends, "channel_calls")
                self.assertEqual(run["persist"].call_count, persists, "persist_calls")
                run["render_detail"].assert_called_once_with(run["payload"])
                run["save_page"].assert_called_once_with(SUBSCRIPTION_ID, "<p>详情</p>", run["payload"])
                self.assertEqual((ENABLED in run["lines"], SKIPPED in run["lines"]), (bool(renders), not renders),
                                 "render_branch_line")
                if renders:
                    run["render_email"].assert_called_once_with(run["payload"])
                    run["send_email"].assert_called_once_with(EMAIL, *RENDERED)
                if sends[1]:
                    run["send_push"].assert_called_once_with(run["push_render"], title=PUSH_TITLE)

    def test_page_only_still_returns_page_save_result(self):
        run = _deliver("page_only", page_saved=False)
        self.assertIs(run["result"], False)
        self.assertEqual(run["render_email"].call_count, 0)
        self.assertEqual(run["records"], [])

    def test_email_render_failure_no_longer_blocks_other_methods(self):
        cases = (
            # 名称, 方式, 绕过归一化, 期望返回, 期望(邮件发送, 推送发送), 期望存档次数
            ("pushplus", "pushplus", False, True, (0, 1), 1),
            ("page_only", "page_only", False, True, (0, 0), 0),
            ("unrecognized_fallback", "legacy", True, True, (0, 1), 1),
            ("email_unchanged", "email", False, False, (0, 0), 0),
            ("both_unchanged", "both", False, False, (0, 0), 0),
        )
        for name, method, raw_method, expected, sends, persists in cases:
            with self.subTest(case=name):
                run = _deliver(method, raw_method=raw_method, render_error=RuntimeError("chart failed"))
                self.assertIs(run["result"], expected)
                self.assertEqual((run["send_email"].call_count, run["send_push"].call_count), sends, "channel_calls")
                self.assertEqual(run["persist"].call_count, persists, "persist_calls")
                aborted = "[推送失败] RuntimeError: chart failed" in run["lines"]
                self.assertIs(aborted, method in {"email", "both"}, "abort_only_when_email_selected")
                if aborted:
                    self.assertEqual(run["render_email"].call_count, 1)
                    self.assertEqual(run["records"][-1]["aborted"], "RuntimeError")
                    self.assertEqual(run["save_page"].call_count, 0, "render_still_precedes_detail")
                else:
                    self.assertEqual(run["render_email"].call_count, 0)
                    self.assertTrue(all("aborted" not in record for record in run["records"]))


class EmailRenderDiagnosticsTest(unittest.TestCase):
    """真实渲染器：邮件诊断只随邮件渲染出现，详情诊断对所有方式照旧。"""

    def _real_render_lines(self, method):
        subscription = {"id": SUBSCRIPTION_ID, "notification_goals": {"method": method, "email": EMAIL}}
        stdout = io.StringIO()
        with (
            patch.object(main, "build_notification_payload", return_value=log_quality._roundtrip_payload()),
            patch.object(main, "feedback_acknowledgement", return_value=None),
            patch.object(main, "delivery_payload_with_detail_token", side_effect=lambda item: item),
            patch.object(main, "_save_result_for_page", return_value=True),
            patch.object(main, "render_pushplus_sections", return_value=object()),
            patch.object(main, "send_email", return_value=True),
            patch.object(main, "send", return_value=True),
            patch.object(main, "persist_notification_payload"),
            redirect_stdout(stdout),
        ):
            self.assertIs(main._deliver_notification(subscription, "SHA-OSA", {"route_info": {}}), True)
        return [line for line in stdout.getvalue().splitlines() if "[口径校验]" in line or "[渲染统计]" in line]

    def test_email_diagnostics_follow_email_rendering(self):
        for method, email_rendered in (("both", True), ("email", True), ("pushplus", False), ("page_only", False)):
            with self.subTest(method=method):
                lines = self._real_render_lines(method)
                self.assertTrue(any("[渲染统计][详情]" in line for line in lines), "detail_diagnostics_kept")
                self.assertIs(any("[渲染统计][邮件]" in line for line in lines), email_rendered)
                self.assertIs(any("[口径校验][邮件]" in line for line in lines), email_rendered)


if __name__ == "__main__":
    unittest.main()
