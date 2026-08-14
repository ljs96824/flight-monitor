import tempfile
import unittest
from pathlib import Path


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
