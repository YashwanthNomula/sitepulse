"""Core data models for sitepulse."""
from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional


@dataclass
class Target:
    """A website to monitor."""

    name: str
    url: str
    interval_s: float = 60.0
    timeout_s: float = 10.0
    expect: List[int] = field(default_factory=lambda: [200])
    keyword: Optional[str] = None
    method: str = "GET"
    headers: Dict[str, str] = field(default_factory=dict)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "name": self.name,
            "url": self.url,
            "interval_s": self.interval_s,
            "timeout_s": self.timeout_s,
            "expect": self.expect,
            "keyword": self.keyword,
            "method": self.method,
            "headers": self.headers,
        }

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "Target":
        return cls(
            name=data["name"],
            url=data["url"],
            interval_s=float(data.get("interval_s", 60.0)),
            timeout_s=float(data.get("timeout_s", 10.0)),
            expect=list(data.get("expect", [200])),
            keyword=data.get("keyword"),
            method=str(data.get("method", "GET")).upper(),
            headers=dict(data.get("headers", {})),
        )


@dataclass
class CheckResult:
    """The outcome of a single check against a target."""

    name: str
    url: str
    timestamp: float
    ok: bool
    status: Optional[int] = None
    latency_ms: Optional[float] = None
    error: Optional[str] = None
    keyword_ok: Optional[bool] = None

    @property
    def age_s(self) -> float:
        return max(0.0, time.time() - self.timestamp)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "name": self.name,
            "url": self.url,
            "timestamp": self.timestamp,
            "ok": self.ok,
            "status": self.status,
            "latency_ms": self.latency_ms,
            "error": self.error,
            "keyword_ok": self.keyword_ok,
        }

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "CheckResult":
        return cls(
            name=data["name"],
            url=data["url"],
            timestamp=float(data["timestamp"]),
            ok=bool(data["ok"]),
            status=data.get("status"),
            latency_ms=data.get("latency_ms"),
            error=data.get("error"),
            keyword_ok=data.get("keyword_ok"),
        )
