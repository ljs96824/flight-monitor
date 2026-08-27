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


class CabinCapabilityAuditTest(unittest.TestCase):
    def test_budget_rejects_seventh_call_and_per_source_fourth_call(self):
        from scripts.cabin_capability_audit import AuditBudget, AuditBudgetExceeded

        budget = AuditBudget()
        for _ in range(3):
            budget.reserve("juhe")
        with self.assertRaises(AuditBudgetExceeded):
            budget.reserve("juhe")

        for _ in range(3):
            budget.reserve("duffel")
        with self.assertRaises(AuditBudgetExceeded):
            budget.reserve("other")

    def test_juhe_single_reference_price_is_not_business_cabin_capability(self):
        from scripts.cabin_capability_audit import summarize_juhe_response

        summary = summarize_juhe_response(
            {
                "error_code": 0,
                "reason": "成功",
                "result": {
                    "flightInfo": [
                        {
                            "flightNo": "MU225",
                            "ticketPrice": 4883,
                            "equipment": "32N",
                        }
                    ]
                },
            }
        )

        self.assertEqual(summary["flight_count"], 1)
        self.assertEqual(summary["price_samples"], [4883])
        self.assertEqual(summary["cabin_field_paths"], [])
        self.assertEqual(summary["capability"], "unavailable")

    def test_duffel_live_business_offer_reports_total_and_tax_scope(self):
        from scripts.cabin_capability_audit import summarize_duffel_response

        summary = summarize_duffel_response(
            {
                "data": {
                    "live_mode": True,
                    "offers": [
                        {
                            "total_amount": "1234.50",
                            "total_currency": "CNY",
                            "tax_amount": "234.50",
                            "tax_currency": "CNY",
                            "owner": {"name": "Example Air", "iata_code": "EX"},
                            "slices": [
                                {
                                    "segments": [
                                        {
                                            "passengers": [
                                                {
                                                    "cabin_class": "business",
                                                    "cabin_class_marketing_name": "Business",
                                                }
                                            ]
                                        }
                                    ]
                                }
                            ],
                        }
                    ],
                }
            }
        )

        self.assertTrue(summary["live_mode"])
        self.assertEqual(summary["observed_cabins"], ["business"])
        self.assertEqual(summary["minimum_offer"]["total_amount"], "1234.50")
        self.assertEqual(summary["minimum_offer"]["tax_amount"], "234.50")
        self.assertTrue(summary["minimum_offer"]["tax_included_in_total"])
        self.assertEqual(summary["capability"], "available")

    def test_duffel_test_offer_is_technical_evidence_not_market_price(self):
        from scripts.cabin_capability_audit import summarize_duffel_response

        summary = summarize_duffel_response(
            {
                "data": {
                    "live_mode": False,
                    "offers": [
                        {
                            "total_amount": "99.00",
                            "total_currency": "GBP",
                            "tax_amount": "20.00",
                            "tax_currency": "GBP",
                            "slices": [
                                {
                                    "segments": [
                                        {"passengers": [{"cabin_class": "business"}]}
                                    ]
                                }
                            ],
                        }
                    ],
                }
            }
        )

        self.assertEqual(summary["capability"], "partial")
        self.assertFalse(summary["market_price_usable"])

    def test_mock_audit_records_only_two_actual_calls_without_secrets(self):
        from api_usage import initialize_usage_ledger, load_usage_strict
        from scripts.cabin_capability_audit import run_audit

        captured = {}

        def fake_get(url, *, params, timeout):
            captured["juhe"] = {"url": url, "params": params, "timeout": timeout}
            return _FakeResponse(
                {
                    "error_code": 0,
                    "reason": "成功",
                    "result": {
                        "flightInfo": [
                            {"flightNo": "MU225", "ticketPrice": 4883}
                        ]
                    },
                }
            )

        def fake_post(url, *, params, json, headers, timeout):
            captured["duffel"] = {
                "url": url,
                "params": params,
                "json": json,
                "headers": headers,
                "timeout": timeout,
            }
            return _FakeResponse(
                {
                    "data": {
                        "live_mode": True,
                        "offers": [
                            {
                                "total_amount": "2500.00",
                                "total_currency": "CNY",
                                "tax_amount": "500.00",
                                "tax_currency": "CNY",
                                "slices": [
                                    {
                                        "segments": [
                                            {
                                                "passengers": [
                                                    {"cabin_class": "business"}
                                                ]
                                            }
                                        ]
                                    }
                                ],
                            }
                        ],
                    }
                }
            )

        with tempfile.TemporaryDirectory() as tmp:
            usage_path = Path(tmp) / "api_usage.json"
            initialize_usage_ledger(usage_path)
            report = run_audit(
                execute=True,
                env={"JUHE_FLIGHT_KEY": "secret-key", "DUFFEL_TOKEN": "secret-token"},
                usage_path=usage_path,
                http_get=fake_get,
                http_post=fake_post,
                round_id="audit-test",
            )
            usage = load_usage_strict(usage_path)

        self.assertEqual(report["actual_calls"], {"juhe": 1, "duffel": 1})
        self.assertEqual(usage["entries"][-2]["counts"], {"juhe": 1})
        self.assertEqual(usage["entries"][-1]["counts"], {"duffel": 1})
        public_report = str(report)
        self.assertNotIn("secret-key", public_report)
        self.assertNotIn("secret-token", public_report)
        self.assertEqual(
            report["calls"][1]["parameters"]["cabin_class"], "business"
        )
        self.assertEqual(captured["duffel"]["json"]["data"]["cabin_class"], "business")
        self.assertEqual(report["recommendation"]["route"], "B")

    def test_dry_run_never_calls_http_or_writes_usage(self):
        from scripts.cabin_capability_audit import run_audit

        def fail(*_args, **_kwargs):
            raise AssertionError("dry-run 不应调用 HTTP")

        with tempfile.TemporaryDirectory() as tmp:
            usage_path = Path(tmp) / "api_usage.json"
            report = run_audit(
                execute=False,
                env={"JUHE_FLIGHT_KEY": "x", "DUFFEL_TOKEN": "y"},
                usage_path=usage_path,
                http_get=fail,
                http_post=fail,
                round_id="audit-dry-run",
            )
            self.assertFalse(usage_path.exists())

        self.assertEqual(report["actual_calls"], {})


if __name__ == "__main__":
    unittest.main()
