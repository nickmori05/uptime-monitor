from contextlib import redirect_stderr, redirect_stdout
from io import StringIO
import json
from pathlib import Path
import sqlite3
import tempfile
import unittest

import monitor

URL = "https://example.com"


class ObjectiveSettingTests(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.database = Path(directory.name) / "checks.sqlite3"
        self.connection = monitor.connect(self.database)
        self.addCleanup(self.connection.close)

    def cli(self, *arguments):
        output, errors = StringIO(), StringIO()
        with redirect_stdout(output), redirect_stderr(errors):
            code = monitor.main(["--database", str(self.database), *arguments])
        return code, output.getvalue(), errors.getvalue()

    def test_targets_save_change_and_clear_an_objective(self):
        code, output, _ = self.cli("add", "api", URL, "--objective", "99.9")
        self.assertEqual(code, 0)
        self.assertEqual(json.loads(output)["objective"], 99.9)
        self.assertEqual(monitor.get_target(self.connection, "api")["objective"], 99.9)
        self.assertEqual(json.loads(self.cli("update", "api", "--objective", "99.5")[1])["objective"], 99.5)
        # Changing something else keeps the objective.
        self.assertEqual(json.loads(self.cli("update", "api", "--timeout", "2")[1])["objective"], 99.5)
        self.assertIsNone(json.loads(self.cli("update", "api", "--no-objective")[1])["objective"])
        self.assertIsNone(monitor.add_target(self.connection, "plain", URL)["objective"])

    def test_objectives_must_be_a_percentage_below_100(self):
        for value in (0, 100, -1, 100.5, float("nan"), float("inf"), True):
            with self.subTest(value=value), self.assertRaisesRegex(ValueError, "Objective"):
                monitor.add_target(self.connection, "api", URL, objective=value)
        self.assertEqual(monitor.list_targets(self.connection), [])
        monitor.add_target(self.connection, "api", URL, objective=50)
        with self.assertRaisesRegex(ValueError, "not both"):
            monitor.update_target(self.connection, "api", objective=99.0, clear_objective=True)
        for flags in (["--objective", "100"], ["--objective", "nan"]):
            with self.subTest(flags=flags):
                code, _, errors = self.cli("update", "api", *flags)
                self.assertEqual(code, 2)
                self.assertIn("Objective must be", errors)
        with redirect_stderr(StringIO()), self.assertRaises(SystemExit) as stopped:
            monitor.main(["--database", str(self.database), "update", "api", "--objective", "1", "--no-objective"])
        self.assertEqual(stopped.exception.code, 2)
        self.assertEqual(monitor.get_target(self.connection, "api")["objective"], 50)
        # The table refuses a bad value even when it skips validation.
        with self.assertRaises(sqlite3.IntegrityError):
            self.connection.execute("UPDATE targets SET objective = 100")

    def test_older_databases_gain_an_empty_objective(self):
        self.connection.close()
        self.database.unlink()
        with sqlite3.connect(self.database) as legacy:
            legacy.execute("CREATE TABLE targets (name TEXT PRIMARY KEY, url TEXT NOT NULL, timeout REAL NOT NULL, "
                           "deadline REAL, alert_webhook TEXT NOT NULL DEFAULT 'default')")
            legacy.execute("INSERT INTO targets (name, url, timeout) VALUES ('api', 'https://example.com', 3)")
        legacy.close()
        self.connection = monitor.connect(self.database)
        self.assertIsNone(monitor.get_target(self.connection, "api")["objective"])
        self.assertEqual(monitor.update_target(self.connection, "api", objective=99.0)["objective"], 99.0)


if __name__ == "__main__":
    unittest.main()
