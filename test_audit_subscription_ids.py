import hashlib
import io
import json
import tempfile
import unittest
from contextlib import redirect_stdout
from pathlib import Path


class SubscriptionIdAuditTest(unittest.TestCase):
    def setUp(self):
        try:
            from scripts import audit_subscription_ids
        except ImportError as exc:
            self.fail(f"审计模块尚未实现: {exc}")
        self.audit_module = audit_subscription_ids
        self.tmpdir = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmpdir.cleanup)
        self.path = Path(self.tmpdir.name) / "subscriptions.json"

    def _write(self, records) -> None:
        self.path.write_text(
            json.dumps(records, ensure_ascii=False),
            encoding="utf-8",
        )

    def _audit(self):
        return self.audit_module.audit_subscription_ids(self.path)

    def test_unique_ids_report_no_duplicate_groups(self):
        self._write(
            [
                {"subscription_id": "123e4567-e89b-12d3-a456-426614174001"},
                {"subscription_id": "123e4567-e89b-12d3-a456-426614174002"},
            ]
        )

        report = self._audit()

        self.assertEqual(report.total_records, 2)
        self.assertEqual(report.records_with_id, 2)
        self.assertEqual(report.records_missing_id, 0)
        self.assertEqual(report.duplicate_groups, ())

    def test_single_duplicate_group_reports_array_positions(self):
        duplicate_id = "123e4567-e89b-12d3-a456-426614174010"
        self._write(
            [
                {"subscription_id": duplicate_id},
                {"subscription_id": "123e4567-e89b-12d3-a456-426614174011"},
                {"subscription_id": duplicate_id},
            ]
        )

        report = self._audit()

        self.assertEqual(len(report.duplicate_groups), 1)
        self.assertEqual(report.duplicate_groups[0].indexes, (0, 2))

    def test_multiple_duplicate_groups_keep_input_position_order(self):
        first = "123e4567-e89b-12d3-a456-426614174020"
        second = "123e4567-e89b-12d3-a456-426614174021"
        self._write(
            [
                {"subscription_id": first},
                {"subscription_id": second},
                {"subscription_id": first},
                {"subscription_id": second},
                {"subscription_id": second},
            ]
        )

        report = self._audit()

        self.assertEqual(
            [group.indexes for group in report.duplicate_groups],
            [(0, 2), (1, 3, 4)],
        )

    def test_missing_ids_and_non_dict_rows_are_counted_as_missing(self):
        self._write(
            [
                {"subscription_id": "123e4567-e89b-12d3-a456-426614174030"},
                {},
                {"subscription_id": ""},
                {"subscription_id": None},
                "dirty-row",
            ]
        )

        report = self._audit()

        self.assertEqual(report.total_records, 5)
        self.assertEqual(report.records_with_id, 1)
        self.assertEqual(report.records_missing_id, 4)
        self.assertEqual(report.duplicate_groups, ())

    def test_success_output_is_pii_safe_and_read_only(self):
        duplicate_id = "123e4567-e89b-12d3-a456-426614174040"
        secret_values = (
            "person@example.com",
            "PVG->KIX",
            "2026-10-01",
            "push-token-secret",
        )
        self._write(
            [
                {
                    "subscription_id": duplicate_id,
                    "email": secret_values[0],
                    "route": secret_values[1],
                    "depart_date": secret_values[2],
                    "token": secret_values[3],
                },
                {"subscription_id": duplicate_id},
            ]
        )
        before = self.path.read_bytes()
        before_hash = hashlib.sha256(before).hexdigest()
        output = io.StringIO()

        with redirect_stdout(output):
            exit_code = self.audit_module.main(["--path", str(self.path)])

        after = self.path.read_bytes()
        rendered = output.getvalue()
        self.assertEqual(exit_code, 0)
        self.assertEqual(after, before)
        self.assertEqual(hashlib.sha256(after).hexdigest(), before_hash)
        self.assertIn("记录总数: 2", rendered)
        self.assertIn("有ID记录数: 2", rendered)
        self.assertIn("缺失ID记录数: 0", rendered)
        self.assertIn("重复ID组数: 1", rendered)
        self.assertIn("位置=[0, 1]", rendered)
        self.assertIn("123e4567-****", rendered)
        self.assertNotIn(duplicate_id, rendered)
        for secret in secret_values:
            self.assertNotIn(secret, rendered)

    def test_neighboring_backup_and_lock_files_are_not_scanned(self):
        self._write(
            [{"subscription_id": "123e4567-e89b-12d3-a456-426614174050"}]
        )
        duplicate_decoy = [
            {"subscription_id": "123e4567-e89b-12d3-a456-426614174051"},
            {"subscription_id": "123e4567-e89b-12d3-a456-426614174051"},
        ]
        self.path.with_name("subscriptions.json.bak.20260829").write_text(
            json.dumps(duplicate_decoy),
            encoding="utf-8",
        )
        self.path.with_name("subscriptions.json.lock").write_text(
            json.dumps(duplicate_decoy),
            encoding="utf-8",
        )

        report = self._audit()

        self.assertEqual(report.total_records, 1)
        self.assertEqual(report.duplicate_groups, ())


if __name__ == "__main__":
    unittest.main()
