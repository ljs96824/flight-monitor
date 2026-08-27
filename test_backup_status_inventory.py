import tempfile
import unittest
from pathlib import Path


class BackupStatusInventoryTest(unittest.TestCase):
    def test_backup_status_is_known_runtime_metadata_not_an_archive_member(self):
        from runtime_backup import scan_runtime_state
        from test_runtime_backup import _runtime_fixture

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            _project, data = _runtime_fixture(root)
            (data / "backup_status.json").write_text("{}", encoding="utf-8")

            inventory = scan_runtime_state(data, strict=True)

        self.assertNotIn(
            "backup_status.json",
            [item.source_rel for item in inventory["selected"]],
        )


if __name__ == "__main__":
    unittest.main()
