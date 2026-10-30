from contextlib import redirect_stderr, redirect_stdout
from datetime import datetime, timedelta, timezone
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
import monitor
import webhooks

DEFAULT = "https://hooks.example/default"
TEAM = "https://team.example/hook?token=secret"
OTHER = "https://other.example/hook"
A, B, C = "https://a.example", "https://b.example", "https://c.example"
START = datetime(2026, 10, 25, 12, 0, tzinfo=timezone.utc)


def check(state, url, minute=0):
    return monitor.Check(url, (START + timedelta(minutes=minute)).isoformat(), state,
                         200 if state == "up" else 503, 1.0, None if state == "up" else "http_503")


class HealthTests(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.database = Path(directory.name) / "checks.sqlite3"
        self.connection = monitor.connect(self.database)
        self.addCleanup(self.connection.close)
        alerts.skip_backlog(self.connection)
        self.minute = 0
        clock = patch.object(webhooks, "now", side_effect=lambda: START + timedelta(minutes=self.minute))
        clock.start()
        self.addCleanup(clock.stop)

    def at(self, minute):
        return (START + timedelta(minutes=minute)).isoformat()

    def record(self, states, url):
        for state in states:
            monitor.save(self.connection, check(state, url, self.minute))

    def deliver(self, failing=(), webhook=DEFAULT):
        def send(destination, _body):
            if isinstance(failing, dict):
                return failing.get(destination)
            return "http_500" if destination in failing else None

        with patch.object(alerts, "send_webhook", side_effect=send), redirect_stderr(io.StringIO()):
            alerts.deliver(self.connection, webhook)

    def health(self):
        return {row["route"]: row for row in alerts.webhook_health(self.connection)}

    def test_an_empty_database_has_no_webhooks(self):
        self.assertEqual(alerts.webhook_health(self.connection), [])

    def test_every_route_a_target_names_is_listed_before_it_is_tried(self):
        monitor.add_target(self.connection, "a", A)
        monitor.add_target(self.connection, "b", B, alert_webhook=TEAM)
        monitor.add_target(self.connection, "b2", B, alert_webhook=TEAM)
        monitor.add_target(self.connection, "c", C, alert_webhook="none")
        self.assertEqual(alerts.webhook_health(self.connection), [
            {"route": "default", "targets": ["a"], "state": "untried", "pending": 0, "oldest_pending_at": None,
             "last_success_at": None, "last_failure_at": None, "last_error": None, "consecutive_failures": 0},
            {"route": TEAM, "targets": ["b", "b2"], "state": "untried", "pending": 0, "oldest_pending_at": None,
             "last_success_at": None, "last_failure_at": None, "last_error": None, "consecutive_failures": 0},
        ])

    def test_a_failing_webhook_shows_its_queue_and_failures_until_it_recovers(self):
        monitor.add_target(self.connection, "a", A)
        monitor.add_target(self.connection, "b", B, alert_webhook=TEAM)
        self.record(["down", "down"], A)
        self.record(["down", "down"], B)
        self.deliver(failing={TEAM})
        self.minute = 1
        self.record(["up", "up"], B)
        self.deliver(failing={TEAM: "timeout"})
        health = self.health()
        self.assertEqual({key: health["default"][key] for key in ("state", "pending", "last_success_at")},
                         {"state": "ok", "pending": 0, "last_success_at": self.at(0)})
        team = health[TEAM]
        self.assertEqual((team["state"], team["pending"], team["consecutive_failures"]), ("failing", 2, 2))
        # The queue is as old as the earliest event still waiting: B's opening, confirmed at minute 0.
        self.assertEqual((team["oldest_pending_at"], team["last_failure_at"]), (self.at(0), self.at(1)))
        self.assertEqual((team["last_error"], team["last_success_at"]), ("timeout", None))
        self.minute = 2
        self.deliver()
        team = self.health()[TEAM]
        self.assertEqual((team["state"], team["pending"], team["oldest_pending_at"]), ("ok", 0, None))
        self.assertEqual((team["consecutive_failures"], team["last_success_at"]), (0, self.at(2)))
        # The last failure stays on record after a success.
        self.assertEqual((team["last_failure_at"], team["last_error"]), (self.at(1), "timeout"))

    def test_only_routes_an_alert_still_needs_count_toward_their_queue(self):
        monitor.add_target(self.connection, "a", A)
        monitor.add_target(self.connection, "shared", A, alert_webhook=TEAM)
        self.record(["down", "down"], A)
        self.deliver(failing={TEAM})
        health = self.health()
        self.assertEqual((health["default"]["pending"], health[TEAM]["pending"]), (0, 1))
        self.assertEqual(health[TEAM]["targets"], ["shared"])

    def test_alerts_for_a_url_without_targets_queue_on_the_default_route(self):
        self.record(["down", "down"], A)
        self.deliver(webhook=None)
        row, = alerts.webhook_health(self.connection)
        self.assertEqual((row["route"], row["targets"], row["state"], row["pending"]), ("default", [], "untried", 1))

    def test_a_removed_webhook_keeps_its_history(self):
        monitor.add_target(self.connection, "b", B, alert_webhook=TEAM)
        self.record(["down", "down"], B)
        self.deliver(failing={TEAM})
        monitor.update_target(self.connection, "b", alert_webhook=OTHER)
        self.deliver()
        health = self.health()
        self.assertEqual(list(health), [OTHER, TEAM])
        self.assertEqual((health[TEAM]["targets"], health[TEAM]["state"], health[TEAM]["pending"]), ([], "failing", 0))
        self.assertEqual((health[OTHER]["targets"], health[OTHER]["state"]), (["b"], "ok"))

    def test_nothing_is_queued_before_alerting_is_first_enabled(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        fresh = monitor.connect(Path(directory.name) / "fresh.sqlite3")
        self.addCleanup(fresh.close)
        monitor.add_target(fresh, "a", A)
        for state in ("down", "down"):
            monitor.save(fresh, check(state, A))
        self.assertEqual(alerts.webhook_health(fresh)[0]["pending"], 0)

    def test_failure_counts_are_rejected_below_zero(self):
        with self.assertRaises(sqlite3.IntegrityError), self.connection:
            self.connection.execute("INSERT INTO webhooks (route, failures) VALUES ('default', -1)")


class HealthCommandTests(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.database = Path(directory.name) / "checks.sqlite3"

    def cli(self, *arguments, states=(), failing=()):
        results = iter(states)
        output, errors = io.StringIO(), io.StringIO()
        with patch.object(monitor, "probe", side_effect=lambda url, *_args: check(next(results), url)), \
                patch.object(monitor, "sleep"), \
                patch.object(alerts, "send_webhook", side_effect=lambda url, _body: "http_502" if url in failing
                             else None), redirect_stdout(output), redirect_stderr(errors):
            code = monitor.main(["--database", str(self.database), *arguments])
        return code, output.getvalue(), errors.getvalue()

    def test_webhooks_reports_what_the_watcher_saw(self):
        self.cli("add", "a", A)
        self.cli("add", "b", B, "--alert-webhook", TEAM)
        self.cli("watch", "--count", "4", "--alert-webhook", DEFAULT, states=["up"] * 2 + ["down"] * 6,
                 failing={TEAM})
        code, output, _ = self.cli("webhooks")
        self.assertEqual(code, 0)
        rows = json.loads(output)
        self.assertEqual([(row["route"], row["state"], row["pending"], row["consecutive_failures"], row["last_error"])
                          for row in rows], [("default", "ok", 0, 0, None), (TEAM, "failing", 1, 2, "http_502")])

    def test_a_database_from_before_webhook_health_is_upgraded(self):
        with sqlite3.connect(self.database) as legacy:
            legacy.execute("CREATE TABLE targets (name TEXT PRIMARY KEY, url TEXT NOT NULL, timeout REAL NOT NULL)")
            legacy.execute("INSERT INTO targets VALUES ('a', 'https://a.example', 2)")
        legacy.close()
        code, output, _ = self.cli("webhooks")
        self.assertEqual((code, [row["route"] for row in json.loads(output)]), (0, ["default"]))


class HealthApiTests(unittest.TestCase):
    def test_the_api_names_a_target_webhook_only_by_its_host(self):
        with tempfile.TemporaryDirectory() as directory:
            database = Path(directory) / "checks.sqlite3"
            connection = monitor.connect(database)
            self.addCleanup(connection.close)
            monitor.add_target(connection, "a", A)
            monitor.add_target(connection, "b", B, alert_webhook=TEAM)
            alerts.skip_backlog(connection)
            for state in ("down", "down"):
                monitor.save(connection, check(state, B))
            with patch.object(alerts, "send_webhook", return_value="connection_error"), \
                    redirect_stderr(io.StringIO()):
                alerts.deliver(connection, None)
            server = api.make_server(database, "127.0.0.1", 0, monitor)
            thread = Thread(target=server.serve_forever, daemon=True)
            thread.start()
            try:
                with urlopen(f"http://127.0.0.1:{server.server_port}/api/webhooks", timeout=5) as response:
                    raw = response.read()
            finally:
                server.shutdown()
                server.server_close()
                thread.join()
        self.assertNotIn(b"secret", raw)
        self.assertNotIn(b"/hook", raw)
        default, team = json.loads(raw)
        self.assertEqual((default["route"], default["host"], default["targets"]), ("default", None, ["a"]))
        self.assertEqual((team["route"], team["host"], team["targets"]), ("webhook", "team.example", ["b"]))
        self.assertEqual((team["state"], team["pending"], team["last_error"]), ("failing", 1, "connection_error"))


if __name__ == "__main__":
    unittest.main()
