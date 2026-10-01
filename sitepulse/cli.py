"""sitepulse CLI: add/remove/list targets, one-shot checks, live dashboard."""
from __future__ import annotations

import argparse
import json
import os
import sys
import threading
import time
from typing import List, Optional

from . import __version__
from .checker import check_all
from .dashboard import dim, green, red, run_dashboard, status_dot
from .models import CheckResult, Target
from .store import History, default_history_path, load_targets, save_targets

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
    use_color = sys.stdout.isatty()
    _print_check_table(results, use_color)
    down = [r for r in results if not r.ok]
    if args.json:
        print(json.dumps([r.to_dict() for r in results], indent=2))
    return 1 if down else 0


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

    def __init__(self, targets: List[Target], history: History):
        super().__init__(daemon=True)
        self.targets = targets
        self.history = history
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
                for result in check_all(due):
                    with self._lock:
                        self.history.record(result)
                        self._due[result.name] = time.monotonic() + next(
                            t.interval_s for t in self.targets if t.name == result.name
                        )
                self.history.save()
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

    scheduler = _Scheduler(targets, history)
    scheduler.start()
    try:
        run_dashboard(targets, history, refresh_s=args.refresh, frames=args.frames)
    finally:
        scheduler.stop()
        scheduler.join(timeout=5)
        history.save()
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
    return p


def main(argv: Optional[List[str]] = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
