# Uptime Monitor

[![Tests](https://github.com/nickmori05/uptime-monitor/actions/workflows/tests.yml/badge.svg)](https://github.com/nickmori05/uptime-monitor/actions/workflows/tests.yml)

A small HTTP uptime monitor written in standard-library Python. It checks
endpoints once or on an interval, stores every observation in SQLite, rebuilds
incidents and summaries from that history, sends a webhook once per incident
change (to a webhook per target if you like), and serves the results through a read-only JSON API and dashboard. It
runs from a clone with no packages to install, or as a hardened Docker Compose
service.

## Quick start

Requires Python 3.10 or later.

```sh
git clone https://github.com/nickmori05/uptime-monitor.git
cd uptime-monitor
python3 monitor.py check https://example.com
python3 monitor.py add example https://example.com --timeout 3
python3 monitor.py watch --interval 5 --count 2
python3 monitor.py summary
python3 monitor.py serve
```

Then open <http://127.0.0.1:8080/> for the dashboard and press Ctrl+C to stop
`serve`. Results are saved in `.local/checks.sqlite3` next to `monitor.py`;
pass `--database PATH` before the command to use another file.

## Usage

Every command prints JSON. Run `python3 monitor.py COMMAND --help` for all
options.

| Command | What it does |
| --- | --- |
| `check URL` | Check a URL once and save the result. |
| `add`, `update`, `remove`, `targets` | Manage named targets with saved settings. |
| `run NAME`, `run-all` | Check one or every saved target once. |
| `watch` | Check every saved target in repeated rounds, optionally sending alerts. |
| `history`, `summary`, `incidents`, `uptime`, `alerts` | Report on saved results without making requests. |
| `serve` | Serve the read-only JSON API and dashboard. |

### Checks

```sh
python3 monitor.py check https://example.com --timeout 3 --deadline 10
python3 monitor.py history --limit 5
```

A check sends one GET, stops after the response headers, and records the UTC
time, status code, latency in milliseconds, and `up` or `down`. Only `2xx` is
`up`; other statuses, connection errors, and timeouts are `down`. Redirects are
recorded, not followed. Response bodies and downstream dependencies are not
checked.

`--timeout` (default 5) is the socket timeout. It doesn't bound DNS lookups or
the whole request, so `--deadline` adds a limit on DNS, connecting, and waiting
for headers. A check that passes its deadline is saved as `down` with the error
`deadline_exceeded`. Both accept more than 0 and at most 60 seconds.

Exit codes are shared by all commands: `0` success (or an `up` check), `1` a
`down` check, `2` invalid input or a storage error, `130` Ctrl+C, and `143`
SIGTERM.

### Targets

```sh
python3 monitor.py add example https://example.com --timeout 3 --deadline 10
python3 monitor.py update example --url https://example.com/health
python3 monitor.py update example --no-deadline
python3 monitor.py update example --alert-webhook https://hooks.example.com/team
python3 monitor.py run example
python3 monitor.py targets
python3 monitor.py remove example
```

A target saves a URL, a timeout, an optional default deadline, and where its
alerts go (see [Routing alerts per target](#routing-alerts-per-target)) under a
name (1–64 letters, digits, `-` or `_`, starting with a letter or digit). `add` and
`update` never make a request. Removing or re-pointing a target keeps its past
checks, which stay tied to the URL they were made against. A `--deadline` given
to `run`, `run-all`, or `watch` overrides saved deadlines for that run only.

### Batches and the watcher

```sh
python3 monitor.py run-all --workers 4
python3 monitor.py watch --interval 30 --deadline 15
```

`run-all` checks every saved target once, at most `--workers` (1–32) at a time,
and prints the results sorted by target name. It exits `1` if any target is down.
Each result is committed as soon as it arrives, so a failure or Ctrl+C keeps
everything already saved.

`watch` repeats `run-all`, waiting `--interval` seconds (1–86,400) after each
round, and prints one JSON line per round. It reloads targets every round, so
other terminals can add, update, or remove them while it runs. `--count N`
stops after N rounds, and the exit code is `1` if any round saw a failure. It
stops when no targets remain. Run one watcher per database.

### Incidents and summaries

```sh
python3 monitor.py incidents --state open
python3 monitor.py incidents --failures 3 --recoveries 2 --limit 10
python3 monitor.py summary --target example --last 50
python3 monitor.py summary --state down
```

An incident opens after `--failures` consecutive failed checks of the same URL
(default 2) and resolves after `--recoveries` consecutive successes (default 2).
With the defaults, one success between failures keeps it open. Reports are
rebuilt from saved checks in insertion order every time, so changing the
thresholds reinterprets history without editing it.

Checks are samples, so an incident's exact length is unknown, but its checks
bound it. Each incident reports `min_downtime_seconds`, from its first failed
check to its last (`last_failure_at`), and `max_downtime_seconds`, from the last
success before it (`previous_success_at`) to the first check of the run of
successes that resolved it (`recovery_started_at`). The real outage lies between
the two. For example, with checks every 30 seconds, an incident with three failed
checks reports 60 and 120. The longest is `null` while the incident is open, and
when history starts with the failure. Successes inside an incident that weren't
enough to resolve it count as part of it, as they do for `failed_checks`. If the
clock is set back during an incident, a span that would be negative is reported
as 0. Alerts carry the same fields in their `incident` object.

`summary` reports, per URL, the latest result and the up/down counts and
latency (successful checks only) over the last `--last` checks (default 100).
`--state` filters by each URL's latest result. Both commands describe what was
observed, not live health or an uptime percentage; gaps between checks are
unknown.

### Uptime

```sh
python3 monitor.py uptime
python3 monitor.py uptime --target example --hours 168
python3 monitor.py uptime --until 2026-10-27T00:00:00+00:00 --hours 24 --max-gap 120
```

`uptime` turns incidents into an uptime range for each URL over a window of
`--hours` (default 24, at most a year) ending `--until` (an ISO 8601 time with a
timezone; default now). Like the incident bounds, it's a range because checks
are samples: `min_downtime_seconds` counts only the time between each
incident's first and last failed check, and `max_downtime_seconds` counts from
the last success before each incident to the start of its recovery. From those,
`min_uptime_percent` and `max_uptime_percent` bracket the real figure. Both are
rounded down to three decimals, so possible downtime never shows as 100%.

Time nobody checked can't be called up. Any stretch where a URL's checks were
more than `--max-gap` seconds apart (default 300), and the time before its first
check, is reported as `unobserved_seconds` and counts toward the longest
downtime but not the shortest. So does the time after its last check, so a URL
that stopped being checked has a falling minimum. Set `--max-gap` above the
watcher's interval plus its deadline. Overlapping spans are counted once, an
incident that started before the window or is still open counts only its part
inside, and `--failures` and `--recoveries` work as they do for `incidents`.

The report lists URLs with a check or an incident in the window, sorted by URL,
along with `checks` and `incidents` counts for the window. `--url` or `--target`
reports one URL even if nothing is known about it, which gives 0 to 100%.

### Alerts

```sh
python3 monitor.py watch --interval 30 --alert-webhook https://hooks.example.com/uptime
python3 monitor.py watch --interval 30 --alert-webhook https://hooks.example.com/uptime --skip-resolved
python3 monitor.py alerts
python3 monitor.py alerts --pending
python3 monitor.py alerts --pending --skip-resolved
```

With `--alert-webhook`, the watcher POSTs one JSON object after each round for
every new incident event, using the default thresholds:

```json
{"event": "opened", "url": "https://example.com", "idempotency_key": "5e57d713...", "text": "Incident opened for ...", "incident": {"...": "..."}}
```

`event` is `opened` or `resolved`, and `incident` matches the `incidents`
report. Each event is sent once: sent events are saved in the database, so
restarts, flapping, and further failures don't repeat them, and several targets
for one URL share alerts. A delivery that fails (non-`2xx`, redirect, connection
error, or no answer within about 10 seconds) is logged to stderr and retried
after the next round, in order (per webhook, when targets have their own).

A watcher killed after the receiver accepts an alert but before saving that it
did sends the alert again when it restarts. The resent copy has the same
`idempotency_key`, which is also sent as the `Idempotency-Key` header, so a
receiver that must not repeat an action can drop any key it has already seen.
The key is 32 hex characters derived from the URL, the incident's first failed
check (its ID and time), and the event. It differs between the opening and the
resolution, and between databases, and it doesn't change when the rest of the
`incident` object does (a resent copy reports later checks). `alerts` and
`alerts --pending` show each event's key, for matching against the receiver's
log.

The first time a watcher alerts (with `--alert-webhook`, or with a target that
has its own webhook), events already in the history are recorded as `skipped`
instead of sent, so turning alerts on doesn't
replay old incidents. From then on nothing is skipped: events from checks made
while no alerting watcher was running (it was stopped, ran without the flag, or
checks came from `run` or `run-all`) are sent when the next alerting watcher
starts, oldest first, before its first round. `alerts` lists sent and skipped
events, newest first, and `alerts --pending` lists the events the next alerting
watcher will send, in order.

After a long gap that catch-up can include incidents that are already over,
sent as an opening followed straight away by its resolution. With
`--skip-resolved`, the watcher instead records an incident as `skipped`, sending
neither event, when it resolved before its opening was sent. That covers
incidents that came and went while no alerting watcher was running and ones
that resolved while the webhook was failing. A resolution is still sent
whenever its opening was, so a receiver never hears about an incident opening
without hearing it close; incidents still open are sent as usual. The option
applies to that watcher run only. `alerts --pending --skip-resolved` previews
what such a watcher would send.

#### Routing alerts per target

```sh
python3 monitor.py add payments https://pay.example.com/health --alert-webhook https://hooks.example.com/payments
python3 monitor.py add batch https://batch.example.com/health --alert-webhook none
python3 monitor.py update payments --alert-webhook default
```

Each target's `alert_webhook` is `default` (the watcher's own `--alert-webhook`,
which is what every target had before), `none`, or a webhook URL of its own.
Alerts belong to a URL, so a URL checked by several targets alerts each of
their distinct webhooks once, and is only silent when all of them say `none`;
a URL no target checks (one seen only by `check`, or whose target was removed)
uses the default. Routes are read when an alert is sent, so changing a target
affects alerts that haven't gone out yet.

Each webhook keeps its own queue. A webhook that fails holds up only the alerts
owed to it, which are retried in order after the next round, while other
webhooks carry on. A webhook that accepted an alert isn't sent it again because
another one is still failing. An alert is listed in `alerts` once every
webhook it is owed has accepted it, and `alerts --pending` shows each pending
alert's remaining `routes`. An alert owed nowhere is recorded as `skipped`, so
turning a target's alerts back on doesn't replay it.

A watcher started without `--alert-webhook` still sends alerts to targets that
have their own webhook, starting the first round that one exists; alerts owed
to `default` wait for a watcher that has one. With `--skip-resolved`, an
incident is only skipped if its opening reached no webhook at all, so every
webhook that heard it open also hears it close.

### API and dashboard

```sh
python3 monitor.py serve --port 8080
curl http://127.0.0.1:8080/api/summary
curl 'http://127.0.0.1:8080/api/uptime?target=example&hours=168'
curl 'http://127.0.0.1:8080/api/history?limit=5&url=https%3A%2F%2Fexample.com'
```

`serve` listens on `127.0.0.1:8080` (`--port 0` picks a free port). It answers
GET requests for `/api/targets`, `/api/history`, `/api/summary`,
`/api/incidents`, and `/api/uptime`, which return the same JSON as the matching
commands and accept their filters as query parameters (`max_gap` for
`--max-gap`). URLs and `until` must be percent-encoded, since a bare `+` in a
timezone reads as a space. The one
difference is that `/api/targets` shows a target's own webhook only as
`"webhook"`, since webhook URLs often carry a secret token. Invalid or
unknown parameters return `400`, unknown paths `404`, and other methods `405`.

`/` serves `dashboard.html`: a single static page that lists each target's
latest result, recent up/down counts, average latency, and open incidents, and
refreshes every 30 seconds. It has no external resources, inserts saved values
only as text, and is served with a strict Content Security Policy.

There is no authentication or TLS. Keep the loopback default unless the network
is trusted, because anyone who can reach the port can read every saved URL.

### Docker

```sh
docker compose build
docker compose run --rm monitor add example https://example.com --timeout 3
docker compose up -d monitor
docker compose logs -f monitor
docker compose run --rm monitor incidents --state open
```

The `monitor` service runs `watch --interval 30 --deadline 20` as a non-root
user with a read-only filesystem, no capabilities, and no published ports. The
database lives in the `monitor-data` volume, which survives `docker compose
down` but not `down --volumes`. Add targets before starting it; the watcher exits
when none remain. Compose allows 75 seconds for SIGTERM shutdown, which the
20-second deadline stays well inside. Inside the container, `localhost` is the
container itself. To send alerts, add `--alert-webhook URL` to the service
`command` in `compose.yml`, or give targets their own webhooks.

## Design

- `monitor.py`: the command line, request probing, SQLite storage, targets,
  batches, and the watcher.
- `incidents.py`: rebuilds incidents from saved checks.
- `uptime.py`: bounds each URL's uptime over a window from its incidents and
  the gaps between its checks.
- `alerts.py`: finds unsent incident events and delivers them to webhooks.
- `routing.py`: works out which webhooks each URL's alerts go to.
- `api.py` and `dashboard.html`: the read-only HTTP API and the static dashboard.

All state lives in one SQLite file with five tables: `checks` (one row per
observation), `targets`, `alerts` (one row per sent or skipped incident event,
unique per event), `deliveries` (one row per webhook route an event reached),
and `alerting` (a single row saving the last check from before alerts were
first enabled; every event after it is owed an alert).
Incidents and summaries are derived from `checks` on demand rather than stored,
and checks are ordered by row ID rather than by timestamp, so a clock change
can't reorder history. `connect` creates missing tables and columns, so
databases from earlier versions upgrade in place.

Requests run on worker threads while the calling thread does every database
write, which keeps SQLite on one connection. Deadlines (and webhook deliveries)
run the request on a daemon thread that is abandoned when time runs out, since
a hung DNS lookup can't be cancelled; the abandoned thread finishes on its own
socket timeout. The API opens the database read-only for each request, so it
can run beside a watcher on the same file.

## Testing

```sh
python3 -m unittest discover -s tests -v
python3 scripts/reliability.py
python3 scripts/docker_smoke.py
```

The unit tests use a temporary local HTTP server and temporary databases, with
connection failures and hung lookups injected, so they need no network access.
They cover checks and deadlines, targets, batches and their concurrency limit,
the watcher and its signal handling, incidents and their downtime bounds,
summaries, alerts, webhook delivery, catching up on missed alerts, skipping
incidents that already resolved, routing alerts per target, and idempotency
keys, the API, the dashboard's headers and policy, and upgrading older
databases.

`scripts/docker_smoke.py` needs Docker with Compose. It builds an isolated image
and project, then checks failure and recovery against a fixture on an internal
network: non-root execution, persistence across containers, an incident's
downtime bounds, SIGTERM shutdown,
deadlines, webhook alerts sent once across restarts with matching keys in the
header, body, and `alerts` log, the API and dashboard
served from another container, an alert for an incident that opened while
no watcher was running, `--skip-resolved` skipping one that opened and
resolved while no watcher was running, and a watcher with no webhook of its own
sending a target's alert to that target's webhook while a muted target stays
silent. Everything it creates is removed afterward.

GitHub Actions runs `ruff check`, the unit tests and the reliability report on
Python 3.10 and 3.14, and the Docker smoke test. Lint settings are in
`pyproject.toml`.

### Measured reliability

`scripts/reliability.py` runs seeded scenarios against the real monitor, with
faults injected by local servers (`tests/faults.py`), and prints one JSON report
per scenario. It exits `1` if any guarantee fails; `--seed` and `--rounds`
change the run. Expected incidents come from a separate implementation of the
incident rules in `tests/scenarios.py`, which the tests cross-check against
`incidents.py`. The receiver also measures what deduplicating on
`Idempotency-Key` would leave: alerts still repeated, alerts sharing a key, and
headers that don't match the body. The unit tests run the same scenarios at a
smaller size, and also confirm that the probe and alert scenarios fail when a
bug is injected (ignored deadlines; dropped, repeated, or reordered alerts; one
key for every alert; skipping too few or too many incidents; ignoring routes; or
one failing webhook holding up the others).

| Scenario | What it does | Measured (seed 1, defaults) |
| --- | --- | --- |
| Probe faults | 8 targets for 20 rounds, each check given one of: `2xx`, `503`, redirect, dropped connection, non-HTTP reply, headers slower than the timeout, headers trickled one byte at a time, or a hung DNS lookup. Timeout 0.4 s, deadline 0.6 s. | 160 checks, 0 misclassified, worst check 36 ms over its limit, slowest round 0.65 s |
| Alert delivery | 3 targets with random outages for 202 rounds; the webhook fails (`500` or dropped connection) in 55 rounds; the watcher restarts 46 times; 36 rounds are checked by `run-all` with no watcher. | 80 alerts expected, 80 delivered, 0 lost, 0 repeated, 0 out of order, every one at the first round it could be; 0 key collisions |
| Skip resolved | The same run with `--skip-resolved`. Which incidents resolve before their opening could be sent is worked out separately from the delivery rules. | 2 incidents skipped, 76 alerts expected, 76 delivered, 0 lost, 0 unexpected, 0 out of order, every one at the first round it could be |
| Routed | The same run with one target on the watcher's webhook, one on its own webhook that fails in 89 of 202 rounds, and one with alerts off. | 50 alerts expected, 50 delivered, 0 lost, 0 misrouted, 0 out of order on either webhook, every one at the first round its own webhook could take it |
| Crash recovery | 43 rounds, each a new watcher process; 11 were killed with SIGKILL at a random point. | 22 alerts expected, 0 lost, 2 sent again after a kill (same key both times, so 0 repeated after deduplicating), database integrity `ok` |
| Kill during delivery | Kills the watcher after the receiver reads an alert, before it answers. | Received twice with one key, recorded as sent once |

Timing limits allow 500 ms of slack for slow machines. The numbers above are
from one run on a laptop. For a given seed the probe and alert counts are the
same on every run; timings, and how many kills land and where, vary.

## Limitations

- Every report and alert round rescans the matching history, so it suits
  local-sized databases rather than long-term storage at scale.
- Observations are point-in-time samples; there are no uptime percentages, and
  outage durations are only known to lie between the incident's two bounds,
  which are as coarse as the check interval.
- The socket timeout alone doesn't bound DNS lookups; use a deadline for that.
- Alert delivery is at least once: a watcher killed mid-delivery repeats that
  alert after restarting (2 of 22 alerts in the crash scenario above). The
  repeat has the same idempotency key, but dropping it is up to the receiver;
  one that ignores the key acts twice.
- Alerts use the default incident thresholds and have no backoff: a failing
  webhook is retried every round, and its queue only shows in `alerts
  --pending`. Events missed
  while no watcher was running are only sent once an alerting watcher starts,
  and after a long gap that includes incidents that have since resolved unless
  the watcher runs with `--skip-resolved`. A skipped incident is gone for good:
  running a later watcher without the option doesn't send it. Databases that
  used alerts before `alerting` existed skip their unsent events once more on
  the next start.
- Per-target webhooks are stored in plain text in the database, like every
  other setting; anyone who can read the file can read their tokens.
- The API and dashboard have no authentication, TLS, or write access.

## Roadmap

Earlier milestones (the API and dashboard, deduplicated alerts with measured
reliability, saved deadlines, and downtime bounds for each incident) are done.
Next:

1. ~~Give every alert an idempotency key so receivers can drop repeats.~~ Done:
   `idempotency_key` and the `Idempotency-Key` header.
2. ~~Let the watcher skip catch-up incidents that already resolved, sending
   neither their opening nor their resolution.~~ Done: `watch --skip-resolved`
   and `alerts --pending --skip-resolved`.
3. ~~Route alerts per target, with each target choosing its own webhook or
   none.~~ Done: `add`/`update --alert-webhook default|none|URL`, with a
   separate queue per webhook.
4. Back off from a failing webhook instead of retrying it every round, and
   report each webhook's last success, last failure, and queue length.
