import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch


class ApiUsageWorkloadTest(unittest.TestCase):
    def test_new_entries_record_each_explicit_workload_class(self):
        from api_usage import initialize_usage_ledger, load_usage_strict, record_actual_requests
        from workload_class import WORKLOAD_CLASSES

        with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as tmp:
            usage_path = Path(tmp) / "api_usage.json"
            initialize_usage_ledger(usage_path)
            for index, workload_class in enumerate(sorted(WORKLOAD_CLASSES)):
                record_actual_requests(
                    {"juhe": 1},
                    path=usage_path,
                    day="2026-08-27",
                    round_id=f"round-{index}",
                    workload_class=workload_class,
                )
            payload = load_usage_strict(usage_path)

        self.assertEqual(
            {entry["workload_class"] for entry in payload["entries"]},
            WORKLOAD_CLASSES,
        )

    def test_historical_entry_without_class_is_unknown_in_memory_only(self):
        from api_usage import entry_workload_class, load_usage_strict

        with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as tmp:
            usage_path = Path(tmp) / "api_usage.json"
            original = (
                '{"version":2,"dates":{"2026-08-20":{"juhe":1}},'
                '"entries":[{"day":"2026-08-20","counts":{"juhe":1}}]}\n'
            ).encode("utf-8")
            usage_path.write_bytes(original)
            payload = load_usage_strict(usage_path)

            self.assertEqual(entry_workload_class(payload["entries"][0]), "unknown")
            self.assertEqual(usage_path.read_bytes(), original)

    def test_retry_attempts_inherit_the_round_workload_class(self):
        import request_cache
        from api_usage import initialize_usage_ledger, load_usage_strict

        class RetryThenSuccess:
            name = "juhe"

            def __init__(self):
                self.calls = 0

            def fetch(self, origin, dest, date_str, cabin_class="economy"):
                self.calls += 1
                if self.calls == 1:
                    raise OSError("retry once")
                return {
                    "source": self.name,
                    "flights": [{"flight_combo": "MU1", "price": 100}],
                }

        with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as tmp:
            root = Path(tmp)
            usage_path = root / "api_usage.json"
            initialize_usage_ledger(usage_path)
            request_cache.reset_for_tests(root / "cache")
            self.addCleanup(request_cache.reset_for_tests, None)
            request_cache.start_request_cache_round(
                "manual-retry",
                track_usage=True,
                usage_path=usage_path,
                workload_class="manual_live",
                entrypoint="web",
            )
            with patch.object(request_cache, "SOURCE_FETCH_IO_RETRY_DELAY_SECONDS", 0):
                request_cache.cached_fetch(
                    RetryThenSuccess(),
                    "PVG",
                    "KIX",
                    "2099-10-01",
                    persist=False,
                    force_fresh=True,
                )
            before_round_end = load_usage_strict(usage_path)
            request_cache.print_request_cache_stats()
            after_round_end = load_usage_strict(usage_path)

        self.assertEqual(len(before_round_end["entries"]), 2)
        self.assertEqual(
            [entry["counts"] for entry in before_round_end["entries"]],
            [{"juhe": 1}, {"juhe": 1}],
        )
        self.assertEqual(
            {entry["workload_class"] for entry in before_round_end["entries"]},
            {"manual_live"},
        )
        self.assertEqual(
            {entry["entrypoint"] for entry in before_round_end["entries"]},
            {"web"},
        )
        self.assertEqual(after_round_end, before_round_end)


if __name__ == "__main__":
    unittest.main()
