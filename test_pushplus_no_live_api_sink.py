import ast
from contextlib import redirect_stderr, redirect_stdout
from copy import deepcopy
from io import StringIO
import os
from pathlib import Path
import unittest
from unittest.mock import patch

import main
import notifier
from pushplus_sections import PushRender, PushSection, prepare_push_render


# 保护范围声明：
# 本笔只保证 notifier.send() 在 effective NO_LIVE_API 精确等于 "1" 时：
# - 不读取 PushPlus token；
# - 不渲染或压缩通知正文；
# - 不调用 PushPlus HTTP 出口；
# - 不执行最小模板重试；
# - 不把通知正文写入本地回退日志；
# - 只记录一条固定、无动态内容的拒绝日志；
# - 返回现有失败语义 False。
# 该“跳过正文回退日志”是严格 no-live 模式下有意的行为差异。
# 本笔不证明 PythonAnywhere Files、Juhe、SerpAPI 或 Duffel 同样受
# NO_LIVE_API 保护。

PROJECT_ROOT = Path(__file__).resolve().parent
GATE_LOG = "[推送] NO_LIVE_API=1，已阻止真实 PushPlus 发送"
TITLE_CANARY = "TEST_ONLY_PUSHPLUS_TITLE_CANARY"
CONTENT_CANARY = "TEST_ONLY_PUSHPLUS_CONTENT_CANARY"
TOKEN_CANARY = "TEST_ONLY_PUSHPLUS_TOKEN_CANARY"
URL_CANARY = "https://example.invalid/detail?token=TEST_ONLY_PUSHPLUS_URL_CANARY"

EXPECTED_RED_TEST_IDS = frozenset(
    {
        "test_pushplus_no_live_api_sink.py::PushPlusNoLiveStaticContractTest::test_gate_is_first_business_statement_and_branch_is_fixed",
        "test_pushplus_no_live_api_sink.py::PushPlusNoLiveDynamicContractTest::test_exact_value_matrix",
        "test_pushplus_no_live_api_sink.py::PushPlusNoLiveDynamicContractTest::test_gate_strictly_short_circuits_all_content_and_network_work",
    }
)


def _function_node(module: ast.Module, name: str) -> ast.FunctionDef:
    for node in module.body:
        if isinstance(node, ast.FunctionDef) and node.name == name:
            return node
    raise AssertionError(f"function not found: {name}")


def _production_python_files():
    for path in sorted(PROJECT_ROOT.rglob("*.py")):
        relative = path.relative_to(PROJECT_ROOT)
        if path.name.startswith("test_") or relative.parts[:1] == ("tests",):
            continue
        if any(part in {".git", "__pycache__", "data"} for part in relative.parts):
            continue
        yield path, relative.as_posix()


def _pushplus_gateway_references():
    callers = set()
    forbidden_references = set()
    url_scopes = set()
    http_scopes = set()

    for path, relative in _production_python_files():
        tree = ast.parse(path.read_text(encoding="utf-8-sig"), filename=relative)
        module_aliases = set()
        direct_aliases = set()
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                for alias in node.names:
                    if alias.name == "notifier":
                        module_aliases.add(alias.asname or "notifier")
            elif isinstance(node, ast.ImportFrom) and node.module == "notifier":
                for alias in node.names:
                    if alias.name == "_post_pushplus":
                        direct_aliases.add(alias.asname or alias.name)
                        forbidden_references.add(
                            f"{relative}:<module>:from-import:_post_pushplus"
                        )

        for parent in ast.walk(tree):
            for child in ast.iter_child_nodes(parent):
                child._contract_parent = parent

        class Visitor(ast.NodeVisitor):
            def __init__(self):
                self.scope = [Path(relative).stem]

            @property
            def label(self):
                return ".".join(self.scope)

            def visit_FunctionDef(self, node):
                self.scope.append(node.name)
                self.generic_visit(node)
                self.scope.pop()

            visit_AsyncFunctionDef = visit_FunctionDef

            def visit_Call(self, node):
                is_gateway_call = False
                if (
                    relative == "notifier.py"
                    and isinstance(node.func, ast.Name)
                    and node.func.id == "_post_pushplus"
                ):
                    is_gateway_call = True
                elif isinstance(node.func, ast.Name) and node.func.id in direct_aliases:
                    is_gateway_call = True
                    forbidden_references.add(
                        f"{relative}:{self.label}:direct-call:_post_pushplus"
                    )
                elif (
                    isinstance(node.func, ast.Attribute)
                    and isinstance(node.func.value, ast.Name)
                    and node.func.value.id in module_aliases
                    and node.func.attr == "_post_pushplus"
                ):
                    is_gateway_call = True
                    forbidden_references.add(
                        f"{relative}:{self.label}:module-call:_post_pushplus"
                    )
                elif (
                    isinstance(node.func, ast.Name)
                    and node.func.id == "getattr"
                    and len(node.args) >= 2
                    and isinstance(node.args[0], ast.Name)
                    and node.args[0].id in module_aliases
                    and isinstance(node.args[1], ast.Constant)
                    and node.args[1].value == "_post_pushplus"
                ):
                    forbidden_references.add(
                        f"{relative}:{self.label}:dynamic-getattr:_post_pushplus"
                    )
                if is_gateway_call:
                    callers.add(self.label)

                if (
                    isinstance(node.func, ast.Attribute)
                    and node.func.attr == "post"
                    and any(
                        isinstance(part, ast.Constant)
                        and isinstance(part.value, str)
                        and "pushplus.plus" in part.value
                        for part in ast.walk(node)
                    )
                ):
                    http_scopes.add(self.label)
                self.generic_visit(node)

            def visit_Name(self, node):
                parent = getattr(node, "_contract_parent", None)
                is_called = isinstance(parent, ast.Call) and parent.func is node
                if (
                    relative == "notifier.py"
                    and node.id == "_post_pushplus"
                    and isinstance(node.ctx, ast.Load)
                    and not is_called
                ):
                    forbidden_references.add(
                        f"{relative}:{self.label}:callback-reference:_post_pushplus"
                    )

            def visit_Attribute(self, node):
                if (
                    isinstance(node.value, ast.Name)
                    and node.value.id in module_aliases
                    and node.attr == "_post_pushplus"
                ):
                    forbidden_references.add(
                        f"{relative}:{self.label}:module-reference:_post_pushplus"
                    )
                self.generic_visit(node)

            def visit_Constant(self, node):
                if isinstance(node.value, str) and "pushplus.plus" in node.value:
                    url_scopes.add(self.label)

        Visitor().visit(tree)

    return (
        frozenset(callers),
        frozenset(forbidden_references),
        frozenset(url_scopes),
        frozenset(http_scopes),
    )


class _TrackingEnvironment(dict):
    def __init__(self, values):
        super().__init__(values)
        self.read_keys = []

    def get(self, key, default=None):
        self.read_keys.append(key)
        if key != "NO_LIVE_API":
            raise AssertionError(f"environment read before no-live return: {key}")
        return super().get(key, default)


def _structured_render(*, oversized=False):
    sections = [
        PushSection("current_price", 0, "当前价:CNY100", True),
    ]
    if oversized:
        sections.append(PushSection("technical", 3, "技术" * 13000, False))
    return PushRender(TITLE_CANARY, tuple(sections), URL_CANARY)


class PushPlusNoLiveStaticContractTest(unittest.TestCase):
    def test_gate_is_first_business_statement_and_branch_is_fixed(self):
        tree = ast.parse((PROJECT_ROOT / "notifier.py").read_text(encoding="utf-8"))
        send_node = _function_node(tree, "send")
        body = list(send_node.body)
        if (
            body
            and isinstance(body[0], ast.Expr)
            and isinstance(body[0].value, ast.Constant)
            and isinstance(body[0].value.value, str)
        ):
            body.pop(0)

        self.assertIsInstance(body[0], ast.If)
        expected_condition = ast.parse(
            'os.environ.get("NO_LIVE_API") == "1"', mode="eval"
        ).body
        self.assertEqual(ast.dump(body[0].test), ast.dump(expected_condition))
        self.assertEqual(len(body[0].body), 2)

        log_statement, return_statement = body[0].body
        self.assertIsInstance(log_statement, ast.Expr)
        self.assertIsInstance(log_statement.value, ast.Call)
        self.assertIsInstance(log_statement.value.func, ast.Name)
        self.assertEqual(log_statement.value.func.id, "safe_log")
        self.assertEqual(len(log_statement.value.args), 1)
        self.assertIsInstance(log_statement.value.args[0], ast.Constant)
        self.assertEqual(log_statement.value.args[0].value, GATE_LOG)
        self.assertEqual(log_statement.value.keywords, [])
        self.assertIsInstance(return_statement, ast.Return)
        self.assertIsInstance(return_statement.value, ast.Constant)
        self.assertIs(return_statement.value.value, False)

    def test_pushplus_network_gateway_call_graph_is_exact(self):
        callers, forbidden, url_scopes, http_scopes = _pushplus_gateway_references()
        self.assertEqual(callers, frozenset({"notifier.send"}))
        self.assertEqual(forbidden, frozenset())
        self.assertEqual(url_scopes, frozenset({"notifier._post_pushplus"}))
        self.assertEqual(http_scopes, frozenset({"notifier._post_pushplus"}))


class PushPlusNoLiveDynamicContractTest(unittest.TestCase):
    def test_exact_value_matrix(self):
        cases = (
            ("1", False, 0, 1),
            (None, True, 1, 0),
            ("", True, 1, 0),
            ("0", True, 1, 0),
            ("true", True, 1, 0),
            ("01", True, 1, 0),
        )
        for value, expected_result, post_calls, gate_log_calls in cases:
            with self.subTest(value=value):
                environment = {"PUSHPLUS_TOKEN": TOKEN_CANARY}
                if value is not None:
                    environment["NO_LIVE_API"] = value
                with (
                    patch.dict(os.environ, environment, clear=True),
                    patch.object(
                        notifier, "_post_pushplus", return_value={"code": 200}
                    ) as post,
                    patch.object(notifier, "_log_notification") as local_log,
                    patch.object(notifier, "safe_log") as gate_log,
                ):
                    result = notifier.send("synthetic content", title="synthetic")

                self.assertIs(result, expected_result)
                self.assertEqual(post.call_count, post_calls)
                self.assertEqual(gate_log.call_count, gate_log_calls)
                local_log.assert_not_called()
                if post_calls:
                    post.assert_called_once_with(
                        TOKEN_CANARY, "synthetic", "synthetic content"
                    )
                else:
                    gate_log.assert_called_once_with(GATE_LOG)

    def test_gate_strictly_short_circuits_all_content_and_network_work(self):
        environment = _TrackingEnvironment(
            {"NO_LIVE_API": "1", "PUSHPLUS_TOKEN": TOKEN_CANARY}
        )
        output = StringIO()
        errors = StringIO()
        guarded_helpers = (
            "render_push_render",
            "_prepare_pushplus_content",
            "prepare_push_render",
            "_notification_title_from_content",
            "_post_pushplus",
            "_log_notification",
        )
        helper_patches = [
            patch.object(
                notifier,
                name,
                side_effect=AssertionError(f"unexpected helper call: {name}"),
            )
            for name in guarded_helpers
        ]
        started = []
        try:
            for helper_patch in helper_patches:
                started.append(helper_patch.start())
            with (
                patch.object(notifier.os, "environ", environment),
                patch.object(
                    notifier.httpx,
                    "post",
                    side_effect=AssertionError("unexpected HTTP call"),
                ) as http_post,
                patch.object(notifier, "safe_log") as gate_log,
                redirect_stdout(output),
                redirect_stderr(errors),
            ):
                result = notifier.send(
                    _structured_render(),
                    title=f"{TITLE_CANARY}:{CONTENT_CANARY}",
                )
        finally:
            for helper_patch in reversed(helper_patches):
                helper_patch.stop()

        self.assertFalse(result)
        self.assertEqual(environment.read_keys, ["NO_LIVE_API"])
        gate_log.assert_called_once_with(GATE_LOG)
        http_post.assert_not_called()
        for helper in started:
            helper.assert_not_called()
        observed = output.getvalue() + errors.getvalue() + repr(gate_log.call_args_list)
        for canary in (TITLE_CANARY, CONTENT_CANARY, TOKEN_CANARY, URL_CANARY):
            self.assertNotIn(canary, observed)


class PushPlusNormalPathCharacterizationTest(unittest.TestCase):
    def test_structured_send_success_is_unchanged(self):
        render = _structured_render()
        with (
            patch.dict(
                os.environ,
                {"NO_LIVE_API": "0", "PUSHPLUS_TOKEN": TOKEN_CANARY},
                clear=True,
            ),
            patch.object(
                notifier, "_post_pushplus", return_value={"code": 200}
            ) as post,
            patch.object(notifier, "_log_notification") as local_log,
        ):
            result = notifier.send(render, title="fallback title")

        self.assertTrue(result)
        self.assertEqual(post.call_count, 1)
        token, title, content = post.call_args.args
        self.assertEqual(token, TOKEN_CANARY)
        self.assertEqual(title, TITLE_CANARY)
        self.assertIn("当前价:CNY100", content)
        local_log.assert_not_called()

    def test_structured_empty_response_retries_minimal_template(self):
        render = _structured_render(oversized=True)
        expected_minimal = prepare_push_render(
            render, compact_chars=0, max_chars=0
        ).content
        with (
            patch.dict(
                os.environ,
                {"NO_LIVE_API": "0", "PUSHPLUS_TOKEN": TOKEN_CANARY},
                clear=True,
            ),
            patch.object(
                notifier,
                "_post_pushplus",
                side_effect=(None, {"code": 200}),
            ) as post,
            patch.object(notifier, "_log_notification") as local_log,
        ):
            result = notifier.send(render)

        self.assertTrue(result)
        self.assertEqual(post.call_count, 2)
        first_content = post.call_args_list[0].args[2]
        second_content = post.call_args_list[1].args[2]
        self.assertNotEqual(first_content, second_content)
        self.assertEqual(second_content, expected_minimal)
        local_log.assert_not_called()

    def test_unstructured_long_content_uses_safe_warning_and_logs_original(self):
        original = CONTENT_CANARY + ("x" * 25001)
        with (
            patch.dict(
                os.environ,
                {"NO_LIVE_API": "0", "PUSHPLUS_TOKEN": TOKEN_CANARY},
                clear=True,
            ),
            patch.object(
                notifier, "_post_pushplus", return_value={"code": 200}
            ) as post,
            patch.object(notifier, "_log_notification") as local_log,
        ):
            result = notifier.send(original, title="synthetic")

        self.assertTrue(result)
        local_log.assert_called_once_with(original)
        post.assert_called_once_with(
            TOKEN_CANARY,
            "synthetic",
            notifier._generic_long_pushplus_warning(),
        )

    def test_missing_token_logs_original_and_returns_false(self):
        with (
            patch.dict(os.environ, {"NO_LIVE_API": "0"}, clear=True),
            patch.object(notifier, "_post_pushplus") as post,
            patch.object(notifier, "_log_notification") as local_log,
        ):
            result = notifier.send(CONTENT_CANARY, title=TITLE_CANARY)

        self.assertFalse(result)
        post.assert_not_called()
        local_log.assert_called_once_with(CONTENT_CANARY)

    def test_final_send_failure_logs_original_and_returns_false(self):
        with (
            patch.dict(
                os.environ,
                {"NO_LIVE_API": "0", "PUSHPLUS_TOKEN": TOKEN_CANARY},
                clear=True,
            ),
            patch.object(
                notifier, "_post_pushplus", return_value={"code": 503}
            ) as post,
            patch.object(notifier, "_log_notification") as local_log,
        ):
            result = notifier.send(CONTENT_CANARY, title=TITLE_CANARY)

        self.assertFalse(result)
        post.assert_called_once_with(TOKEN_CANARY, TITLE_CANARY, CONTENT_CANARY)
        local_log.assert_called_once_with(CONTENT_CANARY)


class PushPlusCallerCompatibilityTest(unittest.TestCase):
    def test_subscription_failure_preserves_channel_semantics_when_push_fails(self):
        cases = (
            ("pushplus", False, 0, 1, True),
            ("both", True, 1, 1, False),
            ("page_only", False, 0, 0, True),
        )
        for method, expected, email_calls, push_calls, records_failure in cases:
            with self.subTest(method=method):
                subscription = {
                    "subscription_id": "00000000-0000-4000-8000-000000000001",
                    "origin": "AAA",
                    "destination": "BBB",
                    "notification_goals": {
                        "method": method,
                        "email": "recipient@example.invalid",
                    },
                }
                with (
                    patch.object(main, "send_email", return_value=True) as email_send,
                    patch.object(main, "send", return_value=False) as push_send,
                    patch.object(main, "safe_log"),
                ):
                    result = main._notify_subscription_failure(
                        subscription, reason="synthetic failure"
                    )

                self.assertIs(result, expected)
                self.assertEqual(email_send.call_count, email_calls)
                self.assertEqual(push_send.call_count, push_calls)
                self.assertEqual("last_failure" in subscription, records_failure)

    def test_system_alert_preserves_both_channel_result_when_push_fails(self):
        subscriptions = [
            {
                "notification_goals": {
                    "method": "both",
                    "email": "recipient@example.invalid",
                }
            }
        ]
        with (
            patch.object(main, "send_email", return_value=True) as email_send,
            patch.object(main, "send", return_value=False) as push_send,
            patch.object(main, "safe_log"),
        ):
            result = main._notify_system_alert(
                subscriptions, "synthetic alert", "synthetic content"
            )

        self.assertTrue(result)
        email_send.assert_called_once_with(
            "recipient@example.invalid",
            "synthetic alert",
            "synthetic content",
            {},
        )
        push_send.assert_called_once_with(
            "synthetic content", title="synthetic alert"
        )

    def test_delivery_preserves_pushplus_both_and_page_only_results(self):
        cases = (
            ("pushplus", False, 0, 1, 0),
            ("both", True, 1, 1, 1),
            ("page_only", True, 0, 0, 0),
        )
        for method, expected, email_calls, push_calls, persist_calls in cases:
            with self.subTest(method=method):
                payload = {
                    "push_type": "synthetic",
                    "route": "AAA->BBB",
                    "recommended_plans": [],
                }
                payload_before = deepcopy(payload)
                subscription = {
                    "subscription_id": "00000000-0000-4000-8000-000000000002",
                    "notification_goals": {
                        "method": method,
                        "email": "recipient@example.invalid",
                    },
                }
                push_render = object()
                with (
                    patch.object(
                        main, "build_notification_payload", return_value=payload
                    ),
                    patch.object(main, "feedback_acknowledgement", return_value=None),
                    patch.object(
                        main,
                        "delivery_payload_with_detail_token",
                        return_value=payload,
                    ),
                    patch.object(
                        main,
                        "render_email",
                        return_value=("subject", "<p>body</p>", {}),
                    ),
                    patch.object(main, "render_detail_html", return_value="<p>detail</p>"),
                    patch.object(main, "_save_result_for_page", return_value=True),
                    patch.object(
                        main, "render_pushplus_sections", return_value=push_render
                    ),
                    patch.object(main, "send_email", return_value=True) as email_send,
                    patch.object(main, "send", return_value=False) as push_send,
                    patch.object(main, "persist_notification_payload") as persist,
                ):
                    result = main._deliver_notification(
                        subscription, "AAA->BBB", {"route_info": {}}
                    )

                self.assertIs(result, expected)
                self.assertEqual(email_send.call_count, email_calls)
                self.assertEqual(push_send.call_count, push_calls)
                self.assertEqual(persist.call_count, persist_calls)
                if push_calls:
                    push_send.assert_called_once_with(
                        push_render, title="【synthetic】AAA->BBB"
                    )
                self.assertEqual(payload, payload_before)


if __name__ == "__main__":
    unittest.main()
