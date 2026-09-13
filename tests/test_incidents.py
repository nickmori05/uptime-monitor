from contextlib import redirect_stdout, redirect_stderr
from io import StringIO
import json
from pathlib import Path
import tempfile
import unittest

import monitor
from incidents import list_incidents


class IncidentTests(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.database = Path(directory.name) / "checks.db"
        self.connection = monitor.connect(self.database)
        self.addCleanup(self.connection.close)

    def record(self, states, url="https://example.com", timestamp=None):
        identifiers = []
        for state in states:
            check = monitor.Check(
                url, timestamp or f"2026-09-13T00:00:{len(identifiers):02d}+00:00",
                state, 200 if state == "up" else 503, 1.0,
                None if state == "up" else "http_503",
            )
            identifiers.append(monitor.save(self.connection, check))
        return identifiers

    def test_single_failure_does_not_open_an_incident(self):
        self.record(["down", "up", "down", "up"])
        self.assertEqual(list_incidents(self.connection), [])

    def test_opens_at_first_failure_and_resolves_on_confirmed_recovery(self):
        ids = self.record(["up", "down", "down", "up", "up"])
        incident, = list_incidents(self.connection)
        self.assertEqual(incident["state"], "resolved")
        self.assertEqual(incident["first_failure_id"], ids[1])
        self.assertEqual(incident["confirmation_check_id"], ids[2])
        self.assertEqual(incident["resolution_check_id"], ids[4])
        self.assertEqual(incident["failed_checks"], 2)
        self.assertEqual(incident["opened_at"], "2026-09-13T00:00:01+00:00")
        self.assertEqual(incident["resolved_at"], "2026-09-13T00:00:04+00:00")

    def test_flapping_stays_one_incident_until_consecutive_recovery(self):
        ids = self.record(["down", "down", "up", "down", "up"])
        incident, = list_incidents(self.connection)
        self.assertEqual(incident["state"], "open")
        self.assertEqual(incident["failed_checks"], 3)
        self.assertEqual(incident["last_check_id"], ids[-1])
        self.assertIsNone(incident["resolution_check_id"])
        self.assertIsNone(incident["resolved_at"])
        self.record(["up"])
        self.assertEqual(list_incidents(self.connection)[0]["state"], "resolved")

    def test_later_failures_start_a_new_incident(self):
        ids = self.record(["down", "down", "up", "up", "down", "down"])
        incidents = list_incidents(self.connection)
        self.assertEqual([item["first_failure_id"] for item in incidents], [ids[4], ids[0]])
        self.assertEqual([item["state"] for item in incidents], ["open", "resolved"])

    def test_interleaved_urls_have_independent_streaks(self):
        self.record(["down"])
        self.record(["up", "down"], "https://other.example")
        self.record(["down"])
        self.record(["up"], "https://other.example")
        incident, = list_incidents(self.connection)
        self.assertEqual(incident["url"], "https://example.com")

    def test_state_and_url_filters_apply_before_limit(self):
        self.record(["down", "down", "up", "up"])
        self.record(["down", "down"], "https://other.example")
        resolved, = list_incidents(self.connection, state="resolved", limit=1)
        self.assertEqual(resolved["url"], "https://example.com")
        self.assertEqual(len(list_incidents(self.connection, url="https://example.com", limit=1)), 1)
        self.assertEqual(list_incidents(self.connection, url="https://missing.example"), [])

    def test_report_survives_reopening_and_removing_target(self):
        monitor.add_target(self.connection, "api", "https://example.com")
        self.record(["down", "down"])
        expected = list_incidents(self.connection)
        monitor.remove_target(self.connection, "api")
        self.connection.close()
        with monitor.connect(self.database) as connection:
            self.assertEqual(list_incidents(connection), expected)
        connection.close()

    def test_thresholds_can_be_changed_without_changing_saved_checks(self):
        self.record(["down", "up"])
        before = monitor.history(self.connection)
        self.assertEqual(list_incidents(self.connection), [])
        self.assertEqual(list_incidents(self.connection, failures=1, recoveries=1)[0]["state"], "resolved")
        self.assertEqual(monitor.history(self.connection), before)

    def test_record_order_is_used_even_if_clock_moves_backwards(self):
        first, = self.record(["down"], timestamp="2026-09-13T12:00:00+00:00")
        self.record(["down", "up", "up"], timestamp="2026-09-12T12:00:00+00:00")
        incident, = list_incidents(self.connection)
        self.assertEqual(incident["first_failure_id"], first)
        self.assertEqual(incident["state"], "resolved")

    def test_invalid_options_are_rejected(self):
        for name, value in [("limit", 0), ("limit", 1001), ("failures", True),
                            ("failures", 101), ("recoveries", 0), ("recoveries", 1.5),
                            ("state", "closed")]:
            with self.subTest(name=name, value=value), self.assertRaises(ValueError):
                list_incidents(self.connection, **{name: value})

    def test_cli_reports_json_and_invalid_threshold_exit_code(self):
        self.record(["down", "down"])
        output = StringIO()
        with redirect_stdout(output):
            self.assertEqual(monitor.main(["--database", str(self.database), "incidents", "--state", "open"]), 0)
        self.assertEqual(json.loads(output.getvalue())[0]["state"], "open")
        with redirect_stderr(StringIO()):
            self.assertEqual(monitor.main(["--database", str(self.database), "incidents", "--failures", "0"]), 2)
