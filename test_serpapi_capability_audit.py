import io
import os
import sys
import tempfile
import types
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from unittest.mock import patch


class _FakeResponse:
    def __init__(self, payload, status_code=200):
        self._payload = payload
        self.status_code = status_code

    def json(self):
        return self._payload

    def raise_for_status(self):
        if self.status_code >= 400:
            raise RuntimeError(f"HTTP {self.status_code}")


def _fixture(travel_class, price):
    return {
        "search_parameters": {
            "engine": "google_flights",
            "travel_class": travel_class,
            "currency": "CNY",
        },
        "best_flights": [
            {
                "price": price,
                "total_duration": 250,
                "flights": [
                    {
                        "airline": "China Eastern",
                        "flight_number": "MU 225",
                        "travel_class": (
                            "Business" if travel_class == 3 else "Economy"
                        ),
                        "departure_airport": {
                            "id": "PVG",
                            "time": "2026-10-01 08:55",
                        },
                        "arrival_airport": {
                            "id": "KIX",
                            "time": "2026-10-01 12:10",
                        },
                        "plane_and_crew_by": "China Eastern",
                    }
                ],
                "layovers": [],
            }
        ],
        "other_flights": [],
    }


class SerpApiCapabilityAuditTest(unittest.TestCase):
    def test_summary_requires_real_business_airline_and_positive_price(self):
        from scripts.serpapi_capability_audit import summarize_response

        summary = summarize_response(_fixture(3, 9230), requested_cabin="business")

        self.assertEqual(summary["best_flights_count"], 1)
        self.assertEqual(summary["other_flights_count"], 0)
        self.assertEqual(summary["airlines"], ["China Eastern"])
        self.assertEqual(summary["minimum_price"], 9230)
        self.assertEqual(summary["currency"], "CNY")
        self.assertEqual(summary["capability"], "available")
        self.assertEqual(summary["codeshare_basis"], ["plane_and_crew_by"])

    def test_business_without_real_quote_fails_gate(self):
        from scripts.serpapi_capability_audit import summarize_response

        summary = summarize_response(
            {"search_parameters": {"travel_class": 3}, "best_flights": []},
            requested_cabin="business",
        )

        self.assertEqual(summary["capability"], "unavailable")
        self.assertFalse(summary["production_gate_passed"])

    def test_execute_calls_each_cabin_once_and_records_two_ledger_entries(self):
        from api_usage import load_usage
        from scripts.serpapi_capability_audit import run_audit

        calls = []

        def fake_get(url, *, params, timeout):
            calls.append(dict(params))
            return _FakeResponse(
                _fixture(int(params["travel_class"]), 9230 if params["travel_class"] == 3 else 2882)
            )

        with tempfile.TemporaryDirectory() as tmp:
            usage_path = Path(tmp) / "api_usage.json"
            report = run_audit(
                execute=True,
                env={"SERPAPI_KEY": "secret"},
                usage_path=usage_path,
                http_get=fake_get,
                round_id="audit-serpapi-test",
            )
            usage = load_usage(usage_path)

        self.assertEqual([call["travel_class"] for call in calls], [3, 1])
        self.assertEqual(report["actual_calls"], {"serpapi": 2})
        self.assertTrue(report["production_gate_passed"])
        self.assertEqual(usage["entries"][-2]["counts"], {"serpapi": 1})
        self.assertEqual(usage["entries"][-1]["counts"], {"serpapi": 1})
        self.assertNotIn("secret", str(report))

    def test_audit_accepts_each_supported_key_alias_and_logs_only_its_name(self):
        from scripts.serpapi_capability_audit import run_audit

        def fake_get(_url, *, params, timeout):
            self.assertGreater(timeout, 0)
            return _FakeResponse(_fixture(int(params["travel_class"]), 9230))

        aliases = ("SERPAPI_KEY", "SERPAPI_API_KEY", "SERP_API_KEY")
        with tempfile.TemporaryDirectory() as tmp:
            for alias in aliases:
                with self.subTest(alias=alias):
                    secret = f"secret-for-{alias}"
                    output = io.StringIO()
                    with redirect_stdout(output):
                        report = run_audit(
                            execute=True,
                            env={alias: secret},
                            usage_path=Path(tmp) / f"{alias}.json",
                            http_get=fake_get,
                            round_id=f"audit-{alias}",
                        )
                    rendered = output.getvalue()
                    self.assertTrue(report["production_gate_passed"])
                    self.assertIn(f"[密钥] 已识别 来源变量={alias}", rendered)
                    self.assertNotIn(secret, rendered)
                    self.assertNotIn(secret, str(report))

    def test_source_accepts_each_supported_key_alias(self):
        from sources.serpapi_source import SerpAPISource

        aliases = ("SERPAPI_KEY", "SERPAPI_API_KEY", "SERP_API_KEY")
        for alias in aliases:
            with self.subTest(alias=alias):
                captured = []

                class FakeGoogleSearch:
                    def __init__(self, params):
                        captured.append(dict(params))

                    def get_dict(self):
                        return {"best_flights": [], "other_flights": []}

                secret = f"source-secret-for-{alias}"
                output = io.StringIO()
                with patch.dict(
                    sys.modules,
                    {"serpapi": types.SimpleNamespace(GoogleSearch=FakeGoogleSearch)},
                ), patch.dict(os.environ, {alias: secret}, clear=True), redirect_stdout(
                    output
                ):
                    SerpAPISource().fetch("PVG", "KIX", "2026-10-01", "business")

                self.assertEqual(captured[0]["api_key"], secret)
                self.assertIn(f"[密钥] 已识别 来源变量={alias}", output.getvalue())
                self.assertNotIn(secret, output.getvalue())

    def test_missing_key_gate_reason_lists_env_variable_names_without_values(self):
        from scripts.serpapi_capability_audit import run_audit

        def fail(*_args, **_kwargs):
            raise AssertionError("缺少密钥时不得访问网络")

        with tempfile.TemporaryDirectory() as tmp:
            env_path = Path(tmp) / ".env"
            env_path.write_text(
                "OTHER_TOKEN=do-not-log-this\nSMTP_HOST=smtp.example.com\n",
                encoding="utf-8",
            )
            report = run_audit(
                execute=True,
                env={},
                env_path=env_path,
                usage_path=Path(tmp) / "api_usage.json",
                http_get=fail,
            )

        reason = report["gate_reason"]
        self.assertIn("SERPAPI_KEY/SERPAPI_API_KEY/SERP_API_KEY", reason)
        self.assertIn(".env 实际变量名=[OTHER_TOKEN, SMTP_HOST]", reason)
        self.assertNotIn("do-not-log-this", reason)
        self.assertNotIn("smtp.example.com", reason)
        self.assertEqual(report["actual_calls"], {})

    def test_dry_run_is_zero_io_and_lists_public_parameters(self):
        from scripts.serpapi_capability_audit import run_audit

        def fail(*_args, **_kwargs):
            raise AssertionError("dry-run 不应访问网络")

        with tempfile.TemporaryDirectory() as tmp:
            usage_path = Path(tmp) / "api_usage.json"
            report = run_audit(
                execute=False,
                env={"SERPAPI_KEY": "secret"},
                usage_path=usage_path,
                http_get=fail,
            )
            self.assertFalse(usage_path.exists())

        self.assertEqual(report["actual_calls"], {})
        self.assertEqual(
            [item["travel_class"] for item in report["planned_calls"]],
            [3, 1],
        )

    def test_budget_stops_before_seventh_call(self):
        from scripts.serpapi_capability_audit import AuditBudget, AuditBudgetExceeded

        budget = AuditBudget(total_limit=6, source_limit=3)
        for _ in range(3):
            budget.reserve("serpapi")
        with self.assertRaises(AuditBudgetExceeded):
            budget.reserve("serpapi")


if __name__ == "__main__":
    unittest.main()
