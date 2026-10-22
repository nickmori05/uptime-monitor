"""Seeded reliability scenarios: run the real monitor against injected faults and measure what it saved and sent.

Each scenario returns a JSON-ready report whose `ok` is true only when every
measured guarantee held. Expected incident events come from `oracle`, written
separately from `incidents.py`, so a bug there shows up as a mismatch here.
"""

from collections import Counter
from contextlib import closing, redirect_stderr, redirect_stdout
import io
from pathlib import Path
import random
import socket
import tempfile
import time
from unittest.mock import patch

from faults import EXPECTED, Clock, Faults, Receiver, resolver
import monitor

SLACK_SECONDS = 0.5


def oracle(states: list[str], failures: int = 2, recoveries: int = 2) -> list[tuple[str, int, int]]:
    """Derive (event, first_failure_index, index) from one URL's states, without using incidents.py."""
    events, opened_from, first, downs, ups = [], None, 0, 0, 0
    for index, state in enumerate(states):
        if opened_from is None:
            first = index if downs == 0 else first
            downs = downs + 1 if state == "down" else 0
            if downs == failures:
                events.append(("opened", first, index))
                opened_from, downs, ups = first, 0, 0
        elif state == "up":
            ups += 1
            if ups == recoveries:
                events.append(("resolved", opened_from, index))
                opened_from = None
        else:
            ups = 0
    return events


def markov(rng: random.Random, rounds: int, fail: float, recover: float) -> list[str]:
    """Return up/down states where an outage starts with chance `fail` and ends with chance `recover` each round."""
    states, down = [], False
    for _ in range(rounds):
        down = rng.random() >= recover if down else rng.random() < fail
        states.append("down" if down else "up")
    return states


def saved_states(connection) -> dict[str, list[dict]]:
    """Group saved checks by URL in the order they were saved."""
    grouped = {}
    for row in connection.execute("SELECT id, url, state, error, latency_ms FROM checks ORDER BY id"):
        grouped.setdefault(row["url"], []).append(dict(row))
    return grouped


def probe_faults(seed: int = 1, rounds: int = 12, timeout: float = 0.4, deadline: float = 0.6) -> dict:
    """Check one target per fault kind each round and measure how each result was classified and how long it took."""
    rng = random.Random(seed)
    kinds = sorted(EXPECTED)
    targets = [f"t{index}" for index in range(len(kinds))]
    # Round 0 gives every target a different fault, so each kind is measured at least once.
    plan = [dict(zip(targets, rng.sample(kinds, len(kinds)), strict=True))]
    plan += [{target: rng.choice(kinds) for target in targets} for _ in range(rounds - 1)]
    clock = Clock()

    def schedule(current, target):
        return plan[min(current, rounds - 1)].get(target, "ok")

    server = Faults(clock, schedule)
    round_seconds = []
    try:
        with tempfile.TemporaryDirectory() as directory, \
                closing(monitor.connect(Path(directory) / "checks.sqlite3")) as connection:
            for target in targets:
                url = f"http://{target}.reliability.test:{server.server_port}/{target}"
                monitor.add_target(connection, target, url, timeout, deadline)
            started = [time.perf_counter()]

            def next_round(_seconds):
                round_seconds.append(time.perf_counter() - started[0])
                clock.round += 1
                started[0] = time.perf_counter()

            with patch.object(monitor, "sleep", side_effect=next_round), \
                    patch.object(socket, "getaddrinfo", resolver(clock, schedule)):
                for _batch in monitor.watch_targets(connection, interval=1, count=rounds, workers=len(targets)):
                    pass
            round_seconds.append(time.perf_counter() - started[0])
            saved = saved_states(connection)
    finally:
        server.close()
    mismatches, over_limit = [], []
    for target in targets:
        checks = next(rows for url, rows in saved.items() if url.endswith(f"/{target}"))
        for current, check in enumerate(checks):
            fault = plan[current][target]
            if (check["state"], check["error"]) != EXPECTED[fault]:
                mismatches.append({"round": current, "fault": fault, "saved": [check["state"], check["error"]]})
            limit = timeout if fault == "slow" else deadline
            over_limit.append(check["latency_ms"] - limit * 1000)
    checks = sum(len(rows) for rows in saved.values())
    report = {
        "scenario": "probe faults", "seed": seed, "rounds": rounds, "checks": checks,
        "faults": dict(sorted(Counter(fault for row in plan for fault in row.values()).items())),
        "misclassified": len(mismatches), "mismatches": mismatches[:5],
        "max_over_limit_ms": round(max(over_limit), 1),
        "max_round_seconds": round(max(round_seconds), 3), "deadline_seconds": deadline,
    }
    report["ok"] = (checks == rounds * len(targets) and not mismatches
                    and report["max_over_limit_ms"] < SLACK_SECONDS * 1000
                    and report["max_round_seconds"] < deadline + SLACK_SECONDS)
    return report


def alert_delivery(seed: int = 1, rounds: int = 40, targets: int = 3, skip_resolved: bool = False) -> dict:
    """Run outages, webhook failures, restarts, and checks with no alerting watcher; measure each alert's delivery.

    With `skip_resolved`, the watcher runs with `--skip-resolved`, so incidents that
    resolve before their opening can be sent are expected to arrive not at all.
    """
    rng = random.Random(seed)
    names = [f"b{index}" for index in range(targets)]
    states = {name: markov(rng, rounds, 0.2, 0.4) + ["up", "up"] for name in names}
    webhook = [rng.choices(("up", "error", "reset"), (0.75, 0.15, 0.1))[0] for _ in range(rounds)] + ["up", "up"]
    # Alternate watcher runs (restarting each time) with stretches checked by `run-all` alone.
    segments, start = [], 0
    while start < rounds:
        length = min(rng.randint(1, 6), rounds - start)
        segments.append(("watch" if not segments or rng.random() < 0.7 else "run-all", start, length))
        start += length
    segments.append(("watch", rounds, 2))
    alerting = [kind == "watch" for kind, _, length in segments for _ in range(length)]
    # A watcher sends missed alerts as it starts, before that round's checks; otherwise after them.
    starts = {start for kind, start, _ in segments if kind == "watch"}
    options = ["--skip-resolved"] if skip_resolved else []
    clock = Clock()
    server = Faults(clock, lambda current, name: "ok" if states[name][current] == "up" else "error")
    receiver = Receiver(clock, lambda current: webhook[current])
    try:
        with tempfile.TemporaryDirectory() as directory:
            database = str(Path(directory) / "checks.sqlite3")
            with closing(monitor.connect(Path(database))) as connection:
                for name in names:
                    monitor.add_target(connection, name, f"http://127.0.0.1:{server.server_port}/{name}", 2)

            def cli(*arguments):
                with redirect_stdout(io.StringIO()), redirect_stderr(io.StringIO()):
                    monitor.main(["--database", database, *arguments])

            with patch.object(monitor, "sleep", side_effect=lambda _seconds: setattr(clock, "round", clock.round + 1)):
                for kind, _, length in segments:
                    if kind == "watch":
                        cli("watch", "--count", str(length), "--workers", "4", "--alert-webhook",
                            f"http://127.0.0.1:{receiver.server_port}/alert", *options)
                        clock.round += 1
                    else:
                        for _ in range(length):
                            cli("run-all", "--workers", "4")
                            clock.round += 1
            with closing(monitor.connect(Path(database))) as connection:
                saved = saved_states(connection)
    finally:
        server.close()
        receiver.close()
    rounds_by_id = {row["id"]: index for rows in saved.values() for index, row in enumerate(rows)}
    expected, skipped = {}, 0
    for name in names:
        url = f"http://127.0.0.1:{server.server_port}/{name}"
        opened_due = {}
        for event, first, index in oracle(states[name]):
            due = next(r for r in range(index, len(alerting)) if alerting[r] and webhook[r] == "up")
            if event == "opened":
                opened_due[first] = due
            elif skip_resolved:
                # Skipped when the resolution is saved before the opening is sent.
                sent_at = opened_due[first]
                if index < sent_at or (index == sent_at and sent_at not in starts):
                    del expected[(url, "opened", first)]
                    skipped += 1
                    continue
            expected[(url, event, first)] = (index, due)
    delivered = [((body["url"], body["event"], rounds_by_id[body["incident"]["first_failure_id"]]), arrived)
                 for body, arrived, accepted, _key in receiver.received if accepted]
    copies = Counter(key for key, _ in delivered)
    arrivals = {key: arrived for key, arrived in reversed(delivered)}
    order = [key for key, _ in delivered]
    report = {
        "scenario": "alert delivery" + (" (skip resolved)" if skip_resolved else ""), "seed": seed,
        "rounds": rounds + 2, "targets": targets,
        "watcher_starts": sum(kind == "watch" for kind, _, _ in segments),
        "rounds_without_alerting": alerting.count(False),
        "rounds_with_webhook_down": sum(mode != "up" for mode in webhook),
        "skipped_incidents": skipped, "expected_alerts": len(expected), "delivered": len(copies),
        "lost": len(set(expected) - set(copies)), "unexpected": len(set(copies) - set(expected)),
        "duplicates": sum(count - 1 for count in copies.values()),
        "out_of_order": sum(1 for a, b in zip(order, order[1:], strict=False)
                            if a in expected and b in expected and expected[a][0] > expected[b][0]),
        "not_at_first_chance": sum(1 for key, (_, due) in expected.items() if arrivals.get(key, due) != due),
        "max_delay_rounds": max((arrivals[key] - index for key, (index, _) in expected.items() if key in arrivals),
                                default=0),
        **receiver.deduplicated(),
    }
    report["ok"] = not any(report[name] for name in ("lost", "unexpected", "duplicates", "out_of_order",
                                                      "not_at_first_chance", "repeated_after_dedupe",
                                                      "key_collisions", "header_mismatches"))
    return report
