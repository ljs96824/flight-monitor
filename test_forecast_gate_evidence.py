import json
import sqlite3
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch


def _passed_backtest():
    horizon = {
        "n": 8,
        "model": {"mape": 5},
        "naive": {"mape": 6},
        "tcurve": {"mape": 7},
        "skill_gate": {"passed": True, "case_n": 8},
    }
    return {
        "horizons": {str(value): dict(horizon) for value in (1, 3, 7)},
        "cases": {str(value): [] for value in (1, 3, 7)},
    }


def _prediction():
    return {
        "t": 10,
        "status": "ok",
        "median": 100,
        "p25": 90,
        "p75": 110,
        "p10": 80,
        "p90": 120,
    }


def _notification_cell(depart_date, observed_day, t_value, price=100):
    return {
        "depart_date": depart_date,
        "observed_day": observed_day,
        "days_to_departure": t_value,
        "min_price": price,
        "degraded": False,
        "min_sources": ["juhe"],
        "lineage_complete": True,
        "round_ids": [f"round-{depart_date}-{observed_day}"],
    }


class ForecastGateEvidenceTest(unittest.TestCase):
    def test_notification_rejects_shape_cells_below_n5_even_when_skill_passes(self):
        from forecast import build_notification_forecast

        cells = [
            _notification_cell(
                "2026-10-01",
                f"2026-09-{18 + offset:02d}",
                13 - offset,
                100 + offset,
            )
            for offset in range(4)
        ]
        with patch(
            "forecast.load_tcurve_daily_cells", return_value=cells
        ), patch(
            "forecast.walk_forward_backtest", return_value=_passed_backtest()
        ), patch(
            "forecast.predict_price", return_value=_prediction()
        ):
            result = build_notification_forecast(
                {
                    "origin": "PVG",
                    "destination": "KIX",
                    "depart_date": "2026-10-01",
                },
                db_path="unused",
                as_of_day="2026-09-21",
            )

        self.assertFalse(result["eligible"])
        self.assertIn(
            "shape_sample_insufficient",
            result["eligibility"]["reason_codes"],
        )

    def test_notification_never_borrows_shape_from_another_regime(self):
        from forecast import build_notification_forecast

        cells = [
            _notification_cell(
                "2026-10-01",
                f"2026-09-{18 + offset:02d}",
                13 - offset,
                100 + offset,
            )
            for offset in range(4)
        ]
        for depart_index in range(5):
            for offset in range(4):
                cells.append(
                    _notification_cell(
                        f"2026-09-{7 + depart_index:02d}",
                        f"2026-08-{20 + offset:02d}",
                        13 - offset,
                        90 + offset,
                    )
                )

        def holiday_labels(_origin, _destination, target_date):
            return (
                ["中国大陆·国庆节(当天)"]
                if str(target_date) == "2026-10-01"
                else []
            )

        with patch(
            "forecast.load_tcurve_daily_cells", return_value=cells
        ), patch(
            "forecast.walk_forward_backtest", return_value=_passed_backtest()
        ), patch(
            "forecast.predict_price", return_value=_prediction()
        ), patch(
            "forecast.holiday_labels_for_route",
            side_effect=holiday_labels,
            create=True,
        ):
            result = build_notification_forecast(
                {
                    "origin": "PVG",
                    "destination": "KIX",
                    "depart_date": "2026-10-01",
                },
                db_path="unused",
                as_of_day="2026-09-21",
            )

        self.assertFalse(result["eligible"])
        self.assertIn(
            "regime_insufficient",
            result["eligibility"]["reason_codes"],
        )

    def test_lineage_gate_does_not_break_overall_min_invariant(self):
        from forecast import evaluate_forecast_eligibility

        result = evaluate_forecast_eligibility(
            level={"reliable": True, "n": 5},
            shape_points=[{"n": 5, "sufficient": True}],
            backtest_gate={"passed": True, "case_n": 8},
            source_coverage=True,
            regime_sample_n=5,
            lineage_complete=False,
            regime="normal",
        )
        overall = result["overall_reliability"]
        component_min = min(
            item["value"] for item in overall["components"].values()
        )

        self.assertEqual(result["status"], "lineage_incomplete")
        self.assertEqual(overall["value"], component_min)
        self.assertNotIn("lineage", overall["bottlenecks"])

    def test_tcurve_cells_retain_round_lineage(self):
        from tcurve import fold_tcurve_daily_cells

        base = {
            "observed_at": "2026-08-24T09:00:00+08:00",
            "route_type": "international",
            "origin_airport": "PVG",
            "dest_airport": "KIX",
            "depart_date": "2026-10-01",
            "days_to_departure": 38,
            "cabin_class": "economy",
            "source": "juhe",
            "flight_combo": "MU225",
            "price_cny": 1000,
        }
        complete = fold_tcurve_daily_cells([{**base, "round_id": "round-a"}])[0]
        incomplete = fold_tcurve_daily_cells([{**base, "round_id": ""}])[0]

        self.assertTrue(complete["lineage_complete"])
        self.assertEqual(complete["round_ids"], ["round-a"])
        self.assertFalse(incomplete["lineage_complete"])

    def test_tcurve_loader_preserves_round_lineage(self):
        from tcurve import load_tcurve_daily_cells

        with tempfile.TemporaryDirectory() as directory:
            db_path = Path(directory) / "observations.sqlite3"
            connection = sqlite3.connect(db_path)
            try:
                connection.execute(
                    """
                    CREATE TABLE observations (
                        observed_at TEXT,
                        round_id TEXT,
                        route_type TEXT,
                        origin_airport TEXT,
                        dest_airport TEXT,
                        depart_date TEXT,
                        days_to_departure INTEGER,
                        cabin_class TEXT,
                        source TEXT,
                        price_cny REAL
                    )
                    """
                )
                connection.execute(
                    "INSERT INTO observations VALUES "
                    "(?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                    (
                        "2026-08-24T09:00:00+08:00", "round-a",
                        "international", "PVG", "KIX", "2026-10-01",
                        38, "economy", "juhe", 1000,
                    ),
                )
                connection.commit()
            finally:
                connection.close()

            cells = load_tcurve_daily_cells(db_path, route="上海-大阪")

        self.assertEqual(cells[0]["round_ids"], ["round-a"])
        self.assertTrue(cells[0]["lineage_complete"])

    def test_report_never_claims_internal_prediction_entered_user_push(self):
        from scripts.forecast_report import generate_report

        cells = [
            _notification_cell("2026-10-01", "2026-09-21", 10, 100)
        ]
        shape = {
            value: {
                "n": 5,
                "sufficient": True,
                "median": 1,
                "p10": 1,
                "p25": 1,
                "p75": 1,
                "p90": 1,
            }
            for value in range(3, 11)
        }
        decision = {
            "status": "eligible",
            "bottleneck": None,
            "reason_codes": [],
            "human_text": "预测资格已满足",
            "overall_reliability": {
                "value": 1,
                "passed": True,
                "components": {},
                "bottlenecks": [],
                "bottleneck_details": [],
            },
        }
        patterns = {
            "combo_occurrence": [],
            "supply_mix": {
                "direct": 0,
                "transfer": 0,
                "n": 0,
                "basis": "基于组合结构",
            },
            "departure_period": {
                "status": "字段不可得",
                "reason": "面板未存起飞时刻(obs_store v2),待schema扩展后自动点亮",
            },
        }
        with patch(
            "scripts.forecast_report.load_tcurve_daily_cells",
            return_value=cells,
        ), patch(
            "scripts.forecast_report.load_route_observations", return_value=[]
        ), patch(
            "scripts.forecast_report.build_shapes_by_regime",
            return_value={"normal": shape},
        ), patch(
            "scripts.forecast_report.walk_forward_backtest",
            return_value=_passed_backtest(),
        ), patch(
            "scripts.forecast_report.estimate_level",
            return_value={"reliable": True, "n": 5, "value": 100},
        ), patch(
            "scripts.forecast_report.evaluate_forecast_eligibility",
            return_value=decision,
        ), patch(
            "scripts.forecast_report.predict_price", return_value=_prediction()
        ), patch(
            "scripts.forecast_report.build_route_patterns",
            return_value=patterns,
        ):
            text, _payload = generate_report(
                db_path="unused",
                route="上海-大阪",
                as_of_day="2026-09-21",
            )

        self.assertIn("预测未进入用户推送", text)
        self.assertNotIn("预测已进入用户推送", text)


class ReadonlySnapshotIntegrityTest(unittest.TestCase):
    @staticmethod
    def _create_sqlite(path, table_name):
        connection = sqlite3.connect(path)
        try:
            connection.execute(
                f"CREATE TABLE {table_name} (id INTEGER PRIMARY KEY, payload TEXT)"
            )
            connection.execute(
                f"INSERT INTO {table_name} (payload) VALUES ('committed')"
            )
            connection.commit()
        finally:
            connection.close()

    def test_snapshot_excludes_uncommitted_sqlite_pages(self):
        from readonly_snapshot import create_readonly_snapshot

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / "data"
            output = root / "snapshots"
            source.mkdir()
            self._create_sqlite(source / "prices.db", "prices")
            self._create_sqlite(
                source / "observations.sqlite3",
                "observations",
            )
            (source / "api_usage.json").write_text(
                json.dumps({"dates": {}, "entries": []}),
                encoding="utf-8",
            )

            writer = sqlite3.connect(source / "observations.sqlite3")
            try:
                self.assertEqual(
                    writer.execute("PRAGMA journal_mode=WAL").fetchone()[0],
                    "wal",
                )
                writer.execute("PRAGMA wal_autocheckpoint=0")
                writer.execute("PRAGMA cache_size=1")
                writer.execute("BEGIN IMMEDIATE")
                writer.executemany(
                    "INSERT INTO observations (payload) VALUES (?)",
                    [("x" * 2048,) for _ in range(3000)],
                )
                create_readonly_snapshot(
                    "transaction-case",
                    source_dir=source,
                    output_root=output,
                )
                snapshot = sqlite3.connect(
                    output / "transaction-case" / "observations.sqlite3"
                )
                try:
                    count = snapshot.execute(
                        "SELECT COUNT(*) FROM observations"
                    ).fetchone()[0]
                finally:
                    snapshot.close()
            finally:
                writer.rollback()
                writer.close()

        self.assertEqual(count, 1)

    def test_snapshot_includes_committed_rows_still_in_wal(self):
        from readonly_snapshot import create_readonly_snapshot

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / "data"
            output = root / "snapshots"
            source.mkdir()
            self._create_sqlite(source / "prices.db", "prices")
            (source / "api_usage.json").write_text(
                json.dumps({"dates": {}, "entries": []}),
                encoding="utf-8",
            )

            observations = source / "observations.sqlite3"
            writer = sqlite3.connect(observations)
            try:
                self.assertEqual(
                    writer.execute("PRAGMA journal_mode=WAL").fetchone()[0],
                    "wal",
                )
                writer.execute("PRAGMA wal_autocheckpoint=0")
                writer.execute(
                    "CREATE TABLE observations "
                    "(id INTEGER PRIMARY KEY, payload TEXT)"
                )
                writer.execute(
                    "INSERT INTO observations (payload) VALUES ('first')"
                )
                writer.commit()
                writer.execute("PRAGMA wal_checkpoint(TRUNCATE)")
                writer.execute(
                    "INSERT INTO observations (payload) VALUES ('second')"
                )
                writer.commit()

                create_readonly_snapshot(
                    "wal-case",
                    source_dir=source,
                    output_root=output,
                )
                snapshot = sqlite3.connect(
                    output / "wal-case" / "observations.sqlite3"
                )
                try:
                    count = snapshot.execute(
                        "SELECT COUNT(*) FROM observations"
                    ).fetchone()[0]
                finally:
                    snapshot.close()
            finally:
                writer.close()

        self.assertEqual(count, 2)

    def test_tcurve_snapshot_uses_frozen_quality_cells_not_live_logs(self):
        from scripts.tcurve_report import _load_default_quality_cells

        quality_cells = [{"t": 49, "origin_city": "上海"}]
        with tempfile.TemporaryDirectory() as directory:
            snapshot = Path(directory)
            (snapshot / "observations.sqlite3").write_bytes(b"fixture")
            (snapshot / "prices.db").write_bytes(b"fixture")
            (snapshot / "api_usage.json").write_text(
                json.dumps({"dates": {}, "entries": []}),
                encoding="utf-8",
            )
            (snapshot / "snapshot_manifest.json").write_text(
                json.dumps(
                    {"permission_quality_cells": quality_cells},
                    ensure_ascii=False,
                ),
                encoding="utf-8",
            )
            with patch(
                "scripts.audit_permission_pollution.build_audit",
                side_effect=AssertionError("不得读取活轮档"),
            ):
                actual = _load_default_quality_cells(snapshot)

        self.assertEqual(actual, quality_cells)


if __name__ == "__main__":
    unittest.main()
