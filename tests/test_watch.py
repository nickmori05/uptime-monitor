from contextlib import redirect_stderr, redirect_stdout
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from io import StringIO
import json
from pathlib import Path
import selectors
import signal
import subprocess
import sys
import tempfile
from threading import Thread
import unittest
from unittest.mock import patch

import monitor


class WatchTests(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.database = Path(directory.name) / "checks.db"
        self.connection = monitor.connect(self.database)
        self.addCleanup(self.connection.close)
        monitor.add_target(self.connection, "api", "https://example.com")

    def result(self, state="up"):
        return monitor.Check("https://example.com", "2026-09-13T00:00:00+00:00", state,
                             200 if state == "up" else 503, 1.0, None if state == "up" else "http_503")

    def test_wait_happens_after_saved_batch_and_not_after_final_batch(self):
        saved_counts = []
        with patch.object(monitor, "probe", return_value=self.result()), \
                patch.object(monitor, "sleep", side_effect=lambda seconds: saved_counts.append(
                    (seconds, len(monitor.history(self.connection))))) as sleep:
            batches = list(monitor.watch_targets(self.connection, interval=7, count=3, workers=1))
        self.assertEqual(saved_counts, [(7, 1), (7, 2)])
        self.assertEqual(sleep.call_count, 2)
        self.assertEqual([batch["cycle"] for batch in batches], [1, 2, 3])
        self.assertEqual([batch["results"][0]["id"] for batch in batches], [1, 2, 3])

    def test_targets_reload_between_batches_from_another_connection(self):
        def replace_target(seconds):
            connection = monitor.connect(self.database)
            try:
                monitor.remove_target(connection, "api")
                monitor.add_target(connection, "new-api", "https://new.example", timeout=2)
            finally:
                connection.close()

        with patch.object(monitor, "probe", return_value=self.result()) as probe, \
                patch.object(monitor, "sleep", side_effect=replace_target):
            batches = list(monitor.watch_targets(self.connection, count=2))
        self.assertEqual([b["results"][0]["target"] for b in batches], ["api", "new-api"])
        self.assertEqual(probe.call_args.args, ("https://new.example", 2))

    def test_empty_target_list_exits_without_waiting(self):
        monitor.remove_target(self.connection, "api")
        with patch.object(monitor, "probe") as probe, patch.object(monitor, "sleep") as sleep:
            self.assertEqual(list(monitor.watch_targets(self.connection)), [{"cycle": 1, "results": []}])
        probe.assert_not_called()
        sleep.assert_not_called()

    def test_removing_last_target_stops_next_cycle(self):
        with patch.object(monitor, "probe", return_value=self.result()), \
                patch.object(monitor, "sleep", side_effect=lambda seconds: monitor.remove_target(self.connection, "api")):
            batches = list(monitor.watch_targets(self.connection))
        self.assertEqual(len(batches), 2)
        self.assertEqual(batches[-1], {"cycle": 2, "results": []})

    def test_invalid_options_never_start_a_batch(self):
        for option, value in [("interval", 0), ("interval", float("nan")), ("interval", float("inf")),
                              ("interval", 86401), ("interval", True), ("count", 0),
                              ("count", -1), ("count", 2.5), ("count", True), ("workers", 0)]:
            with self.subTest(option=option, value=value), patch.object(monitor, "run_targets") as run:
                with self.assertRaises(ValueError):
                    list(monitor.watch_targets(self.connection, **{option: value}))
                run.assert_not_called()

    def test_cli_streams_each_batch_and_reports_any_failure(self):
        output = StringIO()
        with patch.object(monitor, "probe", side_effect=[self.result("down"), self.result()]), \
                patch.object(monitor, "sleep"), redirect_stdout(output):
            code = monitor.main(["--database", str(self.database), "watch", "--count", "2"])
        self.assertEqual(code, 1)
        batches = [json.loads(line) for line in output.getvalue().splitlines()]
        self.assertEqual([b["results"][0]["state"] for b in batches], ["down", "up"])
        self.assertEqual(len(monitor.history(self.connection)), 2)

    def test_healthy_finite_watch_exits_successfully(self):
        with patch.object(monitor, "probe", return_value=self.result()), redirect_stdout(StringIO()):
            self.assertEqual(monitor.main(["--database", str(self.database), "watch", "--count", "1"]), 0)

    def test_cli_invalid_config_and_storage_errors_exit_two(self):
        with redirect_stderr(StringIO()), patch.object(monitor, "probe") as probe:
            self.assertEqual(monitor.main(["--database", str(self.database), "watch", "--interval", "nan"]), 2)
            probe.assert_not_called()
        with redirect_stderr(StringIO()), patch.object(monitor, "run_targets", side_effect=monitor.sqlite3.OperationalError("locked")):
            self.assertEqual(monitor.main(["--database", str(self.database), "watch"]), 2)

    def test_interrupt_between_batches_keeps_saved_results(self):
        for interruption, code in [(KeyboardInterrupt, 130), (monitor.ShutdownRequested, 143)]:
            with self.subTest(interruption=interruption), \
                    patch.object(monitor, "probe", return_value=self.result()), \
                    patch.object(monitor, "sleep", side_effect=interruption), \
                    redirect_stdout(StringIO()), redirect_stderr(StringIO()):
                before = len(monitor.history(self.connection))
                self.assertEqual(monitor.main(["--database", str(self.database), "watch"]), code)
                self.assertEqual(len(monitor.history(self.connection)), before + 1)


class HealthyEndpoint(BaseHTTPRequestHandler):
    def do_GET(self):
        self.send_response(204)
        self.end_headers()

    def log_message(self, *args):
        pass


@unittest.skipIf(sys.platform == "win32", "POSIX signal behavior")
class WatchProcessTests(unittest.TestCase):
    def test_signals_stop_a_waiting_watcher_and_preserve_history(self):
        server = ThreadingHTTPServer(("127.0.0.1", 0), HealthyEndpoint)
        thread = Thread(target=server.serve_forever, daemon=True)
        thread.start()
        self.addCleanup(server.server_close)
        self.addCleanup(thread.join)
        self.addCleanup(server.shutdown)
        with tempfile.TemporaryDirectory() as directory:
            for stop_signal, code in [(signal.SIGINT, 130), (signal.SIGTERM, 143)]:
                with self.subTest(signal=stop_signal):
                    database = Path(directory) / f"{stop_signal}.db"
                    connection = monitor.connect(database)
                    try:
                        monitor.add_target(connection, "api", f"http://127.0.0.1:{server.server_port}")
                    finally:
                        connection.close()
                    process = subprocess.Popen(
                        [sys.executable, str(Path(monitor.__file__).resolve()), "--database", str(database),
                         "watch", "--interval", "60"], stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                    )
                    try:
                        with selectors.DefaultSelector() as selector:
                            selector.register(process.stdout, selectors.EVENT_READ)
                            self.assertTrue(selector.select(timeout=10), "watch did not stream its first batch")
                        batch = json.loads(process.stdout.readline())
                        self.assertEqual(batch["results"][0]["state"], "up")
                        process.send_signal(stop_signal)
                        stdout, stderr = process.communicate(timeout=5)
                        self.assertEqual(process.returncode, code, stderr.decode())
                        self.assertEqual(stdout, b"")
                    finally:
                        if process.poll() is None:
                            process.kill()
                        process.communicate()
                    connection = monitor.connect(database)
                    try:
                        self.assertEqual(len(monitor.history(connection)), 1)
                    finally:
                        connection.close()
