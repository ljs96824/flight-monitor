from __future__ import annotations

import hashlib
import importlib.util
import inspect
import io
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from contextlib import contextmanager, redirect_stderr, redirect_stdout
from datetime import timedelta
from unittest.mock import patch


ROOT = Path(__file__).resolve().parent
LEGACY_MODULES = (
    "debug_api",
    "debug_skyscanner",
    "debug_sources",
    "debug_travelpayouts",
)
AUDIT_MODULES = (
    "scripts.serpapi_capability_audit",
    "scripts.cabin_capability_audit",
)
TEST_CANARY_SECRET = "TEST_ONLY_CANARY_SECRET_7f3c91d2"


class _FakeResponse:
    status_code = 200

    def __init__(self, payload):
        self._payload = payload

    def json(self):
        return self._payload

    def raise_for_status(self):
        return None


class _FakeGate:
    def __init__(self, *, acquired=True, holder=None):
        self.acquired = acquired
        self.holder = dict(holder or {})
        self.released = False

    def release(self):
        self.released = True


def _acquire_ok(*_args, **_kwargs):
    return _FakeGate()


@contextmanager
def _guarded_audit(config, *, acquire=_acquire_ok):
    with patch(
        "scripts.manual_live_guard.config_loader.load_merged_config",
        return_value=config,
    ), patch(
        "scripts.manual_live_guard._acquire_singleflight",
        side_effect=acquire,
    ):
        yield


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _serp_payload(params) -> dict:
    cabin = "Business" if int(params["travel_class"]) == 3 else "Economy"
    return {
        "search_parameters": {"currency": "CNY"},
        "best_flights": [
            {
                "price": 1234,
                "flights": [
                    {
                        "airline": "Fixture Air",
                        "flight_number": "ZZ 101",
                        "travel_class": cabin,
                    }
                ],
            }
        ],
        "other_flights": [],
    }


def _juhe_payload() -> dict:
    return {
        "error_code": 0,
        "result": {"flightInfo": [{"flightNo": "ZZ101", "ticketPrice": 999}]},
    }


def _duffel_payload() -> dict:
    return {"data": {"live_mode": True, "offers": []}}


def _safe_absence(testcase: unittest.TestCase, secret: str, **values) -> None:
    for label, value in values.items():
        rendered = value if isinstance(value, str) else repr(value)
        if secret in rendered:
            testcase.fail(f"test canary secret leaked into {label}")


class LegacyManualLiveRetirementContractTest(unittest.TestCase):
    def test_legacy_paths_are_absent_and_modules_are_not_importable(self):
        for module_name in LEGACY_MODULES:
            with self.subTest(module=module_name):
                self.assertFalse((ROOT / f"{module_name}.py").exists())
                self.assertIsNone(importlib.util.find_spec(module_name))

    def test_legacy_paths_have_no_executable_reference(self):
        names = tuple(f"{name}.py" for name in LEGACY_MODULES)
        scanned_suffixes = {".py", ".ps1", ".bat", ".cmd", ".yml", ".yaml"}
        offenders = []
        for path in ROOT.rglob("*"):
            if not path.is_file() or path.suffix.lower() not in scanned_suffixes:
                continue
            if path == Path(__file__).resolve() or ".git" in path.parts:
                continue
            text = path.read_text(encoding="utf-8", errors="replace")
            if any(name in text for name in (*names, *LEGACY_MODULES)):
                offenders.append(path.relative_to(ROOT).as_posix())
        self.assertEqual(offenders, [])


class ManualLiveOfflineBoundaryTest(unittest.TestCase):
    def _subprocess_env(self, hook_dir: Path) -> dict[str, str]:
        env = dict(os.environ)
        for name in (
            "SERPAPI_KEY",
            "SERPAPI_API_KEY",
            "SERP_API_KEY",
            "JUHE_FLIGHT_KEY",
            "DUFFEL_TOKEN",
        ):
            env.pop(name, None)
        env["NO_LIVE_API"] = "1"
        env["PYTHONIOENCODING"] = "utf-8"
        env["PYTHONPATH"] = os.pathsep.join((str(hook_dir), str(ROOT)))
        return env

    def _write_socket_denial(self, directory: Path) -> None:
        (directory / "sitecustomize.py").write_text(
            """
import socket

def _network_denied(*_args, **_kwargs):
    raise AssertionError("SOCKET_NETWORK_ATTEMPTED")

socket.create_connection = _network_denied
socket.socket.connect = _network_denied
""".lstrip(),
            encoding="utf-8",
        )

    def test_imports_are_network_silent_under_socket_denial(self):
        with tempfile.TemporaryDirectory() as tmp:
            work = Path(tmp)
            self._write_socket_denial(work)
            command = "import " + ", ".join(AUDIT_MODULES) + "; print('IMPORT_OK')"
            completed = subprocess.run(
                [sys.executable, "-X", "utf8", "-c", command],
                cwd=work,
                env=self._subprocess_env(work),
                text=True,
                encoding="utf-8",
                errors="replace",
                capture_output=True,
                check=False,
            )
        self.assertEqual(completed.returncode, 0, completed.stderr)
        self.assertIn("IMPORT_OK", completed.stdout)
        self.assertNotIn("SOCKET_NETWORK_ATTEMPTED", completed.stderr)

    def test_default_cli_is_network_silent_under_socket_denial(self):
        with tempfile.TemporaryDirectory() as tmp:
            work = Path(tmp)
            self._write_socket_denial(work)
            for relative in (
                "scripts/serpapi_capability_audit.py",
                "scripts/cabin_capability_audit.py",
            ):
                with self.subTest(script=relative):
                    completed = subprocess.run(
                        [
                            sys.executable,
                            "-X",
                            "utf8",
                            str(ROOT / relative),
                            "--usage-path",
                            str(work / "missing-usage.json"),
                        ],
                        cwd=work,
                        env=self._subprocess_env(work),
                        text=True,
                        encoding="utf-8",
                        errors="replace",
                        capture_output=True,
                        check=False,
                    )
                    self.assertEqual(completed.returncode, 0, completed.stderr)
                    self.assertNotIn("SOCKET_NETWORK_ATTEMPTED", completed.stderr)


class ManualLiveGuardContractTest(unittest.TestCase):
    def _initialized_usage(self, directory: Path) -> Path:
        from api_usage import initialize_usage_ledger

        path = directory / "api_usage.json"
        initialize_usage_ledger(path)
        return path

    @staticmethod
    def _quota_config() -> dict:
        return {
            "source_quota_budget": {
                "serpapi": {"monthly": 250, "reserve": 30},
                "juhe": 100,
            }
        }

    def test_public_audit_entrypoints_cannot_override_guard_dependencies(self):
        from scripts import cabin_capability_audit, serpapi_capability_audit
        from scripts.manual_live_guard import prepare_manual_live_execution

        forbidden = {"config", "singleflight_acquire", "lock_path"}
        for callable_obj in (
            serpapi_capability_audit.run_audit,
            cabin_capability_audit.run_audit,
            prepare_manual_live_execution,
        ):
            with self.subTest(callable=callable_obj.__qualname__):
                parameters = set(inspect.signature(callable_obj).parameters)
                self.assertEqual(parameters & forbidden, set())
        for module in (serpapi_capability_audit, cabin_capability_audit):
            source = Path(module.__file__).read_text(encoding="utf-8")
            self.assertNotIn("--lock-path", source)

    def test_no_live_api_blocks_direct_execute_before_http_or_ledger_write(self):
        from scripts import cabin_capability_audit, serpapi_capability_audit

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            for module, env in (
                (
                    serpapi_capability_audit,
                    {"NO_LIVE_API": "1", "SERPAPI_KEY": TEST_CANARY_SECRET},
                ),
                (
                    cabin_capability_audit,
                    {
                        "NO_LIVE_API": "1",
                        "JUHE_FLIGHT_KEY": TEST_CANARY_SECRET,
                        "DUFFEL_TOKEN": TEST_CANARY_SECRET,
                    },
                ),
            ):
                with self.subTest(module=module.__name__):
                    usage_path = self._initialized_usage(root / module.__name__.split(".")[-1])
                    before = _sha256(usage_path)
                    calls = []

                    def fail_http(*_args, **_kwargs):
                        calls.append(1)
                        return _FakeResponse({})

                    with patch(
                        "scripts.manual_live_guard.load_usage_strict",
                        side_effect=AssertionError("LEDGER_READ_BEFORE_NO_LIVE_GATE"),
                    ), patch(
                        "scripts.manual_live_guard.config_loader.load_merged_config",
                        side_effect=AssertionError("CONFIG_READ_BEFORE_NO_LIVE_GATE"),
                    ), patch(
                        "scripts.manual_live_guard._acquire_singleflight",
                        side_effect=AssertionError("LOCK_BEFORE_NO_LIVE_GATE"),
                    ):
                        report = module.run_audit(
                            execute=True,
                            env=env,
                            usage_path=usage_path,
                            http_get=fail_http,
                            **(
                                {"http_post": fail_http}
                                if module is cabin_capability_audit
                                else {}
                            ),
                        )
                    self.assertEqual(calls, [])
                    self.assertEqual(_sha256(usage_path), before)
                    self.assertEqual(report["status"], "blocked")
                    self.assertEqual(report["gate_code"], "no_live_api")

    def test_no_live_api_cli_refuses_before_dotenv(self):
        from scripts import cabin_capability_audit, serpapi_capability_audit

        for module in (serpapi_capability_audit, cabin_capability_audit):
            with self.subTest(module=module.__name__), patch.dict(
                os.environ, {"NO_LIVE_API": "1"}, clear=True
            ), patch.object(
                module,
                "load_dotenv",
                side_effect=AssertionError("REAL_DOTENV_READ"),
            ):
                stdout = io.StringIO()
                stderr = io.StringIO()
                try:
                    with redirect_stdout(stdout), redirect_stderr(stderr):
                        code = module.main(["--execute"])
                except AssertionError:
                    self.fail("NO_LIVE_API must reject before load_dotenv")
                self.assertNotEqual(code, 0)
                self.assertNotIn("REAL_DOTENV_READ", stdout.getvalue())
                self.assertNotIn("REAL_DOTENV_READ", stderr.getvalue())

    def test_busy_singleflight_blocks_http_and_ledger_write(self):
        from scripts import serpapi_capability_audit

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            usage_path = self._initialized_usage(root)
            before = _sha256(usage_path)
            calls = []
            busy = _FakeGate(
                acquired=False,
                holder={"round_id": "other-round"},
            )
            with _guarded_audit(
                self._quota_config(),
                acquire=lambda *_a, **_k: busy,
            ):
                report = serpapi_capability_audit.run_audit(
                    execute=True,
                    env={"SERPAPI_KEY": TEST_CANARY_SECRET},
                    usage_path=usage_path,
                    http_get=lambda *_a, **_k: calls.append(1),
                    round_id="audit-busy-test",
                )
            self.assertEqual(calls, [])
            self.assertEqual(_sha256(usage_path), before)
            self.assertEqual(report["gate_code"], "singleflight_busy")
            self.assertNotEqual(report["exit_code"], 0)

    def test_quota_refusal_blocks_http_and_ledger_write(self):
        from scripts import serpapi_capability_audit

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            usage_path = self._initialized_usage(root)
            before = _sha256(usage_path)
            calls = []
            blocked_config = {
                "source_quota_budget": {
                    "serpapi": {"monthly": 2, "reserve": 1}
                }
            }
            with patch(
                "scripts.manual_live_guard.config_loader.load_merged_config",
                return_value=blocked_config,
            ), patch(
                "scripts.manual_live_guard._acquire_singleflight",
                side_effect=AssertionError("singleflight must not follow quota refusal"),
            ):
                report = serpapi_capability_audit.run_audit(
                    execute=True,
                    env={"SERPAPI_KEY": TEST_CANARY_SECRET},
                    usage_path=usage_path,
                    http_get=lambda *_a, **_k: calls.append(1),
                    round_id="audit-quota-test",
                )
            self.assertEqual(calls, [])
            self.assertEqual(_sha256(usage_path), before)
            self.assertEqual(report["gate_code"], "quota_or_reserve")

    def test_corrupt_ledger_blocks_before_http_and_is_not_replaced(self):
        from scripts import serpapi_capability_audit

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            usage_path = root / "api_usage.json"
            usage_path.write_text("{broken", encoding="utf-8")
            before = usage_path.read_bytes()
            calls = []
            with patch(
                "scripts.manual_live_guard._acquire_singleflight",
                side_effect=AssertionError(
                    "singleflight must not be acquired after a ledger failure"
                ),
            ):
                report = serpapi_capability_audit.run_audit(
                    execute=True,
                    env={"SERPAPI_KEY": TEST_CANARY_SECRET},
                    usage_path=usage_path,
                    http_get=lambda *_a, **_k: calls.append(1),
                )
            self.assertEqual(calls, [])
            self.assertEqual(usage_path.read_bytes(), before)
            self.assertEqual(report["gate_code"], "quota_ledger_unhealthy")

    def test_plan_is_printed_before_first_http_attempt(self):
        from scripts import serpapi_capability_audit

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            usage_path = self._initialized_usage(root)
            stream = io.StringIO()

            def fake_get(_url, *, params, timeout):
                print("HTTP_ATTEMPT", file=stream)
                return _FakeResponse(_serp_payload(params))

            with _guarded_audit(self._quota_config()):
                with redirect_stdout(stream):
                    serpapi_capability_audit.run_audit(
                        execute=True,
                        env={"SERPAPI_KEY": TEST_CANARY_SECRET},
                        usage_path=usage_path,
                        http_get=fake_get,
                        round_id="audit-plan-order",
                    )
            output = stream.getvalue()
            self.assertIn("[审计计划]", output)
            self.assertIn("计划调用=2", output)
            self.assertLess(output.index("[审计计划]"), output.index("HTTP_ATTEMPT"))

    def test_past_shanghai_date_is_rejected_before_http(self):
        from scripts import cabin_capability_audit
        from subscription_preflight import shanghai_today

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            usage_path = self._initialized_usage(root)
            calls = []
            report = cabin_capability_audit.run_audit(
                execute=True,
                depart_date=(shanghai_today() - timedelta(days=1)).isoformat(),
                sources=("duffel",),
                env={"DUFFEL_TOKEN": TEST_CANARY_SECRET},
                usage_path=usage_path,
                http_get=lambda *_a, **_k: calls.append(1),
                http_post=lambda *_a, **_k: calls.append(1),
            )
            self.assertEqual(calls, [])
            self.assertEqual(report["gate_code"], "past_departure_date")

    def test_quota_preflight_uses_shanghai_today_not_departure_date(self):
        from datetime import date
        from scripts import serpapi_capability_audit

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            usage_path = self._initialized_usage(root)
            observed_as_of = []

            def fake_metrics(*_args, **kwargs):
                observed_as_of.append(kwargs["as_of"])
                return {
                    "kind": "monthly",
                    "remaining": 100,
                    "reserve": 30,
                }

            with _guarded_audit(self._quota_config()), patch(
                "scripts.manual_live_guard.shanghai_today",
                return_value=date(2026, 8, 29),
            ), patch(
                "quota_policy.metrics",
                side_effect=fake_metrics,
            ):
                report = serpapi_capability_audit.run_audit(
                    execute=True,
                    depart_date="2026-10-01",
                    env={},
                    usage_path=usage_path,
                )
            self.assertEqual(report["gate_code"], "missing_credentials")
            self.assertEqual(observed_as_of, [date(2026, 8, 29)])

    def test_mock_live_attempts_are_recorded_with_manual_workload_and_entrypoints(self):
        from api_usage import load_usage_strict
        from scripts import cabin_capability_audit, serpapi_capability_audit

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            cases = (
                (
                    serpapi_capability_audit,
                    {"SERPAPI_KEY": TEST_CANARY_SECRET},
                    {"http_get": lambda _u, *, params, timeout: _FakeResponse(_serp_payload(params))},
                    2,
                    "serpapi_capability_audit",
                ),
                (
                    cabin_capability_audit,
                    {
                        "JUHE_FLIGHT_KEY": TEST_CANARY_SECRET,
                        "DUFFEL_TOKEN": TEST_CANARY_SECRET,
                    },
                    {
                        "http_get": lambda *_a, **_k: _FakeResponse(_juhe_payload()),
                        "http_post": lambda *_a, **_k: _FakeResponse(_duffel_payload()),
                    },
                    2,
                    "cabin_capability_audit",
                ),
            )
            for module, env, clients, expected_calls, entrypoint in cases:
                with self.subTest(module=module.__name__):
                    case_root = root / entrypoint
                    usage_path = self._initialized_usage(case_root)
                    with _guarded_audit(self._quota_config()):
                        stdout = io.StringIO()
                        with redirect_stdout(stdout):
                            report = module.run_audit(
                                execute=True,
                                env=env,
                                usage_path=usage_path,
                                round_id=f"audit-{entrypoint}",
                                **clients,
                            )
                    usage = load_usage_strict(usage_path)
                    entries = usage["entries"][-expected_calls:]
                    self.assertEqual(len(entries), expected_calls)
                    self.assertEqual(
                        [entry["workload_class"] for entry in entries],
                        ["manual_live"] * expected_calls,
                    )
                    self.assertEqual(
                        [entry["entrypoint"] for entry in entries],
                        [entrypoint] * expected_calls,
                    )
                    self.assertEqual(sum(report["actual_calls"].values()), expected_calls)
                    _safe_absence(
                        self,
                        TEST_CANARY_SECRET,
                        stdout=stdout.getvalue(),
                        report=report,
                        report_repr=repr(report),
                        report_json=json.dumps(report, ensure_ascii=False),
                    )

    def test_serpapi_cli_output_never_contains_canary_secret(self):
        from scripts import serpapi_capability_audit

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            usage_path = self._initialized_usage(root)
            output_path = root / "audit.json"
            stdout = io.StringIO()
            stderr = io.StringIO()
            fake_env = {
                "SERPAPI_KEY": TEST_CANARY_SECRET,
                "NO_LIVE_API": "0",
            }
            with patch.dict(os.environ, fake_env, clear=True), patch.object(
                serpapi_capability_audit, "load_dotenv", return_value=False
            ), _guarded_audit(self._quota_config()), patch.object(
                serpapi_capability_audit.requests,
                "get",
                side_effect=lambda _url, *, params, timeout: _FakeResponse(
                    _serp_payload(params)
                ),
            ), redirect_stdout(stdout), redirect_stderr(stderr):
                code = serpapi_capability_audit.main(
                    [
                        "--execute",
                        "--usage-path",
                        str(usage_path),
                        "--output",
                        str(output_path),
                    ]
                )
            self.assertEqual(code, 0)
            _safe_absence(
                self,
                TEST_CANARY_SECRET,
                stdout=stdout.getvalue(),
                stderr=stderr.getvalue(),
                output=output_path.read_text(encoding="utf-8"),
            )

    def test_supplier_errors_redact_canary_secret_from_all_reports(self):
        from scripts import cabin_capability_audit, serpapi_capability_audit

        def fail_http(*_args, **_kwargs):
            raise RuntimeError(
                f"upstream api_key={TEST_CANARY_SECRET} token={TEST_CANARY_SECRET}"
            )

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            cases = (
                (
                    serpapi_capability_audit,
                    {"SERPAPI_KEY": TEST_CANARY_SECRET},
                    {"http_get": fail_http},
                ),
                (
                    cabin_capability_audit,
                    {
                        "JUHE_FLIGHT_KEY": TEST_CANARY_SECRET,
                        "DUFFEL_TOKEN": TEST_CANARY_SECRET,
                    },
                    {"http_get": fail_http, "http_post": fail_http},
                ),
            )
            for module, env, clients in cases:
                with self.subTest(module=module.__name__):
                    usage_path = self._initialized_usage(
                        root / module.__name__.split(".")[-1]
                    )
                    stdout = io.StringIO()
                    stderr = io.StringIO()
                    with _guarded_audit(self._quota_config()), redirect_stdout(
                        stdout
                    ), redirect_stderr(stderr):
                        report = module.run_audit(
                            execute=True,
                            env=env,
                            usage_path=usage_path,
                            round_id="audit-redaction-test",
                            **clients,
                        )
                    _safe_absence(
                        self,
                        TEST_CANARY_SECRET,
                        stdout=stdout.getvalue(),
                        stderr=stderr.getvalue(),
                        report=report,
                        report_repr=repr(report),
                        report_json=json.dumps(report, ensure_ascii=False),
                    )


if __name__ == "__main__":
    unittest.main()
