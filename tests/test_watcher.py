"""The watcher heartbeat shows a live wait, and a wait that has already passed."""

from contextlib import closing, redirect_stderr, redirect_stdout
from datetime import datetime, timedelta, timezone
import io
import json
from pathlib import Path
import sqlite3
import tempfile
from threading import Thread
import unittest
from unittest.mock import patch
from urllib.error import HTTPError
from urllib.request import urlopen

import api
import monitor

URL = "https://example.com"
WHEN = datetime(2026, 11, 13, 12, 0, tzinfo=timezone.utc)


def observation(state="up"):
    return monitor.Check(URL, "2026-11-13T12:00:00+00:00", state, 200 if state == "up" else 503, 1.0,
                         None if state == "up" else "http_503")


class WatcherTests(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.database = Path(directory.name) / "checks.sqlite3"
        self.connection = monitor.connect(self.database)
        self.addCleanup(self.connection.close)
        monitor.add_target(self.connection, "api", URL)

    def cli(self, *arguments):
        output, errors = io.StringIO(), io.StringIO()
        with redirect_stdout(output), redirect_stderr(errors):
            code = monitor.main(["--database", str(self.database), *arguments])
        return code, output.getvalue(), errors.getvalue()

    def test_nothing_has_run_until_watch_does(self):
        code, output, errors = self.cli("watcher")
        self.assertEqual(code, 0)
        self.assertEqual(errors, "")
        self.assertEqual(json.loads(output), monitor.watcher_status(self.connection))
        self.assertEqual(json.loads(output)["state"], "absent")
        with patch.object(monitor, "probe", return_value=observation()), redirect_stdout(io.StringIO()):
            self.assertEqual(self.cli("run", "api")[0], 0)
            self.assertEqual(self.cli("run-all")[0], 0)
        self.assertEqual(monitor.watcher_status(self.connection)["state"], "absent")
        self.assertEqual(self.connection.execute("SELECT COUNT(*) FROM watcher").fetchone()[0], 0)
        with patch.object(monitor, "probe") as probe, self.assertRaises(ValueError):
            list(monitor.watch_targets(self.connection, interval=0))
        probe.assert_not_called()
        self.assertEqual(monitor.watcher_status(self.connection)["state"], "absent")
        with self.assertRaisesRegex(ValueError, "timezone"):
            monitor.watcher_status(self.connection, now=datetime(2026, 11, 13, 12, 0))

    def test_a_finished_watch_is_stopped_and_a_later_one_replaces_it(self):
        clock = {"at": WHEN}

        def now():
            clock["at"] += timedelta(minutes=1)
            return clock["at"]

        with patch.object(monitor, "probe", return_value=observation()), \
                patch.object(monitor, "sleep"), patch.object(monitor, "_now", side_effect=now), \
                redirect_stdout(io.StringIO()):
            self.assertEqual(self.cli("watch", "--interval", "5", "--count", "2")[0], 0)
            first = monitor.watcher_status(self.connection, now=clock["at"])
            self.assertEqual(self.cli("watch", "--interval", "9", "--count", "1")[0], 0)
            second = monitor.watcher_status(self.connection, now=clock["at"])
        self.assertEqual((first["state"], first["phase"], first["rounds"], first["interval"]),
                         ("stopped", "stopped", 2, 5))
        self.assertIs(first["late"], False)
        self.assertIsNone(first["due_at"])
        self.assertEqual((second["state"], second["rounds"], second["interval"]), ("stopped", 1, 9))
        self.assertGreater(second["started_at"], first["started_at"])
        reopened = monitor.connect(self.database)
        self.addCleanup(reopened.close)
        self.assertEqual(monitor.watcher_status(reopened, now=clock["at"])["rounds"], 1)

    def test_a_wait_is_late_only_after_it_is_due_and_a_check_never_is(self):
        seen = {}

        def during_checks(_connection, _workers, _deadline):
            # The first round has started and not finished, so an old beat is still not late.
            if "checking" not in seen:
                seen["checking"] = monitor.watcher_status(
                    self.connection, now=datetime(2030, 1, 1, tzinfo=timezone.utc))
            return []

        def during_wait(_seconds):
            waiting = monitor.watcher_status(self.connection)
            beat = datetime.fromisoformat(waiting["beat_at"])
            with closing(api.read_only(self.database)) as reader:
                self.assertEqual(monitor.watcher_status(reader, now=beat),
                                 monitor.watcher_status(self.connection, now=beat))
            due = datetime.fromisoformat(waiting["due_at"])
            seen["span"] = due - beat
            seen["on_time"] = monitor.watcher_status(self.connection, now=due)
            seen["late"] = monitor.watcher_status(self.connection, now=due + timedelta(microseconds=1))
            seen["behind"] = monitor.watcher_status(self.connection, now=beat - timedelta(seconds=5))
            code, output, _errors = self.cli("watcher")
            seen["cli"] = (code, json.loads(output)["phase"])

        with patch.object(monitor, "run_targets", side_effect=during_checks), \
                patch.object(monitor, "sleep", side_effect=during_wait):
            batches = list(monitor.watch_targets(self.connection, interval=1.5, count=2, workers=1))
        self.assertEqual([batch["results"] for batch in batches], [[], []])
        self.assertEqual(seen["checking"]["phase"], "checking")
        self.assertIs(seen["checking"]["late"], False)
        self.assertIsNone(seen["checking"]["due_at"])
        self.assertEqual(seen["checking"]["rounds"], 0)
        self.assertGreater(seen["checking"]["quiet_seconds"], 3600)
        self.assertEqual(seen["span"], timedelta(seconds=1.5))
        self.assertIs(seen["on_time"]["late"], False)
        self.assertEqual(seen["on_time"]["quiet_seconds"], 1)
        self.assertIs(seen["late"]["late"], True)
        self.assertEqual((seen["late"]["state"], seen["late"]["phase"], seen["late"]["rounds"]),
                         ("running", "waiting", 1))
        self.assertEqual(seen["behind"]["quiet_seconds"], 0)
        self.assertIs(seen["behind"]["late"], False)
        self.assertEqual(seen["cli"], (0, "waiting"))
        stopped = monitor.watcher_status(self.connection, now=WHEN + timedelta(days=1))
        self.assertEqual((stopped["state"], stopped["phase"], stopped["rounds"]), ("stopped", "stopped", 2))
        self.assertIs(stopped["late"], False)

    def test_a_paused_fleet_still_beats_and_an_empty_list_stops(self):
        monitor.update_target(self.connection, "api", paused=True)
        beats = []

        def during_wait(_seconds):
            beats.append(monitor.watcher_status(self.connection)["rounds"])

        with patch.object(monitor, "probe") as probe, patch.object(monitor, "sleep", side_effect=during_wait):
            batches = list(monitor.watch_targets(self.connection, interval=4, count=2))
        probe.assert_not_called()
        self.assertEqual(batches, [{"cycle": 1, "results": []}, {"cycle": 2, "results": []}])
        self.assertEqual(beats, [1])
        self.assertEqual(monitor.history(self.connection), [])
        self.assertEqual(monitor.watcher_status(self.connection)["rounds"], 2)
        monitor.remove_target(self.connection, "api")
        with patch.object(monitor, "probe") as probe, patch.object(monitor, "sleep") as sleep:
            self.assertEqual(list(monitor.watch_targets(self.connection)), [{"cycle": 1, "results": []}])
        probe.assert_not_called()
        sleep.assert_not_called()
        self.assertEqual(monitor.watcher_status(self.connection)["rounds"], 1)

    def test_an_interrupt_or_a_failed_round_records_stopped(self):
        for interruption, code in [(KeyboardInterrupt, 130), (monitor.ShutdownRequested, 143)]:
            with self.subTest(interruption=interruption), \
                    patch.object(monitor, "probe", return_value=observation()), \
                    patch.object(monitor, "sleep", side_effect=interruption), \
                    redirect_stdout(io.StringIO()), redirect_stderr(io.StringIO()):
                before = len(monitor.history(self.connection))
                self.assertEqual(self.cli("watch", "--interval", "30")[0], code)
                status = monitor.watcher_status(self.connection)
                self.assertEqual((status["state"], status["rounds"], status["late"]), ("stopped", 1, False))
                self.assertEqual(len(monitor.history(self.connection)), before + 1)
        with patch.object(monitor, "run_targets", side_effect=sqlite3.OperationalError("locked")), \
                redirect_stderr(io.StringIO()):
            self.assertEqual(self.cli("watch")[0], 2)
        status = monitor.watcher_status(self.connection)
        self.assertEqual((status["state"], status["rounds"]), ("stopped", 0))

    def test_a_stored_beat_that_is_not_a_timestamp_is_rejected(self):
        with patch.object(monitor, "probe", return_value=observation()), patch.object(monitor, "sleep"), \
                redirect_stdout(io.StringIO()):
            self.assertEqual(self.cli("watch", "--count", "1")[0], 0)
        with self.assertRaises(sqlite3.IntegrityError):
            with self.connection:
                self.connection.execute("UPDATE watcher SET phase = 'waiting', due_at = NULL")
        self.assertEqual(monitor.watcher_status(self.connection)["state"], "stopped")
        with self.connection:
            self.connection.execute("UPDATE watcher SET beat_at = 'yesterday'")
        with self.assertRaisesRegex(ValueError, "beat_at"):
            monitor.watcher_status(self.connection)
        code, _output, errors = self.cli("watcher")
        self.assertEqual(code, 2)
        self.assertIn("beat_at", errors)

    def test_an_older_database_reads_as_absent_and_keeps_its_rows(self):
        legacy_path = self.database.with_name("legacy.sqlite3")
        with sqlite3.connect(legacy_path) as legacy:
            legacy.execute(
                """CREATE TABLE checks (
                    id INTEGER PRIMARY KEY, url TEXT NOT NULL, checked_at TEXT NOT NULL,
                    state TEXT NOT NULL, status_code INTEGER, latency_ms REAL NOT NULL, error TEXT)""")
            legacy.execute("CREATE TABLE targets (name TEXT PRIMARY KEY, url TEXT NOT NULL, timeout REAL NOT NULL)")
            legacy.execute("INSERT INTO targets VALUES ('api', ?, 5)", (URL,))
            legacy.execute(
                "INSERT INTO checks (url, checked_at, state, latency_ms) "
                "VALUES (?, '2026-11-01T00:00:00+00:00', 'up', 1)", (URL,))
        legacy.close()
        upgraded = monitor.connect(legacy_path)
        self.addCleanup(upgraded.close)
        self.assertEqual(monitor.watcher_status(upgraded)["state"], "absent")
        self.assertEqual(monitor.history(upgraded)[0]["url"], URL)
        self.assertIn("watcher", {row["name"] for row in upgraded.execute(
            "SELECT name FROM sqlite_master WHERE type = 'table'")})

    def test_the_api_matches_the_report_and_does_not_write(self):
        server = api.make_server(self.database, "127.0.0.1", 0, monitor)
        thread = Thread(target=server.serve_forever, daemon=True)
        thread.start()
        self.addCleanup(server.server_close)
        self.addCleanup(thread.join)
        self.addCleanup(server.shutdown)
        base = f"http://127.0.0.1:{server.server_port}"

        def get(path):
            with urlopen(base + path, timeout=5) as response:
                return response.status, json.loads(response.read())

        self.assertEqual(get("/api/watcher"), (200, monitor.watcher_status(self.connection, now=WHEN)))
        with self.assertRaises(HTTPError) as failure:
            urlopen(base + "/api/watcher?verbose=1", timeout=5)
        self.assertEqual(failure.exception.code, 400)
        self.assertIn(b"Unknown query parameter: verbose", failure.exception.read())
        with patch.object(monitor, "probe", return_value=observation()), patch.object(monitor, "sleep"), \
                redirect_stdout(io.StringIO()):
            list(monitor.watch_targets(self.connection, interval=8, count=1))
        fixed = WHEN + timedelta(hours=2)
        with patch.object(monitor, "_now", return_value=fixed):
            status, body = get("/api/watcher")
        self.assertEqual((status, body), (200, monitor.watcher_status(self.connection, now=fixed)))
        self.assertEqual(body["state"], "stopped")
        with self.connection:
            self.connection.execute("UPDATE watcher SET beat_at = 'yesterday'")
        with self.assertRaises(HTTPError) as failure:
            urlopen(base + "/api/watcher", timeout=5)
        self.assertEqual(failure.exception.code, 400)
        self.assertIn(b"beat_at", failure.exception.read())


if __name__ == "__main__":
    unittest.main()
