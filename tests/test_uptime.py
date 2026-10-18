"""Tests for the uptime/SLA summary: window clamping, ongoing incidents,
no-incident windows, zero checks, and the CLI."""

import json
import time

import pytest

from sitepulse.cli import main
from sitepulse.models import CheckResult
from sitepulse.store import History
from sitepulse.uptime import uptime_summary

NOW = 1_000_000.0
DAY = 86400.0


def _r(ts, ok, error=None, name="api"):
    return CheckResult(name=name, url="http://x", timestamp=float(ts), ok=ok,
                       status=200 if ok else 500, latency_ms=50.0, error=error)


def test_uptime_pct_uses_only_window_checks():
    results = [
        _r(NOW - 10 * DAY, False, "old"),   # outside the window: ignored
        _r(NOW - 10 * DAY, True),            # outside the window: ignored
        _r(NOW - 0.5 * DAY, True),
        _r(NOW - 0.4 * DAY, True),
        _r(NOW - 0.3 * DAY, False, "boom"),
        _r(NOW - 0.2 * DAY, True),
        _r(NOW - 0.1 * DAY, True),
    ]
    s = uptime_summary("api", results, days=1, now=NOW)
    assert s.checks == 5
    assert s.up_checks == 4
    assert s.uptime_pct == 80.0
    assert s.incidents == 1
    assert s.ongoing == 0
    assert s.downtime_s == pytest.approx(0.1 * DAY)
    assert s.mttr_min == pytest.approx(round(0.1 * DAY / 60.0, 1))


def test_incident_starting_before_window_is_clamped():
    # Incident started 2 days ago, resolved 12h ago; window is 1 day.
    results = [
        _r(NOW - 2 * DAY, False, "boom"),
        _r(NOW - 1.5 * DAY, False, "boom"),
        _r(NOW - 0.5 * DAY, True),
        _r(NOW - 0.1 * DAY, True),
    ]
    s = uptime_summary("api", results, days=1, now=NOW)
    assert s.incidents == 1
    # Only the in-window portion counts: window_start -> resolution.
    assert s.downtime_s == pytest.approx(0.5 * DAY)
    assert s.mttr_min == pytest.approx(round(0.5 * DAY / 60.0, 1))
    # Failures were out of window, so in-window uptime is 100%.
    assert s.uptime_pct == 100.0


def test_ongoing_incident_counts_but_not_in_mttr():
    results = [
        _r(NOW - 0.5 * DAY, True),
        _r(NOW - 2 * 3600, False, "boom"),
        _r(NOW - 3600, False, "boom"),
    ]
    s = uptime_summary("api", results, days=1, now=NOW)
    assert s.incidents == 1
    assert s.ongoing == 1
    assert s.downtime_s == pytest.approx(2 * 3600)
    assert s.mttr_min is None  # no resolved incidents


def test_ongoing_incident_spanning_whole_window_clamps_to_window():
    results = [
        _r(NOW - 5 * DAY, False, "boom"),
        _r(NOW - 4 * DAY, False, "boom"),
    ]
    s = uptime_summary("api", results, days=1, now=NOW)
    assert s.incidents == 1
    assert s.ongoing == 1
    assert s.downtime_s == pytest.approx(DAY)
    assert s.checks == 0
    assert s.uptime_pct is None


def test_incident_ended_before_window_is_ignored():
    results = [
        _r(NOW - 5 * DAY, False, "boom"),
        _r(NOW - 4 * DAY, True),
        _r(NOW - 0.5 * DAY, True),
    ]
    s = uptime_summary("api", results, days=1, now=NOW)
    assert s.incidents == 0
    assert s.ongoing == 0
    assert s.downtime_s == 0.0
    assert s.mttr_min is None
    assert s.uptime_pct == 100.0


def test_mttr_averages_resolved_incidents():
    results = [
        _r(NOW - 0.9 * DAY, False, "a"),   # 1h outage
        _r(NOW - 0.9 * DAY + 3600, True),
        _r(NOW - 0.5 * DAY, True),
        _r(NOW - 0.4 * DAY, False, "b"),   # 3h outage
        _r(NOW - 0.4 * DAY + 3 * 3600, True),
    ]
    s = uptime_summary("api", results, days=1, now=NOW)
    assert s.incidents == 2
    assert s.ongoing == 0
    assert s.mttr_min == pytest.approx(120.0)  # mean of 60 and 180 minutes


def test_no_incidents_in_window():
    results = [_r(NOW - 0.5 * DAY, True), _r(NOW - 0.1 * DAY, True)]
    s = uptime_summary("api", results, days=1, now=NOW)
    assert s.checks == 2
    assert s.uptime_pct == 100.0
    assert s.incidents == 0
    assert s.downtime_s == 0.0
    assert s.mttr_min is None


def test_zero_checks_in_window():
    s = uptime_summary("api", [_r(NOW - 10 * DAY, True)], days=1, now=NOW)
    assert s.checks == 0
    assert s.uptime_pct is None
    assert s.incidents == 0
    assert s.mttr_min is None


def test_days_must_be_positive():
    with pytest.raises(ValueError):
        uptime_summary("api", [], days=0, now=NOW)
    with pytest.raises(ValueError):
        uptime_summary("api", [], days=-3, now=NOW)


def _seed(tmp_path, monkeypatch):
    monkeypatch.setenv("SITEPULSE_CONFIG", str(tmp_path / "sitepulse.json"))
    monkeypatch.setenv("SITEPULSE_HISTORY", str(tmp_path / "sitepulse.history.json"))
    assert main(["add", "http://x", "--name", "api"]) == 0
    assert main(["add", "http://y", "--name", "quiet"]) == 0
    hist = History(str(tmp_path / "sitepulse.history.json"))
    hist.load()
    now = time.time()
    # 30-minute outage 2h ago, then healthy: 3 of 5 checks up.
    for r in [
        _r(now - 3 * 3600, True),
        _r(now - 2 * 3600, False, "boom"),
        _r(now - 2 * 3600 + 900, False, "boom"),
        _r(now - 2 * 3600 + 1800, True),
        _r(now - 3600, True),
    ]:
        hist.record(r)
    hist.save()


def test_cli_uptime_table(tmp_path, monkeypatch, capsys):
    _seed(tmp_path, monkeypatch)
    capsys.readouterr()  # discard `add` chatter
    assert main(["uptime", "api", "--days", "1"]) == 0
    out = capsys.readouterr().out
    assert "SLA summary" in out
    assert "60.0%" in out
    assert "3/5 checks up" in out
    assert "MTTR" in out
    assert "30m 00s" in out  # 1800s outage


def test_cli_uptime_json(tmp_path, monkeypatch, capsys):
    _seed(tmp_path, monkeypatch)
    capsys.readouterr()
    assert main(["uptime", "api", "--days", "1", "--json"]) == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["target"] == "api"
    assert payload["window_days"] == 1.0
    assert payload["checks"] == 5
    assert payload["uptime_pct"] == 60.0
    assert payload["incidents"] == 1
    assert payload["ongoing_incidents"] == 0
    assert payload["mttr_min"] == 30.0


def test_cli_uptime_no_checks(tmp_path, monkeypatch, capsys):
    _seed(tmp_path, monkeypatch)
    capsys.readouterr()
    assert main(["uptime", "quiet", "--days", "7"]) == 0
    assert "no checks recorded for 'quiet' in the last 7 days" in capsys.readouterr().out


def test_cli_uptime_bad_args(tmp_path, monkeypatch, capsys):
    _seed(tmp_path, monkeypatch)
    assert main(["uptime", "ghost"]) == 1
    assert "no target named 'ghost'" in capsys.readouterr().err
    assert main(["uptime", "api", "--days", "0"]) == 1
    assert "--days must be positive" in capsys.readouterr().err
