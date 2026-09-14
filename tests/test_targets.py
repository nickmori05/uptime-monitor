from contextlib import redirect_stderr, redirect_stdout
import io
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

import monitor


class TargetTests(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.database = Path(directory.name) / "checks.sqlite3"
        self.connection = monitor.connect(self.database)
        self.addCleanup(self.connection.close)

    def test_target_settings_survive_reopening(self):
        monitor.add_target(self.connection, "api-health", "https://example.com/health", 2.5)
        reopened = monitor.connect(self.database)
        self.addCleanup(reopened.close)
        self.assertEqual(monitor.get_target(reopened, "api-health"), {
            "name": "api-health", "url": "https://example.com/health", "timeout": 2.5})

    def test_duplicate_name_does_not_replace_configuration(self):
        original = monitor.add_target(self.connection, "api", "https://example.com")
        with self.assertRaisesRegex(ValueError, "already exists"):
            monitor.add_target(self.connection, "api", "https://other.example.com", 3)
        self.assertEqual(monitor.get_target(self.connection, "api"), original)

    def test_cli_updates_selected_settings_and_preserves_history(self):
        url = "https://example.com/health"
        monitor.add_target(self.connection, "api", url, 5)
        other = monitor.add_target(self.connection, "website", "https://example.com")
        monitor.save(self.connection, monitor.Check(url, "2026-09-14T00:00:00+00:00", "up", 200, 1.0, None))
        before = monitor.history(self.connection)
        for flags, expected_url, expected_timeout in (
            (["--timeout", "2"], url, 2),
            (["--url", "https://new.example.com"], "https://new.example.com", 2),
            (["--url", url, "--timeout", "3"], url, 3),
        ):
            with self.subTest(flags=flags), patch("monitor.probe") as probe:
                output = io.StringIO()
                with redirect_stdout(output):
                    code = monitor.main(["--database", str(self.database), "update", "api", *flags])
                self.assertEqual(code, 0)
                expected = {"name": "api", "url": expected_url, "timeout": expected_timeout}
                self.assertEqual(json.loads(output.getvalue()), expected)
                reopened = monitor.connect(self.database)
                try:
                    self.assertEqual(monitor.get_target(reopened, "api"), expected)
                    self.assertEqual(monitor.get_target(reopened, "website"), other)
                    self.assertEqual(monitor.history(reopened), before)
                finally:
                    reopened.close()
                probe.assert_not_called()

    def test_invalid_updates_leave_all_settings_unchanged(self):
        original = monitor.add_target(self.connection, "api", "https://example.com", 5)
        for flags in ([], ["--timeout", "0"], ["--timeout", "nan"], ["--timeout", "inf"],
                      ["--timeout", "61"], ["--url", "file:///etc/hosts", "--timeout", "2"],
                      ["--url", "https://new.example.com", "--timeout", "-1"]):
            with self.subTest(flags=flags), patch("monitor.probe") as probe, \
                    redirect_stderr(io.StringIO()), redirect_stdout(io.StringIO()):
                self.assertEqual(monitor.main(["--database", str(self.database), "update", "api", *flags]), 2)
                self.assertEqual(monitor.get_target(self.connection, "api"), original)
                probe.assert_not_called()

    def test_update_requires_an_existing_exact_name(self):
        original = monitor.add_target(self.connection, "api", "https://example.com")
        for name in ("missing", "API", "api' OR 1=1 --"):
            with self.subTest(name=name), redirect_stderr(io.StringIO()), redirect_stdout(io.StringIO()):
                self.assertEqual(monitor.main(["--database", str(self.database), "update", name, "--timeout", "2"]), 2)
                self.assertEqual(monitor.list_targets(self.connection), [original])

    def test_run_uses_updated_configuration(self):
        monitor.add_target(self.connection, "api", "https://example.com")
        monitor.update_target(self.connection, "api", url="https://example.com/health", timeout=2)
        result = monitor.Check("https://example.com/health", "2026-09-14T00:00:00+00:00", "up", 200, 1.0, None)
        with patch("monitor.probe", return_value=result) as probe, redirect_stdout(io.StringIO()):
            self.assertEqual(monitor.main(["--database", str(self.database), "run", "api"]), 0)
        probe.assert_called_once_with("https://example.com/health", 2)
        self.assertEqual(monitor.history(self.connection)[0]["url"], result.url)

    def test_cli_remove_preserves_history_and_other_targets(self):
        url = "https://example.com/health"
        monitor.add_target(self.connection, "api", url)
        other = monitor.add_target(self.connection, "website", "https://example.com")
        monitor.save(self.connection, monitor.Check(url, "2026-09-11T00:00:00+00:00",
                                                   "up", 200, 1.0, None))
        before = monitor.history(self.connection)
        output = io.StringIO()
        with patch("monitor.probe") as probe, redirect_stdout(output):
            code = monitor.main(["--database", str(self.database), "remove", "api"])
        self.assertEqual(code, 0)
        self.assertEqual(json.loads(output.getvalue()), {"removed": "api"})
        probe.assert_not_called()
        reopened = monitor.connect(self.database)
        self.addCleanup(reopened.close)
        self.assertEqual(monitor.list_targets(reopened), [other])
        self.assertEqual(monitor.history(reopened), before)
        with self.assertRaisesRegex(ValueError, "does not exist"):
            monitor.get_target(reopened, "api")
        monitor.add_target(reopened, "api", "https://replacement.example.com")
        self.assertEqual(monitor.history(reopened), before)

    def test_cli_remove_unknown_name_leaves_targets_unchanged(self):
        original = monitor.add_target(self.connection, "api", "https://example.com")
        for name in ("missing", "API", "api' OR 1=1 --"):
            with self.subTest(name=name):
                errors = io.StringIO()
                output = io.StringIO()
                with redirect_stderr(errors), redirect_stdout(output):
                    code = monitor.main(["--database", str(self.database), "remove", name])
                self.assertEqual(code, 2)
                self.assertIn("does not exist", errors.getvalue())
                self.assertEqual(output.getvalue(), "")
                self.assertEqual(monitor.list_targets(self.connection), [original])

    def test_invalid_configuration_is_rejected_without_network_requests(self):
        with patch("monitor.build_opener") as opener:
            for name in ("", "space name", "../path", "_leading", "x" * 65):
                with self.subTest(name=name), self.assertRaises(ValueError):
                    monitor.add_target(self.connection, name, "https://example.com")
            with self.assertRaises(ValueError):
                monitor.add_target(self.connection, "api", "file:///etc/hosts")
            for timeout in (0, -1, 61, float("nan"), float("inf")):
                with self.subTest(timeout=timeout), self.assertRaises(ValueError):
                    monitor.add_target(self.connection, "api", "https://example.com", timeout)
            opener.assert_not_called()
        self.assertEqual(monitor.list_targets(self.connection), [])

    def test_saved_target_uses_configured_timeout_and_records_failure(self):
        monitor.add_target(self.connection, "api", "https://example.com/health", 2.5)
        result = monitor.Check("https://example.com/health", "2026-09-09T00:00:00+00:00",
                               "down", 503, 12.5, "http_503")
        with patch("monitor.probe", return_value=result) as probe, redirect_stdout(io.StringIO()):
            code = monitor.main(["--database", str(self.database), "run", "api"])
        self.assertEqual(code, 1)
        probe.assert_called_once_with("https://example.com/health", 2.5)
        self.assertEqual(monitor.history(self.connection)[0]["status_code"], 503)

    def test_unknown_target_does_not_make_a_request(self):
        errors = io.StringIO()
        with patch("monitor.probe") as probe, redirect_stderr(errors):
            code = monitor.main(["--database", str(self.database), "run", "missing"])
        self.assertEqual(code, 2)
        self.assertIn("does not exist", errors.getvalue())
        probe.assert_not_called()

    def test_filtered_history_preserves_order_and_applies_limit_after_filtering(self):
        identifiers = []
        for url in ("https://example.com", "https://other.example.com", "https://example.com"):
            identifier = monitor.save(self.connection, monitor.Check(
                url, "2026-09-09T00:00:00+00:00", "up", 200, 1.0, None))
            if url == "https://example.com":
                identifiers.append(identifier)
        rows = monitor.history(self.connection, url="https://example.com")
        self.assertEqual([row["id"] for row in rows], list(reversed(identifiers)))
        self.assertEqual(monitor.history(self.connection, limit=1, url="https://example.com")[0]["id"], identifiers[-1])
        self.assertEqual(monitor.history(self.connection, url="https://missing.example.com"), [])

    def test_cli_add_and_list_are_json_and_leave_observations_empty(self):
        arguments = ["--database", str(self.database)]
        with redirect_stdout(io.StringIO()):
            self.assertEqual(monitor.main(arguments + ["add", "api", "https://example.com"]), 0)
        output = io.StringIO()
        with redirect_stdout(output):
            self.assertEqual(monitor.main(arguments + ["targets"]), 0)
        self.assertEqual(json.loads(output.getvalue())[0]["name"], "api")
        self.assertEqual(monitor.history(self.connection), [])


if __name__ == "__main__":
    unittest.main()
