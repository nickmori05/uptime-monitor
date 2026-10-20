from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import socket
import threading
import time
import unittest

import alerts


class Receiver(BaseHTTPRequestHandler):
    def do_POST(self):
        body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
        self.server.received.append((self.path, self.headers["Content-Type"], body))
        if self.path == "/slow":
            time.sleep(1)
        status = {"/ok": 204, "/fail": 500, "/redirect": 302}.get(self.path, 200)
        self.send_response(status)
        if status == 302:
            self.send_header("Location", "/ok")
        self.send_header("Content-Length", "0")
        self.end_headers()

    def log_message(self, *args):
        pass


class WebhookTests(unittest.TestCase):
    def setUp(self):
        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Receiver)
        self.server.daemon_threads = True
        self.server.received = []
        thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        thread.start()
        self.addCleanup(self.server.server_close)
        self.addCleanup(thread.join)
        self.addCleanup(self.server.shutdown)
        self.base = f"http://127.0.0.1:{self.server.server_port}"

    def test_posts_json_and_accepts_any_success_status(self):
        body = {"event": "opened", "url": "https://example.com", "text": "Incident opened"}
        self.assertIsNone(alerts.send_webhook(self.base + "/ok", body))
        self.assertIsNone(alerts.send_webhook(self.base + "/accepted", body))
        self.assertEqual(self.server.received[0], ("/ok", "application/json", body))

    def test_error_and_redirect_responses_are_failures(self):
        self.assertEqual(alerts.send_webhook(self.base + "/fail", {}), "http_500")
        self.assertEqual(alerts.send_webhook(self.base + "/redirect", {}), "http_302")
        self.assertEqual([path for path, _, _ in self.server.received], ["/fail", "/redirect"])

    def test_unreachable_and_slow_receivers_are_failures(self):
        with socket.socket() as unused:
            unused.bind(("127.0.0.1", 0))
            closed_port = unused.getsockname()[1]
        self.assertEqual(alerts.send_webhook(f"http://127.0.0.1:{closed_port}/", {}), "connection_error")
        self.assertEqual(alerts.send_webhook(self.base + "/slow", {}, timeout=0.2), "timeout")
        started = time.monotonic()
        self.assertEqual(alerts.send_webhook(self.base + "/slow", {}, timeout=5, deadline=0.2), "deadline_exceeded")
        self.assertLess(time.monotonic() - started, 0.9)

    def test_unexpected_errors_are_reported_without_waiting_for_the_deadline(self):
        started = time.monotonic()
        self.assertEqual(alerts.send_webhook(self.base + "/ok", {"value": object()}, deadline=5), "connection_error")
        self.assertLess(time.monotonic() - started, 1)


if __name__ == "__main__":
    unittest.main()
