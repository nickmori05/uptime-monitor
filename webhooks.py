"""Track each alert webhook's health: its last success, its last failure, and the alerts queued for it.

Routes are named as in `routing`: `default` for the watcher's own `--alert-webhook`,
or a target's webhook URL. Every delivery attempt is saved in the `webhooks` table,
and the queue is worked out from the alerts still pending, so the report needs no
running watcher.
"""

from datetime import datetime, timezone
import sqlite3

import routing


def now() -> datetime:
    """Return the current UTC time. The reliability scenarios replace it with a clock that follows their rounds."""
    return datetime.now(timezone.utc)


def note(connection: sqlite3.Connection, route: str, error: str | None) -> None:
    """Save the outcome of one delivery attempt to `route`: None when it was accepted, else its error code."""
    at = now().isoformat()
    with connection:
        if error is None:
            connection.execute(
                """INSERT INTO webhooks (route, last_success_at, failures) VALUES (?, ?, 0)
                   ON CONFLICT(route) DO UPDATE SET last_success_at = excluded.last_success_at, failures = 0""",
                (route, at))
        else:
            connection.execute(
                """INSERT INTO webhooks (route, last_failure_at, last_error, failures) VALUES (?, ?, ?, 1)
                   ON CONFLICT(route) DO UPDATE SET last_failure_at = excluded.last_failure_at,
                   last_error = excluded.last_error, failures = webhooks.failures + 1""",
                (route, at, error))


def report(connection: sqlite3.Connection, unsent: list[dict]) -> list[dict]:
    """Describe every route that a target names, that was ever tried, or that alerts are queued for.

    `unsent` is `alerts.unsent_alerts`: the pending alerts, each with the routes it
    still needs. The default route comes first, then webhooks by URL.
    """
    targets = {}
    for row in connection.execute("SELECT name, alert_webhook FROM targets ORDER BY name"):
        if row["alert_webhook"] != routing.NONE:
            targets.setdefault(row["alert_webhook"], []).append(row["name"])
    queues = {}
    for body in unsent:
        incident = body["incident"]
        happened = incident["confirmed_at"] if body["event"] == "opened" else incident["resolved_at"]
        for route in body["routes"]:
            queues.setdefault(route, []).append(happened)
    saved = {row["route"]: dict(row) for row in connection.execute("SELECT * FROM webhooks")}
    rows = []
    for route in sorted(set(targets) | set(queues) | set(saved), key=lambda route: (route != routing.DEFAULT, route)):
        health = saved.get(route, {})
        failures = health.get("failures", 0)
        queue = queues.get(route, [])
        rows.append({
            "route": route,
            "targets": targets.get(route, []),
            "state": "failing" if failures else "ok" if health.get("last_success_at") else "untried",
            "pending": len(queue),
            "oldest_pending_at": min(queue, default=None),
            "last_success_at": health.get("last_success_at"),
            "last_failure_at": health.get("last_failure_at"),
            "last_error": health.get("last_error"),
            "consecutive_failures": failures,
        })
    return rows
