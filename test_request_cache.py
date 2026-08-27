import json
import tempfile
import unittest
from datetime import datetime
from pathlib import Path
from unittest.mock import patch


class CountingSource:
    name = "fake"

    def __init__(self):
        self.calls = []

    def fetch(self, origin, dest, date_str, cabin_class="economy"):
        self.calls.append((origin, dest, date_str, cabin_class))
        return {
            "flights": [
                {
                    "flight_combo": f"{origin}{dest}{date_str}{cabin_class}",
                    "price": 100,
                }
            ],
            "source": self.name,
        }

class AlternateCountingSource:
    name = "fake"

    def __init__(self):
        self.calls = []

    def fetch(self, origin, dest, date_str, cabin_class="economy"):
        self.calls.append((origin, dest, date_str, cabin_class))
        return {
            "flights": [
                {
                    "flight_combo": "ALT1",
                    "price": 200,
                    "departure_time": f"{date_str} 08:00",
                    "arrival_time": f"{date_str} 10:00",
                }
            ],
            "source": self.name,
        }


class EquipmentSource:
    route_type = "international"

    def __init__(self, name, flights):
        self.name = name
        self.flights = flights

    def fetch(self, origin, dest, date_str, cabin_class="economy"):
        return {"flights": [dict(item) for item in self.flights], "source": self.name}


class QuotaFailureSource:
    name = "juhe"

    def __init__(self):
        self.calls = []

    def fetch(self, origin, dest, date_str, cabin_class="economy"):
        self.calls.append((origin, dest, date_str, cabin_class))
        return {
            "flights": [],
            "source": self.name,
            "source_status": "failed_quota",
            "error": "配额不足(112)",
            "quota_code": "112",
        }


class EmptyResultSource:
    name = "empty"

    def __init__(self):
        self.calls = []

    def fetch(self, origin, dest, date_str, cabin_class="economy"):
        self.calls.append((origin, dest, date_str, cabin_class))
        return {
            "flights": [],
            "source": self.name,
            "source_status": "empty",
            "raw": {"result": {"flightInfo": []}},
        }


class FailedResultSource:
    name = "failed"

    def __init__(self):
        self.calls = []

    def fetch(self, origin, dest, date_str, cabin_class="economy"):
        self.calls.append((origin, dest, date_str, cabin_class))
        return {
            "flights": [],
            "source": self.name,
            "source_status": "failed",
            "error": "测试失败(500)",
            "raw": {"error_code": 500, "reason": "测试失败"},
        }


class PreflightSkipSource:
    name = "skipped"

    def preflight_skip(self, origin, dest, date_str, cabin_class="economy"):
        return {
            "flights": [],
            "source": self.name,
            "source_status": "skipped_preflight",
            "skipped_reason": "测试前置跳过",
        }

    def fetch(self, origin, dest, date_str, cabin_class="economy"):
        raise AssertionError("源级跳过不得进入 fetch")


class RequestCacheTest(unittest.TestCase):
    def setUp(self):
        from request_cache import reset_for_tests

        self._request_cache_tmp = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        self._request_cache_dir = (
            Path(self._request_cache_tmp.name) / self._testMethodName
        )
        reset_for_tests(self._request_cache_dir)
        self.addCleanup(self._cleanup_request_cache)

    def _cleanup_request_cache(self):
        from request_cache import reset_for_tests

        reset_for_tests(None)
        self._request_cache_tmp.cleanup()

    def test_reset_for_tests_redirects_persistent_cache_away_from_default_dir(self):
        from request_cache import cached_fetch, reset_for_tests

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            production_cache = root / "production"
            isolated_cache = root / self._testMethodName

            with patch("request_cache.DEFAULT_CACHE_DIR", production_cache):
                reset_for_tests(None)
                source = CountingSource()
                cached_fetch(source, "SHA", "PEK", "2099-08-20")
                self.assertEqual(len(source.calls), 1)

                reset_for_tests(isolated_cache)
                isolated_source = CountingSource()
                _, status = cached_fetch(
                    isolated_source,
                    "SHA",
                    "PEK",
                    "2099-08-20",
                    include_cache_status=True,
                )

                self.assertEqual(status, "fresh")
                self.assertEqual(len(isolated_source.calls), 1)
                self.assertTrue(list(isolated_cache.glob("api_*.json")))

    def test_reset_for_tests_clears_runtime_pool_circuit_stats_and_plan_state(self):
        from request_cache import (
            activate_collection_plan,
            cache_key,
            cached_fetch,
            get_process_request_cache_stats,
            get_request_cache_stats,
            reset_for_tests,
            start_request_cache_round,
        )

        with tempfile.TemporaryDirectory() as tmp:
            cache_dir = Path(tmp) / self._testMethodName
            quota_source = QuotaFailureSource()
            planned_key = cache_key(quota_source, "SHA", "PEK", "2099-08-20")
            start_request_cache_round("dirty-round")
            activate_collection_plan({planned_key})
            cached_fetch(quota_source, "SHA", "PEK", "2099-08-20", persist=False)

            reset_for_tests(cache_dir)

            self.assertEqual(get_request_cache_stats()["total"], 0)
            self.assertEqual(get_process_request_cache_stats()["total"], 0)

            healthy_source = CountingSource()
            healthy_source.name = "juhe"
            cached_fetch(healthy_source, "SHA", "PEK", "2099-08-20", persist=False)
            self.assertEqual(len(healthy_source.calls), 1)
            self.assertEqual(get_request_cache_stats()["outside_unique"], 0)

    def test_cached_fetch_reports_fresh_then_cache_without_changing_payload(self):
        from request_cache import cached_fetch, reset_request_cache

        reset_request_cache()
        source = CountingSource()
        passengers = {"adult": 1}

        first, first_status = cached_fetch(
            source,
            "SHA",
            "PEK",
            "2026-08-20",
            passengers,
            "economy",
            persist=False,
            include_cache_status=True,
        )
        second, second_status = cached_fetch(
            source,
            "SHA",
            "PEK",
            "2026-08-20",
            passengers,
            "economy",
            persist=False,
            include_cache_status=True,
        )

        self.assertEqual(first, second)
        self.assertEqual((first_status, second_status), ("fresh", "cache"))
        self.assertEqual(len(source.calls), 1)

    def test_persistent_cache_rejects_different_source_implementation_with_same_name(self):
        from request_cache import cached_fetch, reset_request_cache

        first_source = CountingSource()
        cached_fetch(first_source, "PVG", "KIX", "2099-10-01")
        reset_request_cache()

        second_source = AlternateCountingSource()
        result, status = cached_fetch(
            second_source,
            "PVG",
            "KIX",
            "2099-10-01",
            include_cache_status=True,
        )

        self.assertEqual(status, "fresh")
        self.assertEqual(len(second_source.calls), 1)
        self.assertEqual(result["flights"][0]["flight_combo"], "ALT1")

    def test_legacy_listing_cache_without_complete_flight_details_is_rejected(self):
        from request_cache import _cache_path, cache_key, cached_fetch

        source = AlternateCountingSource()
        source.name = "juhe"
        key = cache_key(source, "PVG", "KIX", "2099-10-01")
        path = _cache_path(key)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(
            json.dumps(
                {
                    "fetched_at": datetime.now().isoformat(timespec="seconds"),
                    "key": list(key),
                    "result": {
                        "source_status": "success",
                        "flights": [{"flight_combo": "TEST1", "price": 100}],
                    },
                },
                ensure_ascii=False,
            ),
            encoding="utf-8",
        )

        result, status = cached_fetch(
            source,
            "PVG",
            "KIX",
            "2099-10-01",
            include_cache_status=True,
        )

        self.assertEqual(status, "fresh")
        self.assertEqual(len(source.calls), 1)
        self.assertEqual(result["flights"][0]["flight_combo"], "ALT1")

    def test_same_request_reuses_in_memory_result(self):
        from request_cache import cached_fetch, reset_request_cache

        reset_request_cache()
        source = CountingSource()
        passengers = {"adult": 2, "child": 1, "elderly": 0, "infant": 0}

        first = cached_fetch(source, "SHA", "PEK", "2026-06-20", passengers, "economy")
        second = cached_fetch(source, "SHA", "PEK", "2026-06-20", passengers, "economy")

        self.assertEqual(first, second)
        self.assertEqual(source.calls, [("SHA", "PEK", "2026-06-20", "economy")])

    def test_key_keeps_direction_and_cabin_separate(self):
        from request_cache import cached_fetch, reset_request_cache

        reset_request_cache()
        source = CountingSource()
        passengers = {"adult": 1}

        cached_fetch(source, "SHA", "PEK", "2026-06-20", passengers, "economy")
        cached_fetch(source, "PEK", "SHA", "2026-06-20", passengers, "economy")
        cached_fetch(source, "SHA", "PEK", "2026-06-20", passengers, "business")

        self.assertEqual(len(source.calls), 3)

    def test_persistent_cache_reuses_result_after_memory_reset(self):
        from request_cache import cached_fetch, reset_request_cache

        with tempfile.TemporaryDirectory() as tmp:
            cache_dir = Path(tmp)
            source = CountingSource()
            passengers = {"adult": 1}

            reset_request_cache()
            cached_fetch(
                source,
                "SHA",
                "PEK",
                "2026-06-20",
                passengers,
                "economy",
                cache_dir=cache_dir,
            )
            reset_request_cache()
            cached_fetch(
                source,
                "SHA",
                "PEK",
                "2026-06-20",
                passengers,
                "economy",
                cache_dir=cache_dir,
            )

            self.assertEqual(source.calls, [("SHA", "PEK", "2026-06-20", "economy")])

    def test_force_fresh_bypasses_memory_and_persistent_cache_reads(self):
        from request_cache import cached_fetch, get_request_cache_stats, reset_request_cache

        with tempfile.TemporaryDirectory() as tmp:
            cache_dir = Path(tmp)
            source = CountingSource()
            passengers = {"adult": 1}

            reset_request_cache()
            cached_fetch(
                source,
                "SHA",
                "PEK",
                "2026-07-31",
                passengers,
                "economy",
                cache_dir=cache_dir,
            )
            cached_fetch(
                source,
                "SHA",
                "PEK",
                "2026-07-31",
                passengers,
                "economy",
                cache_dir=cache_dir,
                force_fresh=True,
            )

            stats = get_request_cache_stats()
            self.assertEqual(len(source.calls), 2)
            self.assertEqual(stats["actual"], 2)
            self.assertEqual(stats["hits"], 0)

    def test_force_fresh_only_calls_once_for_same_key_inside_collection_round(self):
        from request_cache import (
            activate_collection_plan,
            cache_key,
            cached_fetch,
            start_request_cache_round,
        )

        source = CountingSource()
        key = cache_key(source, "SHA", "PEK", "2026-08-20", {"adult": 3}, "economy")
        start_request_cache_round("round-force-fresh")
        activate_collection_plan({key})

        for adult_count in (3, 1):
            cached_fetch(
                source,
                "SHA",
                "PEK",
                "2026-08-20",
                {"adult": adult_count},
                "economy",
                persist=False,
                force_fresh=True,
            )

        self.assertEqual(len(source.calls), 1)

    def test_same_round_pool_ignores_ttl_expiry_after_first_real_request(self):
        from request_cache import (
            activate_collection_plan,
            cache_key,
            cached_fetch,
            reset_request_cache,
            start_request_cache_round,
        )

        reset_request_cache()
        self.addCleanup(reset_request_cache)
        source = CountingSource()
        key = cache_key(source, "PVG", "KIX", "2026-10-01", {"adult": 1}, "economy")
        start_request_cache_round("round-long-analysis")
        activate_collection_plan({key})

        first = cached_fetch(source, "PVG", "KIX", "2026-10-01", persist=False)
        with patch("request_cache._fresh", return_value=False):
            second, status = cached_fetch(
                source,
                "PVG",
                "KIX",
                "2026-10-01",
                persist=False,
                include_cache_status=True,
            )

        self.assertEqual(first, second)
        self.assertEqual(status, "cache")
        self.assertEqual(len(source.calls), 1)

    def test_same_round_pool_pins_persistent_cache_result_after_ttl_expiry(self):
        from request_cache import (
            activate_collection_plan,
            cache_key,
            cached_fetch,
            reset_request_cache,
            start_request_cache_round,
        )

        with tempfile.TemporaryDirectory() as tmp:
            cache_dir = Path(tmp)
            source = CountingSource()
            key = cache_key(source, "PVG", "KIX", "2026-10-01", {"adult": 1}, "economy")

            reset_request_cache()
            self.addCleanup(reset_request_cache)
            cached_fetch(
                source,
                "PVG",
                "KIX",
                "2026-10-01",
                cache_dir=cache_dir,
            )
            reset_request_cache()
            start_request_cache_round("round-persistent-long-analysis")
            activate_collection_plan({key})

            first, first_status = cached_fetch(
                source,
                "PVG",
                "KIX",
                "2026-10-01",
                cache_dir=cache_dir,
                include_cache_status=True,
            )
            with patch("request_cache._fresh", return_value=False):
                second, second_status = cached_fetch(
                    source,
                    "PVG",
                    "KIX",
                    "2026-10-01",
                    cache_dir=cache_dir,
                    include_cache_status=True,
                )

        self.assertEqual(first_status, "cache")
        self.assertEqual(second_status, "cache")
        self.assertEqual(first, second)
        self.assertEqual(len(source.calls), 1)

    def test_source_exception_is_pooled_and_api_key_is_redacted(self):
        from request_cache import cached_fetch, start_request_cache_round

        class FailingSource:
            name = "hasdata"

            def __init__(self):
                self.calls = 0

            def fetch(self, origin, dest, date_str, cabin_class="economy"):
                self.calls += 1
                raise RuntimeError("HTTP 422 api_key=super-secret&route=PVG-KIX")

        source = FailingSource()
        start_request_cache_round("round-failure")
        first = cached_fetch(source, "PVG", "KIX", "2026-10-01", persist=False)
        second = cached_fetch(source, "PVG", "KIX", "2026-10-01", persist=False)

        self.assertEqual(source.calls, 1)
        self.assertEqual(first, second)
        self.assertIn("api_key=***", first["error"])
        self.assertNotIn("super-secret", first["error"])

    def test_juhe_past_date_is_source_skip_not_actual_request(self):
        from request_cache import cached_fetch, get_request_cache_stats, reset_request_cache
        from sources.juhe_source import JuheSource

        reset_request_cache()
        source = JuheSource()

        result = cached_fetch(
            source,
            "PVG",
            "ABQ",
            "2000-01-01",
            {"adult": 1},
            "economy",
            persist=False,
            force_fresh=True,
        )

        stats = get_request_cache_stats()
        self.assertEqual(result["source_status"], "skipped_past_date")
        self.assertEqual(stats["actual"], 0)
        self.assertEqual(stats["hits"], 0)
        self.assertEqual(stats["skipped"], 1)
        self.assertEqual(stats["by_source"]["juhe"]["actual"], 0)
        self.assertEqual(stats["by_source"]["juhe"]["skipped"], 1)

    def test_quota_failure_disables_same_source_for_process(self):
        from request_cache import cached_fetch, get_request_cache_stats, reset_request_cache

        reset_request_cache()
        self.addCleanup(reset_request_cache)
        source = QuotaFailureSource()

        first, first_status = cached_fetch(
            source,
            "SHA",
            "PEK",
            "2026-08-20",
            {"adult": 1},
            "economy",
            persist=False,
            include_cache_status=True,
        )
        second, second_status = cached_fetch(
            source,
            "SHA",
            "PKX",
            "2026-08-20",
            {"adult": 1},
            "economy",
            persist=False,
            include_cache_status=True,
        )

        stats = get_request_cache_stats()["by_source"]["juhe"]
        self.assertEqual(first["source_status"], "failed_quota")
        self.assertEqual(first_status, "fresh")
        self.assertEqual(second_status, "skipped")
        self.assertEqual(second["source_status"], "skipped_source_disabled")
        self.assertEqual(second["skipped_reason"], "配额不足(112)")
        self.assertEqual(source.calls, [("SHA", "PEK", "2026-08-20", "economy")])
        self.assertEqual(stats["actual"], 1)
        self.assertEqual(stats["skipped"], 1)

    def test_empty_result_is_round_only_and_retried_next_round(self):
        from request_cache import cached_fetch, start_request_cache_round

        source = EmptyResultSource()
        start_request_cache_round("empty-round-1")
        first, first_status = cached_fetch(
            source,
            "SHA",
            "PEK",
            "2099-08-20",
            persist=True,
            include_cache_status=True,
        )
        second, second_status = cached_fetch(
            source,
            "SHA",
            "PEK",
            "2099-08-20",
            persist=True,
            include_cache_status=True,
        )

        self.assertEqual(first_status, "fresh")
        self.assertEqual(second_status, "round_empty")
        self.assertEqual(first["flights"], [])
        self.assertEqual(second["flights"], [])
        self.assertEqual(len(source.calls), 1)
        self.assertEqual(list(self._request_cache_dir.glob("api_*.json")), [])

        start_request_cache_round("empty-round-2")
        _third, third_status = cached_fetch(
            source,
            "SHA",
            "PEK",
            "2099-08-20",
            persist=True,
            include_cache_status=True,
        )
        self.assertEqual(third_status, "fresh")
        self.assertEqual(len(source.calls), 2)

    def test_listing_empty_result_archives_raw_evidence_with_redaction(self):
        from log_utils import end_round_log_archive, start_round_log_archive
        from request_cache import cached_fetch, start_request_cache_round

        class EmptyJuheSource:
            name = "juhe"

            def fetch(self, origin, dest, date_str, cabin_class="economy"):
                return {
                    "flights": [],
                    "source": self.name,
                    "source_status": "empty",
                    "raw": {
                        "resultcode": "200",
                        "error_code": 0,
                        "reason": "???????",
                        "api_key": "must-not-leak",
                    },
                }

        archive_root = self._request_cache_dir / "rounds"
        path = start_round_log_archive(
            "empty-evidence-round",
            root_dir=archive_root,
        )
        try:
            start_request_cache_round("empty-evidence-round")
            result, status = cached_fetch(
                EmptyJuheSource(),
                "PVG",
                "KIX",
                "2099-08-20",
                persist=True,
                include_cache_status=True,
            )
        finally:
            end_round_log_archive(status="ok")

        content = path.read_text(encoding="utf-8")
        self.assertEqual(status, "fresh")
        self.assertEqual(result["source_status"], "empty")
        self.assertIn('"resultcode": "200"', content)
        self.assertIn("???????", content)
        self.assertNotIn("must-not-leak", content)

    def test_failed_result_is_not_reused_by_next_round_or_persisted(self):
        from request_cache import cached_fetch, start_request_cache_round

        source = FailedResultSource()
        start_request_cache_round("failed-round-1")
        _first, first_status = cached_fetch(
            source,
            "SHA",
            "PEK",
            "2099-08-21",
            persist=True,
            include_cache_status=True,
        )
        start_request_cache_round("failed-round-2")
        _second, second_status = cached_fetch(
            source,
            "SHA",
            "PEK",
            "2099-08-21",
            persist=True,
            include_cache_status=True,
        )

        self.assertEqual(first_status, "fresh")
        self.assertEqual(second_status, "fresh")
        self.assertEqual(len(source.calls), 2)
        self.assertEqual(list(self._request_cache_dir.glob("api_*.json")), [])

    def test_tracked_round_flushes_actual_usage_once(self):
        from api_usage import initialize_usage_ledger, load_usage_strict
        from request_cache import (
            cached_fetch,
            print_request_cache_stats,
            reset_request_cache,
            start_request_cache_round,
        )

        with tempfile.TemporaryDirectory() as tmp:
            usage_path = Path(tmp) / "api_usage.json"
            initialize_usage_ledger(usage_path)
            source = CountingSource()
            reset_request_cache()
            start_request_cache_round(
                "tracked-round",
                track_usage=True,
                usage_path=usage_path,
            )
            cached_fetch(
                source,
                "SHA",
                "PEK",
                "2026-08-20",
                persist=False,
            )
            print_request_cache_stats()
            print_request_cache_stats()

            usage = load_usage_strict(usage_path)

        today_counts = next(iter(usage["dates"].values()))
        self.assertEqual(today_counts, {"fake": 1})

    def test_usage_ledger_records_only_actual_and_appends_round_entry(self):
        from api_usage import initialize_usage_ledger, load_usage_strict
        from request_cache import (
            cached_fetch,
            print_request_cache_stats,
            start_request_cache_round,
        )

        with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as tmp:
            usage_path = Path(tmp) / "api_usage.json"
            initialize_usage_ledger(usage_path)
            source = CountingSource()
            start_request_cache_round(
                "audit-round",
                track_usage=True,
                usage_path=usage_path,
            )
            cached_fetch(source, "SHA", "PEK", "2099-08-20", persist=False)
            cached_fetch(source, "SHA", "PEK", "2099-08-20", persist=False)
            cached_fetch(
                PreflightSkipSource(),
                "SHA",
                "PKX",
                "2099-08-20",
                persist=False,
            )
            print_request_cache_stats()
            usage = load_usage_strict(usage_path)

        today_counts = next(iter(usage["dates"].values()))
        self.assertEqual(today_counts, {"fake": 1})
        self.assertEqual(len(usage["entries"]), 1)
        entry = usage["entries"][0]
        self.assertEqual(entry["round_id"], "audit-round")
        self.assertEqual(entry["counts"], {"fake": 1})
        self.assertRegex(entry["recorded_at"], r"^\d{4}-\d{2}-\d{2}T")

    def test_tracked_round_flushes_actual_usage_once(self):
        from api_usage import initialize_usage_ledger, load_usage_strict
        from request_cache import (
            cached_fetch,
            print_request_cache_stats,
            reset_request_cache,
            start_request_cache_round,
        )

        with tempfile.TemporaryDirectory() as tmp:
            usage_path = Path(tmp) / "api_usage.json"
            initialize_usage_ledger(usage_path)
            source = CountingSource()
            reset_request_cache()
            start_request_cache_round(
                "tracked-round",
                track_usage=True,
                usage_path=usage_path,
            )
            cached_fetch(
                source,
                "SHA",
                "PEK",
                "2026-08-20",
                persist=False,
            )
            print_request_cache_stats()
            print_request_cache_stats()

            usage = load_usage_strict(usage_path)

        today_counts = next(iter(usage["dates"].values()))
        self.assertEqual(today_counts, {"fake": 1})


    def test_stats_requested_counts_real_fetch_not_cache_hits(self):
        from request_cache import cached_fetch, get_request_cache_stats, reset_request_cache

        reset_request_cache()
        source = CountingSource()
        passengers = {"adult": 1}

        cached_fetch(source, "SHA", "PEK", "2026-06-20", passengers, "economy")
        cached_fetch(source, "SHA", "PEK", "2026-06-20", passengers, "economy")

        fake_stats = get_request_cache_stats()["by_source"]["fake"]
        self.assertEqual(fake_stats["actual"], 1)
        self.assertEqual(fake_stats["requested"], 1)
        self.assertEqual(fake_stats["hits"], 1)

    def test_round_stats_reset_while_process_totals_accumulate(self):
        from request_cache import (
            cached_fetch,
            get_process_request_cache_stats,
            get_request_cache_stats,
            print_request_cache_stats,
            reset_request_cache,
            start_request_cache_round,
        )

        reset_request_cache()
        hasdata = CountingSource()
        hasdata.name = "hasdata"
        juhe = CountingSource()
        juhe.name = "juhe"

        start_request_cache_round("round-international")
        cached_fetch(
            hasdata,
            "PVG",
            "KIX",
            "2026-10-01",
            {"adult": 1},
            persist=False,
            force_fresh=True,
        )
        start_request_cache_round("round-domestic")
        cached_fetch(
            juhe,
            "SHA",
            "PEK",
            "2026-07-31",
            {"adult": 1},
            persist=False,
            force_fresh=True,
        )

        round_stats = get_request_cache_stats()
        process_stats = get_process_request_cache_stats()
        self.assertNotIn("hasdata", round_stats["by_source"])
        self.assertEqual(round_stats["by_source"]["juhe"]["requested"], 1)
        self.assertEqual(round_stats["actual"], 1)
        self.assertEqual(process_stats["by_source"]["hasdata"]["requested"], 1)
        self.assertEqual(process_stats["by_source"]["juhe"]["requested"], 1)
        self.assertEqual(process_stats["actual"], 2)

        with patch("request_cache.safe_log") as log:
            print_request_cache_stats()
        messages = [call.args[0] for call in log.call_args_list]
        round_line = next(line for line in messages if line.startswith("[API统计] "))
        process_line = next(
            line for line in messages if line.startswith("[API统计-进程累计] ")
        )
        self.assertIn("round=round-domestic", round_line)
        self.assertNotIn("hasdata", round_line)
        self.assertIn("hasdata", process_line)

    def test_equipment_codes_are_summarized_once_per_source_and_round(self):
        from request_cache import (
            cached_fetch,
            print_request_cache_stats,
            reset_request_cache,
            start_request_cache_round,
        )

        reset_request_cache()
        self.addCleanup(reset_request_cache)
        juhe = EquipmentSource(
            "juhe",
            [
                {"flight_combo": "MU225", "price": 4883, "aircraft_code": "320"},
                {"flight_combo": "MU730", "price": 4153, "aircraft_code": "32S"},
            ],
        )
        hasdata = EquipmentSource(
            "hasdata",
            [
                {
                    "flight_combo": "MU225",
                    "price": 5124,
                    "segments": [{"aircraft": "Airbus A320"}],
                },
                {
                    "flight_combo": "JL891",
                    "price": 7268,
                    "segments": [{"aircraft": "Boeing 787"}],
                },
            ],
        )

        with patch("request_cache.safe_log") as log:
            start_request_cache_round("round-equipment")
            cached_fetch(juhe, "PVG", "KIX", "2026-10-01", {"adult": 1}, persist=False)
            cached_fetch(juhe, "KIX", "PVG", "2026-10-06", {"adult": 1}, persist=False)
            cached_fetch(hasdata, "PVG", "KIX", "2026-10-01", {"adult": 1}, persist=False)
            print_request_cache_stats()

        messages = [str(call.args[0]) for call in log.call_args_list]
        self.assertFalse(any(message.startswith("[机型码收集]") for message in messages))
        summaries = [message for message in messages if message.startswith("[机型码汇总]")]
        self.assertEqual(len(summaries), 2)
        juhe_summary = next(message for message in summaries if "源=juhe" in message)
        hasdata_summary = next(message for message in summaries if "源=hasdata" in message)
        self.assertIn("组合数=4", juhe_summary)
        self.assertIn("机型种类=2", juhe_summary)
        self.assertIn("未映射机型=[]", juhe_summary)
        self.assertIn("组合数=2", hasdata_summary)
        self.assertIn("机型种类=2", hasdata_summary)
        self.assertIn("未映射机型=[]", hasdata_summary)

    def test_aggregator_collect_reuses_cached_source_result(self):
        from request_cache import reset_request_cache
        from sources.aggregator import FlightAggregator

        reset_request_cache()
        source = CountingSource()
        source.name = "juhe"
        aggregator = FlightAggregator([source], [], route_type="domestic")

        aggregator.collect("SHA", "PEK", "2026-06-20", passengers={"adult": 1})
        aggregator.collect("SHA", "PEK", "2026-06-20", passengers={"adult": 1})

        self.assertEqual(source.calls, [("SHA", "PEK", "2026-06-20", "economy")])

    def test_aggregator_reports_fresh_then_cached_collection(self):
        from request_cache import reset_request_cache
        from sources.aggregator import FlightAggregator

        reset_request_cache()
        source = CountingSource()
        source.name = "juhe"
        aggregator = FlightAggregator([source], [], route_type="domestic")

        first = aggregator.collect("SHA", "PEK", "2026-08-20", passengers={"adult": 1})
        second = aggregator.collect("SHA", "PEK", "2026-08-20", passengers={"adult": 1})

        self.assertEqual(first["request_cache_status"], "fresh")
        self.assertEqual(second["request_cache_status"], "cache")
        self.assertEqual(len(first["flights"]), len(second["flights"]))

    def test_aggregator_force_fresh_reaches_request_cache(self):
        from request_cache import reset_request_cache
        from sources.aggregator import FlightAggregator

        reset_request_cache()
        source = CountingSource()
        source.name = "juhe"
        aggregator = FlightAggregator([source], [], route_type="domestic")

        aggregator.collect(
            "SHA",
            "PEK",
            "2026-07-31",
            passengers={"adult": 1},
            force_fresh=True,
        )
        aggregator.collect(
            "SHA",
            "PEK",
            "2026-07-31",
            passengers={"adult": 1},
            force_fresh=True,
        )

        self.assertEqual(len(source.calls), 2)

    def test_price_calendar_source_fetch_reuses_cached_source_result(self):
        from price_calendar import _source_fetch
        from request_cache import reset_request_cache

        reset_request_cache()
        source = CountingSource()

        with tempfile.TemporaryDirectory() as tmp:
            with patch("request_cache.DEFAULT_CACHE_DIR", Path(tmp)):
                _source_fetch(source, "SHA", "PEK", "2026-06-20", "economy", {"adult": 1})
                _source_fetch(source, "SHA", "PEK", "2026-06-20", "economy", {"adult": 1})

        self.assertEqual(source.calls, [("SHA", "PEK", "2026-06-20", "economy")])

if __name__ == "__main__":
    unittest.main()
