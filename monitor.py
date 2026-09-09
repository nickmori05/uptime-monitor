"""Check an HTTP endpoint and keep a local history. Python 3.10+."""

import argparse
from contextlib import closing
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
import http.client
import json
import math
from pathlib import Path
import sqlite3
import sys
from time import perf_counter
from urllib.error import HTTPError, URLError
from urllib.parse import urlsplit
from urllib.request import HTTPRedirectHandler, Request, build_opener


DEFAULT_DATABASE = Path(__file__).resolve().parent / ".local" / "checks.sqlite3"


@dataclass(frozen=True)
class Check:
    url: str
    checked_at: str
    state: str
    status_code: int | None
    latency_ms: float
    error: str | None


class NoRedirects(HTTPRedirectHandler):
    def redirect_request(self, request, response, code, message, headers, new_url):
        # Measure this exact endpoint, not another endpoint named in Location.
        return None


def validate_url(value: str) -> str:
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


def probe(url: str, timeout: float = 5.0) -> Check:
    url = validate_url(url)
    if not math.isfinite(timeout) or not 0 < timeout <= 60:
        raise ValueError("Timeout must be greater than 0 and at most 60 seconds.")
    started_at = datetime.now(timezone.utc).isoformat()
    started = perf_counter()
    status = None
    error = None
    request = Request(url, headers={"User-Agent": "uptime-monitor/0.1"}, method="GET")
    try:
        with build_opener(NoRedirects()).open(request, timeout=timeout) as response:
            status = response.status
            # Headers are sufficient for this first check. Do not download a
            # potentially large or indefinitely streaming response body.
    except HTTPError as response:
        status = response.code
        response.close()
    except TimeoutError:
        error = "timeout"
    except URLError as failure:
        error = "timeout" if isinstance(failure.reason, TimeoutError) else "connection_error"
    except (OSError, http.client.HTTPException):
        error = "connection_error"
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
    connection.commit()
    return connection


def save(connection: sqlite3.Connection, result: Check) -> int:
    with connection:
        cursor = connection.execute(
            """INSERT INTO checks (url, checked_at, state, status_code, latency_ms, error)
               VALUES (:url, :checked_at, :state, :status_code, :latency_ms, :error)""",
            asdict(result),
        )
    return cursor.lastrowid


def history(connection: sqlite3.Connection, limit: int = 20) -> list[dict]:
    if not 1 <= limit <= 1000:
        raise ValueError("History limit must be between 1 and 1000.")
    return [
        dict(row) for row in connection.execute(
            "SELECT * FROM checks ORDER BY id DESC LIMIT ?", (limit,)
        )
    ]


def parser() -> argparse.ArgumentParser:
    root = argparse.ArgumentParser(description=__doc__)
    root.add_argument("--database", type=Path, default=DEFAULT_DATABASE)
    commands = root.add_subparsers(dest="command", required=True)
    check = commands.add_parser("check", help="Check a URL once and save the result")
    check.add_argument("url")
    check.add_argument("--timeout", type=float, default=5.0, help="Socket timeout in seconds (default: 5)")
    listing = commands.add_parser("history", help="Show recent saved checks as JSON")
    listing.add_argument("--limit", type=int, default=20)
    return root


def main(argv: list[str] | None = None) -> int:
    args = parser().parse_args(argv)
    try:
        with closing(connect(args.database)) as connection:
            if args.command == "history":
                print(json.dumps(history(connection, args.limit), indent=2))
                return 0
            result = probe(args.url, args.timeout)
            identifier = save(connection, result)
            print(json.dumps({"id": identifier, **asdict(result)}, indent=2))
            return 0 if result.state == "up" else 1
    except (ValueError, OSError, sqlite3.Error) as error:
        print(f"Error: {error}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
