"""R8-2：详情发布回执，page_only 按查看目标判断是否送达。

基线(R9-3 合并后的 main)：main._save_result_for_page 本地原子保存后调用 _upload_payload_to_pythonanywhere，不看其返回值，
上传异常只记一行，然后一律返回 True；_deliver_notification 对 page_only 直接返回这个值。于是详情地址指向 PA 时，缺 token、
被 NO_LIVE_API 门挡下、上传返回 False 或抛异常，page_only 都被算作送达；指向自定义域名或其他 PA 账户时也一样。
口径(R8 定案 D2 + Q1 + Q2)：_save_result_for_page 改为返回发布回执，分别记录本地保存、上传结果(succeeded / declined /
error / not_attempted)与查看目标，再显式映射为 target_reached。查看目标只按本次详情地址解析出的主机名判断：回环地址
为本地；<账户>.pythonanywhere.com 且账户与实际上传账户(PYTHONANYWHERE_USER，缺省 ljs96824)一致为 PA；PA 域名但账户
不一致为配置冲突；其他一律目标不明确。本地目标在本地保存成功时送达；PA 目标须本次上传取得成功回执，或同进程 web_form
的读取目录、本次发布目录与该账户 PA 部署 payload 目录三者一致(同目录直写)；冲突与不明确一律不算送达。page_only 只认
target_reached；email、pushplus、both 的结论、链接与正文不变，上传行为不变。
"""

import io
import json
import os
from pathlib import Path
import sys
import tempfile
import unittest
from contextlib import ExitStack, redirect_stdout
from types import SimpleNamespace
from unittest.mock import Mock, patch

import detail_publish as dp
import main
import notifier


SUB_ID = "00000000-0000-4000-8000-000000000082"
PA_URL = f"https://ljs96824.pythonanywhere.com/detail?sub={SUB_ID}"
LOCAL_URL = f"http://127.0.0.1:5000/detail?sub={SUB_ID}"
CUSTOM_URL = f"https://flights.example.com/detail?sub={SUB_ID}"
RECEIPT_PREFIX = "[详情发布回执] "
TOKEN_CANARY = "pa-token-canary-r82"
REACHED_LINE = "[推送] 用户选择仅页面查看，详情已到达查看目标"
MISSED_LINE = "[推送] 用户选择仅页面查看，详情未到达查看目标，按未送达处理"
NOT_PATCHED = object()


def environment_without_pa_user(**extra):
    values = {key: value for key, value in os.environ.items() if key != "PYTHONANYWHERE_USER"}
    values.update(extra)
    return values


def receipt_lines(output: str) -> list[dict]:
    return [
        json.loads(line[len(RECEIPT_PREFIX):])
        for line in output.splitlines()
        if line.startswith(RECEIPT_PREFIX)
    ]


class ViewTargetTest(unittest.TestCase):
    def test_target_is_judged_by_the_parsed_host_only(self):
        cases = (
            (LOCAL_URL, dp.TARGET_LOCAL),
            ("http://localhost/detail", dp.TARGET_LOCAL),
            ("http://[::1]:5000/detail", dp.TARGET_LOCAL),
            (PA_URL, dp.TARGET_PYTHONANYWHERE),
            ("https://LJS96824.PythonAnywhere.com/detail", dp.TARGET_PYTHONANYWHERE),
            ("https://someone.pythonanywhere.com/detail", dp.TARGET_PA_ACCOUNT_CONFLICT),
            ("https://www.pythonanywhere.com/user/ljs96824/", dp.TARGET_PA_ACCOUNT_CONFLICT),
            ("https://ljs96824.pythonanywhere.com.example.net/detail", dp.TARGET_UNKNOWN),
            ("https://example.net/ljs96824.pythonanywhere.com/detail", dp.TARGET_UNKNOWN),
            ("https://ljs96824.eu.pythonanywhere.com/detail", dp.TARGET_UNKNOWN),
            ("https://ljs96824.pythonanywhere.com:443/detail", dp.TARGET_PYTHONANYWHERE),
            ("https://ljs96824.pythonanywhere.com:8443/detail", dp.TARGET_UNKNOWN),
            ("https://ljs96824.pythonanywhere.com:abc/detail", dp.TARGET_UNKNOWN),
            ("https://ljs96824.pythonanywhere.com@evil.example/detail", dp.TARGET_UNKNOWN),
            ("https://evil.example\\@ljs96824.pythonanywhere.com/detail", dp.TARGET_UNKNOWN),
            ("http://evil.example\\@localhost:5000/detail", dp.TARGET_UNKNOWN),
            ("https://user@ljs96824.pythonanywhere.com/detail", dp.TARGET_UNKNOWN),
            ("https://ljs96824.pythonanywhere.com./detail", dp.TARGET_UNKNOWN),
            (CUSTOM_URL, dp.TARGET_UNKNOWN),
            ("http://127.0.0.2/detail", dp.TARGET_UNKNOWN),
            ("ftp://127.0.0.1/detail", dp.TARGET_UNKNOWN),
            ("127.0.0.1/detail", dp.TARGET_UNKNOWN),
            ("https://[::1/detail", dp.TARGET_UNKNOWN),
            ("", dp.TARGET_UNKNOWN),
            (None, dp.TARGET_UNKNOWN),
        )
        for url, expected in cases:
            with self.subTest(url=url):
                self.assertEqual(dp.detail_view_target(url, "ljs96824")[0], expected)

    def test_unconfigured_form_url_keeps_the_default_pa_target(self):
        cleared = {
            key: value
            for key, value in os.environ.items()
            if key not in {"SUBSCRIPTION_FORM_URL", "PYTHONANYWHERE_FORM_URL", "PYTHONANYWHERE_USER"}
        }
        with patch.dict(os.environ, cleared, clear=True):
            base = notifier._subscription_form_url({})
            user = main._pythonanywhere_user()
        self.assertEqual(user, "ljs96824")
        self.assertEqual(
            dp.detail_view_target(f"{base}/detail?sub={SUB_ID}", user),
            (dp.TARGET_PYTHONANYWHERE, "ljs96824.pythonanywhere.com"),
        )


class ReceiptMappingTest(unittest.TestCase):
    def test_mapping_matrix(self):
        cases = (
            # 名称, 地址, 本地保存, 上传, 同目录检查结果, 期望方式, 期望同目录记录, 是否执行同目录检查
            ("local_page", LOCAL_URL, True, dp.UPLOAD_DECLINED, False, dp.METHOD_LOCAL_PAGE, None, False),
            ("local_save_failed", LOCAL_URL, False, dp.UPLOAD_NOT_ATTEMPTED, False, None, None, False),
            ("pa_upload", PA_URL, True, dp.UPLOAD_SUCCEEDED, False, dp.METHOD_PA_UPLOAD, None, False),
            ("pa_declined", PA_URL, True, dp.UPLOAD_DECLINED, False, None, False, True),
            ("pa_error_same_directory", PA_URL, True, dp.UPLOAD_ERROR, True, dp.METHOD_PA_SAME_DIRECTORY, True, True),
            ("pa_local_save_failed", PA_URL, False, dp.UPLOAD_NOT_ATTEMPTED, True, None, None, False),
            ("conflict_even_if_uploaded", "https://someone.pythonanywhere.com/d", True, dp.UPLOAD_SUCCEEDED, True,
             None, None, False),
            ("unknown_even_if_uploaded", CUSTOM_URL, True, dp.UPLOAD_SUCCEEDED, True, None, None, False),
        )
        for name, url, local_saved, upload, same, method, same_recorded, checked in cases:
            with self.subTest(case=name):
                directory_probe = Mock(return_value=same)
                receipt = dp.build_detail_publish_receipt(
                    local_saved=local_saved,
                    upload=upload,
                    detail_url=url,
                    pa_user="ljs96824",
                    same_directory_check=directory_probe,
                )
                self.assertEqual(receipt["local_saved"], local_saved)
                self.assertEqual(receipt["upload"], upload)
                self.assertEqual(receipt["method"], method)
                self.assertIs(receipt["same_directory"], same_recorded)
                self.assertIs(receipt["target_reached"], method is not None)
                self.assertIs(directory_probe.called, checked)
                self.assertIs(dp.page_only_delivered(receipt), method is not None)

    def test_failing_same_directory_check_is_not_success(self):
        receipt = dp.build_detail_publish_receipt(
            local_saved=True,
            upload=dp.UPLOAD_DECLINED,
            detail_url=PA_URL,
            pa_user="ljs96824",
            same_directory_check=Mock(side_effect=OSError("probe failed")),
        )
        self.assertIs(receipt["same_directory"], False)
        self.assertIs(receipt["target_reached"], False)

    def test_only_an_explicit_reached_receipt_counts_for_page_only(self):
        for value in (True, 1, "yes", None, {}, {"target_reached": 1}, {"target_reached": "true"},
                      {"local_saved": True}):
            with self.subTest(value=value):
                self.assertIs(dp.page_only_delivered(value), False)
        self.assertIs(dp.page_only_delivered({"target_reached": True}), True)


class SameDirectoryTest(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.base = Path(temporary.name).resolve()
        self.payloads = self.base / "ljs96824" / "flight-monitor" / "data" / "payloads"
        self.payloads.mkdir(parents=True)
        self.other = self.base / "other"
        self.other.mkdir()

    def test_reader_published_and_pa_directories_must_coincide(self):
        web = SimpleNamespace(PAGE_PAYLOADS_DIR=self.payloads)
        self.assertTrue(dp.pa_same_directory(self.payloads, self.payloads, web_module=web))
        self.assertTrue(
            dp.pa_same_directory(
                self.payloads / ".",
                str(self.payloads),
                web_module=SimpleNamespace(PAGE_PAYLOADS_DIR=str(self.payloads / ".." / "payloads")),
            )
        )
        cases = (
            ("reader_elsewhere", self.payloads, self.payloads, SimpleNamespace(PAGE_PAYLOADS_DIR=self.other)),
            ("published_elsewhere", self.other, self.payloads, web),
            ("pa_directory_elsewhere", self.payloads, self.other, web),
            ("reader_without_directory", self.payloads, self.payloads, SimpleNamespace()),
            ("relative_pa_directory", self.payloads, Path("ljs96824/flight-monitor/data/payloads"), web),
        )
        for name, published, pa_directory, module in cases:
            with self.subTest(case=name):
                self.assertFalse(dp.pa_same_directory(published, pa_directory, web_module=module))

    def test_web_form_not_loaded_in_this_process_is_never_same_directory(self):
        with patch.dict(sys.modules):
            sys.modules.pop("web_form", None)
            self.assertFalse(dp.pa_same_directory(self.payloads, self.payloads))

    def test_pa_directory_follows_the_upload_path_template(self):
        self.assertEqual(
            dp.pa_payload_directory(main.PYTHONANYWHERE_PAYLOAD_PATH, "ljs96824").as_posix(),
            "/home/ljs96824/flight-monitor/data/payloads",
        )


class SaveResultReceiptTest(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.base = Path(temporary.name).resolve()
        self.payloads = self.base / "ljs96824" / "flight-monitor" / "data" / "payloads"

    def save(self, detail_url, *, upload, environ=None, template=None, reader=None, subscription_id=SUB_ID):
        upload_mock = upload if isinstance(upload, Mock) else Mock(return_value=upload)
        with ExitStack() as stack:
            stack.enter_context(
                patch.dict(
                    os.environ,
                    environ or environment_without_pa_user(PYTHONANYWHERE_TOKEN=TOKEN_CANARY),
                    clear=True,
                )
            )
            stack.enter_context(patch.object(main, "PAGE_PAYLOADS_DIR", self.payloads))
            stack.enter_context(patch.object(main, "_upload_payload_to_pythonanywhere", upload_mock))
            if template is not None:
                stack.enter_context(patch.object(main, "PYTHONANYWHERE_PAYLOAD_PATH", template))
            stack.enter_context(
                patch.dict(sys.modules, {"web_form": SimpleNamespace(PAGE_PAYLOADS_DIR=reader or self.payloads)})
            )
            output = stack.enter_context(redirect_stdout(io.StringIO()))
            receipt = main._save_result_for_page(
                subscription_id,
                "<p>详情</p>",
                {"detail_url": detail_url, "push_type": "已生成"},
            )
        return receipt, output.getvalue(), upload_mock

    def assert_logged_once(self, receipt, output):
        self.assertEqual(receipt_lines(output), [receipt])
        self.assertNotIn(TOKEN_CANARY, output)

    def test_pa_target_reached_only_with_an_upload_success_receipt(self):
        receipt, output, upload = self.save(PA_URL, upload=True)
        self.assertEqual(
            receipt,
            {
                "local_saved": True,
                "upload": "succeeded",
                "target": "pythonanywhere",
                "target_host": "ljs96824.pythonanywhere.com",
                "same_directory": None,
                "method": "pa_upload",
                "target_reached": True,
            },
        )
        upload.assert_called_once_with(self.payloads / f"{SUB_ID}.json")
        self.assert_logged_once(receipt, output)

    def test_pa_upload_declined_or_failed_is_not_reached_but_local_snapshot_stays(self):
        for name, upload, expected_upload in (
            ("declined", False, "declined"),
            ("non_bool_truthy", Mock(return_value="ok"), "declined"),
            ("error", Mock(side_effect=RuntimeError("upload boom")), "error"),
        ):
            with self.subTest(case=name):
                receipt, output, _upload = self.save(PA_URL, upload=upload)
                self.assertEqual(receipt["upload"], expected_upload)
                self.assertIs(receipt["same_directory"], False)
                self.assertIsNone(receipt["method"])
                self.assertIs(receipt["target_reached"], False)
                self.assertTrue((self.payloads / f"{SUB_ID}.json").is_file())
                self.assertIs(
                    "[详情同步失败] RuntimeError: upload boom" in output,
                    expected_upload == "error",
                )
                self.assert_logged_once(receipt, output)

    def test_local_target_is_reached_by_the_local_snapshot_alone(self):
        receipt, output, upload = self.save(LOCAL_URL, upload=False)
        self.assertEqual(
            (receipt["target"], receipt["method"], receipt["same_directory"], receipt["target_reached"]),
            ("local", "local_page", None, True),
        )
        upload.assert_called_once()
        self.assert_logged_once(receipt, output)

    def test_pa_same_directory_publish_needs_the_web_reader_to_match(self):
        template = f"{self.base.as_posix()}/{{user}}/flight-monitor/data/payloads/{{filename}}"
        receipt, _output, _upload = self.save(PA_URL, upload=False, template=template)
        self.assertEqual(
            (receipt["same_directory"], receipt["method"], receipt["target_reached"]),
            (True, "pa_same_directory", True),
        )
        elsewhere = self.base / "elsewhere"
        receipt, _output, _upload = self.save(PA_URL, upload=False, template=template, reader=elsewhere)
        self.assertEqual(
            (receipt["same_directory"], receipt["method"], receipt["target_reached"]),
            (False, None, False),
        )

    def test_account_mismatch_is_a_conflict_even_after_upload(self):
        environ = environment_without_pa_user(PYTHONANYWHERE_USER="someone", PYTHONANYWHERE_TOKEN=TOKEN_CANARY)
        receipt, output, upload = self.save(PA_URL, upload=True, environ=environ)
        self.assertEqual((receipt["target"], receipt["target_reached"]), ("pa_account_conflict", False))
        upload.assert_called_once()
        self.assert_logged_once(receipt, output)

    def test_unknown_target_is_never_reached(self):
        receipt, _output, _upload = self.save(CUSTOM_URL, upload=True)
        self.assertEqual((receipt["target"], receipt["target_reached"]), ("unknown", False))
        receipt, _output, _upload = self.save("", upload=True)
        self.assertEqual((receipt["target"], receipt["target_reached"]), ("unknown", False))

    def test_non_uuid_identifier_saves_nothing_and_attempts_no_upload(self):
        receipt, output, upload = self.save(PA_URL, upload=True, subscription_id="107")
        self.assertEqual(
            (receipt["local_saved"], receipt["upload"], receipt["target_reached"]),
            (False, "not_attempted", False),
        )
        upload.assert_not_called()
        self.assertFalse(self.payloads.exists())
        self.assert_logged_once(receipt, output)


class PageOnlyDeliveryTest(unittest.TestCase):
    def deliver(self, method, *, detail_url, upload=NOT_PATCHED, environ=None, extra=()):
        # 不受开发机 .env 中 PYTHONANYWHERE_USER 影响：缺省用去掉该变量的环境。
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        payloads = Path(temporary.name) / "payloads"
        payload = {"push_type": "测试提醒", "route": "上海 → 大阪", "detail_url": detail_url}
        subscription = {"id": SUB_ID, "notification_goals": {"method": method, "email": "offline@example.invalid"}}
        with ExitStack() as stack:
            stack.enter_context(
                patch.dict(os.environ, environ if environ is not None else environment_without_pa_user(), clear=True)
            )
            for name, value in (
                ("build_notification_payload", payload),
                ("feedback_acknowledgement", None),
                ("render_email", ("主题", "<p>正文</p>", {})),
                ("render_detail_html", "<p>详情</p>"),
                ("render_pushplus_sections", object()),
                ("send_email", True),
                ("send", True),
            ):
                stack.enter_context(patch.object(main, name, return_value=value))
            stack.enter_context(patch.object(main, "delivery_payload_with_detail_token", side_effect=lambda item: item))
            stack.enter_context(patch.object(main, "persist_notification_payload"))
            stack.enter_context(patch.object(main, "PAGE_PAYLOADS_DIR", payloads))
            if upload is not NOT_PATCHED:
                stack.enter_context(patch.object(main, "_upload_payload_to_pythonanywhere", upload))
            for item in extra:
                stack.enter_context(item)
            output = stack.enter_context(redirect_stdout(io.StringIO()))
            result = main._deliver_notification(subscription, "SHA-OSA", {"route_info": {}})
        return result, output.getvalue().splitlines(), (payloads / f"{SUB_ID}.json").is_file()

    def test_page_only_follows_the_receipt(self):
        cases = (
            ("pa_uploaded", PA_URL, Mock(return_value=True), True),
            ("pa_upload_declined", PA_URL, Mock(return_value=False), False),
            ("pa_upload_error", PA_URL, Mock(side_effect=RuntimeError("boom")), False),
            ("local_target", LOCAL_URL, Mock(return_value=False), True),
            ("custom_domain", CUSTOM_URL, Mock(return_value=True), False),
        )
        for name, url, upload, expected in cases:
            with self.subTest(case=name):
                result, lines, saved = self.deliver("page_only", detail_url=url, upload=upload)
                self.assertIs(result, expected)
                self.assertTrue(saved)
                self.assertIn(REACHED_LINE if expected else MISSED_LINE, lines)
                self.assertNotIn(MISSED_LINE if expected else REACHED_LINE, lines)

    def test_offline_gate_means_a_pa_page_is_not_delivered(self):
        post = Mock(side_effect=AssertionError("unexpected HTTP call"))
        environ = environment_without_pa_user(NO_LIVE_API="1", PYTHONANYWHERE_TOKEN=TOKEN_CANARY)
        result, lines, saved = self.deliver(
            "page_only",
            detail_url=PA_URL,
            environ=environ,
            extra=(patch.object(main, "httpx", SimpleNamespace(post=post)),),
        )
        self.assertIs(result, False)
        self.assertTrue(saved)
        post.assert_not_called()
        self.assertIn("[详情同步] NO_LIVE_API=1，已阻止真实 PythonAnywhere 上传", lines)
        receipts = receipt_lines("\n".join(lines))
        self.assertEqual([(item["upload"], item["target_reached"]) for item in receipts], [("declined", False)])
        self.assertNotIn(TOKEN_CANARY, "\n".join(lines))

    def test_other_methods_keep_their_channel_result_when_the_page_misses(self):
        for method in ("email", "pushplus", "both"):
            with self.subTest(method=method):
                result, lines, saved = self.deliver(method, detail_url=PA_URL, upload=Mock(return_value=False))
                self.assertIs(result, True)
                self.assertTrue(saved)
                self.assertNotIn(MISSED_LINE, lines)


if __name__ == "__main__":
    unittest.main()
