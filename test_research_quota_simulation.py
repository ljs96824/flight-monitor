import hashlib
import io
import json
import sqlite3
import tempfile
import unittest
from contextlib import closing, redirect_stderr, redirect_stdout
from datetime import date
from pathlib import Path


class NoFetchSource:
    calls = 0
    name = "juhe"

    def fetch(self, *_args, **_kwargs):
        self.__class__.calls += 1
        raise AssertionError("配额模拟不得调用真实或伪造fetch")


def no_fetch_source_builder(_origin, _dest, route_type=None):
    return [NoFetchSource()], []


def _sha(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


class ResearchQuotaSimulationTest(unittest.TestCase):
    def test_cli_keeps_stdout_as_json_and_routes_planner_logs_to_stderr(self):
        from unittest.mock import patch

        from scripts.research_quota_simulation import main

        stdout = io.StringIO()
        stderr = io.StringIO()

        def noisy_report(**_kwargs):
            print("[source-route] fixture diagnostic")
            return {"hard_gate": {"ready": False}}

        with (
            patch(
                "scripts.research_quota_simulation.build_report",
                side_effect=noisy_report,
            ),
            redirect_stdout(stdout),
            redirect_stderr(stderr),
        ):
            result = main(["--today", "2026-08-27"])

        self.assertEqual(result, 0)
        self.assertEqual(
            json.loads(stdout.getvalue()),
            {"hard_gate": {"ready": False}},
        )
        self.assertIn("[source-route] fixture diagnostic", stderr.getvalue())

    def test_build_report_is_read_only_and_never_executes_plan(self):
        from scripts.research_quota_simulation import build_report

        with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as tmp:
            root = Path(tmp)
            config = root / "config.yaml"
            state = root / "basket_state.json"
            subscriptions = root / "subscriptions.json"
            observations = root / "observations.sqlite3"
            prices = root / "prices.db"
            usage = root / "api_usage.json"
            config.write_text(
                """
source_quota_budget:
  juhe: 550
RESEARCH_COHORT_V2: false
research_cohort_v2_gates:
  off_disk_copy: true
""".strip(),
                encoding="utf-8",
            )
            subscriptions.write_text("[]", encoding="utf-8")
            usage.write_text('{"version":2,"dates":{},"entries":[]}', encoding="utf-8")
            with closing(sqlite3.connect(observations)) as connection, connection:
                connection.executescript(
                    """
                    CREATE TABLE observations (
                      id INTEGER PRIMARY KEY,
                      observed_at_utc TEXT,
                      observed_day_shanghai TEXT,
                      legacy_time_ambiguous INTEGER NOT NULL DEFAULT 0
                    );
                    CREATE TABLE collection_cells (id INTEGER PRIMARY KEY);
                    """
                )
            with closing(sqlite3.connect(prices)) as connection, connection:
                for table in ("flight_details", "roundtrip_price_history", "push_snapshots"):
                    connection.execute(
                        f"CREATE TABLE {table} (id INTEGER PRIMARY KEY, round_id TEXT)"
                    )
            before = {
                path.name: _sha(path)
                for path in (config, subscriptions, observations, prices, usage)
            }

            NoFetchSource.calls = 0
            report = build_report(
                today=date(2026, 8, 26),
                config_path=config,
                state_path=state,
                subscriptions_path=subscriptions,
                observations_path=observations,
                prices_path=prices,
                usage_path=usage,
                source_builder=no_fetch_source_builder,
                other_scheduled_calls=4,
            )
            after = {
                path.name: _sha(path)
                for path in (config, subscriptions, observations, prices, usage)
            }

        self.assertEqual(NoFetchSource.calls, 0)
        self.assertEqual(report["research_request_count"], 6)
        self.assertEqual(report["sample_roles"], {
            "trajectory_anchor": 2,
            "cross_sectional_probe": 4,
        })
        self.assertEqual(report["quota"]["basket_planned_unique"], 6)
        self.assertEqual(report["quota"]["other_scheduled_calls"], 4)
        self.assertTrue(report["hard_gate"]["ready"])
        self.assertEqual(before, after)


if __name__ == "__main__":
    unittest.main()
