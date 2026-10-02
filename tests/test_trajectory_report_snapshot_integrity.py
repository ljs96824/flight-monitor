"""Snapshot identity contracts using sealed, synthetic SQLite files only."""
import ast
from contextlib import closing
import hashlib
import json
from pathlib import Path
import sqlite3
import struct
import tempfile
import unittest
from unittest.mock import patch

from tests.test_trajectory_report import DEPARTURES, ROUTE, SCRIPT, load_module, make_snapshot


def mutated_gate(kind):
    module = load_module()
    tree = ast.parse(SCRIPT.read_text(encoding="utf-8"))
    gate = next(n for n in tree.body if isinstance(n, ast.FunctionDef) and n.name == "_snapshot_db")
    before = ast.dump(gate, include_attributes=False)
    if kind == "source_digest":
        nodes = [n for n in ast.walk(gate) if isinstance(n, ast.Constant) and n.value == "snapshot_sha256"]
        if len(nodes) != 1:
            raise AssertionError("MUTATION_NODE_MATCH: snapshot_sha256")
        nodes[0].value = "source_sha256"
    elif kind == "skip_database_hash":
        nodes = [n for n in gate.body if isinstance(n, ast.If)
                 and isinstance(n.test, ast.Compare)
                 and isinstance(n.test.left, ast.Name) and n.test.left.id == "database_sha"]
        if len(nodes) != 1:
            raise AssertionError("MUTATION_NODE_MATCH: database_sha")
        gate.body.remove(nodes[0])
    else:
        raise AssertionError("UNKNOWN_MUTATION")
    if before == ast.dump(gate, include_attributes=False):
        raise AssertionError("MUTATION_NO_CHANGE")
    ast.fix_missing_locations(tree)
    code = compile(tree, str(SCRIPT), "exec")
    exec(code, module.__dict__)
    return module


class TrajectorySnapshotIntegrityTest(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.snapshot = Path(temporary.name)/"snapshot"
        self.sha = make_snapshot(self.snapshot)
        self.db = self.snapshot/"observations.sqlite3"
        self.manifest = self.snapshot/"snapshot_manifest.json"

    def seal(self, metadata):
        self.manifest.write_text(json.dumps(metadata), encoding="utf-8")
        self.sha = hashlib.sha256(self.manifest.read_bytes()).hexdigest()

    def report(self, module=None):
        subject = module or load_module()
        before = {p.name: p.read_bytes() for p in self.snapshot.iterdir()}
        try:
            return subject.generate_report(db_path=self.snapshot, expect_manifest_sha=self.sha,
                                           route=ROUTE, airport_pair="PVG-KIX", departures=DEPARTURES)
        finally:
            self.assertEqual(before, {p.name: p.read_bytes() for p in self.snapshot.iterdir()},
                             "REPORT_MUST_NOT_RESEAL_OR_WRITE")

    def change_one_price_byte(self):
        original = self.db.read_bytes()
        price = struct.pack(">d", 100.004)
        self.assertEqual(original.count(price), 1, "PRICE_BYTE_INJECTION_UNIQUE")
        offset = original.index(price) + len(price) - 1
        changed = bytearray(original)
        changed[offset] ^= 1
        self.db.write_bytes(changed)
        self.assertEqual(sum(a != b for a, b in zip(original, changed)), 1)
        with closing(sqlite3.connect(self.db)) as connection:
            actual = connection.execute("SELECT price_cny FROM observations WHERE round_id='display-only'").fetchone()[0]
        self.assertNotEqual(actual, 100.004, "PRICE_VALUE_REALLY_CHANGED")

    def assert_database_mismatch_rejected(self, module=None):
        with self.assertRaisesRegex(ValueError, "snapshot database SHA-256 mismatch",
                                    msg="DATABASE_MISMATCH_NOT_REJECTED"):
            self.report(module)

    def test_matching_database_and_manifest_pass(self):
        text, data = self.report()
        self.assertTrue(text)
        self.assertEqual([t["depart_date"] for t in data["trajectories"]], list(DEPARTURES))

    def test_sealed_database_price_byte_tampering_rejected(self):
        manifest_before = self.manifest.read_bytes()
        self.change_one_price_byte()
        self.assertEqual(self.manifest.read_bytes(), manifest_before)
        self.assert_database_mismatch_rejected()

    def test_missing_snapshot_hash_map_rejected(self):
        self.seal({"label": "synthetic-contract"})
        with self.assertRaisesRegex(ValueError, "snapshot database SHA-256 missing or invalid"):
            self.report()

    def test_missing_snapshot_member_hash_rejected(self):
        self.seal({"snapshot_sha256": {}})
        with self.assertRaisesRegex(ValueError, "snapshot database SHA-256 missing or invalid"):
            self.report()

    def source_only_identity(self):
        digest = hashlib.sha256(self.db.read_bytes()).hexdigest()
        self.seal({"source_sha256": {"observations.sqlite3": digest},
                   "snapshot_sha256": {"observations.sqlite3": "0" * 64}})

    def test_source_digest_cannot_replace_snapshot_digest(self):
        self.source_only_identity()
        self.assert_database_mismatch_rejected()

    def test_verified_manifest_bytes_are_read_once_before_database(self):
        module = load_module()
        read_bytes = Path.read_bytes
        events = []

        def track(path):
            events.append(path)
            if path == self.manifest and events.count(path) > 1:
                raise AssertionError("MANIFEST_SECOND_READ")
            return read_bytes(path)

        with patch.object(Path, "read_bytes", track):
            self.assertEqual(module._snapshot_db(self.snapshot, self.sha), self.db)
        self.assertEqual(events, [self.manifest, self.db], "MANIFEST_THEN_DATABASE")
        events.clear()
        with patch.object(Path, "read_bytes", track), self.assertRaisesRegex(ValueError, "manifest"):
            module._snapshot_db(self.snapshot, "0" * 64)
        self.assertEqual(events, [self.manifest], "DO_NOT_READ_DB_AFTER_MANIFEST_REJECTION")

    def test_rejection_precedes_sqlite_consumers(self):
        self.change_one_price_byte()
        module = load_module()
        with patch.object(module, "load_tcurve_daily_cells", wraps=module.load_tcurve_daily_cells) as daily, \
                patch.object(module, "_load_evidence", wraps=module._load_evidence) as raw:
            self.assert_database_mismatch_rejected(module)
        daily.assert_not_called()
        raw.assert_not_called()

    def test_mutation_source_digest_is_rejected(self):
        self.source_only_identity()
        mutant = mutated_gate("source_digest")
        with self.assertRaisesRegex(AssertionError, "DATABASE_MISMATCH_NOT_REJECTED"):
            self.assert_database_mismatch_rejected(mutant)

    def test_mutation_skipping_database_hash_is_rejected(self):
        self.change_one_price_byte()
        mutant = mutated_gate("skip_database_hash")
        with self.assertRaisesRegex(AssertionError, "DATABASE_MISMATCH_NOT_REJECTED"):
            self.assert_database_mismatch_rejected(mutant)


if __name__ == "__main__":
    unittest.main()
