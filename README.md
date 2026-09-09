# Uptime Monitor

Check an HTTP endpoint, record its response time, and inspect its history.
The first version is a local Python command-line tool with SQLite storage and
no external package dependencies.

## Run

Requires Python 3.10 or later.

```sh
git clone https://github.com/nickmori05/uptime-monitor.git
cd uptime-monitor
python3 monitor.py check https://example.com
python3 monitor.py history --limit 5
```

Each check prints JSON with its UTC timestamp, HTTP status, response time in
milliseconds, and `up` or `down` state. HTTP 200–299 is `up`. Other HTTP statuses,
connection failures, and timeouts are `down`. Redirects are recorded without
following them, so checking a redirecting URL records that endpoint's response.

Checks use GET and stop after receiving response headers. They do not verify
response-body content or the health of dependencies behind the endpoint.

Exit codes: `0` for an up endpoint, `1` for a recorded down endpoint, and `2` for
invalid input or a local storage error. Failed checks are retained in history.

```sh
python3 monitor.py check https://example.com --timeout 3
```

`--timeout` sets the socket timeout, from greater than 0 to at most 60 seconds.
It is not a guaranteed deadline for the whole operation: DNS resolution and
multiple network steps may take additional time.

## Storage

Results are saved in `.local/checks.sqlite3` next to the script. The database is
excluded from Git. History is returned in newest-record-first order. The default
limit is 20 records; the maximum is 1,000.

Choose a separate database for experiments:

```sh
python3 monitor.py --database .local/demo.sqlite3 check https://example.com
python3 monitor.py --database .local/demo.sqlite3 history
```

## Tests

```sh
python3 -m unittest discover -s tests -v
```

The test suite uses a temporary local HTTP server and temporary databases.
It checks HTTP success, server failure, redirects, timeouts, persistence,
history ordering, input validation, and command-line exit codes. Connection
failure is injected so that it does not depend on external networking.

GitHub Actions runs the suite on Python 3.10 and 3.14.

## Structure

- `probe`: make one request and classify the observation.
- `save` and `history`: persist and retrieve observations using SQLite.
- `main`: parse commands, print JSON, and return exit codes.

SQLite keeps the initial local workflow reproducible. A future shared service
would require an explicit concurrency, access, and network-boundary design.

## Development direction

The current release runs one check at a time, when invoked. It has no scheduled
checks, alerts, dashboard, or hosted API, and it does not calculate uptime
percentages from sparse manual observations.

Planned milestones:

1. Give saved monitors names and retain their URL and check settings.
2. Schedule checks with bounded concurrency and predictable shutdown.
3. Model incidents from consecutive failures and recoveries.
4. Add an API and a small dashboard for viewing results.
5. Add alert deduplication and measured reliability tests.

Each milestone should ship with a working example, relevant tests, and an
explanation of its design choices. Start with one complete feature at a time.
