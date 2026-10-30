"""Track each alert webhook's health: its last success, its last failure, and the alerts queued for it.

Routes are named as in `routing`: `default` for the watcher's own `--alert-webhook`,
or a target's webhook URL. Every delivery attempt is saved in the `webhooks` table,
and the queue is worked out from the alerts still pending, so the report needs no
running watcher. A watcher with a `Backoff` also saves when a failing webhook may
be tried again, and leaves it alone until then.
"""

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
import math
import sqlite3

import routing

MAX_BACKOFF = 86400


@dataclass(frozen=True)
class Backoff:
    """Wait `first` seconds after a webhook's first failure in a row, doubling after each further one, up to `limit`."""
    first: float
    limit: float

    def __post_init__(self):
        for value in (self.first, self.limit):
            if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value):
                raise ValueError("Backoff must be a number of seconds.")
        if self.first <= 0:
            raise ValueError("The first backoff must be greater than 0 seconds.")
        if not self.first <= self.limit <= MAX_BACKOFF:
            raise ValueError(f"--max-backoff must be at least the interval and at most {MAX_BACKOFF} seconds.")

    def delay(self, failures: int) -> float:
        """Return how long to wait after `failures` failed attempts in a row."""
        # The exponent is capped so a long outage can't overflow; 2**32 intervals is past any limit.
        return min(self.limit, self.first * 2 ** min(failures - 1, 32))


def now() -> datetime:
    """Return the current UTC time. The reliability scenarios replace it with a clock that follows their rounds."""
    return datetime.now(timezone.utc)


def note(connection: sqlite3.Connection, route: str, error: str | None,
         backoff: Backoff | None = None) -> datetime | None:
    """Save the outcome of one delivery attempt to `route`: None when it was accepted, else its error code.

    With `backoff`, a failure also saves when the route may be tried again, which is returned.
    Otherwise the route is tried again next round, and None is returned.
    """
    current = now()
    with connection:
        if error is None:
            connection.execute(
                """INSERT INTO webhooks (route, last_success_at, failures) VALUES (?, ?, 0)
                   ON CONFLICT(route) DO UPDATE SET last_success_at = excluded.last_success_at, failures = 0,
                   retry_at = NULL""",
                (route, current.isoformat()))
            return None
        connection.execute(
            """INSERT INTO webhooks (route, last_failure_at, last_error, failures) VALUES (?, ?, ?, 1)
               ON CONFLICT(route) DO UPDATE SET last_failure_at = excluded.last_failure_at,
               last_error = excluded.last_error, failures = webhooks.failures + 1""",
            (route, current.isoformat(), error))
        failures = connection.execute("SELECT failures FROM webhooks WHERE route = ?", (route,)).fetchone()[0]
        retry_at = None if backoff is None else current + timedelta(seconds=backoff.delay(failures))
        connection.execute("UPDATE webhooks SET retry_at = ? WHERE route = ?",
                           (None if retry_at is None else retry_at.isoformat(), route))
    return retry_at


def backing_off(connection: sqlite3.Connection) -> set[str]:
    """Return the routes that failed recently enough that a watcher with a backoff should leave them alone."""
    current = now()
    rows = connection.execute("SELECT route, retry_at FROM webhooks WHERE retry_at IS NOT NULL")
    return {row["route"] for row in rows if datetime.fromisoformat(row["retry_at"]) > current}


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
            "retry_at": health.get("retry_at"),
        })
    return rows
