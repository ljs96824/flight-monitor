import io
import json
import tempfile
import unittest
from datetime import datetime
from pathlib import Path
from unittest.mock import patch

import sync_subscriptions as sync_module
import web_form


def _subscription(*, created_at: str, budget: int = 8000, subscription_id: str = "sub-1") -> dict:
    return {
        "id": subscription_id,
        "origin": "上海",
        "destination": "大阪",
        "depart_date": "2026-10-01",
        "return_date": "2026-10-06",
        "round_trip": True,
        "status": "active",
        "created_at": created_at,
        "budget_scope": "per_person",
        "max_budget_scope": "per_person",
        "target_price_scope": "per_person",
        "lcc_policy": "any",
        "hard_constraints": {
            "max_budget": budget,
            "budget_scope": "per_person",
            "max_budget_scope": "per_person",
            "target_price_scope": "per_person",
            "lcc_policy": "any",
        },
    }


class SubscriptionCloneIdempotencyTest(unittest.TestCase):
    def setUp(self):
        web_form.app.config.update(TESTING=True)

    def test_edit_post_keeps_count_and_original_identity(self):
        original = _subscription(created_at="2026-05-27T06:33:38", budget=8000)
        rebuilt = _subscription(created_at="2026-08-17T22:00:00", budget=9000, subscription_id="")
        rebuilt.pop("id")

        with tempfile.TemporaryDirectory() as tmpdir:
            path = Path(tmpdir) / "subscriptions.json"
            path.write_text(json.dumps([original], ensure_ascii=False), encoding="utf-8")
            with (
                patch.object(web_form, "SUBSCRIPTIONS_PATH", path),
                patch.object(web_form, "build_subscription", return_value=rebuilt),
                patch.object(web_form, "start_background_collection"),
            ):
                response = web_form.app.test_client().post(
                    "/subscribe",
                    data={"subscription_index": "0", "form_page": "full"},
                )

            saved = json.loads(path.read_text(encoding="utf-8"))

        self.assertEqual(response.status_code, 302)
        self.assertEqual(len(saved), 1)
        self.assertEqual(saved[0]["id"], "sub-1")
        self.assertEqual(saved[0]["created_at"], "2026-05-27T06:33:38")
        self.assertEqual(saved[0]["hard_constraints"]["max_budget"], 9000)

    def test_repeated_remote_sync_updates_in_place_without_growing_count(self):
        local = _subscription(created_at="2026-05-27T06:33:38", budget=8000)
        remote = _subscription(created_at="2026-05-27T06:33:38", budget=9000)

        with tempfile.TemporaryDirectory() as tmpdir:
            data_dir = Path(tmpdir)
            path = data_dir / "subscriptions.json"
            path.write_text(json.dumps([local], ensure_ascii=False), encoding="utf-8")
            with (
                patch.object(sync_module, "DATA_DIR", data_dir),
                patch.object(sync_module, "LOCAL_SUBSCRIPTIONS", path),
                patch.object(sync_module, "download_remote_subscriptions", return_value=[remote]),
            ):
                first = sync_module.sync_subscriptions()
                second = sync_module.sync_subscriptions()

            saved = json.loads(path.read_text(encoding="utf-8"))

        self.assertEqual(first["total"], 1)
        self.assertEqual(second["total"], 1)
        self.assertEqual(first["added"], 0)
        self.assertEqual(second["added"], 0)
        self.assertEqual(len(saved), 1)
        self.assertEqual(saved[0]["hard_constraints"]["max_budget"], 9000)

    def test_remote_sync_without_id_uses_preserved_created_at_identity(self):
        local = _subscription(created_at="2026-05-27T06:33:38", budget=8000)
        remote = _subscription(created_at="2026-05-27T06:33:38", budget=9000)
        local.pop("id")
        remote.pop("id")

        merged, added = sync_module.merge_subscriptions([local], [remote])

        self.assertEqual(added, 0)
        self.assertEqual(len(merged), 1)
        self.assertEqual(merged[0]["created_at"], "2026-05-27T06:33:38")
        self.assertEqual(merged[0]["hard_constraints"]["max_budget"], 9000)


class DedupeSubscriptionsTest(unittest.TestCase):
    def _revisions(self) -> list[dict]:
        first = _subscription(created_at="2026-08-13T10:00:00", budget=8000, subscription_id="")
        second = _subscription(created_at="2026-08-15T10:00:00", budget=9000, subscription_id="")
        distinct = _subscription(created_at="2026-08-16T10:00:00", budget=7000, subscription_id="")
        for item in (first, second, distinct):
            item.pop("id")
        distinct["return_date"] = "2026-10-07"
        return [first, second, distinct]

    def test_route_identity_groups_revisions_and_keeps_latest(self):
        from scripts.dedupe_subscriptions import deduplicate_subscriptions

        cleaned, clusters = deduplicate_subscriptions(self._revisions())

        self.assertEqual(len(clusters), 1)
        self.assertEqual(clusters[0]["indices"], [0, 1])
        self.assertEqual(clusters[0]["keep_index"], 1)
        self.assertEqual(len(cleaned), 2)
        self.assertEqual(cleaned[0]["hard_constraints"]["max_budget"], 9000)
        self.assertEqual(cleaned[1]["return_date"], "2026-10-07")

    def test_dry_run_is_read_only(self):
        from scripts.dedupe_subscriptions import run

        with tempfile.TemporaryDirectory() as tmpdir:
            path = Path(tmpdir) / "subscriptions.json"
            original = json.dumps(self._revisions(), ensure_ascii=False, indent=2).encode("utf-8")
            path.write_bytes(original)

            result = run(path, execute=False, stream=io.StringIO())

            self.assertEqual(path.read_bytes(), original)
            self.assertEqual(result["before"], 3)
            self.assertEqual(result["after"], 2)
            self.assertIsNone(result["backup_path"])

    def test_execute_backs_up_then_removes_revisions_and_second_run_is_stable(self):
        from scripts.dedupe_subscriptions import run

        with tempfile.TemporaryDirectory() as tmpdir:
            path = Path(tmpdir) / "subscriptions.json"
            original = json.dumps(self._revisions(), ensure_ascii=False, indent=2).encode("utf-8")
            path.write_bytes(original)
            fixed_now = datetime(2026, 8, 17, 23, 0, 0)

            first = run(path, execute=True, now=fixed_now, stream=io.StringIO())
            backup = Path(first["backup_path"])
            second_bytes = path.read_bytes()
            second = run(path, execute=True, now=fixed_now, stream=io.StringIO())

            self.assertTrue(backup.exists())
            self.assertEqual(backup.read_bytes(), original)
            self.assertEqual(len(json.loads(second_bytes.decode("utf-8"))), 2)
            self.assertEqual(first["removed"], 1)
            self.assertEqual(second["removed"], 0)
            self.assertEqual(path.read_bytes(), second_bytes)
            self.assertIsNone(second["backup_path"])


if __name__ == "__main__":
    unittest.main()
