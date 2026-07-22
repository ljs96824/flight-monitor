import contextlib
import io
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

import analyzer
import observations_store
import storage
from notifier import render_detail_html, render_email


def _flight(flight_no, price, origin, destination, depart_time, arrive_time):
    return {
        "flight_no": flight_no,
        "flight_combo": flight_no,
        "price": price,
        "departure_airport": origin,
        "arrival_airport": destination,
        "dep_airport": origin,
        "arr_airport": destination,
        "departure_time": depart_time,
        "arrival_time": arrive_time,
        "dep_time": depart_time,
        "arr_time": arrive_time,
        "stops": 0,
        "segments": [
            {
                "flight_no": flight_no,
                "dep_airport": origin,
                "arr_airport": destination,
                "dep_time": depart_time,
                "arr_time": arrive_time,
            }
        ],
    }


def _roundtrip_payload():
    outbound = _flight("MU225", 4900, "PVG", "KIX", "2026-10-01 08:00", "2026-10-01 11:00")
    return_flight = _flight("JL891", 4100, "KIX", "PVG", "2026-10-06 18:00", "2026-10-06 20:00")
    plan = {
        "label": "方案A",
        "tier": "首选推荐",
        "variant": "首选推荐",
        "route_type": "international",
        "is_roundtrip": True,
        "outbound_price": 4900,
        "return_price": 4100,
        "price": 9000,
        "roundtrip_price": 9000,
        "outbound_flight": outbound,
        "return_flight": return_flight,
        "passenger_pricing": {
            "passengers": {"adult": 1, "child": 0, "elderly": 0, "infant": 0},
        },
        "tags": "直飞",
        "links": {},
    }
    return {
        "push_type": "价格变化",
        "route": "上海 → 大阪",
        "route_type": "international",
        "is_roundtrip": True,
        "recommendation": "并列参考",
        "display_price": 9000,
        "current_price": 9000,
        "transaction_price": 9000,
        "recommended_plans": [plan],
        "excluded_plans": [],
        "trigger_reason": [],
        "price_history": [],
        "action_range": {"ranges": []},
    }


class AnalysisDiagnosticTest(unittest.TestCase):
    def test_probe_roundtrip_analysis_can_suppress_diagnostics(self):
        outbound = _flight("MU225", 4900, "PVG", "KIX", "2026-10-01 08:00", "2026-10-01 11:00")
        return_flight = _flight("JL891", 4100, "KIX", "PVG", "2026-10-06 18:00", "2026-10-06 20:00")
        outbound_analysis = {
            "economy_recommendations": [outbound],
            "all_flights": [outbound],
            "excluded_flights": [],
        }
        return_analysis = {
            "economy_recommendations": [return_flight],
            "all_flights": [return_flight],
            "excluded_flights": [],
        }
        stdout = io.StringIO()

        with contextlib.redirect_stdout(stdout):
            analyzer.analyze_round_trip(
                outbound_analysis,
                return_analysis,
                emit_diagnostics=False,
            )

        output = stdout.getvalue()
        self.assertNotIn("[方案对比]", output)
        self.assertNotIn("[排除诊断]", output)
        self.assertNotIn("[排除组合]", output)

    def test_excluded_price_diagnostic_is_short_and_full_array_is_opt_in(self):
        combos = [
            {"total_price": value, "outbound": {"flight_combo": f"O{index}"}, "return": {"flight_combo": "R1"}}
            for index, value in enumerate((2100.25, 1900.5, 1900.5, 2300.75, 1800.125, 2400.0), start=1)
        ]

        with patch.dict(os.environ, {"FLIGHT_DEBUG_FULL_ARRAYS": "0"}, clear=False):
            stdout = io.StringIO()
            with contextlib.redirect_stdout(stdout):
                analyzer._log_excluded_price_diagnostics(2500.0, 2000.0, combos)
        lines = [line for line in stdout.getvalue().splitlines() if line]
        self.assertEqual(len(lines), 1)
        self.assertLessEqual(len(lines[0]), 300)
        for field in ("候选数=", "去重后=", "min=", "max=", "低于上限=", "最低5="):
            self.assertIn(field, lines[0])
        self.assertNotIn("完整数组", lines[0])

        with patch.dict(os.environ, {"FLIGHT_DEBUG_FULL_ARRAYS": "1"}, clear=False):
            stdout = io.StringIO()
            with contextlib.redirect_stdout(stdout):
                analyzer._log_excluded_price_diagnostics(2500.0, 2000.0, combos)
        self.assertIn("[排除诊断][完整数组]", stdout.getvalue())
        self.assertIn("2100.25", stdout.getvalue())


class RenderDiagnosticChannelTest(unittest.TestCase):
    def test_email_and_detail_diagnostics_are_channel_tagged(self):
        payload = _roundtrip_payload()
        stdout = io.StringIO()

        with contextlib.redirect_stdout(stdout):
            render_email(payload)
            render_detail_html(payload)

        lines = stdout.getvalue().splitlines()
        caliber_lines = [line for line in lines if "[口径校验]" in line]
        self.assertTrue(any("[口径校验][邮件]" in line for line in caliber_lines))
        self.assertTrue(any("[口径校验][详情]" in line for line in caliber_lines))
        self.assertTrue(all("[邮件]" in line or "[详情]" in line for line in caliber_lines))
        self.assertTrue(any("[渲染统计][邮件]" in line for line in lines))
        self.assertTrue(any("[渲染统计][详情]" in line for line in lines))


class Utf8TeeTest(unittest.TestCase):
    def test_run_log_is_utf8_and_matches_stdout_lines(self):
        with tempfile.TemporaryDirectory() as tmp:
            log_path = Path(tmp) / "run_latest.log"
            script = (
                "from log_utils import configure_run_logging\n"
                f"configure_run_logging(r'{log_path}')\n"
                "print('[购买建议] 测试')\n"
                "print('[口径校验][邮件] 测试')\n"
            )
            completed = subprocess.run(
                [sys.executable, "-X", "utf8", "-c", script],
                cwd=Path(__file__).resolve().parent,
                capture_output=True,
                text=True,
                encoding="utf-8",
                check=False,
            )

            self.assertEqual(completed.returncode, 0, completed.stderr)
            expected = ["[购买建议] 测试", "[口径校验][邮件] 测试"]
            self.assertEqual(completed.stdout.splitlines(), expected)
            self.assertEqual(log_path.read_text(encoding="utf-8").splitlines(), expected)
            self.assertFalse(log_path.read_bytes().startswith((b"\xff\xfe", b"\xfe\xff")))


class _FakeConnection:
    def __init__(self):
        self.closed = False

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, traceback):
        return False

    def close(self):
        self.closed = True


class SqliteConnectionLifecycleTest(unittest.TestCase):
    def test_storage_connection_context_closes_connection(self):
        connection = _FakeConnection()
        with patch("storage.sqlite3.connect", return_value=connection):
            with storage._connect() as opened:
                self.assertIs(opened, connection)
        self.assertTrue(connection.closed)

    def test_observation_connection_context_closes_connection(self):
        connection = _FakeConnection()
        with patch("observations_store.sqlite3.connect", return_value=connection):
            with observations_store._managed_connection(Path("unused.sqlite3")) as opened:
                self.assertIs(opened, connection)
        self.assertTrue(connection.closed)


if __name__ == "__main__":
    unittest.main()
