from contextlib import redirect_stderr, redirect_stdout
from datetime import datetime, timedelta, timezone
from io import StringIO
import json
from pathlib import Path
import random
import tempfile
import unittest

from incidents import list_incidents
import monitor
import uptime

START = datetime(2026, 10, 27, 12, 0, tzinfo=timezone.utc)
URL = "https://example.com"
HOUR = 3600


class UptimeTests(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.database = Path(directory.name) / "checks.sqlite3"
        self.connection = monitor.connect(self.database)
        self.addCleanup(self.connection.close)

    def record(self, timeline, url=URL):
        """Save (seconds after START, state) pairs in order."""
        for seconds, state in timeline:
            monitor.save(self.connection, monitor.Check(url, self.at(seconds), state, None, 1.0, None))

    def steady(self, first, last, every=30, url=URL):
        self.record([(seconds, "up") for seconds in range(first, last + 1, every)], url)

    def at(self, seconds):
        return (START + timedelta(seconds=seconds)).isoformat()

    def report(self, until=HOUR, **options):
        return uptime.uptime_report(self.connection, until=self.at(until), **{"hours": 1, **options})

    def test_a_url_checked_and_up_the_whole_window_is_up_the_whole_window(self):
        self.steady(0, HOUR)
        row, = self.report()
        self.assertEqual((row["since"], row["until"], row["window_seconds"]), (self.at(0), self.at(HOUR), 3600.0))
        self.assertEqual((row["checks"], row["incidents"], row["unobserved_seconds"]), (121, 0, 0.0))
        self.assertEqual((row["min_downtime_seconds"], row["max_downtime_seconds"]), (0.0, 0.0))
        self.assertEqual((row["min_uptime_percent"], row["max_uptime_percent"]), (100.0, 100.0))

    def test_an_incident_counts_between_its_two_downtime_bounds(self):
        self.steady(0, 1200)
        self.record([(1230, "down"), (1260, "down"), (1290, "down"), (1320, "up")])
        self.steady(1350, HOUR)
        incident, = list_incidents(self.connection)
        row, = self.report()
        self.assertEqual(row["incidents"], 1)
        self.assertEqual(row["min_downtime_seconds"], incident["min_downtime_seconds"])
        self.assertEqual(row["max_downtime_seconds"], incident["max_downtime_seconds"])
        self.assertEqual((row["min_downtime_seconds"], row["max_downtime_seconds"]), (60.0, 120.0))
        # 120 of 3600 seconds is 96.666...%, rounded down; 60 is 98.333...%.
        self.assertEqual((row["min_uptime_percent"], row["max_uptime_percent"]), (96.666, 98.333))

    def test_an_incident_across_the_window_start_counts_only_its_part_inside(self):
        self.steady(-600, -120)
        self.record([(-90, "down"), (-60, "down"), (-30, "down"), (0, "down"), (30, "down"), (60, "up")])
        self.steady(90, HOUR)
        row, = self.report()
        self.assertEqual((row["min_downtime_seconds"], row["max_downtime_seconds"]), (30.0, 60.0))
        self.assertEqual(row["checks"], 1 + 1 + 1 + (HOUR - 90) // 30 + 1)

    def test_an_open_incident_could_last_to_the_end_of_the_window(self):
        self.steady(0, 3000)
        self.record([(3030, "down"), (3060, "down"), (3090, "up"), (3120, "down")])
        self.record([(seconds, "down") for seconds in range(3150, HOUR + 1, 30)])
        row, = self.report()
        self.assertEqual(row["min_downtime_seconds"], HOUR - 3030)
        self.assertEqual(row["max_downtime_seconds"], HOUR - 3000)

    def test_history_that_starts_down_could_have_been_down_since_the_window_began(self):
        self.record([(600, "down"), (630, "down"), (660, "up"), (690, "up")])
        self.steady(720, HOUR)
        row, = self.report()
        # Nothing is known before the first check, so the longest runs from the start to the recovery.
        self.assertEqual(row["unobserved_seconds"], 600.0)
        self.assertEqual((row["min_downtime_seconds"], row["max_downtime_seconds"]), (30.0, 660.0))

    def test_gaps_longer_than_the_limit_could_have_been_downtime(self):
        self.steady(0, 600)
        self.steady(1800, 3000)
        row, = self.report()
        # Stopped from 600 to 1800, and nothing has been checked since 3000.
        self.assertEqual(row["unobserved_seconds"], 1200.0 + 600.0)
        self.assertEqual((row["min_downtime_seconds"], row["max_downtime_seconds"]), (0.0, 1800.0))
        self.assertEqual((row["min_uptime_percent"], row["max_uptime_percent"]), (50.0, 100.0))
        relaxed, = self.report(max_gap=1200)
        self.assertEqual(relaxed["unobserved_seconds"], 0.0)

    def test_a_gap_inside_an_incident_is_counted_once(self):
        self.steady(0, 600)
        self.record([(630, "down"), (660, "down"), (2400, "down"), (2430, "up"), (2460, "up")])
        self.steady(2490, HOUR)
        row, = self.report()
        self.assertEqual(row["unobserved_seconds"], 2400.0 - 660.0)
        self.assertEqual((row["min_downtime_seconds"], row["max_downtime_seconds"]), (1770.0, 1830.0))

    def test_thresholds_follow_the_incidents_report(self):
        self.steady(0, 1200)
        self.record([(1230, "down"), (1260, "down"), (1290, "up"), (1320, "up")])
        self.steady(1350, HOUR)
        self.assertEqual(self.report()[0]["max_downtime_seconds"], 90.0)
        self.assertEqual(self.report(failures=3)[0]["max_downtime_seconds"], 0.0)
        self.assertEqual(self.report(failures=1, recoveries=3)[0]["incidents"], 1)

    def test_only_urls_with_checks_or_incidents_in_the_window_are_reported(self):
        self.steady(0, HOUR)
        self.steady(-7200, -3600, url="https://old.example")
        self.record([(-120, "up"), (-90, "down"), (-60, "down")], "https://stuck.example")
        rows = {row["url"]: row for row in self.report()}
        self.assertEqual(set(rows), {URL, "https://stuck.example"})
        # Still open and never checked again, so it could have been down all along.
        self.assertEqual(rows["https://stuck.example"]["min_downtime_seconds"], 0.0)
        self.assertEqual(rows["https://stuck.example"]["max_uptime_percent"], 100.0)
        self.assertEqual(rows["https://stuck.example"]["min_uptime_percent"], 0.0)
        # Asking for a URL by name reports it even with nothing to go on.
        old, = self.report(url="https://old.example")
        self.assertEqual((old["checks"], old["unobserved_seconds"], old["min_uptime_percent"]), (0, 3600.0, 0.0))
        never, = self.report(url="https://never.example")
        self.assertEqual(never["max_uptime_percent"], 100.0)

    def test_any_possible_downtime_keeps_the_lowest_below_100_percent(self):
        self.steady(0, HOUR, every=1)
        self.connection.execute("UPDATE checks SET state = 'down' WHERE id IN (1800, 1801)")
        row, = self.report()
        self.assertEqual(row["max_downtime_seconds"], 3.0)
        self.assertEqual(row["min_uptime_percent"], 99.916)
        # Three seconds in a year is 99.99999%, which still shows below 100.
        self.assertEqual(self.report(hours=8784)[0]["max_downtime_seconds"], 8784 * HOUR - HOUR + 3.0)
        self.assertEqual(uptime.percent(8784 * HOUR - 3, 8784 * HOUR), 99.999)
        self.assertEqual(uptime.percent(HOUR, HOUR), 100.0)

    def test_the_window_end_and_limits_are_checked(self):
        self.steady(0, HOUR)
        for options in ({"hours": 0}, {"hours": 8785}, {"hours": 1.5}, {"max_gap": 0}, {"max_gap": 86_401},
                        {"until": "2026-10-27T12:00:00"}, {"until": "yesterday"}):
            with self.subTest(options=options), self.assertRaises(ValueError):
                uptime.uptime_report(self.connection, **{"until": self.at(HOUR), **options})
        # The default window is the last 24 hours, so only a check made just now is in it.
        now = datetime.now(timezone.utc)
        monitor.save(self.connection, monitor.Check("https://now.example", now.isoformat(), "up", 200, 1.0, None))
        row, = uptime.uptime_report(self.connection)
        self.assertEqual((row["url"], row["checks"], row["window_seconds"]), ("https://now.example", 1, 86400.0))
        self.assertLessEqual(abs((datetime.fromisoformat(row["until"]) - now).total_seconds()), 60)

    def test_checks_after_the_window_and_unreadable_timestamps_are_ignored(self):
        self.steady(-600, HOUR + 600)
        self.connection.execute("INSERT INTO checks (url, checked_at, state, latency_ms) "
                                "VALUES (?, 'not a time', 'down', 1)", (URL,))
        row, = self.report()
        self.assertEqual((row["checks"], row["unobserved_seconds"], row["max_downtime_seconds"]), (121, 0.0, 0.0))
        # Ended before the last checks, the window still counts the time up to its end as checked.
        earlier, = self.report(until=HOUR - 15)
        self.assertEqual((earlier["checks"], earlier["unobserved_seconds"]), (120, 0.0))

    def test_random_histories_match_a_second_by_second_count(self):
        rng = random.Random(7)
        for trial in range(6):
            with self.subTest(trial=trial):
                self.connection.execute("DELETE FROM checks")
                url = f"https://trial-{trial}.example"
                timeline, seconds = [], rng.randint(-1800, 600)
                while seconds < HOUR + 300:
                    seconds += rng.choice((10, 20, 30, 30, 30, 400, 900))
                    timeline.append((seconds, rng.choice(("up", "up", "up", "down", "down"))))
                self.record(timeline, url)
                until = rng.randint(HOUR // 2, HOUR)
                row, = uptime.uptime_report(self.connection, until=self.at(until), hours=1, max_gap=300)
                least, most, gaps = reference(timeline, list_incidents(self.connection, limit=1000),
                                              until - HOUR, until, 300)
                self.assertEqual((row["min_downtime_seconds"], row["max_downtime_seconds"],
                                  row["unobserved_seconds"]), (least, most, gaps))
                self.assertLessEqual(row["min_uptime_percent"], row["max_uptime_percent"])

    def test_the_command_reports_by_url_or_target(self):
        self.steady(0, HOUR)
        self.steady(0, HOUR, url="https://other.example")
        monitor.add_target(self.connection, "example", URL)
        base = ["--database", str(self.database), "uptime", "--until", self.at(HOUR), "--hours", "1"]
        for extra in (["--target", "example"], ["--url", URL]):
            output = StringIO()
            with redirect_stdout(output):
                self.assertEqual(monitor.main(base + extra), 0)
            row, = json.loads(output.getvalue())
            self.assertEqual((row["url"], row["min_uptime_percent"]), (URL, 100.0))
        output = StringIO()
        with redirect_stdout(output):
            self.assertEqual(monitor.main(base + ["--max-gap", "60"]), 0)
        self.assertEqual(len(json.loads(output.getvalue())), 2)
        for extra in (["--hours", "0"], ["--until", "soon"], ["--target", "missing"]):
            with self.subTest(extra=extra), redirect_stderr(StringIO()) as errors:
                self.assertEqual(monitor.main(base + extra), 2)
            self.assertIn("Error:", errors.getvalue())


def reference(timeline, incidents, start, end, max_gap):
    """Count, second by second, what the window could and must have been down, and what went unchecked.

    Written separately from `uptime.py`: a second is unchecked when the checks on
    either side of it (or the window's end, after the last one) are more than
    `max_gap` apart, or nothing was checked before it.
    """
    def seconds(value):
        return None if value is None else (datetime.fromisoformat(value) - START).total_seconds()

    times = [moment for moment, _ in timeline if moment <= end]
    must, could = [], []
    for incident in incidents:
        opened = seconds(incident["opened_at"])
        if opened > end:
            continue
        must.append((opened, seconds(incident["last_failure_at"])))
        began, ended = seconds(incident["previous_success_at"]), seconds(incident["recovery_started_at"])
        could.append((start if began is None else began, end if ended is None else ended))
    least = most = unchecked = 0
    for second in range(start, end):
        before = [moment for moment in times if moment <= second]
        after = [moment for moment in times if moment >= second + 1] + [end]
        gap = not before or min(after) - max(before) > max_gap
        unchecked += gap
        least += any(low <= second and second + 1 <= high for low, high in must)
        most += gap or any(low <= second and second + 1 <= high for low, high in could)
    return float(least), float(most), float(unchecked)


if __name__ == "__main__":
    unittest.main()
