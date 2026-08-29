from __future__ import annotations

import ast
import contextlib
from copy import deepcopy
import hashlib
import io
import json
from pathlib import Path
import socket
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

import log_utils
import notifier


CANARY = "TEST_ONLY_LOG_CANARY_7f4e1b9d"
SUBSCRIPTION_SUMMARY_KEYS = {
    "subscription_id",
    "origin",
    "destination",
    "depart_date",
    "return_date",
    "route_type",
    "passenger_count",
    "notification_method",
}
FLIGHT_SUMMARY_KEYS = {
    "flight_combo",
    "source",
    "price",
    "origin",
    "destination",
    "departure_time",
    "arrival_time",
    "stops",
    "missing_fields",
}


class _LeakyObject:
    def __str__(self):
        return CANARY

    def __repr__(self):
        return f"_LeakyObject(secret={CANARY!r})"


def _assert_canary_absent(testcase: unittest.TestCase, value) -> None:
    text = value if isinstance(value, str) else repr(value)
    if CANARY in text:
        testcase.fail("test-only canary was exposed")


class StructuredLogRedactionTest(unittest.TestCase):
    def test_nested_keys_are_normalized_without_masking_route_or_request_keys(self):
        original = {
            "nested": {
                "token": CANARY,
                "apiKey": CANARY,
                "api-key": CANARY,
                "pushplus_token": CANARY,
                "refreshToken": CANARY,
                "Authorization": CANARY,
                "email": "owner@example.test",
                "contactEmail": "owner@example.test",
                "phone": "13800138000",
                "mobileNumber": "13900139000",
                "route_key": "PVG|KIX|2099-10-01",
                "request_key": "request-17",
                "price": 4321,
                "flight_no": "MU225",
                "depart_date": "2099-10-01",
                "tuple_value": ("kept", 7),
            }
        }
        before = deepcopy(original)

        redacted = log_utils.redact_value(original)

        self.assertTrue(original == before, "input object was mutated")
        nested = redacted["nested"]
        for key in (
            "token",
            "apiKey",
            "api-key",
            "pushplus_token",
            "refreshToken",
            "Authorization",
        ):
            if nested[key] != "***":
                self.fail(f"{key} was not redacted")
        self.assertEqual(nested["email"], "<EMAIL>")
        self.assertEqual(nested["contactEmail"], "<EMAIL>")
        self.assertEqual(nested["phone"], "<PHONE>")
        self.assertEqual(nested["mobileNumber"], "<PHONE>")
        self.assertEqual(nested["route_key"], original["nested"]["route_key"])
        self.assertEqual(nested["request_key"], original["nested"]["request_key"])
        self.assertEqual(nested["price"], 4321)
        self.assertEqual(nested["flight_no"], "MU225")
        self.assertEqual(nested["depart_date"], "2099-10-01")
        self.assertEqual(nested["tuple_value"], ("kept", 7))
        self.assertEqual(log_utils.redact_value(redacted), redacted)

    def test_limited_text_fallback_redacts_supported_secret_forms(self):
        text = (
            f"https://example.test/path?token={CANARY}&api_key={CANARY}"
            f"&access_token={CANARY} Authorization: Bearer {CANARY} "
            f'{{"token":"{CANARY}"}} '
            f"{{'token': '{CANARY}'}} owner@example.test "
            "route-key=PVG-KIX request-key=request-17"
        )

        redacted = log_utils.redact_text(text)

        _assert_canary_absent(self, redacted)
        self.assertIn("token=***", redacted)
        self.assertIn("api_key=***", redacted)
        self.assertIn("access_token=***", redacted)
        self.assertIn("Authorization: Bearer ***", redacted)
        self.assertIn("<EMAIL>", redacted)
        self.assertIn("route-key=PVG-KIX", redacted)
        self.assertIn("request-key=request-17", redacted)

    def test_unknown_objects_cycles_and_depth_never_use_object_repr(self):
        unknown = log_utils.redact_value(_LeakyObject())
        _assert_canary_absent(self, unknown)
        self.assertEqual(unknown, "<OBJECT:_LeakyObject>")
        stdout = io.StringIO()
        with contextlib.redirect_stdout(stdout):
            log_utils.safe_log(_LeakyObject())
        self.assertEqual(stdout.getvalue().strip(), "<OBJECT:_LeakyObject>")
        _assert_canary_absent(self, stdout.getvalue())

        cycle = {}
        cycle["self"] = cycle
        self.assertEqual(log_utils.redact_value(cycle)["self"], "<CYCLE>")

        nested = {"value": CANARY}
        for _ in range(20):
            nested = {"next": nested}
        rendered = repr(log_utils.redact_value(nested, max_depth=5))
        self.assertIn("<MAX_DEPTH>", rendered)
        _assert_canary_absent(self, rendered)

        exception_value = log_utils.render_redacted_json(
            {"exception": RuntimeError(CANARY)}
        )
        self.assertEqual(exception_value, '{"exception":"<OBJECT:RuntimeError>"}')
        _assert_canary_absent(self, exception_value)

    def test_json_rendering_is_deterministic_and_uses_safe_metadata_when_large(self):
        payload = {"z": 1, "a": {"token": CANARY}, "flight_no": "MU225"}
        rendered = log_utils.render_redacted_json(payload)
        self.assertEqual(rendered, '{"a":{"token":"***"},"flight_no":"MU225","z":1}')
        _assert_canary_absent(self, rendered)

        redacted_full = log_utils.render_redacted_json(payload, max_chars=10_000)
        oversized = log_utils.render_redacted_json(payload, max_chars=10)
        metadata = json.loads(oversized)
        self.assertEqual(
            set(metadata),
            {"truncated", "chars", "redacted_sha256"},
        )
        self.assertTrue(metadata["truncated"])
        self.assertEqual(metadata["chars"], len(redacted_full))
        self.assertEqual(
            metadata["redacted_sha256"],
            hashlib.sha256(redacted_full.encode("utf-8")).hexdigest(),
        )
        _assert_canary_absent(self, oversized)

    def test_safe_log_json_emits_one_redacted_line_through_safe_log(self):
        with patch.object(log_utils, "safe_log") as safe_log_mock:
            log_utils.safe_log_json("[测试] ", {"token": CANARY, "price": 1234})

        self.assertEqual(safe_log_mock.call_count, 1)
        _assert_canary_absent(self, safe_log_mock.call_args_list)
        self.assertEqual(
            safe_log_mock.call_args.args,
            ('[测试] {"price":1234,"token":"***"}',),
        )

    def test_canary_is_absent_from_stdio_run_log_and_round_archive(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            run_log = root / "run_latest.log"
            round_root = root / "rounds"
            script = f"""
from datetime import datetime
from log_utils import configure_run_logging, end_round_log_archive, safe_log_json, start_round_log_archive

class Leaky:
    def __str__(self):
        return {CANARY!r}
    def __repr__(self):
        return {CANARY!r}

configure_run_logging({str(run_log)!r})
start_round_log_archive('privacy-test', root_dir={str(round_root)!r}, now=datetime(2099, 1, 2, 3, 4, 5))
safe_log_json('[隐私测试] ', {{'token': {CANARY!r}, 'unknown': Leaky()}})
end_round_log_archive(status='ok')
"""
            completed = subprocess.run(
                [sys.executable, "-X", "utf8", "-c", script],
                cwd=Path(__file__).resolve().parent,
                capture_output=True,
                text=True,
                encoding="utf-8",
                check=False,
                env={"NO_LIVE_API": "1", "PATH": str(Path(sys.executable).parent)},
            )

            _assert_canary_absent(self, completed.stdout)
            _assert_canary_absent(self, completed.stderr)
            self.assertEqual(completed.returncode, 0, "privacy log subprocess failed")
            round_log = round_root / "20990102.log"
            combined = "\n".join(
                (
                    completed.stdout,
                    completed.stderr,
                    run_log.read_text(encoding="utf-8"),
                    round_log.read_text(encoding="utf-8"),
                )
            )
            _assert_canary_absent(self, combined)
            self.assertIn('"token":"***"', combined)
            self.assertIn("<OBJECT:Leaky>", combined)


class NotifierLogSummaryTest(unittest.TestCase):
    def test_subscription_summary_uses_exact_whitelist_and_masks_identity(self):
        subscription = {
            "subscription_id": "123e4567-e89b-12d3-a456-426614174000",
            "origin": "PVG",
            "destination": "KIX",
            "depart_date": "2099-10-01",
            "return_date": "2099-10-06",
            "route_type": "international",
            "preferences": {
                "passengers": {"adult": 2, "child": 1, "elderly": 2, "infant": 0},
            },
            "notification_goals": {
                "method": "both",
                "email": "owner@example.test",
                "pushplus_token": CANARY,
            },
            "soft_preferences": {"travel_scenarios": ["tourism", "family"]},
            "raw": {"token": CANARY},
        }
        before = deepcopy(subscription)

        summary = notifier._subscription_log_summary(subscription)

        self.assertTrue(subscription == before, "subscription input was mutated")
        _assert_canary_absent(self, summary)
        self.assertEqual(set(summary), SUBSCRIPTION_SUMMARY_KEYS)
        self.assertEqual(summary["subscription_id"], "123e4567********")
        self.assertEqual(summary["passenger_count"], 5)
        self.assertEqual(summary["notification_method"], "both")
        self.assertNotIn("owner@example.test", repr(summary))
        _assert_canary_absent(self, summary)
        self.assertNotIn("travel_scenarios", summary)

    def test_subscription_summary_non_dict_never_repr_input(self):
        summary = notifier._subscription_log_summary(_LeakyObject())
        self.assertEqual(
            summary,
            {"summary_unavailable": True, "input_type": "_LeakyObject"},
        )
        _assert_canary_absent(self, summary)

    def test_flight_summary_uses_exact_whitelist_and_excludes_raw_links(self):
        flight = {
            "flight_no": "MU225",
            "flight_combo": "MU225",
            "source": "juhe",
            "price": 4321,
            "departure_airport": "PVG",
            "arrival_airport": "KIX",
            "departure_time": "2099-10-01 09:00",
            "arrival_time": "2099-10-01 12:00",
            "stops": 0,
            "segments": [{"booking_url": f"https://example.test/?token={CANARY}"}],
            "raw": {"token": CANARY},
            "booking_options": [{"url": f"https://example.test/?token={CANARY}"}],
            "links": {"buy": f"https://example.test/?token={CANARY}"},
        }
        before = deepcopy(flight)

        summary = notifier._flight_log_summary(flight, missing_fields=["aircraft"])

        self.assertTrue(flight == before, "flight input was mutated")
        _assert_canary_absent(self, summary)
        self.assertEqual(set(summary), FLIGHT_SUMMARY_KEYS)
        self.assertEqual(summary["flight_combo"], "MU225")
        self.assertEqual(summary["source"], "juhe")
        self.assertEqual(summary["price"], 4321)
        self.assertEqual(summary["origin"], "PVG")
        self.assertEqual(summary["destination"], "KIX")
        self.assertEqual(summary["missing_fields"], ["aircraft"])
        serialized = repr(summary)
        for forbidden in ("raw", "booking_options", "links", "booking_url", CANARY):
            self.assertNotIn(forbidden, serialized)

    def test_flight_summary_non_dict_never_repr_input(self):
        summary = notifier._flight_log_summary(_LeakyObject())
        self.assertEqual(
            summary,
            {"summary_unavailable": True, "input_type": "_LeakyObject"},
        )
        _assert_canary_absent(self, summary)

    def test_flight_debug_trigger_keeps_prefix_and_count(self):
        flight = {
            "flight_no": "CA123",
            "flight_combo": "CA123",
            "price": 2000,
            "raw": {"token": CANARY},
            "booking_options": [{"url": f"https://example.test/?token={CANARY}"}],
        }
        with patch.object(notifier, "safe_log_json") as log_json, contextlib.redirect_stdout(io.StringIO()):
            notifier._email_plan_leg_group("去程", flight)

        self.assertEqual(log_json.call_count, 1)
        _assert_canary_absent(self, log_json.call_args_list)
        label, summary = log_json.call_args.args
        self.assertEqual(label, "[航班调试] 完整字段: ")
        self.assertEqual(set(summary), FLIGHT_SUMMARY_KEYS)
        _assert_canary_absent(self, summary)

    def test_summary_and_json_logging_make_no_network_or_delivery_calls(self):
        socket_attempts = []

        def deny_socket(*args, **kwargs):
            socket_attempts.append((args, kwargs))
            raise AssertionError("network access is forbidden in this contract")

        with (
            patch.object(socket.socket, "connect", side_effect=deny_socket),
            patch.object(notifier.httpx, "post") as http_post,
            patch("email_notifier.send_email") as smtp_send,
            patch.object(log_utils, "safe_log") as safe_log_mock,
        ):
            subscription_summary = notifier._subscription_log_summary(
                {"notification_goals": {"method": "both", "token": CANARY}}
            )
            flight_summary = notifier._flight_log_summary(
                {"flight_combo": "MU225", "raw": {"token": CANARY}}
            )
            log_utils.safe_log_json("[privacy] ", subscription_summary)
            log_utils.safe_log_json("[privacy] ", flight_summary)

        self.assertEqual(socket_attempts, [])
        http_post.assert_not_called()
        smtp_send.assert_not_called()
        self.assertEqual(safe_log_mock.call_count, 2)
        _assert_canary_absent(self, safe_log_mock.call_args_list)


class NotifierLogSinkStaticContractTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.path = Path(notifier.__file__).resolve()
        cls.source = cls.path.read_text(encoding="utf-8")
        cls.tree = ast.parse(cls.source)

    def test_full_domain_objects_are_not_interpolated_or_serialized_to_logs(self):
        domain_names = {"subscription", "flight", "payload", "plan"}
        violations = []
        for node in ast.walk(self.tree):
            if not isinstance(node, ast.Call):
                continue
            function_name = ""
            if isinstance(node.func, ast.Name):
                function_name = node.func.id
            elif isinstance(node.func, ast.Attribute) and isinstance(node.func.value, ast.Name):
                function_name = f"{node.func.value.id}.{node.func.attr}"
            if function_name not in {"print", "safe_log"}:
                continue
            for child in ast.walk(node):
                if isinstance(child, ast.FormattedValue) and isinstance(child.value, ast.Name):
                    if child.value.id in domain_names:
                        violations.append((node.lineno, f"f-string:{child.value.id}"))
                if not isinstance(child, ast.Call):
                    continue
                called = ""
                if isinstance(child.func, ast.Name):
                    called = child.func.id
                elif isinstance(child.func, ast.Attribute) and isinstance(child.func.value, ast.Name):
                    called = f"{child.func.value.id}.{child.func.attr}"
                if called not in {"repr", "str", "json.dumps"} or not child.args:
                    continue
                referenced = {
                    name.id
                    for name in ast.walk(child.args[0])
                    if isinstance(name, ast.Name)
                }
                for name in sorted(referenced & domain_names):
                    violations.append((node.lineno, f"{called}:{name}"))
        self.assertEqual(violations, [])

    def test_known_full_object_sinks_use_whitelist_summaries_once(self):
        calls = []
        for node in ast.walk(self.tree):
            if not isinstance(node, ast.Call) or not isinstance(node.func, ast.Name):
                continue
            if node.func.id != "safe_log_json" or len(node.args) < 2:
                continue
            label = node.args[0].value if isinstance(node.args[0], ast.Constant) else None
            calls.append((node, label))

        self.assertEqual(len(calls), 2)
        subscription_calls = [item for item in calls if item[1] == "[人数定位] 完整订阅: "]
        flight_calls = [item for item in calls if item[1] == "[航班调试] 完整字段: "]
        self.assertEqual(len(subscription_calls), 1)
        self.assertEqual(len(flight_calls), 1)
        for node, expected_helper in (
            (subscription_calls[0][0], "_subscription_log_summary"),
            (flight_calls[0][0], "_flight_log_summary"),
        ):
            summary_call = node.args[1]
            self.assertIsInstance(summary_call, ast.Call)
            self.assertIsInstance(summary_call.func, ast.Name)
            self.assertEqual(summary_call.func.id, expected_helper)


if __name__ == "__main__":
    unittest.main()
