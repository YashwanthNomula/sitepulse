# sitepulse

Website uptime & latency monitor with a **live terminal dashboard** — pure Python, zero dependencies.

Point it at your sites and it checks each on its own schedule, keeps a rolling history of every check, and renders a live dashboard with per-site status, latency sparklines, uptime %, and p50/p95 latencies. Failures show up as red `✕` marks in the sparkline so you see outages at a glance.

## Install

```bash
pip install .
```

## Quick start

```bash
# add the sites you care about
sitepulse add https://example.com --name homepage --interval 60
sitepulse add https://example.com/health --name api --keyword '"status":"ok"' --interval 30

# check everything once (exit code 1 if anything is down — great for CI)
sitepulse check

# live dashboard (Ctrl+C to stop)
sitepulse watch

# uptime & latency summary from recorded history
sitepulse report

# downtime incidents: when each outage started, how long it lasted
sitepulse incidents [--target api] [--output incidents.csv]  # CSV export; terminal table still prints

# SLA summary: uptime %, incident count, MTTR over a trailing window
sitepulse uptime api --days 30 [--json] [--alert-below 99.0]  # exits 2 when SLA breached

# incident alerts: POST JSON to your webhook when an incident starts/resolves
sitepulse alert add https://hooks.example.com/sitepulse
sitepulse alert list        # webhooks + incidents already notified
sitepulse alert test <url> # send a test payload

# email alerts: plain-text incident.started / incident.resolved emails
sitepulse alert email add ops@example.com   # register a recipient
sitepulse alert email list                  # list recipients
sitepulse alert email test ops@example.com  # send a test email
# SMTP comes from env vars (never stored on disk):
#   SITEPULSE_SMTP_HOST, SITEPULSE_SMTP_FROM (+ _USER / _PASS if auth needed)

# channel routing: fanout (default) sends every alert via all configured
# channels in parallel; failover tries the primary channel first and only
# falls back to the other when it fails
sitepulse alert mode [failover|fanout] [--primary webhook|email]
```

Targets live in `sitepulse.json` (override with `--config` or `SITEPULSE_CONFIG`); history is kept in `sitepulse.history.json` next to it.

## Demo

Real output from `sitepulse` pointed at three demo sites (one of them down):

```
$ sitepulse check

● homepage                 HTTP 200  93ms
● api-health               HTTP 200  100ms
● flaky-svc                HTTP 500

$ sitepulse report

target                   checks  uptime     avg     p50     p95  last
─────────────────────────────────────────────────────────────────────
homepage                      1  100.0%    93ms    93ms    93ms  UP 200 93ms
api-health                    1  100.0%   100ms   100ms   100ms  UP 200 100ms
flaky-svc                     1    0.0%       —       —       —  DOWN HTTP 500

$ sitepulse watch   # one dashboard frame

──────────────────────────────── sitepulse  ·  12:18:49 ────────────────────────────────
─────────────────────────────────── ⚠ 1 ONGOING INCIDENT ───────────────────────────────
● flaky-svc            down 2m 00s  ·  started 2026-10-02 12:16:49  ·  15 checks failed  ·  HTTP 500

● homepage                   UP  200         22ms  uptime 100.0%
    http://127.0.0.1:42415/
    █▁▁▁▁▁▁▁▁▁▁▁▁▁▁  p50 22ms  p95 45ms

● api-health                 UP  200         52ms  uptime 100.0%
    http://127.0.0.1:42415/api/health
    █▁▁▁▁▁▁▁▁▁▁▁▁▁▁  p50 52ms  p95 67ms

● flaky-svc                  DOWN x15         1ms  uptime   0.0%  ← HTTP 500
    http://127.0.0.1:42415/flaky
    ✕✕✕✕✕✕✕✕✕✕✕✕✕✕✕  p50 —  p95 —

────────────────────────────────────────────────────────────────────────────────────────
 2 up · 1 down · 0 pending · 3 targets                    Ctrl+C to stop
```

In a real terminal the dashboard redraws in place with color (green ● up, red ● down).

## Features

- **Concurrent checks** — all targets checked in parallel via a thread pool
- **Per-target schedules** — each site gets its own check interval
- **Smart checks** — expected HTTP statuses, required body keyword, custom method/headers, per-target timeouts
- **Rolling history** — last 240 results per target persisted as JSON; survives restarts
- **Latency stats** — avg, p50, p95 per target
- **Failure tracking** — consecutive-failure counts (`DOWN x15`) so flapping is obvious
- **Downtime incidents** — `sitepulse incidents` groups consecutive failures into
  outages with start time, duration, check count, and first error; still-failing
  targets show as `ONGOING`
- **SLA summaries** — `sitepulse uptime <target> --days 30` reports uptime %
  (checks in window), incident count, total downtime, and MTTR (resolved
  incidents only) for the trailing window, with `--json` for scripting.
  `--alert-below 99.0` turns the summary into a CI gate: the command exits 2
  (and prints `SLA BREACH`) when uptime % falls below the threshold, and the
  JSON gains `alert_below` / `breach` keys. Negative or non-numeric
  thresholds are rejected fail-closed with exit 2.
  Incidents that straddle the window edge are clamped to it: only the
  in-window portion counts toward downtime and MTTR; checks outside the
  window are ignored
- **Ongoing-incident alert panel** — `sitepulse watch` pins currently-ongoing
  outages at the top of the dashboard with target, duration so far, started-at,
  checks failed, and first error
- **Incident alerts** — register webhook URLs with `sitepulse alert add <url>`
  or email recipients with `sitepulse alert email add <address>`; every
  `check` and `watch` cycle fires `incident.started` / `incident.resolved`
  the moment an outage begins and when it recovers — JSON POSTs to webhooks,
  plain-text emails (SMTP via `SITEPULSE_SMTP_*` env vars, never stored on
  disk) to recipients; notified incidents are tracked in
  `sitepulse.alerts.json` so each transition fires exactly once per channel,
  and a failing channel never breaks a check run (retried next run)
- **Multi-channel failover / fan-out** — `sitepulse alert mode` picks how
  incident alerts cross the webhook and email channels: `fanout` (default)
  sends via every configured channel in parallel, `failover` tries the
  primary channel first and falls back to the other only on failure
  (`--primary webhook|email` picks the order). Delivery is tracked per
  channel, so a retry only re-sends through channels that have not
  delivered yet — an incident event is never double-sent through the same
  channel, and one channel failing never blocks the other
- **CI-friendly** — `sitepulse check` exits non-zero when anything is down, `--json` for scripting

## Project layout

```
sitepulse/
├── sitepulse/
│   ├── __init__.py    # version
│   ├── models.py      # Target / CheckResult dataclasses
│   ├── checker.py     # concurrent HTTP(S) checks, urllib only
│   ├── store.py       # rolling history + JSON config/history persistence
│   ├── dashboard.py   # ANSI live-dashboard renderer (no curses)
│   ├── incidents.py   # downtime incident detection + formatting
│   ├── uptime.py      # SLA summaries: uptime %, incidents, MTTR over a window
│   ├── alerts.py      # incident webhook + email alerts (started/resolved)
│   └── cli.py         # add/remove/list/check/watch/report/incidents/uptime/alert
├── tests/             # 115 tests, all against a local HTTP server (no network)
└── examples/
    └── sitepulse.json # sample config
```

## Tests

```bash
python -m pytest
```

## License

MIT
