"""Unsupported string inputs fail closed; optional-token policy is unchanged."""

import ast
from contextlib import ExitStack, redirect_stderr, redirect_stdout
import importlib
import inspect
import io
import os
from pathlib import Path
import tempfile
import textwrap
import unittest
from unittest.mock import patch

import detail_access


ASCII_TOKEN = "synthetic-detail-" + "a" * 43
NONASCII = "\u4e2d\u6587"
UNSUPPORTED_CASES = (
    ("nonascii_candidate", NONASCII, ASCII_TOKEN),
    ("nonascii_configured", ASCII_TOKEN, NONASCII),
    ("same_nonascii", NONASCII, NONASCII),
)
EMPTY_CASES = (("nonascii", NONASCII), ("ascii", ASCII_TOKEN), ("missing", None))
VALID_ID = "00000000-0000-0000-0000-000000000001"


class DetailTokenInputSafetyTest(unittest.TestCase):
    def setUp(self):
        self.stack = ExitStack()
        self.addCleanup(self.stack.close)
        self.stack.enter_context(patch.dict(os.environ, {
            "NO_LIVE_API": "1", "PYTHON_DOTENV_DISABLED": "1",
            "FLASK_SECRET_KEY": "detail-contract-session-" + "s" * 43,
            "SHARED_DETAIL_TOKEN": ASCII_TOKEN,
            "MANAGEMENT_AUTH_REQUIRED": "1",
            "MANAGEMENT_TOKEN": "detail-contract-management-" + "m" * 43,
        }, clear=True))
        self.stack.enter_context(patch("dotenv.load_dotenv", return_value=False))
        self.stack.enter_context(patch("dotenv.dotenv_values", return_value={}, create=True))
        self.denials = {}
        for name in (
            "socket.socket.connect", "socket.socket.connect_ex", "socket.socket.sendto",
            "socket.create_connection", "socket.getaddrinfo", "smtplib.SMTP",
            "smtplib.SMTP_SSL", "sqlite3.connect", "requests.sessions.Session.request",
            "httpx.get", "httpx.post", "httpx.request", "httpx.Client", "httpx.AsyncClient",
        ):
            self.denials[name] = self.stack.enter_context(
                patch(name, side_effect=AssertionError(name), create=True)
            )
        self.stdout, self.stderr = io.StringIO(), io.StringIO()
        self.stack.enter_context(redirect_stdout(self.stdout))
        self.stack.enter_context(redirect_stderr(self.stderr))
        self.addCleanup(self._assert_no_outbound)

    def _assert_no_outbound(self):
        for name, mock in self.denials.items():
            self.assertEqual(mock.call_count, 0, name)

    def _assert_rejected(self, authorize, candidate, configured):
        self.assertIs(authorize(candidate, configured=configured), False,
                      "unsupported_input_not_rejected")

    def _assert_empty_allowed(self, authorize, candidate):
        self.assertIs(authorize(candidate, configured=""), True, "empty_config_policy_changed")

    def _assert_comparison_identity(self, authorize):
        with patch.object(detail_access.hmac, "compare_digest", return_value=True) as compare:
            self.assertIs(authorize("a token&value", configured="a token&value"), True)
            self.assertEqual(compare.call_count, 1, "comparison_primitive_not_called")
            compare.assert_called_once_with("a token&value", "a token&value")

    def _assert_comparison_result(self, authorize):
        with patch.object(detail_access.hmac, "compare_digest", return_value=False):
            self.assertIs(authorize(ASCII_TOKEN, configured=ASCII_TOKEN), False,
                          "comparison_result_ignored")

    def test_unsupported_inputs(self):
        for case, candidate, configured in UNSUPPORTED_CASES:
            with self.subTest(case=case):
                self._assert_rejected(detail_access.detail_token_authorized, candidate, configured)

    def test_ascii_inputs(self):
        cases = (
            ("correct", ASCII_TOKEN, ASCII_TOKEN, True),
            ("space_and_symbol", "a token&value", "a token&value", True),
            ("wrong", "wrong", ASCII_TOKEN, False),
            ("missing", None, ASCII_TOKEN, False),
            ("empty", "", ASCII_TOKEN, False),
            ("case_sensitive", "TOKEN", "token", False),
            ("short", "x", "x", True),
            ("ascii_control", "\x00\t", "\x00\t", True),
            ("existing_str_conversion", 123, 123, True),
        )
        for case, candidate, configured, expected in cases:
            with self.subTest(case=case):
                self.assertIs(detail_access.detail_token_authorized(candidate, configured=configured),
                              expected)
        self.assertEqual(self.stdout.getvalue(), "")
        self.assertEqual(self.stderr.getvalue(), "")

    def test_empty_configuration_still_disables_validation(self):
        for case, candidate in EMPTY_CASES:
            with self.subTest(case=case):
                self._assert_empty_allowed(detail_access.detail_token_authorized, candidate)

    def test_environment_and_explicit_normalization_remain_distinct(self):
        with patch.dict(os.environ, {"SHARED_DETAIL_TOKEN": " a token&value "}):
            self.assertEqual(detail_access.shared_detail_token(), "a token&value")
            self.assertIs(detail_access.detail_token_authorized("a token&value"), True)
            self.assertIs(detail_access.detail_token_authorized(" a token&value "), False)
        self.assertIs(detail_access.detail_token_authorized(
            "a token&value", configured=" a token&value "), False)
        self.assertIs(detail_access.detail_token_authorized(
            " a token&value ", configured=" a token&value "), True)
        with patch.dict(os.environ, {"SHARED_DETAIL_TOKEN": "   "}):
            self.assertIs(detail_access.detail_token_authorized(NONASCII), True)

    def test_comparison_uses_original_arguments_once(self):
        self._assert_comparison_identity(detail_access.detail_token_authorized)

    def test_comparison_result_controls_authorization(self):
        self._assert_comparison_result(detail_access.detail_token_authorized)

    def test_unsupported_inputs_skip_comparison(self):
        for case, candidate, configured in UNSUPPORTED_CASES:
            with self.subTest(case=case), patch.object(
                detail_access.hmac, "compare_digest", wraps=detail_access.hmac.compare_digest
            ) as compare:
                self._assert_rejected(detail_access.detail_token_authorized, candidate, configured)
                compare.assert_not_called()
        self.assertEqual(self.stdout.getvalue(), "")
        self.assertEqual(self.stderr.getvalue(), "")

    def _route(self):
        # Install import isolation before the application can load dotenv or any client.
        web = importlib.import_module("web_form")
        root = Path(self.stack.enter_context(tempfile.TemporaryDirectory()))
        for name, leaf in (("PAGE_PAYLOADS_DIR", "payloads"),
                           ("SUBSCRIPTIONS_PATH", "subscriptions.json"),
                           ("FEEDBACK_PATH", "feedback.json")):
            self.stack.enter_context(patch.object(web, name, root / leaf))
        self.stack.enter_context(patch.dict(web.app.config, {
            "TESTING": True, "DEBUG": False, "PROPAGATE_EXCEPTIONS": True,
            "SECRET_KEY": "detail-contract-session-" + "s" * 43,
            "SESSION_COOKIE_SECURE": False,
        }))
        for name in ("load_calendar", "_subscription_repository", "load_subscriptions",
                     "start_background_collection", "save_feedback", "notify_feedback_author"):
            self.denials[name] = self.stack.enter_context(
                patch.object(web, name, side_effect=AssertionError(name))
            )
        self.addCleanup(lambda: self.assertEqual(list(root.rglob("*")), []))
        self.assertIs(web.detail_token_authorized, detail_access.detail_token_authorized)
        return web, web.app.test_client()

    def test_route_rejects_unsupported_before_payload_read(self):
        web, client = self._route()
        for case, candidate, configured in UNSUPPORTED_CASES[:2]:
            with self.subTest(case=case), patch.dict(os.environ, {"SHARED_DETAIL_TOKEN": configured}), \
                    patch.object(web, "_load_payload_result") as load:
                response = client.get("/detail", query_string={"sub": VALID_ID, "token": candidate})
                self.assertEqual(response.status_code, 404)
                load.assert_not_called()

    def test_route_ascii_control_reaches_payload_stub(self):
        web, client = self._route()
        with patch.object(web, "_load_payload_result", return_value={
            "html": "<p>synthetic-detail</p>", "payload": {},
        }) as load:
            response = client.get("/detail", query_string={"sub": VALID_ID, "token": ASCII_TOKEN})
            self.assertEqual(response.status_code, 200)
            self.assertIn(b"synthetic-detail", response.data)
            load.assert_called_once_with(VALID_ID)

    def _mutated_authorizer(self, mutation):
        source = textwrap.dedent(inspect.getsource(detail_access.detail_token_authorized))
        tree = ast.parse(source)
        before = ast.dump(tree, include_attributes=False)
        matched = 0

        class Mutate(ast.NodeTransformer):
            def visit_If(self, node):
                nonlocal matched
                if mutation == "remove_ascii_guard" and ast.dump(node.test) == ast.dump(ast.parse(
                    "not supplied.isascii() or not expected.isascii()", mode="eval").body):
                    matched += 1
                    return None
                if mutation == "disable_empty_policy" and ast.dump(node.test) == ast.dump(ast.parse(
                    "not expected", mode="eval").body):
                    if len(node.body) == 1 and isinstance(node.body[0], ast.Return) \
                            and isinstance(node.body[0].value, ast.Constant) \
                            and node.body[0].value.value is True:
                        matched += 1
                        node.body[0].value = ast.Constant(False)
                return self.generic_visit(node)

            def visit_Call(self, node):
                nonlocal matched
                if mutation == "replace_comparison" and ast.dump(node) == ast.dump(ast.parse(
                    "hmac.compare_digest(supplied, expected)", mode="eval").body):
                    matched += 1
                    return ast.copy_location(ast.Compare(
                        left=ast.Name(id="supplied", ctx=ast.Load()), ops=[ast.Eq()],
                        comparators=[ast.Name(id="expected", ctx=ast.Load())]), node)
                return self.generic_visit(node)

        changed = ast.fix_missing_locations(Mutate().visit(tree))
        self.assertEqual(matched, 1, "mutation_node_count")
        self.assertNotEqual(ast.dump(changed, include_attributes=False), before, "mutation_no_change")
        namespace = dict(detail_access.__dict__)
        exec(compile(changed, "<detail-token-mutation>", "exec"), namespace)
        return namespace["detail_token_authorized"]

    def test_mutation_removed_guard_is_rejected(self):
        authorize = self._mutated_authorizer("remove_ascii_guard")
        for case, candidate, configured in UNSUPPORTED_CASES:
            with self.subTest(case=case), self.assertRaisesRegex(
                TypeError, "comparing strings with non-ASCII characters is not supported"
            ):
                self._assert_rejected(authorize, candidate, configured)

    def test_mutation_replaced_comparison_is_rejected(self):
        authorize = self._mutated_authorizer("replace_comparison")
        with self.assertRaisesRegex(AssertionError, "comparison_primitive_not_called"):
            self._assert_comparison_identity(authorize)
        with self.assertRaisesRegex(AssertionError, "comparison_result_ignored"):
            self._assert_comparison_result(authorize)

    def test_mutation_disabled_empty_policy_is_rejected(self):
        authorize = self._mutated_authorizer("disable_empty_policy")
        for case, candidate in EMPTY_CASES:
            with self.subTest(case=case), self.assertRaisesRegex(
                AssertionError, "empty_config_policy_changed"
            ):
                self._assert_empty_allowed(authorize, candidate)


if __name__ == "__main__":
    unittest.main()
