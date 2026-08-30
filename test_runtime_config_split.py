import ast
import inspect
import subprocess
import sys
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

    def test_tracked_config_contract_has_no_deprecated_root_copy_or_runtime_facts(self):
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
            tracked,
            ["config.defaults.yaml", "config.example.yaml"],
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
        self.assertFalse((root / "config.yaml").exists())

    def test_production_python_does_not_reference_deprecated_root_config(self):
        root = Path(__file__).resolve().parent
        tracked_python = subprocess.run(
            ["git", "ls-files", "*.py"],
            cwd=root,
            check=True,
            capture_output=True,
            text=True,
            encoding="utf-8",
        ).stdout.splitlines()
        findings = []
        for relative in tracked_python:
            path = Path(relative)
            if path.parts[0] == "scripts" or path.name.startswith("test_"):
                continue
            tree = ast.parse((root / path).read_text(encoding="utf-8-sig"))
            if any(
                isinstance(node, ast.Constant) and node.value == "config.yaml"
                for node in ast.walk(tree)
            ):
                findings.append(relative)
        self.assertEqual(findings, [])

    def test_default_loader_paths_are_the_two_formal_sources(self):
        from config_loader import (
            DEFAULT_CONFIG_PATH,
            PROJECT_ROOT,
            RUNTIME_CONFIG_PATH,
            load_merged_config,
        )

        self.assertEqual(DEFAULT_CONFIG_PATH, PROJECT_ROOT / "config.defaults.yaml")
        self.assertEqual(
            RUNTIME_CONFIG_PATH,
            PROJECT_ROOT / "data" / "runtime_config.yaml",
        )
        parameters = inspect.signature(load_merged_config).parameters
        self.assertEqual(parameters["defaults_path"].default, DEFAULT_CONFIG_PATH)
        self.assertEqual(parameters["runtime_path"].default, RUNTIME_CONFIG_PATH)

    def test_current_docs_name_formal_sources_and_explicit_legacy_source(self):
        root = Path(__file__).resolve().parent
        current_docs = (
            root / "README.md",
            root / "CONTRIBUTING.md",
            root / "docs" / "runtime-config-separation-2026-08-27.md",
        )
        retired_copy_phrase = "\u653f\u7b56\u517c\u5bb9\u526f\u672c"
        for path in current_docs:
            text = path.read_text(encoding="utf-8")
            with self.subTest(path=path.relative_to(root)):
                self.assertIn("config.defaults.yaml", text)
                self.assertIn("data/runtime_config.yaml", text)
                self.assertNotIn(retired_copy_phrase, text)

        explicit_command = (
            "python -X utf8 scripts/migrate_runtime_config.py "
            "--source <path-to-legacy-config>"
        )
        for relative in (
            "README.md",
            "CONTRIBUTING.md",
            "docs/runtime-config-separation-2026-08-27.md",
        ):
            text = (root / relative).read_text(encoding="utf-8")
            self.assertIn(explicit_command, text, relative)

        historical = (
            root
            / "docs"
            / "superpowers"
            / "plans"
            / "2026-08-26-tcurve-maturity-acceleration.md"
        ).read_text(encoding="utf-8")
        self.assertIn("config.yaml", historical)


class RuntimeConfigMigrationTest(unittest.TestCase):
    def test_parser_requires_explicit_legacy_source(self):
        from scripts.migrate_runtime_config import build_parser

        action = next(
            item for item in build_parser()._actions if item.dest == "source"
        )
        self.assertTrue(action.required)
        self.assertIsNone(action.default)
        self.assertEqual(action.metavar, "LEGACY_CONFIG")

    def test_cli_without_source_is_argparse_error_and_has_no_file_side_effects(self):
        root = Path(__file__).resolve().parent
        script = root / "scripts" / "migrate_runtime_config.py"
        with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as directory:
            temporary = Path(directory)
            defaults_path = temporary / "config.defaults.yaml"
            runtime_path = temporary / "data" / "runtime_config.yaml"
            before = sorted(path.relative_to(temporary) for path in temporary.rglob("*"))

            result = subprocess.run(
                [
                    sys.executable,
                    "-X",
                    "utf8",
                    str(script),
                    "--defaults-output",
                    str(defaults_path),
                    "--runtime-output",
                    str(runtime_path),
                ],
                cwd=temporary,
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                check=False,
            )
            after = sorted(path.relative_to(temporary) for path in temporary.rglob("*"))

        self.assertEqual(result.returncode, 2, result.stderr)
        self.assertIn("usage:", result.stderr.lower())
        self.assertIn("--source", result.stderr)
        self.assertEqual(after, before)

    def test_cli_explicit_source_preserves_dry_run_write_backup_and_merge(self):
        import yaml

        from config_loader import load_merged_config

        root = Path(__file__).resolve().parent
        script = root / "scripts" / "migrate_runtime_config.py"
        with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as directory:
            temporary = Path(directory)
            source = temporary / "legacy-config.yaml"
            defaults_path = temporary / "config.defaults.yaml"
            runtime_path = temporary / "data" / "runtime_config.yaml"
            source.write_text(
                yaml.safe_dump(LEGACY_FIXTURE, allow_unicode=True, sort_keys=False),
                encoding="utf-8",
            )
            source_bytes = source.read_bytes()
            command = [
                sys.executable,
                "-X",
                "utf8",
                str(script),
                "--source",
                str(source),
                "--defaults-output",
                str(defaults_path),
                "--runtime-output",
                str(runtime_path),
            ]

            dry_run = subprocess.run(
                command,
                cwd=temporary,
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                check=False,
            )
            self.assertEqual(dry_run.returncode, 0, dry_run.stderr)
            self.assertIn("mode=dry-run status=dry-run", dry_run.stdout)
            self.assertFalse(defaults_path.exists())
            self.assertFalse(runtime_path.exists())

            written = subprocess.run(
                [*command, "--write"],
                cwd=temporary,
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                check=False,
            )
            backups = list(temporary.glob("legacy-config.yaml.pre-runtime-split-*.bak"))

            self.assertEqual(written.returncode, 0, written.stderr)
            self.assertIn("mode=write status=written", written.stdout)
            self.assertEqual(load_merged_config(defaults_path, runtime_path), LEGACY_FIXTURE)
            self.assertEqual(len(backups), 1)
            self.assertEqual(backups[0].read_bytes(), source_bytes)

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
