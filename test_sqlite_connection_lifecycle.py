import ast
import gc
import os
import sqlite3
import sys
import tempfile
import unittest
import warnings
from collections import defaultdict
from contextlib import contextmanager
from pathlib import Path
from unittest.mock import Mock, patch


ROOT = Path(__file__).resolve().parent

ALLOWED_DIRECT_SQLITE_CONNECT = {
    ("analytics/report_lib.py", "_readonly_connection", 1),
    ("observations_store.py", "_managed_connection", 1),
    ("observations_store.py", "load_fresh_observation_snapshot", 1),
    ("provenance.py", "readonly_connection", 1),
    ("readonly_snapshot.py", "_backup_sqlite", 1),
    ("readonly_snapshot.py", "_backup_sqlite", 2),
    ("readonly_snapshot.py", "_open_sqlite_watchers", 1),
    ("scripts/audit_permission_pollution.py", "readonly_connection", 1),
    ("storage.py", "_connect", 1),
    ("tcurve.py", "readonly_connection", 1),
}

DIRECT_CONNECT_CLOSE_OWNER = {
    ("analytics/report_lib.py", "_readonly_connection", 1): (
        "analytics/report_lib.py",
        "load_observations",
    ),
    ("observations_store.py", "_managed_connection", 1): (
        "observations_store.py",
        "_managed_connection",
    ),
    ("observations_store.py", "load_fresh_observation_snapshot", 1): (
        "observations_store.py",
        "load_fresh_observation_snapshot",
    ),
    ("provenance.py", "readonly_connection", 1): (
        "provenance.py",
        "readonly_connection",
    ),
    ("readonly_snapshot.py", "_backup_sqlite", 1): (
        "readonly_snapshot.py",
        "_backup_sqlite",
    ),
    ("readonly_snapshot.py", "_backup_sqlite", 2): (
        "readonly_snapshot.py",
        "_backup_sqlite",
    ),
    ("readonly_snapshot.py", "_open_sqlite_watchers", 1): (
        "readonly_snapshot.py",
        "create_readonly_snapshot",
    ),
    ("scripts/audit_permission_pollution.py", "readonly_connection", 1): (
        "scripts/audit_permission_pollution.py",
        "readonly_connection",
    ),
    ("storage.py", "_connect", 1): ("storage.py", "_connect"),
    ("tcurve.py", "readonly_connection", 1): (
        "tcurve.py",
        "readonly_connection",
    ),
}

READONLY_CONNECT_SCOPES = {
    ("analytics/report_lib.py", "_readonly_connection"),
    ("observations_store.py", "load_fresh_observation_snapshot"),
    ("provenance.py", "readonly_connection"),
    ("readonly_snapshot.py", "_backup_sqlite"),
    ("readonly_snapshot.py", "_open_sqlite_watchers"),
    ("scripts/audit_permission_pollution.py", "readonly_connection"),
    ("tcurve.py", "readonly_connection"),
}


@contextmanager
def capture_unraisable():
    """捕获析构器经 sys.unraisablehook 抛出的 ResourceWarning。"""
    captured = []
    previous_hook = sys.unraisablehook

    def hook(unraisable):
        if isinstance(unraisable.exc_value, ResourceWarning):
            # 不保存 unraisable.object；它会反向延长 SQLite 句柄寿命。
            captured.append(unraisable.exc_value)
        else:  # pragma: no cover - 保留解释器对其他析构异常的默认处理
            previous_hook(unraisable)

    with warnings.catch_warnings():
        warnings.simplefilter("error", ResourceWarning)
        sys.unraisablehook = hook
        try:
            yield captured
        finally:
            gc.collect()
            gc.collect()
            sys.unraisablehook = previous_hook


class _DirectConnectVisitor(ast.NodeVisitor):
    def __init__(self):
        self.scope = []
        self.calls = []

    def _visit_scope(self, node):
        self.scope.append(node.name)
        self.generic_visit(node)
        self.scope.pop()

    def visit_FunctionDef(self, node):
        self._visit_scope(node)

    def visit_AsyncFunctionDef(self, node):
        self._visit_scope(node)

    def visit_ClassDef(self, node):
        self._visit_scope(node)

    def visit_Call(self, node):
        function = node.func
        if (
            isinstance(function, ast.Attribute)
            and function.attr == "connect"
            and isinstance(function.value, ast.Name)
            and function.value.id == "sqlite3"
        ):
            self.calls.append((".".join(self.scope) or "<module>", node.lineno))
        self.generic_visit(node)


def _production_python_files():
    for path in ROOT.rglob("*.py"):
        relative = path.relative_to(ROOT)
        if path.name.startswith("test_"):
            continue
        if any(part in {".git", ".pytest_cache", "__pycache__", "data"} for part in relative.parts):
            continue
        yield path


def _direct_connect_points():
    grouped = defaultdict(list)
    for path in _production_python_files():
        relative = path.relative_to(ROOT).as_posix()
        visitor = _DirectConnectVisitor()
        visitor.visit(ast.parse(path.read_text(encoding="utf-8-sig"), filename=str(path)))
        for scope, line_number in visitor.calls:
            grouped[(relative, scope)].append(line_number)

    points = set()
    for (relative, scope), line_numbers in grouped.items():
        for ordinal, _line_number in enumerate(sorted(line_numbers), start=1):
            points.add((relative, scope, ordinal))
    return points


def _function_node(relative_path, function_name):
    tree = ast.parse(
        (ROOT / relative_path).read_text(encoding="utf-8-sig"),
        filename=relative_path,
    )
    return next(
        node
        for node in tree.body
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
        and node.name == function_name
    )


def _has_explicit_close(node):
    return any(
        isinstance(child, ast.Call)
        and isinstance(child.func, ast.Attribute)
        and child.func.attr == "close"
        for child in ast.walk(node)
    )


def _has_query_only(node):
    return any(
        isinstance(child, ast.Constant)
        and isinstance(child.value, str)
        and child.value.strip().upper() == "PRAGMA QUERY_ONLY=ON"
        for child in ast.walk(node)
    )


class SqliteConnectionLifecycleContractTest(unittest.TestCase):
    def test_capture_unraisable_detects_an_unclosed_connection(self):
        with tempfile.TemporaryDirectory() as directory:
            database = Path(directory) / "leak.sqlite3"
            with capture_unraisable() as captured:
                connection = sqlite3.connect(database)
                connection.execute("CREATE TABLE fixture (id INTEGER)")
                del connection

        self.assertEqual(len(captured), 1)
        self.assertIsInstance(captured[0], ResourceWarning)

    def test_interleaved_round_fixture_closes_its_query_connection(self):
        from test_collection_context_singleflight import ObservationRoundContextTest

        case = ObservationRoundContextTest(
            "test_interleaved_threads_write_to_their_own_round_context"
        )
        result = unittest.TestResult()
        with capture_unraisable() as captured:
            case.run(result)

        self.assertTrue(result.wasSuccessful(), result.errors + result.failures)
        self.assertEqual(captured, [])

    def test_production_direct_connect_points_match_frozen_registry(self):
        self.assertEqual(_direct_connect_points(), ALLOWED_DIRECT_SQLITE_CONNECT)

    def test_every_registered_connect_has_an_explicit_close_owner(self):
        self.assertEqual(set(DIRECT_CONNECT_CLOSE_OWNER), ALLOWED_DIRECT_SQLITE_CONNECT)
        for point, owner in DIRECT_CONNECT_CLOSE_OWNER.items():
            with self.subTest(point=point, owner=owner):
                self.assertTrue(_has_explicit_close(_function_node(*owner)))

    def test_true_readonly_connectors_enable_query_only(self):
        for scope in READONLY_CONNECT_SCOPES:
            with self.subTest(scope=scope):
                self.assertTrue(_has_query_only(_function_node(*scope)))

    def test_report_readonly_setup_failure_closes_connection(self):
        from analytics import report_lib

        connection = Mock()
        connection.execute.side_effect = sqlite3.OperationalError("query_only failed")
        with tempfile.TemporaryDirectory() as directory:
            database = Path(directory) / "observations.sqlite3"
            database.touch()
            with patch("analytics.report_lib.sqlite3.connect", return_value=connection):
                with self.assertRaises(sqlite3.OperationalError):
                    report_lib._readonly_connection(database)

        connection.close.assert_called_once()

    def test_observation_read_failure_closes_query_only_connection(self):
        import observations_store

        connection = Mock()
        connection.execute.side_effect = sqlite3.OperationalError("read failed")
        with tempfile.TemporaryDirectory() as directory:
            database = Path(directory) / "observations.sqlite3"
            database.touch()
            with patch(
                "observations_store.sqlite3.connect",
                return_value=connection,
            ):
                result = observations_store.load_fresh_observation_snapshot(
                    source="juhe",
                    origin_airport="PVG",
                    dest_airport="KIX",
                    depart_date="2099-10-01",
                    cabin_class="economy",
                    db_path=database,
                )

        self.assertIsNone(result)
        connection.close.assert_called_once()
        self.assertEqual(
            connection.execute.call_args_list[0].args,
            ("PRAGMA query_only=ON",),
        )


    def test_snapshot_destination_connect_failure_closes_source_connection(self):
        import readonly_snapshot

        source_connection = Mock()
        connect_failure = sqlite3.OperationalError("destination connect failed")
        with patch(
            "readonly_snapshot.sqlite3.connect",
            side_effect=[source_connection, connect_failure],
        ):
            with self.assertRaises(sqlite3.OperationalError):
                readonly_snapshot._backup_sqlite(
                    Path("source.sqlite3"),
                    Path("destination.sqlite3"),
                )

        source_connection.close.assert_called_once()

    def test_snapshot_watcher_setup_failure_closes_current_connection(self):
        import readonly_snapshot

        connection = Mock()
        connection.execute.side_effect = sqlite3.OperationalError(
            "query_only failed"
        )
        sources = {
            name: Path(name)
            for name in readonly_snapshot.SQLITE_FILENAMES
        }
        with patch("readonly_snapshot.sqlite3.connect", return_value=connection):
            with self.assertRaises(sqlite3.OperationalError):
                readonly_snapshot._open_sqlite_watchers(sources)

        connection.close.assert_called_once()
    def test_core_paths_release_windows_file_handles_before_return(self):
        import observations_store
        import storage
        from analytics.report_lib import load_observations
        from readonly_snapshot import _backup_sqlite

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            observations = root / "observations.sqlite3"
            prices = root / "prices.db"

            observations_backup = root / "observations.backup.sqlite3"
            with capture_unraisable() as captured:
                observations_store.init_observations_db(observations)
                self.assertIsNone(
                    observations_store.load_fresh_observation_snapshot(
                        source="juhe",
                        origin_airport="PVG",
                        dest_airport="KIX",
                        depart_date="2099-10-01",
                        cabin_class="economy",
                        db_path=observations,
                    )
                )
                self.assertEqual(load_observations(observations), [])
                _backup_sqlite(observations, observations_backup)
                with patch.object(storage, "DB_PATH", prices):
                    storage.init_db()
                    with storage._connect() as connection:
                        self.assertEqual(connection.execute("SELECT 1").fetchone()[0], 1)

            self.assertEqual(captured, [])
            for database in (observations, prices, observations_backup):
                moved = database.with_suffix(database.suffix + ".moved")
                os.replace(database, moved)
                moved.unlink()
                for suffix in ("-wal", "-shm"):
                    sidecar = Path(str(database) + suffix)
                    if sidecar.exists():
                        sidecar.unlink()


if __name__ == "__main__":
    unittest.main()
