import hashlib
import sqlite3
import tempfile
import unittest
from contextlib import closing
from pathlib import Path
from unittest.mock import patch


ROOT = Path(__file__).resolve().parent
EXPECTED_TRACKED_SQLITE_EXCEPTIONS = {
    "observations.combo-normalize-20260709162848.sqlite3.bak",
    "observations.combo-normalize-20260709164309.sqlite3.bak",
    "observations.combo-normalize-20260709164334.sqlite3.bak",
}


class TrackedSqliteBackupAuditTest(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.root = Path(self.temp_dir.name)

    def tearDown(self):
        self.temp_dir.cleanup()

    @staticmethod
    def _sha256(path):
        return hashlib.sha256(Path(path).read_bytes()).hexdigest()

    def _create_fixture(self, name="audit.sqlite3.bak"):
        path = self.root / name
        with closing(sqlite3.connect(path)) as connection:
            connection.executescript(
                """
                CREATE TABLE observations (
                  id INTEGER PRIMARY KEY,
                  observed_at TEXT NOT NULL,
                  round_id TEXT NOT NULL,
                  origin_airport TEXT NOT NULL,
                  dest_airport TEXT NOT NULL,
                  depart_date TEXT NOT NULL,
                  passenger_count INTEGER,
                  budget_cny REAL,
                  contact_email TEXT
                );
                CREATE INDEX idx_observations_route
                  ON observations(origin_airport, dest_airport, depart_date);
                CREATE VIEW observation_counts AS
                  SELECT depart_date, COUNT(*) AS row_count
                  FROM observations GROUP BY depart_date;
                CREATE TRIGGER observations_no_delete
                  BEFORE DELETE ON observations
                  BEGIN SELECT RAISE(ABORT, 'fixture is append-only'); END;
                """
            )
            connection.execute(
                """
                INSERT INTO observations (
                  observed_at, round_id, origin_airport, dest_airport,
                  depart_date, passenger_count, budget_cny, contact_email
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    "2099-01-01T09:00:00+08:00",
                    "fixture-round-sensitive-marker",
                    "AAA",
                    "BBB",
                    "2099-03-01",
                    7,
                    4321.25,
                    "privacy-fixture@example.invalid",
                ),
            )
            connection.commit()
        return path

    def test_gitignore_contract_uses_exact_tracked_git_set(self):
        from scripts.audit_tracked_sqlite_backups import (
            discover_tracked_sqlite_artifacts,
        )

        tracked = set(discover_tracked_sqlite_artifacts(ROOT))

        self.assertEqual(tracked, EXPECTED_TRACKED_SQLITE_EXCEPTIONS)
        ignore_lines = {
            line.strip()
            for line in (ROOT / ".gitignore").read_text(encoding="utf-8").splitlines()
            if line.strip() and not line.lstrip().startswith("#")
        }
        self.assertTrue(
            {"*.sqlite3", "*.sqlite3.bak", "*.db", "*-wal", "*-shm", "*-journal"}
            <= ignore_lines
        )

    def test_audit_uses_immutable_readonly_and_preserves_file(self):
        from scripts import audit_tracked_sqlite_backups as module

        path = self._create_fixture()
        before_hash = self._sha256(path)
        before_mtime = path.stat().st_mtime_ns
        before_names = {item.name for item in self.root.iterdir()}
        real_connect = sqlite3.connect
        calls = []

        def recording_connect(*args, **kwargs):
            calls.append((args, kwargs))
            return real_connect(*args, **kwargs)

        with patch.object(module.sqlite3, "connect", side_effect=recording_connect):
            audit = module.audit_sqlite_file(path)

        self.assertEqual(audit["integrity_check"], "ok")
        self.assertEqual(audit["user_version"], 0)
        self.assertEqual(audit["tables"][0]["name"], "observations")
        self.assertEqual(audit["tables"][0]["row_count"], 1)
        self.assertEqual(audit["views"], ["observation_counts"])
        self.assertEqual(audit["triggers"], ["observations_no_delete"])
        self.assertIn("mode=ro", calls[0][0][0])
        self.assertIn("immutable=1", calls[0][0][0])
        self.assertTrue(calls[0][1]["uri"])
        self.assertEqual(self._sha256(path), before_hash)
        self.assertEqual(path.stat().st_mtime_ns, before_mtime)
        self.assertEqual({item.name for item in self.root.iterdir()}, before_names)

    def test_public_report_contains_categories_and_counts_but_no_values(self):
        from scripts.audit_tracked_sqlite_backups import (
            audit_sqlite_file,
            render_public_report,
        )

        path = self._create_fixture()
        audit = audit_sqlite_file(path)
        report = render_public_report(
            artifact_identities=[
                {
                    "path": "fixture.sqlite3.bak",
                    "git_blob": "abc123",
                    "sha256": self._sha256(path),
                    "bytes": path.stat().st_size,
                    "first_commit": "first123",
                    "last_commit": "last123",
                }
            ],
            audit=audit,
            generated_on="2099-01-02",
        )

        self.assertIn("直接个人信息", report)
        self.assertIn("个人行程元数据", report)
        self.assertIn("命中行数", report)
        for sensitive_value in (
            "privacy-fixture@example.invalid",
            "fixture-round-sensitive-marker",
            "AAA",
            "BBB",
            "2099-03-01",
            "4321.25",
        ):
            self.assertNotIn(sensitive_value, report)

    def test_confirmed_secret_blocks_public_report(self):
        from scripts.audit_tracked_sqlite_backups import (
            SecretCredentialDetected,
            audit_sqlite_file,
            render_public_report,
        )

        path = self.root / "secret.sqlite3.bak"
        with closing(sqlite3.connect(path)) as connection:
            connection.execute("CREATE TABLE credentials (api_token TEXT)")
            connection.execute(
                "INSERT INTO credentials VALUES (?)",
                ("sk-test-only-never-print-abcdefghijklmnopqrstuvwxyz",),
            )
            connection.commit()

        audit = audit_sqlite_file(path)
        self.assertTrue(audit["secret_credentials_detected"])
        with self.assertRaises(SecretCredentialDetected):
            render_public_report(
                artifact_identities=[],
                audit=audit,
                generated_on="2099-01-02",
            )


if __name__ == "__main__":
    unittest.main()
