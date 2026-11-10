"""Readers and the watcher share one SQLite file without blocking each other."""

import json
from pathlib import Path
import shutil
import sqlite3
import tempfile
from threading import Thread
import time
import unittest
from urllib.request import urlopen

import api
import monitor


def observation(second: int) -> monitor.Check:
    return monitor.Check("https://example.com", f"2026-11-06T12:00:{second:02d}+00:00", "up", 204, 1.0, None)


class StorageTests(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.database = Path(directory.name) / "checks.sqlite3"

    def test_connect_uses_wal_full_synchronous_and_a_five_second_wait(self):
        connection = monitor.connect(self.database)
        self.addCleanup(connection.close)
        self.assertEqual(connection.execute("PRAGMA journal_mode").fetchone()[0], "wal")
        self.assertEqual(connection.execute("PRAGMA synchronous").fetchone()[0], 2)
        self.assertEqual(connection.execute("PRAGMA busy_timeout").fetchone()[0], 5000)
        reader = api.read_only(self.database)
        self.addCleanup(reader.close)
        self.assertEqual(reader.execute("PRAGMA journal_mode").fetchone()[0], "wal")
        self.assertEqual(reader.execute("PRAGMA busy_timeout").fetchone()[0], 5000)

    def test_an_older_rollback_database_keeps_its_rows_and_switches_to_wal(self):
        with sqlite3.connect(self.database) as legacy:
            legacy.execute("PRAGMA journal_mode=DELETE")
            legacy.execute(
                """CREATE TABLE checks (
                    id INTEGER PRIMARY KEY,
                    url TEXT NOT NULL,
                    checked_at TEXT NOT NULL,
                    state TEXT NOT NULL CHECK(state IN ('up', 'down')),
                    status_code INTEGER,
                    latency_ms REAL NOT NULL CHECK(latency_ms >= 0),
                    error TEXT
                )"""
            )
            legacy.execute(
                "INSERT INTO checks (url, checked_at, state, status_code, latency_ms, error) "
                "VALUES ('https://example.com', '2026-11-06T12:00:00+00:00', 'up', 204, 1, NULL)"
            )
        legacy.close()
        before = sqlite3.connect(self.database)
        self.assertEqual(before.execute("PRAGMA journal_mode").fetchone()[0], "delete")
        before.close()
        connection = monitor.connect(self.database)
        self.addCleanup(connection.close)
        self.assertEqual(connection.execute("PRAGMA journal_mode").fetchone()[0], "wal")
        self.assertEqual(monitor.history(connection), [{
            "id": 1, "url": "https://example.com", "checked_at": "2026-11-06T12:00:00+00:00",
            "state": "up", "status_code": 204, "latency_ms": 1, "error": None,
        }])
        connection.close()
        reopened = sqlite3.connect(self.database)
        self.addCleanup(reopened.close)
        self.assertEqual(reopened.execute("PRAGMA journal_mode").fetchone()[0], "wal")

    def test_an_open_reader_does_not_block_saving_a_check(self):
        connection = monitor.connect(self.database)
        self.addCleanup(connection.close)
        # Fail in well under a second if a reader can still block the commit.
        connection.execute("PRAGMA busy_timeout=300")
        monitor.save(connection, observation(0))
        reader = api.read_only(self.database)
        self.addCleanup(reader.close)
        reader.execute("BEGIN")
        self.assertEqual(reader.execute("SELECT COUNT(*) FROM checks").fetchone()[0], 1)
        started = time.monotonic()
        monitor.save(connection, observation(1))
        self.assertLess(time.monotonic() - started, 1)
        # The open reader keeps the snapshot from before the commit.
        self.assertEqual(reader.execute("SELECT COUNT(*) FROM checks").fetchone()[0], 1)
        fresh = api.read_only(self.database)
        self.addCleanup(fresh.close)
        self.assertEqual(fresh.execute("SELECT COUNT(*) FROM checks").fetchone()[0], 2)

    def test_an_exclusive_writer_does_not_block_a_reader(self):
        connection = monitor.connect(self.database)
        self.addCleanup(connection.close)
        monitor.save(connection, observation(0))
        connection.execute("BEGIN EXCLUSIVE")
        connection.execute(
            "INSERT INTO checks (url, checked_at, state, status_code, latency_ms, error) "
            "VALUES ('https://example.com', '2026-11-06T12:00:01+00:00', 'down', 503, 1, 'http_503')"
        )
        reader = api.read_only(self.database)
        self.addCleanup(reader.close)
        reader.execute("PRAGMA busy_timeout=300")
        started = time.monotonic()
        count = reader.execute("SELECT COUNT(*) FROM checks").fetchone()[0]
        self.assertLess(time.monotonic() - started, 1)
        self.assertEqual(count, 1)
        connection.rollback()

    def test_the_api_answers_while_a_write_transaction_is_open(self):
        connection = monitor.connect(self.database)
        self.addCleanup(connection.close)
        monitor.save(connection, observation(0))
        server = api.make_server(self.database, "127.0.0.1", 0, monitor)
        thread = Thread(target=server.serve_forever, daemon=True)
        thread.start()
        self.addCleanup(server.server_close)
        self.addCleanup(thread.join)
        self.addCleanup(server.shutdown)
        connection.execute("BEGIN EXCLUSIVE")
        connection.execute(
            "INSERT INTO checks (url, checked_at, state, status_code, latency_ms, error) "
            "VALUES ('https://example.com', '2026-11-06T12:00:01+00:00', 'down', 503, 1, 'http_503')"
        )
        with urlopen(f"http://127.0.0.1:{server.server_port}/api/history", timeout=2) as response:
            body = json.loads(response.read())
            self.assertEqual(response.status, 200)
        self.assertEqual([row["id"] for row in body], [1])
        connection.rollback()

    def test_a_copy_of_the_main_file_misses_checks_still_in_the_wal(self):
        connection = monitor.connect(self.database)
        self.addCleanup(connection.close)
        monitor.save(connection, observation(0))
        self.assertEqual(connection.execute("PRAGMA wal_checkpoint(TRUNCATE)").fetchone()[0], 0)
        monitor.save(connection, observation(1))
        copy = self.database.with_name("copy.sqlite3")
        shutil.copy(self.database, copy)
        copied = sqlite3.connect(copy)
        try:
            self.assertEqual(copied.execute("PRAGMA integrity_check").fetchone()[0], "ok")
            self.assertEqual(copied.execute("SELECT COUNT(*) FROM checks").fetchone()[0], 1)
        finally:
            copied.close()
        self.assertEqual(connection.execute("PRAGMA wal_checkpoint(TRUNCATE)").fetchone()[0], 0)
        shutil.copy(self.database, copy)
        copied = sqlite3.connect(copy)
        self.addCleanup(copied.close)
        self.assertEqual(copied.execute("PRAGMA integrity_check").fetchone()[0], "ok")
        self.assertEqual(copied.execute("SELECT COUNT(*) FROM checks").fetchone()[0], 2)


if __name__ == "__main__":
    unittest.main()
