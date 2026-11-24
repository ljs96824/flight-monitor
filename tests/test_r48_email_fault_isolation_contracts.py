import copy
import email
import hashlib
import inspect
import io
import json
import os
from contextlib import contextmanager, redirect_stderr, redirect_stdout
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import Mock, patch

import config_loader
import email_notifier
import main
import notifier
import test_email_no_live_api_sink
import web_form
from tests import test_delivery_send_exception_state, test_email_render_by_channel


HOST = "smtp.example.invalid"
SENDER = "sender@example.invalid"
TO = "recipient@example.invalid"
PASS = "R48_SMTP_PASS_CANARY"
CID_HTML = '<img src="cid:trendchart">'
FIXTURE = Path(__file__).parent / "fixtures" / "frozen_email" / "economy_payload.json"


@contextmanager
def offline_smtp(**overrides):
    env = {"NO_LIVE_API": "0", "SMTP_PROVIDER": "qq", "SMTP_HOST": HOST, "SMTP_PORT": "465",
           "SMTP_SSL": "true", "SMTP_USER": SENDER, "SMTP_PASS": PASS, **overrides}
    server, out = Mock(), io.StringIO()
    with (patch.object(email_notifier.smtplib, "SMTP_SSL", return_value=server) as ssl,
          patch.object(email_notifier.smtplib, "SMTP", return_value=server) as smtp,
          patch.object(email_notifier.socket, "create_connection", side_effect=AssertionError("unexpected socket")) as sock,
          patch.dict(os.environ, env), redirect_stdout(out), redirect_stderr(out)):
        yield server, ssl, smtp, sock, out


class MailContract(unittest.TestCase):
    def boolean_call(self, func, *args, **kwargs):
        try:
            value = func(*args, **kwargs)
        except Exception as exc:
            self.fail(f"unexpected uncaught {type(exc).__name__}: {exc}")
        self.assertIs(type(value), bool)
        return value

    def send(self, recipient=TO, html="<p>update</p>", images=None, **kwargs):
        return self.boolean_call(email_notifier.send_email, recipient, "Update", html, images, **kwargs)

    def assert_private_log(self, out):
        for private in (PASS, TO, SENDER):
            self.assertNotIn(private, out.getvalue())


class TestTrack1SmtpConfigAndMimeFaultBoundary(MailContract):
    def test_g01_invalid_smtp_port_and_host_isolated_without_socket_or_secret_leak(self):
        cases = [("SMTP_PORT", p) for p in ("465,587", "abc", "465.0", "   ", "0", "-1", "65536", "70000")]
        cases += [("SMTP_HOST", h) for h in ("   ", "bad host", "smtp.example.invalid\r\nBcc:x")]
        for key, value in cases:
            with offline_smtp(**{key: value}) as (server, ssl, smtp, sock, out):
                with self.assertRaises(ValueError):
                    email_notifier._smtp_config()
                self.assertFalse(self.send())
            for mock in (ssl, smtp, sock, server.sendmail):
                mock.assert_not_called()
            self.assertIn("[邮件] SMTP配置无效", out.getvalue())
            self.assert_private_log(out)

    def test_g02_mime_assembly_header_crlf_and_transport_error_redaction(self):
        with offline_smtp() as (server, ssl, smtp, sock, out):
            self.assertFalse(self.send(html=CID_HTML, images={"trendchart": b"not-an-image"}, privacy_level="full"))
            self.assertFalse(self.send(recipient=TO + "\r\ninvalid"))
            with patch.dict(os.environ, {"SMTP_USER": SENDER + "\r\ninvalid"}):
                self.assertFalse(self.send())
            for config in ({}, {"host": HOST, "port": True, "ssl": True, "provider": "qq"}):
                with patch.object(email_notifier, "_smtp_config", return_value=config) as getter:
                    self.assertFalse(self.send())
                getter.assert_called_once_with()
            error = f"synthetic {PASS} {TO} {SENDER}"
            with patch.object(email_notifier, "_smtp_config", side_effect=ValueError(error)):
                self.assertFalse(self.send())
            with patch.object(email_notifier.MIMEMultipart, "as_string", side_effect=ValueError(error)):
                self.assertFalse(self.send())
            self.assertIn("[邮件] 邮件构造失败", out.getvalue())
            for mock in (ssl, smtp, sock, server.sendmail):
                mock.assert_not_called()
            for exc in (RuntimeError(error), OSError(error)):
                server.login.side_effect = exc
                self.assertFalse(self.send())
            self.assertEqual(server.quit.call_count, 2)
            server.sendmail.assert_not_called()
        self.assert_private_log(out)


class TestTrack2BothChannelIsolationAndOrContract(MailContract):
    def test_g03_deliver_notification_both_isolates_email_faults_and_preserves_or_contract(self):
        cases = (("both", "465,587", None, True), ("both", "465,587", None, False),
                 ("both", "465", {"trendchart": b"corrupt"}, True), ("email", "465,587", None, None))
        for method, port, images, push_ok in cases:
            sub = {"id": "test-r48", "notification_goals": {"email": TO, "method": method}}
            with (offline_smtp(SMTP_PORT=port) as (server, ssl, smtp, sock, out),
                  patch.object(main, "build_notification_payload", return_value={"route": "AAA-BBB"}),
                  patch.object(main, "feedback_acknowledgement", return_value=None),
                  patch.object(main, "render_email", return_value=("Update", CID_HTML, images)),
                  patch.object(main, "render_detail_html", return_value="<p>detail</p>"),
                  patch.object(main, "_save_result_for_page", return_value=True),
                  patch.object(main, "render_pushplus_sections", return_value="update"),
                  patch.object(main, "send", return_value=push_ok) as push,
                  patch.object(main, "persist_notification_payload") as persist):
                sent = main._deliver_notification(sub, "AAA-BBB", {})
            self.assertIs(sent, bool(push_ok))
            self.assertEqual(push.call_count, int(method == "both"))
            self.assertEqual(persist.call_count, int(bool(push_ok)))
            prefix = "[推送渠道] "
            records = [json.loads(line[len(prefix):]) for line in out.getvalue().splitlines() if line.startswith(prefix)]
            self.assertEqual(len(records), 1)
            rec = records[0]
            self.assertIs(rec["email_sent"], False)
            self.assertIs(rec["pushplus_sent"], push_ok)
            self.assertIs(rec["any_sent"], sent)
            self.assertNotIn("aborted", rec)
            for mock in (server.sendmail, ssl, smtp, sock):
                mock.assert_not_called()

    def test_g04_subscription_failure_system_alert_and_feedback_author_survive_bad_smtp_port(self):
        sub = {"id": "test-r48", "origin": "AAA", "destination": "BBB",
               "notification_goals": {"method": "both", "email": TO}}
        with (offline_smtp(SMTP_PORT="465,587", FEEDBACK_NOTIFY_EMAIL=TO) as (server, ssl, smtp, sock, out),
              patch.object(main, "send", return_value=True) as push):
            self.assertTrue(main._notify_subscription_failure(sub, reason="offline"))
            push.assert_called_once()
            self.assertNotIn("last_failure", sub)
            push.reset_mock()
            self.assertTrue(main._notify_system_alert([sub], "Test", "Test"))
            push.assert_called_once()
            self.assertFalse(self.boolean_call(web_form.notify_feedback_author, {"feedback_type": "test"}))
        self.assertEqual(out.getvalue().count("[邮件] SMTP配置无效"), 3)
        self.assertIn("send_email返回False", out.getvalue())
        self.assertNotIn("email_error", out.getvalue())
        for mock in (server.sendmail, ssl, smtp, sock):
            mock.assert_not_called()


class TestTrack3HealthySmtpAndBoundaryRegression(MailContract):
    def test_g05_valid_ssl_and_starttls_succeed_while_port_65536_fails_closed(self):
        for port, use_ssl in (("465", True), ("587", False)):
            with offline_smtp(SMTP_PORT=port, SMTP_SSL=str(use_ssl)) as (server, ssl, smtp, sock, _):
                self.assertTrue(self.send())
                (ssl if use_ssl else smtp).assert_called_once_with(HOST, int(port), timeout=30)
                (smtp if use_ssl else ssl).assert_not_called()
                self.assertEqual(server.starttls.call_count, int(not use_ssl))
                server.login.assert_called_once_with(SENDER, PASS)
                server.sendmail.assert_called_once()
                self.assertEqual(server.sendmail.call_args.args[:2], (SENDER, [TO]))
                server.quit.assert_called_once_with()
                sock.assert_not_called()
        with offline_smtp(SMTP_PORT="65536") as (server, ssl, smtp, sock, _):
            self.assertFalse(self.send())
            for mock in (server.sendmail, ssl, smtp, sock):
                mock.assert_not_called()

    def test_g06_padded_port_and_r47_privacy_preserved_while_blank_host_fails_closed(self):
        rows = [{"date": str(i), "price": price} for i, price in enumerate((9800, 10100, 10431))]
        png = email_notifier.build_trend_png(rows)
        self.assertTrue(isinstance(png, bytes) and png.startswith(b"\x89PNG\r\n\x1a\n"))
        with offline_smtp(SMTP_PORT=" 465 ") as (server, ssl, _, _, _out):
            self.assertTrue(self.send(html=CID_HTML, images={"trendchart": png}, privacy_level="redacted"))
            ssl.assert_called_once_with(HOST, 465, timeout=30)
            message = email.message_from_string(server.sendmail.call_args.args[2])
            self.assertFalse(any(p.get_content_maintype() == "image" or p["Content-ID"] for p in message.walk()))
        with offline_smtp(SMTP_HOST="   ") as (server, ssl, smtp, sock, _out):
            self.assertFalse(self.send())
            for mock in (server.sendmail, ssl, smtp, sock):
                mock.assert_not_called()


class TestTrack4LockedAstAndFrozenCarrierGuards(unittest.TestCase):
    def test_g07_ast_locks_and_existing_channel_exception_contracts_intact(self):
        source = inspect.getsource(email_notifier.build_trend_png).encode("utf-8")
        self.assertEqual(hashlib.sha256(source).hexdigest(),
                         "21f2ba56d736dc088f9df3d8457332cb542c3af9d952e918afb385342a9eae34")
        for case in (
            test_email_no_live_api_sink.EmailNoLiveApiSinkContractTest("test_scope_comment_and_first_business_statement_are_exact"),
            test_delivery_send_exception_state.SendExceptionStateTest("test_exception_state_matrix"),
            test_email_render_by_channel.EmailRenderByChannelTest("test_email_render_failure_no_longer_blocks_other_methods"),
        ):
            result = case.run()
            self.assertTrue(result.wasSuccessful(), (result.failures, result.errors))

    def test_g08_three_frozen_carriers(self):
        payload = json.loads(FIXTURE.read_text(encoding="utf-8"))
        with (TemporaryDirectory() as tmp, redirect_stdout(io.StringIO()),
              patch.object(config_loader, "RUNTIME_CONFIG_PATH", Path(tmp) / "absent.yaml")):
            bodies = (notifier.render_email(copy.deepcopy(payload))[1],
                      notifier.prepare_push_render(notifier.render_pushplus_sections(copy.deepcopy(payload))).content,
                      notifier.render_detail_html(copy.deepcopy(payload)))
        specs = [(53307, "1dd353d2b7bcafc02087957ccccb6661478e20472a22fb3e15dae6a56473ee6b"),
                 (5552, "2ac0e80d9f82dbe7ebccf524c0832f09d42d916212b6178316b0b7bcd60574ff"),
                 (54248, "e31b8728b8d0bc05c9bbd4bd99c299aaf6dca7b66674b6c560ec089b1681ffa6")]
        self.assertEqual([(len(b.encode("utf-8")), hashlib.sha256(b.encode("utf-8")).hexdigest())
                          for b in bodies], specs)
