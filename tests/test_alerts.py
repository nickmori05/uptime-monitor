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


class AlertTests(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.database = Path(directory.name) / "checks.sqlite3"
        self.connection = monitor.connect(self.database)
        self.addCleanup(self.connection.close)
        self.sent = []

    def check(self, state, url="https://example.com"):
        return monitor.Check(url, "2026-10-19T12:00:00+00:00", state, 200 if state == "up" else 503,
                             1.0, None if state == "up" else "http_503")

    def record(self, states, url="https://example.com"):
        return [monitor.save(self.connection, self.check(state, url)) for state in states]

    def accept(self, webhook, body):
        self.sent.append(body)
        return None

    def deliver(self):
        with patch.object(alerts, "send_webhook", side_effect=self.accept), redirect_stderr(io.StringIO()):
            return alerts.deliver(self.connection, "https://hooks.example/alert")

    def test_opening_and_resolution_are_each_sent_once(self):
        ids = self.record(["down", "down"])
        self.assertEqual([body["event"] for body in self.deliver()], ["opened"])
        self.assertEqual(self.deliver(), [])
        self.record(["up", "up"])
        resolved, = self.deliver()
        self.assertEqual(self.deliver(), [])
        self.assertEqual(resolved["event"], "resolved")
        self.assertEqual(resolved["incident"]["first_failure_id"], ids[0])
        self.assertIn("Incident resolved for https://example.com", resolved["text"])
        self.assertEqual([(row["event"], row["status"]) for row in alerts.list_alerts(self.connection)],
                         [("resolved", "sent"), ("opened", "sent")])

    def test_flapping_and_extra_failures_do_not_alert_again(self):
        self.record(["down", "down", "down", "up", "down", "up"])
        self.assertEqual([body["event"] for body in self.deliver()], ["opened"])
        self.record(["down", "down"])
        self.assertEqual(self.deliver(), [])

    def test_a_new_incident_after_recovery_alerts_again(self):
        self.record(["down", "down", "up", "up", "down", "down"])
        bodies = self.deliver()
        self.assertEqual([body["event"] for body in bodies], ["opened", "resolved", "opened"])
        self.assertNotEqual(bodies[0]["incident"]["first_failure_id"], bodies[2]["incident"]["first_failure_id"])

    def test_events_are_sent_in_check_order_across_urls(self):
        self.record(["down"], "https://a.example")
        self.record(["down", "down"], "https://b.example")
        self.record(["down"], "https://a.example")
        self.assertEqual([body["url"] for body in self.deliver()], ["https://b.example", "https://a.example"])

    def test_a_failed_delivery_is_retried_and_stops_the_round(self):
        self.record(["down", "down", "up", "up"])
        output = io.StringIO()
        with patch.object(alerts, "send_webhook", return_value="http_500") as send, redirect_stderr(output):
            self.assertEqual(alerts.deliver(self.connection, "https://hooks.example/alert"), [])
        send.assert_called_once()
        self.assertIn("Alert not delivered (http_500)", output.getvalue())
        self.assertEqual(alerts.list_alerts(self.connection), [])
        self.assertEqual([body["event"] for body in self.deliver()], ["opened", "resolved"])

    def test_backlog_is_skipped_and_never_sent(self):
        self.record(["down", "down", "up", "up", "down", "down"])
        self.assertEqual(alerts.skip_backlog(self.connection), 3)
        self.assertEqual(alerts.skip_backlog(self.connection), 0)
        self.assertEqual(self.deliver(), [])
        self.assertEqual({row["status"] for row in alerts.list_alerts(self.connection)}, {"skipped"})
        self.record(["up", "up"])
        self.assertEqual([body["event"] for body in self.deliver()], ["resolved"])

    def test_storage_constraints_reject_duplicates_and_unknown_values(self):
        self.record(["down", "down"])
        event, = alerts.pending_alerts(self.connection)
        alerts.record(self.connection, [event], "sent")
        alerts.record(self.connection, [event], "skipped")
        row, = alerts.list_alerts(self.connection)
        self.assertEqual(row["status"], "sent")
        with self.assertRaises(sqlite3.IntegrityError):
            alerts.record(self.connection, [{**event, "event": "closed"}], "sent")
        with self.assertRaises(sqlite3.IntegrityError):
            alerts.record(self.connection, [{**event, "event": "resolved"}], "queued")

    def test_list_limit_is_validated(self):
        for limit in (0, 1001, True, 2.5):
            with self.subTest(limit=limit), self.assertRaises(ValueError):
                alerts.list_alerts(self.connection, limit)

    def test_older_database_gains_an_empty_alert_table(self):
        legacy_path = self.database.with_name("legacy.sqlite3")
        with sqlite3.connect(legacy_path) as legacy:
            legacy.execute("CREATE TABLE checks (id INTEGER PRIMARY KEY, url TEXT NOT NULL, checked_at TEXT NOT NULL, "
                           "state TEXT NOT NULL, status_code INTEGER, latency_ms REAL NOT NULL, error TEXT)")
            legacy.execute("CREATE TABLE targets (name TEXT PRIMARY KEY, url TEXT NOT NULL, timeout REAL NOT NULL)")
            legacy.execute("INSERT INTO checks (url, checked_at, state, latency_ms) "
                           "VALUES ('https://example.com', '2026-10-01T00:00:00+00:00', 'down', 1)")
        legacy.close()
        upgraded = monitor.connect(legacy_path)
        self.addCleanup(upgraded.close)
        self.assertEqual(alerts.list_alerts(upgraded), [])
        self.assertEqual(len(monitor.history(upgraded)), 1)


class AlertCommandTests(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.database = Path(directory.name) / "checks.sqlite3"
        connection = monitor.connect(self.database)
        monitor.add_target(connection, "api", "https://example.com")
        connection.close()
        self.sent = []

    def check(self, state):
        return monitor.Check("https://example.com", "2026-10-19T12:00:00+00:00", state,
                             200 if state == "up" else 503, 1.0, None if state == "up" else "http_503")

    def cli(self, *arguments, states=()):
        output, errors = io.StringIO(), io.StringIO()
        with patch.object(monitor, "probe", side_effect=[self.check(state) for state in states]), \
                patch.object(monitor, "sleep"), \
                patch.object(alerts, "send_webhook", side_effect=lambda url, body: self.sent.append((url, body))), \
                redirect_stdout(output), redirect_stderr(errors):
            code = monitor.main(["--database", str(self.database), *arguments])
        return code, output.getvalue(), errors.getvalue()

    def test_watch_sends_each_change_once_across_restarts(self):
        webhook = "https://hooks.example/alert?token=abc"
        code, output, errors = self.cli("watch", "--count", "4", "--alert-webhook", webhook,
                                        states=["down", "down", "up", "up"])
        self.assertEqual(code, 1)
        self.assertEqual(len(output.splitlines()), 4)
        self.assertEqual([(url, body["event"]) for url, body in self.sent],
                         [(webhook, "opened"), (webhook, "resolved")])
        self.assertIn("Alert sent: Incident opened for https://example.com", errors)
        self.assertEqual(self.cli("watch", "--count", "1", "--alert-webhook", webhook, states=["up"])[0], 0)
        self.assertEqual(len(self.sent), 2)
        code, output, _ = self.cli("alerts", "--limit", "5")
        self.assertEqual(code, 0)
        self.assertEqual([(row["event"], row["status"]) for row in json.loads(output)],
                         [("resolved", "sent"), ("opened", "sent")])

    def test_restarted_watch_skips_incidents_from_before_alerting(self):
        self.assertEqual(self.cli("watch", "--count", "2", states=["down", "down"])[0], 1)
        code, _, errors = self.cli("watch", "--count", "1", "--alert-webhook", "https://hooks.example/a",
                                   states=["down"])
        self.assertEqual(code, 1)
        self.assertEqual(self.sent, [])
        self.assertIn("Skipped 1 earlier incident event(s)", errors)

    def test_watch_without_a_webhook_records_no_alerts(self):
        self.cli("watch", "--count", "2", states=["down", "down"])
        self.assertEqual(json.loads(self.cli("alerts")[1]), [])
        self.assertEqual(self.sent, [])

    def test_invalid_webhooks_and_limits_exit_two_without_checking(self):
        invalid = ("ftp://hooks.example", "https://user:secret@hooks.example", "https://hooks.example/#x", "hooks")
        for webhook in invalid:
            with self.subTest(webhook=webhook), redirect_stderr(io.StringIO()), self.assertRaises(SystemExit) as exit:
                monitor.main(["--database", str(self.database), "watch", "--alert-webhook", webhook])
            self.assertEqual(exit.exception.code, 2)
        self.assertEqual(self.cli("alerts", "--limit", "0")[0], 2)
        self.assertEqual(self.sent, [])

    def test_storage_errors_while_alerting_exit_two(self):
        with patch.object(alerts, "pending_alerts", side_effect=sqlite3.OperationalError("locked")):
            code, _, errors = self.cli("watch", "--count", "1", "--alert-webhook", "https://hooks.example/a",
                                       states=["up"])
        self.assertEqual(code, 2)
        self.assertIn("locked", errors)


if __name__ == "__main__":
    unittest.main()
