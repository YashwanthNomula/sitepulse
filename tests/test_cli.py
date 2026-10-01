"""CLI integration tests: add/list/remove/check/report against local targets."""
from __future__ import annotations

import json
import os

import pytest

from sitepulse.cli import main


@pytest.fixture
def workdir(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("SITEPULSE_CONFIG", str(tmp_path / "sitepulse.json"))
    monkeypatch.setenv("SITEPULSE_HISTORY", str(tmp_path / "sitepulse.history.json"))
    return tmp_path


def _add(url, name="t", **kw):
    args = ["add", url, "--name", name]
    for k, v in kw.items():
        args += [f"--{k}", str(v)]
    assert main(args) == 0


def test_add_list_remove(workdir, capsys):
    _add("https://example.com", name="ex", interval=30)
    assert main(["list"]) == 0
    out = capsys.readouterr().out
    assert "ex" in out and "example.com" in out

    # duplicate name is rejected
    assert main(["add", "https://example.com", "--name", "ex"]) == 1

    assert main(["remove", "ex"]) == 0
    assert main(["remove", "ex"]) == 1  # already gone


def test_check_records_history_and_exit_codes(workdir, server, capsys):
    _add(server + "/fast", name="good")
    _add(server + "/boom", name="bad")

    rc = main(["check"])
    assert rc == 1  # one target down
    out = capsys.readouterr().out
    assert "good" in out and "bad" in out

    history = json.loads(open(os.environ["SITEPULSE_HISTORY"]).read())
    assert set(history) == {"good", "bad"}
    assert history["good"][0]["ok"] is True
    assert history["bad"][0]["ok"] is False


def test_check_all_up_exits_zero(workdir, server):
    _add(server + "/fast", name="good")
    assert main(["check"]) == 0


def test_check_json_output(workdir, server, capsys):
    _add(server + "/fast", name="good")
    assert main(["check", "--json"]) == 0
    out = capsys.readouterr().out
    payload = json.loads(out[out.index("["):])
    assert payload[0]["name"] == "good" and payload[0]["ok"] is True


def test_report_reads_history(workdir, server, capsys):
    _add(server + "/fast", name="good", interval=30)
    assert main(["check"]) == 0
    assert main(["report"]) == 0
    out = capsys.readouterr().out
    assert "good" in out and "100.0%" in out


def test_check_without_config_fails(workdir):
    assert main(["check"]) == 2


def test_watch_frames_mode(workdir, server, capsys):
    _add(server + "/fast", name="good", interval=5)
    # frames=1 renders a single dashboard frame then exits — used for demos/tests
    assert main(["watch", "--frames", "1", "--refresh", "0.01"]) == 0
    out = capsys.readouterr().out
    assert "sitepulse" in out and "good" in out
