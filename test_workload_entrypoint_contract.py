import ast
import tempfile
import unittest
from pathlib import Path


class WorkloadEntrypointContractTest(unittest.TestCase):
    def test_usage_entry_records_explicit_entrypoint(self):
        from api_usage import initialize_usage_ledger, load_usage_strict, record_actual_requests

        with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as tmp:
            path = Path(tmp) / "api_usage.json"
            initialize_usage_ledger(path)
            record_actual_requests(
                {"juhe": 1},
                path=path,
                workload_class="canary",
                entrypoint="basket_canary",
            )
            entry = load_usage_strict(path)["entries"][0]

        self.assertEqual(entry["workload_class"], "canary")
        self.assertEqual(entry["entrypoint"], "basket_canary")

    def test_all_production_round_starts_pass_workload_and_entrypoint(self):
        root = Path(__file__).resolve().parent
        calls = []
        for filename in ("main.py", "basket_collect.py"):
            tree = ast.parse((root / filename).read_text(encoding="utf-8"))
            for node in ast.walk(tree):
                if not isinstance(node, ast.Call):
                    continue
                if isinstance(node.func, ast.Name) and node.func.id == "start_request_cache_round":
                    calls.append(
                        (
                            filename,
                            {keyword.arg for keyword in node.keywords if keyword.arg},
                        )
                    )

        self.assertEqual(len(calls), 3)
        for filename, keywords in calls:
            self.assertIn("workload_class", keywords, filename)
            self.assertIn("entrypoint", keywords, filename)

    def test_basket_cli_has_explicit_canary_mode(self):
        from basket_collect import build_parser

        self.assertTrue(build_parser().parse_args(["--canary"]).canary)
        self.assertFalse(build_parser().parse_args([]).canary)


if __name__ == "__main__":
    unittest.main()
