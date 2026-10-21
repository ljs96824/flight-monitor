"""R3：PythonAnywhere Files 上传与下载两个出口的 NO_LIVE_API 硬门。

基线(b696128)：main._upload_payload_to_pythonanywhere(POST)与 sync_subscriptions.download_remote_subscriptions(GET)
在读取 PYTHONANYWHERE_TOKEN、发起 httpx 请求之前都不判断 NO_LIVE_API，覆盖清单
docs/external-network-no-live-api-coverage-2026-09-03.md 把两者记为 absent。CI 与受控离线验证进程设置 NO_LIVE_API=1，
但只要进程里有真实 token、调用方又没有 mock，这两个出口仍会真实联网。
口径(与 SMTP / PushPlus 出口门一致)：两个函数的第一条业务分支精确判断 effective NO_LIVE_API == "1"，命中时只记一条
固定日志并返回各自现有的"跳过"值(上传 False、下载 [])；此前不读其他环境变量、不检查 httpx、不打开文件、不发请求。
其他取值保持原行为。详情 payload 的本地保存照常成功；同步命中门时返回 synced=False，本地订阅文件不写。
"""

import ast
from contextlib import ExitStack, redirect_stdout
import io
import json
import os
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

import main
import sync_subscriptions as sync


ROOT = Path(__file__).resolve().parents[1]
UPLOAD_LOG = "[详情同步] NO_LIVE_API=1，已阻止真实 PythonAnywhere 上传"
DOWNLOAD_LOG = "[sync] NO_LIVE_API=1，已阻止真实 PythonAnywhere 下载"
TOKEN_CANARY = "TEST_ONLY_PA_TOKEN_CANARY"
USER_CANARY = "test-only-pa-user"
SUB_ID = "123e4567-e89b-12d3-a456-426614174070"
PA_API_MARKER = "www.pythonanywhere.com/api"
# effective NO_LIVE_API 取值 → 是否被门拦下；只有精确的 "1" 拦截
VALUES = (("1", True), (None, False), ("", False), ("0", False), ("true", False), ("01", False))


class FakeResponse:
    def __init__(self, data=None):
        self.data = data

    def raise_for_status(self):
        return None

    def json(self):
        return self.data


class TrackingEnvironment(dict):
    """门之前只允许读取 NO_LIVE_API。"""

    def __init__(self, values):
        super().__init__(values)
        self.read_keys = []

    def get(self, key, default=None):
        self.read_keys.append(key)
        if key != "NO_LIVE_API":
            raise AssertionError(f"environment read before no-live return: {key}")
        return super().get(key, default)


def function_node(relative: str, name: str) -> ast.FunctionDef:
    tree = ast.parse((ROOT / relative).read_text(encoding="utf-8-sig"))
    for node in tree.body:
        if isinstance(node, ast.FunctionDef) and node.name == name:
            return node
    raise AssertionError(f"function not found: {relative}:{name}")


def production_python_files():
    for path in sorted(ROOT.rglob("*.py")):
        relative = path.relative_to(ROOT)
        if path.name.startswith("test_") or relative.parts[:1] == ("tests",):
            continue
        if any(part in {".git", "__pycache__", "data"} for part in relative.parts):
            continue
        yield path, relative.as_posix()


def pa_api_url_scopes() -> set[str]:
    scopes = set()
    for path, relative in production_python_files():
        tree = ast.parse(path.read_text(encoding="utf-8-sig"), filename=relative)

        class Visitor(ast.NodeVisitor):
            def __init__(self):
                self.scope = []

            def visit_FunctionDef(self, node):
                self.scope.append(node.name)
                self.generic_visit(node)
                self.scope.pop()

            visit_AsyncFunctionDef = visit_FunctionDef
            visit_ClassDef = visit_FunctionDef

            def visit_Constant(self, node):
                if isinstance(node.value, str) and PA_API_MARKER in node.value:
                    scopes.add(f"{relative}:{'.'.join(self.scope) or '<module>'}")

        Visitor().visit(tree)
    return scopes


class PaFilesStaticGateTest(unittest.TestCase):
    def test_gate_is_first_business_statement_with_fixed_branch(self):
        condition = ast.dump(ast.parse('os.environ.get("NO_LIVE_API") == "1"', mode="eval").body)
        cases = (
            ("main.py", "_upload_payload_to_pythonanywhere", UPLOAD_LOG, "return False"),
            ("sync_subscriptions.py", "download_remote_subscriptions", DOWNLOAD_LOG, "return []"),
        )
        for relative, name, log, return_source in cases:
            with self.subTest(gateway=name):
                body = list(function_node(relative, name).body)
                if body and isinstance(body[0], ast.Expr) and isinstance(body[0].value, ast.Constant) \
                        and isinstance(body[0].value.value, str):
                    body.pop(0)
                gate = body[0]
                self.assertIsInstance(gate, ast.If)
                self.assertEqual(ast.dump(gate.test), condition)
                self.assertEqual(gate.orelse, [])
                self.assertEqual([ast.dump(item) for item in gate.body], [
                    ast.dump(ast.parse(f"safe_log({log!r})").body[0]),
                    ast.dump(ast.parse(return_source).body[0]),
                ])

    def test_pa_files_api_url_only_in_the_two_gateways(self):
        self.assertEqual(pa_api_url_scopes(), {
            "main.py:_upload_payload_to_pythonanywhere",
            "sync_subscriptions.py:download_remote_subscriptions",
        })


class PaPayloadUploadGateTest(unittest.TestCase):
    def test_exact_value_matrix(self):
        with tempfile.TemporaryDirectory() as tmp:
            payload_path = Path(tmp) / f"{SUB_ID}.json"
            payload_path.write_text("{}", encoding="utf-8")
            for value, blocked in VALUES:
                with self.subTest(value=value):
                    environment = {"PYTHONANYWHERE_TOKEN": TOKEN_CANARY, "PYTHONANYWHERE_USER": USER_CANARY}
                    if value is not None:
                        environment["NO_LIVE_API"] = value
                    post = Mock(return_value=FakeResponse())
                    with (
                        patch.dict(os.environ, environment, clear=True),
                        patch.object(main, "httpx", SimpleNamespace(post=post)),
                        patch.object(main, "safe_log") as gate_log,
                        redirect_stdout(io.StringIO()),
                    ):
                        result = main._upload_payload_to_pythonanywhere(payload_path)
                    self.assertIs(result, not blocked)
                    self.assertEqual(post.call_count, 0 if blocked else 1)
                    if blocked:
                        gate_log.assert_called_once_with(UPLOAD_LOG)
                    else:
                        gate_log.assert_not_called()
                        self.assertIn(f"/home/{USER_CANARY}/flight-monitor/data/payloads/{SUB_ID}.json",
                                      post.call_args.args[0])

    def test_gate_returns_before_any_other_work(self):
        environment = TrackingEnvironment({"NO_LIVE_API": "1", "PYTHONANYWHERE_TOKEN": TOKEN_CANARY})
        with tempfile.TemporaryDirectory() as tmp:
            never_opened = Path(tmp) / "missing.json"
            with (
                patch.object(main.os, "environ", environment),
                patch.object(main, "httpx", None),
                patch.object(main, "safe_log") as gate_log,
                redirect_stdout(io.StringIO()) as output,
            ):
                result = main._upload_payload_to_pythonanywhere(never_opened)
        self.assertIs(result, False)
        self.assertEqual(environment.read_keys, ["NO_LIVE_API"])
        gate_log.assert_called_once_with(UPLOAD_LOG)
        self.assertEqual(output.getvalue(), "", "httpx_check_not_reached")

    def test_payload_still_saved_locally(self):
        with tempfile.TemporaryDirectory() as tmp, ExitStack() as stack:
            payloads = Path(tmp) / "payloads"
            post = Mock(side_effect=AssertionError("unexpected HTTP call"))
            stack.enter_context(patch.dict(os.environ, {"NO_LIVE_API": "1", "PYTHONANYWHERE_TOKEN": TOKEN_CANARY},
                                           clear=True))
            stack.enter_context(patch.object(main, "PAGE_PAYLOADS_DIR", payloads))
            stack.enter_context(patch.object(main, "httpx", SimpleNamespace(post=post)))
            output = stack.enter_context(redirect_stdout(io.StringIO()))
            saved = main._save_result_for_page(SUB_ID, "<p>x</p>", {"push_type": "已生成"})
            record = json.loads((payloads / f"{SUB_ID}.json").read_text(encoding="utf-8"))
        self.assertIs(saved["local_saved"], True)
        self.assertEqual(saved["upload"], "declined")
        post.assert_not_called()
        self.assertEqual((record["subscription_id"], record["payload"]), (SUB_ID, {"push_type": "已生成"}))
        self.assertIn(UPLOAD_LOG, output.getvalue())
        self.assertNotIn("[详情同步失败]", output.getvalue())
        self.assertNotIn(TOKEN_CANARY, output.getvalue())


class PaSubscriptionDownloadGateTest(unittest.TestCase):
    REMOTE = [{"subscription_id": SUB_ID, "origin": "上海", "destination": "大阪"}]

    def test_exact_value_matrix(self):
        for value, blocked in VALUES:
            with self.subTest(value=value):
                environment = {"PYTHONANYWHERE_TOKEN": TOKEN_CANARY, "PYTHONANYWHERE_USER": USER_CANARY}
                if value is not None:
                    environment["NO_LIVE_API"] = value
                get = Mock(return_value=FakeResponse(self.REMOTE))
                with (
                    patch.dict(os.environ, environment, clear=True),
                    patch.object(sync.httpx, "get", get),
                    patch.object(sync, "safe_log") as gate_log,
                    redirect_stdout(io.StringIO()),
                ):
                    result = sync.download_remote_subscriptions()
                self.assertEqual(result, [] if blocked else self.REMOTE)
                self.assertEqual(get.call_count, 0 if blocked else 1)
                if blocked:
                    gate_log.assert_called_once_with(DOWNLOAD_LOG)
                else:
                    gate_log.assert_not_called()
                    self.assertIn(f"/home/{USER_CANARY}/flight-monitor/data/subscriptions.json", get.call_args.args[0])

    def test_gate_returns_before_any_other_work(self):
        environment = TrackingEnvironment({"NO_LIVE_API": "1", "PYTHONANYWHERE_TOKEN": TOKEN_CANARY})
        get = Mock(side_effect=AssertionError("unexpected HTTP call"))
        with (
            patch.object(sync.os, "environ", environment),
            patch.object(sync.httpx, "get", get),
            patch.object(sync, "safe_log") as gate_log,
            redirect_stdout(io.StringIO()) as output,
        ):
            result = sync.download_remote_subscriptions()
        self.assertEqual(result, [])
        self.assertEqual(environment.read_keys, ["NO_LIVE_API"])
        gate_log.assert_called_once_with(DOWNLOAD_LOG)
        get.assert_not_called()
        self.assertEqual(output.getvalue(), "")

    def test_sync_leaves_local_subscriptions_untouched(self):
        with tempfile.TemporaryDirectory() as tmp:
            local = Path(tmp) / "subscriptions.json"
            local.write_text(json.dumps([{"subscription_id": SUB_ID}]), encoding="utf-8")
            before = local.read_bytes()
            get = Mock(side_effect=AssertionError("unexpected HTTP call"))
            with (
                patch.dict(os.environ, {"NO_LIVE_API": "1", "PYTHONANYWHERE_TOKEN": TOKEN_CANARY}, clear=True),
                patch.object(sync, "LOCAL_SUBSCRIPTIONS", local),
                patch.object(sync.httpx, "get", get),
                redirect_stdout(io.StringIO()) as output,
            ):
                result = sync.sync_subscriptions()
            after = local.read_bytes()
            names = sorted(path.name for path in Path(tmp).iterdir())
        self.assertEqual(result, {"synced": False, "added": 0, "total": 1})
        self.assertEqual((after, names), (before, ["subscriptions.json"]))
        get.assert_not_called()
        self.assertIn(DOWNLOAD_LOG, output.getvalue())
        self.assertNotIn(TOKEN_CANARY, output.getvalue())


if __name__ == "__main__":
    unittest.main()
