"""Live terminal dashboard renderer. ANSI only, no curses, no dependencies."""
from __future__ import annotations

import shutil
import sys
import time
from datetime import datetime
from typing import List, Optional

from .models import Target
from .store import History

BLOCKS = " ▁▂▃▄▅▆▇█"
FAIL_MARK = "✕"

CSI = "\x1b["
RESET = CSI + "0m"


def _colorize(text: str, code: str, use_color: bool) -> str:
    if not use_color:
        return text
    return f"{CSI}{code}m{text}{RESET}"


def green(t: str, c: bool) -> str:
    return _colorize(t, "32", c)


def red(t: str, c: bool) -> str:
    return _colorize(t, "31;1", c)


def yellow(t: str, c: bool) -> str:
    return _colorize(t, "33", c)


def dim(t: str, c: bool) -> str:
    return _colorize(t, "2", c)


def sparkline(values: List[Optional[float]], use_color: bool = True) -> str:
    """Latency sparkline; failures render as a red ✕.

    Values are latencies in ms; None marks a failed check.
    """
    if not values:
        return dim("— no data —", use_color)
    nums = [v for v in values if v is not None]
    lo = min(nums) if nums else 0.0
    hi = max(nums) if nums else 1.0
    span = max(hi - lo, 1e-9)
    out = []
    for v in values:
        if v is None:
            out.append(red(FAIL_MARK, use_color))
            continue
        idx = int(round((v - lo) / span * 7)) + 1  # 1..8, skip the blank block
        idx = max(1, min(8, idx))
        glyph = BLOCKS[idx]
        if v >= lo + span * 0.75:
            glyph = yellow(glyph, use_color)
        out.append(glyph)
    return "".join(out)


def latency_bar(latency_ms: Optional[float], scale_ms: float = 1000.0, width: int = 12,
                use_color: bool = True) -> str:
    if latency_ms is None:
        return dim("—", use_color)
    filled = int(round(min(latency_ms, scale_ms) / scale_ms * width))
    bar = "█" * filled + "░" * (width - filled)
    if latency_ms > scale_ms * 0.75:
        return yellow(bar, use_color)
    return green(bar, use_color)


def status_dot(ok: Optional[bool], use_color: bool = True) -> str:
    if ok is None:
        return dim("○", use_color)
    return green("●", use_color) if ok else red("●", use_color)


def fmt_ms(value: Optional[float]) -> str:
    if value is None:
        return "—"
    if value >= 1000:
        return f"{value / 1000:.2f}s"
    return f"{value:.0f}ms"


def render_frame(targets: List[Target], history: History, use_color: bool,
                 term_width: Optional[int] = None) -> str:
    """Render one full dashboard frame as a string."""
    width = term_width or shutil.get_terminal_size((100, 30)).columns
    width = max(60, min(width, 140))
    now = datetime.now().strftime("%H:%M:%S")
    lines: List[str] = []

    title = f" sitepulse  ·  {now} "
    lines.append(_colorize(title.center(width, "─"), "1;36", use_color))

    up_count = 0
    down_count = 0
    pending_count = 0
    for target in targets:
        st = history.stats(target.name)
        last = st["last"]
        if last is None:
            pending_count += 1
            dot = status_dot(None, use_color)
            state = dim("pending", use_color)
            lat = dim("—", use_color)
        elif last.ok:
            up_count += 1
            dot = status_dot(True, use_color)
            state = green(f"UP  {last.status}", use_color)
            lat = fmt_ms(last.latency_ms)
        else:
            down_count += 1
            dot = status_dot(False, use_color)
            state = red(f"DOWN{f' x{st['consec_fail']}' if st['consec_fail'] > 1 else ''}", use_color)
            lat = dim(fmt_ms(last.latency_ms), use_color)

        uptime = f"{st['uptime_pct']}%" if st["uptime_pct"] is not None else "—"
        spark = sparkline(history.recent_latencies(target.name), use_color)
        err = ""
        if last is not None and not last.ok and last.error:
            err = dim(f"  ← {last.error}", use_color)

        name_col = f"{dot} {target.name}"[:28].ljust(28)
        lines.append(f"{name_col} {state:<12} {lat:>7}  uptime {uptime:>6}{err}")
        lines.append(f"    {dim(target.url, use_color)[:width - 8]}")
        lines.append(f"    {spark}  {dim('p50 ' + fmt_ms(st['p50_ms']) + '  p95 ' + fmt_ms(st['p95_ms']), use_color)}")
        lines.append("")

    summary = f" {up_count} up · {down_count} down · {pending_count} pending · {len(targets)} targets "
    if down_count:
        summary = red(summary, use_color)
    elif pending_count == len(targets):
        summary = dim(summary, use_color)
    else:
        summary = green(summary, use_color)
    lines.append("─" * width)
    lines.append(summary + dim("  Ctrl+C to stop", use_color).rjust(max(0, width - len(summary) - 16)))
    return "\n".join(lines)


def clear_screen(use_color: bool = True) -> None:
    if use_color:
        sys.stdout.write(CSI + "2J" + CSI + "H")
    else:
        sys.stdout.write("\n" + "=" * 40 + "\n")
    sys.stdout.flush()


def run_dashboard(targets: List[Target], history: History, refresh_s: float = 0.5,
                  frames: Optional[int] = None, out=None) -> None:
    """Redraw the dashboard until interrupted (or `frames` frames rendered).

    `frames` is mainly for demos/tests; None means run forever.
    """
    out = out or sys.stdout
    use_color = hasattr(out, "isatty") and out.isatty()
    rendered = 0
    try:
        while True:
            clear_screen(use_color)
            out.write(render_frame(targets, history, use_color) + "\n")
            out.flush()
            rendered += 1
            if frames is not None and rendered >= frames:
                break
            time.sleep(refresh_s)
    except KeyboardInterrupt:
        pass
    finally:
        out.write("\n")
        out.flush()
