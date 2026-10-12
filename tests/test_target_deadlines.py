from contextlib import redirect_stderr, redirect_stdout
import io
import json
from pathlib import Path
import sqlite3
import tempfile
import unittest
from unittest.mock import patch

import monitor


class TargetDeadlineTests(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.database = Path(directory.name) / "checks.sqlite3"
        self.connection = monitor.connect(self.database)
        self.addCleanup(self.connection.close)

    def cli(self, *arguments):
        output = io.StringIO()
        with redirect_stdout(output), redirect_stderr(io.StringIO()):
            code = monitor.main(["--database", str(self.database), *arguments])
        return code, output.getvalue()

    def test_saved_deadline_survives_reopening(self):
        code, output = self.cli("add", "api", "https://example.com", "--timeout", "3", "--deadline", "10")
        self.assertEqual(code, 0)
        expected = {"name": "api", "url": "https://example.com", "timeout": 3, "deadline": 10}
        self.assertEqual(json.loads(output), expected)
        reopened = monitor.connect(self.database)
        self.addCleanup(reopened.close)
        self.assertEqual(monitor.get_target(reopened, "api"), expected)

    def test_invalid_saved_deadlines_are_rejected(self):
        for deadline in ("0", "-1", "61", "nan", "inf"):
            with self.subTest(deadline=deadline):
                self.assertEqual(self.cli("add", "api", "https://example.com", "--deadline", deadline)[0], 2)
        self.assertEqual(monitor.list_targets(self.connection), [])
        with self.assertRaises(sqlite3.IntegrityError), self.connection:
            self.connection.execute("INSERT INTO targets VALUES ('raw', 'https://example.com', 5, 0)")

    def test_update_sets_keeps_and_clears_the_deadline(self):
        monitor.add_target(self.connection, "api", "https://example.com", 3)
        for flags, expected in ((["--deadline", "8"], 8), (["--timeout", "2"], 8),
                                (["--no-deadline"], None), (["--timeout", "4"], None)):
            with self.subTest(flags=flags):
                code, output = self.cli("update", "api", *flags)
                self.assertEqual(code, 0)
                self.assertEqual(json.loads(output)["deadline"], expected)
                self.assertEqual(monitor.get_target(self.connection, "api")["deadline"], expected)

    def test_invalid_deadline_updates_leave_settings_unchanged(self):
        original = monitor.add_target(self.connection, "api", "https://example.com", 3, 8)
        for flags in (["--deadline", "0"], ["--deadline", "61"], ["--timeout", "2", "--deadline", "nan"]):
            with self.subTest(flags=flags):
                self.assertEqual(self.cli("update", "api", *flags)[0], 2)
                self.assertEqual(monitor.get_target(self.connection, "api"), original)
        with self.assertRaises(SystemExit) as exit:
            self.cli("update", "api", "--deadline", "5", "--no-deadline")
        self.assertEqual(exit.exception.code, 2)
        self.assertEqual(monitor.get_target(self.connection, "api"), original)
        with self.assertRaisesRegex(ValueError, "not both"):
            monitor.update_target(self.connection, "api", deadline=5, clear_deadline=True)

    def test_databases_without_the_deadline_column_are_upgraded(self):
        legacy_path = self.database.with_name("legacy.sqlite3")
        with sqlite3.connect(legacy_path) as legacy:
            legacy.execute("CREATE TABLE targets (name TEXT PRIMARY KEY, url TEXT NOT NULL, "
                           "timeout REAL NOT NULL CHECK(timeout > 0 AND timeout <= 60))")
            legacy.execute("INSERT INTO targets VALUES ('api', 'https://example.com', 2)")
        legacy.close()
        upgraded = monitor.connect(legacy_path)
        self.addCleanup(upgraded.close)
        self.assertEqual(monitor.get_target(upgraded, "api")["deadline"], None)
        monitor.update_target(upgraded, "api", deadline=6)
        self.assertEqual(monitor.get_target(upgraded, "api")["deadline"], 6)
        monitor.connect(legacy_path).close()


if __name__ == "__main__":
    unittest.main()
