"""sitepulse CLI: add/remove/list targets, one-shot checks, live dashboard,
SLA summaries, incident history and alert channels."""
from __future__ import annotations

import argparse
import json
import os
import re
import sys
import threading
import time
from typing import Callable, List, Optional

from . import __version__
from .alerts import (
    AlertState,
    EmailSender,
    SmtpConfigError,
    WebhookSender,
    build_test_payload,
    process_alerts,
    smtp_config_from_env,
)
from .checker import check_all
from .dashboard import dim, green, red, run_dashboard, status_dot
from .incidents import find_incidents, fmt_duration, fmt_time
from .models import CheckResult, Target
from .store import (
    History,
    default_alerts_path,
    default_history_path,
    load_alert_mode,
    load_alert_primary,
    load_email_recipients,
    load_targets,
    load_webhooks,
    save_alert_mode,
    save_alert_primary,
    save_email_recipients,
    save_targets,
    save_webhooks,
)
from .uptime import UptimeSummary, uptime_summary

DEFAULT_CONFIG = "sitepulse.json"


def _resolve_config(explicit: Optional[str]) -> str:
    if explicit:
        return explicit
    env = os.environ.get("SITEPULSE_CONFIG")
    return env or DEFAULT_CONFIG


def _resolve_history(explicit: Optional[str], config_path: str) -> str:
    if explicit:
        return explicit
    env = os.environ.get("SITEPULSE_HISTORY")
    return env or default_history_path(config_path)


def cmd_add(args: argparse.Namespace) -> int:
    config = _resolve_config(args.config)
    targets = load_targets(config) if os.path.exists(config) else []
    if any(t.name == args.name for t in targets):
        print(f"target '{args.name}' already exists", file=sys.stderr)
        return 1
    name = args.name or args.url
    target = Target(
        name=name,
        url=args.url,
        interval_s=args.interval,
        timeout_s=args.timeout,
        expect=args.expect,
        keyword=args.keyword,
    )
    targets.append(target)
    save_targets(config, targets)
    print(f"added '{target.name}' → {target.url}  (every {target.interval_s:g}s)")
    return 0


def cmd_remove(args: argparse.Namespace) -> int:
    config = _resolve_config(args.config)
    if not os.path.exists(config):
        print("no config file yet — nothing to remove", file=sys.stderr)
        return 1
    targets = load_targets(config)
    kept = [t for t in targets if t.name != args.name]
    if len(kept) == len(targets):
        print(f"no target named '{args.name}'", file=sys.stderr)
        return 1
    save_targets(config, kept)
    print(f"removed '{args.name}'")
    return 0


def cmd_list(args: argparse.Namespace) -> int:
    config = _resolve_config(args.config)
    if not os.path.exists(config):
        print("(no targets yet — try: sitepulse add https://example.com)")
        return 0
    for t in load_targets(config):
        kw = f"  keyword={t.keyword!r}" if t.keyword else ""
        print(f"• {t.name}: {t.url}  every {t.interval_s:g}s  expect={t.expect}{kw}")
    return 0


def _print_check_table(results: List[CheckResult], use_color: bool) -> None:
    for r in results:
        dot = status_dot(r.ok, use_color)
        if r.ok:
            detail = green(f"HTTP {r.status}  {r.latency_ms:.0f}ms", use_color)
        else:
            detail = red(f"{r.error or 'failed'}", use_color)
        print(f"{dot} {r.name:<24} {detail}")


def _process_alerts(config: str, targets: List[Target], history: History) -> None:
    """Send incident started/resolved alerts for any new transitions.

    Alerts are routed across the registered webhooks and, when configured,
    email recipients per the ``alert_mode`` / ``alert_primary`` config
    (failover or parallel fan-out). Never breaks the run: delivery failures
    print a warning and are retried on the next check.
    """
    state = AlertState(default_alerts_path(config))
    state.load()
    sender = WebhookSender(load_webhooks(config))
    email_sender = _email_sender_or_warn(config)
    mode = load_alert_mode(config)
    primary = load_alert_primary(config)
    started, resolved, failures = process_alerts(targets, history, state, sender,
                                                email_sender, mode=mode,
                                                primary=primary)
    state.save()
    for dest, err in failures:
        kind = "email" if dest in (email_sender.recipients
                                   if email_sender else ()) else "webhook"
        print(f"⚠ alert {kind} failed ({dest}): {err}", file=sys.stderr)
    if (started or resolved) and sys.stdout.isatty():
        bits = []
        if started:
            bits.append(f"{started} incident started")
        if resolved:
            bits.append(f"{resolved} resolved")
        print(dim(f"→ alerts sent: {', '.join(bits)}", True))


def cmd_check(args: argparse.Namespace) -> int:
    config = _resolve_config(args.config)
    if not os.path.exists(config):
        print(f"config not found: {config}  (try: sitepulse add <url>)", file=sys.stderr)
        return 2
    targets = load_targets(config)
    history = History(_resolve_history(args.history, config))
    history.load()
    results = check_all(targets)
    for r in results:
        history.record(r)
    history.save()
    _process_alerts(config, targets, history)
    use_color = sys.stdout.isatty()
    _print_check_table(results, use_color)
    down = [r for r in results if not r.ok]
    if args.json:
        print(json.dumps([r.to_dict() for r in results], indent=2))
    return 1 if down else 0


def cmd_incidents(args: argparse.Namespace) -> int:
    config = _resolve_config(args.config)
    if not os.path.exists(config):
        print(f"config not found: {config}", file=sys.stderr)
        return 2
    targets = load_targets(config)
    if args.target:
        targets = [t for t in targets if t.name == args.target]
        if not targets:
            print(f"no target named '{args.target}'", file=sys.stderr)
            return 1
    history = History(_resolve_history(args.history, config))
    history.load()
    use_color = sys.stdout.isatty()
    rows = []
    for t in targets:
        for inc in find_incidents(t.name, history.results_for(t.name)):
            rows.append((t.name, inc))
    if not rows:
        print("no incidents recorded — everything stayed up 🎉")
        return 0
    header = f"{'target':<20} {'started':<19} {'duration':>9} {'checks':>6}  {'status':<8} first error"
    print(dim(header, use_color))
    print(dim("─" * len(header), use_color))
    for name, inc in rows:
        dur = fmt_duration(inc.duration_s())
        status = red("ONGOING", use_color) if inc.ongoing else green("resolved", use_color)
        err = (inc.error or "—")[:40]
        print(f"{name:<20} {fmt_time(inc.started):<19} {dur:>9} "
              f"{inc.failures:>6}  {status:<8} {err}")
    return 0


def cmd_uptime(args: argparse.Namespace) -> int:
    config = _resolve_config(args.config)
    if not os.path.exists(config):
        print(f"config not found: {config}", file=sys.stderr)
        return 2
    targets = {t.name: t for t in load_targets(config)}
    if args.target not in targets:
        print(f"no target named '{args.target}'", file=sys.stderr)
        return 1
    if args.days <= 0:
        print("--days must be positive", file=sys.stderr)
        return 1
    history = History(_resolve_history(args.history, config))
    history.load()
    summary = uptime_summary(args.target, history.results_for(args.target),
                             days=args.days)
    if args.json:
        print(json.dumps(_uptime_json(summary), indent=2))
        return 0
    use_color = sys.stdout.isatty()
    days_g = f"{args.days:g}"
    if summary.checks == 0:
        print(f"no checks recorded for '{args.target}' in the last {days_g} days")
        return 0
    print(dim(f"{summary.target}  ·  SLA summary  ·  last {days_g} days", use_color))
    print(dim(f"  window    {fmt_time(summary.window_start)} → {fmt_time(summary.now)}",
              use_color))
    up_s = green(f"{summary.uptime_pct}%", use_color)
    print(f"  uptime    {up_s}  ({summary.up_checks}/{summary.checks} checks up)")
    ongo = f"  ({summary.ongoing} ongoing)" if summary.ongoing else ""
    print(f"  incidents {summary.incidents}{ongo}")
    print(f"  downtime  {fmt_duration(summary.downtime_s)} in window")
    mttr_s = (fmt_duration(summary.mttr_min * 60.0)
              if summary.mttr_min is not None else "—")
    n_resolved = summary.incidents - summary.ongoing
    if n_resolved:
        plural = "s" if n_resolved != 1 else ""
        res_s = f"  ({n_resolved} resolved incident{plural})"
    else:
        res_s = "  (no resolved incidents)"
    print(f"  MTTR      {mttr_s}{res_s}")
    return 0


def _uptime_json(summary: UptimeSummary) -> dict:
    return {
        "target": summary.target,
        "window_days": summary.days,
        "window_start": summary.window_start,
        "now": summary.now,
        "checks": summary.checks,
        "up_checks": summary.up_checks,
        "uptime_pct": summary.uptime_pct,
        "incidents": summary.incidents,
        "ongoing_incidents": summary.ongoing,
        "downtime_s": round(summary.downtime_s, 1),
        "mttr_min": summary.mttr_min,
    }


def cmd_report(args: argparse.Namespace) -> int:
    config = _resolve_config(args.config)
    if not os.path.exists(config):
        print(f"config not found: {config}", file=sys.stderr)
        return 2
    targets = load_targets(config)
    history = History(_resolve_history(args.history, config))
    history.load()
    use_color = sys.stdout.isatty()
    header = f"{'target':<24} {'checks':>6} {'uptime':>7} {'avg':>7} {'p50':>7} {'p95':>7}  last"
    print(dim(header, use_color))
    print(dim("─" * len(header), use_color))
    for t in targets:
        st = history.stats(t.name)
        last = st["last"]
        if last is None:
            last_s = dim("no data", use_color)
        elif last.ok:
            last_s = green(f"UP {last.status} {last.latency_ms:.0f}ms", use_color)
        else:
            last_s = red(f"DOWN {last.error}", use_color)
        uptime = f"{st['uptime_pct']}%" if st["uptime_pct"] is not None else "—"
        avg = f"{st['avg_ms']:.0f}ms" if st["avg_ms"] is not None else "—"
        p50 = f"{st['p50_ms']:.0f}ms" if st["p50_ms"] is not None else "—"
        p95 = f"{st['p95_ms']:.0f}ms" if st["p95_ms"] is not None else "—"
        print(f"{t.name:<24} {st['total']:>6} {uptime:>7} {avg:>7} {p50:>7} {p95:>7}  {last_s}")
    return 0


class _Scheduler(threading.Thread):
    """Background thread that checks each target on its own interval."""

    def __init__(self, targets: List[Target], history: History,
                 on_results: Optional[Callable[[List[CheckResult]], None]] = None):
        super().__init__(daemon=True)
        self.targets = targets
        self.history = history
        self.on_results = on_results
        self._stop_event = threading.Event()
        self._lock = threading.Lock()
        now = time.monotonic()
        self._due = {t.name: now for t in targets}

    def stop(self) -> None:
        self._stop_event.set()

    def run(self) -> None:  # noqa: D102
        while not self._stop_event.is_set():
            now = time.monotonic()
            with self._lock:
                due = [t for t in self.targets if self._due.get(t.name, 0) <= now]
            if due:
                results = check_all(due)
                with self._lock:
                    for result in results:
                        self.history.record(result)
                        self._due[result.name] = time.monotonic() + next(
                            t.interval_s for t in self.targets if t.name == result.name
                        )
                self.history.save()
                if self.on_results:
                    self.on_results(results)
            self._stop_event.wait(0.2)


def cmd_watch(args: argparse.Namespace) -> int:
    config = _resolve_config(args.config)
    if not os.path.exists(config):
        print(f"config not found: {config}  (try: sitepulse add <url>)", file=sys.stderr)
        return 2
    targets = load_targets(config)
    if not targets:
        print("no targets configured — try: sitepulse add <url>")
        return 1
    history = History(_resolve_history(args.history, config))
    history.load()

    # Immediate first pass so the dashboard isn't empty.
    print("checking targets…", flush=True)
    for result in check_all(targets):
        history.record(result)
    history.save()

    scheduler = _Scheduler(
        targets, history,
        on_results=lambda _results: _process_alerts(config, targets, history),
    )
    scheduler.start()
    try:
        run_dashboard(targets, history, refresh_s=args.refresh, frames=args.frames)
    finally:
        scheduler.stop()
        scheduler.join(timeout=5)
        history.save()
    return 0


def cmd_alert_add(args: argparse.Namespace) -> int:
    config = _resolve_config(args.config)
    url = args.url.strip()
    if not url.startswith(("http://", "https://")):
        print(f"not a valid webhook URL: {url!r} (must start with http:// or https://)",
              file=sys.stderr)
        return 1
    webhooks = load_webhooks(config)
    if url in webhooks:
        print(f"webhook already registered: {url}", file=sys.stderr)
        return 1
    webhooks.append(url)
    save_webhooks(config, webhooks)
    print(f"alert webhook added: {url}")
    print("sitepulse will POST incident.started / incident.resolved JSON here")
    return 0


def cmd_alert_remove(args: argparse.Namespace) -> int:
    config = _resolve_config(args.config)
    webhooks = load_webhooks(config)
    if args.url not in webhooks:
        print(f"no alert webhook registered: {args.url}", file=sys.stderr)
        return 1
    save_webhooks(config, [u for u in webhooks if u != args.url])
    print(f"alert webhook removed: {args.url}")
    return 0


def cmd_alert_list(args: argparse.Namespace) -> int:
    config = _resolve_config(args.config)
    mode = load_alert_mode(config)
    primary = load_alert_primary(config)
    print(f"alert mode: {mode} (primary channel: {primary})")
    webhooks = load_webhooks(config)
    if not webhooks:
        print("(no alert webhooks — try: sitepulse alert add https://hooks.example.com/x)")
        return 0
    print("alert webhooks:")
    for url in webhooks:
        print(f"• {url}")
    state = AlertState(default_alerts_path(config))
    state.load()
    pending = [(name, e) for name, e in state._open.items()
               if "ended" not in e]
    if pending:
        print("\nopen incidents already notified:")
        for name, entry in pending:
            print(f"• {name}: failing since {fmt_time(float(entry['started']))}")
    return 0


def cmd_alert_test(args: argparse.Namespace) -> int:
    url = args.url.strip()
    if not url.startswith(("http://", "https://")):
        print(f"not a valid webhook URL: {url!r}", file=sys.stderr)
        return 1
    sender = WebhookSender([url])
    failures = sender.broadcast(build_test_payload(url))
    if failures:
        print(f"⚠ test webhook failed: {failures[0][1]}", file=sys.stderr)
        return 1
    print(f"test alert sent to {url} — check your endpoint")
    return 0


_EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")


def _email_sender_or_warn(config: str) -> Optional[EmailSender]:
    """Build an EmailSender from config-file recipients + SMTP env vars.

    Returns None when no recipients are registered. When recipients exist
    but the SMTP environment is missing or invalid, prints a stderr hint
    and returns None (email is skipped for this run, not fatal).
    """
    recipients = load_email_recipients(config)
    if not recipients:
        return None
    try:
        return EmailSender(recipients, smtp_config_from_env())
    except SmtpConfigError as exc:
        print(f"⚠ {exc}", file=sys.stderr)
        return None


def cmd_alert_email_add(args: argparse.Namespace) -> int:
    config = _resolve_config(args.config)
    address = args.address.strip()
    if not _EMAIL_RE.match(address):
        print(f"not a valid email address: {address!r}", file=sys.stderr)
        return 1
    recipients = load_email_recipients(config)
    if address in recipients:
        print(f"email recipient already registered: {address}", file=sys.stderr)
        return 1
    recipients.append(address)
    save_email_recipients(config, recipients)
    print(f"email recipient added: {address}")
    print("sitepulse will email incident.started / incident.resolved here")
    print("set SITEPULSE_SMTP_HOST and SITEPULSE_SMTP_FROM "
          "(+ SITEPULSE_SMTP_USER / SITEPULSE_SMTP_PASS if auth is needed)")
    return 0


def cmd_alert_email_remove(args: argparse.Namespace) -> int:
    config = _resolve_config(args.config)
    recipients = load_email_recipients(config)
    if args.address not in recipients:
        print(f"no email recipient registered: {args.address}", file=sys.stderr)
        return 1
    save_email_recipients(config, [a for a in recipients if a != args.address])
    print(f"email recipient removed: {args.address}")
    return 0


def cmd_alert_email_list(args: argparse.Namespace) -> int:
    config = _resolve_config(args.config)
    recipients = load_email_recipients(config)
    if not recipients:
        print("(no email recipients — try: sitepulse alert email add you@example.com)")
        return 0
    print("email recipients:")
    for address in recipients:
        print(f"• {address}")
    return 0


def cmd_alert_email_test(args: argparse.Namespace) -> int:
    address = args.address.strip()
    if not _EMAIL_RE.match(address):
        print(f"not a valid email address: {address!r}", file=sys.stderr)
        return 1
    try:
        config = smtp_config_from_env()
    except SmtpConfigError as exc:
        print(f"⚠ {exc}", file=sys.stderr)
        return 1
    sender = EmailSender([address], config)
    failures = sender.broadcast(build_test_payload(address))
    if failures:
        print(f"⚠ test email failed: {failures[0][1]}", file=sys.stderr)
        return 1
    print(f"test email sent to {address} — check your inbox")
    return 0


def cmd_alert_mode(args: argparse.Namespace) -> int:
    config = _resolve_config(args.config)
    if args.mode:
        save_alert_mode(config, args.mode)
    if args.primary:
        save_alert_primary(config, args.primary)
    mode = load_alert_mode(config)
    primary = load_alert_primary(config)
    print(f"alert mode: {mode} (primary channel: {primary})")
    if mode == "failover":
        print("failover: the primary channel is tried first; the other "
              "channel is only used when the primary fails")
    else:
        print("fanout: every incident alert goes out via all configured "
              "channels in parallel")
    return 0


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="sitepulse",
        description="Website uptime & latency monitor with a live terminal dashboard.",
    )
    p.add_argument("--version", action="version", version=f"sitepulse {__version__}")
    p.add_argument("--config", default=None, help="config file (default: sitepulse.json)")
    p.add_argument("--history", default=None, help="history file (default: <config>.history.json)")
    sub = p.add_subparsers(dest="command", required=True)

    a = sub.add_parser("add", help="add a target to monitor")
    a.add_argument("url", help="URL to check")
    a.add_argument("--name", default=None, help="short name (default: the URL)")
    a.add_argument("--interval", type=float, default=60.0, help="seconds between checks (default: 60)")
    a.add_argument("--timeout", type=float, default=10.0, help="request timeout in seconds (default: 10)")
    a.add_argument("--expect", type=int, nargs="+", default=[200], help="acceptable HTTP statuses")
    a.add_argument("--keyword", default=None, help="require this text in the response body")
    a.set_defaults(func=cmd_add)

    r = sub.add_parser("remove", help="remove a target")
    r.add_argument("name", help="target name")
    r.set_defaults(func=cmd_remove)

    sub.add_parser("list", help="list configured targets").set_defaults(func=cmd_list)

    c = sub.add_parser("check", help="check all targets once and print results")
    c.add_argument("--json", action="store_true", help="also print raw JSON results")
    c.set_defaults(func=cmd_check)

    w = sub.add_parser("watch", help="live terminal dashboard")
    w.add_argument("--refresh", type=float, default=0.5, help="dashboard refresh seconds (default: 0.5)")
    w.add_argument("--frames", type=int, default=None, help="render N frames then exit (for demos)")
    w.set_defaults(func=cmd_watch)

    sub.add_parser("report", help="uptime & latency summary from recorded history").set_defaults(func=cmd_report)

    n = sub.add_parser("incidents", help="downtime incidents from recorded history")
    n.add_argument("--target", default=None, help="only show incidents for this target")
    n.set_defaults(func=cmd_incidents)

    u = sub.add_parser("uptime", help="SLA summary: uptime %, incidents, MTTR over a trailing window")
    u.add_argument("target", help="target name")
    u.add_argument("--days", type=float, default=30.0,
                   help="trailing window in days (default: 30)")
    u.add_argument("--json", action="store_true", help="print machine-readable JSON")
    u.set_defaults(func=cmd_uptime)

    al = sub.add_parser("alert", help="manage incident alert channels")
    asub = al.add_subparsers(dest="alert_cmd", required=True)

    al_add = asub.add_parser("add", help="register a webhook for incident alerts")
    al_add.add_argument("url", help="webhook URL (receives JSON POSTs)")
    al_add.set_defaults(func=cmd_alert_add)

    al_rm = asub.add_parser("remove", help="unregister an alert webhook")
    al_rm.add_argument("url", help="webhook URL")
    al_rm.set_defaults(func=cmd_alert_remove)

    asub.add_parser("list", help="list alert webhooks and notified open incidents") \
        .set_defaults(func=cmd_alert_list)

    al_test = asub.add_parser("test", help="send a test payload to a webhook URL")
    al_test.add_argument("url", help="webhook URL")
    al_test.set_defaults(func=cmd_alert_test)

    al_email = asub.add_parser("email", help="manage email alert recipients")
    esub = al_email.add_subparsers(dest="email_cmd", required=True)

    e_add = esub.add_parser("add", help="register an email recipient")
    e_add.add_argument("address", help="email address to notify")
    e_add.set_defaults(func=cmd_alert_email_add)

    e_rm = esub.add_parser("remove", help="unregister an email recipient")
    e_rm.add_argument("address", help="email address")
    e_rm.set_defaults(func=cmd_alert_email_remove)

    esub.add_parser("list", help="list email alert recipients") \
        .set_defaults(func=cmd_alert_email_list)

    e_test = esub.add_parser("test", help="send a test email (needs SMTP env config)")
    e_test.add_argument("address", help="email address to test")
    e_test.set_defaults(func=cmd_alert_email_test)

    al_mode = asub.add_parser("mode", help="set incident alert channel routing")
    al_mode.add_argument("mode", nargs="?", choices=["failover", "fanout"],
                         help="failover: primary channel first, fallback on "
                              "failure; fanout: all channels in parallel "
                              "(default; omit to just show the current mode)")
    al_mode.add_argument("--primary", choices=["webhook", "email"],
                         help="channel tried first in failover mode "
                              "(default: webhook)")
    al_mode.set_defaults(func=cmd_alert_mode)
    return p


def main(argv: Optional[List[str]] = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
