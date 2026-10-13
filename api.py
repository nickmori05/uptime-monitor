from contextlib import closing
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
from pathlib import Path
import re
import sqlite3
import sys
from urllib.parse import parse_qs, urlsplit

from incidents import list_incidents


class Query:
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
        "/api/targets": ((), lambda connection, query: store.list_targets(connection)),
        "/api/history": (("limit", "url"), lambda connection, query: store.history(
            connection, query.integer("limit", 20), query.text("url"))),
        "/api/summary": (("url", "target", "last", "state"), summary),
        "/api/incidents": (("url", "state", "limit", "failures", "recoveries"), incidents),
    }


def read_only(database: Path) -> sqlite3.Connection:
    # Opening read-only guarantees a request can never change saved history.
    connection = sqlite3.connect(f"{database.resolve().as_uri()}?mode=ro", uri=True)
    connection.row_factory = sqlite3.Row
    return connection


def make_server(database: Path, host: str, port: int, store) -> ThreadingHTTPServer:
    table = routes(store)

    class Handler(BaseHTTPRequestHandler):
        server_version = "uptime-monitor/0.1"

        def do_GET(self):
            parts = urlsplit(self.path)
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
            payload = json.dumps(body, indent=2).encode()
            self.send_response(status)
            self.send_header("Content-Type", "application/json")
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
