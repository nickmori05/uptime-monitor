from contextlib import redirect_stderr, redirect_stdout
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import io
import json
from pathlib import Path
import shutil
import sqlite3
import tempfile
import threading
import unittest
from unittest.mock import patch

import alerts
import monitor

WEBHOOK = "https://hooks.example/alert"


def check(state, url="https://example.com", checked_at="2026-10-21T12:00:00+00:00"):
    return monitor.Check(url, checked_at, state, 200 if state == "up" else 503,
                         1.0, None if state == "up" else "http_503")


class Receiver(BaseHTTPRequestHandler):
    def do_POST(self):
        body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
        self.server.received.append((self.headers.get("Idempotency-Key"), body))
        self.send_response(204)
        self.send_header("Content-Length", "0")
        self.end_headers()

    def log_message(self, *args):
        pass


class KeyTests(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.database = Path(directory.name) / "checks.sqlite3"
        self.connection = monitor.connect(self.database)
        self.addCleanup(self.connection.close)
        self.sent = []

    def record(self, states, url="https://example.com", connection=None, checked_at="2026-10-21T12:00:00+00:00"):
        return [monitor.save(connection or self.connection, check(state, url, checked_at)) for state in states]

    def deliver(self, connection=None):
        def accept(webhook, body):
            self.sent.append(body)

        with patch.object(alerts, "send_webhook", side_effect=accept), redirect_stderr(io.StringIO()):
            return alerts.deliver(connection or self.connection, WEBHOOK)

    def test_key_format_is_fixed_so_receivers_keep_deduplicating_across_upgrades(self):
        key = alerts.idempotency_key("https://example.com", 1, "2026-10-21T12:00:00+00:00", "opened")
        self.assertEqual(key, "5e57d7134e32960969a70e0f02c65c08")

    def test_each_event_gets_its_own_key(self):
        self.record(["down", "down", "up", "up", "down", "down"])
        self.record(["down", "down"], "https://b.example")
        bodies = self.deliver()
        self.assertEqual(len(bodies), 4)
        self.assertEqual(len({body["idempotency_key"] for body in bodies}), 4)
        for body in bodies:
            self.assertRegex(body["idempotency_key"], r"^[0-9a-f]{32}$")

    def test_a_resend_after_a_crash_carries_the_same_key(self):
        self.record(["down", "down"])
        # The receiver accepts the alert, then the watcher dies before saving that it did.
        with patch.object(alerts, "record", side_effect=sqlite3.OperationalError("killed")), \
                self.assertRaises(sqlite3.OperationalError):
            self.deliver()
        self.record(["down", "down"])
        first, = self.sent
        resent, = self.deliver()
        self.assertEqual(resent["idempotency_key"], first["idempotency_key"])
        self.assertNotEqual(resent["incident"]["last_check_id"], first["incident"]["last_check_id"])
        self.assertEqual(self.deliver(), [])

    def test_databases_with_the_same_check_ids_produce_different_keys(self):
        other = monitor.connect(self.database.with_name("other.sqlite3"))
        self.addCleanup(other.close)
        self.record(["down", "down"])
        self.record(["down", "down"], connection=other, checked_at="2026-10-21T12:00:05+00:00")
        mine, = self.deliver()
        theirs, = self.deliver(other)
        self.assertEqual(mine["incident"]["first_failure_id"], theirs["incident"]["first_failure_id"])
        self.assertNotEqual(mine["idempotency_key"], theirs["idempotency_key"])

    def test_a_copied_database_keeps_the_same_keys(self):
        self.record(["down", "down"])
        copy = self.database.with_name("copy.sqlite3")
        shutil.copy(self.database, copy)
        restored = monitor.connect(copy)
        self.addCleanup(restored.close)
        self.assertEqual(self.deliver()[0]["idempotency_key"], self.deliver(restored)[0]["idempotency_key"])

    def test_listed_alerts_show_the_key_that_was_sent(self):
        self.record(["down", "down", "up", "up"])
        alerts.skip_backlog(self.connection)
        self.record(["down", "down"])
        pending, = alerts.list_alerts(self.connection, pending=True)
        sent, = self.deliver()
        self.assertEqual(pending["idempotency_key"], sent["idempotency_key"])
        listed = alerts.list_alerts(self.connection)
        self.assertEqual([(row["status"], row["idempotency_key"]) for row in listed][0],
                         ("sent", sent["idempotency_key"]))
        self.assertEqual(len({row["idempotency_key"] for row in listed}), 3)

    def test_an_alert_whose_first_check_is_gone_lists_no_key(self):
        ids = self.record(["down", "down"])
        self.deliver()
        with self.connection:
            self.connection.execute("DELETE FROM checks WHERE id = ?", (ids[0],))
        row, = alerts.list_alerts(self.connection)
        self.assertEqual(row["status"], "sent")
        self.assertIsNone(row["idempotency_key"])

    def test_alerts_recorded_by_earlier_versions_list_their_key(self):
        legacy_path = self.database.with_name("legacy.sqlite3")
        with sqlite3.connect(legacy_path) as legacy:
            legacy.execute("CREATE TABLE checks (id INTEGER PRIMARY KEY, url TEXT NOT NULL, checked_at TEXT NOT NULL, "
                           "state TEXT NOT NULL, status_code INTEGER, latency_ms REAL NOT NULL, error TEXT)")
            legacy.execute("CREATE TABLE alerts (id INTEGER PRIMARY KEY, url TEXT NOT NULL, first_failure_id INTEGER "
                           "NOT NULL, event TEXT NOT NULL, check_id INTEGER NOT NULL, status TEXT NOT NULL, "
                           "recorded_at TEXT NOT NULL, UNIQUE(first_failure_id, event))")
            legacy.executemany("INSERT INTO checks (url, checked_at, state, latency_ms) VALUES (?, ?, 'down', 1)",
                               [("https://example.com", "2026-10-21T12:00:00+00:00")] * 2)
            legacy.execute("INSERT INTO alerts VALUES (1, 'https://example.com', 1, 'opened', 2, 'sent', '2026-10-21')")
        legacy.close()
        upgraded = monitor.connect(legacy_path)
        self.addCleanup(upgraded.close)
        row, = alerts.list_alerts(upgraded)
        self.assertEqual(row["idempotency_key"], "5e57d7134e32960969a70e0f02c65c08")

    def test_alerts_command_prints_keys(self):
        self.record(["down", "down"])
        sent, = self.deliver()
        output = io.StringIO()
        with redirect_stdout(output):
            self.assertEqual(monitor.main(["--database", str(self.database), "alerts"]), 0)
        self.assertEqual(json.loads(output.getvalue())[0]["idempotency_key"], sent["idempotency_key"])


class KeyHeaderTests(unittest.TestCase):
    def setUp(self):
        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Receiver)
        self.server.daemon_threads = True
        self.server.received = []
        thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        thread.start()
        self.addCleanup(self.server.server_close)
        self.addCleanup(thread.join)
        self.addCleanup(self.server.shutdown)
        self.webhook = f"http://127.0.0.1:{self.server.server_port}/alert"

    def test_the_key_is_sent_as_a_header_matching_the_body(self):
        incident = {"url": "https://example.com", "first_failure_id": 1, "opened_at": "2026-10-21T12:00:00+00:00"}
        body = alerts.payload({"event": "opened", "check_id": 2, "incident": incident})
        self.assertIsNone(alerts.send_webhook(self.webhook, body))
        (header, received), = self.server.received
        self.assertEqual(header, "5e57d7134e32960969a70e0f02c65c08")
        self.assertEqual(received["idempotency_key"], header)

    def test_bodies_without_a_key_send_no_header(self):
        self.assertIsNone(alerts.send_webhook(self.webhook, {"event": "opened"}))
        self.assertEqual(self.server.received, [(None, {"event": "opened"})])


if __name__ == "__main__":
    unittest.main()
