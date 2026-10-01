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
│   └── cli.py         # add/remove/list/check/watch/report
├── tests/             # 32 tests, all against a local HTTP server
└── examples/
    └── sitepulse.json # sample config
```

## Tests

```bash
python -m pytest
```

## License

MIT
