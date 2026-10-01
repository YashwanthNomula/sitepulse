"""Rolling history store: keeps recent check results per target as JSON."""
from __future__ import annotations

import json
import os
from collections import deque
from typing import Dict, List, Optional

from .models import CheckResult, Target

DEFAULT_WINDOW = 240  # results kept per target


class History:
    """In-memory ring buffers per target, persisted to a JSON file."""

    def __init__(self, path: Optional[str] = None, window: int = DEFAULT_WINDOW):
        self.path = path
        self.window = window
        self._data: Dict[str, deque] = {}

    # -- recording -----------------------------------------------------
    def record(self, result: CheckResult) -> None:
        buf = self._data.setdefault(result.name, deque(maxlen=self.window))
        buf.append(result)

    def results_for(self, target_name: str) -> List[CheckResult]:
        return list(self._data.get(target_name, ()))

    def last(self, target_name: str) -> Optional[CheckResult]:
        buf = self._data.get(target_name)
        return buf[-1] if buf else None

    # -- statistics ----------------------------------------------------
    def stats(self, target_name: str) -> Dict[str, object]:
        results = self.results_for(target_name)
        total = len(results)
        if total == 0:
            return {
                "total": 0,
                "up": 0,
                "down": 0,
                "uptime_pct": None,
                "avg_ms": None,
                "p50_ms": None,
                "p95_ms": None,
                "last": None,
                "consec_fail": 0,
            }
        up = sum(1 for r in results if r.ok)
        latencies = sorted(r.latency_ms for r in results if r.ok and r.latency_ms is not None)
        consec = 0
        for r in reversed(results):
            if r.ok:
                break
            consec += 1
        return {
            "total": total,
            "up": up,
            "down": total - up,
            "uptime_pct": round(100.0 * up / total, 1),
            "avg_ms": round(sum(latencies) / len(latencies), 1) if latencies else None,
            "p50_ms": _percentile(latencies, 50),
            "p95_ms": _percentile(latencies, 95),
            "last": results[-1],
            "consec_fail": consec,
        }

    def recent_latencies(self, target_name: str, n: int = 40) -> List[Optional[float]]:
        """Last n results as latency-or-None (None marks a failure) for sparklines."""
        out: List[Optional[float]] = []
        for r in self.results_for(target_name)[-n:]:
            out.append(r.latency_ms if r.ok and r.latency_ms is not None else None)
        return out

    # -- persistence ---------------------------------------------------
    def save(self) -> None:
        if not self.path:
            return
        payload = {
            name: [r.to_dict() for r in buf] for name, buf in self._data.items()
        }
        tmp = self.path + ".tmp"
        os.makedirs(os.path.dirname(os.path.abspath(self.path)), exist_ok=True)
        with open(tmp, "w", encoding="utf-8") as fh:
            json.dump(payload, fh, indent=1)
        os.replace(tmp, self.path)

    def load(self) -> None:
        if not self.path or not os.path.exists(self.path):
            return
        try:
            with open(self.path, encoding="utf-8") as fh:
                payload = json.load(fh)
        except (json.JSONDecodeError, OSError):
            return
        for name, items in payload.items():
            buf: deque = deque(maxlen=self.window)
            for item in items[-self.window :]:
                try:
                    buf.append(CheckResult.from_dict(item))
                except (KeyError, TypeError, ValueError):
                    continue
            self._data[name] = buf


def _percentile(sorted_vals: List[float], pct: float) -> Optional[float]:
    if not sorted_vals:
        return None
    if len(sorted_vals) == 1:
        return round(sorted_vals[0], 1)
    k = (len(sorted_vals) - 1) * (pct / 100.0)
    lo = int(k)
    hi = min(lo + 1, len(sorted_vals) - 1)
    frac = k - lo
    return round(sorted_vals[lo] + (sorted_vals[hi] - sorted_vals[lo]) * frac, 1)


def load_targets(config_path: str) -> List[Target]:
    """Read targets from a JSON config file: {"targets": [...]}."""
    with open(config_path, encoding="utf-8") as fh:
        data = json.load(fh)
    raw = data.get("targets", [])
    if not isinstance(raw, list):
        raise ValueError("config 'targets' must be a list")
    return [Target.from_dict(item) for item in raw]


def save_targets(config_path: str, targets: List[Target]) -> None:
    tmp = config_path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as fh:
        json.dump({"targets": [t.to_dict() for t in targets]}, fh, indent=2)
        fh.write("\n")
    os.replace(tmp, config_path)


def default_history_path(config_path: str) -> str:
    base, _ext = os.path.splitext(config_path)
    return base + ".history.json"
