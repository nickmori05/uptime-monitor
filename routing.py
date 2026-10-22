"""Work out where each URL's alerts go from the saved targets' alert settings.

A target's `alert_webhook` is `default` (the watcher's own `--alert-webhook`),
`none`, or a webhook URL of its own. Alerts belong to a URL, so a URL checked by
several targets is owed an alert on each of their routes, once per distinct route.
"""

import sqlite3
from urllib.parse import urlsplit

DEFAULT = "default"
NONE = "none"


def routes_by_url(connection: sqlite3.Connection) -> dict[str, list[str]]:
    """Map each saved target's URL to its targets' alert settings, in target-name order."""
    table = {}
    for row in connection.execute("SELECT url, alert_webhook FROM targets ORDER BY name"):
        table.setdefault(row["url"], []).append(row["alert_webhook"])
    return table


def owed(table: dict[str, list[str]], url: str, webhook: str | None = None) -> list[str]:
    """Return the distinct routes one URL's alerts go to, in a stable order.

    A URL no target checks any more (or that only `check` saw) goes to the default
    route. A target webhook that is the watcher's own `webhook` is the default route,
    so it is sent once.
    """
    routes = []
    for route in table.get(url, [DEFAULT]):
        if route == webhook:
            route = DEFAULT
        if route != NONE and route not in routes:
            routes.append(route)
    return routes


def has_webhooks(connection: sqlite3.Connection) -> bool:
    """Return whether any saved target has a webhook of its own."""
    row = connection.execute("SELECT 1 FROM targets WHERE alert_webhook NOT IN (?, ?) LIMIT 1", (DEFAULT, NONE))
    return row.fetchone() is not None


def describe(route: str) -> str:
    """Name a route in log messages without printing a webhook's path or query, which may hold a token."""
    return "" if route == DEFAULT else f" to {urlsplit(route).hostname}"


def public(target: dict) -> dict:
    """Return a target for the unauthenticated API, showing a target's own webhook only as `webhook`."""
    if target["alert_webhook"] in (DEFAULT, NONE):
        return target
    return {**target, "alert_webhook": "webhook"}
