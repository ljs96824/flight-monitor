import copy
import email
import hashlib
import inspect
import io
import json
import os
import tempfile
import unittest
from contextlib import contextmanager, redirect_stdout
from email.header import decode_header, make_header
from pathlib import Path
from unittest.mock import Mock, patch

import config_loader
import email_notifier
import main
import notifier
import plan_tracker
import storage
import test_email_no_live_api_sink


FIXTURE = Path(__file__).parent / "fixtures" / "frozen_email" / "economy_payload.json"
PNG = b"\x89PNG\r\n\x1a\n"
RECIPIENT = "recipient@example.invalid"
PASSENGERS = "2成人+1儿童+2老人"


def history():
    return [{"date": f"2026-11-{i:02}", "price": price}
            for i, price in enumerate((9800, 10100, 10431, 9950, 10200), 1)]


def payload(**fields):
    values = json.loads(FIXTURE.read_text(encoding="utf-8"))
    values.update(price_history=history(), ideal_price=9500, max_price=11000,
                  current_price=10431, display_price=10431, passenger_summary=PASSENGERS,
                  constraint_change={}, detail_url="https://example.invalid/detail?sub=sample")
    values.update(fields)
    return values


@contextmanager
def mailbox(*, real_config=False):
    server = Mock()
    env = {"NO_LIVE_API": "0", "SMTP_USER": "sender@example.invalid", "SMTP_PASS": "offline",
           "SMTP_HOST": "smtp.example.invalid", "SMTP_PORT": "465", "SMTP_SSL": "true"}
    with (patch.object(email_notifier.smtplib, "SMTP_SSL", return_value=server),
          patch.dict(os.environ, env)):
        if real_config:
            yield server
        else:
            with patch.object(email_notifier, "_smtp_config", return_value={
                "host": "smtp.example.invalid", "port": 465, "ssl": True, "provider": "qq",
            }):
                yield server


def assert_carrier(case, server, expected_image=None):
    server.sendmail.assert_called_once()
    raw = server.sendmail.call_args.args[2]
    parsed = email.message_from_string(raw)
    parts = list(parsed.walk())
    images = [part for part in parts if part.get_content_maintype() == "image"]
    if expected_image is None:
        case.assertEqual(len(images), 0)
        case.assertFalse(any("trendchart" in str(part.get("Content-ID", "")) for part in parts))
        case.assertNotIn("iVBORw0KGgo", raw)
    else:
        case.assertEqual(len(images), 1)
        case.assertEqual(images[0].get_content_type(), "image/png")
        case.assertEqual(images[0]["Content-ID"], "<trendchart>")
        image = images[0].get_payload(decode=True)
        case.assertTrue(image.startswith(PNG))
        if isinstance(expected_image, bytes):
            case.assertEqual(image, expected_image)
    html_parts = [part for part in parts if part.get_content_type() == "text/html"]
    case.assertEqual(len(html_parts), 1)
    html = html_parts[0].get_payload(decode=True).decode("utf-8")
    subject = str(make_header(decode_header(parsed["Subject"])))
    if expected_image is None:
        for value in ("10431", "10,431", PASSENGERS):
            case.assertNotIn(value, html)
            case.assertNotIn(value, subject)
    return html


class TestTrack1RenderEmailPrivacyGate(unittest.TestCase):
    def test_g01_privacy_precedes_png_generation(self):
        for level in ("redacted", "minimal"):
            for fields in ({"notification_privacy_level": level}, {"privacy_level": level},
                           {"notification_goals": {"privacy_level": level}},
                           {"route_info": {"notification_goals": {"privacy_level": level}}}):
                p = payload(**fields)
                with patch.object(email_notifier, "build_trend_png", wraps=email_notifier.build_trend_png) as build:
                    _, html, images = email_notifier.render_email(p)
                self.assertFalse(images, (level, fields.keys()))
                build.assert_not_called()
                for value in ("10,431", "10431", "cid:trendchart"):
                    self.assertNotIn(value, html)
        for fields in ({}, {"privacy_level": "full"}):
            with patch.object(email_notifier, "build_trend_png", wraps=email_notifier.build_trend_png) as build:
                _, html, images = email_notifier.render_email(payload(**fields))
            build.assert_called_once()
            self.assertIn("cid:trendchart", html)
            self.assertTrue(images["trendchart"].startswith(PNG))

    def test_g02_full_mail_without_chart_reference_skips_renderer(self):
        p = payload(privacy_level="full", push_type="数据不完整")
        with patch.object(email_notifier, "build_trend_png", wraps=email_notifier.build_trend_png) as build:
            _, html, images = email_notifier.render_email(p)
        self.assertNotIn("cid:trendchart", html)
        build.assert_not_called()
        self.assertEqual(images, {})


class TestTrack2SendEmailFinalMimeCarrierContract(unittest.TestCase):
    def test_g03_private_html_rejects_real_png_at_smtp_boundary(self):
        png = email_notifier.build_trend_png(history(), 9500, 11000, 10431)
        self.assertTrue(isinstance(png, bytes) and png.startswith(PNG))
        for level in ("redacted", "minimal"):
            for kind in ("价格提醒", "数据不完整"):
                subject, html = notifier.render_email(payload(privacy_level=level, push_type=kind))
                with mailbox() as server:
                    self.assertTrue(email_notifier.send_email(RECIPIENT, subject, html, {"trendchart": png}))
                self.assertEqual(assert_carrier(self, server), html)
                # A stray CID reference cannot override the private body marker.
                with mailbox() as server:
                    self.assertTrue(email_notifier.send_email(
                        RECIPIENT, subject, html + '<img src="cid:trendchart">', {"trendchart": png}))
                assert_carrier(self, server)

    def test_g04_level_and_html_reference_both_control_mime_parts(self):
        self.assertIn("privacy_level", inspect.signature(email_notifier.send_email).parameters)
        png = email_notifier.build_trend_png(history(), 9500, 11000, 10431)
        self.assertTrue(isinstance(png, bytes) and png.startswith(PNG))
        for level, referenced in (("redacted", True), ("minimal", True), ("full", True), ("full", False)):
            html = '<img src="cid:trendchart">' if referenced else "<p>Update</p>"
            with mailbox() as server:
                self.assertTrue(email_notifier.send_email(
                    RECIPIENT, "Update", html, {"trendchart": png, "unused": png}, privacy_level=level))
            assert_carrier(self, server, png if level == "full" and referenced else None)


class TestTrack3EndToEndDeliveryMimePrivacy(unittest.TestCase):
    def test_g05_real_subscription_to_email_and_both(self):
        flight = {"flight_combo": "NH970", "price": 10431, "cabin_class": "economy",
                  "stops": 0, "departure_time": "09:00", "arrival_time": "12:00"}
        request = {"analysis_result": {"all_flights": [flight], "economy_recommendations": [flight]},
                   "route_info": {"origin": "SHA", "destination": "KIX", "depart_date": "2026-12-12",
                                  "lowest_price_history": history()}}
        with (tempfile.TemporaryDirectory() as tmp,
              patch.object(storage, "DB_PATH", Path(tmp) / "prices.db"),
              patch.object(plan_tracker, "DEFAULT_DATA_DIR", Path(tmp)),
              patch.object(config_loader, "RUNTIME_CONFIG_PATH", Path(tmp) / "absent.yaml")):
            for method in ("email", "both"):
                for level in ("redacted", "minimal", "full"):
                    sub = {"id": "00000000-0000-4000-8000-000000000047", "origin": "SHA", "destination": "KIX",
                           "notification_goals": {"method": method, "privacy_level": level, "email": RECIPIENT}}
                    with (mailbox(real_config=True) as server, patch.object(main, "send", return_value=True) as push,
                          patch.object(main, "_save_result_for_page", return_value={"target_reached": True}),
                          patch.object(main, "persist_notification_payload"), redirect_stdout(io.StringIO()) as log):
                        sent = main._deliver_notification(sub, "SHA-KIX", copy.deepcopy(request))
                    self.assertTrue(sent, log.getvalue())
                    self.assertEqual(push.call_count, int(method == "both"))
                    body = assert_carrier(self, server, True if level == "full" else None)
                    self.assertIn({"redacted": "航班监控·已脱敏", "minimal": "航班监控有变动",
                                   "full": "cid:trendchart"}[level], body)

    def test_g06_detail_token_and_image_filter_do_not_mutate_inputs(self):
        png = email_notifier.build_trend_png(history(), 9500, 11000, 10431)
        self.assertTrue(isinstance(png, bytes) and png.startswith(PNG))
        for level in ("redacted", "minimal"):
            p = payload(notification_privacy_level=level)
            original = copy.deepcopy(p)
            wrapped = main.delivery_payload_with_detail_token(p, token="offline-test")
            self.assertIn("token=offline-test", wrapped["detail_url"])
            subject, html, images = email_notifier.render_email(wrapped)
            self.assertFalse(images)
            supplied = {"trendchart": png}
            before = supplied.copy()
            with mailbox() as server:
                self.assertTrue(email_notifier.send_email(RECIPIENT, subject, html, supplied))
            assert_carrier(self, server)
            self.assertEqual(supplied, before)
            self.assertEqual(p, original)


class TestTrack4LockedAstAndFrozenCarrierGuards(unittest.TestCase):
    def test_g07_chart_renderer_and_no_live_api_boundary(self):
        source = inspect.getsource(email_notifier.build_trend_png).encode("utf-8")
        self.assertEqual(hashlib.sha256(source).hexdigest(),
                         "21f2ba56d736dc088f9df3d8457332cb542c3af9d952e918afb385342a9eae34")
        result = test_email_no_live_api_sink.EmailNoLiveApiSinkContractTest(
            "test_scope_comment_and_first_business_statement_are_exact").run()
        self.assertTrue(result.wasSuccessful(), (result.errors, result.failures))

    def test_g08_three_frozen_carriers(self):
        p = json.loads(FIXTURE.read_text(encoding="utf-8"))
        with (tempfile.TemporaryDirectory() as tmp, redirect_stdout(io.StringIO()),
              patch.object(config_loader, "RUNTIME_CONFIG_PATH", Path(tmp) / "absent.yaml")):
            outputs = (notifier.render_email(copy.deepcopy(p))[1],
                       notifier.prepare_push_render(notifier.render_pushplus_sections(copy.deepcopy(p))).content,
                       notifier.render_detail_html(copy.deepcopy(p)))
        self.assertEqual([(len(v.encode("utf-8")), hashlib.sha256(v.encode("utf-8")).hexdigest()) for v in outputs], [
            (53307, "1dd353d2b7bcafc02087957ccccb6661478e20472a22fb3e15dae6a56473ee6b"),
            (5552, "2ac0e80d9f82dbe7ebccf524c0832f09d42d916212b6178316b0b7bcd60574ff"),
            (54248, "e31b8728b8d0bc05c9bbd4bd99c299aaf6dca7b66674b6c560ec089b1681ffa6"),
        ])
