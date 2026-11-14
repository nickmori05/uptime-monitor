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
| `history`, `summary`, `incidents`, `alerts`, `webhooks`, `watcher` | Report on saved results without making requests. |
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
python3 monitor.py add example https://example.com --timeout 3 --deadline 10 --failures 3
python3 monitor.py update example --url https://example.com/health
python3 monitor.py update example --no-deadline
python3 monitor.py update example --recoveries 1
python3 monitor.py update example --no-failures --no-recoveries
python3 monitor.py update example --alert-webhook https://hooks.example.com/team
python3 monitor.py add held https://held.example/health --paused
python3 monitor.py update held --pause
python3 monitor.py update held --unpause
python3 monitor.py run example
python3 monitor.py targets
python3 monitor.py remove example
```

A target saves a URL, a timeout, an optional default deadline, where its
alerts go (see [Routing alerts per target](#routing-alerts-per-target)), and how
many consecutive failures open an incident and successes resolve it, under a
name (1–64 letters, digits, `-` or `_`, starting with a letter or digit). `add` and
`update` never make a request. Removing or re-pointing a target keeps its past
checks, which stay tied to the URL they were made against. A `--deadline` given
to `run`, `run-all`, or `watch` overrides saved deadlines for that run only.

`--failures` and `--recoveries` default to unset, which means 2. Each must be a
whole number from 1 to 100. `targets` shows a count as `null` until it is set.
`--no-failures` and `--no-recoveries` put one count back at the default without
changing the other. Targets that check the same URL share its incidents, so
they share one pair of counts: an override on any of them applies to the URL, a
count none of them set stays at 2, and saving a second override that disagrees
is rejected. A URL no target checks (a bare `check`, or a target that was
removed) uses 2 and 2.

`add --paused` saves a target that `watch` and `run-all` will not check.
`update --pause` and `update --unpause` change that later, and either can be
combined with another setting. Pausing keeps the name, URL, timeout, deadline,
webhook, and incident thresholds, and it keeps every check already saved.
`targets` shows `"paused": true` or `false`. Reports still use a paused
target's saved thresholds, and alerts already owed are still sent. `run NAME`
still checks that one target. A URL that another, unpaused target also checks
is still checked.

### Batches and the watcher

```sh
python3 monitor.py run-all --workers 4
python3 monitor.py watch --interval 30 --deadline 15
```

`run-all` checks every saved target that is not paused, at most `--workers`
(1–32) at a time, and prints the results sorted by target name. It exits `1` if
any target is down.
Each result is committed as soon as it arrives, so a failure or Ctrl+C keeps
everything already saved.

`watch` repeats `run-all`, waiting `--interval` seconds (1–86,400) after each
round, and prints one JSON line per round. It reloads targets every round, so
other terminals can add, update, remove, pause, or unpause them while it runs.
`--count N` stops after N rounds, and the exit code is `1` if any round saw a
failure. It stops when no targets remain. A round where every target is paused
checks nothing, and the watcher keeps waiting, so unpausing one is picked up on
the next round. Run one watcher per database.

### Whether the watcher is running

```sh
python3 monitor.py watcher
```

```json
{"state": "running", "phase": "waiting", "interval": 30.0,
 "started_at": "2026-11-13T12:00:00+00:00", "beat_at": "2026-11-13T12:01:02+00:00",
 "rounds": 2, "due_at": "2026-11-13T12:01:32+00:00", "late": false, "stale": false,
 "quiet_seconds": 4}
```

`watch` replaces one database row when it starts a round, when that round's checks
finish, when the wait begins, and when it exits. While it runs, it also rewrites
`beat_at` every 5 seconds, during a check, the alert delivery after a round,
and the wait. That rewrite changes nothing else. `watcher` reads the row and
makes no request. `state` is `absent` when no watcher has written it, `running`
while one has, and `stopped` after an exit Python can unwind: the count was
reached, no targets remain, Ctrl+C or SIGTERM, or a round raised. `phase` is
`checking` from the start of a round until the wait begins (the alert delivery
after the checks is still `checking`, since a delivery has no promised end) and
`waiting` during the interval. `rounds` counts rounds whose checks have
finished. `due_at` is set only while `waiting`, and it is one interval after
the beat that started the wait: the moment the next round's checks are due to
start. Later beats during the wait do not move it. `late` is true
only when that moment has passed. `quiet_seconds` is the whole seconds since
`beat_at`, rounded down, or 0 when the clock is behind `beat_at`. `stale` is
true when `state` is `running` and `quiet_seconds` is at least 15. A live
watcher stays under that, because it beats every 5 seconds. A process that has
died, or that can no longer write, crosses it. The two are separate: a wait can
pass `due_at` while the process is still beating, and a process can go stale
during a check, or during a wait that is not due yet.

A round that checks nothing because every target is paused still beats, so the
row shows the watcher is alive when history is not growing. `run` and `run-all`
do not write it. The next `watch` replaces the row; it is the latest run, not a
log of earlier ones. A kill that does not unwind (SIGKILL) leaves whatever beat
was written last. That is `late` only if the watcher was already `waiting`, and
it is `stale` once 15 seconds pass with no further beat, in either phase.
A slow round that is still running keeps beating, so it is not `stale`: a check
without a deadline can sit in DNS, and a round of many slow checks has no
promised end, but the beat shows the process is still there.

### Incidents and summaries

```sh
python3 monitor.py incidents --state open
python3 monitor.py incidents --failures 3 --recoveries 2 --limit 10
python3 monitor.py summary --target example --last 50
python3 monitor.py summary --state down
```

An incident opens after a number of consecutive failed checks of the same URL
and resolves after a number of consecutive successes. Both default to 2, and a
target can save its own (see [Targets](#targets)). With the defaults, one
success between failures keeps the incident open. `incidents` with no counts
given uses those saved thresholds, and so do alerts, so the two describe the
same incidents. `--failures` and `--recoveries` reinterpret every URL for that
one report; the count you leave out stays at 2, and alerts are unchanged.
Passing either one does not read saved thresholds. Reports are rebuilt from
saved checks in insertion order every time, so changing a target's counts
reinterprets its history without editing the checks. Each incident includes the
`failures` and `recoveries` counts that produced it.

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

### Alerts

```sh
python3 monitor.py watch --interval 30 --alert-webhook https://hooks.example.com/uptime
python3 monitor.py watch --interval 30 --alert-webhook https://hooks.example.com/uptime --skip-resolved
python3 monitor.py watch --interval 30 --alert-webhook https://hooks.example.com/uptime --max-backoff 900
python3 monitor.py alerts
python3 monitor.py alerts --pending
python3 monitor.py alerts --pending --skip-resolved
python3 monitor.py webhooks
```

With `--alert-webhook`, the watcher POSTs one JSON object after each round for
every new incident event, using each URL's saved thresholds (the same ones
`incidents` uses when no counts are given):

```json
{"event": "opened", "url": "https://example.com", "idempotency_key": "5e57d713...", "text": "Incident opened for ...", "incident": {"...": "..."}}
```

`event` is `opened` or `resolved`, and `incident` matches the `incidents`
report. Each event is sent once: sent events are saved in the database, so
restarts, flapping, and further failures don't repeat them, and several targets
for one URL share alerts. A delivery that fails (non-`2xx`, redirect, connection
error, or no answer within about 10 seconds) is logged to stderr and retried
after the next round, in order (per webhook, when targets have their own), or
later with `--max-backoff` (see [Backing off](#backing-off-from-a-failing-webhook)).

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

#### Webhook health

```sh
python3 monitor.py webhooks
```

```json
[{"route": "default", "targets": ["example"], "state": "ok", "pending": 0, "oldest_pending_at": null,
  "last_success_at": "2026-10-25T12:00:31+00:00", "last_failure_at": null, "last_error": null,
  "consecutive_failures": 0, "retry_at": null},
 {"route": "https://hooks.example.com/payments", "targets": ["payments"], "state": "failing", "pending": 2,
  "oldest_pending_at": "2026-10-25T11:58:02+00:00", "last_success_at": "2026-10-24T09:12:40+00:00",
  "last_failure_at": "2026-10-25T12:00:31+00:00", "last_error": "http_503", "consecutive_failures": 5,
  "retry_at": "2026-10-25T12:08:31+00:00"}]
```

Every delivery attempt is saved per route, so `webhooks` can show how each
webhook is doing without a watcher running. It lists the default route and every
webhook a target names, has been tried, or has alerts queued for, default first,
then by URL. `targets` are the targets that name the route (a URL no target
checks also alerts `default`). `state` is `failing` when the latest attempt
failed, `ok` when it succeeded, and `untried` before the first attempt.
`pending` counts the alerts still owed to the route, the same ones `alerts
--pending` lists with it in `routes`, and `oldest_pending_at` is when the
oldest of them happened (the check that opened or resolved its incident), so
it shows how far behind the webhook is. `last_error` is the code from the last
failure, which stays on record after the webhook recovers;
`consecutive_failures` counts failed attempts since the last success.
`retry_at` is when a watcher with `--max-backoff` will next try a failing
webhook; it is `null` when the next round will (always, without the option). A
watcher only tries a webhook while it has alerts to send.

#### Backing off from a failing webhook

```sh
python3 monitor.py watch --interval 30 --alert-webhook https://hooks.example.com/uptime --max-backoff 900
```

By default a failing webhook is tried again every round while it has alerts
queued, which for a receiver that's down for an hour means 120 failed requests
and 120 lines on stderr at a 30-second interval. With `--max-backoff SECONDS`,
the watcher instead leaves a webhook alone for one interval after its first
failure in a row, then twice as long after each further one (30, 60, 120, 240
seconds, and so on), up to `SECONDS`, which must be at least the interval and
at most 86,400. The retry time is saved with the webhook's health, so it holds
across rounds and shows as `retry_at` in `webhooks`, and stderr says when the
next try is. Each webhook backs off on its own: the others carry on, and alerts
waiting for a backing-off webhook stay queued in order. A success resets the
wait, so the next failure starts from one interval again.

A watcher that starts tries every webhook with alerts owed once straight away,
whatever its retry time, since restarting the watcher is the usual moment
after fixing a receiver; a failure then extends the wait as usual. A watcher
run without the option ignores saved retry times and clears them as it fails.
The cost is latency: an alert can wait up to `SECONDS` after its webhook
recovers before it's sent, and longer if the webhook came back briefly during a
wait and failed again at the next try.

### API and dashboard

```sh
python3 monitor.py serve --port 8080
curl http://127.0.0.1:8080/api/summary
curl 'http://127.0.0.1:8080/api/history?limit=5&url=https%3A%2F%2Fexample.com'
```

`serve` listens on `127.0.0.1:8080` (`--port 0` picks a free port). It answers
GET requests for `/api/targets`, `/api/history`, `/api/summary`,
`/api/incidents`, `/api/webhooks`, and `/api/watcher`, which return the same JSON as the
matching commands and accept their filters as query parameters (URLs must be
percent-encoded). The one difference is that webhook URLs often carry a secret
token, so `/api/targets` shows a target's own webhook only as `"webhook"`, and
`/api/webhooks` shows it as `"route": "webhook"` with its `host`. `paused` is
included, the same `true` or `false` as the `targets` command. Invalid or
unknown parameters return `400`, unknown paths `404`, and other methods `405`.

`/` serves `dashboard.html`: a single static page that lists each target's
latest result, recent up/down counts, average latency, incident thresholds,
open incidents, and each alert webhook's health, says whether the watcher is
checking, waiting, late, stale, or stopped, and refreshes every 30 seconds.
The open incidents are the ones `incidents` reports with no counts given, so
they match alerts. The endpoints table shows each target's saved counts:
`default` when both are unset, and otherwise that target's name with the counts
it contributes (`api: 3 down, 2 up`, using 2 for a count it did not set). A
paused target's name is shown with `(paused)`. An
open incident shows the shared reading for its URL, such as `3 down, 2 up`.
It has no external resources, inserts saved values
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
database lives in the `monitor-data` volume, with its `-wal` and `-shm` files
beside it. The volume survives `docker compose down` but not `down --volumes`.
Add targets before starting it; the watcher exits
when none remain. Compose allows 75 seconds for SIGTERM shutdown, which the
20-second deadline stays well inside. Inside the container, `localhost` is the
container itself. To send alerts, add `--alert-webhook URL` to the service
`command` in `compose.yml`, or give targets their own webhooks, and add
`--max-backoff SECONDS` there to back off from failing ones.

## Design

- `monitor.py`: the command line, request probing, SQLite storage, targets,
  batches, and the watcher.
- `incidents.py`: rebuilds incidents from saved checks.
- `alerts.py`: finds unsent incident events and delivers them to webhooks.
- `routing.py`: works out which webhooks each URL's alerts go to.
- `webhooks.py`: saves each delivery attempt's outcome, works out backoff, and
  reports webhook health.
- `api.py` and `dashboard.html`: the read-only HTTP API and the static dashboard.

All state lives in one SQLite file with seven tables: `checks` (one row per
observation), `targets`, `alerts` (one row per sent or skipped incident event,
unique per event), `deliveries` (one row per webhook route an event reached),
`webhooks` (one row per route that was ever tried, with its latest outcomes and
any retry time),
`alerting` (a single row saving the last check from before alerts were
first enabled; every event after it is owed an alert),
and `watcher` (a single row for the latest `watch` run, replaced when the next
one starts).
Incidents and summaries are derived from `checks` on demand rather than stored,
and checks are ordered by row ID rather than by timestamp, so a clock change
can't reorder history. `connect` creates missing tables and columns, so
databases from earlier versions upgrade in place. A target saved before it
could set incident thresholds gains null counts, which mean the default of 2
and 2. A target saved before it could be paused gains `paused` 0, so the
watcher still checks it. A database from before the watcher heartbeat gains an
empty `watcher` table, which reads as `absent`.

Requests run on worker threads while the calling thread does every database
write, which keeps SQLite on one connection. The exception is the watcher
pulse: while `watch` runs, a second connection on its own thread rewrites that
row's `beat_at` every 5 seconds and then stops. It updates nothing else. `run`
and `run-all` do not start it. Deadlines (and webhook deliveries)
run the request on a daemon thread that is abandoned when time runs out, since
a hung DNS lookup can't be cancelled; the abandoned thread finishes on its own
socket timeout. The API opens the database read-only for each request, so it
can run beside a watcher on the same file. The file is in WAL mode, so that
read does not block the watcher from committing a check, and a commit does not
block the read. `synchronous` stays `FULL`. A checkpoint can still take a brief
exclusive lock; a connection waits up to 5 seconds for it. An older database
switches to WAL the next time a command opens it, and that switch needs the
file to itself.

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
incidents that already resolved, routing alerts per target, idempotency keys,
webhook health and backoff, the API, the dashboard's headers and policy,
upgrading older databases, per-target incident thresholds (invalid counts,
overrides, resetting to the default, and alerts matching the incidents report),
pausing a target (`watch` and `run-all` skip it, `run` still checks it, its
settings and history stay, an older database gains the column unpaused, and an
alert already owed is still sent),
and reading the database while a check is committed, and the watcher heartbeat
(a paused fleet still beats, a wait past `due_at` is `late`, a check in progress
is not `late`, a running watcher quiet for 15 seconds is `stale` and one quiet
for 14 is not, the beat keeps moving during a blocked check, a wait, and the
gap while a round is handed back for alerts, and it stops moving once `watch`
returns, `run` and `run-all` do not write the row, an older database reads as
`absent`, and Ctrl+C or SIGTERM records `stopped`).

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
silent, a webhook that refuses alerts reported as failing with its queue, by
host only in the API, and that webhook left alone for two intervals with
`--max-backoff` but retried every round without it, and a target whose failure
count is 1 opening an incident (and a matching pending alert) after one down
check while a target left at the default does not, until that override is
cleared, and a paused target left out of `watch` and `run-all`, checked by
`run` while it stays paused, then checked again on the next `watch` round after
it is unpaused, and after that watch the `watcher` report is `stopped` with
that one round and not stale, which `/api/watcher` agrees with. Everything it
creates is removed afterward.

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
headers that don't match the body. Each alert scenario also compares the
`webhooks` report with the receivers' own logs (last success, last failure and
its error, failures in a row, and queue length), against a clock that advances
30 seconds per round. A retry model, also written separately from `alerts.py`,
replays each webhook's attempts round by round; except with `--skip-resolved`,
the receivers must see exactly the POSTs it predicts, and with `--max-backoff`
it decides which round each alert is due. The unit tests run the same scenarios
at a smaller size, and also confirm that the probe and alert scenarios fail when
a bug is injected (ignored deadlines; dropped, repeated, or reordered alerts;
one key for every alert; skipping too few or too many incidents; ignoring
routes; one failing webhook holding up the others; a health report that misses
successes or failures; or a backoff that's ignored, has no limit, or holds
webhooks when a watcher starts).

| Scenario | What it does | Measured (seed 1, defaults) |
| --- | --- | --- |
| Probe faults | 8 targets for 20 rounds, each check given one of: `2xx`, `503`, redirect, dropped connection, non-HTTP reply, headers slower than the timeout, headers trickled one byte at a time, or a hung DNS lookup. Timeout 0.4 s, deadline 0.6 s. | 160 checks, 0 misclassified, worst check 36 ms over its limit, slowest round 0.65 s |
| Alert delivery | 3 targets with random outages for 202 rounds; the webhook fails (`500` or dropped connection) in 55 rounds; the watcher restarts 46 times; 36 rounds are checked by `run-all` with no watcher. | 80 alerts expected, 80 delivered, 0 lost, 0 repeated, 0 out of order, every one at the first round it could be; 0 key collisions |
| Skip resolved | The same run with `--skip-resolved`. Which incidents resolve before their opening could be sent is worked out separately from the delivery rules. | 2 incidents skipped, 76 alerts expected, 76 delivered, 0 lost, 0 unexpected, 0 out of order, every one at the first round it could be |
| Routed | The same run with one target on the watcher's webhook, one on its own webhook that fails in 89 of 202 rounds, and one with alerts off. | 50 alerts expected, 50 delivered, 0 lost, 0 misrouted, 0 out of order on either webhook, every one at the first round its own webhook could take it; webhook health matched both receivers |
| Outages | The routed run with both webhooks failing in outages of several rounds instead (down in 65 and 67 of 202 rounds). | 50 alerts expected, 50 delivered, 0 lost, 0 out of order; 109 POSTs, 59 of them failed, exactly as the retry model predicts |
| Outages with backoff | The same with `--max-backoff 240` (8 rounds). | 50 alerts expected, 50 delivered, 0 lost, 0 out of order, each in the round the retry model predicts; 79 POSTs, 29 failed (half as many); the longest an alert waited past its webhook's first good round was 22 rounds, and past the check that raised it 30 rounds (24 without backoff) |
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
- The watcher heartbeat is one row, replaced by the next `watch`, so it is not
  a history of runs. A second watcher on the same database overwrites it; run
  one watcher per database. While a watcher runs it rewrites `beat_at` every 5
  seconds, so `quiet_seconds` stays small through a slow check, the alert
  delivery after a round, and the wait. `stale` means a running watcher has
  been quiet for 15 seconds: the process is gone, or it cannot write. `late`
  means a wait has passed `due_at`. A live watcher can be late without being
  stale, and a dead one can be stale before it is late. A round that is still
  `checking` is never `late`. `run` and `run-all` do not beat. A clock set back
  reports
  `quiet_seconds` 0 and is neither `late` nor `stale` until the clock passes
  `beat_at`. A machine that cannot run the pulse for 15 seconds looks stale
  while it is still alive. The pulse does not say which check the round is on,
  only that the process can still write.
- Pausing a target keeps it out of `watch` and `run-all` until `update --unpause`.
  `run NAME` still checks it. A URL that another, unpaused target also checks is
  still checked. While a URL is not checked, an open incident stays open, and
  alerts already owed to its webhooks are still sent. A paused target still
  counts when targets that share a URL must agree on incident thresholds.
- Changing a target's thresholds reinterprets its history and does not rewrite
  alerts already sent. The alert log can list an opening the new counts no
  longer call an incident, and an opening already sent is not sent again when
  that same run of failures is still an incident. `incidents --failures` and
  `--recoveries` reinterpret that one report only; alerts ignore them. Targets
  that share a URL share one pair of counts, and a second override that
  disagrees is rejected.
- Backoff is opt-in, so without
  `--max-backoff` a failing webhook is retried every round while it has alerts
  queued; with it, alerts can wait past a webhook's recovery (22 rounds at
  most in the scenario above, where webhooks recover briefly mid-outage). The
  backoff has no jitter, and a crash-looping watcher tries every failing
  webhook each time it starts. Events missed
  while no watcher was running are only sent once an alerting watcher starts,
  and after a long gap that includes incidents that have since resolved unless
  the watcher runs with `--skip-resolved`. A skipped incident is gone for good:
  running a later watcher without the option doesn't send it. Databases that
  used alerts before `alerting` existed skip their unsent events once more on
  the next start.
- Webhook health is kept per route, and the watcher's own webhook is the route
  `default` whatever its URL, so a watcher started with a different
  `--alert-webhook` shows the old one's history until its first attempt.
- Per-target webhooks are stored in plain text in the database, like every
  other setting; anyone who can read the file can read their tokens.
- The API and dashboard have no authentication, TLS, or write access.
- Copying the `.sqlite3` file while a process has it open can omit the newest
  checks: they sit in the `-wal` file until a checkpoint. Copy that file as
  well, or copy the main file after every connection has closed, which
  checkpoints and removes the `-wal` file. A copy of only the main file still
  opens and passes `integrity_check`, so it looks complete when rows are
  missing. Every process that opens the database, including the API, needs a
  writable directory beside it for the `-shm` file.

## Roadmap

Earlier milestones (the API and dashboard, deduplicated alerts with measured
reliability, saved deadlines, and downtime bounds for each incident) are done,
and so are these:

1. ~~Give every alert an idempotency key so receivers can drop repeats.~~ Done:
   `idempotency_key` and the `Idempotency-Key` header.
2. ~~Let the watcher skip catch-up incidents that already resolved, sending
   neither their opening nor their resolution.~~ Done: `watch --skip-resolved`
   and `alerts --pending --skip-resolved`.
3. ~~Route alerts per target, with each target choosing its own webhook or
   none.~~ Done: `add`/`update --alert-webhook default|none|URL`, with a
   separate queue per webhook.
4. ~~Report each webhook's last success, last failure, and queue length.~~
   Done: `webhooks`, `/api/webhooks`, and the dashboard's webhook table.
5. ~~Back off from a failing webhook instead of retrying it every round.~~
   Done: `watch --max-backoff`, with `retry_at` in `webhooks`.
6. ~~Let targets choose their own incident thresholds, for alerts as well as
   reports.~~ Done: `add`/`update --failures` and `--recoveries`, cleared with
   `--no-failures` and `--no-recoveries`. Alerts and `incidents` (with no
   counts given) use the saved ones.
7. ~~Pause a target so the watcher skips it without deleting its settings or
   history.~~ Done: `add --paused` and `update --pause` / `--unpause`. `watch`
   and `run-all` leave a paused target out; `run NAME` still checks it.
8. ~~Record whether the watcher is running, and when a finished round's wait
   has already passed.~~ Done: `watcher` and `/api/watcher`. The written list
   ended at pausing a target. This follows that: once every target can be
   paused, a live watcher may save no checks, so the check history cannot show
   that it is still running, and a watcher that dies during its wait leaves
   those checks looking merely old.
9. ~~Keep beating through a slow round, so a dead watcher is not mistaken for
   one.~~ Done: while `watch` runs it rewrites `beat_at` every 5 seconds, and
   `stale` is true once a running watcher has been quiet for 15 seconds. A
   round that is still checking is never `late`; `stale` is how a beat that
   has stopped shows up, including during a check and before a wait is due.
