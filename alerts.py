"""Send each incident opening and resolution to a webhook, keyed so receivers can drop repeats."""

from datetime import datetime, timezone
import hashlib
import http.client
import json
import sqlite3
import sys
import threading
from urllib.error import HTTPError, URLError
from urllib.request import HTTPRedirectHandler, Request, build_opener

from incidents import scan_incidents

WEBHOOK_TIMEOUT = 5.0
WEBHOOK_DEADLINE = 10.0


class NoRedirects(HTTPRedirectHandler):
    """Report redirects as failed deliveries instead of following them."""

    def redirect_request(self, request, response, code, message, headers, new_url):
        return None


def incident_events(connection: sqlite3.Connection) -> list[dict]:
    """List every incident opening and resolution in saved history, oldest first."""
    events = []
    for incident in scan_incidents(connection):
        events.append({"event": "opened", "check_id": incident["confirmation_check_id"], "incident": incident})
        if incident["state"] == "resolved":
            events.append({"event": "resolved", "check_id": incident["resolution_check_id"], "incident": incident})
    return sorted(events, key=lambda event: event["check_id"])


def pending_alerts(connection: sqlite3.Connection) -> list[dict]:
    """Return incident events that have not been sent or skipped yet."""
    recorded = {(row["first_failure_id"], row["event"])
                for row in connection.execute("SELECT first_failure_id, event FROM alerts")}
    return [event for event in incident_events(connection)
            if (event["incident"]["first_failure_id"], event["event"]) not in recorded]


def record(connection: sqlite3.Connection, events: list[dict], status: str) -> None:
    """Save events as sent or skipped. Events that are already saved are left alone."""
    with connection:
        _insert(connection, events, status)


def _insert(connection: sqlite3.Connection, events: list[dict], status: str) -> None:
    recorded_at = datetime.now(timezone.utc).isoformat()
    connection.executemany(
        """INSERT INTO alerts (url, first_failure_id, event, check_id, status, recorded_at)
           VALUES (?, ?, ?, ?, ?, ?) ON CONFLICT(first_failure_id, event) DO NOTHING""",
        [(event["incident"]["url"], event["incident"]["first_failure_id"], event["event"],
          event["check_id"], status, recorded_at) for event in events],
    )


def enabled_after(connection: sqlite3.Connection) -> int | None:
    """Return the last check ID from before alerting was first enabled, or None if it never was."""
    row = connection.execute("SELECT after_check_id FROM alerting WHERE id = 1").fetchone()
    return None if row is None else row["after_check_id"]


def skip_backlog(connection: sqlite3.Connection) -> int:
    """The first time alerting is enabled, mark events already in history as skipped. Return how many."""
    if enabled_after(connection) is not None:
        return 0
    last = connection.execute("SELECT COALESCE(MAX(id), 0) FROM checks").fetchone()[0]
    events = [event for event in pending_alerts(connection) if event["check_id"] <= last]
    with connection:
        _insert(connection, events, "skipped")
        # Everything after this check is owed an alert, even if no watcher is running when it happens.
        connection.execute("INSERT INTO alerting (id, enabled_at, after_check_id) VALUES (1, ?, ?) "
                           "ON CONFLICT(id) DO NOTHING", (datetime.now(timezone.utc).isoformat(), last))
    return len(events)


def unsent_alerts(connection: sqlite3.Connection) -> list[dict]:
    """Return the events the next alerting watcher would send, oldest first."""
    if enabled_after(connection) is None:
        return []
    return [payload(event) for event in pending_alerts(connection)]


def start(connection: sqlite3.Connection, webhook: str) -> list[dict]:
    """Prepare a watcher to alert, sending any events missed while no alerting watcher was running."""
    skipped = skip_backlog(connection)
    if skipped:
        print(f"Skipped {skipped} earlier incident event(s); alerting on new ones.", file=sys.stderr, flush=True)
    missed = len(pending_alerts(connection))
    if not missed:
        return []
    print(f"Sending {missed} incident event(s) not alerted yet.", file=sys.stderr, flush=True)
    return deliver(connection, webhook)


def idempotency_key(url: str, first_failure_id: int, opened_at: str, event: str) -> str:
    """Return the key for one incident event: the same on every resend, and distinct across events and databases.

    It depends only on the event and the incident's first failed check, never on later
    checks, so a copy sent again after a restart carries the same key. The URL and the
    first failure's timestamp keep two databases from producing the same key.
    """
    material = json.dumps([url, first_failure_id, opened_at, event], separators=(",", ":"))
    return hashlib.sha256(material.encode()).hexdigest()[:32]


def payload(event: dict) -> dict:
    """Build the JSON body sent to the webhook for one event."""
    incident = event["incident"]
    if event["event"] == "opened":
        text = f"Incident opened for {incident['url']}: failing since {incident['opened_at']}."
    else:
        text = f"Incident resolved for {incident['url']}: recovered at {incident['resolved_at']}."
    key = idempotency_key(incident["url"], incident["first_failure_id"], incident["opened_at"], event["event"])
    return {"event": event["event"], "url": incident["url"], "idempotency_key": key, "text": text,
            "incident": incident}


def post(webhook: str, body: dict, timeout: float) -> str | None:
    """Send one POST and return None on a 2xx response, or a short error code."""
    headers = {"Content-Type": "application/json", "User-Agent": "uptime-monitor/0.1"}
    if body.get("idempotency_key") is not None:
        headers["Idempotency-Key"] = body["idempotency_key"]
    request = Request(webhook, data=json.dumps(body).encode(), method="POST", headers=headers)
    try:
        with build_opener(NoRedirects()).open(request, timeout=timeout) as response:
            status = response.status
    except HTTPError as response:
        response.close()
        status = response.code
    except TimeoutError:
        return "timeout"
    except URLError as failure:
        return "timeout" if isinstance(failure.reason, TimeoutError) else "connection_error"
    except (OSError, http.client.HTTPException):
        return "connection_error"
    return None if 200 <= status < 300 else f"http_{status}"


def send_webhook(webhook: str, body: dict, timeout: float = WEBHOOK_TIMEOUT,
                 deadline: float = WEBHOOK_DEADLINE) -> str | None:
    """POST one alert as JSON. Return None when accepted, or a short error code."""
    # Like probe deadlines, a hung DNS lookup is abandoned on a daemon thread.
    outcome = []

    def attempt():
        try:
            outcome.append(post(webhook, body, timeout))
        except Exception:
            outcome.append("connection_error")

    worker = threading.Thread(target=attempt, name="alert-webhook", daemon=True)
    worker.start()
    worker.join(deadline)
    return outcome[0] if outcome else "deadline_exceeded"


def deliver(connection: sqlite3.Connection, webhook: str) -> list[dict]:
    """Send pending alerts in order, stopping at the first failure so it is retried next round."""
    sent = []
    for event in pending_alerts(connection):
        body = payload(event)
        error = send_webhook(webhook, body)
        if error is not None:
            print(f"Alert not delivered ({error}); retrying next round.", file=sys.stderr, flush=True)
            break
        record(connection, [event], "sent")
        print(f"Alert sent: {body['text']}", file=sys.stderr, flush=True)
        sent.append(body)
    return sent


def list_alerts(connection: sqlite3.Connection, limit: int = 20, *, pending: bool = False) -> list[dict]:
    """Return recorded alerts newest first, or with `pending`, unsent events in the order they'd be sent."""
    if type(limit) is not int or not 1 <= limit <= 1000:
        raise ValueError("Alert limit must be between 1 and 1000.")
    if pending:
        return unsent_alerts(connection)[:limit]
    rows = connection.execute(
        """SELECT alerts.*, checks.checked_at AS opened_at FROM alerts
           LEFT JOIN checks ON checks.id = alerts.first_failure_id
           ORDER BY alerts.id DESC LIMIT ?""", (limit,))
    listed = []
    for row in rows:
        alert = dict(row)
        opened_at = alert.pop("opened_at")
        # Shows the key each sent alert carried, so it can be matched against the receiver's log.
        alert["idempotency_key"] = None if opened_at is None else idempotency_key(
            alert["url"], alert["first_failure_id"], opened_at, alert["event"])
        listed.append(alert)
    return listed
