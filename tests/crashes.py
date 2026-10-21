"""Kill the watcher process with SIGKILL at random moments and measure what survives.

Every round is a separate `watch --count 1 --alert-webhook` process, so each one
also exercises startup catch-up. Expected alerts are derived from the checks that
were actually saved, because a killed round may not have saved its own.
"""

from collections import Counter
from contextlib import closing
from pathlib import Path
import random
import sqlite3
import subprocess
import sys
import tempfile
import time

from faults import Clock, Faults, Receiver
import monitor
from scenarios import oracle, saved_states

MONITOR = Path(__file__).resolve().parents[1] / "monitor.py"
RECEIVER_DELAY = 0.03


def crash_recovery(seed: int = 1, rounds: int = 12, kill_rate: float = 0.5) -> dict:
    """Run watcher rounds, SIGKILL some of them part-way, then measure lost and repeated alerts.

    Kills land at a random point within the longest unkilled round so far, so they
    hit startup, checking, saving, and delivery in rough proportion on any machine.
    """
    rng = random.Random(seed)
    # Three-round outages, offset per target, still open an incident when one of their rounds is killed.
    # Round 0 is never killed so alerting is enabled before any incident; the last two rounds drain.
    states = {name: ["up"] + ["down" if (current + offset) // 3 % 2 == 0 else "up" for current in range(rounds)]
              + ["up", "up"] for name, offset in (("c0", 0), ("c1", 2))}
    names = sorted(states)
    total = len(states["c0"])
    clock = Clock()
    server = Faults(clock, lambda current, name: "ok" if states[name][min(current, total - 1)] == "up" else "error")
    receiver = Receiver(clock, lambda _current: "up")
    # Holding each alert briefly widens the window between the receiver accepting it and the watcher saving that.
    receiver.before_reply = lambda _body: time.sleep(RECEIVER_DELAY)
    kills, window = 0, 0.0
    try:
        with tempfile.TemporaryDirectory() as directory:
            database = Path(directory) / "checks.sqlite3"
            with closing(monitor.connect(database)) as connection:
                for name in names:
                    monitor.add_target(connection, name, f"http://127.0.0.1:{server.server_port}/{name}", 2)
            command = [sys.executable, str(MONITOR), "--database", str(database), "watch", "--count", "1",
                       "--alert-webhook", f"http://127.0.0.1:{receiver.server_port}/alert"]
            for current in range(total):
                clock.round = current
                started = time.perf_counter()
                process = subprocess.Popen(command, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
                if 0 < current < total - 2 and rng.random() < kill_rate:
                    time.sleep(rng.uniform(0, window))
                    if process.poll() is None:
                        process.kill()
                        kills += 1
                process.wait(timeout=30)
                if process.returncode >= 0:
                    window = max(window, time.perf_counter() - started)
            with closing(sqlite3.connect(database)) as raw:
                integrity = raw.execute("PRAGMA integrity_check").fetchone()[0]
            with closing(monitor.connect(database)) as connection:
                saved = saved_states(connection)
                recorded = {(row["url"], row["event"], row["first_failure_id"]) for row in connection.execute(
                    "SELECT url, event, first_failure_id FROM alerts WHERE status = 'sent'")}
    finally:
        server.close()
        receiver.close()
    expected = set()
    for url, rows in saved.items():
        for event, first, _index in oracle([row["state"] for row in rows]):
            expected.add((url, event, rows[first]["id"]))
    copies = Counter((body["url"], body["event"], body["incident"]["first_failure_id"])
                     for body in receiver.accepted())
    report = {
        "scenario": "crash recovery", "seed": seed, "rounds": total, "kills": kills,
        "kill_window_seconds": round(window, 3),
        "checks_saved": sum(len(rows) for rows in saved.values()), "integrity": integrity,
        "expected_alerts": len(expected), "delivered": len(copies),
        "lost": len(expected - set(copies)), "unexpected": len(set(copies) - expected),
        "repeated_after_kill": sum(count - 1 for count in copies.values()),
        "not_recorded_as_sent": len(expected - recorded),
        **receiver.deduplicated(),
    }
    # A kill after the receiver accepts an alert but before it is saved means it is sent again,
    # with the same key, so a receiver that drops repeated keys sees each alert once.
    report["ok"] = (integrity == "ok" and not report["lost"] and not report["unexpected"]
                    and not report["not_recorded_as_sent"] and report["repeated_after_kill"] <= kills
                    and not any(report[name] for name in ("repeated_after_dedupe", "key_collisions",
                                                          "header_mismatches")))
    return report


def kill_during_delivery() -> dict:
    """SIGKILL a watcher after the receiver reads an alert and before it answers, then restart it once."""
    clock = Clock()
    server = Faults(clock, lambda _current, _name: "error")
    receiver = Receiver(clock, lambda _current: "up")
    processes = []

    def kill_watcher(_body):
        if clock.round == 1:
            processes[-1].kill()
            processes[-1].wait(timeout=30)

    receiver.before_reply = kill_watcher
    try:
        with tempfile.TemporaryDirectory() as directory:
            database = Path(directory) / "checks.sqlite3"
            with closing(monitor.connect(database)) as connection:
                monitor.add_target(connection, "down", f"http://127.0.0.1:{server.server_port}/down", 2)
            command = [sys.executable, str(MONITOR), "--database", str(database), "watch", "--count", "1",
                       "--alert-webhook", f"http://127.0.0.1:{receiver.server_port}/alert"]
            # Round 0 enables alerting, round 1 opens the incident and is killed mid-delivery, round 2 recovers.
            for current in range(3):
                clock.round = current
                processes.append(subprocess.Popen(command, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL))
                processes[-1].wait(timeout=30)
            with closing(monitor.connect(database)) as connection:
                sent = connection.execute("SELECT COUNT(*) FROM alerts WHERE status = 'sent'").fetchone()[0]
                checks = len(monitor.history(connection))
    finally:
        server.close()
        receiver.close()
    copies = [body["event"] for body in receiver.accepted()]
    report = {"scenario": "kill during delivery", "killed": processes[1].returncode < 0, "checks_saved": checks,
              "copies_received": len(copies), "recorded_sent": sent,
              "distinct_keys": len({body["idempotency_key"] for body in receiver.accepted()}),
              **receiver.deduplicated()}
    report["ok"] = (report["killed"] and checks == 3 and copies == ["opened", "opened"] and sent == 1
                    and report["distinct_keys"] == 1 and not report["header_mismatches"])
    return report
