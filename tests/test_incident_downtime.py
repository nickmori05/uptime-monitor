from contextlib import redirect_stdout
from datetime import datetime, timedelta, timezone
from io import StringIO
import json
from pathlib import Path
import random
import tempfile
import unittest

import alerts
from incidents import list_incidents, seconds_between
import monitor

START = datetime(2026, 10, 27, 12, 0, tzinfo=timezone.utc)
URL = "https://example.com"


class DowntimeTests(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.database = Path(directory.name) / "checks.sqlite3"
        self.connection = monitor.connect(self.database)
        self.addCleanup(self.connection.close)

    def record(self, timeline, url=URL):
        """Save (seconds after START, state) pairs in order."""
        for seconds, state in timeline:
            checked_at = (START + timedelta(seconds=seconds)).isoformat()
            monitor.save(self.connection, monitor.Check(url, checked_at, state, None, 1.0, None))

    def at(self, seconds):
        return (START + timedelta(seconds=seconds)).isoformat()

    def test_a_resolved_incident_is_bounded_by_the_checks_around_it(self):
        self.record([(0, "up"), (10, "down"), (20, "down"), (30, "down"), (40, "up"), (50, "up")])
        incident, = list_incidents(self.connection)
        self.assertEqual((incident["previous_success_at"], incident["last_failure_at"],
                          incident["recovery_started_at"]), (self.at(0), self.at(30), self.at(40)))
        # Down from at least the first failure to the last; at most from the last success to the recovery.
        self.assertEqual((incident["min_downtime_seconds"], incident["max_downtime_seconds"]), (20.0, 40.0))

    def test_flapping_counts_from_the_last_failure_to_the_final_recovery(self):
        self.record([(0, "up"), (10, "down"), (20, "down"), (30, "up"), (40, "down"), (50, "up"), (60, "up")])
        incident, = list_incidents(self.connection)
        self.assertEqual((incident["last_failure_at"], incident["recovery_started_at"]), (self.at(40), self.at(50)))
        self.assertEqual((incident["min_downtime_seconds"], incident["max_downtime_seconds"]), (30.0, 50.0))

    def test_the_recovery_run_follows_the_thresholds(self):
        self.record([(0, "up"), (10, "down"), (20, "up"), (30, "up"), (40, "up")])
        self.assertEqual(list_incidents(self.connection, failures=1, recoveries=3)[0]["max_downtime_seconds"], 20.0)
        self.assertEqual(list_incidents(self.connection, failures=1, recoveries=3)[0]["min_downtime_seconds"], 0.0)
        self.assertEqual(list_incidents(self.connection, failures=2), [])

    def test_an_open_incident_has_no_longest_downtime_yet(self):
        self.record([(0, "up"), (10, "down"), (25, "down"), (30, "up")])
        incident, = list_incidents(self.connection)
        self.assertEqual(incident["state"], "open")
        self.assertEqual(incident["min_downtime_seconds"], 15.0)
        # One success isn't a recovery yet, so it doesn't end the outage.
        self.assertIsNone(incident["recovery_started_at"])
        self.assertIsNone(incident["max_downtime_seconds"])

    def test_history_that_starts_down_has_no_known_start(self):
        self.record([(0, "down"), (10, "down"), (20, "up"), (30, "up")])
        incident, = list_incidents(self.connection)
        self.assertIsNone(incident["previous_success_at"])
        self.assertEqual(incident["min_downtime_seconds"], 10.0)
        self.assertIsNone(incident["max_downtime_seconds"])

    def test_a_later_incident_starts_from_its_own_last_success(self):
        self.record([(0, "down"), (10, "down"), (20, "up"), (30, "up"), (40, "up"), (50, "down"), (60, "down"),
                     (70, "up"), (80, "up")])
        later, earlier = list_incidents(self.connection)
        self.assertEqual(later["previous_success_at"], self.at(40))
        self.assertEqual(later["max_downtime_seconds"], 30.0)
        self.assertEqual(earlier["recovery_started_at"], self.at(20))

    def test_urls_are_bounded_separately(self):
        self.record([(0, "up")], "https://a.example")
        self.record([(5, "up"), (10, "down"), (20, "down"), (30, "up"), (40, "up")])
        self.record([(50, "down"), (60, "down")], "https://a.example")
        bounds = {item["url"]: (item["previous_success_at"], item["max_downtime_seconds"])
                  for item in list_incidents(self.connection)}
        self.assertEqual(bounds, {URL: (self.at(5), 25.0), "https://a.example": (self.at(0), None)})

    def test_clock_changes_and_unreadable_timestamps_do_not_break_the_report(self):
        self.assertEqual(seconds_between(self.at(30), self.at(10)), 0.0)
        self.assertIsNone(seconds_between("not a time", self.at(10)))
        self.assertIsNone(seconds_between(None, self.at(10)))
        # A clock set back during the outage reports 0 rather than a negative span.
        self.record([(100, "up"), (90, "down"), (80, "down"), (70, "up"), (60, "up")])
        incident, = list_incidents(self.connection)
        self.assertEqual((incident["min_downtime_seconds"], incident["max_downtime_seconds"]), (0.0, 0.0))

    def test_random_histories_always_give_consistent_bounds(self):
        rng = random.Random(5)
        timeline, seconds = [], 0
        for _ in range(400):
            seconds += rng.randint(1, 30)
            timeline.append((seconds, rng.choice(("up", "up", "down"))))
        self.record(timeline)
        incidents = list_incidents(self.connection, limit=1000, failures=2, recoveries=2)
        self.assertGreater(len(incidents), 10)
        for incident in incidents:
            with self.subTest(first=incident["first_failure_id"]):
                lowest, highest = incident["min_downtime_seconds"], incident["max_downtime_seconds"]
                self.assertGreaterEqual(lowest, 0)
                if incident["state"] == "open" or incident["previous_success_at"] is None:
                    self.assertIsNone(highest)
                    continue
                self.assertGreater(highest, lowest)
                self.assertLess(incident["previous_success_at"], incident["opened_at"])
                self.assertLess(incident["last_failure_at"], incident["recovery_started_at"])
                self.assertLessEqual(incident["recovery_started_at"], incident["resolved_at"])

    def test_the_command_and_alerts_carry_the_bounds(self):
        self.record([(0, "up"), (10, "down"), (20, "down"), (30, "up"), (40, "up")])
        output = StringIO()
        with redirect_stdout(output):
            self.assertEqual(monitor.main(["--database", str(self.database), "incidents"]), 0)
        incident, = json.loads(output.getvalue())
        self.assertEqual((incident["min_downtime_seconds"], incident["max_downtime_seconds"]), (10.0, 30.0))
        alerts.skip_backlog(self.connection)
        self.record([(50, "down"), (60, "down"), (70, "up"), (80, "up")])
        bodies = [alerts.payload(event) for event in alerts.pending_alerts(self.connection)]
        self.assertEqual([body["event"] for body in bodies], ["opened", "resolved"])
        self.assertEqual(bodies[1]["incident"]["max_downtime_seconds"], 30.0)


if __name__ == "__main__":
    unittest.main()
