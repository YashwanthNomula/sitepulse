"""Uptime/SLA summaries over a trailing window.

Built on :mod:`sitepulse.incidents`, this module answers "how reliable was a
target over the last N days" with uptime %, incident count, and MTTR.

Window semantics (mirrored in the README):
- Only checks with timestamps inside ``[window_start, now]`` count toward
  uptime %. Checks outside the window are ignored entirely.
- An incident counts when its span ``[started, ended or now]`` overlaps the
  window, even if it started before the window opened. Overlapping incidents
  are *clamped* to the window: only the portion inside
  ``[window_start, now]`` contributes to downtime and MTTR. Incidents that
  ended before the window opened are ignored.
- MTTR (mean time to resolve) covers resolved incidents only, in minutes.
  Ongoing incidents are excluded (they have no resolution time yet) and are
  reported separately as ``ongoing``.
- When no checks were recorded inside the window, uptime % is None — the
  command says "no data" rather than guessing.
"""
from __future__ import annotations

import time
from dataclasses import dataclass
from typing import List, Optional

from .incidents import find_incidents
from .models import CheckResult


@dataclass
class UptimeSummary:
    """SLA numbers for one target over a trailing window."""

    target: str
    days: float
    window_start: float            # epoch, inclusive lower bound
    now: float                     # epoch
    checks: int                    # checks inside the window
    up_checks: int                  # of those, the successful ones
    uptime_pct: Optional[float]    # 100 * up/total, 1dp; None when no checks
    incidents: int                 # incidents overlapping the window
    ongoing: int                   # of those, still ongoing
    downtime_s: float              # total clamped downtime seconds in window
    mttr_min: Optional[float]      # mean clamped resolve time, minutes
                                   # (resolved incidents only; None if none)


def uptime_summary(target: str, results: List[CheckResult], days: float = 30.0,
                   now: Optional[float] = None) -> UptimeSummary:
    """Compute the SLA summary for ``target`` over the trailing ``days``."""
    if days <= 0:
        raise ValueError("days must be positive")
    now = time.time() if now is None else now
    window_start = now - days * 86400.0

    windowed = [r for r in results if window_start <= r.timestamp <= now]
    up = sum(1 for r in windowed if r.ok)

    downtime_s = 0.0
    incidents = 0
    ongoing = 0
    resolved_durations: List[float] = []
    for inc in find_incidents(target, results):
        end = inc.ended if inc.ended is not None else now
        # Overlap of [inc.started, end] with [window_start, now], clamped.
        lo, hi = max(inc.started, window_start), min(end, now)
        if hi <= lo:
            continue  # ended before the window opened
        incidents += 1
        clamped = hi - lo
        downtime_s += clamped
        if inc.ongoing:
            ongoing += 1
        else:
            resolved_durations.append(clamped)

    mttr_min = (round(sum(resolved_durations) / len(resolved_durations) / 60.0, 1)
                if resolved_durations else None)
    return UptimeSummary(
        target=target,
        days=days,
        window_start=window_start,
        now=now,
        checks=len(windowed),
        up_checks=up,
        uptime_pct=round(100.0 * up / len(windowed), 1) if windowed else None,
        incidents=incidents,
        ongoing=ongoing,
        downtime_s=downtime_s,
        mttr_min=mttr_min,
    )
