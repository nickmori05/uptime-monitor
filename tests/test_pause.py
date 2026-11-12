"""Pausing a target keeps it out of watch and run-all without deleting it."""

from contextlib import redirect_stderr, redirect_stdout
import io
import json
from pathlib import Path
import sqlite3
import tempfile
from threading import Thread
import unittest
from unittest.mock import patch
from urllib.request import urlopen

import alerts
import api
from incidents import list_incidents
import monitor

HELD = "https://held.example"
LIVE = "https://live.example"


def observation(url, state, second):
    return monitor.Check(url, f"2026-11-12T12:00:{second:02d}+00:00", state,
                         200 if state == "up" else 503, 1.0, None if state == "up" else "http_503")


class PauseTests(unittest.TestCase):
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

    def test_a_target_is_active_until_it_is_paused(self):
        code, output, _errors = self.cli("add", "api", LIVE)
        self.assertEqual(code, 0)
        self.assertIs(json.loads(output)["paused"], False)
        code, output, _errors = self.cli("add", "held", HELD, "--paused", "--timeout", "3")
        saved = json.loads(output)
        self.assertEqual(code, 0)
        self.assertIs(saved["paused"], True)
        self.assertEqual((saved["url"], saved["timeout"]), (HELD, 3))
        reopened = monitor.connect(self.database)
        self.addCleanup(reopened.close)
        self.assertIs(monitor.get_target(reopened, "held")["paused"], True)
        self.assertIs(monitor.get_target(reopened, "api")["paused"], False)
        listed = {row["name"]: row["paused"] for row in json.loads(self.cli("targets")[1])}
        self.assertEqual(listed, {"api": False, "held": True})

    def test_pause_and_unpause_keep_the_other_settings_and_history(self):
        monitor.add_target(self.connection, "api", HELD, 3, 8, failures=3, recoveries=4)
        for second in range(3):
            monitor.save(self.connection, observation(HELD, "down", second))
        before = monitor.history(self.connection)
        code, output, _errors = self.cli("update", "api", "--pause")
        self.assertEqual(code, 0)
        paused = json.loads(output)
        self.assertIs(paused["paused"], True)
        self.assertEqual(
            (paused["timeout"], paused["deadline"], paused["failures"], paused["recoveries"]), (3, 8, 3, 4))
        self.assertEqual(monitor.history(self.connection), before)
        self.assertEqual([incident["state"] for incident in list_incidents(self.connection)], ["open"])
        code, output, _errors = self.cli("update", "api", "--timeout", "4")
        self.assertEqual(code, 0)
        kept = json.loads(output)
        self.assertIs(kept["paused"], True)
        self.assertEqual(kept["timeout"], 4)
        code, output, _errors = self.cli("update", "api", "--unpause", "--timeout", "3")
        self.assertEqual(code, 0)
        self.assertIs(json.loads(output)["paused"], False)
        self.assertEqual(monitor.get_target(self.connection, "api")["failures"], 3)
        self.assertEqual(monitor.history(self.connection), before)
        removed = json.loads(self.cli("remove", "api")[1])
        self.assertEqual(removed, {"removed": "api"})
        self.assertEqual(monitor.history(self.connection), before)

    def test_invalid_pause_updates_leave_the_target_unchanged(self):
        original = monitor.add_target(self.connection, "api", HELD, paused=True)
        self.assertEqual(self.cli("update", "api")[0], 2)
        self.assertEqual(monitor.get_target(self.connection, "api"), original)
        with self.assertRaises(SystemExit) as exit:
            self.cli("update", "api", "--pause", "--unpause")
        self.assertEqual(exit.exception.code, 2)
        self.assertEqual(monitor.get_target(self.connection, "api"), original)
        for value in (1, 0, "yes"):
            with self.subTest(value=value):
                with self.assertRaisesRegex(ValueError, "true or false"):
                    monitor.update_target(self.connection, "api", paused=value)
                with self.assertRaisesRegex(ValueError, "true or false"):
                    monitor.add_target(self.connection, "other", LIVE, paused=value)
        with self.assertRaisesRegex(ValueError, "true or false"):
            monitor.add_target(self.connection, "other", LIVE, paused=None)
        self.assertEqual(monitor.get_target(self.connection, "api"), original)
        self.assertEqual([target["name"] for target in monitor.list_targets(self.connection)], ["api"])
        with self.assertRaises(sqlite3.IntegrityError), self.connection:
            self.connection.execute(
                "INSERT INTO targets (name, url, timeout, paused) VALUES ('raw', 'https://example.com', 5, 2)")

    def test_unknown_name_does_not_pause_another_target(self):
        original = monitor.add_target(self.connection, "api", HELD)
        for name in ("missing", "API"):
            with self.subTest(name=name):
                self.assertEqual(self.cli("update", name, "--pause")[0], 2)
                self.assertEqual(monitor.list_targets(self.connection), [original])

    def test_run_all_and_watch_skip_paused_targets(self):
        monitor.add_target(self.connection, "held", HELD, paused=True)
        monitor.add_target(self.connection, "live", LIVE)
        up = observation(LIVE, "up", 0)
        with patch("monitor.probe", return_value=up) as probe:
            results = monitor.run_targets(self.connection, workers=2)
        probe.assert_called_once_with(LIVE, 5, None)
        self.assertEqual([row["target"] for row in results], ["live"])
        self.assertEqual([row["url"] for row in monitor.history(self.connection)], [LIVE])
        with patch("monitor.probe", return_value=up) as probe, patch("monitor.sleep") as sleep:
            batches = list(monitor.watch_targets(self.connection, interval=1, count=2, workers=1))
        self.assertEqual(probe.call_count, 2)
        self.assertEqual(sleep.call_count, 1)
        self.assertEqual([batch["results"][0]["target"] for batch in batches], ["live", "live"])
        self.assertTrue(all(call.args[0] == LIVE for call in probe.call_args_list))

    def test_run_still_checks_a_paused_target(self):
        monitor.add_target(self.connection, "held", HELD, paused=True)
        down = observation(HELD, "down", 1)
        with patch("monitor.probe", return_value=down) as probe, redirect_stdout(io.StringIO()):
            code = monitor.main(["--database", str(self.database), "run", "held"])
        self.assertEqual(code, 1)
        probe.assert_called_once_with(HELD, 5, None)
        self.assertIs(monitor.get_target(self.connection, "held")["paused"], True)
        self.assertEqual(monitor.history(self.connection)[0]["url"], HELD)

    def test_a_paused_url_is_still_checked_when_another_target_watches_it(self):
        monitor.add_target(self.connection, "held", HELD, paused=True, failures=3)
        monitor.add_target(self.connection, "live", HELD)
        up = observation(HELD, "up", 0)
        with patch("monitor.probe", return_value=up) as probe:
            results = monitor.run_targets(self.connection)
        probe.assert_called_once_with(HELD, 5, None)
        self.assertEqual([row["target"] for row in results], ["live"])
        with self.assertRaisesRegex(ValueError, "different failure thresholds"):
            monitor.update_target(self.connection, "live", failures=4)
        self.assertIs(monitor.get_target(self.connection, "held")["paused"], True)
        self.assertEqual(monitor.get_target(self.connection, "live")["failures"], None)

    def test_the_watcher_keeps_going_while_every_target_is_paused_and_resumes(self):
        monitor.add_target(self.connection, "held", HELD, paused=True)
        calls = []

        def probe(url, timeout, deadline):
            calls.append(url)
            return observation(url, "up", len(calls))

        def unpause(_seconds):
            monitor.update_target(self.connection, "held", paused=False)

        with patch("monitor.probe", side_effect=probe), patch("monitor.sleep", side_effect=unpause):
            batches = list(monitor.watch_targets(self.connection, interval=1, count=2))
        self.assertEqual(len(batches), 2)
        self.assertEqual(batches[0]["results"], [])
        self.assertEqual(batches[1]["results"][0]["target"], "held")
        self.assertEqual(calls, [HELD])
        self.assertEqual(len(monitor.history(self.connection)), 1)

    def test_pausing_the_only_target_does_not_stop_the_watcher(self):
        monitor.add_target(self.connection, "api", HELD)
        seen = []

        def probe(url, timeout, deadline):
            seen.append(url)
            return observation(url, "up", len(seen))

        def pause(_seconds):
            monitor.update_target(self.connection, "api", paused=True)

        with patch("monitor.probe", side_effect=probe), patch("monitor.sleep", side_effect=pause):
            batches = list(monitor.watch_targets(self.connection, interval=3, count=3))
        self.assertEqual(len(batches), 3)
        self.assertEqual(seen, [HELD])
        self.assertEqual(batches[0]["results"][0]["target"], "api")
        self.assertEqual([batch["results"] for batch in batches[1:]], [[], []])
        self.assertEqual(len(monitor.history(self.connection)), 1)

    def test_removing_the_last_paused_target_stops_the_watcher(self):
        monitor.add_target(self.connection, "held", HELD, paused=True)

        def remove(_seconds):
            monitor.remove_target(self.connection, "held")

        with patch("monitor.probe") as probe, patch("monitor.sleep", side_effect=remove):
            batches = list(monitor.watch_targets(self.connection, interval=1))
        probe.assert_not_called()
        self.assertEqual(batches, [{"cycle": 1, "results": []}, {"cycle": 2, "results": []}])

    def test_a_bad_paused_target_still_stops_the_batch_before_requests(self):
        monitor.add_target(self.connection, "held", HELD, paused=True)
        monitor.add_target(self.connection, "live", LIVE)
        with self.connection:
            self.connection.execute("UPDATE targets SET url = 'file:///etc/hosts' WHERE name = 'held'")
        with patch("monitor.probe") as probe, self.assertRaises(ValueError):
            monitor.run_targets(self.connection)
        probe.assert_not_called()
        self.assertEqual(monitor.history(self.connection), [])

    def test_an_older_database_gains_the_column_unpaused_and_keeps_its_history(self):
        legacy_path = self.database.with_name("legacy.sqlite3")
        with sqlite3.connect(legacy_path) as legacy:
            legacy.execute(
                """CREATE TABLE checks (
                    id INTEGER PRIMARY KEY, url TEXT NOT NULL, checked_at TEXT NOT NULL,
                    state TEXT NOT NULL, status_code INTEGER, latency_ms REAL NOT NULL, error TEXT)""")
            legacy.execute(
                """CREATE TABLE targets (
                    name TEXT PRIMARY KEY, url TEXT NOT NULL,
                    timeout REAL NOT NULL CHECK(timeout > 0 AND timeout <= 60))""")
            legacy.execute("INSERT INTO targets (name, url, timeout) VALUES ('api', ?, 2)", (HELD,))
            legacy.execute("INSERT INTO checks (url, checked_at, state, latency_ms) "
                           "VALUES (?, '2026-11-01T00:00:00+00:00', 'up', 1)", (HELD,))
        legacy.close()
        upgraded = monitor.connect(legacy_path)
        self.addCleanup(upgraded.close)
        target = monitor.get_target(upgraded, "api")
        self.assertIs(target["paused"], False)
        self.assertEqual(target["timeout"], 2)
        self.assertEqual(monitor.history(upgraded)[0]["url"], HELD)
        self.assertIn("paused", {row["name"] for row in upgraded.execute("PRAGMA table_info(targets)")})
        with patch("monitor.probe", return_value=observation(HELD, "up", 2)) as probe:
            self.assertEqual(len(monitor.run_targets(upgraded)), 1)
        probe.assert_called_once_with(HELD, 2, None)

    def test_a_stored_pause_flag_other_than_zero_or_one_is_rejected(self):
        legacy_path = self.database.with_name("corrupt.sqlite3")
        with sqlite3.connect(legacy_path) as legacy:
            legacy.execute("CREATE TABLE targets (name TEXT PRIMARY KEY, url TEXT NOT NULL, timeout REAL NOT NULL, "
                           "paused INTEGER)")
            legacy.execute("INSERT INTO targets VALUES ('api', ?, 5, 2)", (HELD,))
        legacy.close()
        upgraded = monitor.connect(legacy_path)
        self.addCleanup(upgraded.close)
        with self.assertRaisesRegex(ValueError, "true or false"):
            monitor.get_target(upgraded, "api")
        with patch("monitor.probe") as probe, self.assertRaisesRegex(ValueError, "true or false"):
            monitor.run_targets(upgraded)
        probe.assert_not_called()

    def test_watch_of_only_paused_targets_exits_successfully_without_requests(self):
        monitor.add_target(self.connection, "held", HELD, paused=True)
        output = io.StringIO()
        with patch("monitor.probe") as probe, patch("monitor.sleep") as sleep, redirect_stdout(output):
            code = monitor.main(["--database", str(self.database), "watch", "--count", "2", "--interval", "1"])
        self.assertEqual(code, 0)
        probe.assert_not_called()
        self.assertEqual(sleep.call_count, 1)
        batches = [json.loads(line) for line in output.getvalue().splitlines()]
        self.assertEqual(batches, [{"cycle": 1, "results": []}, {"cycle": 2, "results": []}])
        self.assertEqual(monitor.history(self.connection), [])

    def test_run_all_of_only_paused_targets_exits_successfully(self):
        monitor.add_target(self.connection, "held", HELD, paused=True)
        output = io.StringIO()
        with patch("monitor.probe") as probe, redirect_stdout(output):
            code = monitor.main(["--database", str(self.database), "run-all"])
        self.assertEqual(code, 0)
        probe.assert_not_called()
        self.assertEqual(json.loads(output.getvalue()), [])

    def test_the_api_reports_the_pause_flag(self):
        monitor.add_target(self.connection, "held", HELD, paused=True)
        monitor.add_target(self.connection, "live", LIVE)
        server = api.make_server(self.database, "127.0.0.1", 0, monitor)
        thread = Thread(target=server.serve_forever, daemon=True)
        thread.start()
        self.addCleanup(server.server_close)
        self.addCleanup(thread.join)
        self.addCleanup(server.shutdown)
        with urlopen(f"http://127.0.0.1:{server.server_port}/api/targets", timeout=5) as response:
            targets = {row["name"]: row["paused"] for row in json.loads(response.read())}
        self.assertEqual(targets, {"held": True, "live": False})

    def test_an_alert_owed_while_paused_is_still_sent_and_watch_adds_no_check(self):
        webhook = "https://hooks.example/alert"
        monitor.add_target(self.connection, "held", HELD)
        sent = []

        def send(_webhook, body):
            sent.append(body)
            return None

        with patch("monitor.probe", return_value=observation(HELD, "up", 0)), \
                patch.object(alerts, "send_webhook", side_effect=send), \
                patch("monitor.sleep"), redirect_stdout(io.StringIO()), redirect_stderr(io.StringIO()):
            code = monitor.main(
                ["--database", str(self.database), "watch", "--count", "1", "--alert-webhook", webhook])
        self.assertEqual(code, 0)
        self.assertEqual(sent, [])
        monitor.update_target(self.connection, "held", paused=True)
        for second in (1, 2):
            down = observation(HELD, "down", second)
            with patch("monitor.probe", return_value=down), redirect_stdout(io.StringIO()):
                self.assertEqual(monitor.main(["--database", str(self.database), "run", "held"]), 1)
        self.assertEqual(len(monitor.history(self.connection)), 3)
        incident, = list_incidents(self.connection)
        self.assertEqual(incident["state"], "open")
        with patch("monitor.probe") as probe, patch.object(alerts, "send_webhook", side_effect=send), \
                patch("monitor.sleep"), redirect_stdout(io.StringIO()), redirect_stderr(io.StringIO()):
            code = monitor.main(
                ["--database", str(self.database), "watch", "--count", "1", "--alert-webhook", webhook])
        self.assertEqual(code, 0)
        probe.assert_not_called()
        self.assertEqual(len(monitor.history(self.connection)), 3)
        self.assertEqual([(body["event"], body["url"]) for body in sent], [("opened", HELD)])
        self.assertEqual(sent[0]["incident"]["first_failure_id"], incident["first_failure_id"])
        self.assertIs(monitor.get_target(self.connection, "held")["paused"], True)


if __name__ == "__main__":
    unittest.main()
