import io
import tempfile
import unittest
from contextlib import redirect_stdout
from datetime import date, datetime
from pathlib import Path
from unittest.mock import patch


class ResearchBasketSwitchTest(unittest.TestCase):
    def test_legacy_true_switch_migrates_to_enabled_cohort_v2(self):
        from collection_plan import load_collection_settings

        with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as tmp:
            config_path = Path(tmp) / "config.yaml"
            config_path.write_text("RESEARCH_COHORT_V2: true\n", encoding="utf-8")
            output = io.StringIO()
            with redirect_stdout(output):
                settings = load_collection_settings(config_path)

        self.assertTrue(settings["research_basket_enabled"])
        self.assertEqual(settings["research_basket_strategy"], "cohort_v2")
        self.assertTrue(settings["research_basket_migrated_from_legacy"])
        self.assertIn("[配置迁移] RESEARCH_COHORT_V2=true", output.getvalue())

    def test_explicit_switch_does_not_depend_on_legacy_key(self):
        from collection_plan import load_collection_settings

        with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as tmp:
            config_path = Path(tmp) / "config.yaml"
            config_path.write_text(
                "RESEARCH_BASKET_ENABLED: true\n"
                "RESEARCH_BASKET_STRATEGY: legacy\n"
                "RESEARCH_COHORT_V2: false\n",
                encoding="utf-8",
            )
            settings = load_collection_settings(config_path)

        self.assertTrue(settings["research_basket_enabled"])
        self.assertEqual(settings["research_basket_strategy"], "legacy")
        self.assertFalse(settings["research_basket_migrated_from_legacy"])

    def test_unknown_strategy_is_rejected(self):
        from collection_plan import load_collection_settings

        with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as tmp:
            config_path = Path(tmp) / "config.yaml"
            config_path.write_text(
                "RESEARCH_BASKET_ENABLED: true\n"
                "RESEARCH_BASKET_STRATEGY: surprise\n",
                encoding="utf-8",
            )
            with self.assertRaisesRegex(ValueError, "RESEARCH_BASKET_STRATEGY"):
                load_collection_settings(config_path)

    def test_disabled_basket_returns_before_any_collection_side_effect(self):
        from basket_collect import run_basket

        settings = {
            "research_basket_enabled": False,
            "research_basket_strategy": "cohort_v2",
        }
        with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as tmp:
            root = Path(tmp)
            usage_path = root / "api_usage.json"
            usage_path.write_text(
                '{"version":2,"dates":{},"entries":[]}',
                encoding="utf-8",
            )
            before_usage = usage_path.read_bytes()
            with (
                patch("basket_collect.load_collection_settings", return_value=settings),
                patch(
                    "basket_collect.acquire_collection_singleflight",
                    side_effect=AssertionError("disabled basket acquired single-flight"),
                ) as acquire_lock,
                patch("basket_collect.build_collection_plan") as build_plan,
                patch("basket_collect.start_request_cache_round") as start_cache,
                patch("basket_collect.set_current_round") as set_round,
                patch("basket_collect.start_round_log_archive") as start_archive,
                redirect_stdout(io.StringIO()),
            ):
                summary = run_basket(
                    today=date(2026, 8, 27),
                    now=datetime(2026, 8, 27, 17, 35, 0),
                    state_path=root / "basket_state.json",
                    db_path=root / "observations.sqlite3",
                    usage_path=usage_path,
                    singleflight_lock_path=root / "collection.lock",
                )

            self.assertEqual(usage_path.read_bytes(), before_usage)
            self.assertFalse((root / "basket_state.json").exists())
            self.assertFalse((root / "observations.sqlite3").exists())

        self.assertEqual(
            summary,
            {
                "status": "disabled",
                "reason": "research_basket_disabled",
                "actual_requests": 0,
                "round_id": "basket_20260827T173500",
                "queues": 0,
                "success": 0,
                "failed": 0,
                "written": 0,
                "skipped": True,
            },
        )
        acquire_lock.assert_not_called()
        build_plan.assert_not_called()
        start_cache.assert_not_called()
        set_round.assert_not_called()
        start_archive.assert_not_called()


if __name__ == "__main__":
    unittest.main()
