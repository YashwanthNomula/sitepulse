"""Downtime incidents: consecutive failed checks grouped into outages.

Where :meth:`History.stats <sitepulse.store.History.stats>` answers "how much
was it down", this module answers "when, and for how long". A run of one or
more consecutive failed checks is one incident; it ends at the first
successful check afterwards, or is still ongoing if the latest check failed.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import List, Optional

from .models import CheckResult


@dataclass
class Incident:
    """One stretch of downtime for a target."""

    target: str
    started: float               # epoch of the first failed check
    ended: Optional[float]       # epoch of the first successful check after,
                                 # or None when still ongoing
    failures: int                # failed checks inside the incident
    error: Optional[str]         # first failure's error (the likely cause)

    @property
    def ongoing(self) -> bool:
        return self.ended is None

    def duration_s(self, now: Optional[float] = None) -> float:
        """Seconds from start to end (or to `now` for ongoing incidents)."""
        import time
        end = self.ended if self.ended is not None else (now if now is not None
                                                         else time.time())
        return max(0.0, end - self.started)


def find_incidents(target: str, results: List[CheckResult]) -> List[Incident]:
    """Group a target's check results (chronological) into incidents."""
    incidents: List[Incident] = []
    start: Optional[CheckResult] = None
    failures = 0
    for r in results:
        if r.ok:
            if start is not None:
                incidents.append(Incident(target=target, started=start.timestamp,
                                          ended=r.timestamp, failures=failures,
                                          error=start.error))
                start = None
        else:
            if start is None:
                start = r
                failures = 0
            failures += 1
    if start is not None:
        incidents.append(Incident(target=target, started=start.timestamp,
                                  ended=None, failures=failures,
                                  error=start.error))
    return incidents


def fmt_duration(seconds: float) -> str:
    """Compact human duration: 45s, 12m 05s, 3h 02m, 2d 04h."""
    s = int(seconds)
    if s < 60:
        return f"{s}s"
    m, s = divmod(s, 60)
    if m < 60:
        return f"{m}m {s:02d}s"
    h, m = divmod(m, 60)
    if h < 24:
        return f"{h}h {m:02d}m"
    d, h = divmod(h, 24)
    return f"{d}d {h:02d}h"


def fmt_time(epoch: float) -> str:
    import time
    return time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(epoch))
