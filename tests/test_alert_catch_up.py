from contextlib import redirect_stderr, redirect_stdout
import io
import json
from pathlib import Path
import sqlite3
import tempfile
import unittest
from unittest.mock import patch

import alerts
import monitor

WEBHOOK = "https://hooks.example/alert"


def check(state, url="https://example.com"):
    return monitor.Check(url, "2026-10-20T12:00:00+00:00", state, 200 if state == "up" else 503,
                         1.0, None if state == "up" else "http_503")


class CatchUpTests(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.database = Path(directory.name) / "checks.sqlite3"
        self.connection = monitor.connect(self.database)
        self.addCleanup(self.connection.close)
        self.sent = []

    def record(self, states, url="https://example.com"):
        return [monitor.save(self.connection, check(state, url)) for state in states]

    def start(self, results=None):
        outcomes = iter(results or [])

        def send(webhook, body):
            outcome = next(outcomes, None)
            if outcome is None:
                self.sent.append(body)
            return outcome

        errors = io.StringIO()
        with patch.object(alerts, "send_webhook", side_effect=send), redirect_stderr(errors):
            delivered = alerts.start(self.connection, WEBHOOK)
        return delivered, errors.getvalue()

    def test_first_start_skips_history_and_saves_where_alerting_began(self):
        ids = self.record(["down", "down"])
        self.assertIsNone(alerts.enabled_after(self.connection))
        delivered, errors = self.start()
        self.assertEqual(delivered, [])
        self.assertIn("Skipped 1 earlier incident event(s)", errors)
        self.assertEqual(alerts.enabled_after(self.connection), ids[-1])
        self.assertEqual(alerts.skip_backlog(self.connection), 0)

    def test_first_start_on_an_empty_database_skips_nothing(self):
        self.assertEqual(self.start(), ([], ""))
        self.assertEqual(alerts.enabled_after(self.connection), 0)

    def test_events_after_alerting_began_are_never_skipped(self):
        self.start()
        self.record(["down", "down", "up", "up"])
        self.assertEqual(alerts.skip_backlog(self.connection), 0)
        delivered, errors = self.start()
        self.assertEqual([body["event"] for body in delivered], ["opened", "resolved"])
        self.assertIn("Sending 2 incident event(s) not alerted yet.", errors)
        self.assertEqual(self.start(), ([], ""))

    def test_an_incident_open_at_first_start_still_alerts_its_resolution(self):
        self.record(["down", "down"])
        self.start()
        self.record(["up", "up"])
        delivered, _ = self.start()
        self.assertEqual([body["event"] for body in delivered], ["resolved"])

    def test_a_failed_catch_up_stops_and_leaves_the_rest_pending(self):
        self.start()
        self.record(["down", "down", "up", "up"])
        delivered, errors = self.start(["connection_error"])
        self.assertEqual(delivered, [])
        self.assertIn("Alert not delivered (connection_error)", errors)
        self.assertEqual([body["event"] for body in alerts.list_alerts(self.connection, pending=True)],
                         ["opened", "resolved"])
        self.assertEqual([body["event"] for body in self.start()[0]], ["opened", "resolved"])

    def test_pending_lists_only_events_a_watcher_would_send(self):
        self.record(["down", "down"])
        self.assertEqual(alerts.list_alerts(self.connection, pending=True), [])
        self.start()
        self.assertEqual(alerts.list_alerts(self.connection, pending=True), [])
        self.record(["up", "up"], "https://example.com")
        self.record(["down", "down"], "https://b.example")
        pending = alerts.list_alerts(self.connection, pending=True)
        self.assertEqual([(body["event"], body["url"]) for body in pending],
                         [("resolved", "https://example.com"), ("opened", "https://b.example")])
        self.assertIn("Incident opened for https://b.example", pending[1]["text"])
        self.assertEqual(alerts.list_alerts(self.connection, 1, pending=True), pending[:1])
        with self.assertRaises(ValueError):
            alerts.list_alerts(self.connection, 0, pending=True)

    def test_the_alerting_table_holds_one_valid_row(self):
        for row in ((1, "2026-10-20", -1), (1, None, 0), (2, "2026-10-20", 0)):
            with self.subTest(row=row), self.assertRaises(sqlite3.IntegrityError), self.connection:
                self.connection.execute("INSERT INTO alerting VALUES (?, ?, ?)", row)
        self.start()
        with self.assertRaises(sqlite3.IntegrityError), self.connection:
            self.connection.execute("INSERT INTO alerting VALUES (1, '2026-10-20', 5)")
        self.assertEqual(alerts.enabled_after(self.connection), 0)

    def test_database_from_before_catch_up_skips_once_then_catches_up(self):
        legacy_path = self.database.with_name("legacy.sqlite3")
        with sqlite3.connect(legacy_path) as legacy:
            legacy.execute("CREATE TABLE checks (id INTEGER PRIMARY KEY, url TEXT NOT NULL, checked_at TEXT NOT NULL, "
                           "state TEXT NOT NULL, status_code INTEGER, latency_ms REAL NOT NULL, error TEXT)")
            legacy.execute("CREATE TABLE alerts (id INTEGER PRIMARY KEY, url TEXT NOT NULL, first_failure_id INTEGER "
                           "NOT NULL, event TEXT NOT NULL, check_id INTEGER NOT NULL, status TEXT NOT NULL, "
                           "recorded_at TEXT NOT NULL, UNIQUE(first_failure_id, event))")
            legacy.executemany("INSERT INTO checks (url, checked_at, state, latency_ms) VALUES (?, ?, ?, 1)",
                               [("https://example.com", "2026-10-19T00:00:00+00:00", state)
                                for state in ("down", "down", "up", "up", "down", "down")])
            legacy.execute("INSERT INTO alerts VALUES (1, 'https://example.com', 1, 'opened', 2, 'sent', '2026-10-19')")
        legacy.close()
        self.connection.close()
        self.connection = monitor.connect(legacy_path)
        self.assertIsNone(alerts.enabled_after(self.connection))
        delivered, errors = self.start()
        self.assertEqual(delivered, [])
        self.assertIn("Skipped 2 earlier incident event(s)", errors)
        self.assertEqual(alerts.enabled_after(self.connection), 6)
        self.record(["up", "up"])
        self.assertEqual([body["event"] for body in self.start()[0]], ["resolved"])
        self.assertEqual([row["status"] for row in alerts.list_alerts(self.connection)],
                         ["sent", "skipped", "skipped", "sent"])


class CatchUpCommandTests(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.database = Path(directory.name) / "checks.sqlite3"
        connection = monitor.connect(self.database)
        monitor.add_target(connection, "api", "https://example.com")
        connection.close()
        self.events = []

    def cli(self, *arguments, states=(), webhook_results=()):
        outcomes = iter(webhook_results)
        results = iter(states)

        def probe(*_args):
            self.events.append("check")
            return check(next(results))

        def send(webhook, body):
            outcome = next(outcomes, None)
            self.events.append(body["event"] if outcome is None else f"failed {body['event']}")
            return outcome

        output, errors = io.StringIO(), io.StringIO()
        with patch.object(monitor, "probe", side_effect=probe), patch.object(monitor, "sleep"), \
                patch.object(alerts, "send_webhook", side_effect=send), redirect_stdout(output), \
                redirect_stderr(errors):
            code = monitor.main(["--database", str(self.database), *arguments])
        return code, output.getvalue(), errors.getvalue()

    def test_incidents_from_runs_without_alerting_are_sent_before_the_next_round(self):
        self.assertEqual(self.cli("watch", "--count", "1", "--alert-webhook", WEBHOOK, states=["up"])[0], 0)
        self.assertEqual(self.cli("watch", "--count", "2", states=["down", "down"])[0], 1)
        self.assertEqual(self.cli("run", "api", states=["up"])[0], 0)
        code, output, _ = self.cli("alerts", "--pending")
        self.assertEqual(code, 0)
        self.assertEqual([body["event"] for body in json.loads(output)], ["opened"])
        self.events.clear()
        code, _, errors = self.cli("watch", "--count", "1", "--alert-webhook", WEBHOOK, states=["up"])
        self.assertEqual(code, 0)
        self.assertEqual(self.events, ["opened", "check", "resolved"])
        self.assertIn("Sending 1 incident event(s) not alerted yet.", errors)
        self.assertEqual(json.loads(self.cli("alerts", "--pending")[1]), [])
        self.assertEqual([(row["event"], row["status"]) for row in json.loads(self.cli("alerts")[1])],
                         [("resolved", "sent"), ("opened", "sent")])

    def test_a_catch_up_that_fails_is_retried_after_the_first_round(self):
        self.cli("watch", "--count", "1", "--alert-webhook", WEBHOOK, states=["up"])
        self.cli("run-all", states=["down"])
        self.cli("run-all", states=["down"])
        self.events.clear()
        code, _, errors = self.cli("watch", "--count", "1", "--alert-webhook", WEBHOOK, states=["down"],
                                   webhook_results=["http_503"])
        self.assertEqual(code, 1)
        self.assertEqual(self.events, ["failed opened", "check", "opened"])
        self.assertIn("Alert not delivered (http_503); retrying next round.", errors)

    def test_pending_option_validates_its_limit(self):
        self.assertEqual(self.cli("alerts", "--pending", "--limit", "0")[0], 2)
        self.assertEqual(self.cli("alerts", "--pending", "--limit", "1001")[0], 2)


if __name__ == "__main__":
    unittest.main()
