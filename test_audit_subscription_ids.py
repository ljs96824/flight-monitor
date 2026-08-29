import hashlib
import io
import json
import tempfile
import unittest
from contextlib import redirect_stderr
from pathlib import Path

from scripts.audit_subscription_ids import main, run


SUB_A = "123e4567-e89b-12d3-a456-426614174101"
SUB_B = "223e4567-e89b-12d3-a456-426614174102"
SUB_C = "323e4567-e89b-12d3-a456-426614174103"


def _record(subscription_id: str | None, *, marker: str) -> dict:
    record = {
        "email": f"{marker}@example.com",
        "origin": "PVG",
        "destination": "KIX",
        "depart_date": "2026-10-01",
        "notification_token": f"secret-{marker}",
        "subscription_body": f"private-body-{marker}",
    }
    if subscription_id is not None:
        record["subscription_id"] = subscription_id
    return record


class SubscriptionIdAuditTest(unittest.TestCase):
    def _run_case(
        self,
        payload: list,
        *,
        use_directory: bool = False,
        add_ignored_files: bool = False,
    ) -> tuple[dict, str]:
        with tempfile.TemporaryDirectory() as directory:
            data_dir = Path(directory) / "data"
            data_dir.mkdir()
            path = data_dir / "subscriptions.json"
            path.write_text(
                json.dumps(payload, ensure_ascii=False, indent=2),
                encoding="utf-8",
            )
            if add_ignored_files:
                ignored_payload = [
                    _record(SUB_C, marker="backup-a"),
                    _record(SUB_C, marker="backup-b"),
                ]
                path.with_name(
                    "subscriptions.json.bak.identity.20260828"
                ).write_text(
                    json.dumps(ignored_payload, ensure_ascii=False),
                    encoding="utf-8",
                )
                path.with_name("subscriptions.json.lock").write_text(
                    json.dumps(ignored_payload, ensure_ascii=False),
                    encoding="utf-8",
                )

            before = hashlib.sha256(path.read_bytes()).hexdigest()
            output = io.StringIO()
            report = run(data_dir if use_directory else path, stream=output)
            after = hashlib.sha256(path.read_bytes()).hexdigest()

        self.assertEqual(after, before)
        return report, output.getvalue()

    def test_no_duplicates_and_backup_or_lock_files_are_excluded(self):
        report, output = self._run_case(
            [_record(SUB_A, marker="a"), _record(SUB_B, marker="b")],
            use_directory=True,
            add_ignored_files=True,
        )

        self.assertEqual(report["total_records"], 2)
        self.assertEqual(report["records_with_id"], 2)
        self.assertEqual(report["missing_id_records"], 0)
        self.assertEqual(report["duplicate_group_count"], 0)
        self.assertNotIn(SUB_C[:8], output)

    def test_single_duplicate_group_reports_mask_and_array_indexes(self):
        report, output = self._run_case(
            [
                _record(SUB_A, marker="first"),
                _record(SUB_B, marker="middle"),
                _record(SUB_A, marker="last"),
            ]
        )

        self.assertEqual(report["duplicate_group_count"], 1)
        self.assertEqual(report["duplicate_groups"][0]["indexes"], [0, 2])
        self.assertIn("标识=123e4567********", output)
        self.assertIn("索引=[0, 2]", output)
        self.assertNotIn(SUB_A, output)

    def test_multiple_duplicate_groups_are_reported_in_first_seen_order(self):
        report, output = self._run_case(
            [
                _record(SUB_A, marker="a0"),
                _record(SUB_B, marker="b1"),
                _record(SUB_A, marker="a2"),
                _record(SUB_B, marker="b3"),
                _record(SUB_C, marker="unique"),
                _record(SUB_B, marker="b5"),
            ]
        )

        self.assertEqual(report["duplicate_group_count"], 2)
        self.assertEqual(
            [group["indexes"] for group in report["duplicate_groups"]],
            [[0, 2], [1, 3, 5]],
        )
        self.assertLess(output.index("索引=[0, 2]"), output.index("索引=[1, 3, 5]"))

    def test_missing_id_mix_counts_empty_whitespace_absent_and_legacy_only(self):
        legacy_only = _record(None, marker="legacy")
        legacy_only["id"] = SUB_B
        report, _output = self._run_case(
            [
                _record(SUB_A, marker="valid"),
                _record("", marker="empty"),
                _record("   ", marker="whitespace"),
                _record(None, marker="absent"),
                legacy_only,
                {"subscription_id": {"email": "private@example.com"}},
            ]
        )

        self.assertEqual(report["total_records"], 6)
        self.assertEqual(report["records_with_id"], 1)
        self.assertEqual(report["missing_id_records"], 5)
        self.assertEqual(report["duplicate_group_count"], 0)

    def test_non_dict_dirty_rows_count_as_missing_without_echoing_them(self):
        report, output = self._run_case(
            [
                _record(SUB_A, marker="valid-a"),
                None,
                "private-dirty-text",
                42,
                ["private", "dirty", "list"],
                _record(SUB_B, marker="valid-b"),
            ]
        )

        self.assertEqual(report["total_records"], 6)
        self.assertEqual(report["records_with_id"], 2)
        self.assertEqual(report["missing_id_records"], 4)
        self.assertNotIn("private-dirty-text", output)
        self.assertNotIn("private", output)

    def test_output_contains_no_pii_tokens_bodies_or_full_ids(self):
        payload = [
            _record(SUB_A, marker="duplicate-owner"),
            _record(SUB_A, marker="duplicate-owner-2"),
        ]
        _report, output = self._run_case(payload)

        forbidden = (
            "duplicate-owner@example.com",
            "PVG",
            "KIX",
            "2026-10-01",
            "secret-duplicate-owner",
            "private-body-duplicate-owner",
            SUB_A,
        )
        for value in forbidden:
            with self.subTest(value=value):
                self.assertNotIn(value, output)

    def test_non_uuid_duplicate_id_uses_hash_without_echoing_pii(self):
        private_id = "person@example.com"
        report, output = self._run_case(
            [_record(private_id, marker="one"), _record(private_id, marker="two")]
        )

        self.assertEqual(report["duplicate_group_count"], 1)
        self.assertIn("标识=sha256:", output)
        self.assertNotIn(private_id, output)
        self.assertNotIn(private_id[:8], output)

    def test_cli_exposes_no_mutation_mode(self):
        for option in ("--execute", "--write"):
            with self.subTest(option=option):
                errors = io.StringIO()
                with redirect_stderr(errors), self.assertRaises(SystemExit) as raised:
                    main([option])

                self.assertEqual(raised.exception.code, 2)
                self.assertIn(f"unrecognized arguments: {option}", errors.getvalue())


if __name__ == "__main__":
    unittest.main()
