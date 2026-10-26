"""Tests for downtime incidents: grouping failures into outages."""

import csv

import pytest

from sitepulse.cli import main
from sitepulse.incidents import find_incidents, fmt_duration, fmt_time
from sitepulse.models import CheckResult
from sitepulse.store import History


def _r(ts, ok, error=None, name="api"):
    return CheckResult(name=name, url="http://x", timestamp=float(ts), ok=ok,
                       status=200 if ok else 500, latency_ms=50.0, error=error)


def test_single_incident_boundaries():
    results = [_r(100, True), _r(160, False, "timeout"), _r(220, False, "timeout"),
               _r(280, True)]
    incs = find_incidents("api", results)
    assert len(incs) == 1
    inc = incs[0]
    assert (inc.started, inc.ended) == (160.0, 280.0)
    assert inc.failures == 2
    assert inc.error == "timeout"
    assert not inc.ongoing
    assert inc.duration_s() == pytest.approx(120.0)


def test_ongoing_incident():
    results = [_r(100, True), _r(160, False, "HTTP 500")]
    incs = find_incidents("api", results)
    assert len(incs) == 1
    assert incs[0].ongoing
    assert incs[0].ended is None
    assert incs[0].duration_s(now=460.0) == pytest.approx(300.0)


def test_no_incidents_when_all_up():
    assert find_incidents("api", [_r(100, True), _r(160, True)]) == []


def test_multiple_incidents():
    results = [_r(0, False, "boom"), _r(60, True), _r(120, False, "timeout"),
               _r(180, True), _r(240, False, "dns")]
    incs = find_incidents("api", results)
    assert [i.failures for i in incs] == [1, 1, 1]
    assert [i.error for i in incs] == ["boom", "timeout", "dns"]
    assert incs[-1].ongoing


def test_fmt_duration():
    assert fmt_duration(45) == "45s"
    assert fmt_duration(125) == "2m 05s"
    assert fmt_duration(3 * 3600 + 2 * 60) == "3h 02m"
    assert fmt_duration(2 * 86400 + 4 * 3600) == "2d 04h"


def _seed_history(tmp_path, monkeypatch):
    monkeypatch.setenv("SITEPULSE_CONFIG", str(tmp_path / "sitepulse.json"))
    monkeypatch.setenv("SITEPULSE_HISTORY", str(tmp_path / "sitepulse.history.json"))
    assert main(["add", "http://x", "--name", "api"]) == 0
    assert main(["add", "http://y", "--name", "web"]) == 0
    hist = History(str(tmp_path / "sitepulse.history.json"))
    hist.load()
    for r in [_r(1000, True), _r(1060, False, "timeout"), _r(1120, False, "timeout"),
              _r(1180, True)]:
        hist.record(r)
    for r in [_r(1000, True, name="web"), _r(1060, True, name="web")]:
        hist.record(r)
    hist.save()


def test_cli_incidents_table(tmp_path, monkeypatch, capsys):
    _seed_history(tmp_path, monkeypatch)
    capsys.readouterr()  # discard `add` chatter
    assert main(["incidents"]) == 0
    out = capsys.readouterr().out
    assert "api" in out and "timeout" in out
    assert "2m 00s" in out  # incident ran 1060 -> 1180
    assert "web" not in out  # no incidents for web


def test_cli_incidents_filtered_and_empty(tmp_path, monkeypatch, capsys):
    _seed_history(tmp_path, monkeypatch)
    assert main(["incidents", "--target", "web"]) == 0
    assert "no incidents recorded" in capsys.readouterr().out
    assert main(["incidents", "--target", "api"]) == 0
    assert "timeout" in capsys.readouterr().out
    assert main(["incidents", "--target", "ghost"]) == 1


def _read_csv(path):
    with open(path, newline="", encoding="utf-8") as f:
        reader = csv.DictReader(f)
        rows = list(reader)
    return reader.fieldnames, rows


def test_cli_incidents_csv_export_columns_and_content(tmp_path, monkeypatch, capsys):
    _seed_history(tmp_path, monkeypatch)
    capsys.readouterr()  # discard `add` chatter
    out_path = tmp_path / "incidents.csv"
    assert main(["incidents", "--output", str(out_path)]) == 0
    out = capsys.readouterr().out
    # Terminal table is unchanged alongside the export.
    assert "timeout" in out and "wrote 1 incident to" in out
    fieldnames, rows = _read_csv(out_path)
    assert fieldnames == ["target", "start", "end", "duration_s", "checks",
                          "first_error"]
    assert len(rows) == 1
    row = rows[0]
    assert row["target"] == "api"
    assert row["start"] == fmt_time(1060)
    assert row["end"] == fmt_time(1180)
    assert row["duration_s"] == "120"
    assert row["checks"] == "2"
    assert row["first_error"] == "timeout"


def _seed_ongoing_history(tmp_path, monkeypatch):
    monkeypatch.setenv("SITEPULSE_CONFIG", str(tmp_path / "sitepulse.json"))
    monkeypatch.setenv("SITEPULSE_HISTORY", str(tmp_path / "sitepulse.history.json"))
    assert main(["add", "http://x", "--name", "api"]) == 0
    hist = History(str(tmp_path / "sitepulse.history.json"))
    hist.load()
    for r in [_r(1000, True), _r(1060, False, "dns"),
              _r(1120, False, "dns")]:
        hist.record(r)
    hist.save()


def test_cli_incidents_csv_export_ongoing(tmp_path, monkeypatch, capsys):
    _seed_ongoing_history(tmp_path, monkeypatch)
    capsys.readouterr()  # discard `add` chatter
    out_path = tmp_path / "ongoing.csv"
    assert main(["incidents", "--output", str(out_path)]) == 0
    _, rows = _read_csv(out_path)
    assert len(rows) == 1
    row = rows[0]
    assert row["target"] == "api"
    assert row["start"] == fmt_time(1060)
    assert row["end"] == "ONGOING"
    assert row["checks"] == "2"
    assert row["first_error"] == "dns"
    assert float(row["duration_s"]) >= 60.0  # start -> now, at least one interval


def test_cli_incidents_csv_export_no_incidents_writes_header(tmp_path, monkeypatch):
    _seed_history(tmp_path, monkeypatch)
    out_path = tmp_path / "empty.csv"
    assert main(["incidents", "--target", "web", "--output", str(out_path)]) == 0
    fieldnames, rows = _read_csv(out_path)
    assert fieldnames == ["target", "start", "end", "duration_s", "checks",
                          "first_error"]
    assert rows == []


def test_cli_incidents_csv_export_bad_path_fails_closed(tmp_path, monkeypatch, capsys):
    _seed_history(tmp_path, monkeypatch)
    capsys.readouterr()  # discard `add` chatter
    # Missing parent directory: nothing is created, no table is printed.
    bad_path = tmp_path / "missing-dir" / "incidents.csv"
    assert main(["incidents", "--output", str(bad_path)]) == 1
    captured = capsys.readouterr()
    assert "cannot write CSV export" in captured.err
    assert "timeout" not in captured.out
    assert not bad_path.exists()
    # A directory as the output path fails the same way.
    assert main(["incidents", "--output", str(tmp_path)]) == 1
    assert "cannot write CSV export" in capsys.readouterr().err
