from __future__ import annotations

import subprocess
import unittest
from pathlib import Path


PROJECT_ROOT = Path(__file__).resolve().parent
ONE_OFF_ROOT_ARTIFACTS = (
    "before.json",
    "after.json",
    "check.json",
    "snapshot_run_test.json",
    "copy_provenance_shanghai_osaka_diff.json",
    "shanghai_osaka_rounding_cards_diff.json",
    "before_copy_provenance_shanghai_osaka_email.html",
    "after_copy_provenance_shanghai_osaka_email.html",
    "before_shanghai_osaka_rounding_cards_email.html",
    "after_shanghai_osaka_rounding_cards_email.html",
    "after_shanghai_osaka_rounding_cards_diagnostics.log",
)


class SnapshotArtifactHygieneTest(unittest.TestCase):
    def test_default_snapshot_output_uses_ignored_data_directory(self):
        from scripts.snapshot_run import resolve_snapshot_output_path

        root = Path("repo-root")
        self.assertEqual(
            resolve_snapshot_output_path(None, project_root=root),
            root / "data" / "snapshots" / "snapshot.json",
        )
        self.assertEqual(
            resolve_snapshot_output_path("custom.json", project_root=root),
            root / "custom.json",
        )

    def test_one_off_snapshot_outputs_are_ignored(self):
        candidates = (*ONE_OFF_ROOT_ARTIFACTS, "data/snapshots/snapshot.json")
        stdin_payload = b"\0".join(item.encode("utf-8") for item in candidates) + b"\0"
        result = subprocess.run(
            ["git", "check-ignore", "--no-index", "-z", "--stdin"],
            cwd=PROJECT_ROOT,
            input=stdin_payload,
            capture_output=True,
            check=False,
        )

        ignored = {
            item.decode("utf-8")
            for item in result.stdout.split(b"\0")
            if item
        }
        self.assertEqual(result.returncode, 0, result.stderr.decode("utf-8"))
        self.assertEqual(ignored, set(candidates))

    def test_one_off_snapshot_outputs_are_not_tracked(self):
        result = subprocess.run(
            ["git", "ls-files", "--", *ONE_OFF_ROOT_ARTIFACTS],
            cwd=PROJECT_ROOT,
            capture_output=True,
            text=True,
            encoding="utf-8",
            check=True,
        )

        self.assertEqual(result.stdout.strip(), "")


if __name__ == "__main__":
    unittest.main()
