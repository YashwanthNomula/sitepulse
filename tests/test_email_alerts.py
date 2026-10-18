"""Email incident alerts: SMTP config, sender, transitions, CLI management."""
from __future__ import annotations

import json
import os
import smtplib
import time

import pytest

from sitepulse.alerts import (
    AlertState,
    EmailSender,
    SmtpConfig,
    SmtpConfigError,
    WebhookSender,
    build_payload,
    build_test_payload,
    email_body,
    email_subject,
    smtp_config_from_env,
    process_alerts,
)
from sitepulse.cli import main
from sitepulse.incidents import Incident
from sitepulse.models import CheckResult, Target
from sitepulse.store import History, load_email_recipients, save_email_recipients


USER = "ops@example.com"


class FakeSMTP:
    """Stand-in for smtplib.SMTP / SMTP_SSL: records sent messages."""

    instances = []

    def __init__(self, host, port, timeout=None, fail=False):
        self.host = host
        self.port = port
        self.fail = fail
        self.starttls_called = False
        self.login_args = None
        self.sent = []  # EmailMessage objects
        self.quitted = False
        FakeSMTP.instances.append(self)

    # context manager is NOT used by EmailSender; plain calls are
    def starttls(self):
        self.starttls_called = True

    def login(self, user, password):
        self.login_args = (user, password)

    def send_message(self, msg):
        if self.fail:
            raise smtplib.SMTPException("relay refused")
        self.sent.append(msg)

    def quit(self):
        self.quitted = True


@pytest.fixture(autouse=True)
def fresh_fake(monkeypatch):
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
    return {
        "SITEPULSE_SMTP_HOST": "smtp.example.com",
        "SITEPULSE_SMTP_FROM": "sitepulse@example.com",
    }


def _config(**over):
    cfg = SmtpConfig(host="smtp.example.com", from_addr="sitepulse@example.com")
    for k, v in over.items():
        setattr(cfg, k, v)
    return cfg


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


# -- env config --------------------------------------------------------

def test_smtp_config_from_env_defaults(smtp_env):
    cfg = smtp_config_from_env()
    assert cfg.host == "smtp.example.com"
    assert cfg.port == 587
    assert cfg.from_addr == "sitepulse@example.com"
    assert cfg.use_tls and not cfg.use_ssl
    assert cfg.user == ""


def test_smtp_config_ssl_switches_port(monkeypatch):
    monkeypatch.setenv("SITEPULSE_SMTP_HOST", "smtp.example.com")
    monkeypatch.setenv("SITEPULSE_SMTP_FROM", "sitepulse@example.com")
    monkeypatch.setenv("SITEPULSE_SMTP_SSL", "1")
    cfg = smtp_config_from_env()
    assert cfg.use_ssl and cfg.port == 465 and not cfg.use_tls


def test_smtp_config_explicit_port_wins(monkeypatch):
    monkeypatch.setenv("SITEPULSE_SMTP_HOST", "smtp.example.com")
    monkeypatch.setenv("SITEPULSE_SMTP_FROM", "sitepulse@example.com")
    monkeypatch.setenv("SITEPULSE_SMTP_SSL", "1")
    monkeypatch.setenv("SITEPULSE_SMTP_PORT", "2525")
    assert smtp_config_from_env().port == 2525


def test_smtp_config_bad_port(monkeypatch):
    monkeypatch.setenv("SITEPULSE_SMTP_HOST", "smtp.example.com")
    monkeypatch.setenv("SITEPULSE_SMTP_FROM", "sitepulse@example.com")
    monkeypatch.setenv("SITEPULSE_SMTP_PORT", "nope")
    with pytest.raises(SmtpConfigError) as ei:
        smtp_config_from_env()
    assert "SITEPULSE_SMTP_PORT" in str(ei.value)


def test_missing_env_config_names_the_vars(monkeypatch):
    monkeypatch.delenv("SITEPULSE_SMTP_HOST", raising=False)
    monkeypatch.delenv("SITEPULSE_SMTP_FROM", raising=False)
    with pytest.raises(SmtpConfigError) as ei:
        smtp_config_from_env()
    msg = str(ei.value)
    assert "SITEPULSE_SMTP_HOST" in msg
    assert "SITEPULSE_SMTP_FROM" in msg


# -- message rendering -------------------------------------------------

def test_email_subject_and_body():
    p = build_payload("incident.started", _target(), _incident())
    assert email_subject(p) == "[sitepulse] incident.started: web"
    body = email_body(p)
    assert "event:  incident.started" in body
    assert "target: web" in body
    assert "url:    http://x/" in body
    assert "failures: 2" in body
    assert "error:  HTTP 500" in body
    assert "started: 1970-01-01T00:01:40" in body

    p2 = build_payload("incident.resolved", _target(), _incident(), ended=160.0)
    body2 = email_body(p2)
    assert "ended:   1970-01-01T00:02:40" in body2
    assert "duration: 60.0s" in body2


# -- sender ------------------------------------------------------------

def test_send_one_email_with_right_subject_and_recipient():
    sender = EmailSender([USER], _config())
    p = build_payload("incident.started", _target(), _incident())
    failures = sender.broadcast(p)
    assert failures == []
    assert len(FakeSMTP.instances) == 1
    inst = FakeSMTP.instances[0]
    assert inst.host == "smtp.example.com" and inst.port == 587
    assert inst.starttls_called and inst.login_args is None
    assert inst.quitted
    assert len(inst.sent) == 1
    msg = inst.sent[0]
    assert msg["Subject"] == "[sitepulse] incident.started: web"
    assert msg["To"] == USER
    assert msg["From"] == "sitepulse@example.com"
    assert "incident.started" in msg.get_content()


def test_login_when_credentials_set():
    sender = EmailSender([USER], _config(user="bot", password="s3cret"))
    sender.broadcast(build_test_payload(USER))
    assert FakeSMTP.instances[0].login_args == ("bot", "s3cret")


def test_broadcast_collects_failures_without_raising(monkeypatch):
    monkeypatch.setattr(smtplib, "SMTP",
                        lambda *a, **k: FakeSMTP(*a, fail=True, **k))
    sender = EmailSender([USER, "two@example.com"], _config())
    failures = sender.broadcast(build_test_payload(USER))
    assert len(failures) == 2
    assert all("refused" in err for _, err in failures)


def test_no_recipients_sends_nothing():
    sender = EmailSender([], _config())
    assert sender.broadcast(build_test_payload(USER)) == []
    assert FakeSMTP.instances == []


# -- transition detection ----------------------------------------------

def _process(history, state, targets=None, recipients=(USER,)):
    email = EmailSender(list(recipients), _config()) if recipients else None
    targets = targets if targets is not None else [_target()]
    return process_alerts(targets, history, state, WebhookSender([]), email)


def test_started_then_resolved_sends_two_emails(tmp_path):
    path = str(tmp_path / "alerts.json")
    h, state = History(), AlertState(path)

    h.record(_res("web", True, 10.0))
    h.record(_res("web", False, 100.0, "HTTP 500"))
    assert _process(h, state) == (1, 0, [])
    assert sum(len(i.sent) for i in FakeSMTP.instances) == 1
    subjects = [m["Subject"] for i in FakeSMTP.instances for m in i.sent]
    assert subjects == ["[sitepulse] incident.started: web"]

    # more failures → no duplicate
    h.record(_res("web", False, 160.0, "HTTP 500"))
    assert _process(h, state) == (0, 0, [])
    assert sum(len(i.sent) for i in FakeSMTP.instances) == 1

    # recovery → one resolved email
    h.record(_res("web", True, 200.0))
    assert _process(h, state) == (0, 1, [])
    subjects = [m["Subject"] for i in FakeSMTP.instances for m in i.sent]
    assert subjects[-1] == "[sitepulse] incident.resolved: web"
    assert len(subjects) == 2

    # settled → quiet even across a state reload
    state.save()
    state2 = AlertState(path)
    state2.load()
    assert _process(h, state2) == (0, 0, [])
    assert sum(len(i.sent) for i in FakeSMTP.instances) == 2


def test_email_and_webhook_together():
    h, state = History(), AlertState()
    h.record(_res("web", False, 100.0))
    email = EmailSender([USER], _config())

    # keep webhook behavior intact: a minimal subclass recording posts
    class StubHook(WebhookSender):
        def __init__(self):
            super().__init__(["https://hooks.example.com/x"])
            self.posts = []
        def send(self, url, payload):
            self.posts.append(payload)

    sender = StubHook()
    started, resolved, failures = process_alerts([_target()], h, state,
                                                 sender, email)
    assert (started, resolved, failures) == (1, 0, [])
    assert len(sender.posts) == 1
    assert sum(len(i.sent) for i in FakeSMTP.instances) == 1


def test_email_failure_is_collected_and_retried(monkeypatch):
    h, state = History(), AlertState()
    h.record(_res("web", False, 100.0))

    monkeypatch.setattr(smtplib, "SMTP",
                        lambda *a, **k: FakeSMTP(*a, fail=True, **k))
    email = EmailSender([USER], _config())
    started, resolved, failures = process_alerts([_target()], h, state,
                                                 WebhookSender([]), email)
    assert (started, resolved) == (0, 0)
    assert len(failures) == 1
    assert failures[0][0] == USER
    assert state.open_for("web") is None  # not marked → retried later

    # next run delivers and marks
    monkeypatch.setattr(smtplib, "SMTP", FakeSMTP)
    started, resolved, failures = process_alerts([_target()], h, state,
                                                 WebhookSender([]),
                                                 EmailSender([USER], _config()))
    assert (started, resolved, failures) == (1, 0, [])
    assert sum(len(i.sent) for i in FakeSMTP.instances) == 1


def test_no_recipients_no_webhooks_is_quiet():
    h, state = History(), AlertState()
    h.record(_res("web", False, 100.0))
    assert process_alerts([_target()], h, state, WebhookSender([]), None) == (0, 0, [])


# -- store ----------------------------------------------------------------

def test_email_recipient_config_roundtrip(tmp_path):
    cfg = str(tmp_path / "sitepulse.json")
    assert load_email_recipients(cfg) == []
    save_email_recipients(cfg, [USER])
    assert load_email_recipients(cfg) == [USER]
    # must not clobber targets or webhooks
    from sitepulse.store import load_webhooks, save_webhooks
    save_webhooks(cfg, ["https://hooks.example.com/x"])
    assert load_email_recipients(cfg) == [USER]
    assert load_webhooks(cfg) == ["https://hooks.example.com/x"]


# -- CLI ------------------------------------------------------------------

def test_alert_email_add_list_remove(workdir, capsys):
    assert main(["alert", "email", "add", USER]) == 0
    assert main(["alert", "email", "add", USER]) == 1  # duplicate
    assert main(["alert", "email", "add", "not-an-email"]) == 1

    assert main(["alert", "email", "list"]) == 0
    assert USER in capsys.readouterr().out

    assert main(["alert", "email", "remove", USER]) == 0
    assert main(["alert", "email", "remove", USER]) == 1  # already gone
    assert main(["alert", "email", "list"]) == 0
    assert "no email recipients" in capsys.readouterr().out


def test_alert_email_test_sends_test_email(workdir, smtp_env, capsys):
    assert main(["alert", "email", "test", USER]) == 0
    inst = FakeSMTP.instances[0]
    assert len(inst.sent) == 1
    msg = inst.sent[0]
    assert msg["Subject"] == f"[sitepulse] test: {USER}"
    assert msg["To"] == USER
    assert "test email sent" in capsys.readouterr().out


def test_alert_email_test_fails_cleanly_without_env(workdir, capsys, monkeypatch):
    monkeypatch.delenv("SITEPULSE_SMTP_HOST", raising=False)
    assert main(["alert", "email", "test", USER]) == 1
    err = capsys.readouterr().err
    assert "SITEPULSE_SMTP_HOST" in err
    assert FakeSMTP.instances == []


def test_alert_email_test_reports_delivery_failure(workdir, smtp_env, monkeypatch):
    monkeypatch.setattr(smtplib, "SMTP",
                        lambda *a, **k: FakeSMTP(*a, fail=True, **k))
    assert main(["alert", "email", "test", USER]) == 1


def test_check_sends_email_on_incident(workdir, server, smtp_env):
    assert main(["add", server + "/boom", "--name", "bad"]) == 0
    assert main(["alert", "email", "add", USER]) == 0

    assert main(["check"]) == 1  # target down
    msgs = [m for i in FakeSMTP.instances for m in i.sent]
    assert len(msgs) == 1
    assert msgs[0]["Subject"] == "[sitepulse] incident.started: bad"
    assert msgs[0]["To"] == USER

    # second check: incident still open → no duplicate
    assert main(["check"]) == 1
    assert len([m for i in FakeSMTP.instances for m in i.sent]) == 1

    # state persisted next to the config
    state_path = os.path.join(workdir, "sitepulse.alerts.json")
    assert "bad" in json.loads(open(state_path).read())["open"]


def test_check_without_smtp_env_warns_but_still_checks(workdir, server, capsys):
    assert main(["add", server + "/boom", "--name", "bad"]) == 0
    assert main(["alert", "email", "add", USER]) == 0
    # no SITEPULSE_SMTP_* set → email skipped with a warning, check proceeds
    assert main(["check"]) == 1
    assert "SITEPULSE_SMTP_HOST" in capsys.readouterr().err
    assert FakeSMTP.instances == []
