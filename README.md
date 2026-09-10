# Uptime Monitor

[![Tests](https://github.com/nickmori05/uptime-monitor/actions/workflows/tests.yml/badge.svg)](https://github.com/nickmori05/uptime-monitor/actions/workflows/tests.yml)

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

Exit codes: `0` for an up endpoint, `1` for a recorded down endpoint, `2` for
invalid input or a local storage error, and `130` for cancellation. Failed checks
are retained in history.

```sh
python3 monitor.py check https://example.com --timeout 3
```

`--timeout` sets the socket timeout, from greater than 0 to at most 60 seconds.
It is not a guaranteed deadline for the whole operation: DNS resolution and
multiple network steps may take additional time.

## Named targets

Save an endpoint and its timeout once, then run checks by name:

```sh
python3 monitor.py add example https://example.com --timeout 3
python3 monitor.py targets
python3 monitor.py run example
python3 monitor.py history --url https://example.com --limit 5
```

`add` saves configuration without sending a request. `run` makes one check,
persists the result, and uses the same exit codes as `check`. Names are case
sensitive and contain 1–64 ASCII letters, digits, underscores, or hyphens;
the first character must be a letter or digit. Duplicate names are rejected
without changing the existing configuration. History filtering matches the
exact saved URL, including its path and query string.

## Check all saved targets

```sh
python3 monitor.py add example-home https://example.com --timeout 3
python3 monitor.py add example-missing https://example.com/missing --timeout 3
python3 monitor.py run-all --workers 2
```

`run-all` checks each saved target once. It starts at most `--workers` requests
at a time (default 4, maximum 32), using each target's stored timeout. Database
writes happen on the calling thread and each finished observation is committed
separately. Output is a JSON array sorted by target name; each entry includes
the target name and the saved observation ID.

The command returns `0` if all targets are up or no targets exist, `1` if any
target is down, and `2` for invalid configuration or storage errors. A down
endpoint does not prevent the other results from being saved. Configuration is
validated before any batch requests start.

On Ctrl+C, queued requests are cancelled and the command returns `130` after
active requests finish. Already committed observations remain in history.
The socket timeout is still not a whole-operation deadline, so an active DNS
lookup or request may delay shutdown. There is no recurring schedule yet.

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
Target tests cover persisted settings, duplicate names, invalid configuration,
configured timeouts, failed checks, and URL-filtered history.
Batch tests verify overlapping requests, the worker limit, writes on the calling
thread, mixed results, and CLI exit codes. Concurrency is checked with a barrier
rather than by comparing wall-clock timings.

GitHub Actions runs the suite on Python 3.10 and 3.14.

## Structure

- `probe`: make one request and classify the observation.
- `save` and `history`: persist and retrieve observations using SQLite.
- `add_target`, `get_target`, and `list_targets`: manage reusable check settings.
- `run_targets`: run requests concurrently and save completed checks on the caller's connection.
- `main`: parse commands, print JSON, and return exit codes.

SQLite keeps the initial local workflow reproducible. A future shared service
would require an explicit concurrency, access, and network-boundary design.

## Development direction

The current release runs individual checks or one batch when invoked. It has no scheduled
checks, alerts, dashboard, or hosted API, and it does not calculate uptime
percentages from sparse manual observations.

Next milestones:

1. Schedule repeated batches and add an explicit total-request deadline.
2. Model incidents from consecutive failures and recoveries.
3. Add an API and a small dashboard for viewing results.
4. Add alert deduplication and measured reliability tests.

Each milestone should ship with a working example, relevant tests, and an
explanation of its design choices. Start with one complete feature at a time.
