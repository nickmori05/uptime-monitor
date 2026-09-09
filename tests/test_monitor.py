from contextlib import redirect_stderr, redirect_stdout
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import io
import json
from pathlib import Path
import tempfile
import threading
import time
import unittest
from unittest.mock import patch
from urllib.error import URLError

import monitor


class Endpoint(BaseHTTPRequestHandler):
    def do_GET(self):
        if self.path == "/slow":
            time.sleep(0.3)
        status = {"/ok": 200, "/fail": 503, "/redirect": 302, "/slow": 200}.get(self.path, 404)
        self.send_response(status)
        if self.path == "/redirect":
            self.send_header("Location", "/ok")
        self.send_header("Content-Length", "0")
        try:
            self.end_headers()
        except (BrokenPipeError, ConnectionResetError):
            pass

    def log_message(self, *_args):
        pass


class MonitorTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.server = ThreadingHTTPServer(("127.0.0.1", 0), Endpoint)
        cls.thread = threading.Thread(target=cls.server.serve_forever, daemon=True)
        cls.thread.start()
        cls.base_url = f"http://127.0.0.1:{cls.server.server_port}"

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()
        cls.server.server_close()
        cls.thread.join(timeout=2)

    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.database = Path(self.directory.name) / "checks.sqlite3"

    def test_successful_request_records_status_and_time(self):
        result = monitor.probe(self.base_url + "/ok")
        self.assertEqual(result.state, "up")
        self.assertEqual(result.status_code, 200)
        self.assertIsNone(result.error)
        self.assertGreaterEqual(result.latency_ms, 0)
        self.assertTrue(result.checked_at.endswith("+00:00"))

    def test_http_failure_is_a_check_result(self):
        result = monitor.probe(self.base_url + "/fail")
        self.assertEqual(result.state, "down")
        self.assertEqual(result.status_code, 503)
        self.assertEqual(result.error, "http_503")

    def test_redirect_is_not_silently_followed(self):
        result = monitor.probe(self.base_url + "/redirect")
        self.assertEqual(result.status_code, 302)
        self.assertEqual(result.state, "down")

    def test_slow_endpoint_records_a_timeout(self):
        result = monitor.probe(self.base_url + "/slow", timeout=0.03)
        self.assertEqual(result.state, "down")
        self.assertIsNone(result.status_code)
        self.assertEqual(result.error, "timeout")

    def test_connection_failure_is_a_check_result(self):
        with patch("monitor.build_opener") as factory:
            factory.return_value.open.side_effect = URLError(ConnectionRefusedError())
            result = monitor.probe("https://example.com")
        self.assertEqual(result.state, "down")
        self.assertEqual(result.error, "connection_error")
        self.assertIsNone(result.status_code)

    def test_invalid_inputs_never_make_a_request(self):
        with patch("monitor.build_opener") as factory:
            for url in (
                "file:///etc/hosts",
                "https://",
                "https://user:password@example.com",
                "https://example.com:0",
                "https://example.com:99999",
                "https://example.com\n",
                "https://example.com#fragment",
            ):
                with self.subTest(url=url), self.assertRaises(ValueError):
                    monitor.probe(url)
            for timeout in (0, -1, 61, float("nan"), float("inf")):
                with self.subTest(timeout=timeout), self.assertRaises(ValueError):
                    monitor.probe("https://example.com", timeout)
            factory.assert_not_called()

    def test_history_survives_reopening_in_newest_first_order(self):
        connection = monitor.connect(self.database)
        try:
            good_id = monitor.save(connection, monitor.probe(self.base_url + "/ok"))
            bad_id = monitor.save(connection, monitor.probe(self.base_url + "/fail"))
        finally:
            connection.close()
        reopened = monitor.connect(self.database)
        self.addCleanup(reopened.close)
        rows = monitor.history(reopened)
        self.assertEqual([row["id"] for row in rows], [bad_id, good_id])
        self.assertEqual([row["state"] for row in rows], ["down", "up"])
        self.assertEqual(len(monitor.history(reopened, limit=1)), 1)
        for limit in (0, -1, 1001):
            with self.subTest(limit=limit), self.assertRaises(ValueError):
                monitor.history(reopened, limit)

    def test_cli_records_failed_checks_and_returns_meaningful_exit_codes(self):
        args = ["--database", str(self.database)]
        with redirect_stdout(io.StringIO()):
            self.assertEqual(monitor.main(args + ["check", self.base_url + "/ok"]), 0)
            self.assertEqual(monitor.main(args + ["check", self.base_url + "/fail"]), 1)
        output = io.StringIO()
        with redirect_stdout(output):
            self.assertEqual(monitor.main(args + ["history"]), 0)
        rows = json.loads(output.getvalue())
        self.assertEqual(len(rows), 2)
        self.assertEqual(rows[0]["status_code"], 503)
        with redirect_stderr(io.StringIO()):
            self.assertEqual(monitor.main(args + ["check", "invalid"]), 2)


if __name__ == "__main__":
    unittest.main()
