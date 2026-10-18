"""Incident webhook alerts: payloads, transitions, CLI management, store roundtrip."""
from __future__ import annotations

import json
import os
import time
from urllib.error import URLError

import pytest

from sitepulse.alerts import (
    AlertState,
    WebhookSender,
    build_payload,
    build_test_payload,
    process_alerts,
)
from sitepulse.cli import main
from sitepulse.incidents import Incident
from sitepulse.models import CheckResult, Target
from sitepulse.store import History, load_webhooks, save_targets, save_webhooks


HOOK = "https://hooks.example.com/sitepulse"


class _DummyResponse:
    def __enter__(self):
        return self

    def __exit__(self, *args):
        return False


class FakeOpener:
    """Stand-in for urllib.request.urlopen: records POSTs, optionally fails."""

    def __init__(self, fail: bool = False):
        self.posts = []  # (url, payload dict, headers dict)
        self.fail = fail

    def __call__(self, req, timeout=None):
        if self.fail:
            raise URLError("connection refused")
        self.posts.append(
            (req.full_url, json.loads(req.data.decode("utf-8")),
             {k.lower(): v for k, v in req.header_items()})
        )
        return _DummyResponse()


@pytest.fixture
def workdir(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("SITEPULSE_CONFIG", str(tmp_path / "sitepulse.json"))
    monkeypatch.setenv("SITEPULSE_HISTORY", str(tmp_path / "sitepulse.history.json"))
    return tmp_path


def _target(name="web", url="http://x/"):
    return Target(name=name, url=url)


def _res(name, ok, ts, error=None):
    return CheckResult(
        name=name, url="http://x/", timestamp=ts, ok=ok,
        status=200 if ok else 500,
        latency_ms=10.0 if ok else None,
        error=None if ok else (error or "HTTP 500"),
    )


def _incident(name="web", started=100.0, ended=None, failures=2):
    return Incident(target=name, started=started, ended=ended,
                    failures=failures, error="HTTP 500")


# -- payloads ------------------------------------------------------------

def test_started_payload_shape():
    p = build_payload("incident.started", _target(), _incident())
    assert p["event"] == "incident.started"
    assert p["source"] == "sitepulse"
    assert p["target"] == "web" and p["url"] == "http://x/"
    assert p["failures"] == 2 and p["error"] == "HTTP 500"
    assert p["started_at"].startswith("1970-01-01T00:01:40")
    assert "ended_at" not in p and "duration_s" not in p


def test_resolved_payload_shape():
    p = build_payload("incident.resolved", _target(), _incident(), ended=160.0)
    assert p["event"] == "incident.resolved"
    assert p["ended_at"].startswith("1970-01-01T00:02:40")
    assert p["duration_s"] == 60.0


def test_test_payload_shape():
    p = build_test_payload(HOOK)
    assert p["event"] == "test" and p["url"] == HOOK


# -- sender --------------------------------------------------------------

def test_broadcast_posts_json_to_every_webhook():
    fake = FakeOpener()
    sender = WebhookSender([HOOK, HOOK + "/2"], opener=fake)
    failures = sender.broadcast({"hello": "world"})
    assert failures == []
    assert len(fake.posts) == 2
    for url, body, headers in fake.posts:
        assert body == {"hello": "world"}
        assert headers["content-type"] == "application/json"


def test_broadcast_collects_failures_without_raising():
    fake = FakeOpener(fail=True)
    sender = WebhookSender([HOOK, HOOK + "/2"], opener=fake)
    failures = sender.broadcast({"x": 1})
    assert len(failures) == 2
    assert all("refused" in err for _, err in failures)
    assert fake.posts == []


# -- transition detection ------------------------------------------------

def _process(history, state, fake, targets=None, webhooks=(HOOK,)):
    sender = WebhookSender(list(webhooks), opener=fake)
    targets = targets if targets is not None else [_target()]
    return process_alerts(targets, history, state, sender)


def test_no_alerts_when_everything_up():
    h, state, fake = History(), AlertState(), FakeOpener()
    h.record(_res("web", True, 100.0))
    h.record(_res("web", True, 160.0))
    assert _process(h, state, fake) == (0, 0, [])
    assert fake.posts == []


def test_no_alerts_without_webhooks():
    h, state, fake = History(), AlertState(), FakeOpener()
    h.record(_res("web", False, 100.0))
    assert _process(h, state, fake, webhooks=()) == (0, 0, [])
    assert fake.posts == []


def test_started_once_then_resolved(tmp_path):
    path = str(tmp_path / "alerts.json")
    h, state, fake = History(), AlertState(path), FakeOpener()

    # ongoing incident starts → one started alert
    h.record(_res("web", True, 10.0))
    h.record(_res("web", False, 100.0, "HTTP 500"))
    assert _process(h, state, fake) == (1, 0, [])
    assert len(fake.posts) == 1
    event, body, _ = fake.posts[0]
    assert body["event"] == "incident.started"
    assert body["failures"] == 1

    # more failures → no duplicate
    h.record(_res("web", False, 160.0, "HTTP 500"))
    assert _process(h, state, fake) == (0, 0, [])
    assert len(fake.posts) == 1

    # recovery → one resolved alert with duration
    h.record(_res("web", True, 200.0))
    assert _process(h, state, fake) == (0, 1, [])
    assert len(fake.posts) == 2
    _, body, _ = fake.posts[1]
    assert body["event"] == "incident.resolved"
    assert body["duration_s"] == 100.0
    assert body["failures"] == 2

    # settled → quiet, even across a state reload
    assert _process(h, state, fake) == (0, 0, [])
    state.save()
    state2 = AlertState(path)
    state2.load()
    assert _process(h, state2, fake) == (0, 0, [])
    assert len(fake.posts) == 2

    # a fresh incident later alerts again
    h.record(_res("web", False, 300.0, "HTTP 503"))
    assert _process(h, state2, fake) == (1, 0, [])
    assert fake.posts[-1][1]["error"] == "HTTP 503"


def test_targets_are_independent():
    h, state, fake = History(), AlertState(), FakeOpener()
    targets = [_target("a"), _target("b")]
    h.record(_res("a", False, 100.0))
    h.record(_res("b", True, 100.0))
    started, resolved, _ = _process(h, state, fake, targets=targets)
    assert (started, resolved) == (1, 0)
    assert fake.posts[0][1]["target"] == "a"


def test_failed_delivery_retries_next_run():
    h, state = History(), AlertState()
    h.record(_res("web", False, 100.0))
    bad = FakeOpener(fail=True)
    started, resolved, failures = _process(h, state, bad)
    assert (started, resolved) == (0, 0)
    assert len(failures) == 1
    assert state.open_for("web") is None  # not marked → retried later

    good = FakeOpener()
    assert _process(h, state, good) == (1, 0, [])
    assert len(good.posts) == 1


def test_corrupt_state_file_is_ignored(tmp_path):
    path = str(tmp_path / "alerts.json")
    with open(path, "w") as fh:
        fh.write("{not json")
    state = AlertState(path)
    state.load()  # must not raise
    assert state.open_for("web") is None


# -- store ----------------------------------------------------------------

def test_webhook_config_roundtrip(tmp_path):
    cfg = str(tmp_path / "sitepulse.json")
    assert load_webhooks(cfg) == []  # missing file → no webhooks
    save_webhooks(cfg, [HOOK])
    assert load_webhooks(cfg) == [HOOK]
    # save_targets must not clobber the webhooks key
    save_targets(cfg, [_target()])
    assert load_webhooks(cfg) == [HOOK]
    assert json.loads(open(cfg).read())["targets"][0]["name"] == "web"


# -- CLI ------------------------------------------------------------------

def _add_target(url, name="t"):
    assert main(["add", url, "--name", name]) == 0


def test_alert_add_list_remove(workdir, capsys):
    assert main(["alert", "add", HOOK]) == 0
    assert main(["alert", "add", HOOK]) == 1  # duplicate
    assert main(["alert", "add", "not-a-url"]) == 1

    assert main(["alert", "list"]) == 0
    out = capsys.readouterr().out
    assert HOOK in out

    assert main(["alert", "remove", HOOK]) == 0
    assert main(["alert", "remove", HOOK]) == 1  # already gone
    assert main(["alert", "list"]) == 0
    assert "no alert webhooks" in capsys.readouterr().out


def test_alert_test_sends_test_payload(workdir, monkeypatch, capsys):
    fake = FakeOpener()
    monkeypatch.setattr("urllib.request.urlopen", fake)
    assert main(["alert", "test", HOOK]) == 0
    assert len(fake.posts) == 1
    assert fake.posts[0][1]["event"] == "test"
    assert "test alert sent" in capsys.readouterr().out


def test_alert_test_reports_failure(workdir, monkeypatch):
    monkeypatch.setattr("urllib.request.urlopen", FakeOpener(fail=True))
    assert main(["alert", "test", HOOK]) == 1


def test_check_sends_started_alert_once(workdir, server, monkeypatch):
    _add_target(server + "/boom", name="bad")
    assert main(["alert", "add", HOOK]) == 0
    fake = FakeOpener()
    monkeypatch.setattr("urllib.request.urlopen", fake)

    assert main(["check"]) == 1  # target down
    assert len(fake.posts) == 1
    body = fake.posts[0][1]
    assert body["event"] == "incident.started"
    assert body["target"] == "bad"

    # second check: incident still open → no duplicate alert
    assert main(["check"]) == 1
    assert len(fake.posts) == 1

    # alert state persisted next to the config
    state_path = os.path.join(workdir, "sitepulse.alerts.json")
    assert os.path.exists(state_path)
    assert "bad" in json.loads(open(state_path).read())["open"]


def test_check_with_failing_webhook_still_records(workdir, server, monkeypatch):
    _add_target(server + "/boom", name="bad")
    assert main(["alert", "add", HOOK]) == 0
    monkeypatch.setattr("urllib.request.urlopen", FakeOpener(fail=True))
    assert main(["check"]) == 1  # check itself unaffected by webhook failure
    history = json.loads(open(os.environ["SITEPULSE_HISTORY"]).read())
    assert history["bad"][0]["ok"] is False
