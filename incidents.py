"""Rebuild incidents (runs of consecutive failures) from saved observations."""

from contextlib import closing
from datetime import datetime
from heapq import nlargest
import sqlite3


def list_incidents(
    connection: sqlite3.Connection,
    *,
    url: str | None = None,
    state: str | None = None,
    limit: int = 20,
    failures: int = 2,
    recoveries: int = 2,
) -> list[dict]:
    """Return incidents newest first, optionally filtered by URL and state."""
    for name, value, maximum in (
        ("Limit", limit, 1000), ("Failures", failures, 100), ("Recoveries", recoveries, 100)
    ):
        if type(value) is not int or not 1 <= value <= maximum:
            raise ValueError(f"{name} must be between 1 and {maximum}.")
    if state not in (None, "open", "resolved"):
        raise ValueError("State must be open or resolved.")
    return nlargest(
        limit,
        (incident for incident in scan_incidents(connection, url=url, failures=failures, recoveries=recoveries)
         if state is None or incident["state"] == state),
        key=lambda incident: incident["first_failure_id"],
    )


def scan_incidents(connection: sqlite3.Connection, *, url: str | None = None,
                   failures: int = 2, recoveries: int = 2) -> list[dict]:
    """Rebuild every incident from saved checks, in no particular order."""
    for name, value in (("Failures", failures), ("Recoveries", recoveries)):
        if type(value) is not int or not 1 <= value <= 100:
            raise ValueError(f"{name} must be between 1 and 100.")
    query = "SELECT id, url, checked_at, state FROM checks"
    parameters = ()
    if url is not None:
        query += " WHERE url = ?"
        parameters = (url,)
    query += " ORDER BY id"
    with closing(connection.execute(query, parameters)) as rows:
        return list(_episodes(rows, failures, recoveries))


def seconds_between(start: str | None, end: str | None) -> float | None:
    """Return the seconds from one saved timestamp to another, or None if either is missing or unreadable.

    Checks are ordered by row ID, so a clock change can make a later check look
    earlier; a negative span is reported as 0.
    """
    if start is None or end is None:
        return None
    try:
        span = (datetime.fromisoformat(end) - datetime.fromisoformat(start)).total_seconds()
    except (TypeError, ValueError):
        return None
    return round(max(span, 0.0), 3)


def _bound(incident: dict) -> dict:
    """Add the shortest and longest outage the incident's checks allow.

    The outage covers at least the first to the last failed check. It began after
    the last success before it and ended before the first check of the recovery,
    so the longest is the span between those two, unknown while either is missing.
    """
    incident["min_downtime_seconds"] = seconds_between(incident["opened_at"], incident["last_failure_at"])
    incident["max_downtime_seconds"] = seconds_between(incident["previous_success_at"],
                                                       incident["recovery_started_at"])
    return incident


def _episodes(rows, failures: int, recoveries: int):
    pending = {}
    active = {}
    healthy = {}
    last_up = {}
    for row in rows:
        url = row["url"]
        incident = active.get(url)
        if incident is None:
            if row["state"] == "up":
                pending.pop(url, None)
                last_up[url] = row["checked_at"]
                continue
            first, count = pending.get(url, (row, 0))
            count += 1
            if count < failures:
                pending[url] = (first, count)
                continue
            incident = {
                "url": url,
                "state": "open",
                "opened_at": first["checked_at"],
                "first_failure_id": first["id"],
                "confirmed_at": row["checked_at"],
                "confirmation_check_id": row["id"],
                "resolved_at": None,
                "resolution_check_id": None,
                "failed_checks": count,
                "last_checked_at": row["checked_at"],
                "last_check_id": row["id"],
                "previous_success_at": last_up.get(url),
                "last_failure_at": row["checked_at"],
                "recovery_started_at": None,
            }
            active[url] = incident
            pending.pop(url, None)
            healthy[url] = 0
            continue
        incident["last_checked_at"] = row["checked_at"]
        incident["last_check_id"] = row["id"]
        if row["state"] == "down":
            incident["failed_checks"] += 1
            incident["last_failure_at"] = row["checked_at"]
            incident["recovery_started_at"] = None
            healthy[url] = 0
        else:
            healthy[url] += 1
            if healthy[url] == 1:
                incident["recovery_started_at"] = row["checked_at"]
            if healthy[url] >= recoveries:
                incident.update(
                    state="resolved", resolved_at=row["checked_at"],
                    resolution_check_id=row["id"],
                )
                last_up[url] = row["checked_at"]
                yield _bound(incident)
                del active[url]
                del healthy[url]
    for incident in active.values():
        # Still open: the outage hasn't ended, so the longest it could be is unknown.
        yield _bound({**incident, "recovery_started_at": None})
