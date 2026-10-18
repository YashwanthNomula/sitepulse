"""Multi-channel alert failover/fan-out: routing, per-channel outcomes, retries."""
from __future__ import annotations

import json
import smtplib
from urllib.error import URLError

import pytest

from sitepulse.alerts import (
    AlertState,
    ChannelResult,
    EmailSender,
    MultiChannelSender,
    SmtpConfig,
    WebhookSender,
    process_alerts,
)
from sitepulse.cli import main
from sitepulse.models import CheckResult, Target
from sitepulse.store import (
    History,
    load_alert_mode,
    load_alert_primary,
    save_alert_mode,
    save_alert_primary,
    save_webhooks,
)


HOOK = "https://hooks.example.com/sitepulse"
USER = "ops@example.com"


class _DummyResponse:
    def __enter__(self):
        return self

    def __exit__(self, *args):
        return False


class FakeOpener:
    """Stand-in for urllib.request.urlopen: records POSTs, optionally fails."""

    def __init__(self, fail=False):
        self.posts = []  # (url, payload dict)
        self.fail = fail

    def __call__(self, req, timeout=None):
        if self.fail:
            raise URLError("connection refused")
        self.posts.append((req.full_url, json.loads(req.data.decode("utf-8"))))
        return _DummyResponse()


class FakeSMTP:
    """Stand-in for smtplib.SMTP: records sent messages, optionally fails."""

    instances = []

    def __init__(self, host, port, timeout=None, fail=False):
        self.fail = fail
        self.sent = []
        FakeSMTP.instances.append(self)

    def starttls(self):
        pass

    def login(self, user, password):
        pass

    def send_message(self, msg):
        if self.fail:
            raise smtplib.SMTPException("relay refused")
        self.sent.append(msg)

    def quit(self):
        pass


@pytest.fixture(autouse=True)
def fake_smtp(monkeypatch):
    FakeSMTP.instances = []
    monkeypatch.setattr(smtplib, "SMTP", FakeSMTP)
    monkeypatch.setattr(smtplib, "SMTP_SSL", FakeSMTP)
    yield
    FakeSMTP.instances = []


@pytest.fixture
def workdir(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("SITEPULSE_CONFIG", str(tmp_path / "sitepulse.json"))
    monkeypatch.setenv("SITEPULSE_HISTORY", str(tmp_path / "sitepulse.history.json"))
    return tmp_path


@pytest.fixture
def smtp_env(monkeypatch):
    monkeypatch.setenv("SITEPULSE_SMTP_HOST", "smtp.example.com")
    monkeypatch.setenv("SITEPULSE_SMTP_FROM", "sitepulse@example.com")


def _target(name="web", url="http://x/"):
    return Target(name=name, url=url)


def _res(name, ok, ts, error=None):
    return CheckResult(
        name=name, url="http://x/", timestamp=ts, ok=ok,
        status=200 if ok else 500,
        latency_ms=10.0 if ok else None,
        error=None if ok else (error or "HTTP 500"),
    )


def _smtp_config():
    return SmtpConfig(host="smtp.example.com", from_addr="sitepulse@example.com")


def _mc(fake, recipients=(USER,), **kw):
    sender = WebhookSender([HOOK], opener=fake)
    email = EmailSender(list(recipients), _smtp_config()) if recipients else None
    return MultiChannelSender(sender, email, **kw)


def _process(history, state, fake, recipients=(USER,), targets=None,
             mode="failover", primary="webhook"):
    sender = WebhookSender([HOOK], opener=fake)
    email = EmailSender(list(recipients), _smtp_config()) if recipients else None
    targets = targets if targets is not None else [_target()]
    return process_alerts(targets, history, state, sender, email,
                          mode=mode, primary=primary)


def _emails_sent():
    return [m for i in FakeSMTP.instances for m in i.sent]


# -- MultiChannelSender ------------------------------------------------

def test_failover_skips_secondary_when_primary_succeeds():
    fake = FakeOpener()
    mc = _mc(fake, mode="failover")
    report = mc.broadcast({"event": "incident.started"})
    assert report.attempted == ["webhook"]
    assert len(fake.posts) == 1
    assert _emails_sent() == []  # email never attempted
    assert all(r.ok for r in report.results)


def test_failover_falls_back_to_secondary_on_primary_failure():
    fake = FakeOpener(fail=True)
    mc = _mc(fake, mode="failover")
    report = mc.broadcast({"event": "incident.started"})
    assert report.attempted == ["webhook", "email"]
    assert fake.posts == []
    assert len(_emails_sent()) == 1
    by_channel = {r.channel: r for r in report.results}
    assert isinstance(by_channel["webhook"], ChannelResult)
    assert by_channel["webhook"].destination == HOOK
    assert not by_channel["webhook"].ok
    assert "refused" in by_channel["webhook"].error
    email_result = by_channel["email"]
    assert email_result.destination == USER
    assert email_result.ok and email_result.error is None


def test_failover_respects_primary_email():
    fake = FakeOpener()
    mc = _mc(fake, mode="failover", primary="email")
    report = mc.broadcast({"event": "x"})
    assert report.attempted == ["email"]
    assert len(_emails_sent()) == 1
    assert fake.posts == []  # webhook skipped


def test_fanout_sends_via_both_channels():
    fake = FakeOpener()
    mc = _mc(fake, mode="fanout")
    report = mc.broadcast({"event": "incident.started"})
    assert report.attempted == ["webhook", "email"]
    assert len(fake.posts) == 1
    assert len(_emails_sent()) == 1
    assert {r.channel for r in report.results} == {"webhook", "email"}


def test_fanout_failure_in_one_channel_does_not_block_the_other():
    fake = FakeOpener(fail=True)
    mc = _mc(fake, mode="fanout")
    report = mc.broadcast({"event": "incident.started"})
    assert report.attempted == ["webhook", "email"]
    assert len(_emails_sent()) == 1  # email still went out
    by_channel = {r.channel: r for r in report.results}
    assert not by_channel["webhook"].ok and by_channel["email"].ok


def test_unconfigured_channel_is_skipped():
    mc = MultiChannelSender(WebhookSender([HOOK], opener=FakeOpener()),
                            None, mode="failover")
    assert mc.available_channels() == ["webhook"]
    report = mc.broadcast({"event": "x"})
    assert report.attempted == ["webhook"]


def test_invalid_mode_and_primary_raise():
    with pytest.raises(ValueError):
        MultiChannelSender(WebhookSender([]), mode="bogus")
    with pytest.raises(ValueError):
        MultiChannelSender(WebhookSender([]), primary="sms")


def test_sender_deliver_reports_per_destination_outcome():
    sender = WebhookSender([HOOK], opener=FakeOpener(fail=True))
    results = sender.deliver({"x": 1})
    assert len(results) == 1
    assert results[0][0] == HOOK and "refused" in results[0][1]
    failures = sender.broadcast({"x": 1})
    assert failures == results  # broadcast contract unchanged


# -- process_alerts: failover ------------------------------------------

def test_failover_webhook_success_means_no_email():
    h, state = History(), AlertState()
    h.record(_res("web", False, 100.0))
    started, resolved, failures = _process(h, state, FakeOpener())
    assert (started, resolved, failures) == (1, 0, [])
    assert _emails_sent() == []
    entry = state.open_for("web")
    # skipped channels count as delivered — never retried later
    assert entry["started_delivered"] == {"webhook": True, "email": True}


def test_failover_webhook_failure_falls_back_to_email():
    h, state = History(), AlertState()
    h.record(_res("web", False, 100.0))
    started, resolved, failures = _process(h, state, FakeOpener(fail=True))
    assert started == 1 and resolved == 0
    assert len(failures) == 1
    assert failures[0][0] == HOOK and "refused" in failures[0][1]
    msgs = _emails_sent()
    assert len(msgs) == 1
    assert msgs[0]["Subject"] == "[sitepulse] incident.started: web"
    entry = state.open_for("web")
    assert entry["started_delivered"] == {"webhook": False, "email": True}


def test_failover_both_channels_fail_surfaces_errors(monkeypatch):
    h, state = History(), AlertState()
    h.record(_res("web", False, 100.0))
    monkeypatch.setattr(EmailSender, "deliver",
                        lambda self, p: [(USER, "relay refused")])
    started, resolved, failures = _process(h, state, FakeOpener(fail=True))
    assert (started, resolved) == (0, 0)
    assert len(failures) == 2
    assert {d for d, _ in failures} == {HOOK, USER}
    assert state.open_for("web") is None  # nothing delivered → retried later


def test_failover_retries_only_pending_channel_no_double_send():
    h, state = History(), AlertState()
    h.record(_res("web", False, 100.0))

    # run 1: webhook down → email carries the alert (counts as sent)
    bad = FakeOpener(fail=True)
    started, resolved, failures = _process(h, state, bad)
    assert (started, resolved) == (1, 0)
    assert len(failures) == 1
    assert failures[0][0] == HOOK and "refused" in failures[0][1]
    assert len(_emails_sent()) == 1

    # run 2: webhook back → only webhook is retried, email not re-sent;
    # the transition was already counted, so this run is quiet
    good = FakeOpener()
    started, resolved, failures = _process(h, state, good)
    assert (started, resolved, failures) == (0, 0, [])
    assert len(good.posts) == 1
    assert good.posts[0][1]["event"] == "incident.started"
    assert len(_emails_sent()) == 1  # no duplicate email

    # run 3: everything delivered → quiet
    assert _process(h, state, FakeOpener()) == (0, 0, [])
    assert len(_emails_sent()) == 1


def test_failover_resolved_falls_back_without_double_send():
    h, state = History(), AlertState()
    h.record(_res("web", True, 10.0))
    h.record(_res("web", False, 100.0))
    assert _process(h, state, FakeOpener()) == (1, 0, [])  # started via webhook

    # incident resolves while the webhook is down → resolved via email
    h.record(_res("web", True, 200.0))
    started, resolved, failures = _process(h, state, FakeOpener(fail=True))
    assert (started, resolved) == (0, 1)  # delivered via the fallback channel
    assert len(failures) == 1
    assert failures[0][0] == HOOK and "refused" in failures[0][1]
    msgs = _emails_sent()
    assert len(msgs) == 1
    assert msgs[0]["Subject"] == "[sitepulse] incident.resolved: web"
    assert state.open_for("web") is not None  # webhook delivery still pending

    # webhook recovers → resolved retried there only, email not re-sent
    good = FakeOpener()
    started, resolved, failures = _process(h, state, good)
    assert (started, resolved, failures) == (0, 0, [])
    assert len(good.posts) == 1
    assert good.posts[0][1]["event"] == "incident.resolved"
    assert len(_emails_sent()) == 1
    assert state.open_for("web") is None


# -- process_alerts: fanout --------------------------------------------

def test_fanout_partial_failure_retries_only_failed_channel(monkeypatch):
    h, state = History(), AlertState()
    h.record(_res("web", False, 100.0))

    # run 1: webhook ok, email fails → alert counted (it went out),
    # but the email failure is surfaced and retried later
    monkeypatch.setattr(smtplib, "SMTP",
                        lambda *a, **k: FakeSMTP(*a, fail=True, **k))
    started, resolved, failures = _process(h, state, FakeOpener(),
                                           mode="fanout")
    assert (started, resolved) == (1, 0)
    assert failures == [(USER, "relay refused")]

    # run 2: only email retried — webhook is not double-sent
    monkeypatch.setattr(smtplib, "SMTP", FakeSMTP)
    good = FakeOpener()
    started, resolved, failures = _process(h, state, good, mode="fanout")
    assert (started, resolved, failures) == (0, 0, [])
    assert good.posts == []  # webhook already delivered → not re-sent
    assert len(_emails_sent()) == 1


def test_fanout_full_delivery_then_quiet():
    h, state = History(), AlertState()
    h.record(_res("web", False, 100.0))
    fake = FakeOpener()
    assert _process(h, state, fake, mode="fanout") == (1, 0, [])
    assert len(fake.posts) == 1 and len(_emails_sent()) == 1
    # next run: nothing pending → no double-send on either channel
    assert _process(h, state, FakeOpener(), mode="fanout") == (0, 0, [])
    assert len(_emails_sent()) == 1


# -- config ------------------------------------------------------------

def test_alert_routing_config_roundtrip(tmp_path):
    cfg = str(tmp_path / "sitepulse.json")
    assert load_alert_mode(cfg) == "fanout"
    assert load_alert_primary(cfg) == "webhook"
    save_alert_mode(cfg, "failover")
    save_alert_primary(cfg, "email")
    assert load_alert_mode(cfg) == "failover"
    assert load_alert_primary(cfg) == "email"
    # preserved alongside other keys
    save_webhooks(cfg, [HOOK])
    assert load_alert_mode(cfg) == "failover"
    # garbage in the config falls back to defaults
    data = json.loads(open(cfg).read())
    data["alert_mode"] = "bogus"
    data["alert_primary"] = "sms"
    json.dump(data, open(cfg, "w"))
    assert load_alert_mode(cfg) == "fanout"
    assert load_alert_primary(cfg) == "webhook"
    with pytest.raises(ValueError):
        save_alert_mode(cfg, "bogus")
    with pytest.raises(ValueError):
        save_alert_primary(cfg, "sms")


# -- CLI ---------------------------------------------------------------

def test_cli_alert_mode_set_and_show(workdir, capsys):
    assert main(["alert", "mode"]) == 0
    assert "alert mode: fanout" in capsys.readouterr().out

    assert main(["alert", "mode", "failover", "--primary", "email"]) == 0
    out = capsys.readouterr().out
    assert "alert mode: failover (primary channel: email)" in out
    assert load_alert_mode(str(workdir / "sitepulse.json")) == "failover"
    assert load_alert_primary(str(workdir / "sitepulse.json")) == "email"


def test_cli_alert_list_shows_mode(workdir, capsys):
    assert main(["alert", "mode", "failover"]) == 0
    capsys.readouterr()
    assert main(["alert", "list"]) == 0
    assert "alert mode: failover" in capsys.readouterr().out


def test_cli_check_uses_failover_mode(workdir, server, monkeypatch, smtp_env,
                                      capsys):
    assert main(["add", server + "/boom", "--name", "bad"]) == 0
    assert main(["alert", "add", HOOK]) == 0
    assert main(["alert", "email", "add", USER]) == 0
    assert main(["alert", "mode", "failover"]) == 0
    capsys.readouterr()

    monkeypatch.setattr("urllib.request.urlopen", FakeOpener(fail=True))
    assert main(["check"]) == 1  # target down
    err = capsys.readouterr().err
    assert "alert webhook failed" in err and HOOK in err
    # the alert still went out via the fallback channel
    msgs = _emails_sent()
    assert len(msgs) == 1
    assert msgs[0]["Subject"] == "[sitepulse] incident.started: bad"
