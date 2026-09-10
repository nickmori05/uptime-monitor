from contextlib import redirect_stderr, redirect_stdout
import io
import json
from pathlib import Path
import tempfile
import threading
import unittest
from unittest.mock import patch

import monitor


class BatchTests(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.database = Path(directory.name) / "checks.sqlite3"
        self.connection = monitor.connect(self.database)
        self.addCleanup(self.connection.close)

    def targets(self, count=4):
        for number in range(count):
            monitor.add_target(self.connection, f"target-{number}", f"https://example.com/{number}", number + 1)

    def result(self, url, timeout):
        down = url.endswith("/1")
        return monitor.Check(url, "2026-09-10T00:00:00+00:00", "down" if down else "up",
                             503 if down else 200, 1.0, "http_503" if down else None)

    def test_batch_uses_saved_settings_and_persists_each_observation(self):
        self.targets()
        with patch("monitor.probe", side_effect=self.result) as probe:
            results = monitor.run_targets(self.connection, workers=2)
        self.assertEqual([row["target"] for row in results], [f"target-{number}" for number in range(4)])
        self.assertEqual([row["state"] for row in results], ["up", "down", "up", "up"])
        self.assertEqual({call.args for call in probe.call_args_list},
                         {(f"https://example.com/{number}", number + 1) for number in range(4)})
        reopened = monitor.connect(self.database)
        self.addCleanup(reopened.close)
        self.assertEqual(len(monitor.history(reopened)), 4)
        self.assertEqual({row["id"] for row in results}, {row["id"] for row in monitor.history(reopened)})

    def test_requests_overlap_but_do_not_exceed_worker_limit(self):
        self.targets()
        barrier = threading.Barrier(2)
        lock = threading.Lock()
        active = 0
        peak = 0

        def probe(url, timeout):
            nonlocal active, peak
            with lock:
                active += 1
                peak = max(peak, active)
            barrier.wait(timeout=5)
            with lock:
                active -= 1
            return self.result(url, timeout)

        with patch("monitor.probe", side_effect=probe):
            results = monitor.run_targets(self.connection, workers=2)
        self.assertEqual(peak, 2)
        self.assertEqual(len(results), 4)

    def test_database_writes_stay_on_the_calling_thread(self):
        self.targets()
        caller = threading.get_ident()
        original_save = monitor.save

        def save(connection, result):
            self.assertEqual(threading.get_ident(), caller)
            return original_save(connection, result)

        with patch("monitor.probe", side_effect=self.result), patch("monitor.save", side_effect=save):
            self.assertEqual(len(monitor.run_targets(self.connection)), 4)

    def test_invalid_worker_count_never_sends_a_request(self):
        self.targets(1)
        with patch("monitor.probe") as probe:
            for workers in (0, -1, 33, True, 2.5):
                with self.subTest(workers=workers), self.assertRaises(ValueError):
                    monitor.run_targets(self.connection, workers)
            probe.assert_not_called()

    def test_invalid_saved_url_is_rejected_before_any_requests(self):
        self.targets(2)
        with self.connection:
            self.connection.execute("UPDATE targets SET url = 'file:///etc/hosts' WHERE name = 'target-1'")
        with patch("monitor.probe") as probe, self.assertRaises(ValueError):
            monitor.run_targets(self.connection)
        probe.assert_not_called()
        self.assertEqual(monitor.history(self.connection), [])

    def test_empty_batch_returns_no_observations(self):
        with patch("monitor.probe") as probe:
            self.assertEqual(monitor.run_targets(self.connection), [])
        probe.assert_not_called()

    def test_cli_reports_mixed_results_as_json_with_failure_exit_code(self):
        self.targets(3)
        output = io.StringIO()
        with patch("monitor.probe", side_effect=self.result), redirect_stdout(output):
            code = monitor.main(["--database", str(self.database), "run-all", "--workers", "2"])
        self.assertEqual(code, 1)
        rows = json.loads(output.getvalue())
        self.assertEqual([row["target"] for row in rows], ["target-0", "target-1", "target-2"])
        self.assertEqual(rows[1]["error"], "http_503")
        self.assertEqual(len(monitor.history(self.connection)), 3)

    def test_cli_empty_and_healthy_batches_exit_successfully(self):
        arguments = ["--database", str(self.database), "run-all"]
        with redirect_stdout(io.StringIO()):
            self.assertEqual(monitor.main(arguments), 0)
        self.targets(1)
        with patch("monitor.probe", side_effect=self.result), redirect_stdout(io.StringIO()):
            self.assertEqual(monitor.main(arguments), 0)

    def test_cli_invalid_worker_count_exits_before_network_requests(self):
        self.targets(1)
        errors = io.StringIO()
        with patch("monitor.probe") as probe, redirect_stderr(errors):
            code = monitor.main(["--database", str(self.database), "run-all", "--workers", "0"])
        self.assertEqual(code, 2)
        self.assertIn("Workers must be between", errors.getvalue())
        probe.assert_not_called()

    def test_cli_cancellation_has_a_distinct_exit_code(self):
        errors = io.StringIO()
        with patch("monitor.run_targets", side_effect=KeyboardInterrupt), redirect_stderr(errors):
            code = monitor.main(["--database", str(self.database), "run-all"])
        self.assertEqual(code, 130)
        self.assertIn("Cancelled", errors.getvalue())


if __name__ == "__main__":
    unittest.main()
