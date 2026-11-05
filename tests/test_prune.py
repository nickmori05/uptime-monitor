from contextlib import redirect_stderr, redirect_stdout
from datetime import datetime, timedelta, timezone
import io
import json
from pathlib import Path
import random
import sqlite3
import tempfile
import unittest
from unittest.mock import patch

import alerts
from incidents import scan_incidents
import monitor
import retention

START = datetime(2026, 1, 1, tzinfo=timezone.utc)
NOW = START + timedelta(days=100)
URL = "https://example.com"
WEBHOOK = "https://hooks.example/alert"


def incidents(connection):
    return sorted(scan_incidents(connection), key=lambda incident: incident["first_failure_id"])


def day(days):
    return (START + timedelta(days=days)).isoformat()


class PruneTests(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.directory = Path(directory.name)
        self.database = self.directory / "checks.sqlite3"
        self.connection = monitor.connect(self.database)
        self.addCleanup(self.connection.close)

    def record(self, timeline, url=URL, connection=None):
        """Save (days after START, state) pairs in order and return their IDs."""
        return [monitor.save(connection or self.connection,
                             monitor.Check(url, day(days) if isinstance(days, (int, float)) else days, state,
                                           200 if state == "up" else 503, 1.0, None if state == "up" else "http_503"))
                for days, state in timeline]

    def prune(self, keep_days=30, connection=None):
        return retention.prune(connection or self.connection, keep_days, now=NOW)

    def ids(self, url=URL):
        return [row["id"] for row in self.connection.execute("SELECT id FROM checks WHERE url = ? ORDER BY id", (url,))]

    def deliver(self, outcomes=(), connection=None):
        """Run one alert round with `send_webhook` answering from `outcomes` (None accepts), returning bodies sent."""
        answers = iter(outcomes)
        with patch.object(alerts, "send_webhook", side_effect=lambda webhook, body: next(answers, None)), \
                redirect_stderr(io.StringIO()):
            return alerts.Alerter(connection or self.connection, WEBHOOK)()

    def test_checks_before_the_last_success_before_the_cutoff_are_deleted(self):
        ids = self.record([(10, "up"), (20, "up"), (50, "up"), (80, "up"), (90, "up")])
        report = self.prune(30)
        # The cutoff is day 70, so the success on day 50 is kept as the start of what remains.
        self.assertEqual(report, {"cutoff": day(70), "deleted_checks": 2, "deleted_alerts": 0, "urls": [
            {"url": URL, "deleted_checks": 2, "kept_from_id": ids[2], "kept_from_at": day(50)}]})
        self.assertEqual(self.ids(), ids[2:])
        self.assertFalse(self.connection.in_transaction)
        self.assertEqual(self.prune(30)["urls"], [])

    def test_nothing_newer_than_the_cutoff_is_ever_deleted(self):
        self.record([(80, "up"), (90, "up")])
        self.record([(10, "down"), (20, "down"), (75, "up")], "https://down.example")
        self.assertEqual(self.prune(30)["deleted_checks"], 0)
        self.assertEqual(len(self.ids()) + len(self.ids("https://down.example")), 5)

    def test_an_incident_open_at_the_cutoff_keeps_its_checks_and_the_success_before_it(self):
        ids = self.record([(10, "up"), (20, "up"), (30, "down"), (40, "down"), (50, "up"), (60, "down"),
                           (80, "up"), (90, "up")])
        before = scan_incidents(self.connection)
        report = self.prune(30)
        # The success on day 50 is inside the incident, so the cut falls at the last success before it.
        self.assertEqual(report["urls"][0]["kept_from_id"], ids[1])
        self.assertEqual(self.ids(), ids[1:])
        self.assertEqual(scan_incidents(self.connection), before)
        self.assertEqual(before[0]["previous_success_at"], day(20))

    def test_a_resolved_incident_is_deleted_whole_or_not_at_all(self):
        ids = self.record([(10, "up"), (20, "down"), (30, "down"), (40, "up"), (50, "down"), (55, "up"),
                           (60, "up"), (65, "up"), (90, "down"), (95, "down")])
        before = scan_incidents(self.connection)
        self.prune(30)
        # Day 65 is the last success before day 70 outside an incident; day 40's didn't resolve one.
        self.assertEqual(self.ids(), ids[7:])
        later, = scan_incidents(self.connection)
        self.assertEqual(later, next(incident for incident in before if incident["first_failure_id"] == ids[8]))
        self.assertEqual(later["previous_success_at"], day(65))

    def test_a_clock_set_back_never_deletes_a_check_from_inside_the_window(self):
        ids = self.record([(10, "up"), (80, "up"), (20, "up"), (90, "up")])
        self.assertEqual(self.prune(30)["deleted_checks"], 0)
        self.record([(5, "up"), ("not a time", "up"), (6, "up")], "https://odd.example")
        self.prune(30)
        self.assertEqual(self.ids(), ids)
        self.assertEqual(len(self.ids("https://odd.example")), 3)

    def test_sent_alerts_go_with_their_incident_and_kept_ones_keep_their_keys(self):
        self.deliver()
        ids = self.record([(10, "up"), (20, "down"), (30, "down"), (40, "up"), (50, "up"),
                           (60, "down"), (65, "down")])
        self.assertEqual(len(self.deliver()), 3)
        kept = [alert for alert in alerts.list_alerts(self.connection) if alert["first_failure_id"] == ids[5]]
        report = self.prune(30)
        self.assertEqual((report["deleted_checks"], report["deleted_alerts"]), (4, 2))
        self.assertEqual(alerts.list_alerts(self.connection), kept)
        self.assertEqual(self.connection.execute("SELECT COUNT(*) FROM deliveries").fetchone()[0], 1)
        self.assertEqual(alerts.unsent_alerts(self.connection), [])

    def test_an_alert_still_owed_keeps_its_incident(self):
        self.deliver()
        ids = self.record([(10, "up"), (20, "down"), (30, "down"), (40, "up"), (50, "up"), (60, "up")])
        self.deliver(["http_500"])
        owed = alerts.unsent_alerts(self.connection)
        self.assertEqual(len(owed), 2)
        self.assertEqual(self.prune(30)["urls"], [])
        self.assertEqual(self.ids(), ids)
        self.assertEqual(alerts.unsent_alerts(self.connection), owed)
        self.assertEqual(len(self.deliver()), 2)
        self.assertEqual(self.prune(30)["urls"][0]["kept_from_id"], ids[5])

    def test_events_from_before_alerting_was_enabled_hold_nothing(self):
        ids = self.record([(10, "up"), (20, "down"), (30, "down"), (40, "up"), (50, "up"), (60, "up")])
        self.assertIsNone(alerts.enabled_after(self.connection))
        self.prune(30)
        self.assertEqual(self.ids(), ids[5:])
        self.assertEqual(self.deliver(), [])
        self.assertEqual(alerts.list_alerts(self.connection), [])

    def test_invalid_retention_is_rejected(self):
        self.record([(10, "up"), (20, "up")])
        for value in (0, -1, 36501, 1.5, True, "30"):
            with self.subTest(value=value), self.assertRaisesRegex(ValueError, "between 1 and 36500"):
                self.prune(value)
        self.assertEqual(len(self.ids()), 2)

    def test_cli_prunes_and_rejects_bad_input(self):
        now = datetime.now(timezone.utc)
        old = (now - timedelta(days=40)).isoformat()
        ids = self.record([(old, "up"), (old, "up"), (now.isoformat(), "up")])
        output = io.StringIO()
        with redirect_stdout(output):
            self.assertEqual(monitor.main(["--database", str(self.database), "prune", "--keep-days", "30"]), 0)
        self.assertEqual(json.loads(output.getvalue())["deleted_checks"], 1)
        self.assertEqual(self.ids(), ids[1:])
        with redirect_stderr(io.StringIO()) as errors:
            self.assertEqual(monitor.main(["--database", str(self.database), "prune", "--keep-days", "0"]), 2)
        self.assertIn("between 1 and 36500", errors.getvalue())
        with redirect_stderr(io.StringIO()), self.assertRaises(SystemExit) as exit:
            monitor.main(["--database", str(self.database), "prune"])
        self.assertEqual(exit.exception.code, 2)

    def test_pruning_never_changes_later_incidents_or_alerts(self):
        urls = [URL, "https://b.example", "https://c.example"]
        for seed in range(20):
            with self.subTest(seed=seed):
                self.assert_prune_is_invisible(random.Random(seed), urls, f"seed{seed}")

    def assert_prune_is_invisible(self, rng, urls, name):
        """Prune one copy of a random history and compare it with an unpruned copy as both carry on."""
        pruned = monitor.connect(self.directory / f"{name}-pruned.sqlite3")
        whole = monitor.connect(self.directory / f"{name}-whole.sqlite3")
        self.addCleanup(pruned.close)
        self.addCleanup(whole.close)
        moment, alerting, outage = 0.0, False, False

        def step(count):
            nonlocal moment, alerting, outage
            for _ in range(count):
                # Mostly forward in time, with the odd clock set back.
                moment += rng.choice([0.25, 0.5, 1, 1.5]) if rng.random() < 0.95 else rng.choice([-2, -10])
                timeline = [(moment, rng.choice(["up", "up", "down"]))]
                url = rng.choice(urls)
                self.record(timeline, url, pruned)
                self.record(timeline, url, whole)
                if not alerting and rng.random() < 0.03:
                    alerting = True
                if rng.random() < 0.04:
                    # The webhook goes down (or comes back) for a stretch, so alerts back up behind it.
                    outage = not outage
                if alerting:
                    outcomes = [rng.choice(["http_500"] if outage else [None, None, "http_500"]) for _ in range(4)]
                    self.assertEqual(self.deliver(outcomes, pruned), self.deliver(outcomes, whole))

        step(rng.randrange(80, 200))
        before = incidents(pruned)
        alerts_before = alerts.list_alerts(pruned, 1000)
        unsent = alerts.unsent_alerts(pruned)
        checks_before = [dict(row) for row in pruned.execute("SELECT * FROM checks ORDER BY id")]
        keep_days = rng.randrange(1, max(int(moment * 0.8), 2))
        report = retention.prune(pruned, keep_days, now=START + timedelta(days=moment))
        cutoff = datetime.fromisoformat(report["cutoff"])
        kept = {row["id"] for row in pruned.execute("SELECT id FROM checks")}
        self.assertEqual(len(checks_before) - len(kept), report["deleted_checks"])
        for check in checks_before:
            if check["id"] not in kept:
                self.assertLess(datetime.fromisoformat(check["checked_at"]), cutoff)
        # Every incident left is identical, and none that is open or owed an alert is gone.
        after = incidents(pruned)
        self.assertEqual(after, [incident for incident in before if incident["first_failure_id"] in kept])
        self.assertTrue(all(incident["first_failure_id"] in kept for incident in before
                            if incident["state"] == "open"))
        self.assertEqual(alerts.unsent_alerts(pruned), unsent)
        self.assertEqual(alerts.list_alerts(pruned, 1000),
                         [alert for alert in alerts_before if alert["first_failure_id"] in kept])
        self.assertTrue(all(alert["idempotency_key"] for alert in alerts.list_alerts(pruned, 1000)))
        self.assertEqual(pruned.execute("SELECT COUNT(*) FROM deliveries WHERE first_failure_id NOT IN "
                                        "(SELECT id FROM checks)").fetchone()[0], 0)
        step(rng.randrange(20, 60))
        newest = checks_before[-1]["id"] if checks_before else 0
        self.assertEqual(incidents(pruned), [incident for incident in incidents(whole)
                                             if incident["first_failure_id"] in kept
                                             or incident["first_failure_id"] > newest])
        self.assertEqual(alerts.unsent_alerts(pruned), alerts.unsent_alerts(whole))
        self.assertEqual(pruned.execute("PRAGMA integrity_check").fetchone()[0], "ok")


    def test_an_empty_database_has_nothing_to_prune(self):
        self.assertEqual(self.prune(1), {"cutoff": (NOW - timedelta(days=1)).isoformat(), "deleted_checks": 0,
                                         "deleted_alerts": 0, "urls": []})

    def test_a_failure_part_way_deletes_nothing(self):
        for url in (URL, "https://b.example"):
            self.record([(1, "up"), (2, "up"), (3, "up")], url)
        connection, deletes = self.connection, []

        class Failing:
            """Pass everything through to the connection, but fail the second URL's delete."""

            def execute(self, sql, *values):
                if sql.startswith("DELETE FROM checks"):
                    deletes.append(sql)
                    if len(deletes) == 2:
                        raise sqlite3.OperationalError("disk I/O error")
                return connection.execute(sql, *values)

            def __getattr__(self, attribute):
                return getattr(connection, attribute)

        with self.assertRaises(sqlite3.OperationalError):
            retention.prune(Failing(), 1, now=NOW)
        self.assertFalse(self.connection.in_transaction)
        self.assertEqual(len(self.ids()) + len(self.ids("https://b.example")), 6)
