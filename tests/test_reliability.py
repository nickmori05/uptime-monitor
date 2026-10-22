from contextlib import closing
from pathlib import Path
import random
import tempfile
import unittest
from unittest.mock import patch

import alerts
from crashes import crash_recovery, kill_during_delivery
from faults import EXPECTED
from incidents import scan_incidents
import monitor
from scenarios import alert_delivery, oracle, probe_faults


class OracleTests(unittest.TestCase):
    def test_oracle_follows_the_incident_rules(self):
        cases = [
            (["down"], 2, 2, []),
            (["down", "down", "up", "down", "up", "up"], 2, 2, [("opened", 0, 1), ("resolved", 0, 5)]),
            (["down", "up", "down", "down", "down"], 2, 2, [("opened", 2, 3)]),
            (["down", "down", "up", "down", "down"], 3, 1, []),
            (["down", "down", "up", "down", "down", "down"], 3, 1, [("opened", 3, 5)]),
            (["down", "down", "down", "up", "down", "down", "down"], 3, 1,
             [("opened", 0, 2), ("resolved", 0, 3), ("opened", 4, 6)]),
        ]
        for states, failures, recoveries, expected in cases:
            with self.subTest(states=states, failures=failures, recoveries=recoveries):
                self.assertEqual(oracle(states, failures, recoveries), expected)

    def test_oracle_agrees_with_saved_incidents_on_random_histories(self):
        rng = random.Random(7)
        with tempfile.TemporaryDirectory() as directory, \
                closing(monitor.connect(Path(directory) / "checks.sqlite3")) as connection:
            states = {url: [rng.choice(("up", "down")) for _ in range(200)] for url in ("https://a.test", "https://b.test")}
            ids = {url: [] for url in states}
            for index in range(200):
                for url in states:
                    check = monitor.Check(url, "2026-10-20T00:00:00+00:00", states[url][index], None, 1.0, None)
                    ids[url].append(monitor.save(connection, check))
            for failures, recoveries in ((1, 1), (2, 2), (3, 2)):
                with self.subTest(failures=failures, recoveries=recoveries):
                    derived = sorted((url, event, ids[url][first], ids[url][index])
                                     for url in states for event, first, index in oracle(states[url], failures,
                                                                                         recoveries))
                    saved = []
                    for incident in scan_incidents(connection, failures=failures, recoveries=recoveries):
                        saved.append((incident["url"], "opened", incident["first_failure_id"],
                                      incident["confirmation_check_id"]))
                        if incident["state"] == "resolved":
                            saved.append((incident["url"], "resolved", incident["first_failure_id"],
                                          incident["resolution_check_id"]))
                    self.assertEqual(derived, sorted(saved))
                    self.assertGreater(len(derived), 10)


class ProbeFaultTests(unittest.TestCase):
    def test_every_fault_is_classified_within_its_time_limit(self):
        report = probe_faults(seed=1, rounds=6)
        self.assertTrue(report["ok"], report)
        self.assertEqual(set(report["faults"]), set(EXPECTED))
        self.assertEqual(report["checks"], 6 * len(EXPECTED))

    def test_ignoring_deadlines_is_caught(self):
        def without_deadline(url, timeout, deadline):
            return monitor.request_status(url, timeout)

        with patch.object(monitor, "request_within", side_effect=without_deadline):
            report = probe_faults(seed=1, rounds=1)
        self.assertFalse(report["ok"])
        self.assertEqual({row["fault"] for row in report["mismatches"]}, {"trickle", "dns"})
        self.assertGreater(report["max_round_seconds"], report["deadline_seconds"] + 1)


class AlertDeliveryTests(unittest.TestCase):
    def test_alerts_arrive_once_and_at_the_first_chance(self):
        for seed in (1, 2):
            with self.subTest(seed=seed):
                report = alert_delivery(seed=seed, rounds=30)
                self.assertTrue(report["ok"], report)
                for exercised in ("expected_alerts", "rounds_without_alerting", "rounds_with_webhook_down"):
                    self.assertGreater(report[exercised], 0, report)
                self.assertEqual(report["delivered"], report["expected_alerts"])

    def test_broken_delivery_is_caught(self):
        real_send, real_events = alerts.send_webhook, alerts.incident_events

        def drop_resolutions(webhook, body):
            return None if body["event"] == "resolved" else real_send(webhook, body)

        broken = {
            "lost": patch.object(alerts, "send_webhook", side_effect=drop_resolutions),
            "duplicates": patch.object(alerts, "record"),
            "out_of_order": patch.object(alerts, "incident_events",
                                         side_effect=lambda connection: real_events(connection)[::-1]),
            "key_collisions": patch.object(alerts, "idempotency_key", return_value="same-for-every-alert"),
        }
        for measure, fault in broken.items():
            with self.subTest(measure=measure), fault:
                report = alert_delivery(seed=1, rounds=30)
                self.assertFalse(report["ok"])
                self.assertGreater(report[measure], 0, report)


class SkipResolvedDeliveryTests(unittest.TestCase):
    def test_incidents_resolved_before_their_opening_is_sent_arrive_not_at_all(self):
        # At this size, seeds 2 and 3 each include an incident that resolves before its opening can be sent.
        for seed in (2, 3):
            with self.subTest(seed=seed):
                report = alert_delivery(seed=seed, rounds=30, skip_resolved=True)
                self.assertTrue(report["ok"], report)
                self.assertGreater(report["skipped_incidents"], 0, report)
                self.assertEqual(report["delivered"], report["expected_alerts"])

    def test_skipping_too_little_or_too_much_is_caught(self):
        def every_resolved(_connection, events):
            return [event for event in events if event["incident"]["state"] == "resolved"]

        broken = {
            "unexpected": patch.object(alerts, "skip_resolved_incidents", return_value=0),
            "lost": patch.object(alerts, "resolved_unsent", side_effect=every_resolved),
        }
        for measure, fault in broken.items():
            with self.subTest(measure=measure), fault:
                report = alert_delivery(seed=2, rounds=30, skip_resolved=True)
                self.assertFalse(report["ok"])
                self.assertGreater(report[measure], 0, report)


class CrashTests(unittest.TestCase):
    def test_sigkill_loses_no_alerts_and_keeps_the_database_intact(self):
        report = crash_recovery(seed=1)
        self.assertTrue(report["ok"], report)
        self.assertGreater(report["kills"], 0, report)
        self.assertGreater(report["expected_alerts"], 0, report)
        self.assertEqual(report["repeated_after_dedupe"], 0, report)

    def test_a_kill_after_the_receiver_accepts_resends_the_alert_once(self):
        report = kill_during_delivery()
        self.assertTrue(report["ok"], report)
        self.assertEqual((report["copies_received"], report["recorded_sent"]), (2, 1))
        # Both copies carry one key, so a receiver that drops repeated keys acts once.
        self.assertEqual((report["distinct_keys"], report["repeated_after_dedupe"]), (1, 0))


if __name__ == "__main__":
    unittest.main()
