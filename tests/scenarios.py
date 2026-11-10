"""Seeded reliability scenarios: run the real monitor against injected faults and measure what it saved and sent.

Each scenario returns a JSON-ready report whose `ok` is true only when every
measured guarantee held. Expected incident events come from `oracle`, written
separately from `incidents.py`, so a bug there shows up as a mismatch here.
"""

from collections import Counter
from contextlib import closing, redirect_stderr, redirect_stdout
from datetime import datetime, timedelta, timezone
import io
from pathlib import Path
import random
import socket
import tempfile
import time
from unittest.mock import patch

import alerts
from faults import EXPECTED, Clock, Faults, Receiver, resolver
import monitor
import webhooks

SLACK_SECONDS = 0.5
# The alert scenarios' watchers save webhook health against a clock that advances one interval per round.
EPOCH = datetime(2026, 1, 1, tzinfo=timezone.utc)
INTERVAL = 30
ERRORS = {"error": "http_500", "reset": "connection_error"}


def at(current: int) -> datetime:
    """Return the time the alert scenarios' clock shows during a round."""
    return EPOCH + timedelta(seconds=INTERVAL * current)


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


def outage_plan(rng: random.Random, rounds: int) -> list[str]:
    """Return webhook modes where it fails in outages lasting several rounds, as a receiver that's down does."""
    return ["up" if state == "up" else rng.choice(("error", "reset")) for state in markov(rng, rounds, 0.06, 0.15)]


def retry_model(ready: dict, plan: list[str], alerting: list[bool], starts: set[int], limit_rounds: float) -> tuple:
    """Replay one webhook's delivery attempts round by round, written separately from alerts.py.

    `ready` maps each alert owed to the webhook to the round whose checks produced it.
    A starting watcher tries the webhook straight away with alerts from earlier rounds,
    whatever its backoff. After each round's checks it tries again with alerts up to
    that round, unless it is still waiting: after `failures` failures in a row it waits
    min(`limit_rounds`, 2 ** (failures - 1)) rounds, so 0 means it never waits. Each
    alert is its own POST, and a failure ends the attempt. Return the round each alert
    arrives in and the number of POSTs.
    """
    arrived, attempts, failures, retry = {}, 0, 0, 0
    for current, watching in enumerate(alerting):
        for starting in (True, False):
            if not watching or (starting and current not in starts):
                continue
            waiting = [key for key, index in ready.items()
                       if key not in arrived and (index < current if starting else index <= current)]
            if not waiting or (not starting and current < retry):
                continue
            if plan[current] == "up":
                attempts += len(waiting)
                arrived.update(dict.fromkeys(waiting, current))
                failures, retry = 0, 0
            else:
                attempts += 1
                failures += 1
                retry = current + min(limit_rounds, 2 ** (failures - 1))
    return arrived, attempts


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


def alert_delivery(seed: int = 1, rounds: int = 40, targets: int = 3, skip_resolved: bool = False,
                   routed: bool = False, outages: bool = False, max_backoff: int | None = None) -> dict:
    """Run outages, webhook failures, restarts, and checks with no alerting watcher; measure each alert's delivery.

    With `skip_resolved`, the watcher runs with `--skip-resolved`, so incidents that
    resolve before their opening can be sent are expected to arrive not at all. With
    `routed`, the first target alerts the watcher's webhook, the second its own
    webhook (which fails on a separate schedule), and the rest have alerts off. With
    `outages`, webhooks fail in outages of several rounds instead of single rounds.
    With `max_backoff`, the watcher runs with `--max-backoff`, and when each alert
    is due comes from `retry_model`.
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
    # Which receiver each target's alerts should reach, or None when its alerts are off.
    route_of = {name: "main" if not routed or index == 0 else "own" if index == 1 else None
                for index, name in enumerate(names)}
    schedules = {"main": webhook}
    if routed:
        other = random.Random(f"{seed}-own")
        schedules["own"] = [other.choices(("up", "error", "reset"), (0.6, 0.25, 0.15))[0]
                            for _ in range(rounds)] + ["up", "up"]
    if outages:
        # A separate generator, so the outages, segments, and other runs' schedules stay the same for a seed.
        down = random.Random(f"{seed}-outages")
        schedules = {label: outage_plan(down, rounds) + ["up", "up"] for label in schedules}
    if max_backoff is not None:
        options += ["--interval", str(INTERVAL), "--max-backoff", str(max_backoff)]
    clock = Clock()
    server = Faults(clock, lambda current, name: "ok" if states[name][current] == "up" else "error")
    receivers = {label: Receiver(clock, lambda current, plan=plan: plan[current]) for label, plan in schedules.items()}
    try:
        with tempfile.TemporaryDirectory() as directory:
            database = str(Path(directory) / "checks.sqlite3")
            with closing(monitor.connect(Path(database))) as connection:
                for name in names:
                    setting = {"main": "default", None: "none"}.get(route_of[name])
                    if setting is None:
                        setting = f"http://127.0.0.1:{receivers['own'].server_port}/own"
                    monitor.add_target(connection, name, f"http://127.0.0.1:{server.server_port}/{name}", 2,
                                       alert_webhook=setting)

            def cli(*arguments):
                with redirect_stdout(io.StringIO()), redirect_stderr(io.StringIO()):
                    monitor.main(["--database", database, *arguments])

            def next_round(_seconds):
                clock.round += 1

            with patch.object(monitor, "sleep", side_effect=next_round), \
                    patch.object(webhooks, "now", side_effect=lambda: at(clock.round)):
                for kind, _, length in segments:
                    if kind == "watch":
                        cli("watch", "--count", str(length), "--workers", "4", "--alert-webhook",
                            f"http://127.0.0.1:{receivers['main'].server_port}/alert", *options)
                        clock.round += 1
                    else:
                        for _ in range(length):
                            cli("run-all", "--workers", "4")
                            clock.round += 1
            with closing(monitor.connect(Path(database))) as connection:
                saved = saved_states(connection)
                health = {row["route"]: row for row in alerts.webhook_health(connection)}
    finally:
        server.close()
        for receiver in receivers.values():
            receiver.close()
    rounds_by_id = {row["id"]: index for rows in saved.values() for index, row in enumerate(rows)}
    wanted = {f"http://127.0.0.1:{server.server_port}/{name}": route_of[name] for name in names}
    expected, skipped = {}, 0
    for name in names:
        url = f"http://127.0.0.1:{server.server_port}/{name}"
        if route_of[name] is None:
            continue
        plan = schedules[route_of[name]]
        opened_due = {}
        for event, first, index in oracle(states[name]):
            due = next(r for r in range(index, len(alerting)) if alerting[r] and plan[r] == "up")
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
    # The model also counts the attempts each webhook should get. Skipped incidents can fail
    # attempts before they're skipped, which it doesn't replay, so it's left out for those runs.
    expected_attempts = None if skip_resolved else 0
    first_chance = {key: due for key, (_, due) in expected.items()}
    for label, plan in schedules.items():
        if skip_resolved:
            break
        ready = {key: index for key, (index, _) in expected.items() if wanted[key[0]] == label}
        arrived, attempts = retry_model(ready, plan, alerting, starts, (max_backoff or 0) / INTERVAL)
        expected_attempts += attempts
        if max_backoff is not None:
            # Backoff holds alerts past the first round their webhook is up; the model says until when.
            expected.update({key: (index, arrived.get(key)) for key, index in ready.items()})
    delivered = [(label, (body["url"], body["event"], rounds_by_id[body["incident"]["first_failure_id"]]), arrived)
                 for label, receiver in receivers.items()
                 for body, arrived, accepted, _key in receiver.received if accepted]
    copies = Counter(key for _, key, _ in delivered)
    arrivals = {key: arrived for _, key, arrived in reversed(delivered)}
    # Order only matters on each route; a failing webhook may fall behind the others.
    orders = [[key for label, key, _ in delivered if label == route] for route in receivers]
    deduplicated = [receiver.deduplicated() for receiver in receivers.values()]
    routes = {"main": "default", "own": f"http://127.0.0.1:{receivers['own'].server_port}/own" if routed else None}
    health_mismatches = []
    for label, receiver in receivers.items():
        # Each POST the receiver read is one delivery attempt, so its log says what the report should show.
        attempts = [(arrived, accepted, schedules[label][arrived]) for _, arrived, accepted, _ in receiver.received]
        successes = [arrived for arrived, accepted, _ in attempts if accepted]
        failures = [(arrived, mode) for arrived, accepted, mode in attempts if not accepted]
        trailing = len(attempts) - 1 - max((index for index, (_, accepted, _) in enumerate(attempts) if accepted),
                                           default=-1)
        expected_health = {
            "last_success_at": at(successes[-1]).isoformat() if successes else None,
            "last_failure_at": at(failures[-1][0]).isoformat() if failures else None,
            "last_error": ERRORS[failures[-1][1]] if failures else None,
            "consecutive_failures": trailing,
            "pending": sum(1 for key in expected if wanted[key[0]] == label and key not in arrivals),
        }
        reported = health.get(routes[label], {})
        health_mismatches += [f"{label}.{field}" for field, value in expected_health.items()
                              if reported.get(field) != value]
    report = {
        "scenario": "alert delivery" + (" (skip resolved)" if skip_resolved else "") + (" (routed)" if routed else "")
                    + (" (outages)" if outages else "") + (f" (backoff {max_backoff} s)" if max_backoff else ""),
        "seed": seed, "rounds": rounds + 2, "targets": targets,
        "watcher_starts": sum(kind == "watch" for kind, _, _ in segments),
        "rounds_without_alerting": alerting.count(False),
        **{("rounds_with_webhook_down" if label == "main" else f"rounds_with_{label}_webhook_down"):
           sum(mode != "up" for mode in plan) for label, plan in schedules.items()},
        "skipped_incidents": skipped, "expected_alerts": len(expected), "delivered": len(copies),
        "lost": len(set(expected) - set(copies)), "unexpected": len(set(copies) - set(expected)),
        "misrouted": sum(1 for label, key, _ in delivered if wanted[key[0]] != label),
        "duplicates": sum(count - 1 for count in copies.values()),
        "out_of_order": sum(1 for order in orders for a, b in zip(order, order[1:], strict=False)
                            if a in expected and b in expected and expected[a][0] > expected[b][0]),
        "not_at_first_chance": sum(1 for key, (_, due) in expected.items() if arrivals.get(key, due) != due),
        "max_delay_rounds": max((arrivals[key] - index for key, (index, _) in expected.items() if key in arrivals),
                                default=0),
        **{name: sum(counts[name] for counts in deduplicated) for name in deduplicated[0]},
        # How many rounds past its webhook's first good round an alert waited, when the backoff held it.
        "max_held_by_backoff_rounds": max((arrivals[key] - first_chance[key] for key in expected
                                           if key in arrivals and first_chance.get(key) is not None), default=0)
                                      if max_backoff is not None else 0,
        "attempts": sum(len(receiver.received) for receiver in receivers.values()),
        "failed_attempts": sum(not accepted for receiver in receivers.values()
                               for _, _, accepted, _ in receiver.received),
        "expected_attempts": expected_attempts,
        "health_mismatches": health_mismatches,
    }
    report["ok"] = not any(report[name] for name in ("lost", "unexpected", "misrouted", "duplicates", "out_of_order",
                                                      "not_at_first_chance", "repeated_after_dedupe",
                                                      "key_collisions", "header_mismatches", "health_mismatches"))
    if expected_attempts is not None:
        report["ok"] = report["ok"] and report["attempts"] == expected_attempts
    return report
