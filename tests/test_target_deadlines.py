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
        expected = {"name": "api", "url": "https://example.com", "timeout": 3, "deadline": 10,
                    "alert_webhook": "default", "objective": None}
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
            self.connection.execute("INSERT INTO targets (name, url, timeout, deadline) "
                                    "VALUES ('raw', 'https://example.com', 5, 0)")

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

    def result(self, url, *_):
        return monitor.Check(url, "2026-10-12T00:00:00+00:00", "up", 200, 1.0, None)

    def test_run_uses_the_saved_deadline_unless_one_is_given(self):
        monitor.add_target(self.connection, "api", "https://example.com", 3, 8)
        monitor.add_target(self.connection, "plain", "https://example.com/plain", 3)
        for arguments, expected in ((["run", "api"], ("https://example.com", 3, 8)),
                                    (["run", "api", "--deadline", "2"], ("https://example.com", 3, 2)),
                                    (["run", "plain"], ("https://example.com/plain", 3, None))):
            with self.subTest(arguments=arguments), patch("monitor.probe", side_effect=self.result) as probe:
                self.assertEqual(self.cli(*arguments)[0], 0)
                probe.assert_called_once_with(*expected)
        self.assertEqual(monitor.get_target(self.connection, "api")["deadline"], 8)

    def test_batches_mix_saved_deadlines_and_apply_an_override_to_all(self):
        monitor.add_target(self.connection, "api", "https://example.com/api", 3, 8)
        monitor.add_target(self.connection, "web", "https://example.com/web", 2)
        with patch("monitor.probe", side_effect=self.result) as probe:
            monitor.run_targets(self.connection)
        self.assertEqual({call.args for call in probe.call_args_list},
                         {("https://example.com/api", 3, 8), ("https://example.com/web", 2, None)})
        with patch("monitor.probe", side_effect=self.result) as probe:
            self.assertEqual(self.cli("run-all", "--deadline", "4")[0], 0)
        self.assertEqual({call.args[2] for call in probe.call_args_list}, {4})

    def test_watcher_picks_up_deadline_changes_between_rounds(self):
        monitor.add_target(self.connection, "api", "https://example.com", 3)
        seen = []

        def probe(url, timeout, deadline):
            seen.append(deadline)
            return self.result(url)

        def change_deadline(seconds):
            monitor.update_target(self.connection, "api", deadline=5)

        with patch("monitor.probe", side_effect=probe), patch("monitor.sleep", side_effect=change_deadline):
            list(monitor.watch_targets(self.connection, interval=1, count=2))
        self.assertEqual(seen, [None, 5])

    def test_out_of_range_stored_deadline_stops_the_batch_before_requests(self):
        monitor.add_target(self.connection, "api", "https://example.com", 3)
        with patch("monitor.list_targets", return_value=[
                {"name": "api", "url": "https://example.com", "timeout": 3, "deadline": 0}]), \
                patch("monitor.probe") as probe:
            with self.assertRaises(ValueError):
                monitor.run_targets(self.connection)
            self.assertEqual(self.cli("run-all")[0], 2)
        probe.assert_not_called()


if __name__ == "__main__":
    unittest.main()
