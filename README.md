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

Change an existing target without removing it:

```sh
python3 monitor.py update example --timeout 2
python3 monitor.py update example --url https://example.com/health --timeout 3
```

Supply `--url`, `--timeout`, or both. Omitted settings stay unchanged. Updates
print the saved settings as JSON without making a request. Invalid values or
missing names return exit code `2` and leave the configuration unchanged.
Past checks and incidents remain tied to their original URL. A running watcher
loads the updated settings on its next round; a batch already in progress keeps
its original settings.

Remove a target you no longer want to check:

```sh
python3 monitor.py remove example
```

`remove` deletes only the saved configuration and prints `{"removed": "example"}`.
Past checks remain in history. Missing names return exit code `2`. A batch that
already loaded its targets may still finish checking a removed target.

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
lookup or request may delay shutdown.

## Keep watching

```sh
python3 monitor.py watch --interval 30 --workers 4
python3 monitor.py watch --interval 5 --count 4
```

`watch` repeats batches in the foreground. It waits `--interval` seconds after
one batch finishes before starting the next; batches never overlap within one
watcher. The interval must be 1–86,400 seconds. Omit `--count` to keep running,
or set a positive number of batches. An empty target list produces one empty
batch and exits. Run one watcher per database to avoid duplicate checks.

Each round reloads the saved targets, so another terminal can add or remove them
without restarting the watcher. Each completed batch is flushed as one JSON line
with `cycle` and `results` fields. Observations are saved as requests finish,
before the batch output appears. A finite run exits `1` if any round observed a
failure, even if later rounds recover; otherwise it exits `0`.

Ctrl+C exits `130`; SIGTERM exits `143`. While waiting between rounds, shutdown
is immediate. During a batch, queued requests are cancelled and shutdown waits
for active requests. Already committed observations survive. The socket timeout
limitations above also apply here. Storage or configuration errors stop the
watcher with exit code `2`.

## Incident reports

```sh
python3 monitor.py incidents
python3 monitor.py incidents --state open --url https://example.com
python3 monitor.py incidents --failures 3 --recoveries 2 --limit 10
```

By default, two consecutive failed observations open an incident and two
consecutive successful observations resolve it. One success followed by another
failure keeps the same incident open. `opened_at` is the first failure;
`confirmed_at` is the failure that reaches the threshold. `resolved_at` is the
success that confirms recovery. The report includes the matching check IDs.

Reports are reconstructed from saved checks in insertion order, grouped by exact
URL. Checks from every command and target alias count. Changing the thresholds
reinterprets that history; it does not edit records. Results are newest incident
first, with URL and state filters applied before the limit (maximum 1,000).
Thresholds must be between 1 and 100. Reporting succeeds with exit code `0`, even
when incidents are open.

An open incident means recovery has not been confirmed in the saved history.
It does not prove an endpoint is still down: check `last_checked_at`, especially
if a target was removed. Gaps between checks are unknown. Reports scan all
matching observations, so they are intended for local-sized histories. This is
not an alerting system or a measurement of exact outage duration.

## Docker

```sh
docker compose build
docker compose run --rm monitor add example https://example.com --timeout 3
docker compose up -d monitor
docker compose logs -f monitor
```

The service checks saved targets every 30 seconds after each batch finishes.
It runs as a non-root user with a read-only filesystem, except for `/data` and
`/tmp`. The named `monitor-data` volume stores the database. Container recreation
and `docker compose down` preserve it. `docker compose down --volumes` deletes it.
The container database is separate from the default local Python database.

Use another container to inspect or change the same stored data:

```sh
docker compose run --rm monitor history --limit 10
docker compose run --rm monitor incidents --state open
docker compose run --rm monitor remove example
docker compose stop monitor
```

Add at least one target before starting the watcher. It exits when no targets
remain and does not automatically restart; after adding new targets, run
`docker compose up -d monitor` again. No inbound ports are published. Inside the
container, `localhost` refers to the container, so target URLs must be reachable
from its network.

Compose sends SIGTERM and allows 75 seconds before forcing shutdown. Committed
checks remain in the volume, but an active request can still exceed this grace
period because socket timeouts do not bound the whole operation.

Run the container integration check with Docker and Compose available:

```sh
python3 scripts/docker_smoke.py
```

It builds an isolated image, uses a temporary Compose project and volume, and
checks two failures followed by two recoveries against a fixture on an internal
network. It also checks non-root execution, persistence across containers, and
SIGTERM shutdown. Its temporary containers, network, volume, and image are
removed afterward; your regular monitor volume is not used.

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
configured timeouts, failed checks, URL-filtered history, and target removal
without losing past checks.
Batch tests verify overlapping requests, the worker limit, writes on the calling
thread, mixed results, and CLI exit codes. Concurrency is checked with a barrier
rather than by comparing wall-clock timings.

Watch tests cover target reloads, wait placement, streamed output, saved results
after interruption, and real process shutdown using SIGINT and SIGTERM. Incident
tests cover independent streaks, recovery, filtering, persistence, and clock changes.

GitHub Actions runs the suite on Python 3.10 and 3.14 and the Docker integration check.

## Structure

- `probe`: make one request and classify the observation.
- `save` and `history`: persist and retrieve observations using SQLite.
- `add_target`, `get_target`, `list_targets`, `update_target`, and `remove_target`: manage reusable check settings.
- `run_targets`: run requests concurrently and save completed checks on the caller's connection.
- `watch_targets`: repeat batches with a delay and fresh settings each round.
- `list_incidents`: derive failure and recovery episodes from saved observations.
- `main`: parse commands, print JSON, and return exit codes.

SQLite keeps the initial local workflow reproducible. A future shared service
would require an explicit concurrency, access, and network-boundary design.

## Development direction

The current release runs individual checks, batches, and a foreground watcher,
and derives incident reports from their history. It has no alerts, dashboard,
or hosted API, and does not calculate uptime percentages from sparse observations.

Next milestones:

1. Add an explicit total-request deadline.
2. Add an API and a small dashboard for viewing results.
3. Add alert deduplication and measured reliability tests.
