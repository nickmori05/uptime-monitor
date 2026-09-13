from contextlib import closing
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
    for name, value, maximum in (
        ("Limit", limit, 1000), ("Failures", failures, 100), ("Recoveries", recoveries, 100)
    ):
        if type(value) is not int or not 1 <= value <= maximum:
            raise ValueError(f"{name} must be between 1 and {maximum}.")
    if state not in (None, "open", "resolved"):
        raise ValueError("State must be open or resolved.")
    query = "SELECT id, url, checked_at, state FROM checks"
    parameters = ()
    if url is not None:
        query += " WHERE url = ?"
        parameters = (url,)
    query += " ORDER BY id"
    with closing(connection.execute(query, parameters)) as rows:
        return nlargest(
            limit,
            (incident for incident in _episodes(rows, failures, recoveries)
             if state is None or incident["state"] == state),
            key=lambda incident: incident["first_failure_id"],
        )


def _episodes(rows, failures: int, recoveries: int):
    pending = {}
    active = {}
    healthy = {}
    for row in rows:
        url = row["url"]
        incident = active.get(url)
        if incident is None:
            if row["state"] == "up":
                pending.pop(url, None)
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
            }
            active[url] = incident
            pending.pop(url, None)
            healthy[url] = 0
            continue
        incident["last_checked_at"] = row["checked_at"]
        incident["last_check_id"] = row["id"]
        if row["state"] == "down":
            incident["failed_checks"] += 1
            healthy[url] = 0
        else:
            healthy[url] += 1
            if healthy[url] >= recoveries:
                incident.update(
                    state="resolved", resolved_at=row["checked_at"],
                    resolution_check_id=row["id"],
                )
                yield incident
                del active[url]
                del healthy[url]
    yield from active.values()
