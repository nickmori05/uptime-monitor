"""`health` says whether the watcher is running and still beating."""

from contextlib import closing, redirect_stderr, redirect_stdout
from datetime import datetime, timedelta, timezone
import io
import json
from pathlib import Path
import tempfile
import threading
import unittest
from unittest.mock import patch

import monitor

URL = "https://example.com"
WHEN = datetime(2026, 11, 22, 12, 0, tzinfo=timezone.utc)


def observation():
    return monitor.Check(URL, "2026-11-22T12:00:00+00:00", "up", 200, 1.0, None)


class WatcherHealthTests(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.database = Path(directory.name) / "checks.sqlite3"
        self.connection = monitor.connect(self.database)
        self.addCleanup(self.connection.close)

    def cli(self, *arguments):
        output, errors = io.StringIO(), io.StringIO()
        with redirect_stdout(output), redirect_stderr(errors):
            code = monitor.main(["--database", str(self.database), *arguments])
        return code, output.getvalue(), errors.getvalue()

    def save_beat(self, *, state="running", phase="checking", seconds_ago=0, due_at=None, rounds=1):
        beat_at = WHEN - timedelta(seconds=seconds_ago)
        with self.connection:
            self.connection.execute("DELETE FROM watcher")
            self.connection.execute(
                """INSERT INTO watcher (id, state, phase, interval, started_at, beat_at, rounds, due_at)
                   VALUES (1, ?, ?, 30, ?, ?, ?, ?)""",
                (state, phase, beat_at.isoformat(), beat_at.isoformat(), rounds, due_at),
            )

    def health_at(self, **beat):
        self.save_beat(**beat)
        return monitor.watcher_health(self.connection, now=WHEN)

    def test_only_a_running_watcher_that_is_still_beating_is_ok(self):
        absent = monitor.watcher_health(self.connection, now=WHEN)
        self.assertEqual(absent, {"ok": False, "state": "absent", "stale": False, "quiet_seconds": None})
        fresh = self.health_at(seconds_ago=4)
        self.assertEqual(fresh, {"ok": True, "state": "running", "stale": False, "quiet_seconds": 4})
        self.assertEqual(self.health_at(seconds_ago=14.9),
                         {"ok": True, "state": "running", "stale": False, "quiet_seconds": 14})
        self.assertEqual(self.health_at(seconds_ago=15),
                         {"ok": False, "state": "running", "stale": True, "quiet_seconds": 15})
        self.assertEqual(self.health_at(state="stopped", phase="stopped", seconds_ago=4),
                         {"ok": False, "state": "stopped", "stale": False, "quiet_seconds": 4})
        # A clock behind the beat is not stale, so the watcher still counts as alive.
        self.assertEqual(self.health_at(seconds_ago=-5),
                         {"ok": True, "state": "running", "stale": False, "quiet_seconds": 0})

    def test_a_late_wait_stays_ok_while_the_beat_is_fresh(self):
        due = (WHEN - timedelta(seconds=1)).isoformat()
        report = self.health_at(phase="waiting", due_at=due, seconds_ago=2)
        self.assertEqual(report, {"ok": True, "state": "running", "stale": False, "quiet_seconds": 2})
        self.assertIs(monitor.watcher_status(self.connection, now=WHEN)["late"], True)
        stale = self.health_at(phase="waiting", due_at=due, seconds_ago=15)
        self.assertEqual(stale, {"ok": False, "state": "running", "stale": True, "quiet_seconds": 15})
        self.assertIs(monitor.watcher_status(self.connection, now=WHEN)["late"], True)

    def test_the_command_exits_with_the_report_and_does_not_change_the_beat(self):
        code, output, errors = self.cli("health")
        self.assertEqual((code, errors), (1, ""))
        self.assertEqual(json.loads(output), {"ok": False, "state": "absent", "stale": False,
                                               "quiet_seconds": None})
        self.assertEqual(self.connection.execute("SELECT COUNT(*) FROM watcher").fetchone()[0], 0)

        self.save_beat(seconds_ago=0)
        before = self.connection.execute("SELECT beat_at, rounds, phase FROM watcher").fetchone()
        with patch.object(monitor, "_now", return_value=WHEN):
            code, output, errors = self.cli("health")
        self.assertEqual((code, errors), (0, ""))
        self.assertEqual(json.loads(output), monitor.watcher_health(self.connection, now=WHEN))
        self.assertIs(json.loads(output)["ok"], True)
        after = self.connection.execute("SELECT beat_at, rounds, phase FROM watcher").fetchone()
        self.assertEqual(tuple(after), tuple(before))

        self.save_beat(seconds_ago=15)
        with patch.object(monitor, "_now", return_value=WHEN), patch.object(monitor, "probe") as probe:
            code, output, errors = self.cli("health")
        probe.assert_not_called()
        self.assertEqual((code, errors), (1, ""))
        self.assertEqual(json.loads(output), {"ok": False, "state": "running", "stale": True, "quiet_seconds": 15})

        self.save_beat(state="stopped", phase="stopped", seconds_ago=1)
        with patch.object(monitor, "_now", return_value=WHEN):
            code, output, _errors = self.cli("health")
        self.assertEqual(code, 1)
        self.assertEqual(json.loads(output)["state"], "stopped")
        self.assertIs(json.loads(output)["ok"], False)

    def test_a_beat_that_is_not_a_timestamp_is_an_error(self):
        self.save_beat()
        with self.connection:
            self.connection.execute("UPDATE watcher SET beat_at = 'yesterday'")
        with self.assertRaisesRegex(ValueError, "beat_at"):
            monitor.watcher_health(self.connection)
        code, output, errors = self.cli("health")
        self.assertEqual(code, 2)
        self.assertEqual(output, "")
        self.assertIn("beat_at", errors)

    def test_a_beating_watch_is_healthy_until_it_stops(self):
        monitor.add_target(self.connection, "api", URL)
        release, entered = threading.Event(), threading.Event()
        errors = []

        def probe(*_args):
            entered.set()
            if not release.wait(5):
                raise AssertionError("probe was not released")
            return observation()

        def run():
            connection = monitor.connect(self.database)
            try:
                with patch.object(monitor, "probe", side_effect=probe), patch.object(monitor, "sleep"):
                    list(monitor.watch_targets(connection, interval=30, count=1, workers=1, pulse_every=0.05))
            except Exception as error:
                errors.append(error)
            finally:
                connection.close()

        worker = threading.Thread(target=run)
        worker.start()
        self.addCleanup(lambda: (release.set(), worker.join(timeout=5)))
        self.assertTrue(entered.wait(5))
        with closing(monitor.connect(self.database)) as reader:
            self.assertIs(monitor.watcher_health(reader)["ok"], True)
        code, output, errors_text = self.cli("health")
        self.assertEqual((code, errors_text), (0, ""))
        self.assertIs(json.loads(output)["ok"], True)
        self.assertEqual(json.loads(output)["state"], "running")
        release.set()
        worker.join(5)
        self.assertFalse(worker.is_alive())
        self.assertEqual(errors, [])
        code, output, _errors = self.cli("health")
        self.assertEqual(code, 1)
        stopped = json.loads(output)
        self.assertEqual((stopped["ok"], stopped["state"], stopped["stale"]), (False, "stopped", False))

    def test_an_unreadable_database_exits_two(self):
        self.connection.close()
        self.database.unlink()
        self.database.mkdir()
        code, output, errors = self.cli("health")
        self.assertEqual(code, 2)
        self.assertEqual(output, "")
        self.assertIn("Error:", errors)


if __name__ == "__main__":
    unittest.main()
