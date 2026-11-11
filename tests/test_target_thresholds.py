"""Per-target incident thresholds, shared by alerts and reports."""

from contextlib import closing, redirect_stderr, redirect_stdout
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

TOUCHY = "https://touchy.example"
STEADY = "https://steady.example"
SHARED = "https://shared.example"


def check(url, state, second):
    return monitor.Check(url, f"2026-11-11T12:00:{second:02d}+00:00", state,
                         200 if state == "up" else 503, 1.0, None if state == "up" else "http_503")


class TargetThresholdTests(unittest.TestCase):
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

    def test_saved_thresholds_survive_reopening(self):
        code, output = self.cli("add", "api", "https://example.com", "--failures", "3", "--recoveries", "4")
        self.assertEqual(code, 0)
        expected = {"name": "api", "url": "https://example.com", "timeout": 5, "deadline": None,
                    "alert_webhook": "default", "failures": 3, "recoveries": 4}
        self.assertEqual(json.loads(output), expected)
        reopened = monitor.connect(self.database)
        self.addCleanup(reopened.close)
        self.assertEqual(monitor.get_target(reopened, "api"), expected)
        listed, = json.loads(self.cli("targets")[1])
        self.assertEqual((listed["failures"], listed["recoveries"]), (3, 4))

    def test_invalid_thresholds_are_rejected(self):
        for flag, value in (("--failures", "0"), ("--failures", "-1"), ("--failures", "101"),
                            ("--recoveries", "0"), ("--recoveries", "101")):
            with self.subTest(flag=flag, value=value):
                self.assertEqual(self.cli("add", "api", "https://example.com", flag, value)[0], 2)
        for value in ("1.5", "nope", "true"):
            with self.subTest(value=value), self.assertRaises(SystemExit) as exit:
                self.cli("add", "api", "https://example.com", "--failures", value)
            self.assertEqual(exit.exception.code, 2)
        self.assertEqual(monitor.list_targets(self.connection), [])
        for count in (True, 1.5, 0, 101):
            with self.subTest(count=count), self.assertRaises(ValueError):
                monitor.add_target(self.connection, "api", "https://example.com", failures=count)
            with self.assertRaises(ValueError):
                monitor.add_target(self.connection, "api", "https://example.com", recoveries=count)
        with self.assertRaises(sqlite3.IntegrityError), self.connection:
            self.connection.execute("INSERT INTO targets (name, url, timeout, failures) "
                                    "VALUES ('raw', 'https://example.com', 5, 0)")
        with self.assertRaises(sqlite3.IntegrityError), self.connection:
            self.connection.execute("INSERT INTO targets (name, url, timeout, recoveries) "
                                    "VALUES ('raw', 'https://example.com', 5, 101)")

    def test_update_sets_keeps_and_resets_each_threshold(self):
        monitor.add_target(self.connection, "api", "https://example.com")
        code, output = self.cli("update", "api", "--failures", "3")
        self.assertEqual(code, 0)
        self.assertEqual((json.loads(output)["failures"], json.loads(output)["recoveries"]), (3, None))
        for flags, expected in ((["--recoveries", "4"], (3, 4)),
                                (["--timeout", "2"], (3, 4)),
                                (["--no-failures"], (None, 4)),
                                (["--deadline", "5"], (None, 4)),
                                (["--no-recoveries"], (None, None)),
                                (["--timeout", "3"], (None, None))):
            with self.subTest(flags=flags):
                code, output = self.cli("update", "api", *flags)
                self.assertEqual(code, 0)
                saved = monitor.get_target(self.connection, "api")
                self.assertEqual((saved["failures"], saved["recoveries"]), expected)
                self.assertEqual((json.loads(output)["failures"], json.loads(output)["recoveries"]), expected)
        self.assertEqual(monitor.get_target(self.connection, "api")["timeout"], 3)

    def test_invalid_threshold_updates_leave_settings_unchanged(self):
        original = monitor.add_target(self.connection, "api", "https://example.com", failures=3, recoveries=4)
        for flags in (["--failures", "0"], ["--recoveries", "101"], ["--failures", "-1"],
                      ["--timeout", "2", "--recoveries", "0"]):
            with self.subTest(flags=flags):
                self.assertEqual(self.cli("update", "api", *flags)[0], 2)
                self.assertEqual(monitor.get_target(self.connection, "api"), original)
        for flags in (["--failures", "1", "--no-failures"], ["--recoveries", "1", "--no-recoveries"]):
            with self.subTest(flags=flags), self.assertRaises(SystemExit) as exit:
                self.cli("update", "api", *flags)
            self.assertEqual(exit.exception.code, 2)
            self.assertEqual(monitor.get_target(self.connection, "api"), original)
        with self.assertRaisesRegex(ValueError, "not both"):
            monitor.update_target(self.connection, "api", failures=1, clear_failures=True)
        with self.assertRaisesRegex(ValueError, "not both"):
            monitor.update_target(self.connection, "api", recoveries=1, clear_recoveries=True)
        self.assertEqual(monitor.get_target(self.connection, "api"), original)

    def test_databases_from_before_thresholds_are_upgraded(self):
        legacy_path = self.database.with_name("legacy.sqlite3")
        with sqlite3.connect(legacy_path) as legacy:
            legacy.execute(
                """CREATE TABLE checks (
                    id INTEGER PRIMARY KEY, url TEXT NOT NULL, checked_at TEXT NOT NULL,
                    state TEXT NOT NULL, status_code INTEGER, latency_ms REAL NOT NULL, error TEXT)""")
            legacy.execute(
                """CREATE TABLE targets (
                    name TEXT PRIMARY KEY, url TEXT NOT NULL,
                    timeout REAL NOT NULL CHECK(timeout > 0 AND timeout <= 60),
                    deadline REAL CHECK(deadline IS NULL OR (deadline > 0 AND deadline <= 60)),
                    alert_webhook TEXT NOT NULL DEFAULT 'default')""")
            legacy.execute("INSERT INTO targets (name, url, timeout, deadline, alert_webhook) "
                           "VALUES ('api', 'https://example.com', 2, NULL, 'default')")
            legacy.execute("INSERT INTO targets (name, url, timeout) VALUES ('other', 'https://other.example', 5)")
            legacy.executemany("INSERT INTO checks (url, checked_at, state, latency_ms) VALUES (?, ?, 'down', 1)",
                               [("https://example.com", "2026-11-01T00:00:00+00:00"),
                                ("https://example.com", "2026-11-01T00:00:01+00:00"),
                                ("https://other.example", "2026-11-01T00:00:02+00:00")])
        legacy.close()
        upgraded = monitor.connect(legacy_path)
        self.addCleanup(upgraded.close)
        api_target = monitor.get_target(upgraded, "api")
        self.assertEqual((api_target["failures"], api_target["recoveries"], api_target["deadline"],
                          api_target["alert_webhook"], api_target["timeout"]), (None, None, None, "default", 2))
        self.assertEqual([incident["url"] for incident in list_incidents(upgraded)], ["https://example.com"])
        self.assertEqual(list_incidents(upgraded)[0]["failures"], 2)
        monitor.update_target(upgraded, "other", failures=1)
        incidents = list_incidents(upgraded)
        self.assertEqual([incident["url"] for incident in incidents], ["https://other.example", "https://example.com"])
        self.assertEqual(incidents[0]["failures"], 1)
        self.assertEqual([event["incident"] for event in alerts.incident_events(upgraded)], incidents[::-1])
        monitor.connect(legacy_path).close()
        names = {row["name"] for row in upgraded.execute("PRAGMA table_info(targets)")}
        self.assertIn("failures", names)
        self.assertIn("recoveries", names)

    def test_a_corrupt_stored_threshold_is_rejected_by_reports_and_alerts(self):
        legacy_path = self.database.with_name("corrupt.sqlite3")
        with sqlite3.connect(legacy_path) as legacy:
            legacy.execute("CREATE TABLE targets (name TEXT PRIMARY KEY, url TEXT NOT NULL, timeout REAL NOT NULL, "
                           "failures INTEGER)")
            legacy.execute("INSERT INTO targets VALUES ('api', 'https://example.com', 5, 0)")
            legacy.execute("CREATE TABLE checks (id INTEGER PRIMARY KEY, url TEXT NOT NULL, checked_at TEXT NOT NULL, "
                           "state TEXT NOT NULL, status_code INTEGER, latency_ms REAL NOT NULL, error TEXT)")
        legacy.close()
        upgraded = monitor.connect(legacy_path)
        self.addCleanup(upgraded.close)
        with self.assertRaisesRegex(ValueError, "Failures must be between 1 and 100"):
            list_incidents(upgraded)
        with self.assertRaisesRegex(ValueError, "Failures must be between 1 and 100"):
            alerts.incident_events(upgraded)

    def test_targets_that_share_a_url_must_agree(self):
        monitor.add_target(self.connection, "api", SHARED, failures=3)
        with self.assertRaisesRegex(ValueError, "different failure thresholds"):
            monitor.add_target(self.connection, "web", SHARED, failures=4)
        self.assertEqual([target["name"] for target in monitor.list_targets(self.connection)], ["api"])
        monitor.add_target(self.connection, "web", SHARED, failures=3, recoveries=4)
        monitor.add_target(self.connection, "extra", SHARED)
        self.assertEqual(monitor.get_target(self.connection, "extra")["failures"], None)
        original = monitor.get_target(self.connection, "web")
        with self.assertRaisesRegex(ValueError, "different failure thresholds"):
            monitor.update_target(self.connection, "web", failures=5)
        self.assertEqual(monitor.get_target(self.connection, "web"), original)
        elsewhere = monitor.add_target(self.connection, "else", "https://else.example", failures=1)
        with self.assertRaisesRegex(ValueError, "different failure thresholds"):
            monitor.update_target(self.connection, "else", url=SHARED)
        self.assertEqual(monitor.get_target(self.connection, "else"), elsewhere)
        for state in ("down", "down", "down", "up"):
            monitor.save(self.connection, check(SHARED, state, monitor.history(self.connection).__len__()))
        incident, = [row for row in list_incidents(self.connection) if row["url"] == SHARED]
        self.assertEqual((incident["state"], incident["failures"], incident["recoveries"]), ("open", 3, 4))
        monitor.remove_target(self.connection, "api")
        monitor.remove_target(self.connection, "web")
        incident, = [row for row in list_incidents(self.connection) if row["url"] == SHARED]
        self.assertEqual((incident["failures"], incident["recoveries"]), (2, 2))

    def test_each_count_overrides_the_default_for_alerts_and_reports(self):
        monitor.add_target(self.connection, "quick", TOUCHY, failures=1)
        monitor.add_target(self.connection, "brief", STEADY, recoveries=1)
        monitor.add_target(self.connection, "plain", "https://plain.example")
        second = 0

        def record(url, state):
            nonlocal second
            second += 1
            monitor.save(self.connection, check(url, state, second))

        record(TOUCHY, "down")
        record(STEADY, "down")
        record("https://plain.example", "down")
        self.assertEqual([row["url"] for row in list_incidents(self.connection)], [TOUCHY])
        record(STEADY, "down")
        record("https://plain.example", "down")
        record(STEADY, "up")
        record("https://plain.example", "up")
        record(TOUCHY, "up")
        reported = {row["url"]: row for row in list_incidents(self.connection)}
        self.assertEqual(reported[TOUCHY]["state"], "open")
        self.assertEqual((reported[TOUCHY]["failures"], reported[TOUCHY]["recoveries"]), (1, 2))
        self.assertEqual(reported[STEADY]["state"], "resolved")
        self.assertEqual((reported[STEADY]["failures"], reported[STEADY]["recoveries"]), (2, 1))
        self.assertEqual(reported["https://plain.example"]["state"], "open")
        sent = []
        with patch.object(alerts, "send_webhook", side_effect=lambda _webhook, body: sent.append(body)), \
                redirect_stderr(io.StringIO()):
            alerts.deliver(self.connection, "https://hooks.example/alert")
        self.assertEqual({(body["url"], body["event"]) for body in sent},
                         {(TOUCHY, "opened"), (STEADY, "opened"), (STEADY, "resolved"),
                          ("https://plain.example", "opened")})
        for body in sent:
            self.assertEqual(body["incident"], reported[body["url"]])

    def test_resetting_a_threshold_changes_the_report_and_unsent_alerts_together(self):
        monitor.add_target(self.connection, "api", TOUCHY, failures=1, recoveries=1)
        monitor.save(self.connection, check(TOUCHY, "down", 0))
        sent = []
        with patch.object(alerts, "send_webhook", side_effect=lambda _webhook, body: sent.append(body)), \
                redirect_stderr(io.StringIO()):
            alerts.deliver(self.connection, "https://hooks.example/alert")
        self.assertEqual([body["event"] for body in sent], ["opened"])
        self.assertEqual(sent[0]["incident"], list_incidents(self.connection)[0])
        monitor.update_target(self.connection, "api", clear_failures=True, clear_recoveries=True)
        self.assertEqual(list_incidents(self.connection), [])
        self.assertEqual(alerts.incident_events(self.connection), [])
        self.assertEqual([row["event"] for row in alerts.list_alerts(self.connection)], ["opened"])
        monitor.save(self.connection, check(TOUCHY, "down", 1))
        incident, = list_incidents(self.connection)
        self.assertEqual((incident["failures"], incident["recoveries"], incident["state"]), (2, 2, "open"))
        # The opening was already sent, and it is the same incident, so it is not sent again.
        self.assertEqual(incident["first_failure_id"], sent[0]["incident"]["first_failure_id"])
        self.assertEqual(alerts.pending_alerts(self.connection), [])
        with patch.object(alerts, "send_webhook") as resend, redirect_stderr(io.StringIO()):
            self.assertEqual(alerts.deliver(self.connection, "https://hooks.example/alert"), [])
        resend.assert_not_called()

    def test_an_explicit_report_threshold_does_not_change_alerts(self):
        monitor.add_target(self.connection, "api", TOUCHY, failures=3)
        monitor.save(self.connection, check(TOUCHY, "down", 0))
        monitor.save(self.connection, check(TOUCHY, "down", 1))
        self.assertEqual(list_incidents(self.connection), [])
        opened, = list_incidents(self.connection, failures=2)
        self.assertEqual(opened["failures"], 2)
        self.assertEqual(alerts.incident_events(self.connection), [])
        monitor.save(self.connection, check(TOUCHY, "down", 2))
        incident, = list_incidents(self.connection)
        event, = alerts.incident_events(self.connection)
        self.assertEqual(event["incident"], incident)
        self.assertEqual(incident["failures"], 3)


class ThresholdCommandTests(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.database = Path(directory.name) / "checks.sqlite3"
        self.queues = {TOUCHY: [], STEADY: []}
        self.sent = []
        self.second = 0

    def cli(self, *arguments):
        output, errors = io.StringIO(), io.StringIO()

        def probe(url, _timeout, _deadline):
            self.second += 1
            state = self.queues[url].pop(0)
            return check(url, state, self.second)

        def send(_webhook, body):
            self.sent.append(body)
            return None

        with patch("monitor.probe", side_effect=probe), \
                patch.object(alerts, "send_webhook", side_effect=send), \
                redirect_stdout(output), redirect_stderr(errors):
            code = monitor.main(["--database", str(self.database), *arguments])
        return code, output.getvalue(), errors.getvalue()

    def test_commands_api_and_alerts_follow_saved_thresholds(self):
        webhook = "https://hooks.example/alert"
        self.assertEqual(self.cli("add", "touchy", TOUCHY, "--failures", "1", "--recoveries", "1")[0], 0)
        self.assertEqual(self.cli("add", "steady", STEADY)[0], 0)
        self.queues[TOUCHY].append("up")
        self.queues[STEADY].append("up")
        self.assertEqual(self.cli("watch", "--count", "1", "--workers", "1", "--alert-webhook", webhook)[0], 0)
        self.assertEqual(self.sent, [])
        self.queues[TOUCHY].append("down")
        self.queues[STEADY].append("down")
        self.assertEqual(self.cli("run", "touchy")[0], 1)
        self.assertEqual(self.cli("run", "steady")[0], 1)
        reported = json.loads(self.cli("incidents")[1])
        pending = json.loads(self.cli("alerts", "--pending")[1])
        self.assertEqual([row["url"] for row in reported], [TOUCHY])
        self.assertEqual((reported[0]["failures"], reported[0]["recoveries"], reported[0]["state"]), (1, 1, "open"))
        self.assertEqual([row["incident"] for row in pending], reported)
        self.assertEqual(json.loads(self.cli("incidents", "--failures", "2")[1]), [])
        self.assertEqual(json.loads(self.cli("alerts", "--pending")[1]), pending)
        server = api.make_server(self.database, "127.0.0.1", 0, monitor)
        thread = Thread(target=server.serve_forever, daemon=True)
        thread.start()
        self.addCleanup(server.server_close)
        self.addCleanup(thread.join)
        self.addCleanup(server.shutdown)
        with urlopen(f"http://127.0.0.1:{server.server_port}/api/incidents", timeout=5) as response:
            self.assertEqual(json.loads(response.read()), reported)
        with urlopen(f"http://127.0.0.1:{server.server_port}/api/targets", timeout=5) as response:
            targets = {row["name"]: row for row in json.loads(response.read())}
        self.assertEqual((targets["touchy"]["failures"], targets["touchy"]["recoveries"]), (1, 1))
        self.assertEqual((targets["steady"]["failures"], targets["steady"]["recoveries"]), (None, None))
        self.assertEqual(self.cli("update", "touchy", "--no-failures", "--no-recoveries")[0], 0)
        self.assertEqual(json.loads(self.cli("incidents")[1]), [])
        self.assertEqual(json.loads(self.cli("alerts", "--pending")[1]), [])
        with urlopen(f"http://127.0.0.1:{server.server_port}/api/incidents", timeout=5) as response:
            self.assertEqual(json.loads(response.read()), [])
        saved = {row["name"]: (row["failures"], row["recoveries"]) for row in json.loads(self.cli("targets")[1])}
        self.assertEqual(saved, {"steady": (None, None), "touchy": (None, None)})
        self.queues[TOUCHY].append("down")
        self.assertEqual(self.cli("run", "touchy")[0], 1)
        reported = json.loads(self.cli("incidents")[1])
        pending = json.loads(self.cli("alerts", "--pending")[1])
        self.assertEqual([row["url"] for row in reported], [TOUCHY])
        self.assertEqual((reported[0]["failures"], reported[0]["recoveries"]), (2, 2))
        self.assertEqual([row["incident"] for row in pending], reported)
        self.queues[TOUCHY].append("up")
        self.queues[STEADY].append("up")
        code, _, errors = self.cli("watch", "--count", "1", "--workers", "1", "--alert-webhook", webhook)
        self.assertEqual(code, 0, errors)
        # The watcher sends the opening it already owed before this round's checks.
        self.assertEqual([(body["url"], body["event"]) for body in self.sent], [(TOUCHY, "opened")])
        self.assertEqual(self.sent[0]["incident"], reported[0])
        later, = json.loads(self.cli("incidents")[1])
        self.assertEqual((later["first_failure_id"], later["failures"], later["state"]),
                         (reported[0]["first_failure_id"], 2, "open"))
        self.assertGreater(later["last_check_id"], reported[0]["last_check_id"])
        self.assertEqual(json.loads(self.cli("alerts", "--pending")[1]), [])

    def test_the_watcher_uses_a_threshold_changed_between_rounds(self):
        self.cli("add", "api", TOUCHY)
        self.queues[TOUCHY].extend(["down", "up"])

        def change(_seconds):
            with closing(monitor.connect(self.database)) as connection:
                monitor.update_target(connection, "api", failures=1)

        with patch("monitor.sleep", side_effect=change):
            code, _, errors = self.cli("watch", "--count", "2", "--workers", "1",
                                       "--alert-webhook", "https://hooks.example/alert")
        self.assertEqual(code, 1, errors)
        body, = self.sent
        self.assertEqual(body["event"], "opened")
        reported, = json.loads(self.cli("incidents")[1])
        self.assertEqual(body["incident"], reported)
        self.assertEqual((reported["failures"], reported["recoveries"], reported["state"]), (1, 2, "open"))


if __name__ == "__main__":
    unittest.main()
