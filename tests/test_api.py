from contextlib import redirect_stderr
import io
import json
from pathlib import Path
import re
import selectors
import signal
import socket
import sqlite3
import subprocess
import sys
import tempfile
from threading import Thread
import unittest
from unittest.mock import patch
from urllib.error import HTTPError
from urllib.request import Request, urlopen

import api
import monitor


class ApiTests(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.database = Path(directory.name) / "checks.sqlite3"
        self.connection = monitor.connect(self.database)
        self.addCleanup(self.connection.close)
        self.server = api.make_server(self.database, "127.0.0.1", 0, monitor)
        thread = Thread(target=self.server.serve_forever, daemon=True)
        thread.start()
        self.addCleanup(self.server.server_close)
        self.addCleanup(thread.join)
        self.addCleanup(self.server.shutdown)
        self.base_url = f"http://127.0.0.1:{self.server.server_port}"

    def save(self, state="up", url="https://example.com"):
        return monitor.save(self.connection, monitor.Check(
            url, "2026-10-13T12:00:00+00:00", state, 200 if state == "up" else 503, 12.5,
            None if state == "up" else "http_503"))

    def get(self, path, method="GET"):
        try:
            with urlopen(Request(self.base_url + path, method=method), timeout=5) as response:
                return response.status, json.loads(response.read()), response.headers
        except HTTPError as response:
            with response:
                return response.code, json.loads(response.read()), response.headers

    def test_endpoints_return_the_same_data_as_the_commands(self):
        monitor.add_target(self.connection, "home", "https://example.com", timeout=3, deadline=8)
        for state in ("down", "down", "up", "up"):
            self.save(state)
        self.save("down", url="https://other.example")
        status, targets, headers = self.get("/api/targets")
        self.assertEqual((status, targets), (200, monitor.list_targets(self.connection)))
        self.assertEqual(headers["Content-Type"], "application/json")
        self.assertEqual(headers["Cache-Control"], "no-store")
        self.assertEqual(self.get("/api/history?limit=2")[1], monitor.history(self.connection, 2))
        self.assertEqual(self.get("/api/summary")[1], monitor.summarize(self.connection))
        self.assertEqual(self.get("/api/incidents")[1], monitor.list_incidents(self.connection))

    def test_filters_match_the_command_line_options(self):
        monitor.add_target(self.connection, "home", "https://example.com/search?q=a+b&page=2")
        self.save("down", url="https://example.com/search?q=a+b&page=2")
        self.save("up", url="https://other.example")
        _, rows, _ = self.get("/api/history?url=https%3A%2F%2Fexample.com%2Fsearch%3Fq%3Da%2Bb%26page%3D2")
        self.assertEqual([row["url"] for row in rows], ["https://example.com/search?q=a+b&page=2"])
        _, rows, _ = self.get("/api/summary?target=home&last=1")
        self.assertEqual([row["url"] for row in rows], ["https://example.com/search?q=a+b&page=2"])
        _, rows, _ = self.get("/api/summary?state=up")
        self.assertEqual([row["url"] for row in rows], ["https://other.example"])
        encoded = "https%3A%2F%2Fexample.com%2Fsearch%3Fq%3Da%2Bb%26page%3D2"
        _, rows, _ = self.get(f"/api/incidents?failures=1&state=open&url={encoded}")
        self.assertEqual([(row["url"], row["state"]) for row in rows],
                         [("https://example.com/search?q=a+b&page=2", "open")])

    def test_empty_database_returns_empty_lists(self):
        for path in ("/api/targets", "/api/history", "/api/summary", "/api/incidents"):
            with self.subTest(path=path):
                self.assertEqual(self.get(path)[:2], (200, []))

    def test_invalid_queries_return_400_with_a_message(self):
        cases = {
            "/api/history?limit=0": "History limit",
            "/api/history?limit=ten": "limit must be a whole number",
            "/api/history?limit=99999999999999999999": "limit must be a whole number",
            "/api/history?url=file:///etc/hosts": "http",
            "/api/history?limit=1&limit=2": "more than once",
            "/api/targets?verbose=1": "Unknown query parameter: verbose",
            "/api/summary?last=0": "Summary sample size",
            "/api/summary?state=sideways": "Summary state",
            "/api/summary?url=https://example.com&target=home": "either url or target",
            "/api/summary?target=missing": "Target 'missing' does not exist",
            "/api/incidents?failures=101": "Failures",
            "/api/incidents?url=": "http",
        }
        for path, message in cases.items():
            with self.subTest(path=path):
                status, body, _ = self.get(path)
                self.assertEqual(status, 400)
                self.assertIn(message, body["error"])

    def test_unknown_paths_and_write_methods_are_rejected(self):
        status, body, _ = self.get("/api/checks")
        self.assertEqual((status, body), (404, {"error": "No endpoint at /api/checks."}))
        for method in ("POST", "PUT", "PATCH", "DELETE"):
            with self.subTest(method=method):
                status, body, headers = self.get("/api/targets", method)
                self.assertEqual(status, 405)
                self.assertEqual(headers["Allow"], "GET")

    def test_requests_never_write_or_probe(self):
        self.save("down")
        before = self.database.read_bytes()
        with patch("monitor.probe") as probe, patch("monitor.save") as save:
            for path in ("/api/targets", "/api/history", "/api/summary", "/api/incidents"):
                self.assertEqual(self.get(path)[0], 200)
        probe.assert_not_called()
        save.assert_not_called()
        self.assertEqual(self.database.read_bytes(), before)
        connection = api.read_only(self.database)
        self.addCleanup(connection.close)
        with self.assertRaisesRegex(sqlite3.OperationalError, "readonly"):
            connection.execute("DELETE FROM checks")

    def test_storage_errors_return_500_without_details(self):
        self.connection.close()
        self.database.unlink()
        with redirect_stderr(io.StringIO()) as errors:
            status, body, _ = self.get("/api/history")
        self.assertEqual((status, body), (500, {"error": "The database could not be read."}))
        self.assertIn("Storage error", errors.getvalue())
        self.assertFalse(self.database.exists())


class ServeCommandTests(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.database = Path(directory.name) / "checks.sqlite3"

    def test_invalid_port_and_busy_port_exit_2(self):
        for port in ("-1", "65536"):
            with self.subTest(port=port), redirect_stderr(io.StringIO()) as errors:
                self.assertEqual(monitor.main(["--database", str(self.database), "serve", "--port", port]), 2)
            self.assertIn("Port must be between", errors.getvalue())
        with socket.socket() as taken:
            taken.bind(("127.0.0.1", 0))
            taken.listen()
            port = str(taken.getsockname()[1])
            with redirect_stderr(io.StringIO()) as errors:
                self.assertEqual(monitor.main(["--database", str(self.database), "serve", "--port", port]), 2)
        self.assertIn("Error:", errors.getvalue())

    def test_serve_upgrades_an_older_database_and_stops_on_signals(self):
        with sqlite3.connect(self.database) as connection:
            connection.execute("CREATE TABLE targets (name TEXT PRIMARY KEY, url TEXT NOT NULL, timeout REAL NOT NULL)")
            connection.execute("INSERT INTO targets VALUES ('home', 'https://example.com', 3)")
        connection.close()
        for stop_signal, code in [(signal.SIGINT, 130), (signal.SIGTERM, 143)]:
            with self.subTest(signal=stop_signal):
                process = subprocess.Popen(
                    [sys.executable, str(Path(monitor.__file__).resolve()), "--database", str(self.database),
                     "serve", "--port", "0"], stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                )
                try:
                    with selectors.DefaultSelector() as selector:
                        selector.register(process.stderr, selectors.EVENT_READ)
                        self.assertTrue(selector.select(timeout=10), "serve did not report its address")
                    address = re.search(r"http://\S+", process.stderr.readline().decode()).group()
                    with urlopen(address + "/api/targets", timeout=5) as response:
                        targets = json.loads(response.read())
                    self.assertEqual(targets, [{"name": "home", "url": "https://example.com",
                                                "timeout": 3, "deadline": None}])
                    with urlopen(address + "/", timeout=5) as response:
                        self.assertIn(b"<title>Uptime Monitor</title>", response.read())
                    process.send_signal(stop_signal)
                    stdout, stderr = process.communicate(timeout=5)
                    self.assertEqual(process.returncode, code, stderr.decode())
                    self.assertEqual(stdout, b"")
                finally:
                    if process.poll() is None:
                        process.kill()
                    process.communicate()


if __name__ == "__main__":
    unittest.main()
