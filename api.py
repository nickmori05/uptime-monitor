"""Read-only HTTP API and dashboard for saved results."""

import base64
from contextlib import closing
import hashlib
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
from pathlib import Path
import re
import sqlite3
import sys
from urllib.parse import parse_qs, urlsplit

import alerts
from incidents import list_incidents
import routing

DASHBOARD = Path(__file__).resolve().with_name("dashboard.html")


class Query:
    """Parse a query string, rejecting unknown or repeated parameters."""

    def __init__(self, raw: str, allowed: tuple[str, ...]):
        try:
            values = parse_qs(raw, keep_blank_values=True, strict_parsing=bool(raw))
        except ValueError as error:
            raise ValueError("Malformed query string.") from error
        unknown = sorted(set(values) - set(allowed))
        if unknown:
            raise ValueError(f"Unknown query parameter: {unknown[0]}.")
        repeated = sorted(name for name, items in values.items() if len(items) > 1)
        if repeated:
            raise ValueError(f"Query parameter {repeated[0]} was given more than once.")
        self.values = {name: items[0] for name, items in values.items()}

    def text(self, name: str) -> str | None:
        return self.values.get(name)

    def integer(self, name: str, default: int) -> int:
        value = self.values.get(name)
        if value is None:
            return default
        if not re.fullmatch(r"-?[0-9]{1,9}", value):
            raise ValueError(f"{name} must be a whole number.")
        return int(value)


def routes(store) -> dict:
    # `store` is the monitor module, passed in so this file doesn't import it back.
    """Map each API path to its allowed parameters and handler."""
    def summary(connection, query):
        url, target = query.text("url"), query.text("target")
        if url is not None and target is not None:
            raise ValueError("Choose either url or target, not both.")
        if target is not None:
            url = store.get_target(connection, target)["url"]
        return store.summarize(connection, query.integer("last", 100), url, state=query.text("state"))

    def incidents(connection, query):
        url = query.text("url")
        return list_incidents(
            connection, url=store.validate_url(url) if url is not None else None,
            state=query.text("state"), limit=query.integer("limit", 20),
            failures=query.integer("failures", 2), recoveries=query.integer("recoveries", 2),
        )

    return {
        "/api/targets": ((), lambda connection, query: [routing.public(target)
                                                         for target in store.list_targets(connection)]),
        "/api/history": (("limit", "url"), lambda connection, query: store.history(
            connection, query.integer("limit", 20), query.text("url"))),
        "/api/summary": (("url", "target", "last", "state"), summary),
        "/api/incidents": (("url", "state", "limit", "failures", "recoveries"), incidents),
        "/api/webhooks": ((), lambda connection, query: [routing.public_webhook(row)
                                                          for row in alerts.webhook_health(connection)]),
    }


def read_only(database: Path) -> sqlite3.Connection:
    # Opening read-only guarantees a request can never change saved history.
    """Open the database so that no request can change it."""
    connection = sqlite3.connect(f"{database.resolve().as_uri()}?mode=ro", uri=True)
    connection.row_factory = sqlite3.Row
    return connection


def dashboard_headers(page: str) -> dict:
    # Only the page's own inline script and style may run, and it may only call this server.
    """Build security headers that allow only the page's own inline script and style."""
    def hashes(tag):
        return " ".join(
            "'sha256-" + base64.b64encode(hashlib.sha256(body.encode()).digest()).decode() + "'"
            for body in re.findall(rf"<{tag}>(.*?)</{tag}>", page, re.DOTALL))
    policy = (f"default-src 'none'; script-src {hashes('script')}; style-src {hashes('style')}; "
              "connect-src 'self'; base-uri 'none'; form-action 'none'; frame-ancestors 'none'")
    return {"Content-Security-Policy": policy, "X-Content-Type-Options": "nosniff",
            "Referrer-Policy": "no-referrer"}


def make_server(database: Path, host: str, port: int, store) -> ThreadingHTTPServer:
    """Create (but don't start) the HTTP server for the API and dashboard."""
    table = routes(store)
    # Read once on startup so a missing page fails `serve` instead of every request.
    page = DASHBOARD.read_text(encoding="utf-8")
    page_headers = dashboard_headers(page)

    class Handler(BaseHTTPRequestHandler):
        server_version = "uptime-monitor/0.1"

        def do_GET(self):
            parts = urlsplit(self.path)
            if parts.path == "/":
                try:
                    Query(parts.query, ())
                except ValueError as error:
                    return self.reply(400, {"error": str(error)})
                return self.respond(200, page.encode(), "text/html; charset=utf-8", page_headers)
            route = table.get(parts.path)
            if route is None:
                return self.reply(404, {"error": f"No endpoint at {parts.path}."})
            allowed, handler = route
            try:
                query = Query(parts.query, allowed)
                with closing(read_only(database)) as connection:
                    body = handler(connection, query)
            except ValueError as error:
                return self.reply(400, {"error": str(error)})
            except sqlite3.Error as error:
                print(f"Storage error: {error}", file=sys.stderr)
                return self.reply(500, {"error": "The database could not be read."})
            self.reply(200, body)

        def refuse(self):
            self.reply(405, {"error": "The API is read-only; use GET."}, {"Allow": "GET"})

        do_POST = do_PUT = do_PATCH = do_DELETE = refuse

        def reply(self, status: int, body, headers: dict | None = None):
            self.respond(status, json.dumps(body, indent=2).encode(), "application/json", headers)

        def respond(self, status: int, payload: bytes, content_type: str, headers: dict | None = None):
            self.send_response(status)
            self.send_header("Content-Type", content_type)
            self.send_header("Content-Length", str(len(payload)))
            self.send_header("Cache-Control", "no-store")
            for name, value in (headers or {}).items():
                self.send_header(name, value)
            self.end_headers()
            self.wfile.write(payload)

        def log_message(self, *_args):
            pass

    server = ThreadingHTTPServer((host, port), Handler)
    server.daemon_threads = True
    return server
