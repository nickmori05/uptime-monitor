from contextlib import redirect_stderr, redirect_stdout
import io
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

import alerts
import monitor

WEBHOOK = "https://hooks.example/alert"


def check(state, url="https://example.com"):
    return monitor.Check(url, "2026-10-22T12:00:00+00:00", state, 200 if state == "up" else 503,
                         1.0, None if state == "up" else "http_503")


class SkipResolvedTests(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.connection = monitor.connect(Path(directory.name) / "checks.sqlite3")
        self.addCleanup(self.connection.close)
        self.sent = []

    def record(self, states, url="https://example.com"):
        return [monitor.save(self.connection, check(state, url)) for state in states]

    def call(self, function, *arguments, results=(), **options):
        outcomes = iter(results)

        def send(webhook, body):
            outcome = next(outcomes, None)
            if outcome is None:
                self.sent.append(body)
            return outcome

        errors = io.StringIO()
        with patch.object(alerts, "send_webhook", side_effect=send), redirect_stderr(errors):
            delivered = function(self.connection, *arguments, **options)
        return delivered, errors.getvalue()

    def start(self, skip_resolved=True, results=()):
        return self.call(alerts.start, WEBHOOK, skip_resolved=skip_resolved, results=results)

    def deliver(self, skip_resolved=True, results=()):
        return self.call(alerts.deliver, WEBHOOK, skip_resolved=skip_resolved, results=results)

    def logged(self):
        return [(row["event"], row["status"]) for row in alerts.list_alerts(self.connection)][::-1]

    def test_catch_up_skips_an_incident_that_opened_and_resolved_while_no_watcher_ran(self):
        self.start()
        self.record(["down", "down", "up", "up"])
        delivered, errors = self.start()
        self.assertEqual((delivered, self.sent), ([], []))
        self.assertIn("Skipped 1 incident(s) that resolved before their opening was sent.", errors)
        self.assertNotIn("Sending", errors)
        self.assertEqual(self.logged(), [("opened", "skipped"), ("resolved", "skipped")])
        # Skipped alerts still list the key they would have carried.
        self.assertTrue(all(row["idempotency_key"] for row in alerts.list_alerts(self.connection)))
        self.assertEqual(self.start(skip_resolved=False), ([], ""))

    def test_without_the_option_catch_up_still_sends_both_events(self):
        self.start()
        self.record(["down", "down", "up", "up"])
        delivered, _ = self.start(skip_resolved=False)
        self.assertEqual([body["event"] for body in delivered], ["opened", "resolved"])

    def test_an_incident_still_open_is_sent_and_later_resolves_normally(self):
        self.start()
        self.record(["down", "down", "up", "up"], "https://a.example")
        self.record(["down", "down"], "https://b.example")
        delivered, errors = self.start()
        self.assertEqual([(body["event"], body["url"]) for body in delivered], [("opened", "https://b.example")])
        self.assertIn("Sending 1 incident event(s) not alerted yet.", errors)
        self.record(["up", "up"], "https://b.example")
        self.assertEqual([body["event"] for body in self.deliver()[0]], ["resolved"])

    def test_a_resolution_is_still_sent_when_its_opening_was(self):
        self.start()
        self.record(["down", "down"])
        self.assertEqual([body["event"] for body in self.deliver()[0]], ["opened"])
        # The watcher stops, and the incident resolves before the next one starts.
        self.record(["up", "up"])
        delivered, errors = self.start()
        self.assertEqual([body["event"] for body in delivered], ["resolved"])
        self.assertNotIn("Skipped", errors)
        self.assertEqual(self.logged(), [("opened", "sent"), ("resolved", "sent")])

    def test_an_incident_that_resolves_while_the_webhook_is_failing_is_skipped(self):
        self.start()
        self.record(["down", "down"])
        self.assertEqual(self.deliver(results=["http_503"])[0], [])
        self.record(["up", "up", "down", "down"])
        delivered, errors = self.deliver()
        self.assertEqual([body["event"] for body in delivered], ["opened"])
        self.assertEqual(delivered[0]["incident"]["first_failure_id"], 5)
        self.assertIn("Skipped 1 incident(s)", errors)
        self.assertEqual(self.logged(), [("opened", "skipped"), ("resolved", "skipped"), ("opened", "sent")])

    def test_a_resolution_whose_opening_was_skipped_at_first_start_is_skipped_too(self):
        self.record(["down", "down"])
        self.start()
        self.record(["up", "up"])
        self.assertEqual(self.start(), ([], "Skipped 1 incident(s) that resolved before their opening was sent.\n"))
        self.assertEqual(self.logged(), [("opened", "skipped"), ("resolved", "skipped")])

    def test_skipping_happens_even_when_the_next_delivery_fails(self):
        self.start()
        self.record(["down", "down", "up", "up"], "https://a.example")
        self.record(["down", "down"], "https://b.example")
        delivered, errors = self.start(results=["connection_error"])
        self.assertEqual(delivered, [])
        self.assertIn("Alert not delivered (connection_error)", errors)
        self.assertEqual([body["url"] for body in alerts.list_alerts(self.connection, pending=True)],
                         ["https://b.example"])

    def test_pending_preview_matches_what_the_watcher_then_sends(self):
        self.start()
        self.record(["down", "down", "up", "up"], "https://a.example")
        self.record(["down", "down"], "https://b.example")
        self.assertEqual([body["url"] for body in alerts.list_alerts(self.connection, pending=True)],
                         ["https://a.example", "https://a.example", "https://b.example"])
        preview = alerts.list_alerts(self.connection, pending=True, skip_resolved=True)
        self.assertEqual(alerts.list_alerts(self.connection), [])
        delivered, _ = self.start()
        self.assertEqual(delivered, preview)
        with self.assertRaises(ValueError):
            alerts.list_alerts(self.connection, skip_resolved=True)


class SkipResolvedCommandTests(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.database = Path(directory.name) / "checks.sqlite3"
        connection = monitor.connect(self.database)
        monitor.add_target(connection, "api", "https://example.com")
        connection.close()
        self.events = []

    def cli(self, *arguments, states=()):
        results = iter(states)

        def probe(*_args):
            self.events.append("check")
            return check(next(results))

        def send(webhook, body):
            self.events.append(body["event"])

        output, errors = io.StringIO(), io.StringIO()
        with patch.object(monitor, "probe", side_effect=probe), patch.object(monitor, "sleep"), \
                patch.object(alerts, "send_webhook", side_effect=send), redirect_stdout(output), \
                redirect_stderr(errors):
            code = monitor.main(["--database", str(self.database), *arguments])
        return code, output.getvalue(), errors.getvalue()

    def test_watch_skips_resolved_catch_up_and_alerts_on_new_incidents(self):
        self.cli("watch", "--count", "1", "--alert-webhook", WEBHOOK, states=["up"])
        for state in ("down", "down", "up", "up"):
            self.cli("run", "api", states=[state])
        code, output, _ = self.cli("alerts", "--pending", "--skip-resolved")
        self.assertEqual((code, json.loads(output)), (0, []))
        self.assertEqual(len(json.loads(self.cli("alerts", "--pending")[1])), 2)
        self.events.clear()
        code, _, errors = self.cli("watch", "--count", "3", "--alert-webhook", WEBHOOK, "--skip-resolved",
                                   states=["down", "down", "up"])
        self.assertEqual(code, 1)
        self.assertEqual(self.events, ["check", "check", "opened", "check"])
        self.assertIn("Skipped 1 incident(s) that resolved before their opening was sent.", errors)
        self.assertEqual([(row["event"], row["status"]) for row in json.loads(self.cli("alerts")[1])],
                         [("opened", "sent"), ("resolved", "skipped"), ("opened", "skipped")])

    def test_the_option_without_alerts_or_pending_exits_two_without_checking(self):
        code, _, errors = self.cli("watch", "--count", "1", "--skip-resolved", states=["down"])
        self.assertEqual(code, 2)
        self.assertIn("needs --alert-webhook", errors)
        code, _, errors = self.cli("alerts", "--skip-resolved")
        self.assertEqual(code, 2)
        self.assertIn("needs --pending", errors)
        self.assertEqual(self.events, [])


if __name__ == "__main__":
    unittest.main()
