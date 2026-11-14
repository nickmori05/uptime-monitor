"""The watcher heartbeat shows a live wait, a missed one, and a beat that has stopped."""

from contextlib import closing, redirect_stderr, redirect_stdout
from datetime import datetime, timedelta, timezone
import io
import json
from pathlib import Path
import sqlite3
import tempfile
import threading
import time
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
        self.assertIs(json.loads(output)["stale"], False)
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
        self.assertIs(seen["checking"]["stale"], True)
        self.assertIsNone(seen["checking"]["due_at"])
        self.assertEqual(seen["checking"]["rounds"], 0)
        self.assertGreater(seen["checking"]["quiet_seconds"], 3600)
        self.assertEqual(seen["span"], timedelta(seconds=1.5))
        self.assertIs(seen["on_time"]["late"], False)
        self.assertIs(seen["on_time"]["stale"], False)
        self.assertEqual(seen["on_time"]["quiet_seconds"], 1)
        self.assertIs(seen["late"]["late"], True)
        self.assertIs(seen["late"]["stale"], False)
        self.assertEqual((seen["late"]["state"], seen["late"]["phase"], seen["late"]["rounds"]),
                         ("running", "waiting", 1))
        self.assertEqual(seen["behind"]["quiet_seconds"], 0)
        self.assertIs(seen["behind"]["late"], False)
        self.assertIs(seen["behind"]["stale"], False)
        self.assertEqual(seen["cli"], (0, "waiting"))
        stopped = monitor.watcher_status(self.connection, now=WHEN + timedelta(days=1))
        self.assertEqual((stopped["state"], stopped["phase"], stopped["rounds"]), ("stopped", "stopped", 2))
        self.assertIs(stopped["late"], False)
        self.assertIs(stopped["stale"], False)

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
                self.assertEqual((status["state"], status["rounds"], status["late"], status["stale"]),
                                 ("stopped", 1, False, False))
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
        thread = threading.Thread(target=server.serve_forever, daemon=True)
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
        self.assertIs(body["stale"], False)
        with self.connection:
            self.connection.execute("UPDATE watcher SET beat_at = 'yesterday'")
        with self.assertRaises(HTTPError) as failure:
            urlopen(base + "/api/watcher", timeout=5)
        self.assertEqual(failure.exception.code, 400)
        self.assertIn(b"beat_at", failure.exception.read())

    def test_silence_is_stale_only_while_the_watcher_is_running(self):
        self.assertEqual((monitor.PULSE_EVERY, monitor.STALE_AFTER), (5.0, 15))
        beat = WHEN
        due = beat + timedelta(seconds=30)
        with self.connection:
            self.connection.execute(
                """INSERT INTO watcher (id, state, phase, interval, started_at, beat_at, rounds, due_at)
                   VALUES (1, 'running', 'checking', 30, ?, ?, 1, NULL)""",
                (beat.isoformat(), beat.isoformat()))

        def at(seconds):
            return monitor.watcher_status(self.connection, now=beat + timedelta(seconds=seconds))

        self.assertEqual((at(14.9)["quiet_seconds"], at(14.9)["stale"], at(14.9)["late"]), (14, False, False))
        self.assertEqual((at(15)["quiet_seconds"], at(15)["stale"], at(15)["late"]), (15, True, False))
        behind = monitor.watcher_status(self.connection, now=beat - timedelta(seconds=5))
        self.assertEqual((behind["quiet_seconds"], behind["stale"], behind["late"]), (0, False, False))
        with self.connection:
            self.connection.execute(
                "UPDATE watcher SET phase = 'waiting', due_at = ? WHERE id = 1", (due.isoformat(),))
        waiting = at(15)
        self.assertEqual((waiting["phase"], waiting["stale"], waiting["late"]), ("waiting", True, False))
        # A beat during the wait leaves the due time where it was, so the round can be late
        # while the process is still writing.
        fresh = due + timedelta(seconds=1)
        with self.connection:
            self.connection.execute("UPDATE watcher SET beat_at = ? WHERE id = 1", (fresh.isoformat(),))
        overdue = monitor.watcher_status(self.connection, now=fresh + timedelta(seconds=1))
        self.assertEqual((overdue["late"], overdue["stale"], overdue["quiet_seconds"], overdue["due_at"]),
                         (True, False, 1, due.isoformat()))
        with self.connection:
            self.connection.execute(
                "UPDATE watcher SET state = 'stopped', phase = 'stopped', due_at = NULL WHERE id = 1")
        stopped = monitor.watcher_status(self.connection, now=fresh + timedelta(days=1))
        self.assertEqual((stopped["state"], stopped["stale"], stopped["late"]), ("stopped", False, False))

    def test_a_blocked_check_keeps_beating_and_a_finished_watch_does_not(self):
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
        first = self.read_watcher()
        self.assertEqual((first["state"], first["phase"], first["rounds"], first["late"], first["stale"]),
                         ("running", "checking", 0, False, False))
        self.assertIsNone(first["due_at"])
        moved = self.until_beat_moves(first["beat_at"])
        self.assertEqual((moved["phase"], moved["rounds"], moved["started_at"], moved["due_at"]),
                         ("checking", 0, first["started_at"], None))
        self.assertIs(moved["stale"], False)
        self.assertLess(moved["quiet_seconds"], monitor.STALE_AFTER)
        release.set()
        worker.join(5)
        self.assertFalse(worker.is_alive())
        self.assertEqual(errors, [])
        stopped = self.read_watcher()
        self.assertEqual((stopped["state"], stopped["stale"]), ("stopped", False))
        time.sleep(0.25)
        self.assertEqual(self.read_watcher()["beat_at"], stopped["beat_at"])
        self.assertEqual(self.pulse_threads(), [])

    def test_a_wait_keeps_its_due_time_while_the_beat_moves(self):
        release, held = threading.Event(), threading.Event()
        errors = []

        def during_wait(seconds):
            if seconds != 30:
                raise AssertionError(f"wait was {seconds}, not 30")
            held.set()
            if not release.wait(5):
                raise AssertionError("wait was not released")

        def run():
            connection = monitor.connect(self.database)
            try:
                with patch.object(monitor, "probe", return_value=observation()), \
                        patch.object(monitor, "sleep", side_effect=during_wait):
                    list(monitor.watch_targets(connection, interval=30, count=2, pulse_every=0.05))
            except Exception as error:
                errors.append(error)
            finally:
                connection.close()

        worker = threading.Thread(target=run)
        worker.start()
        self.addCleanup(lambda: (release.set(), worker.join(timeout=5)))
        self.assertTrue(held.wait(5))
        first = self.read_watcher()
        self.assertEqual((first["state"], first["phase"], first["rounds"], first["late"]),
                         ("running", "waiting", 1, False))
        moved = self.until_beat_moves(first["beat_at"])
        self.assertEqual((moved["phase"], moved["rounds"], moved["due_at"], moved["started_at"]),
                         ("waiting", 1, first["due_at"], first["started_at"]))
        self.assertIs(moved["stale"], False)
        self.assertIs(moved["late"], False)
        release.set()
        worker.join(5)
        self.assertFalse(worker.is_alive())
        self.assertEqual(errors, [])
        self.assertEqual(self.read_watcher()["state"], "stopped")
        self.assertEqual(self.pulse_threads(), [])

    def test_the_beat_moves_while_a_round_is_handed_back_for_alerts(self):
        with patch.object(monitor, "probe", return_value=observation()), patch.object(monitor, "sleep"):
            rounds = monitor.watch_targets(self.connection, interval=30, count=2, pulse_every=0.05)
            self.addCleanup(rounds.close)
            first = next(rounds)
            self.assertEqual(first["cycle"], 1)
            started = monitor.watcher_status(self.connection)
            self.assertEqual((started["phase"], started["rounds"], started["stale"], started["due_at"]),
                             ("checking", 1, False, None))
            moved = self.until_beat_moves(started["beat_at"])
            self.assertEqual((moved["phase"], moved["rounds"], moved["started_at"], moved["due_at"], moved["stale"]),
                             ("checking", 1, started["started_at"], None, False))
            second = next(rounds)
            self.assertEqual(second["cycle"], 2)
            with self.assertRaises(StopIteration):
                next(rounds)
        stopped = monitor.watcher_status(self.connection)
        self.assertEqual((stopped["state"], stopped["rounds"], stopped["stale"]), ("stopped", 2, False))
        time.sleep(0.25)
        self.assertEqual(monitor.watcher_status(self.connection)["beat_at"], stopped["beat_at"])

    def test_run_all_does_not_beat_while_a_check_blocks(self):
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
                with patch.object(monitor, "probe", side_effect=probe):
                    monitor.run_targets(connection, workers=1)
            except Exception as error:
                errors.append(error)
            finally:
                connection.close()

        worker = threading.Thread(target=run)
        worker.start()
        self.addCleanup(lambda: (release.set(), worker.join(timeout=5)))
        self.assertTrue(entered.wait(5))
        self.assertEqual(self.pulse_threads(), [])
        time.sleep(0.2)
        self.assertEqual(self.read_watcher()["state"], "absent")
        release.set()
        worker.join(5)
        self.assertFalse(worker.is_alive())
        self.assertEqual(errors, [])
        self.assertEqual(self.read_watcher()["state"], "absent")

    def test_a_pulse_interval_outside_the_limit_is_rejected(self):
        for value in (0, -1, True, float("nan"), monitor.PULSE_EVERY + 0.01):
            with self.subTest(value=value), patch.object(monitor, "probe") as probe:
                with self.assertRaises(ValueError):
                    list(monitor.watch_targets(self.connection, pulse_every=value))
                probe.assert_not_called()
        self.assertEqual(monitor.watcher_status(self.connection)["state"], "absent")

    def read_watcher(self):
        with closing(api.read_only(self.database)) as reader:
            return monitor.watcher_status(reader)

    def pulse_threads(self):
        return [thread.name for thread in threading.enumerate() if thread.name == "watcher-pulse"]

    def until_beat_moves(self, previous):
        deadline = time.perf_counter() + 3
        last = None
        while time.perf_counter() < deadline:
            last = self.read_watcher()
            if last["beat_at"] != previous:
                return last
            time.sleep(0.02)
        self.fail(f"beat did not move from {previous}; last={last}")


if __name__ == "__main__":
    unittest.main()
