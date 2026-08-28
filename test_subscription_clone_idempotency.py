import io
import json
import tempfile
import unittest
from datetime import datetime
from pathlib import Path
from unittest.mock import patch
from uuid import UUID

import sync_subscriptions as sync_module
import web_form
from web_test_utils import enable_csrf


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
        self.client = web_form.app.test_client()
        enable_csrf(self.client)

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
                response = self.client.post(
                    "/subscribe",
                    data={"subscription_index": "0", "form_page": "full"},
                )

            saved = json.loads(path.read_text(encoding="utf-8"))

        self.assertEqual(response.status_code, 302)
        self.assertEqual(len(saved), 1)
        self.assertEqual(saved[0]["id"], "sub-1")
        self.assertEqual(saved[0]["created_at"], "2026-05-27T06:33:38")
        self.assertEqual(saved[0]["hard_constraints"]["max_budget"], 9000)

    def test_repeated_remote_sync_skips_existing_identity_without_overwrite(self):
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
        self.assertEqual(first["skipped_identity"], 1)
        self.assertEqual(second["skipped_identity"], 1)
        self.assertEqual(len(saved), 1)
        self.assertEqual(saved[0]["hard_constraints"]["max_budget"], 8000)

    def test_remote_sync_without_id_uses_preserved_created_at_identity(self):
        local = _subscription(created_at="2026-05-27T06:33:38", budget=8000)
        remote = _subscription(created_at="2026-05-27T06:33:38", budget=9000)
        local.pop("id")
        remote.pop("id")

        merged, added = sync_module.merge_subscriptions([local], [remote])

        self.assertEqual(added, 0)
        self.assertEqual(len(merged), 1)
        self.assertEqual(merged[0]["created_at"], "2026-05-27T06:33:38")
        self.assertEqual(merged[0]["hard_constraints"]["max_budget"], 8000)

    def test_remote_route_match_with_different_identity_is_skipped(self):
        local = _subscription(
            created_at="2026-08-15T17:07:11",
            budget=8000,
            subscription_id="local-canonical",
        )
        remote = _subscription(
            created_at="2026-05-31T08:15:06",
            budget=9000,
            subscription_id="remote-fossil",
        )

        merged, added = sync_module.merge_subscriptions([local], [remote])

        self.assertEqual(added, 0)
        self.assertEqual(len(merged), 1)
        self.assertEqual(merged[0]["id"], "local-canonical")
        self.assertEqual(merged[0]["hard_constraints"]["max_budget"], 8000)

    def test_true_new_remote_is_appended_with_subscription_id(self):
        local = _subscription(created_at="2026-08-15T17:07:11")
        remote = _subscription(
            created_at="2026-08-17T12:00:00",
            subscription_id="",
        )
        remote.pop("id")
        remote["destination"] = "香港"
        remote["depart_date"] = "2026-11-01"
        remote["return_date"] = "2026-11-05"

        merged, added = sync_module.merge_subscriptions([local], [remote])

        self.assertEqual(added, 1)
        self.assertEqual(len(merged), 2)
        UUID(merged[1]["subscription_id"])

    def test_subscription_identity_prefers_canonical_subscription_id(self):
        subscription = _subscription(created_at="2026-08-15T17:07:11")
        subscription["subscription_id"] = "canonical-id"
        subscription["id"] = "legacy-id"

        self.assertEqual(sync_module._subscription_key(subscription), "id:canonical-id")

    def test_new_save_generates_subscription_id(self):
        from subscription_repository import LOCAL_OWNER_ID, SubscriptionRepository

        subscription = _subscription(created_at="2026-08-17T12:00:00", subscription_id="")
        subscription.pop("id")

        with tempfile.TemporaryDirectory() as tmpdir:
            path = Path(tmpdir) / "subscriptions.json"
            saved_subscription = SubscriptionRepository(path).create(
                LOCAL_OWNER_ID,
                subscription,
            )
            saved = json.loads(path.read_text(encoding="utf-8"))

        self.assertEqual(len(saved), 1)
        self.assertEqual(saved_subscription, saved[0])
        UUID(saved[0]["subscription_id"])


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


class SubscriptionIdentityMigrationTest(unittest.TestCase):
    def _without_ids(self) -> list[dict]:
        subscriptions = []
        for index in range(3):
            item = _subscription(
                created_at=f"2026-08-{index + 1:02d}T12:00:00",
                subscription_id="",
            )
            item.pop("id")
            item["depart_date"] = f"2026-10-{index + 1:02d}"
            subscriptions.append(item)
        return subscriptions

    def test_identity_migration_dry_run_is_read_only(self):
        from scripts.migrate_subscription_ids import run

        with tempfile.TemporaryDirectory() as tmpdir:
            path = Path(tmpdir) / "subscriptions.json"
            original = json.dumps(
                self._without_ids(), ensure_ascii=False, indent=2
            ).encode("utf-8")
            path.write_bytes(original)

            result = run(path, execute=False, stream=io.StringIO())

            self.assertEqual(path.read_bytes(), original)
            self.assertEqual(result["before"], 3)
            self.assertEqual(result["migrated"], 3)
            self.assertIsNone(result["backup_path"])

    def test_identity_migration_execute_backs_up_and_assigns_unique_uuids(self):
        from scripts.migrate_subscription_ids import run

        with tempfile.TemporaryDirectory() as tmpdir:
            path = Path(tmpdir) / "subscriptions.json"
            original = json.dumps(
                self._without_ids(), ensure_ascii=False, indent=2
            ).encode("utf-8")
            path.write_bytes(original)

            first = run(
                path,
                execute=True,
                now=datetime(2026, 8, 17, 23, 45, 0),
                stream=io.StringIO(),
            )
            migrated_bytes = path.read_bytes()
            migrated = json.loads(migrated_bytes.decode("utf-8"))
            second = run(
                path,
                execute=True,
                now=datetime(2026, 8, 17, 23, 46, 0),
                stream=io.StringIO(),
            )

            ids = [item["subscription_id"] for item in migrated]
            self.assertEqual(len(set(ids)), 3)
            for value in ids:
                UUID(value)
            backup = Path(first["backup_path"])
            self.assertEqual(backup.read_bytes(), original)
            self.assertEqual(first["migrated"], 3)
            self.assertEqual(second["migrated"], 0)
            self.assertIsNone(second["backup_path"])
            self.assertEqual(path.read_bytes(), migrated_bytes)


if __name__ == "__main__":
    unittest.main()
