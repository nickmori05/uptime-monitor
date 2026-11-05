"""Delete checks older than a retention period without changing the incidents and alerts that follow them.

Incidents are rebuilt from saved checks, so deleting an arbitrary prefix of a URL's
history could split an incident, renumber its first failure (and with it the
alert's idempotency key), or lose the last success that bounds the next one. Each
URL is instead cut just before a success that is outside any incident (under the
default thresholds that alerts use): rebuilding from that check finds exactly the
same incidents after it, with the same IDs and bounds.
"""

from datetime import datetime, timedelta, timezone
import math
import sqlite3

import alerts
from incidents import scan_incidents


def prune(connection: sqlite3.Connection, keep_days: int, *, now: datetime | None = None,
          dry_run: bool = False) -> dict:
    """Delete each URL's checks from before the last `keep_days` days, and the alert records of their incidents.

    A URL keeps every check from its last clean success before the cutoff onward.
    Checks newer than the cutoff, incidents still open, and incidents with an alert
    still owed are never deleted, so a URL can keep checks older than the cutoff;
    `held` lists the incidents that did. With `dry_run`, report without deleting.
    """
    if type(keep_days) is not int or not 1 <= keep_days <= 36500:
        raise ValueError("Days to keep must be between 1 and 36500.")
    cutoff = (now or datetime.now(timezone.utc)) - timedelta(days=keep_days)
    # Plan and delete in one write transaction, so a watcher can't save a check or send an alert in between.
    connection.execute("BEGIN IMMEDIATE")
    try:
        cuts, held = plan(connection, cutoff)
        deleted_alerts = 0
        for cut in cuts:
            older = "SELECT id FROM checks WHERE url = ? AND id < ?"
            values = (cut["url"], cut["kept_from_id"])
            connection.execute(f"DELETE FROM deliveries WHERE first_failure_id IN ({older})", values)
            deleted_alerts += connection.execute(
                f"DELETE FROM alerts WHERE first_failure_id IN ({older})", values).rowcount
            connection.execute("DELETE FROM checks WHERE url = ? AND id < ?", values)
        if dry_run:
            connection.rollback()
        else:
            connection.commit()
    except BaseException:
        connection.rollback()
        raise
    return {"cutoff": cutoff.isoformat(), "dry_run": dry_run,
            "deleted_checks": sum(cut["deleted_checks"] for cut in cuts),
            "deleted_alerts": deleted_alerts, "urls": cuts, "held": held}


def plan(connection: sqlite3.Connection, cutoff: datetime) -> tuple[list[dict], list[dict]]:
    """Return the cuts and the holds.

    A cut is, per URL with checks to delete, the first check it keeps and how many
    come before it. A hold is an incident that began before the cutoff and stopped
    its URL's cut there, because it was still open at the cutoff or is owed an alert.
    """
    inside = {}
    for incident in scan_incidents(connection):
        # Checks from the first failure up to (not including) the resolving success belong to the incident.
        end = incident["resolution_check_id"]
        inside.setdefault(incident["url"], []).append(
            (incident["first_failure_id"], math.inf if end is None else end, incident["opened_at"]))
    held = {}
    if alerts.enabled_after(connection) is not None:
        # An incident with an event still owed must keep its checks, or the alert would never be sent.
        for event in alerts.pending_alerts(connection):
            incident = event["incident"]
            if incident["first_failure_id"] < held.get(incident["url"], (math.inf,))[0]:
                held[incident["url"]] = (incident["first_failure_id"], incident["opened_at"])
    cuts, holds = [], []
    rows = connection.execute("SELECT id, url, checked_at, state FROM checks ORDER BY url, id")
    current, position, anchor, done = None, 0, None, False
    spans, span = [], 0
    for row in rows:
        if row["url"] != current:
            _keep(cuts, current, anchor)
            current, position, anchor, done = row["url"], 0, None, False
            spans, span = sorted(inside.get(current, [])), 0
        position += 1
        if done:
            continue
        while span < len(spans) and spans[span][1] <= row["id"]:
            span += 1
        within = span < len(spans) and spans[span][0] <= row["id"]
        if not _older(row["checked_at"], cutoff):
            # Nothing from here on is old enough, and an incident across the cutoff keeps its older checks too.
            if within and spans[span][0] < row["id"]:
                holds.append(_hold(current, "open_at_cutoff", spans[span][0], spans[span][2]))
            done = True
            continue
        owed = held.get(current)
        if owed is not None and row["id"] >= owed[0]:
            # Nothing from here on can go: an incident is still owed an alert.
            holds.append(_hold(current, "unsent_alert", *owed))
            done = True
            continue
        if row["state"] == "up" and not within:
            anchor = (row["id"], row["checked_at"], position - 1)
    _keep(cuts, current, anchor)
    return cuts, holds


def _hold(url: str, reason: str, first_failure_id: int, opened_at: str) -> dict:
    return {"url": url, "reason": reason, "first_failure_id": first_failure_id, "opened_at": opened_at}


def _keep(cuts: list[dict], url: str | None, anchor: tuple | None) -> None:
    if url is not None and anchor is not None and anchor[2] > 0:
        cuts.append({"url": url, "deleted_checks": anchor[2], "kept_from_id": anchor[0], "kept_from_at": anchor[1]})


def _older(checked_at: str, cutoff: datetime) -> bool:
    """Return whether a saved timestamp is before the cutoff. Unreadable or naive ones never are."""
    try:
        moment = datetime.fromisoformat(checked_at)
    except (TypeError, ValueError):
        return False
    return moment.tzinfo is not None and moment < cutoff
