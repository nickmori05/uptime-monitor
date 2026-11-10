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
import routing
import webhooks

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


def record(connection: sqlite3.Connection, events: list[dict], status: str, route: str | None = None) -> None:
    """Save events as sent or skipped, and with `route`, that they reached it. Saved events are left alone."""
    with connection:
        if route is not None:
            _reached(connection, events, route)
        _insert(connection, events, status)


def _reached(connection: sqlite3.Connection, events: list[dict], route: str) -> None:
    connection.executemany(
        """INSERT INTO deliveries (first_failure_id, event, route, recorded_at) VALUES (?, ?, ?, ?)
           ON CONFLICT(first_failure_id, event, route) DO NOTHING""",
        [(event["incident"]["first_failure_id"], event["event"], route, datetime.now(timezone.utc).isoformat())
         for event in events],
    )


def plan(connection: sqlite3.Connection, webhook: str | None) -> list[tuple[dict, list[str], set[str]]]:
    """Pair each pending event with the routes it is owed and the ones it already reached, oldest first."""
    table = routing.routes_by_url(connection)
    reached = {}
    for row in connection.execute("SELECT first_failure_id, event, route FROM deliveries"):
        # A target webhook that is also this watcher's default counts as the default route (see `routing.owed`).
        route = routing.DEFAULT if row["route"] == webhook else row["route"]
        reached.setdefault((row["first_failure_id"], row["event"]), set()).add(route)
    return [(event, routing.owed(table, event["incident"]["url"], webhook),
             reached.get((event["incident"]["first_failure_id"], event["event"]), set()))
            for event in pending_alerts(connection)]


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


def resolved_unsent(connection: sqlite3.Connection, events: list[dict]) -> list[dict]:
    """Return the pending events of incidents that resolved before their opening was sent, oldest first.

    That is an incident whose opening is still pending or was skipped, and reached
    no route. Both of its events are returned, so it is skipped as a whole and its
    resolution never arrives for an opening the receiver didn't get.
    """
    opened = {row["first_failure_id"] for row in connection.execute(
        "SELECT first_failure_id FROM alerts WHERE event = 'opened' AND status = 'sent' "
        "UNION SELECT first_failure_id FROM deliveries WHERE event = 'opened'")}
    return [event for event in events
            if event["incident"]["state"] == "resolved" and event["incident"]["first_failure_id"] not in opened]


def skip_resolved_incidents(connection: sqlite3.Connection) -> int:
    """Mark incidents that resolved before their opening was sent as skipped. Return how many incidents."""
    events = resolved_unsent(connection, pending_alerts(connection))
    record(connection, events, "skipped")
    skipped = len({event["incident"]["first_failure_id"] for event in events})
    if skipped:
        print(f"Skipped {skipped} incident(s) that resolved before their opening was sent.",
              file=sys.stderr, flush=True)
    return skipped


def unsent_alerts(connection: sqlite3.Connection, *, skip_resolved: bool = False) -> list[dict]:
    """Return the events the next alerting watcher would send, oldest first, with the routes each still needs.

    With `skip_resolved`, leave out the incidents a watcher run with `--skip-resolved` would skip.
    """
    if enabled_after(connection) is None:
        return []
    planned = plan(connection, None)
    skipped = set()
    if skip_resolved:
        skipped = {event["incident"]["first_failure_id"]
                   for event in resolved_unsent(connection, [event for event, _, _ in planned])}
    unsent = []
    for event, owed, reached in planned:
        routes = [route for route in owed if route not in reached]
        if routes and event["incident"]["first_failure_id"] not in skipped:
            unsent.append({**payload(event), "routes": routes})
    return unsent


def start(connection: sqlite3.Connection, webhook: str | None, *, skip_resolved: bool = False,
          backoff: webhooks.Backoff | None = None) -> list[dict]:
    """Prepare a watcher to alert, sending any events missed while no alerting watcher was running.

    With `skip_resolved`, missed incidents that have already resolved are skipped instead.
    Every webhook with alerts owed is tried once now, even one a backoff would leave alone,
    since a restart is when a fixed webhook is most likely to be waiting.
    """
    skipped = skip_backlog(connection)
    if skipped:
        print(f"Skipped {skipped} earlier incident event(s); alerting on new ones.", file=sys.stderr, flush=True)
    if skip_resolved:
        skip_resolved_incidents(connection)
    missed = sum(any(route not in reached and (route != routing.DEFAULT or webhook is not None) for route in owed)
                 for _, owed, reached in plan(connection, webhook))
    if not missed:
        return []
    print(f"Sending {missed} incident event(s) not alerted yet.", file=sys.stderr, flush=True)
    return deliver(connection, webhook, skip_resolved=skip_resolved, backoff=backoff, retry_now=True)


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


def deliver(connection: sqlite3.Connection, webhook: str | None, *, skip_resolved: bool = False,
            backoff: webhooks.Backoff | None = None, retry_now: bool = False) -> list[dict]:
    """Send pending alerts on every route they are owed, in order on each route. Return the bodies sent.

    A route stops at its first failure this round, so its alerts are retried in
    order next round, while other routes carry on. With `backoff`, a failed route
    is left alone until its saved retry time instead, unless `retry_now`. The
    default route needs `webhook`; without one, alerts owed there wait for a
    watcher that has it. An alert owed nowhere (every target for its URL has
    alerts off) is skipped. With `skip_resolved`, incidents that resolved before
    their opening was sent are skipped first.
    """
    if skip_resolved:
        skip_resolved_incidents(connection)
    sent = []
    # Routes still waiting out a backoff are held like routes that already failed this round.
    stopped = webhooks.backing_off(connection) if backoff is not None and not retry_now else set()
    for event, owed, reached in plan(connection, webhook):
        if reached.issuperset(owed):
            # Owed nowhere, or every route it still has was reached before the rest were removed.
            record(connection, [event], "sent" if reached else "skipped")
            continue
        body = payload(event)
        for route in owed:
            if route in reached or route in stopped or (route == routing.DEFAULT and webhook is None):
                continue
            error = send_webhook(webhook if route == routing.DEFAULT else route, body)
            retry_at = webhooks.note(connection, route, error, backoff)
            where = routing.describe(route)
            if error is not None:
                when = "next round" if retry_at is None else f"after {retry_at.isoformat(timespec='seconds')}"
                print(f"Alert not delivered ({error}){where}; retrying {when}.", file=sys.stderr, flush=True)
                stopped.add(route)
                continue
            reached.add(route)
            if reached.issuperset(owed):
                record(connection, [event], "sent", route)
            else:
                with connection:
                    _reached(connection, [event], route)
            print(f"Alert sent{where}: {body['text']}", file=sys.stderr, flush=True)
            sent.append(body)
    return sent


class Alerter:
    """Send one watcher's alerts each round, starting the first time it has somewhere to send them."""

    def __init__(self, connection: sqlite3.Connection, webhook: str | None = None, *, skip_resolved: bool = False,
                 backoff: webhooks.Backoff | None = None):
        self.connection, self.webhook, self.skip_resolved = connection, webhook, skip_resolved
        self.backoff = backoff
        self.started = False

    def __call__(self) -> list[dict]:
        if self.started:
            return deliver(self.connection, self.webhook, skip_resolved=self.skip_resolved, backoff=self.backoff)
        # Targets are reloaded every round, so one given a webhook mid-run starts alerting then.
        if self.webhook is None and not routing.has_webhooks(self.connection):
            return []
        self.started = True
        return start(self.connection, self.webhook, skip_resolved=self.skip_resolved, backoff=self.backoff)


def webhook_health(connection: sqlite3.Connection) -> list[dict]:
    """Report each webhook's last success, last failure, and the alerts queued for it."""
    return webhooks.report(connection, unsent_alerts(connection))


def list_alerts(connection: sqlite3.Connection, limit: int = 20, *, pending: bool = False,
                skip_resolved: bool = False) -> list[dict]:
    """Return recorded alerts newest first, or with `pending`, unsent events in the order they'd be sent.

    `skip_resolved` previews a watcher run with `--skip-resolved`, so it needs `pending`.
    """
    if type(limit) is not int or not 1 <= limit <= 1000:
        raise ValueError("Alert limit must be between 1 and 1000.")
    if skip_resolved and not pending:
        raise ValueError("--skip-resolved previews pending alerts, so it needs --pending.")
    if pending:
        return unsent_alerts(connection, skip_resolved=skip_resolved)[:limit]
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
