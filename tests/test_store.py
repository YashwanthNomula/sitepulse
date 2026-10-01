"""Tests for the rolling history store."""
from __future__ import annotations

import json
import time

from sitepulse.models import CheckResult
from sitepulse.store import History, load_targets, save_targets
from sitepulse.models import Target


def _res(name, ok, latency=None, ts=None):
    return CheckResult(
        name=name,
        url="http://x/",
        timestamp=ts if ts is not None else time.time(),
        ok=ok,
        status=200 if ok else 500,
        latency_ms=latency if latency is not None else (10.0 if ok else None),
        error=None if ok else "HTTP 500",
    )


def test_stats_empty():
    h = History()
    st = h.stats("nope")
    assert st["total"] == 0 and st["uptime_pct"] is None and st["last"] is None


def test_stats_uptime_and_percentiles():
    h = History()
    for i in range(8):
        h.record(_res("web", True, latency=float(10 + i * 10)))
    for _ in range(2):
        h.record(_res("web", False))
    st = h.stats("web")
    assert st["total"] == 10
    assert st["uptime_pct"] == 80.0
    assert st["avg_ms"] == 45.0
    assert st["p50_ms"] == 45.0
    assert st["consec_fail"] == 2


def test_ring_buffer_window():
    h = History(window=5)
    for _ in range(9):
        h.record(_res("web", True))
    assert h.stats("web")["total"] == 5


def test_save_and_load_roundtrip(tmp_path):
    path = str(tmp_path / "hist.json")
    h = History(path)
    h.record(_res("web", True, latency=12.5))
    h.record(_res("web", False))
    h.save()

    h2 = History(path)
    h2.load()
    st = h2.stats("web")
    assert st["total"] == 2 and st["uptime_pct"] == 50.0
    assert h2.last("web").latency_ms is None  # last one failed


def test_load_missing_file_is_fine(tmp_path):
    h = History(str(tmp_path / "nope.json"))
    h.load()  # must not raise
    assert h.stats("x")["total"] == 0


def test_load_corrupt_file_is_fine(tmp_path):
    path = tmp_path / "bad.json"
    path.write_text("{not json")
    h = History(str(path))
    h.load()
    assert h.stats("x")["total"] == 0


def test_recent_latencies_marks_failures():
    h = History()
    h.record(_res("web", True, latency=10.0))
    h.record(_res("web", False))
    h.record(_res("web", True, latency=20.0))
    assert h.recent_latencies("web") == [10.0, None, 20.0]


def test_target_config_roundtrip(tmp_path):
    path = str(tmp_path / "conf.json")
    targets = [
        Target(name="a", url="https://a.example", interval_s=30, keyword="hi"),
        Target(name="b", url="https://b.example", expect=[200, 301]),
    ]
    save_targets(path, targets)
    loaded = load_targets(path)
    assert [t.name for t in loaded] == ["a", "b"]
    assert loaded[0].keyword == "hi" and loaded[0].interval_s == 30.0
    assert loaded[1].expect == [200, 301]
    # file is valid JSON with the expected shape
    raw = json.loads(open(path).read())
    assert set(raw) == {"targets"}
