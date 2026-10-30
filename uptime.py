"""Bound each URL's uptime over a time window from its incidents and the gaps between its checks."""

from contextlib import closing
from datetime import datetime, timedelta, timezone
import math
import sqlite3

from incidents import scan_incidents

MAX_HOURS = 24 * 366
MAX_GAP_SECONDS = 86_400


def parse_time(value: str | None) -> datetime | None:
    """Read a saved timestamp, or None if it's missing, unreadable, or has no timezone."""
    if value is None:
        return None
    try:
        parsed = datetime.fromisoformat(value)
    except (TypeError, ValueError):
        return None
    return parsed if parsed.tzinfo is not None else None


def window_end(until: str | None) -> datetime:
    """Return the end of the report window: `until`, or now."""
    if until is None:
        return datetime.now(timezone.utc)
    end = parse_time(until)
    if end is None:
        raise ValueError("Until must be an ISO 8601 timestamp with a timezone, such as 2026-10-27T12:00:00+00:00.")
    return end


def measure(spans: list[tuple[datetime, datetime]], start: datetime, end: datetime) -> float:
    """Return the seconds of [start, end] covered by at least one span, counting overlaps once."""
    clipped = sorted((max(low, start), min(high, end)) for low, high in spans)
    total = 0.0
    reach = start
    for low, high in clipped:
        low = max(low, reach)
        if high > low:
            total += (high - low).total_seconds()
            reach = high
    return total


def unobserved(times: list[datetime], start: datetime, end: datetime,
               max_gap: float) -> list[tuple[datetime, datetime]]:
    """Return the stretches of the window where checks were more than `max_gap` seconds apart.

    `times` are the URL's check times in the order they were saved. Before its
    first check nothing is known, and after its last one the window's end counts
    as the next check, so a URL that stopped being checked is unobserved from then on.
    """
    if not times:
        return [(start, end)]
    gaps = [(start, times[0])] if times[0] > start else []
    limit = timedelta(seconds=max_gap)
    for earlier, later in zip(times, [*times[1:], end], strict=True):
        # A clock set back makes a later check look earlier; that isn't a gap.
        if later - earlier > limit:
            gaps.append((earlier, later))
    return gaps


def uptime_report(connection: sqlite3.Connection, *, url: str | None = None, hours: int = 24,
                  until: str | None = None, max_gap: int = 300, failures: int = 2,
                  recoveries: int = 2) -> list[dict]:
    """Report the shortest and longest each URL could have been down in a window, and the uptime that leaves.

    Only incidents count as downtime, using the same thresholds as `incidents`.
    The shortest is the time inside the window between each incident's first
    and last failed check. The longest adds the time from the last success
    before each incident to the start of its recovery, and every stretch with
    no check for more than `max_gap` seconds, since the URL could have been
    down then without anyone seeing.
    """
    for name, value, maximum in (("Hours", hours, MAX_HOURS), ("Max gap", max_gap, MAX_GAP_SECONDS)):
        if type(value) is not int or not 1 <= value <= maximum:
            raise ValueError(f"{name} must be between 1 and {maximum}.")
    end = window_end(until)
    start = end - timedelta(hours=hours)
    window = (end - start).total_seconds()
    query = "SELECT url, checked_at FROM checks"
    parameters = ()
    if url is not None:
        query += " WHERE url = ?"
        parameters = (url,)
    times = {url: []} if url is not None else {}
    with closing(connection.execute(query + " ORDER BY id", parameters)) as rows:
        for row in rows:
            checked_at = parse_time(row["checked_at"])
            if checked_at is not None and checked_at <= end:
                times.setdefault(row["url"], []).append(checked_at)
    shortest, longest, counted = {}, {}, {}
    for incident in scan_incidents(connection, url=url, failures=failures, recoveries=recoveries):
        opened, last_failure = parse_time(incident["opened_at"]), parse_time(incident["last_failure_at"])
        if opened is None or last_failure is None or opened > end:
            continue
        # Unknown ends stretch to the window's edges: history that starts down, or an incident still open.
        began = parse_time(incident["previous_success_at"]) or start
        ended = parse_time(incident["recovery_started_at"]) or end
        if ended < start:
            continue
        key = incident["url"]
        counted[key] = counted.get(key, 0) + 1
        shortest.setdefault(key, []).append((opened, last_failure))
        longest.setdefault(key, []).append((min(began, opened), max(ended, last_failure)))
    report = []
    for key in sorted(times):
        checks = times[key]
        observed = [checked_at for checked_at in checks if start <= checked_at]
        if not observed and key not in counted and url is None:
            continue
        gaps = unobserved(checks, start, end, max_gap)
        least = measure(shortest.get(key, []), start, end)
        most = measure(longest.get(key, []) + gaps, start, end)
        report.append({
            "url": key,
            "since": start.isoformat(),
            "until": end.isoformat(),
            "window_seconds": round(window, 3),
            "checks": len(observed),
            "incidents": counted.get(key, 0),
            "unobserved_seconds": round(measure(gaps, start, end), 3),
            "min_downtime_seconds": round(least, 3),
            "max_downtime_seconds": round(most, 3),
            "min_uptime_percent": percent(window - most, window),
            "max_uptime_percent": percent(window - least, window),
        })
    return report


def percent(part: float, whole: float) -> float:
    """Return part/whole as a percentage, rounded down to three decimal places.

    Rounding down means a window with any possible downtime never shows as 100%.
    """
    # The small allowance keeps float error from turning an exact 100% into 99.999%.
    return math.floor(max(part, 0.0) / whole * 100_000 + 1e-6) / 1000
