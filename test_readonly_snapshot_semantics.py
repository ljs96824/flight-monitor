import json
import sqlite3
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

import readonly_snapshot


class ReadonlySnapshotSemanticsTest(unittest.TestCase):
    @staticmethod
    def _create_database(path, table):
        connection = sqlite3.connect(path)
        try:
            connection.execute(
                f"CREATE TABLE {table} (id INTEGER PRIMARY KEY, payload TEXT)"
            )
            connection.execute(
                f"INSERT INTO {table} (payload) VALUES ('fixture')"
            )
            connection.commit()
        finally:
            connection.close()

    def test_data_version_before_and_after_use_the_same_watcher_objects(self):
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
            watchers = {
                name: Mock(name=f"watcher-{name}")
                for name in readonly_snapshot.SQLITE_FILENAMES
            }
            for watcher in watchers.values():
                watcher.execute.return_value.fetchone.return_value = (7,)

            with patch(
                "readonly_snapshot._open_sqlite_watchers",
                return_value=watchers,
            ) as open_watchers:
                readonly_snapshot.create_readonly_snapshot(
                    "same-watchers",
                    source_dir=source,
                    output_root=output,
                )

            open_watchers.assert_called_once()
            for watcher in watchers.values():
                data_version_calls = [
                    call
                    for call in watcher.execute.call_args_list
                    if call.args == ("PRAGMA data_version",)
                ]
                self.assertEqual(len(data_version_calls), 2)
                watcher.close.assert_called_once()

    def test_source_connections_are_query_only_before_reading_or_backup(self):
        sources = {
            name: Path("unused") / name
            for name in readonly_snapshot.SQLITE_FILENAMES
        }
        watcher_connections = [Mock(), Mock()]
        for connection in watcher_connections:
            connection.execute.return_value.fetchone.return_value = (1,)
        with patch(
            "readonly_snapshot.sqlite3.connect",
            side_effect=watcher_connections,
        ):
            watchers = readonly_snapshot._open_sqlite_watchers(sources)
        try:
            for connection in watcher_connections:
                self.assertEqual(
                    connection.execute.call_args_list[0].args,
                    ("PRAGMA query_only=ON",),
                )
        finally:
            for connection in watchers.values():
                connection.close()

        source_connection = Mock()
        destination_connection = Mock()
        destination_connection.execute.return_value.fetchone.return_value = ("ok",)
        with patch(
            "readonly_snapshot.sqlite3.connect",
            side_effect=[source_connection, destination_connection],
        ):
            readonly_snapshot._backup_sqlite(
                Path("source.sqlite3"),
                Path("destination.sqlite3"),
            )
        self.assertEqual(
            source_connection.execute.call_args_list[0].args,
            ("PRAGMA query_only=ON",),
        )
        self.assertNotIn(
            ("PRAGMA query_only=ON",),
            [entry.args for entry in destination_connection.execute.call_args_list],
        )
        source_connection.backup.assert_called_once()


if __name__ == "__main__":
    unittest.main()
