import json
import sqlite3
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch


class ReadonlySnapshotGroupConsistencyTest(unittest.TestCase):
    @staticmethod
    def _create_database(path, table):
        connection = sqlite3.connect(path)
        try:
            connection.execute(
                f"CREATE TABLE {table} (id INTEGER PRIMARY KEY, payload TEXT)"
            )
            connection.execute(
                f"INSERT INTO {table} (payload) VALUES ('before')"
            )
            connection.commit()
        finally:
            connection.close()

    def test_external_wal_commits_between_database_backups_retry_whole_group(self):
        import readonly_snapshot

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / "data"
            output = root / "snapshots"
            source.mkdir()
            self._create_database(source / "prices.db", "prices")
            self._create_database(
                source / "observations.sqlite3",
                "observations",
            )
            (source / "api_usage.json").write_text(
                json.dumps({"dates": {}, "entries": []}),
                encoding="utf-8",
            )

            writers = {}
            for name in ("prices.db", "observations.sqlite3"):
                connection = sqlite3.connect(source / name)
                connection.execute("PRAGMA journal_mode=WAL")
                connection.execute("PRAGMA wal_autocheckpoint=0")
                connection.execute("PRAGMA wal_checkpoint(TRUNCATE)")
                writers[name] = connection

            original_backup = readonly_snapshot._backup_sqlite
            injected = False

            def backup_with_concurrent_commit(source_path, destination):
                nonlocal injected
                original_backup(source_path, destination)
                if source_path.name == "prices.db" and not injected:
                    writers["prices.db"].execute(
                        "INSERT INTO prices (payload) VALUES ('after')"
                    )
                    writers["observations.sqlite3"].execute(
                        "INSERT INTO observations (payload) VALUES ('after')"
                    )
                    writers["prices.db"].commit()
                    writers["observations.sqlite3"].commit()
                    injected = True

            try:
                with patch(
                    "readonly_snapshot._backup_sqlite",
                    side_effect=backup_with_concurrent_commit,
                ) as backup_mock:
                    result = readonly_snapshot.create_readonly_snapshot(
                        "group-retry",
                        source_dir=source,
                        output_root=output,
                        retries=1,
                    )
            finally:
                for connection in writers.values():
                    connection.close()

            target = Path(result["path"])
            prices = sqlite3.connect(target / "prices.db")
            observations = sqlite3.connect(target / "observations.sqlite3")
            try:
                price_count = prices.execute(
                    "SELECT COUNT(*) FROM prices"
                ).fetchone()[0]
                observation_count = observations.execute(
                    "SELECT COUNT(*) FROM observations"
                ).fetchone()[0]
            finally:
                prices.close()
                observations.close()
            manifest = json.loads(
                (target / "snapshot_manifest.json").read_text(encoding="utf-8")
            )

        self.assertEqual(backup_mock.call_count, 4)
        self.assertEqual((price_count, observation_count), (2, 2))
        self.assertEqual(manifest["capture"]["attempts"], 2)
        self.assertEqual(
            manifest["capture"]["consistency"],
            "file_level_stable_inputs",
        )


if __name__ == "__main__":
    unittest.main()
