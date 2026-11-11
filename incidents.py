"""Rebuild incidents (runs of consecutive failures) from saved observations."""

from contextlib import closing
from datetime import datetime
from heapq import nlargest
import sqlite3

DEFAULT_FAILURES = 2
DEFAULT_RECOVERIES = 2


def validate_count(name: str, value: int) -> int:
    """Return a failure or recovery count if it is an integer from 1 to 100."""
    if type(value) is not int or not 1 <= value <= 100:
        raise ValueError(f"{name} must be between 1 and 100.")
    return value


def ensure_agreement(url: str, targets: list[dict]) -> tuple[int, int]:
    """Return the failure and recovery counts for one URL.

    Targets of a URL share its checks, so they share one incident reading. A count
    left unset stays at the default. An override on any target applies to the URL,
    and two different overrides are an error.
    """
    failures = _agreed(url, targets, "failures", "Failures", "failure", DEFAULT_FAILURES)
    recoveries = _agreed(url, targets, "recoveries", "Recoveries", "recovery", DEFAULT_RECOVERIES)
    return failures, recoveries


def _agreed(url: str, targets: list[dict], field: str, label: str, noun: str, default: int) -> int:
    chosen: dict[int, list[str]] = {}
    for target in targets:
        value = target.get(field)
        if value is None:
            continue
        chosen.setdefault(validate_count(label, value), []).append(target["name"])
    if len(chosen) > 1:
        names = ", ".join(f"'{name}'" for name in sorted(name for group in chosen.values() for name in group))
        raise ValueError(f"Targets {names} set different {noun} thresholds for {url}.")
    if not chosen:
        return default
    return next(iter(chosen))


def thresholds_by_url(connection: sqlite3.Connection) -> dict[str, tuple[int, int]]:
    """Map each saved target's URL to the failure and recovery counts its targets share."""
    grouped: dict[str, list[dict]] = {}
    for row in connection.execute("SELECT name, url, failures, recoveries FROM targets ORDER BY name"):
        grouped.setdefault(row["url"], []).append(
            {"name": row["name"], "failures": row["failures"], "recoveries": row["recoveries"]})
    return {url: ensure_agreement(url, rows) for url, rows in grouped.items()}


def _forced_thresholds(failures: int | None, recoveries: int | None) -> tuple[int, int] | None:
    """Return counts that override every URL, or None to use each URL's saved counts.

    Passing either count selects the override and leaves the other at its default,
    which is how `incidents --failures N` has always read history.
    """
    if failures is None and recoveries is None:
        return None
    if failures is None:
        failures = DEFAULT_FAILURES
    if recoveries is None:
        recoveries = DEFAULT_RECOVERIES
    return validate_count("Failures", failures), validate_count("Recoveries", recoveries)


def list_incidents(
    connection: sqlite3.Connection,
    *,
    url: str | None = None,
    state: str | None = None,
    limit: int = 20,
    failures: int | None = None,
    recoveries: int | None = None,
) -> list[dict]:
    """Return incidents newest first, optionally filtered by URL and state.

    With no counts given, each URL uses the thresholds its targets saved, the same
    ones alerts use. Passing either count reinterprets every URL for this report only.
    """
    if type(limit) is not int or not 1 <= limit <= 1000:
        raise ValueError("Limit must be between 1 and 1000.")
    if state not in (None, "open", "resolved"):
        raise ValueError("State must be open or resolved.")
    return nlargest(
        limit,
        (incident for incident in scan_incidents(connection, url=url, failures=failures, recoveries=recoveries)
         if state is None or incident["state"] == state),
        key=lambda incident: incident["first_failure_id"],
    )


def scan_incidents(connection: sqlite3.Connection, *, url: str | None = None,
                   failures: int | None = None, recoveries: int | None = None) -> list[dict]:
    """Rebuild every incident from saved checks, in no particular order.

    With no counts given, each URL uses the thresholds its targets saved. Passing
    either count reinterprets every URL with that pair instead.
    """
    forced = _forced_thresholds(failures, recoveries)
    # An explicit pair applies to every URL. Otherwise a URL with no target keeps the default.
    by_url = {} if forced is not None else thresholds_by_url(connection)
    default = forced if forced is not None else (DEFAULT_FAILURES, DEFAULT_RECOVERIES)

    def counts(checked: str) -> tuple[int, int]:
        return by_url.get(checked, default)

    query = "SELECT id, url, checked_at, state FROM checks"
    parameters = ()
    if url is not None:
        query += " WHERE url = ?"
        parameters = (url,)
    query += " ORDER BY id"
    with closing(connection.execute(query, parameters)) as rows:
        return list(_episodes(rows, counts))


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


def _episodes(rows, counts):
    pending = {}
    active = {}
    healthy = {}
    last_up = {}
    for row in rows:
        url = row["url"]
        failures, recoveries = counts(url)
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
                "failures": failures,
                "recoveries": recoveries,
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
