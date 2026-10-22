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
import routing

DEFAULT = "https://hooks.example/default"
TEAM = "https://team.example/hook?token=secret"
OTHER = "https://other.example/hook"
A, B, C = "https://a.example", "https://b.example", "https://c.example"


def check(state, url):
    return monitor.Check(url, "2026-10-22T12:00:00+00:00", state, 200 if state == "up" else 503,
                         1.0, None if state == "up" else "http_503")


class RoutingTests(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.database = Path(directory.name) / "checks.sqlite3"
        self.connection = monitor.connect(self.database)
        self.addCleanup(self.connection.close)
        self.sent = []
        alerts.skip_backlog(self.connection)

    def record(self, states, url):
        return [monitor.save(self.connection, check(state, url)) for state in states]

    def deliver(self, webhook=DEFAULT, failing=(), **options):
        def send(destination, body):
            if destination in failing:
                return "http_500"
            self.sent.append((destination, body["event"], body["url"]))
            return None

        errors = io.StringIO()
        with patch.object(alerts, "send_webhook", side_effect=send), redirect_stderr(errors):
            alerts.deliver(self.connection, webhook, **options)
        return errors.getvalue()

    def logged(self):
        return [(row["event"], row["url"], row["status"]) for row in alerts.list_alerts(self.connection)][::-1]

    def test_each_target_alerts_its_own_route(self):
        monitor.add_target(self.connection, "a", A)
        monitor.add_target(self.connection, "b", B, alert_webhook=TEAM)
        monitor.add_target(self.connection, "c", C, alert_webhook="none")
        for url in (A, B, C):
            self.record(["down", "down"], url)
        errors = self.deliver()
        self.assertEqual(self.sent, [(DEFAULT, "opened", A), (TEAM, "opened", B)])
        self.assertEqual(self.logged(), [("opened", A, "sent"), ("opened", B, "sent"), ("opened", C, "skipped")])
        self.assertIn("Alert sent to team.example: Incident opened for https://b.example", errors)
        self.assertNotIn("secret", errors)

    def test_a_url_shared_by_targets_alerts_each_distinct_route_once(self):
        monitor.add_target(self.connection, "a1", A)
        monitor.add_target(self.connection, "a2", A, alert_webhook=TEAM)
        monitor.add_target(self.connection, "a3", A, alert_webhook=TEAM)
        monitor.add_target(self.connection, "a4", A, alert_webhook="none")
        monitor.add_target(self.connection, "a5", A, alert_webhook=DEFAULT)
        self.record(["down", "down"], A)
        self.deliver()
        self.assertEqual(self.sent, [(DEFAULT, "opened", A), (TEAM, "opened", A)])
        self.deliver()
        self.assertEqual(len(self.sent), 2)

    def test_a_failing_route_holds_up_only_its_own_alerts(self):
        monitor.add_target(self.connection, "a", A, alert_webhook=TEAM)
        monitor.add_target(self.connection, "b", B)
        monitor.add_target(self.connection, "shared", B, alert_webhook=TEAM)
        self.record(["down", "down"], A)
        self.record(["down", "down"], B)
        errors = self.deliver(failing={TEAM})
        self.assertEqual(self.sent, [(DEFAULT, "opened", B)])
        self.assertEqual(errors.count("Alert not delivered (http_500) to team.example; retrying next round."), 1)
        # B reached the default route but still owes the team webhook, so it isn't recorded yet.
        self.assertEqual(self.logged(), [])
        pending = alerts.list_alerts(self.connection, pending=True)
        self.assertEqual([(body["url"], body["routes"]) for body in pending], [(A, [TEAM]), (B, [TEAM])])
        self.record(["up", "up"], B)
        self.deliver(failing={TEAM})
        self.assertEqual(self.sent[-1], (DEFAULT, "resolved", B))
        self.sent.clear()
        self.deliver()
        self.assertEqual(self.sent, [(TEAM, "opened", A), (TEAM, "opened", B), (TEAM, "resolved", B)])
        self.assertEqual(self.logged(), [("opened", A, "sent"), ("opened", B, "sent"), ("resolved", B, "sent")])

    def test_a_target_webhook_reached_earlier_is_not_resent_as_the_default(self):
        monitor.add_target(self.connection, "a", A, alert_webhook=TEAM)
        monitor.add_target(self.connection, "b", A)
        self.record(["down", "down"], A)
        self.deliver(webhook=None)
        self.assertEqual(self.sent, [(TEAM, "opened", A)])
        self.deliver(webhook=TEAM)
        self.assertEqual(self.sent, [(TEAM, "opened", A)])
        self.assertEqual(self.logged(), [("opened", A, "sent")])

    def test_a_route_reached_before_a_crash_is_not_sent_again(self):
        monitor.add_target(self.connection, "a", A)
        monitor.add_target(self.connection, "shared", A, alert_webhook=TEAM)
        self.record(["down", "down"], A)
        self.deliver(failing={TEAM})
        self.sent.clear()
        # The watcher restarts; only the route that never accepted the alert gets it.
        self.deliver()
        self.assertEqual(self.sent, [(TEAM, "opened", A)])

    def test_routes_are_read_when_the_alert_is_sent(self):
        monitor.add_target(self.connection, "a", A, alert_webhook=TEAM)
        self.record(["down", "down"], A)
        self.deliver(failing={TEAM})
        monitor.update_target(self.connection, "a", alert_webhook=OTHER)
        self.deliver()
        self.assertEqual(self.sent, [(OTHER, "opened", A)])
        # A route removed after the alert reached another one no longer holds it up.
        monitor.add_target(self.connection, "shared", A, alert_webhook=TEAM)
        self.record(["up", "up"], A)
        self.deliver(failing={TEAM})
        self.assertEqual(self.sent[-1], (OTHER, "resolved", A))
        self.assertEqual(alerts.list_alerts(self.connection, pending=True)[0]["routes"], [TEAM])
        monitor.update_target(self.connection, "shared", alert_webhook="none")
        self.deliver()
        self.assertEqual(len(self.sent), 2)
        self.assertEqual(self.logged(), [("opened", A, "sent"), ("resolved", A, "sent")])
        self.assertEqual(alerts.list_alerts(self.connection, pending=True), [])

    def test_without_a_default_webhook_default_routes_wait(self):
        monitor.add_target(self.connection, "a", A)
        monitor.add_target(self.connection, "b", B, alert_webhook=TEAM)
        self.record(["down", "down"], A)
        self.record(["down", "down"], B)
        self.deliver(webhook=None)
        self.assertEqual(self.sent, [(TEAM, "opened", B)])
        self.assertEqual([(body["url"], body["routes"]) for body in alerts.list_alerts(self.connection, pending=True)],
                         [(A, ["default"])])
        self.deliver()
        self.assertEqual(self.sent[-1], (DEFAULT, "opened", A))

    def test_skip_resolved_only_skips_incidents_that_reached_no_route(self):
        monitor.add_target(self.connection, "a", A)
        monitor.add_target(self.connection, "shared", A, alert_webhook=TEAM)
        monitor.add_target(self.connection, "b", B, alert_webhook=TEAM)
        self.record(["down", "down"], A)
        self.record(["down", "down"], B)
        self.deliver(failing={TEAM})
        self.record(["up", "up"], A)
        self.record(["up", "up"], B)
        self.sent.clear()
        errors = self.deliver(skip_resolved=True)
        # A's opening reached the default route, so every route hears it open and close; B reached none.
        self.assertEqual(self.sent, [(TEAM, "opened", A), (DEFAULT, "resolved", A), (TEAM, "resolved", A)])
        self.assertIn("Skipped 1 incident(s)", errors)
        self.assertEqual([(event, url, status) for event, url, status in self.logged() if url == B],
                         [("opened", B, "skipped"), ("resolved", B, "skipped")])


class AlerterTests(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.connection = monitor.connect(Path(directory.name) / "checks.sqlite3")
        self.addCleanup(self.connection.close)
        monitor.add_target(self.connection, "a", A)
        self.sent = []

    def run_alerter(self, alerter):
        with patch.object(alerts, "send_webhook", side_effect=lambda url, body: self.sent.append(url)), \
                redirect_stderr(io.StringIO()):
            return alerter()

    def test_a_watcher_without_a_webhook_starts_when_a_target_gets_one(self):
        alerter = alerts.Alerter(self.connection)
        self.assertEqual(self.run_alerter(alerter), [])
        self.assertIsNone(alerts.enabled_after(self.connection))
        for state in ("down", "down"):
            monitor.save(self.connection, check(state, A))
        monitor.update_target(self.connection, "a", alert_webhook=TEAM)
        # Like any first start, events already in history are skipped.
        self.assertEqual(self.run_alerter(alerter), [])
        self.assertEqual(alerts.enabled_after(self.connection), 2)
        for state in ("up", "up"):
            monitor.save(self.connection, check(state, A))
        self.assertEqual([body["event"] for body in self.run_alerter(alerter)], ["resolved"])
        self.assertEqual(self.sent, [TEAM])


class RoutingStorageTests(unittest.TestCase):
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

    def test_add_and_update_save_the_alert_webhook(self):
        code, output, _ = self.cli("add", "api", A, "--alert-webhook", TEAM)
        self.assertEqual((code, json.loads(output)["alert_webhook"]), (0, TEAM))
        for value in ("none", "default", OTHER):
            with self.subTest(value=value):
                code, output, _ = self.cli("update", "api", "--alert-webhook", value)
                self.assertEqual((code, json.loads(output)["alert_webhook"]), (0, value))
        self.assertEqual(json.loads(self.cli("update", "api", "--timeout", "2")[1])["alert_webhook"], OTHER)

    def test_invalid_alert_webhooks_are_rejected(self):
        monitor.add_target(self.connection, "api", A)
        for value in ("ftp://hooks.example", "nowhere", "https://user:pw@hooks.example", "https://hooks.example/#x",
                      "Default"):
            with self.subTest(value=value):
                code, _, errors = self.cli("update", "api", "--alert-webhook", value)
                self.assertEqual(code, 2)
                self.assertIn("Alert webhook must be default, none, or a webhook URL.", errors)
                self.assertNotIn("pw", errors)
                self.assertEqual(self.cli("add", "other", B, "--alert-webhook", value)[0], 2)
        self.assertEqual(monitor.get_target(self.connection, "api")["alert_webhook"], "default")
        self.assertEqual([target["name"] for target in monitor.list_targets(self.connection)], ["api"])
        with self.assertRaises(sqlite3.IntegrityError), self.connection:
            self.connection.execute("UPDATE targets SET alert_webhook = 'sometimes'")
        with self.assertRaises(sqlite3.IntegrityError), self.connection:
            self.connection.execute("INSERT INTO deliveries (first_failure_id, event, route, recorded_at) "
                                    "VALUES (1, 'closed', 'default', '2026-10-22')")

    def test_the_api_shows_a_target_webhook_only_as_webhook(self):
        monitor.add_target(self.connection, "a", A)
        monitor.add_target(self.connection, "b", B, alert_webhook=TEAM)
        monitor.add_target(self.connection, "c", C, alert_webhook="none")
        served = [routing.public(target) for target in monitor.list_targets(self.connection)]
        self.assertEqual([target["alert_webhook"] for target in served], ["default", "webhook", "none"])
        self.assertEqual(monitor.get_target(self.connection, "b")["alert_webhook"], TEAM)

    def test_database_from_before_routing_keeps_alerting_the_default_webhook(self):
        legacy_path = self.database.with_name("legacy.sqlite3")
        with sqlite3.connect(legacy_path) as legacy:
            legacy.execute("CREATE TABLE checks (id INTEGER PRIMARY KEY, url TEXT NOT NULL, checked_at TEXT NOT NULL, "
                           "state TEXT NOT NULL, status_code INTEGER, latency_ms REAL NOT NULL, error TEXT)")
            legacy.execute("CREATE TABLE targets (name TEXT PRIMARY KEY, url TEXT NOT NULL, timeout REAL NOT NULL, "
                           "deadline REAL)")
            legacy.execute("CREATE TABLE alerts (id INTEGER PRIMARY KEY, url TEXT NOT NULL, first_failure_id INTEGER "
                           "NOT NULL, event TEXT NOT NULL, check_id INTEGER NOT NULL, status TEXT NOT NULL, "
                           "recorded_at TEXT NOT NULL, UNIQUE(first_failure_id, event))")
            legacy.execute("CREATE TABLE alerting (id INTEGER PRIMARY KEY, enabled_at TEXT NOT NULL, "
                           "after_check_id INTEGER NOT NULL)")
            legacy.execute("INSERT INTO targets VALUES ('api', 'https://a.example', 2, NULL)")
            legacy.execute("INSERT INTO alerting VALUES (1, '2026-10-21', 0)")
            legacy.executemany("INSERT INTO checks (url, checked_at, state, latency_ms) VALUES (?, ?, ?, 1)",
                               [(A, "2026-10-21T00:00:00+00:00", state) for state in ("down", "down", "up", "up")])
            legacy.execute("INSERT INTO alerts VALUES (1, 'https://a.example', 1, 'opened', 2, 'sent', '2026-10-21')")
        legacy.close()
        upgraded = monitor.connect(legacy_path)
        self.addCleanup(upgraded.close)
        self.assertEqual(monitor.get_target(upgraded, "api")["alert_webhook"], "default")
        sent = []
        with patch.object(alerts, "send_webhook", side_effect=lambda url, body: sent.append((url, body["event"]))), \
                redirect_stderr(io.StringIO()):
            alerts.Alerter(upgraded, DEFAULT)()
        self.assertEqual(sent, [(DEFAULT, "resolved")])


class RoutingCommandTests(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.database = Path(directory.name) / "checks.sqlite3"
        self.sent = []

    def cli(self, *arguments, states=()):
        results = iter(states)

        def send(webhook, body):
            self.sent.append((webhook, body["url"]))

        output, errors = io.StringIO(), io.StringIO()
        with patch.object(monitor, "probe", side_effect=lambda url, *_args: check(next(results), url)), \
                patch.object(monitor, "sleep"), patch.object(alerts, "send_webhook", side_effect=send), \
                redirect_stdout(output), redirect_stderr(errors):
            code = monitor.main(["--database", str(self.database), *arguments])
        return code, output.getvalue(), errors.getvalue()

    def test_watch_without_a_default_webhook_alerts_targets_that_have_one(self):
        self.cli("add", "a", A)
        self.cli("add", "b", B, "--alert-webhook", TEAM)
        code, _, _ = self.cli("watch", "--count", "3", "--skip-resolved", states=["up", "up"] + ["down"] * 4)
        self.assertEqual(code, 1)
        self.assertEqual(self.sent, [(TEAM, B)])
        pending = json.loads(self.cli("alerts", "--pending")[1])
        self.assertEqual([(body["url"], body["routes"]) for body in pending], [(A, ["default"])])
        self.assertEqual(json.loads(self.cli("targets")[1])[1]["alert_webhook"], TEAM)

    def test_skip_resolved_with_nowhere_to_send_exits_two(self):
        self.cli("add", "a", A, "--alert-webhook", "none")
        code, _, errors = self.cli("watch", "--count", "1", "--skip-resolved", states=["up"])
        self.assertEqual(code, 2)
        self.assertIn("a target with its own webhook", errors)


if __name__ == "__main__":
    unittest.main()
