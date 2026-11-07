import base64
from contextlib import redirect_stderr
import hashlib
import io
from pathlib import Path
import re
import tempfile
from threading import Thread
import unittest
from unittest.mock import patch
from urllib.error import HTTPError
from urllib.request import Request, urlopen

import api
import monitor


def fetch(url, method="GET"):
    try:
        with urlopen(Request(url, method=method), timeout=5) as response:
            return response.status, response.read(), response.headers
    except HTTPError as response:
        with response:
            return response.code, response.read(), response.headers


class DashboardTests(unittest.TestCase):
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
        self.page = api.DASHBOARD.read_text(encoding="utf-8")

    def test_root_serves_the_dashboard_page(self):
        status, body, headers = fetch(self.base_url + "/")
        self.assertEqual(status, 200)
        self.assertEqual(body.decode(), self.page)
        self.assertEqual(headers["Content-Type"], "text/html; charset=utf-8")
        self.assertEqual(headers["Cache-Control"], "no-store")
        self.assertEqual(headers["X-Content-Type-Options"], "nosniff")
        self.assertIn("<title>Uptime Monitor</title>", self.page)

    def test_policy_allows_only_the_pages_own_script_and_style(self):
        policy = fetch(self.base_url + "/")[2]["Content-Security-Policy"]
        for tag in ("script", "style"):
            with self.subTest(tag=tag):
                body, = re.findall(rf"<{tag}>(.*?)</{tag}>", self.page, re.DOTALL)
                digest = base64.b64encode(hashlib.sha256(body.encode()).digest()).decode()
                self.assertIn(f"{tag}-src 'sha256-{digest}';", policy)
        self.assertIn("default-src 'none'", policy)
        self.assertIn("connect-src 'self'", policy)
        self.assertNotIn("unsafe", policy)

    def test_page_only_reads_endpoints_the_server_answers(self):
        monitor.add_target(self.connection, "home", "https://example.com")
        monitor.save(self.connection, monitor.Check(
            "https://example.com", "2026-10-15T12:00:00+00:00", "down", None, 3000.0, "timeout"))
        paths = re.findall(r'"(/api/[^"]*)"', self.page)
        self.assertEqual(len(paths), 4, paths)
        for path in paths:
            with self.subTest(path=path):
                self.assertEqual(fetch(self.base_url + path)[0], 200)

    def test_page_inserts_saved_values_as_text_only(self):
        # Saved URLs and errors are untrusted, so the page must never parse them as HTML.
        for unsafe in ("innerHTML", "outerHTML", "insertAdjacentHTML", "document.write", "eval(", "http://", "https://"):
            with self.subTest(unsafe=unsafe):
                self.assertNotIn(unsafe, self.page)
        self.assertIn("textContent", self.page)

    def test_root_rejects_queries_other_paths_and_write_methods(self):
        status, body, _ = fetch(self.base_url + "/?refresh=1")
        self.assertEqual((status, body), (400, b'{\n  "error": "Unknown query parameter: refresh."\n}'))
        self.assertEqual(fetch(self.base_url + "/index.html")[0], 404)
        self.assertEqual(fetch(self.base_url + "/dashboard.html")[0], 404)
        status, _, headers = fetch(self.base_url + "/", "POST")
        self.assertEqual((status, headers["Allow"]), (405, "GET"))

    def test_page_is_served_without_reading_the_database(self):
        self.connection.close()
        self.database.unlink()
        self.assertEqual(fetch(self.base_url + "/")[0], 200)
        self.assertFalse(self.database.exists())


class MissingDashboardTests(unittest.TestCase):
    def test_serve_exits_2_when_the_page_is_missing(self):
        with tempfile.TemporaryDirectory() as directory, \
                patch("api.DASHBOARD", Path(directory) / "missing.html"), \
                redirect_stderr(io.StringIO()) as errors:
            code = monitor.main(["--database", str(Path(directory) / "checks.sqlite3"), "serve", "--port", "0"])
        self.assertEqual(code, 2)
        self.assertIn("missing.html", errors.getvalue())
        self.assertNotIn("Serving", errors.getvalue())


if __name__ == "__main__":
    unittest.main()
