"""Tests for the dashboard renderer (pure string functions)."""
from __future__ import annotations

import time

from sitepulse.dashboard import (
    fmt_ms,
    latency_bar,
    render_frame,
    sparkline,
    status_dot,
)
from sitepulse.models import CheckResult, Target
from sitepulse.store import History


def _ok(name="web", latency=42.0):
    return CheckResult(name=name, url="http://x/", timestamp=time.time(),
                       ok=True, status=200, latency_ms=latency)


def _down(name="web"):
    return CheckResult(name=name, url="http://x/", timestamp=time.time(),
                       ok=False, status=500, latency_ms=None, error="HTTP 500")


def test_sparkline_marks_failures():
    s = sparkline([10.0, None, 20.0], use_color=False)
    assert "✕" in s
    assert len(s) == 3


def test_sparkline_empty():
    assert "no data" in sparkline([], use_color=False)


def test_sparkline_scales():
    s = sparkline([1.0, 100.0], use_color=False)
    assert len(s) == 2 and s[0] != s[1]


def test_fmt_ms():
    assert fmt_ms(None) == "—"
    assert fmt_ms(42.0) == "42ms"
    assert fmt_ms(1500.0) == "1.50s"


def test_status_dot_no_color():
    assert status_dot(True, False) == "●"
    assert status_dot(False, False) == "●"
    assert status_dot(None, False) == "○"


def test_latency_bar_width():
    bar = latency_bar(500.0, scale_ms=1000.0, width=10, use_color=False)
    assert bar.count("█") == 5 and bar.count("░") == 5


def test_render_frame_up_and_down():
    targets = [Target(name="web", url="http://x/"), Target(name="api", url="http://y/")]
    h = History()
    h.record(_ok("web", 42.0))
    h.record(_down("api"))
    frame = render_frame(targets, h, use_color=False)
    assert "sitepulse" in frame
    assert "web" in frame and "UP" in frame
    assert "api" in frame and "DOWN" in frame
    assert "HTTP 500" in frame
    assert "100.0%" in frame  # web uptime


def test_render_frame_pending():
    targets = [Target(name="new", url="http://z/")]
    frame = render_frame(targets, History(), use_color=False)
    assert "pending" in frame


def test_render_frame_no_ansi_when_disabled():
    targets = [Target(name="web", url="http://x/")]
    h = History()
    h.record(_ok())
    frame = render_frame(targets, h, use_color=False)
    assert "\x1b[" not in frame
