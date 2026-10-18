"""Tests for the dashboard renderer (pure string functions)."""
from __future__ import annotations

import time

from sitepulse.dashboard import (
    fmt_ms,
    latency_bar,
    render_frame,
    render_ongoing_panel,
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


def _down_seq(name, n, error="HTTP 500", gap_s=10.0):
    """n consecutive failures, `gap_s` apart, ending just before now."""
    base = time.time() - n * gap_s
    return [
        CheckResult(name=name, url="http://x/", timestamp=base + i * gap_s,
                    ok=False, status=500, latency_ms=None, error=error)
        for i in range(n)
    ]


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


def test_ongoing_panel_lists_target_duration_failures_error():
    targets = [Target(name="api", url="http://y/")]
    h = History()
    for r in _down_seq("api", 5, error="HTTP 500", gap_s=30.0):
        h.record(r)
    lines = render_ongoing_panel(targets, h, use_color=False, width=100)
    assert lines, "expected a panel for the ongoing incident"
    body = "\n".join(lines)
    assert "1 ONGOING INCIDENT" in body
    assert "api" in body
    assert "5 checks failed" in body
    assert "HTTP 500" in body          # first error
    assert "started" in body
    assert "2m" in body                # ~2m30s of downtime so far, sane duration


def test_ongoing_panel_multiple_targets_longest_first():
    targets = [Target(name="new-outage", url="http://x/"),
               Target(name="old-outage", url="http://y/")]
    h = History()
    for r in _down_seq("new-outage", 2, gap_s=10.0):
        h.record(r)
    for r in _down_seq("old-outage", 20, gap_s=60.0):
        h.record(r)
    lines = render_ongoing_panel(targets, h, use_color=False, width=100)
    body = "\n".join(lines)
    assert "2 ONGOING INCIDENTS" in body
    assert body.index("old-outage") < body.index("new-outage")


def test_ongoing_panel_empty_when_all_up():
    targets = [Target(name="web", url="http://x/")]
    h = History()
    h.record(_ok("web"))
    assert render_ongoing_panel(targets, h, use_color=False, width=100) == []


def test_ongoing_panel_empty_when_incident_resolved():
    targets = [Target(name="api", url="http://y/")]
    h = History()
    base = time.time()
    h.record(CheckResult(name="api", url="http://y/", timestamp=base - 60,
                         ok=False, status=500, latency_ms=None, error="HTTP 500"))
    h.record(CheckResult(name="api", url="http://y/", timestamp=base,
                         ok=True, status=200, latency_ms=42.0))
    assert render_ongoing_panel(targets, h, use_color=False, width=100) == []


def test_render_frame_shows_ongoing_incidents_before_target_rows():
    targets = [Target(name="api", url="http://y/"), Target(name="web", url="http://x/")]
    h = History()
    for r in _down_seq("api", 3):
        h.record(r)
    h.record(_ok("web"))
    frame = render_frame(targets, h, use_color=False)
    assert "ONGOING" in frame
    # alert section sits above the per-target status rows ("uptime" only appears there)
    assert frame.index("ONGOING") < frame.index("uptime")


def test_render_frame_no_alert_section_when_nothing_ongoing():
    targets = [Target(name="web", url="http://x/"), Target(name="api", url="http://y/")]
    h = History()
    h.record(_ok("web"))
    h.record(_ok("api"))
    frame = render_frame(targets, h, use_color=False)
    assert "ONGOING" not in frame
