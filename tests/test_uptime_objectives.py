from contextlib import redirect_stderr, redirect_stdout
from datetime import datetime, timedelta, timezone
from io import StringIO
import json
from pathlib import Path
import random
import sqlite3
import tempfile
from threading import Thread
import unittest
from urllib.error import HTTPError
from urllib.parse import quote
from urllib.request import urlopen

import api
import monitor
import uptime

START = datetime(2026, 10, 27, 12, 0, tzinfo=timezone.utc)
URL = "https://example.com"
HOUR = 3600


class ObjectiveSettingTests(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.database = Path(directory.name) / "checks.sqlite3"
        self.connection = monitor.connect(self.database)
        self.addCleanup(self.connection.close)

    def cli(self, *arguments):
        output, errors = StringIO(), StringIO()
        with redirect_stdout(output), redirect_stderr(errors):
            code = monitor.main(["--database", str(self.database), *arguments])
        return code, output.getvalue(), errors.getvalue()

    def test_targets_save_change_and_clear_an_objective(self):
        code, output, _ = self.cli("add", "api", URL, "--objective", "99.9")
        self.assertEqual(code, 0)
        self.assertEqual(json.loads(output)["objective"], 99.9)
        self.assertEqual(monitor.get_target(self.connection, "api")["objective"], 99.9)
        self.assertEqual(json.loads(self.cli("update", "api", "--objective", "99.5")[1])["objective"], 99.5)
        # Changing something else keeps the objective.
        self.assertEqual(json.loads(self.cli("update", "api", "--timeout", "2")[1])["objective"], 99.5)
        self.assertIsNone(json.loads(self.cli("update", "api", "--no-objective")[1])["objective"])
        self.assertIsNone(monitor.add_target(self.connection, "plain", URL)["objective"])

    def test_objectives_must_be_a_percentage_below_100(self):
        for value in (0, 100, -1, 100.5, float("nan"), float("inf"), True):
            with self.subTest(value=value), self.assertRaisesRegex(ValueError, "Objective"):
                monitor.add_target(self.connection, "api", URL, objective=value)
        self.assertEqual(monitor.list_targets(self.connection), [])
        monitor.add_target(self.connection, "api", URL, objective=50)
        with self.assertRaisesRegex(ValueError, "not both"):
            monitor.update_target(self.connection, "api", objective=99.0, clear_objective=True)
        for flags in (["--objective", "100"], ["--objective", "nan"]):
            with self.subTest(flags=flags):
                code, _, errors = self.cli("update", "api", *flags)
                self.assertEqual(code, 2)
                self.assertIn("Objective must be", errors)
        with redirect_stderr(StringIO()), self.assertRaises(SystemExit) as stopped:
            monitor.main(["--database", str(self.database), "update", "api", "--objective", "1", "--no-objective"])
        self.assertEqual(stopped.exception.code, 2)
        self.assertEqual(monitor.get_target(self.connection, "api")["objective"], 50)
        # The table refuses a bad value even when it skips validation.
        with self.assertRaises(sqlite3.IntegrityError):
            self.connection.execute("UPDATE targets SET objective = 100")

    def test_older_databases_gain_an_empty_objective(self):
        self.connection.close()
        self.database.unlink()
        with sqlite3.connect(self.database) as legacy:
            legacy.execute("CREATE TABLE targets (name TEXT PRIMARY KEY, url TEXT NOT NULL, timeout REAL NOT NULL, "
                           "deadline REAL, alert_webhook TEXT NOT NULL DEFAULT 'default')")
            legacy.execute("INSERT INTO targets (name, url, timeout) VALUES ('api', 'https://example.com', 3)")
        legacy.close()
        self.connection = monitor.connect(self.database)
        self.assertIsNone(monitor.get_target(self.connection, "api")["objective"])
        self.assertEqual(monitor.update_target(self.connection, "api", objective=99.0)["objective"], 99.0)


class ObjectiveJudgingTests(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.database = Path(directory.name) / "checks.sqlite3"
        self.connection = monitor.connect(self.database)
        self.addCleanup(self.connection.close)

    def record(self, timeline, url=URL):
        for seconds, state in timeline:
            monitor.save(self.connection, monitor.Check(url, at(seconds), state, None, 1.0, None))

    def hour(self, down=(), skip=(), url=URL):
        """Check every 10 seconds for an hour, down at the seconds in `down`, missing those in `skip`."""
        self.record([(seconds, "down" if seconds in down else "up") for seconds in range(0, HOUR + 1, 10)
                     if seconds not in skip], url)

    def report(self, **options):
        return uptime.uptime_report(self.connection, until=at(HOUR), **{"hours": 1, "max_gap": 60, **options})

    def test_downtime_within_the_budget_meets_the_objective(self):
        monitor.add_target(self.connection, "api", URL, objective=99)
        self.hour(down={1200, 1210})
        row, = self.report()
        # 1% of an hour is 36 seconds; the incident was down 10 to 30 of them.
        self.assertEqual((row["objective_percent"], row["budget_seconds"]), (99, 36.0))
        self.assertEqual((row["min_downtime_seconds"], row["max_downtime_seconds"]), (10.0, 30.0))
        self.assertEqual((row["min_budget_left_seconds"], row["max_budget_left_seconds"]), (6.0, 26.0))
        self.assertEqual(row["objective_status"], "met")

    def test_downtime_past_the_budget_misses_the_objective(self):
        monitor.add_target(self.connection, "api", URL, objective=99)
        self.hour(down=set(range(1200, 1270, 10)))
        row, = self.report()
        self.assertEqual((row["min_downtime_seconds"], row["max_downtime_seconds"]), (60.0, 80.0))
        self.assertEqual((row["min_budget_left_seconds"], row["max_budget_left_seconds"]), (-44.0, -24.0))
        self.assertEqual(row["objective_status"], "missed")

    def test_a_gap_that_could_hide_too_much_downtime_is_uncertain(self):
        monitor.add_target(self.connection, "api", URL, objective=99)
        self.hour(skip=set(range(1010, 1100, 10)))
        row, = self.report()
        self.assertEqual((row["unobserved_seconds"], row["min_downtime_seconds"]), (100.0, 0.0))
        self.assertEqual(row["objective_status"], "uncertain")
        # Within the gap limit the same history is fully observed and meets it.
        self.assertEqual(self.report(max_gap=100)[0]["objective_status"], "met")

    def test_the_strictest_saved_objective_for_a_url_applies(self):
        monitor.add_target(self.connection, "api", URL, objective=99)
        monitor.add_target(self.connection, "api-strict", URL, objective=99.95)
        monitor.add_target(self.connection, "api-plain", URL)
        monitor.add_target(self.connection, "other", "https://other.example")
        self.hour(down={1200, 1210})
        self.hour(url="https://other.example")
        self.hour(url="https://removed.example")
        rows = {row["url"]: row for row in self.report()}
        self.assertEqual((rows[URL]["objective_percent"], rows[URL]["budget_seconds"]), (99.95, 1.8))
        self.assertEqual(rows[URL]["objective_status"], "missed")
        for url in ("https://other.example", "https://removed.example"):
            with self.subTest(url=url):
                self.assertEqual({key: rows[url][key] for key in (
                    "objective_percent", "budget_seconds", "min_budget_left_seconds", "max_budget_left_seconds",
                    "objective_status")}, dict.fromkeys((
                        "objective_percent", "budget_seconds", "min_budget_left_seconds",
                        "max_budget_left_seconds", "objective_status")))
        monitor.update_target(self.connection, "api-strict", url="https://moved.example")
        self.assertEqual(self.report(url=URL)[0]["objective_percent"], 99)

    def test_an_objective_given_to_the_report_replaces_saved_ones(self):
        monitor.add_target(self.connection, "api", URL, objective=99.95)
        self.hour(down={1200, 1210})
        self.hour(url="https://other.example")
        rows = self.report(objective=99)
        self.assertEqual([(row["url"], row["objective_percent"], row["objective_status"]) for row in rows],
                         [(URL, 99, "met"), ("https://other.example", 99, "met")])

    def test_status_keeps_only_matching_urls_and_bad_values_are_rejected(self):
        monitor.add_target(self.connection, "api", URL, objective=99)
        monitor.add_target(self.connection, "other", "https://other.example", objective=99)
        self.hour(down=set(range(1200, 1270, 10)))
        self.hour(url="https://other.example")
        self.hour(url="https://plain.example")
        self.assertEqual([row["url"] for row in self.report(status="missed")], [URL])
        self.assertEqual([row["url"] for row in self.report(status="met")], ["https://other.example"])
        self.assertEqual(self.report(status="uncertain"), [])
        for options in ({"status": "unknown"}, {"objective": 100}, {"objective": 0}, {"objective": float("nan")},
                        {"objective": True}):
            with self.subTest(options=options), self.assertRaises(ValueError):
                self.report(**options)

    def test_the_command_exits_1_when_a_reported_objective_was_missed(self):
        monitor.add_target(self.connection, "api", URL, objective=99)
        monitor.add_target(self.connection, "other", "https://other.example", objective=99)
        self.hour(down=set(range(1200, 1270, 10)))
        self.hour(url="https://other.example")
        base = ["--database", str(self.database), "uptime", "--hours", "1", "--until", at(HOUR), "--max-gap", "60"]
        for extra, code, urls in (([], 1, [URL, "https://other.example"]),
                                  (["--status", "met"], 0, ["https://other.example"]),
                                  (["--target", "other"], 0, ["https://other.example"]),
                                  (["--objective", "98"], 0, [URL, "https://other.example"])):
            with self.subTest(extra=extra):
                output = StringIO()
                with redirect_stdout(output):
                    self.assertEqual(monitor.main(base + extra), code)
                self.assertEqual([row["url"] for row in json.loads(output.getvalue())], urls)
        for extra in (["--objective", "100"], ["--objective", "inf"]):
            with self.subTest(extra=extra), redirect_stderr(StringIO()) as errors:
                self.assertEqual(monitor.main(base + extra), 2)
            self.assertIn("Objective must be", errors.getvalue())

    def test_random_histories_judge_consistently_with_the_bounds(self):
        rng = random.Random(11)
        statuses = set()
        for trial in range(40):
            with self.subTest(trial=trial):
                self.connection.execute("DELETE FROM checks")
                seconds, timeline = 0, []
                while seconds <= HOUR:
                    timeline.append((seconds, "down" if rng.random() < rng.choice((0.01, 0.05, 0.15)) else "up"))
                    seconds += rng.choice((10, 10, 10, 30, 30, 90))
                self.record(timeline)
                objective = rng.choice((90, 95, 99, 99.9))
                row, = self.report(objective=objective, max_gap=rng.choice((60, 120)))
                self.assertLessEqual(row["min_budget_left_seconds"], row["max_budget_left_seconds"])
                status = row["objective_status"]
                statuses.add(status)
                self.assertEqual(status == "met", row["min_budget_left_seconds"] >= 0)
                self.assertEqual(status == "missed", row["max_budget_left_seconds"] < 0)
                # The percentages tell the same story, give or take their rounding down.
                if status == "met":
                    self.assertGreaterEqual(row["min_uptime_percent"], objective - 0.001)
                if status == "missed":
                    self.assertLess(row["max_uptime_percent"], objective)
        self.assertEqual(statuses, set(uptime.STATUSES))

    def test_the_api_judges_objectives_and_lists_them_on_targets(self):
        monitor.add_target(self.connection, "api", URL, objective=99)
        self.hour(down=set(range(1200, 1270, 10)))
        server = api.make_server(self.database, "127.0.0.1", 0, monitor)
        thread = Thread(target=server.serve_forever, daemon=True)
        thread.start()
        self.addCleanup(server.server_close)
        self.addCleanup(thread.join)
        self.addCleanup(server.shutdown)
        base = f"http://127.0.0.1:{server.server_port}/api"

        def get(path):
            try:
                with urlopen(base + path, timeout=5) as response:
                    return response.status, json.loads(response.read())
            except HTTPError as response:
                with response:
                    return response.code, json.loads(response.read())

        window = f"hours=1&max_gap=60&until={quote(at(HOUR), safe='')}"
        self.assertEqual(get(f"/uptime?{window}&status=missed"), (200, self.report(status="missed")))
        self.assertEqual(get(f"/uptime?{window}&objective=98.5")[1], self.report(objective=98.5))
        self.assertEqual(get("/targets")[1][0]["objective"], 99)
        for query, message in (("objective=abc", "objective must be a decimal number"),
                               ("objective=-1", "objective must be a decimal number"),
                               ("objective=100", "Objective must be"), ("status=late", "Status must be")):
            with self.subTest(query=query):
                status, body = get(f"/uptime?{query}")
                self.assertEqual(status, 400)
                self.assertIn(message, body["error"])


def at(seconds):
    return (START + timedelta(seconds=seconds)).isoformat()


if __name__ == "__main__":
    unittest.main()
