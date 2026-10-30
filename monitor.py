"""Check HTTP endpoints, save each observation to SQLite, and report on the history."""

import argparse
from concurrent.futures import ThreadPoolExecutor, as_completed
from contextlib import closing
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
import http.client
import json
import math
from pathlib import Path
import re
import signal
import sqlite3
import sys
import threading
from time import perf_counter, sleep
from urllib.error import HTTPError, URLError
from urllib.parse import urlsplit
from urllib.request import HTTPRedirectHandler, Request, build_opener

import alerts
import api
from incidents import list_incidents
import routing
import webhooks

DEFAULT_DATABASE = Path(__file__).resolve().parent / ".local" / "checks.sqlite3"
ALERT_WEBHOOK_COLUMN = ("TEXT NOT NULL DEFAULT 'default' CHECK(alert_webhook IN ('default', 'none') "
                        "OR alert_webhook LIKE 'http://%' OR alert_webhook LIKE 'https://%')")


@dataclass(frozen=True)
class Check:
    """One saved observation of an endpoint."""
    url: str
    checked_at: str
    state: str
    status_code: int | None
    latency_ms: float
    error: str | None


class NoRedirects(HTTPRedirectHandler):
    """Report redirects as responses instead of following them."""

    def redirect_request(self, request, response, code, message, headers, new_url):
        return None


def validate_url(value: str) -> str:
    """Return the URL if it is an absolute http(s) URL that can be sent as-is, else raise ValueError."""
    if any(character.isspace() or ord(character) < 32 or ord(character) == 127 for character in value):
        raise ValueError("URLs must not contain whitespace or control characters.")
    try:
        parsed = urlsplit(value)
        port = parsed.port
        value.encode("ascii")
    except (ValueError, UnicodeEncodeError) as error:
        raise ValueError("Use a valid HTTP(S) URL with an encoded hostname and path.") from error
    if parsed.scheme not in ("http", "https") or not parsed.hostname:
        raise ValueError("Use an http:// or https:// URL with a hostname.")
    if parsed.username is not None or parsed.password is not None:
        raise ValueError("Usernames and passwords embedded in URLs are not supported.")
    if parsed.fragment:
        raise ValueError("Remove the URL fragment; it is not sent to the server.")
    if port == 0:
        raise ValueError("The port must be between 1 and 65535.")
    return value


def validate_timeout(timeout: float) -> float:
    """Return the socket timeout if it is in (0, 60] seconds, else raise ValueError."""
    if not math.isfinite(timeout) or not 0 < timeout <= 60:
        raise ValueError("Timeout must be greater than 0 and at most 60 seconds.")
    return timeout


def validate_deadline(deadline: float | None) -> float | None:
    """Return the whole-request deadline (or None) if it is in (0, 60] seconds, else raise ValueError."""
    if deadline is None:
        return None
    if isinstance(deadline, bool) or not math.isfinite(deadline) or not 0 < deadline <= 60:
        raise ValueError("Deadline must be greater than 0 and at most 60 seconds.")
    return deadline


def validate_alert_webhook(value: str) -> str:
    """Return a target's alert setting if it is `default`, `none`, or a webhook URL, else raise ValueError."""
    if value in (routing.DEFAULT, routing.NONE):
        return value
    try:
        return validate_url(value)
    except ValueError as error:
        raise ValueError(f"Alert webhook must be default, none, or a webhook URL. {error}") from error


def request_status(url: str, timeout: float) -> tuple[int | None, str | None]:
    """Send one GET and return its status code, or None and an error code if it failed."""
    request = Request(url, headers={"User-Agent": "uptime-monitor/0.1"}, method="GET")
    try:
        with build_opener(NoRedirects()).open(request, timeout=timeout) as response:
            return response.status, None
    except HTTPError as response:
        response.close()
        return response.code, None
    except TimeoutError:
        return None, "timeout"
    except URLError as failure:
        return None, "timeout" if isinstance(failure.reason, TimeoutError) else "connection_error"
    except (OSError, http.client.HTTPException):
        return None, "connection_error"


def request_within(url: str, timeout: float, deadline: float) -> tuple[int | None, str | None]:
    # DNS lookups and slow servers can outlast the socket timeout, so the request
    # runs on a daemon thread that is abandoned once the deadline passes.
    """Run `request_status`, giving up with `deadline_exceeded` once the deadline passes."""
    outcome = []

    def attempt():
        try:
            outcome.append(request_status(url, timeout))
        except BaseException as error:
            outcome.append(error)

    worker = threading.Thread(target=attempt, name="probe-request", daemon=True)
    worker.start()
    worker.join(deadline)
    if not outcome:
        return None, "deadline_exceeded"
    if isinstance(outcome[0], BaseException):
        raise outcome[0]
    return outcome[0]


def probe(url: str, timeout: float = 5.0, deadline: float | None = None) -> Check:
    """Check a URL once and classify the result as up (2xx) or down. Nothing is saved."""
    url = validate_url(url)
    timeout = validate_timeout(timeout)
    deadline = validate_deadline(deadline)
    started_at = datetime.now(timezone.utc).isoformat()
    started = perf_counter()
    if deadline is None:
        status, error = request_status(url, timeout)
    else:
        status, error = request_within(url, timeout, deadline)
    healthy = status is not None and 200 <= status < 300
    if not healthy and error is None:
        error = f"http_{status}"
    return Check(
        url=url,
        checked_at=started_at,
        state="up" if healthy else "down",
        status_code=status,
        latency_ms=round((perf_counter() - started) * 1000, 3),
        error=error,
    )


def connect(database: Path) -> sqlite3.Connection:
    """Open the database, creating or upgrading its tables as needed."""
    database.parent.mkdir(parents=True, exist_ok=True)
    connection = sqlite3.connect(database)
    connection.row_factory = sqlite3.Row
    connection.execute(
        """CREATE TABLE IF NOT EXISTS checks (
            id INTEGER PRIMARY KEY,
            url TEXT NOT NULL,
            checked_at TEXT NOT NULL,
            state TEXT NOT NULL CHECK(state IN ('up', 'down')),
            status_code INTEGER,
            latency_ms REAL NOT NULL CHECK(latency_ms >= 0),
            error TEXT
        )"""
    )
    connection.execute(
        f"""CREATE TABLE IF NOT EXISTS targets (
            name TEXT PRIMARY KEY,
            url TEXT NOT NULL,
            timeout REAL NOT NULL CHECK(timeout > 0 AND timeout <= 60),
            deadline REAL CHECK(deadline IS NULL OR (deadline > 0 AND deadline <= 60)),
            alert_webhook {ALERT_WEBHOOK_COLUMN}
        )"""
    )
    # Databases created before targets had a saved deadline gain the column empty.
    columns = {row["name"] for row in connection.execute("PRAGMA table_info(targets)")}
    if "deadline" not in columns:
        connection.execute("ALTER TABLE targets ADD COLUMN deadline REAL "
                           "CHECK(deadline IS NULL OR (deadline > 0 AND deadline <= 60))")
    # Targets saved before alert routing send to the watcher's own webhook, as they always did.
    if "alert_webhook" not in columns:
        connection.execute(f"ALTER TABLE targets ADD COLUMN alert_webhook {ALERT_WEBHOOK_COLUMN}")
    connection.execute("CREATE INDEX IF NOT EXISTS checks_url_id ON checks(url, id)")
    # One row per incident event, so an alert is never sent twice for the same change.
    connection.execute(
        """CREATE TABLE IF NOT EXISTS alerts (
            id INTEGER PRIMARY KEY,
            url TEXT NOT NULL,
            first_failure_id INTEGER NOT NULL,
            event TEXT NOT NULL CHECK(event IN ('opened', 'resolved')),
            check_id INTEGER NOT NULL,
            status TEXT NOT NULL CHECK(status IN ('sent', 'skipped')),
            recorded_at TEXT NOT NULL,
            UNIQUE(first_failure_id, event)
        )"""
    )
    # A single row saved when alerting is first enabled; incident events after it are owed an alert.
    connection.execute(
        """CREATE TABLE IF NOT EXISTS alerting (
            id INTEGER PRIMARY KEY CHECK(id = 1),
            enabled_at TEXT NOT NULL,
            after_check_id INTEGER NOT NULL CHECK(after_check_id >= 0)
        )"""
    )
    # One row per route an incident event reached, so a failing webhook never holds up or repeats another's.
    connection.execute(
        """CREATE TABLE IF NOT EXISTS deliveries (
            id INTEGER PRIMARY KEY,
            first_failure_id INTEGER NOT NULL,
            event TEXT NOT NULL CHECK(event IN ('opened', 'resolved')),
            route TEXT NOT NULL,
            recorded_at TEXT NOT NULL,
            UNIQUE(first_failure_id, event, route)
        )"""
    )
    # One row per webhook route that was ever tried, with its latest outcomes, for the `webhooks` report.
    connection.execute(
        """CREATE TABLE IF NOT EXISTS webhooks (
            route TEXT PRIMARY KEY,
            last_success_at TEXT,
            last_failure_at TEXT,
            last_error TEXT,
            failures INTEGER NOT NULL DEFAULT 0 CHECK(failures >= 0),
            retry_at TEXT
        )"""
    )
    # Webhook health saved before backoff has no retry time, so every webhook is tried next round.
    if "retry_at" not in {row["name"] for row in connection.execute("PRAGMA table_info(webhooks)")}:
        connection.execute("ALTER TABLE webhooks ADD COLUMN retry_at TEXT")
    connection.commit()
    return connection


def save(connection: sqlite3.Connection, result: Check) -> int:
    """Store one observation and return its row ID."""
    with connection:
        cursor = connection.execute(
            """INSERT INTO checks (url, checked_at, state, status_code, latency_ms, error)
               VALUES (:url, :checked_at, :state, :status_code, :latency_ms, :error)""",
            asdict(result),
        )
    return cursor.lastrowid


def history(connection: sqlite3.Connection, limit: int = 20, url: str | None = None) -> list[dict]:
    """Return saved observations newest first, optionally for one exact URL."""
    if not 1 <= limit <= 1000:
        raise ValueError("History limit must be between 1 and 1000.")
    if url is not None:
        url = validate_url(url)
        return [
            dict(row) for row in connection.execute(
                "SELECT * FROM checks WHERE url = ? ORDER BY id DESC LIMIT ?", (url, limit)
            )
        ]
    return [
        dict(row) for row in connection.execute(
            "SELECT * FROM checks ORDER BY id DESC LIMIT ?", (limit,)
        )
    ]


def summarize(connection: sqlite3.Connection, last: int = 100, url: str | None = None,
              *, state: str | None = None) -> list[dict]:
    """Summarize the most recent `last` observations for each URL."""
    if type(last) is not int or not 1 <= last <= 10000:
        raise ValueError("Summary sample size must be between 1 and 10000.")
    if state is not None and state not in ("up", "down"):
        raise ValueError("Summary state must be up or down.")
    if url is not None:
        url = validate_url(url)
    where = " WHERE url = ?" if url is not None else ""
    values = (url, last) if url is not None else (last,)
    rows = connection.execute(
        """WITH ranked AS (
            SELECT *, ROW_NUMBER() OVER (PARTITION BY url ORDER BY id DESC) AS position,
                   COUNT(*) OVER (PARTITION BY url) AS stored_checks
            FROM checks""" + where + """
        )
        SELECT url, MAX(stored_checks) AS stored_checks, COUNT(*) AS sampled_checks,
               SUM(state = 'up') AS up_checks, SUM(state = 'down') AS down_checks,
               MAX(CASE WHEN position = 1 THEN id END) AS latest_id,
               MAX(CASE WHEN position = 1 THEN checked_at END) AS latest_checked_at,
               MAX(CASE WHEN position = 1 THEN state END) AS latest_state,
               MAX(CASE WHEN position = 1 THEN error END) AS latest_error,
               MAX(CASE WHEN position = 1 THEN status_code END) AS latest_status_code,
               ROUND(AVG(CASE WHEN state = 'up' THEN latency_ms END), 3) AS up_avg_latency_ms,
               MIN(CASE WHEN state = 'up' THEN latency_ms END) AS up_min_latency_ms,
               MAX(CASE WHEN state = 'up' THEN latency_ms END) AS up_max_latency_ms
        FROM ranked WHERE position <= ? GROUP BY url ORDER BY url""", values,
    ).fetchall()
    return [dict(row) for row in rows if state is None or row["latest_state"] == state]


def add_target(connection: sqlite3.Connection, name: str, url: str, timeout: float = 5.0,
               deadline: float | None = None, alert_webhook: str = routing.DEFAULT) -> dict:
    """Save a new named target without checking it."""
    if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_-]{0,63}", name):
        raise ValueError("Name must be 1–64 letters, digits, hyphens, or underscores, starting with a letter or digit.")
    url = validate_url(url)
    timeout = validate_timeout(timeout)
    deadline = validate_deadline(deadline)
    alert_webhook = validate_alert_webhook(alert_webhook)
    try:
        with connection:
            connection.execute(
                "INSERT INTO targets (name, url, timeout, deadline, alert_webhook) VALUES (?, ?, ?, ?, ?)",
                (name, url, timeout, deadline, alert_webhook))
    except sqlite3.IntegrityError as error:
        raise ValueError(f"A target named '{name}' already exists.") from error
    return {"name": name, "url": url, "timeout": timeout, "deadline": deadline, "alert_webhook": alert_webhook}


def get_target(connection: sqlite3.Connection, name: str) -> dict:
    """Return a saved target by name, or raise ValueError if it doesn't exist."""
    row = connection.execute("SELECT * FROM targets WHERE name = ?", (name,)).fetchone()
    if row is None:
        raise ValueError(f"Target '{name}' does not exist.")
    return dict(row)


def list_targets(connection: sqlite3.Connection) -> list[dict]:
    """Return every saved target, sorted by name."""
    return [dict(row) for row in connection.execute("SELECT * FROM targets ORDER BY name")]


def update_target(connection: sqlite3.Connection, name: str, *,
                  url: str | None = None, timeout: float | None = None,
                  deadline: float | None = None, clear_deadline: bool = False,
                  alert_webhook: str | None = None) -> dict:
    """Change a saved target's URL, timeout, deadline, or alert webhook, keeping settings that aren't given."""
    if deadline is not None and clear_deadline:
        raise ValueError("Choose either a new deadline or clearing it, not both.")
    if url is None and timeout is None and deadline is None and not clear_deadline and alert_webhook is None:
        raise ValueError("Provide a URL, timeout, deadline, or alert webhook to update.")
    if alert_webhook is not None:
        alert_webhook = validate_alert_webhook(alert_webhook)
    if url is not None:
        url = validate_url(url)
    if timeout is not None:
        timeout = validate_timeout(timeout)
    deadline = validate_deadline(deadline)
    with connection:
        cursor = connection.execute(
            """UPDATE targets SET url = COALESCE(?, url), timeout = COALESCE(?, timeout),
               deadline = CASE WHEN ? THEN NULL ELSE COALESCE(?, deadline) END,
               alert_webhook = COALESCE(?, alert_webhook) WHERE name = ?""",
            (url, timeout, clear_deadline, deadline, alert_webhook, name),
        )
        if cursor.rowcount == 0:
            raise ValueError(f"Target '{name}' does not exist.")
        target = get_target(connection, name)
    return target


def remove_target(connection: sqlite3.Connection, name: str) -> dict:
    """Delete a saved target's settings. Its past observations are kept."""
    with connection:
        cursor = connection.execute("DELETE FROM targets WHERE name = ?", (name,))
        if cursor.rowcount == 0:
            raise ValueError(f"Target '{name}' does not exist.")
    return {"removed": name}


def target_deadline(target: dict, override: float | None = None) -> float | None:
    # A deadline given for one run replaces the target's saved default.
    """Return the deadline for one run: the override if given, else the target's saved default."""
    return override if override is not None else target["deadline"]


def run_targets(connection: sqlite3.Connection, workers: int = 4,
                deadline: float | None = None) -> list[dict]:
    """Check every saved target once, `workers` at a time, saving each result on this connection."""
    if type(workers) is not int or not 1 <= workers <= 32:
        raise ValueError("Workers must be between 1 and 32.")
    deadline = validate_deadline(deadline)
    targets = list_targets(connection)
    for target in targets:
        validate_url(target["url"])
        validate_timeout(target["timeout"])
        validate_deadline(target["deadline"])
    if not targets:
        return []
    pool = ThreadPoolExecutor(max_workers=workers)
    results = []
    try:
        pending = {
            pool.submit(probe, target["url"], target["timeout"], target_deadline(target, deadline)): target["name"]
            for target in targets
        }
        for future in as_completed(pending):
            result = future.result()
            identifier = save(connection, result)
            results.append({"target": pending[future], "id": identifier, **asdict(result)})
    finally:
        pool.shutdown(wait=True, cancel_futures=True)
    return sorted(results, key=lambda row: row["target"])


def watch_targets(connection: sqlite3.Connection, *, interval: float = 30.0,
                  count: int | None = None, workers: int = 4, deadline: float | None = None):
    """Yield one batch of results per round, waiting `interval` seconds between rounds."""
    if (isinstance(interval, bool) or not isinstance(interval, (int, float))
            or not math.isfinite(interval) or not 1 <= interval <= 86400):
        raise ValueError("Interval must be between 1 and 86400 seconds.")
    if count is not None and (type(count) is not int or count < 1):
        raise ValueError("Count must be a positive integer.")
    if type(workers) is not int or not 1 <= workers <= 32:
        raise ValueError("Workers must be between 1 and 32.")
    deadline = validate_deadline(deadline)
    cycle = 0
    while True:
        results = run_targets(connection, workers, deadline)
        cycle += 1
        yield {"cycle": cycle, "results": results}
        if not results or (count is not None and cycle >= count):
            return
        sleep(interval)


class ShutdownRequested(BaseException):
    """Raised from the SIGTERM handler to stop the command cleanly."""


def terminate(signum, frame):
    """SIGTERM handler that turns the signal into `ShutdownRequested`."""
    raise ShutdownRequested()


def url_argument(value: str) -> str:
    """Validate a URL option, letting argparse show the reason it was rejected."""
    try:
        return validate_url(value)
    except ValueError as error:
        raise argparse.ArgumentTypeError(str(error)) from error


def parser() -> argparse.ArgumentParser:
    """Build the command-line parser."""
    root = argparse.ArgumentParser(description="Check HTTP endpoints and keep a local history.")
    root.add_argument("--database", type=Path, default=DEFAULT_DATABASE,
                      help="SQLite file to use (default: .local/checks.sqlite3 next to monitor.py)")
    commands = root.add_subparsers(dest="command", required=True)
    check = commands.add_parser("check", help="Check a URL once and save the result")
    check.add_argument("url")
    check.add_argument("--timeout", type=float, default=5.0, help="Socket timeout in seconds (default: 5)")
    check.add_argument("--deadline", type=float, help="Give up on the whole request after this many seconds")
    listing = commands.add_parser("history", help="Show recent saved checks as JSON")
    listing.add_argument("--limit", type=int, default=20, help="Checks to show, 1–1000 (default: 20)")
    listing.add_argument("--url", help="Only show checks for this exact URL")
    summary = commands.add_parser("summary", help="Summarize saved checks per endpoint without making requests")
    summary.add_argument("--last", type=int, default=100, help="Recent checks per URL, 1–10000 (default: 100)")
    summary_target = summary.add_mutually_exclusive_group()
    summary_target.add_argument("--url", help="Only summarize this exact URL")
    summary_target.add_argument("--target", help="Summarize the URL currently saved for this target name")
    summary.add_argument("--state", choices=("up", "down"),
                         help="Only include endpoints whose latest saved check has this state")
    add = commands.add_parser("add", help="Save a named target without making a request")
    add.add_argument("name")
    add.add_argument("url")
    add.add_argument("--timeout", type=float, default=5.0, help="Socket timeout in seconds (default: 5)")
    add.add_argument("--deadline", type=float, help="Default whole-request limit for this target's runs")
    add.add_argument("--alert-webhook", default=routing.DEFAULT, metavar="default|none|URL",
                     help="Where this target's alerts go: the watcher's webhook (default), nowhere, or this URL")
    update = commands.add_parser("update", help="Change a saved target's URL, timeout, deadline, or alert webhook")
    update.add_argument("name")
    update.add_argument("--url", help="Save a new URL")
    update.add_argument("--timeout", type=float, help="Save a new socket timeout")
    update_deadline = update.add_mutually_exclusive_group()
    update_deadline.add_argument("--deadline", type=float, help="Save a new default deadline")
    update_deadline.add_argument("--no-deadline", action="store_true", help="Clear the saved deadline")
    update.add_argument("--alert-webhook", metavar="default|none|URL", help="Save where this target's alerts go")
    commands.add_parser("targets", help="List saved targets")
    remove = commands.add_parser("remove", help="Remove a saved target without deleting its history")
    remove.add_argument("name")
    run = commands.add_parser("run", help="Check one saved target and record the result")
    run.add_argument("name")
    run.add_argument("--deadline", type=float, help="Override the target's saved deadline for this run")
    batch = commands.add_parser("run-all", help="Check all saved targets with bounded concurrency")
    batch.add_argument("--workers", type=int, default=4, help="Concurrent requests, 1–32 (default: 4)")
    batch.add_argument("--deadline", type=float, help="Per-request limit for every target, overriding saved deadlines")
    watch = commands.add_parser("watch", help="Repeat batches until stopped or the count is reached")
    watch.add_argument("--workers", type=int, default=4, help="Concurrent requests, 1–32 (default: 4)")
    watch.add_argument("--interval", type=float, default=30.0, help="Seconds to wait after each batch")
    watch.add_argument("--count", type=int, help="Number of batches; omit to keep watching")
    watch.add_argument("--deadline", type=float, help="Per-request limit for every target, overriding saved deadlines")
    watch.add_argument("--alert-webhook", type=url_argument,
                       help="POST incident openings and resolutions to this URL, unless a target sets its own")
    watch.add_argument("--skip-resolved", action="store_true",
                       help="Skip incidents that resolved before their opening was sent, sending neither event")
    watch.add_argument("--max-backoff", type=float, metavar="SECONDS",
                       help="Wait before retrying a failing webhook: one interval, doubling per failure up to this")
    alert_log = commands.add_parser("alerts", help="Show sent and skipped alerts as JSON")
    alert_log.add_argument("--limit", type=int, default=20, help="Alerts to show, 1–1000 (default: 20)")
    alert_log.add_argument("--pending", action="store_true",
                           help="Show events the next alerting watcher will send instead, oldest first")
    alert_log.add_argument("--skip-resolved", action="store_true",
                           help="With --pending, leave out incidents a watcher with --skip-resolved would skip")
    commands.add_parser("webhooks", help="Show each alert webhook's last success, last failure, and queued alerts")
    incidents = commands.add_parser("incidents", help="Report incidents from saved checks")
    incidents.add_argument("--url", type=url_argument, help="Only report this exact URL")
    incidents.add_argument("--state", choices=("open", "resolved"), help="Only report open or resolved incidents")
    incidents.add_argument("--limit", type=int, default=20, help="Incidents to show, 1–1000 (default: 20)")
    incidents.add_argument("--failures", type=int, default=2, help="Consecutive failures to open an incident")
    incidents.add_argument("--recoveries", type=int, default=2, help="Consecutive successes to resolve it")
    serve = commands.add_parser("serve", help="Serve a dashboard and read-only JSON API for saved results")
    serve.add_argument("--host", default="127.0.0.1", help="Address to listen on (default: 127.0.0.1)")
    serve.add_argument("--port", type=int, default=8080, help="Port to listen on, 0 for any free port (default: 8080)")
    return root


def serve(database: Path, host: str, port: int) -> None:
    """Serve the dashboard and read-only API until interrupted."""
    if type(port) is not int or not 0 <= port <= 65535:
        raise ValueError("Port must be between 0 and 65535.")
    # Create or upgrade the database once so every request can open it read-only.
    connect(database).close()
    with api.make_server(database, host, port, sys.modules[__name__]) as server:
        print(f"Serving dashboard and read-only API on http://{host}:{server.server_port}", file=sys.stderr, flush=True)
        server.serve_forever()


def main(argv: list[str] | None = None) -> int:
    """Run one command and return its exit code."""
    args = parser().parse_args(argv)
    try:
        if args.command == "serve":
            serve(args.database, args.host, args.port)
            return 0
        with closing(connect(args.database)) as connection:
            if args.command == "watch":
                for option, given in (("--skip-resolved", args.skip_resolved),
                                      ("--max-backoff", args.max_backoff is not None)):
                    if given and args.alert_webhook is None and not routing.has_webhooks(connection):
                        raise ValueError(f"{option} only applies to alerts, so it needs --alert-webhook "
                                         "or a target with its own webhook.")
                backoff = None
                if args.max_backoff is not None:
                    backoff = webhooks.Backoff(args.interval, args.max_backoff)
                alert = alerts.Alerter(connection, args.alert_webhook, skip_resolved=args.skip_resolved,
                                       backoff=backoff)
                alert()
                failed = False
                for batch in watch_targets(connection, interval=args.interval, count=args.count,
                                           workers=args.workers, deadline=args.deadline):
                    failed = failed or any(row["state"] == "down" for row in batch["results"])
                    print(json.dumps(batch), flush=True)
                    alert()
                return 1 if failed else 0
            if args.command == "alerts":
                listed = alerts.list_alerts(connection, args.limit, pending=args.pending,
                                            skip_resolved=args.skip_resolved)
                print(json.dumps(listed, indent=2))
                return 0
            if args.command == "webhooks":
                print(json.dumps(alerts.webhook_health(connection), indent=2))
                return 0
            if args.command == "incidents":
                results = list_incidents(
                    connection, url=args.url, state=args.state, limit=args.limit,
                    failures=args.failures, recoveries=args.recoveries,
                )
                print(json.dumps(results, indent=2))
                return 0
            if args.command == "history":
                print(json.dumps(history(connection, args.limit, args.url), indent=2))
                return 0
            if args.command == "summary":
                url = get_target(connection, args.target)["url"] if args.target is not None else args.url
                print(json.dumps(summarize(connection, args.last, url, state=args.state), indent=2))
                return 0
            if args.command == "add":
                target = add_target(connection, args.name, args.url, args.timeout, args.deadline, args.alert_webhook)
                print(json.dumps(target, indent=2))
                return 0
            if args.command == "targets":
                print(json.dumps(list_targets(connection), indent=2))
                return 0
            if args.command == "update":
                target = update_target(connection, args.name, url=args.url, timeout=args.timeout,
                                       deadline=args.deadline, clear_deadline=args.no_deadline,
                                       alert_webhook=args.alert_webhook)
                print(json.dumps(target, indent=2))
                return 0
            if args.command == "remove":
                print(json.dumps(remove_target(connection, args.name), indent=2))
                return 0
            if args.command == "run-all":
                results = run_targets(connection, args.workers, args.deadline)
                print(json.dumps(results, indent=2))
                return 1 if any(row["state"] == "down" for row in results) else 0
            if args.command == "run":
                target = get_target(connection, args.name)
                result = probe(target["url"], target["timeout"], target_deadline(target, args.deadline))
            else:
                result = probe(args.url, args.timeout, args.deadline)
            identifier = save(connection, result)
            print(json.dumps({"id": identifier, **asdict(result)}, indent=2))
            return 0 if result.state == "up" else 1
    except (ValueError, OSError, sqlite3.Error) as error:
        print(f"Error: {error}", file=sys.stderr)
        return 2
    except ShutdownRequested:
        print("Stopped.", file=sys.stderr)
        return 143
    except KeyboardInterrupt:
        print("\nCancelled.", file=sys.stderr)
        return 130


if __name__ == "__main__":
    signal.signal(signal.SIGTERM, terminate)
    raise SystemExit(main())
