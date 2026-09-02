from __future__ import annotations

import ast
from contextlib import redirect_stderr, redirect_stdout
import inspect
import io
import os
import textwrap
import unittest
from unittest.mock import Mock, patch

import email_notifier
import main
import web_form


EXPECTED_RED_TEST_IDS = frozenset(
    {
        "test_email_no_live_api_sink.py::EmailNoLiveApiSinkContractTest::test_exact_one_blocks_before_configuration_and_smtp_operations",
        "test_email_no_live_api_sink.py::EmailNoLiveApiSinkContractTest::test_exact_value_matrix_only_one_blocks",
        "test_email_no_live_api_sink.py::EmailNoLiveApiSinkContractTest::test_scope_comment_and_first_business_statement_are_exact",
    }
)

GATE_LOG = "[邮件] NO_LIVE_API=1，已阻止真实 SMTP 发送"


def _config(*, ssl: bool) -> dict:
    return {
        "host": "smtp.example.invalid",
        "port": 465 if ssl else 587,
        "ssl": ssl,
        "provider": "qq",
    }


class EmailNoLiveApiSinkContractTest(unittest.TestCase):
    def test_scope_comment_and_first_business_statement_are_exact(self):
        source = inspect.getsource(email_notifier.send_email)
        # 本笔只保证 email_notifier.send_email() 在 effective NO_LIVE_API 精确等于 "1" 时拒绝 SMTP 发送。
        # 它不证明 PushPlus、PythonAnywhere Files、Juhe、SerpAPI、Duffel 或任何其他 sink 同样受该变量保护。
        self.assertIn(
            '本笔只保证 email_notifier.send_email() 在 effective NO_LIVE_API 精确等于 "1" 时拒绝 SMTP 发送。',
            source,
        )
        self.assertIn(
            "它不证明 PushPlus、PythonAnywhere Files、Juhe、SerpAPI、Duffel 或任何其他 sink 同样受该变量保护。",
            source,
        )

        function = ast.parse(textwrap.dedent(source)).body[0]
        self.assertIsInstance(function, ast.FunctionDef)
        first_business_statement = function.body[1]
        self.assertIsInstance(first_business_statement, ast.If)
        expected_test = ast.parse(
            'os.environ.get("NO_LIVE_API") == "1"', mode="eval"
        ).body
        self.assertEqual(
            ast.dump(first_business_statement.test, include_attributes=False),
            ast.dump(expected_test, include_attributes=False),
        )
        self.assertEqual(len(first_business_statement.body), 2)
        log_statement, return_statement = first_business_statement.body
        self.assertIsInstance(log_statement, ast.Expr)
        self.assertIsInstance(log_statement.value, ast.Call)
        self.assertEqual(log_statement.value.func.id, "safe_log")
        self.assertEqual(log_statement.value.args[0].value, GATE_LOG)
        self.assertIsInstance(return_statement, ast.Return)
        self.assertIs(return_statement.value.value, False)

    def test_exact_value_matrix_only_one_blocks(self):
        cases = (
            (None, False),
            ("", False),
            ("0", False),
            ("true", False),
            ("01", False),
            ("1", True),
        )
        for value, should_block in cases:
            with self.subTest(value=value):
                environment = {
                    "SMTP_USER": "sender@example.invalid",
                    "SMTP_PASS": "test-password",
                }
                if value is not None:
                    environment["NO_LIVE_API"] = value
                server = Mock()
                with (
                    patch.dict(os.environ, environment, clear=True),
                    patch.object(
                        email_notifier, "_smtp_config", return_value=_config(ssl=True)
                    ) as config_mock,
                    patch.object(
                        email_notifier.smtplib, "SMTP_SSL", return_value=server
                    ) as smtp_ssl,
                    patch.object(email_notifier.smtplib, "SMTP") as smtp,
                    patch.object(email_notifier, "safe_log"),
                ):
                    result = email_notifier.send_email(
                        "recipient@example.invalid", "subject", "<p>body</p>"
                    )

                if should_block:
                    config_mock.assert_not_called()
                    smtp_ssl.assert_not_called()
                    smtp.assert_not_called()
                    server.login.assert_not_called()
                    server.sendmail.assert_not_called()
                    self.assertFalse(result)
                else:
                    config_mock.assert_called_once_with()
                    smtp_ssl.assert_called_once_with(
                        "smtp.example.invalid", 465, timeout=30
                    )
                    smtp.assert_not_called()
                    server.login.assert_called_once_with(
                        "sender@example.invalid", "test-password"
                    )
                    self.assertTrue(result)

    def test_exact_one_blocks_before_configuration_and_smtp_operations(self):
        recipient = "smtp-recipient-canary@example.invalid"
        subject = "SMTP_SUBJECT_TEST_CANARY"
        body = "SMTP_BODY_TEST_CANARY"
        password = "SMTP_PASSWORD_TEST_CANARY"
        server = Mock()
        stdout = io.StringIO()
        stderr = io.StringIO()
        environment = {
            "NO_LIVE_API": "1",
            "SMTP_PORT": "not-an-integer",
            "SMTP_USER": "sender@example.invalid",
            "SMTP_PASS": password,
        }
        with (
            patch.dict(os.environ, environment, clear=True),
            patch.object(
                email_notifier, "_smtp_config", return_value=_config(ssl=True)
            ) as config_mock,
            patch.object(
                email_notifier.smtplib, "SMTP_SSL", return_value=server
            ) as smtp_ssl,
            patch.object(email_notifier.smtplib, "SMTP") as smtp,
            patch.object(email_notifier.socket, "create_connection") as socket_connect,
            patch.object(email_notifier, "safe_log") as safe_log,
            redirect_stdout(stdout),
            redirect_stderr(stderr),
        ):
            result = email_notifier.send_email(recipient, subject, body)

        config_mock.assert_not_called()
        smtp_ssl.assert_not_called()
        smtp.assert_not_called()
        socket_connect.assert_not_called()
        server.login.assert_not_called()
        server.sendmail.assert_not_called()
        safe_log.assert_called_once_with(GATE_LOG)
        self.assertFalse(result)
        observable = stdout.getvalue() + stderr.getvalue() + repr(safe_log.mock_calls)
        for canary in (recipient, subject, body, password):
            self.assertNotIn(canary, observable)

    def test_ssl_and_starttls_paths_are_unchanged_when_gate_is_inactive(self):
        cases = ((True, None), (False, "0"))
        for use_ssl, no_live_value in cases:
            with self.subTest(use_ssl=use_ssl, no_live_value=no_live_value):
                environment = {
                    "SMTP_USER": "sender@example.invalid",
                    "SMTP_PASS": "test-password",
                }
                if no_live_value is not None:
                    environment["NO_LIVE_API"] = no_live_value
                server = Mock()
                with (
                    patch.dict(os.environ, environment, clear=True),
                    patch.object(
                        email_notifier,
                        "_smtp_config",
                        return_value=_config(ssl=use_ssl),
                    ),
                    patch.object(
                        email_notifier.smtplib, "SMTP_SSL", return_value=server
                    ) as smtp_ssl,
                    patch.object(
                        email_notifier.smtplib, "SMTP", return_value=server
                    ) as smtp,
                    patch.object(email_notifier, "safe_log"),
                ):
                    result = email_notifier.send_email(
                        "recipient@example.invalid", "subject", "<p>body</p>"
                    )

                self.assertTrue(result)
                if use_ssl:
                    smtp_ssl.assert_called_once_with(
                        "smtp.example.invalid", 465, timeout=30
                    )
                    smtp.assert_not_called()
                    server.starttls.assert_not_called()
                else:
                    smtp.assert_called_once_with(
                        "smtp.example.invalid", 587, timeout=30
                    )
                    smtp_ssl.assert_not_called()
                    server.starttls.assert_called_once_with()
                server.login.assert_called_once_with(
                    "sender@example.invalid", "test-password"
                )
                self.assertEqual(server.sendmail.call_count, 1)
                server.quit.assert_called_once_with()


class EmailSinkCallerCompatibilityTest(unittest.TestCase):
    def test_subscription_failure_preserves_email_both_and_page_only_semantics(self):
        cases = (
            ("email", False, 1, 0, True),
            ("both", True, 1, 1, False),
            ("page_only", False, 0, 0, True),
        )
        for method, expected_result, email_calls, push_calls, records_failure in cases:
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
                    patch.object(main, "send_email", return_value=False) as email_send,
                    patch.object(main, "send", return_value=True) as push_send,
                    patch.object(main, "safe_log"),
                ):
                    result = main._notify_subscription_failure(
                        subscription, reason="synthetic failure"
                    )

                self.assertIs(result, expected_result)
                self.assertEqual(email_send.call_count, email_calls)
                self.assertEqual(push_send.call_count, push_calls)
                self.assertEqual("last_failure" in subscription, records_failure)

    def test_system_alert_preserves_email_false_then_pushplus_fallback(self):
        subscriptions = [
            {
                "notification_goals": {
                    "method": "both",
                    "email": "recipient@example.invalid",
                }
            }
        ]
        with (
            patch.object(main, "send_email", return_value=False) as email_send,
            patch.object(main, "send", return_value=True) as push_send,
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

    def test_delivery_preserves_both_fallback_and_page_only_zero_smtp(self):
        cases = (("both", True, 1, 1, 1), ("page_only", True, 0, 0, 0))
        for method, expected_result, email_calls, push_calls, persist_calls in cases:
            with self.subTest(method=method):
                payload = {
                    "push_type": "synthetic",
                    "route": "AAA->BBB",
                    "recommended_plans": [],
                }
                subscription = {
                    "subscription_id": "00000000-0000-4000-8000-000000000002",
                    "notification_goals": {
                        "method": method,
                        "email": "recipient@example.invalid",
                    },
                }
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
                    patch.object(main, "render_pushplus_sections", return_value=object()),
                    patch.object(main, "send_email", return_value=False) as email_send,
                    patch.object(main, "send", return_value=True) as push_send,
                    patch.object(main, "persist_notification_payload") as persist,
                ):
                    result = main._deliver_notification(
                        subscription, "AAA->BBB", {"route_info": {}}
                    )

                self.assertIs(result, expected_result)
                self.assertEqual(email_send.call_count, email_calls)
                self.assertEqual(push_send.call_count, push_calls)
                self.assertEqual(persist.call_count, persist_calls)

    def test_feedback_author_uses_delayed_email_notifier_lookup(self):
        record = {
            "subscription_id": "00000000-0000-4000-8000-000000000003",
            "feedback_type": "synthetic",
        }
        with (
            patch.dict(
                os.environ,
                {"FEEDBACK_NOTIFY_EMAIL": "author@example.invalid"},
                clear=True,
            ),
            patch("email_notifier.send_email", return_value=False) as email_send,
            patch.object(web_form, "safe_log"),
        ):
            result = web_form.notify_feedback_author(record)

        self.assertFalse(result)
        email_send.assert_called_once()


if __name__ == "__main__":
    unittest.main()
