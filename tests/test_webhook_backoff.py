from contextlib import redirect_stderr, redirect_stdout
from datetime import datetime, timedelta, timezone
import io
import json
from pathlib import Path
import sqlite3
import tempfile
import unittest
from unittest.mock import patch

import alerts
import monitor
import webhooks

DEFAULT = "https://hooks.example/default"
TEAM = "https://team.example/hook?token=secret"
A, B = "https://a.example", "https://b.example"
START = datetime(2026, 10, 25, 12, 0, tzinfo=timezone.utc)


def later(seconds):
    return (START + timedelta(seconds=seconds)).isoformat()


def check(state, url):
    return monitor.Check(url, START.isoformat(), state, 200 if state == "up" else 503, 1.0,
                         None if state == "up" else "http_503")


class BackoffTests(unittest.TestCase):
    def test_the_delay_doubles_from_the_first_up_to_the_limit(self):
        backoff = webhooks.Backoff(30, 300)
        self.assertEqual([backoff.delay(failures) for failures in range(1, 7)], [30, 60, 120, 240, 300, 300])
        self.assertEqual(backoff.delay(10**6), 300)
        self.assertEqual(webhooks.Backoff(30, 30).delay(5), 30)

    def test_invalid_backoffs_are_rejected(self):
        for first, limit, message in ((30, 29, "at least the interval"), (30, 86401, "at most 86400"),
                                      (0, 30, "greater than 0"), (30, float("nan"), "number of seconds"),
                                      (30, True, "number of seconds"), (float("inf"), 30, "number of seconds")):
            with self.subTest(first=first, limit=limit), self.assertRaisesRegex(ValueError, message):
                webhooks.Backoff(first, limit)


class BackoffDeliveryTests(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.database = Path(directory.name) / "checks.sqlite3"
        self.connection = monitor.connect(self.database)
        self.addCleanup(self.connection.close)
        alerts.skip_backlog(self.connection)
        monitor.add_target(self.connection, "a", A)
        monitor.add_target(self.connection, "b", B, alert_webhook=TEAM)
        self.seconds = 0
        clock = patch.object(webhooks, "now", side_effect=lambda: START + timedelta(seconds=self.seconds))
        clock.start()
        self.addCleanup(clock.stop)
        self.tried = []
        self.backoff = webhooks.Backoff(30, 120)

    def record(self, states, url):
        for state in states:
            monitor.save(self.connection, check(state, url))

    def deliver(self, at, failing=(), **options):
        self.seconds = at

        def send(destination, body):
            self.tried.append((at, destination, body["event"], body["url"]))
            return "http_503" if destination in failing else None

        errors = io.StringIO()
        with patch.object(alerts, "send_webhook", side_effect=send), redirect_stderr(errors):
            alerts.deliver(self.connection, DEFAULT, **options)
        return errors.getvalue()

    def team_attempts(self):
        return [at for at, destination, _, _ in self.tried if destination == TEAM]

    def test_a_failing_webhook_is_left_alone_until_its_retry_time(self):
        self.record(["down", "down"], B)
        errors = self.deliver(0, {TEAM}, backoff=self.backoff)
        self.assertIn(f"Alert not delivered (http_503) to team.example; retrying after {later(30)}.", errors)
        self.assertNotIn("secret", errors)
        # Rounds every 10 seconds: the waits are 30, 60, 120, then 120 again at the limit.
        for at in range(10, 400, 10):
            self.deliver(at, {TEAM}, backoff=self.backoff)
        self.assertEqual(self.team_attempts(), [0, 30, 90, 210, 330])
        row, = [row for row in alerts.webhook_health(self.connection) if row["route"] == TEAM]
        self.assertEqual((row["consecutive_failures"], row["retry_at"]), (5, later(450)))
        self.assertEqual(alerts.list_alerts(self.connection, pending=True)[0]["routes"], [TEAM])

    def test_other_webhooks_carry_on_while_one_backs_off(self):
        self.record(["down", "down"], B)
        self.deliver(0, {TEAM}, backoff=self.backoff)
        self.record(["down", "down"], A)
        self.deliver(10, {TEAM}, backoff=self.backoff)
        self.assertEqual([(at, destination) for at, destination, _, _ in self.tried], [(0, TEAM), (10, DEFAULT)])

    def test_a_success_resets_the_backoff(self):
        self.record(["down", "down"], B)
        self.deliver(0, {TEAM}, backoff=self.backoff)
        self.deliver(30, {TEAM}, backoff=self.backoff)
        self.deliver(90, backoff=self.backoff)
        self.record(["up", "up"], B)
        self.deliver(100, {TEAM}, backoff=self.backoff)
        self.deliver(120, {TEAM}, backoff=self.backoff)
        self.deliver(130, backoff=self.backoff)
        # After the success at 90, the next failure waits the first 30 seconds again.
        self.assertEqual(self.team_attempts(), [0, 30, 90, 100, 130])
        row, = [row for row in alerts.webhook_health(self.connection) if row["route"] == TEAM]
        self.assertEqual((row["state"], row["consecutive_failures"], row["retry_at"], row["pending"]),
                         ("ok", 0, None, 0))

    def test_alerts_stay_in_order_behind_a_backoff(self):
        self.record(["down", "down"], B)
        self.deliver(0, {TEAM}, backoff=self.backoff)
        self.record(["up", "up"], B)
        self.deliver(10, backoff=self.backoff)
        self.assertEqual(self.team_attempts(), [0])
        self.deliver(30, backoff=self.backoff)
        self.assertEqual([(at, event) for at, destination, event, _ in self.tried if destination == TEAM],
                         [(0, "opened"), (30, "opened"), (30, "resolved")])

    def test_retry_now_tries_a_webhook_that_is_backing_off(self):
        self.record(["down", "down"], B)
        self.deliver(0, {TEAM}, backoff=self.backoff)
        self.deliver(10, {TEAM}, backoff=self.backoff, retry_now=True)
        self.deliver(20, {TEAM}, backoff=self.backoff)
        # The forced attempt counts as the second failure, so the next wait is 60 seconds.
        self.deliver(69, {TEAM}, backoff=self.backoff)
        self.deliver(70, {TEAM}, backoff=self.backoff)
        self.assertEqual(self.team_attempts(), [0, 10, 70])

    def test_a_watcher_without_backoff_retries_every_round_and_clears_the_retry_time(self):
        self.record(["down", "down"], B)
        self.deliver(0, {TEAM}, backoff=self.backoff)
        self.deliver(10, {TEAM})
        self.deliver(20, {TEAM}, backoff=self.backoff)
        self.assertEqual(self.team_attempts(), [0, 10, 20])
        errors = self.deliver(30, {TEAM})
        self.assertIn("retrying next round.", errors)
        row, = [row for row in alerts.webhook_health(self.connection) if row["route"] == TEAM]
        self.assertIsNone(row["retry_at"])

    def test_a_starting_watcher_tries_every_webhook_once(self):
        self.record(["down", "down"], B)
        self.deliver(0, {TEAM}, backoff=self.backoff)
        self.seconds = 5
        with patch.object(alerts, "send_webhook", side_effect=lambda url, body: self.tried.append((5, url)) or None), \
                redirect_stderr(io.StringIO()):
            sent = alerts.Alerter(self.connection, DEFAULT, backoff=self.backoff)()
        self.assertEqual([body["url"] for body in sent], [B])
        self.assertEqual(self.tried[-1], (5, TEAM))


class BackoffCommandTests(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.database = Path(directory.name) / "checks.sqlite3"
        self.tried = []
        self.seconds = 0

    def cli(self, *arguments, states=()):
        results = iter(states)

        def send(url, _body):
            self.tried.append(self.seconds)
            return "http_503"

        def wait(seconds):
            self.seconds += seconds

        output, errors = io.StringIO(), io.StringIO()
        with patch.object(monitor, "probe", side_effect=lambda url, *_args: check(next(results), url)), \
                patch.object(monitor, "sleep", side_effect=wait), \
                patch.object(alerts, "send_webhook", side_effect=send), \
                patch.object(webhooks, "now", side_effect=lambda: START + timedelta(seconds=self.seconds)), \
                redirect_stdout(output), redirect_stderr(errors):
            code = monitor.main(["--database", str(self.database), *arguments])
        return code, output.getvalue(), errors.getvalue()

    def test_watch_backs_off_from_one_interval_up_to_the_limit(self):
        self.cli("add", "b", B, "--alert-webhook", TEAM)
        code, _, errors = self.cli("watch", "--interval", "10", "--count", "20", "--max-backoff", "40",
                                   states=["up"] + ["down"] * 19)
        self.assertEqual(code, 1)
        # The incident opens in the third round (20 s); waits of 10, 20, 40, then 40 at the limit.
        self.assertEqual(self.tried, [20, 30, 50, 90, 130, 170])
        self.assertEqual(errors.count("Alert not delivered"), 6)
        row, = json.loads(self.cli("webhooks")[1])
        self.assertEqual((row["consecutive_failures"], row["retry_at"]), (6, later(210)))

    def test_invalid_max_backoff_exits_two(self):
        self.cli("add", "b", B, "--alert-webhook", TEAM)
        for arguments, message in ((["--interval", "30", "--max-backoff", "10"], "at least the interval"),
                                   (["--max-backoff", "90000"], "at most 86400"),
                                   (["--max-backoff", "nan"], "number of seconds")):
            with self.subTest(arguments=arguments):
                code, _, errors = self.cli("watch", "--count", "1", *arguments, states=["up"])
                self.assertEqual(code, 2)
                self.assertIn(message, errors)

    def test_max_backoff_with_nowhere_to_send_exits_two(self):
        self.cli("add", "a", A)
        code, _, errors = self.cli("watch", "--count", "1", "--max-backoff", "60", states=["up"])
        self.assertEqual(code, 2)
        self.assertIn("--max-backoff only applies to alerts", errors)


class BackoffStorageTests(unittest.TestCase):
    def test_webhook_health_from_before_backoff_gains_an_empty_retry_time(self):
        with tempfile.TemporaryDirectory() as directory:
            database = Path(directory) / "checks.sqlite3"
            with sqlite3.connect(database) as legacy:
                legacy.execute("CREATE TABLE webhooks (route TEXT PRIMARY KEY, last_success_at TEXT, "
                               "last_failure_at TEXT, last_error TEXT, failures INTEGER NOT NULL DEFAULT 0)")
                legacy.execute("INSERT INTO webhooks (route, last_failure_at, last_error, failures) "
                               "VALUES (?, '2026-10-24T00:00:00+00:00', 'http_500', 3)", (TEAM,))
            legacy.close()
            connection = monitor.connect(database)
            self.addCleanup(connection.close)
            row, = alerts.webhook_health(connection)
            self.assertEqual((row["consecutive_failures"], row["retry_at"]), (3, None))
            self.assertEqual(webhooks.backing_off(connection), set())


if __name__ == "__main__":
    unittest.main()
