import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch


LEGACY_FIXTURE = {
    "source_quota_budget": {
        "juhe": {
            "kind": "purchased_packs",
            "packs": [
                {"id": "pack-fixture", "added": 100, "added_at": "2026-08-01"}
            ],
            "reconciliation": {
                "checked_at": "2026-08-02",
                "console_used": 10,
                "console_remaining": 90,
                "local_ledger_used": 9,
                "unrecorded_usage_adjustment": 1,
            },
            "reserve": {
                "kind": "workload_p90",
                "epoch_started_at": "2026-08-01T00:00:00+08:00",
                "window_complete_days": 7,
                "target_date": "2026-10-01",
                "minimum_daily_p90": 10,
                "safety_multiplier": 1.2,
                "manual_live_buffer": 30,
                "canary_buffer": 12,
                "research_batch_calls": 30,
                "scheduled_anomaly_threshold": 12,
                "scheduled_anomaly_consecutive_days": 2,
            },
        },
        "serpapi": {"monthly": 250, "reserve": 30},
    },
    "source_quota_low_remaining_threshold": 50,
    "FRESHNESS_HOURS": 6,
    "SUB_ROUND_FRESH_SCOPE": "primary_only",
    "SERPAPI_ECONOMY_CROSS_CHECK": False,
    "RESEARCH_BASKET_ENABLED": True,
    "RESEARCH_BASKET_STRATEGY": "cohort_v2",
    "research_cohort_v2_gates": {
        "backup_evidence_max_age_days": 30,
        "minimum_expected_days": 30,
        "minimum_worst_case_days": 20,
    },
    "paused_research_routes": [
        {"route": "AAA->BBB", "reason": "fixture", "resume_when": "manual"}
    ],
    "retention_days": {"payloads": 90, "round_archives": 90, "backups": 180},
    "subscriptions": [{"name": "fixture", "origin": "AAA", "destination": "BBB"}],
}


class RuntimeConfigLoaderTest(unittest.TestCase):
    def test_split_then_deep_merge_is_field_for_field_identical(self):
        from config_loader import deep_merge, split_legacy_config

        defaults, runtime = split_legacy_config(LEGACY_FIXTURE)

        self.assertEqual(deep_merge(defaults, runtime), LEGACY_FIXTURE)
        self.assertEqual(defaults["source_quota_budget"]["juhe"]["packs"], [])
        self.assertNotIn("console_used", str(defaults))
        self.assertEqual(runtime["subscriptions"], LEGACY_FIXTURE["subscriptions"])

    def test_missing_or_corrupt_runtime_config_fails_closed(self):
        import yaml

        from config_loader import RuntimeConfigError, load_merged_config

        with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as directory:
            root = Path(directory)
            defaults, runtime = __import__("config_loader").split_legacy_config(
                LEGACY_FIXTURE
            )
            defaults_path = root / "config.defaults.yaml"
            runtime_path = root / "runtime_config.yaml"
            defaults_path.write_text(
                yaml.safe_dump(defaults, allow_unicode=True, sort_keys=False),
                encoding="utf-8",
            )

            with self.assertRaises(RuntimeConfigError):
                load_merged_config(defaults_path, runtime_path)

            runtime_path.write_text("[broken", encoding="utf-8")
            with self.assertRaises(RuntimeConfigError):
                load_merged_config(defaults_path, runtime_path)

            runtime_path.write_text("[]\n", encoding="utf-8")
            with self.assertRaises(RuntimeConfigError):
                load_merged_config(defaults_path, runtime_path)

            runtime_path.write_text(
                yaml.safe_dump(runtime, allow_unicode=True, sort_keys=False),
                encoding="utf-8",
            )
            self.assertEqual(
                load_merged_config(defaults_path, runtime_path), LEGACY_FIXTURE
            )

    def test_production_basket_stops_before_side_effects_when_runtime_is_missing(self):
        from config_loader import RuntimeConfigError
        from basket_collect import run_basket

        with (
            patch(
                "basket_collect.load_collection_settings",
                side_effect=RuntimeConfigError("运行配置缺失"),
            ),
            patch("basket_collect.acquire_collection_singleflight") as acquire,
            patch("basket_collect.start_request_cache_round") as start_cache,
            patch("basket_collect.build_collection_plan") as build_plan,
            self.assertRaises(RuntimeConfigError),
        ):
            run_basket()

        acquire.assert_not_called()
        start_cache.assert_not_called()
        build_plan.assert_not_called()

    def test_tracked_config_contract_contains_no_runtime_facts(self):
        import yaml

        root = Path(__file__).resolve().parent
        tracked = subprocess.run(
            [
                "git",
                "ls-files",
                "config.yaml",
                "config.defaults.yaml",
                "config.example.yaml",
            ],
            cwd=root,
            check=True,
            capture_output=True,
            text=True,
            encoding="utf-8",
        ).stdout.splitlines()
        self.assertEqual(
            set(tracked),
            {"config.yaml", "config.defaults.yaml", "config.example.yaml"},
        )
        forbidden_keys = {
            "console_used",
            "console_remaining",
            "unrecorded_usage_adjustment",
        }
        for relative in tracked:
            raw_text = (root / relative).read_text(encoding="utf-8")
            payload = yaml.safe_load(raw_text) or {}
            for key in forbidden_keys:
                self.assertNotIn(key, raw_text, relative)
            self.assertEqual(payload.get("subscriptions") or [], [], relative)
            target_date = (
                ((payload.get("source_quota_budget") or {}).get("juhe") or {})
                .get("reserve", {})
                .get("target_date")
            )
            self.assertIn(target_date, (None, "YYYY-MM-DD", "<YYYY-MM-DD>"), relative)
        legacy_policy = yaml.safe_load(
            (root / "config.yaml").read_text(encoding="utf-8")
        )
        defaults_policy = yaml.safe_load(
            (root / "config.defaults.yaml").read_text(encoding="utf-8")
        )
        self.assertEqual(legacy_policy, defaults_policy)


class RuntimeConfigMigrationTest(unittest.TestCase):
    def test_migration_is_dry_run_by_default_and_write_is_idempotent(self):
        import yaml

        from config_loader import load_merged_config
        from scripts.migrate_runtime_config import migrate_runtime_config

        with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as directory:
            root = Path(directory)
            source = root / "config.yaml"
            defaults_path = root / "config.defaults.yaml"
            runtime_path = root / "data" / "runtime_config.yaml"
            source.write_text(
                yaml.safe_dump(LEGACY_FIXTURE, allow_unicode=True, sort_keys=False),
                encoding="utf-8",
            )

            dry_run = migrate_runtime_config(
                source,
                defaults_path=defaults_path,
                runtime_path=runtime_path,
                write=False,
            )
            self.assertEqual(dry_run["status"], "dry-run")
            self.assertFalse(defaults_path.exists())
            self.assertFalse(runtime_path.exists())

            first = migrate_runtime_config(
                source,
                defaults_path=defaults_path,
                runtime_path=runtime_path,
                write=True,
            )
            before = (defaults_path.read_bytes(), runtime_path.read_bytes())
            second = migrate_runtime_config(
                source,
                defaults_path=defaults_path,
                runtime_path=runtime_path,
                write=True,
            )

            self.assertEqual(first["merged_equal"], True)
            self.assertEqual(second["status"], "unchanged")
            self.assertEqual(
                before, (defaults_path.read_bytes(), runtime_path.read_bytes())
            )
            self.assertEqual(
                load_merged_config(defaults_path, runtime_path), LEGACY_FIXTURE
            )
            self.assertTrue(Path(first["backup_path"]).is_file())

    def test_policy_only_source_never_overwrites_existing_runtime(self):
        import yaml

        from config_loader import split_legacy_config
        from scripts.migrate_runtime_config import migrate_runtime_config

        with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as directory:
            root = Path(directory)
            source = root / "config.yaml"
            defaults_path = root / "config.defaults.yaml"
            runtime_path = root / "data" / "runtime_config.yaml"
            defaults, runtime = split_legacy_config(LEGACY_FIXTURE)
            source.write_text(
                yaml.safe_dump(defaults, allow_unicode=True, sort_keys=False),
                encoding="utf-8",
            )
            defaults_path.write_text(
                yaml.safe_dump(defaults, allow_unicode=True, sort_keys=False),
                encoding="utf-8",
            )
            runtime_path.parent.mkdir()
            runtime_path.write_text(
                yaml.safe_dump(runtime, allow_unicode=True, sort_keys=False),
                encoding="utf-8",
            )
            before = runtime_path.read_bytes()

            result = migrate_runtime_config(
                source,
                defaults_path=defaults_path,
                runtime_path=runtime_path,
                write=True,
            )

            self.assertEqual(result["status"], "already-migrated")
            self.assertEqual(runtime_path.read_bytes(), before)


class RuntimeConfigBackupContractTest(unittest.TestCase):
    def test_runtime_config_is_required_and_archived_under_state(self):
        from runtime_backup import RUNTIME_BACKUP_SPEC, _archive_path_for

        self.assertIn("runtime_config.yaml", RUNTIME_BACKUP_SPEC["required_core"])
        self.assertEqual(
            _archive_path_for("runtime_config.yaml", "required_core"),
            "state/runtime_config.yaml",
        )

    def test_production_restore_maps_runtime_config_back_to_data_root(self):
        from runtime_restore import _production_mappings

        with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as directory:
            root = Path(directory)
            restored = root / "restored"
            state = restored / "state"
            state.mkdir(parents=True)
            (state / "runtime_config.yaml").write_text("fixture: true\n", encoding="utf-8")
            mappings = _production_mappings(
                restored,
                {
                    "files": [
                        {
                            "present": True,
                            "source_rel": "runtime_config.yaml",
                            "path": "state/runtime_config.yaml",
                        }
                    ]
                },
                root / "production-data",
            )

        self.assertEqual(
            mappings,
            [
                (
                    restored / "state" / "runtime_config.yaml",
                    root / "production-data" / "runtime_config.yaml",
                )
            ],
        )


if __name__ == "__main__":
    unittest.main()
