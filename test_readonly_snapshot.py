import hashlib
import json
import sqlite3
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch


class ReadonlySnapshotTest(unittest.TestCase):
    @staticmethod
    def _create_sqlite(path, marker):
        connection = sqlite3.connect(path)
        try:
            connection.execute(
                "CREATE TABLE fixture (id INTEGER PRIMARY KEY, marker TEXT)"
            )
            connection.execute(
                "INSERT INTO fixture (marker) VALUES (?)", (marker,)
            )
            connection.commit()
        finally:
            connection.close()

    def test_snapshot_copies_three_inputs_and_reports_matching_hashes(self):
        from readonly_snapshot import SNAPSHOT_FILENAMES, create_readonly_snapshot

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / "data"
            output = root / "snapshots"
            source.mkdir()
            self._create_sqlite(source / "prices.db", "prices")
            self._create_sqlite(source / "observations.sqlite3", "observations")
            (source / "api_usage.json").write_text(
                json.dumps({"dates": {}}, ensure_ascii=False),
                encoding="utf-8",
            )

            result = create_readonly_snapshot(
                "unit-case",
                source_dir=source,
                output_root=output,
                generated_at="2026-08-24T16:00:00+08:00",
            )

            self.assertEqual(result["label"], "unit-case")
            self.assertEqual(result["generated_at"], "2026-08-24T16:00:00+08:00")
            for name in SNAPSHOT_FILENAMES:
                copied = output / "unit-case" / name
                self.assertEqual(
                    result["files"][name]["sha256"],
                    hashlib.sha256(copied.read_bytes()).hexdigest(),
                )

                self.assertEqual(
                    result["files"][name]["source_sha256"],
                    hashlib.sha256((source / name).read_bytes()).hexdigest(),
                )
            self.assertEqual(
                (output / "unit-case" / "api_usage.json").read_bytes(),
                (source / "api_usage.json").read_bytes(),
            )
            for name in ("prices.db", "observations.sqlite3"):
                source_connection = sqlite3.connect(source / name)
                copied_connection = sqlite3.connect(output / "unit-case" / name)
                try:
                    self.assertEqual(
                        copied_connection.execute(
                            "SELECT marker FROM fixture"
                        ).fetchall(),
                        source_connection.execute(
                            "SELECT marker FROM fixture"
                        ).fetchall(),
                    )
                finally:
                    copied_connection.close()
                    source_connection.close()
            manifest = json.loads(
                (output / "unit-case" / "snapshot_manifest.json").read_text(
                    encoding="utf-8"
                )
            )
            self.assertEqual(manifest["label"], "unit-case")
            self.assertEqual(
                set(manifest["snapshot_sha256"]), set(SNAPSHOT_FILENAMES)
            )

    def test_invalid_snapshot_label_is_rejected(self):
        from readonly_snapshot import create_readonly_snapshot

        with tempfile.TemporaryDirectory() as directory:
            with self.assertRaises(ValueError):
                create_readonly_snapshot(
                    "../escape",
                    source_dir=Path(directory),
                    output_root=Path(directory) / "out",
                )

    def test_reports_accept_snapshot_directory_as_db_argument(self):
        from readonly_snapshot import resolve_observations_db
        from scripts.forecast_report import generate_report as forecast_report
        from scripts.tcurve_report import generate_report as tcurve_report

        with tempfile.TemporaryDirectory() as directory:
            snapshot = Path(directory) / "snapshot"
            snapshot.mkdir()
            observations = snapshot / "observations.sqlite3"
            observations.write_bytes(b"fixture")

            self.assertEqual(resolve_observations_db(snapshot), observations)
            with patch(
                "scripts.forecast_report.load_tcurve_daily_cells", return_value=[]
            ) as forecast_loader:
                text, _ = forecast_report(db_path=snapshot, route="上海-大阪")
            self.assertIn("无可用非退化观测数据", text)
            self.assertEqual(Path(forecast_loader.call_args.args[0]), observations)

            curve = {
                "origin_city": "上海",
                "dest_city": "大阪",
                "price_caliber": "单人单程CNY含税",
                "method_version": "tcurve_v1",
                "airport_pair": None,
                "daily_cell_count": 0,
                "included_depart_dates": [],
                "degraded_count": 0,
                "degraded_excluded_count": 0,
                "coverage": {"t_min": None, "t_max": None},
                "points": [],
                "daily_cells": [],
            }
            with patch(
                "scripts.tcurve_report.build_tcurve", return_value=curve
            ) as tcurve_builder:
                report = tcurve_report(
                    db_path=snapshot,
                    route="上海-大阪",
                    quality_cells=[],
                )
            self.assertIn("无数据", report)
            self.assertEqual(Path(tcurve_builder.call_args.args[0]), observations)


if __name__ == "__main__":
    unittest.main()
