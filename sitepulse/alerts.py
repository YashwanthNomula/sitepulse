"""Incident alerts: webhook + email notifiers, POST JSON / send plain-text email.

Webhooks are registered with ``sitepulse alert add <url>`` and email
recipients with ``sitepulse alert email add <address>``; both are stored in
the config file. After every batch of checks, :func:`process_alerts`
compares the currently-open incidents against a small persisted state file
(``<config>.alerts.json``) and notifies once per transition:

- ``incident.started``  — first failed check of a new incident
- ``incident.resolved``  — first successful check after an incident

SMTP configuration comes from ``SITEPULSE_SMTP_*`` environment variables
(never the config file — passwords must never be written to disk by this
tool). A failed delivery never breaks a check run: errors are collected
and reported as warnings, and delivery is retried on the next run only if
the transition is still unacknowledged (the state file is updated only for
channels that delivered — partial failures are retried next time).

Pure stdlib: urllib for the POST, smtplib/email for mail, no new deps.

Channel routing: :class:`MultiChannelSender` wraps the webhook and email
senders and decides, per alert event, which channels to try:

- ``failover`` — try the primary channel first (webhook by default); only
  if it fails, fall back to the secondary channel. A channel that was
  skipped because a higher-priority channel already delivered is not
  retried later.
- ``fanout`` (default) — send via every configured channel in parallel
  (threads); one channel failing never blocks the other.

Delivery is tracked per channel in the state file, so a retry only
re-sends through channels that have not delivered yet — an incident event
is never double-sent through the same channel.
"""
from __future__ import annotations

import json
import os
import smtplib
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from email.message import EmailMessage
from typing import Callable, Dict, List, NamedTuple, Optional, Tuple
from urllib import request as urllib_request
from urllib.error import URLError

from . import __version__
from .incidents import Incident, find_incidents
from .models import Target
from .store import (
    ALERT_CHANNELS,
    ALERT_MODES,
    History,
)

SOURCE = "sitepulse"

# Environment variables for SMTP configuration. Passwords are never stored in
# the config file — they come from the environment only.
SMTP_ENV_VARS = (
    "SITEPULSE_SMTP_HOST",
    "SITEPULSE_SMTP_PORT",
    "SITEPULSE_SMTP_USER",
    "SITEPULSE_SMTP_PASS",
    "SITEPULSE_SMTP_FROM",
    "SITEPULSE_SMTP_TLS",
    "SITEPULSE_SMTP_SSL",
)


def _iso(epoch: float) -> str:
    return datetime.fromtimestamp(epoch, tz=timezone.utc).isoformat()


def build_payload(event: str, target: Target, incident: Incident,
                 ended: Optional[float] = None) -> Dict[str, object]:
    """JSON payload for an incident alert.

    ``event`` is ``incident.started``, ``incident.resolved`` or ``test``.
    """
    payload: Dict[str, object] = {
        "event": event,
        "source": SOURCE,
        "version": __version__,
        "target": target.name,
        "url": target.url,
        "started_at": _iso(incident.started),
        "failures": incident.failures,
        "error": incident.error,
    }
    if ended is not None:
        payload["ended_at"] = _iso(ended)
        payload["duration_s"] = round(max(0.0, ended - incident.started), 1)
    return payload


def build_test_payload(url: str) -> Dict[str, object]:
    return {
        "event": "test",
        "source": SOURCE,
        "version": __version__,
        "url": url,
        "sent_at": _iso(time.time()),
        "message": "sitepulse alert webhook test — your endpoint is reachable",
    }


class WebhookSender:
    """POSTs alert payloads to registered webhook URLs."""

    def __init__(self, webhooks: List[str], timeout: float = 10.0,
                 opener: Optional[Callable] = None):
        self.webhooks = list(webhooks)
        self.timeout = timeout
        self._opener = opener or urllib_request.urlopen

    def send(self, url: str, payload: Dict[str, object]) -> None:
        """POST one payload; raises on network/HTTP failure."""
        body = json.dumps(payload).encode("utf-8")
        req = urllib_request.Request(
            url, data=body, method="POST",
            headers={"Content-Type": "application/json",
                     "User-Agent": f"sitepulse/{__version__}"},
        )
        with self._opener(req, timeout=self.timeout):
            pass

    def deliver(self, payload: Dict[str, object]) -> List[Tuple[str, Optional[str]]]:
        """POST to every webhook.

        Returns ``[(url, error)]`` with ``error`` None for each URL that
        accepted the payload — a failure at one URL never blocks the rest.
        """
        results: List[Tuple[str, Optional[str]]] = []
        for url in self.webhooks:
            try:
                self.send(url, payload)
            except (URLError, OSError, ValueError) as exc:
                results.append((url, str(exc) or type(exc).__name__))
            else:
                results.append((url, None))
        return results

    def broadcast(self, payload: Dict[str, object]) -> List[Tuple[str, str]]:
        """POST to every webhook. Returns [(url, error)] for failures."""
        return [(url, err) for url, err in self.deliver(payload) if err]


class SmtpConfigError(Exception):
    """Raised when the SMTP environment configuration is missing or invalid."""


class SmtpConfig:
    """SMTP connection settings.

    Built only from environment variables — passwords are never written to
    the config file by this tool.
    """

    def __init__(self, host: str, port: int = 587, user: str = "",
                 password: str = "", from_addr: str = "",
                 use_tls: bool = True, use_ssl: bool = False,
                 timeout: float = 10.0):
        self.host = host
        self.port = port
        self.user = user
        self.password = password
        self.from_addr = from_addr
        self.use_tls = use_tls
        self.use_ssl = use_ssl
        self.timeout = timeout

    def __repr__(self) -> str:  # pragma: no cover - never log the password
        return (f"SmtpConfig(host={self.host!r}, port={self.port!r}, "
                f"user={self.user!r}, from_addr={self.from_addr!r}, "
                f"use_tls={self.use_tls!r}, use_ssl={self.use_ssl!r})")


def smtp_config_from_env(env: Optional[Dict[str, str]] = None) -> SmtpConfig:
    """Build an :class:`SmtpConfig` from ``SITEPULSE_SMTP_*`` env vars.

    Requires ``SITEPULSE_SMTP_HOST`` and ``SITEPULSE_SMTP_FROM``; port
    defaults to 587 (or 465 when ``SITEPULSE_SMTP_SSL=1`` for implicit TLS).
    Raises :class:`SmtpConfigError` naming the missing variables.
    """
    e = env if env is not None else os.environ
    host = (e.get("SITEPULSE_SMTP_HOST") or "").strip()
    from_addr = (e.get("SITEPULSE_SMTP_FROM") or "").strip()
    missing = [name for name, val in (("SITEPULSE_SMTP_HOST", host),
                                      ("SITEPULSE_SMTP_FROM", from_addr))
               if not val]
    if missing:
        raise SmtpConfigError(
            "email alerts need SMTP configuration — missing "
            + ", ".join(missing)
            + " (set SITEPULSE_SMTP_HOST and SITEPULSE_SMTP_FROM; "
            "add SITEPULSE_SMTP_USER / SITEPULSE_SMTP_PASS if your server "
            "needs auth)")
    use_ssl = (e.get("SITEPULSE_SMTP_SSL") or "").strip() == "1"
    port_raw = (e.get("SITEPULSE_SMTP_PORT") or "").strip()
    if port_raw:
        try:
            port = int(port_raw)
        except ValueError:
            raise SmtpConfigError(
                f"invalid SITEPULSE_SMTP_PORT: {port_raw!r} (must be a number)")
    else:
        port = 465 if use_ssl else 587
    use_tls = (e.get("SITEPULSE_SMTP_TLS") or "").strip().lower() \
        not in ("0", "false", "no")
    return SmtpConfig(
        host=host,
        port=port,
        user=(e.get("SITEPULSE_SMTP_USER") or "").strip(),
        password=e.get("SITEPULSE_SMTP_PASS") or "",
        from_addr=from_addr,
        use_tls=use_tls and not use_ssl,
        use_ssl=use_ssl,
    )


def email_subject(payload: Dict[str, object]) -> str:
    """Subject line for an incident alert email."""
    event = str(payload.get("event", "alert"))
    target = str(payload.get("target") or payload.get("url") or "")
    return f"[sitepulse] {event}: {target}"


def email_body(payload: Dict[str, object]) -> str:
    """Plain-text summary of an alert payload."""
    lines = [
        f"event:  {payload.get('event', 'alert')}",
        f"source: {payload.get('source', SOURCE)}",
        f"target: {payload.get('target', '')}",
        f"url:    {payload.get('url', '')}",
    ]
    if payload.get("sent_at") is not None:
        lines.append(f"sent:    {payload['sent_at']}")
    if payload.get("started_at") is not None:
        lines.append(f"started: {payload['started_at']}")
    if payload.get("ended_at") is not None:
        lines.append(f"ended:   {payload['ended_at']}")
    if payload.get("duration_s") is not None:
        lines.append(f"duration: {payload['duration_s']}s")
    lines.append(f"failures: {payload.get('failures', '')}")
    if payload.get("error") is not None:
        lines.append(f"error:  {payload['error']}")
    if payload.get("message"):
        lines.append(f"message: {payload['message']}")
    lines.append("")
    lines.append(f"— sent by sitepulse {payload.get('version', '')}")
    return "\n".join(lines)


def build_email_message(payload: Dict[str, object], recipient: str,
                        from_addr: str) -> EmailMessage:
    """Assemble a ready-to-send plain-text alert email."""
    msg = EmailMessage()
    msg["Subject"] = email_subject(payload)
    msg["From"] = from_addr
    msg["To"] = recipient
    msg.set_content(email_body(payload))
    return msg


class EmailSender:
    """Sends alert payloads as plain-text email via SMTP."""

    def __init__(self, recipients: List[str], config: SmtpConfig,
                 timeout: float = 10.0):
        self.recipients = list(recipients)
        self.config = config
        self.timeout = timeout

    def _connect(self) -> smtplib.SMTP:
        cfg = self.config
        if cfg.use_ssl:
            smtp = smtplib.SMTP_SSL(cfg.host, cfg.port, timeout=self.timeout)
        else:
            smtp = smtplib.SMTP(cfg.host, cfg.port, timeout=self.timeout)
            if cfg.use_tls:
                smtp.starttls()
        if cfg.user:
            smtp.login(cfg.user, cfg.password)
        return smtp

    def send(self, recipient: str, subject: str, body: str) -> None:
        """Send one email; raises on SMTP/network failure."""
        msg = EmailMessage()
        msg["Subject"] = subject
        msg["From"] = self.config.from_addr
        msg["To"] = recipient
        msg.set_content(body)
        smtp = self._connect()
        try:
            smtp.send_message(msg)
        finally:
            try:
                smtp.quit()
            except (smtplib.SMTPException, OSError):
                pass

    def broadcast(self, payload: Dict[str, object]) -> List[Tuple[str, str]]:
        """Email every recipient. Returns [(recipient, error)] for failures."""
        return [(dest, err) for dest, err in self.deliver(payload) if err]

    def deliver(self, payload: Dict[str, object]) -> List[Tuple[str, Optional[str]]]:
        """Email every recipient.

        Returns ``[(recipient, error)]`` with ``error`` None for each
        recipient that accepted the message — a failure at one address
        never blocks the rest.
        """
        subject = email_subject(payload)
        body = email_body(payload)
        results: List[Tuple[str, Optional[str]]] = []
        for recipient in self.recipients:
            try:
                self.send(recipient, subject, body)
            except (smtplib.SMTPException, OSError, ValueError) as exc:
                results.append((recipient, str(exc) or type(exc).__name__))
            else:
                results.append((recipient, None))
        return results


class ChannelResult(NamedTuple):
    """Outcome of one alert delivery attempt.

    ``channel`` is ``"webhook"`` or ``"email"``, ``destination`` the URL or
    address tried, ``error`` None on success or the failure message.
    """
    channel: str
    destination: str
    error: Optional[str]

    @property
    def ok(self) -> bool:
        return self.error is None


class DeliveryReport(NamedTuple):
    """What :meth:`MultiChannelSender.broadcast` did this call.

    ``results`` holds one :class:`ChannelResult` per destination attempted;
    ``attempted`` lists the channels that were tried (in failover mode a
    lower-priority channel is absent when a higher-priority one delivered).
    """
    results: List[ChannelResult]
    attempted: List[str]


class MultiChannelSender:
    """Routes one alert payload across the webhook and email channels.

    ``mode`` is ``"failover"`` (try the primary channel, fall back to the
    other only on failure) or ``"fanout"`` (send via every configured
    channel in parallel). ``primary`` picks the first channel tried in
    failover mode. Channels with no destinations configured are skipped
    silently. Raises :class:`ValueError` on an unknown mode or primary.
    """

    def __init__(self, webhook_sender: Optional[WebhookSender] = None,
                 email_sender: Optional[EmailSender] = None,
                 mode: str = "fanout", primary: str = "webhook"):
        if mode not in ALERT_MODES:
            raise ValueError(f"unknown alert mode: {mode!r} "
                             f"(expected one of {ALERT_MODES})")
        if primary not in ALERT_CHANNELS:
            raise ValueError(f"unknown primary channel: {primary!r} "
                             f"(expected one of {ALERT_CHANNELS})")
        self.webhook = webhook_sender
        self.email = email_sender
        self.mode = mode
        self.primary = primary

    def available_channels(self) -> List[str]:
        """Configured channels, primary first."""
        ordered = [self.primary] + [c for c in ALERT_CHANNELS
                                    if c != self.primary]
        return [c for c in ordered if self._destinations(c)]

    def _destinations(self, channel: str) -> List[str]:
        if channel == "webhook":
            return list(self.webhook.webhooks) if self.webhook else []
        return list(self.email.recipients) if self.email else []

    def _send_channel(self, channel: str,
                      payload: Dict[str, object]) -> List[ChannelResult]:
        sender = self.webhook if channel == "webhook" else self.email
        if sender is None:
            return []
        return [ChannelResult(channel, dest, err)
                for dest, err in sender.deliver(payload)]

    def broadcast(self, payload: Dict[str, object],
                  only_channels: Optional[List[str]] = None
                  ) -> DeliveryReport:
        """Deliver ``payload`` per the routing mode.

        ``only_channels`` restricts the attempt to a subset (used when
        retrying channels that have not delivered yet). Returns a
        :class:`DeliveryReport` with the per-destination outcomes; a
        failure in one channel never prevents trying the other.
        """
        targets = [c for c in self.available_channels()
                   if only_channels is None or c in only_channels]
        results: List[ChannelResult] = []
        attempted: List[str] = []
        if self.mode == "fanout":
            if targets:
                with ThreadPoolExecutor(max_workers=len(targets)) as pool:
                    # map() preserves channel order → deterministic results
                    for channel, channel_results in zip(
                            targets, pool.map(
                                lambda c: self._send_channel(c, payload),
                                targets)):
                        results.extend(channel_results)
                        attempted.append(channel)
        else:  # failover: stop at the first channel that fully delivers
            for channel in targets:
                channel_results = self._send_channel(channel, payload)
                results.extend(channel_results)
                attempted.append(channel)
                if channel_results and all(r.ok for r in channel_results):
                    break
        return DeliveryReport(results=results, attempted=attempted)


class AlertState:
    """Persisted record of incidents we've already alerted on.

    ``{"open": {target_name: entry}}`` — an entry exists while the incident
    is open and unacknowledged-as-resolved. Each entry tracks per-channel
    delivery so retries never double-send through a channel that already
    delivered:

    ``{"started": epoch, "failures": int, "error": str|None, "url": str,
    "started_delivered": {"webhook": bool, "email": bool},
    "ended": epoch,                       # only while a resolved alert
    "resolved_delivered": {...}}           # is still pending
    ``

    Entries written by older versions lack the ``*_delivered`` keys; they
    are treated as fully delivered (the old code only recorded an entry
    after every channel succeeded).
    """

    def __init__(self, path: Optional[str] = None):
        self.path = path
        self._open: Dict[str, Dict[str, object]] = {}

    def load(self) -> None:
        if not self.path or not os.path.exists(self.path):
            return
        try:
            with open(self.path, encoding="utf-8") as fh:
                data = json.load(fh)
        except (json.JSONDecodeError, OSError):
            return
        raw = data.get("open")
        if isinstance(raw, dict):
            self._open = {k: v for k, v in raw.items() if isinstance(v, dict)}

    def save(self) -> None:
        if not self.path:
            return
        tmp = self.path + ".tmp"
        os.makedirs(os.path.dirname(os.path.abspath(self.path)), exist_ok=True)
        with open(tmp, "w", encoding="utf-8") as fh:
            json.dump({"open": self._open}, fh, indent=1)
        os.replace(tmp, self.path)

    def open_for(self, target_name: str) -> Optional[Dict[str, object]]:
        return self._open.get(target_name)

    def mark_open(self, target_name: str, target: Target,
                  incident: Incident,
                  channels: Optional[List[str]] = None) -> None:
        self._open[target_name] = {
            "started": incident.started,
            "failures": incident.failures,
            "error": incident.error,
            "url": target.url,
            "started_delivered": {c: False for c in
                                  (channels if channels is not None
                                   else ALERT_CHANNELS)},
        }

    def refresh(self, target_name: str, incident: Incident) -> None:
        """Update the failure count of an already-notified open incident."""
        entry = self._open.get(target_name)
        if entry is not None:
            entry["failures"] = incident.failures
            entry["error"] = incident.error

    def clear(self, target_name: str) -> None:
        self._open.pop(target_name, None)


def _ended_incident(history: History, target_name: str,
                    started: float) -> Optional[Incident]:
    """Find the now-resolved incident that began at ``started``."""
    for inc in find_incidents(target_name, history.results_for(target_name)):
        if not inc.ongoing and inc.started == started:
            return inc
    return None


def _channel_ok(report: DeliveryReport, channel: str) -> bool:
    """True when every attempted destination of ``channel`` delivered."""
    results = [r for r in report.results if r.channel == channel]
    return bool(results) and all(r.ok for r in results)


def _pending(entry: Dict[str, object], key: str,
             channels: List[str], legacy_delivered: bool = True) -> List[str]:
    """Channels that have not delivered ``key`` yet.

    Entries written by older versions have no delivery keys: for the
    ``started`` event that means fully delivered (the old code only
    recorded an entry after every channel succeeded), so
    ``legacy_delivered`` defaults True there; a missing ``resolved`` key
    means the resolved alert was never attempted.
    """
    delivered = entry.get(key)
    if not isinstance(delivered, dict):
        return [] if legacy_delivered else list(channels)
    return [c for c in channels if not delivered.get(c, False)]


def _mark_outcomes(entry: Dict[str, object], key: str,
                   channels: List[str], report: DeliveryReport) -> None:
    """Record a broadcast's per-channel outcomes on a state entry.

    Channels attempted this call are marked by their result; channels that
    were pending but skipped (failover stopped at a higher-priority
    success) count as delivered — the event already went out.
    """
    delivered = entry.setdefault(key, {c: False for c in channels})
    assert isinstance(delivered, dict)
    by_channel: Dict[str, List[ChannelResult]] = {}
    for result in report.results:
        by_channel.setdefault(result.channel, []).append(result)
    for channel in report.attempted:
        results = by_channel.get(channel, [])
        delivered[channel] = bool(results) and all(r.ok for r in results)
    pending = _pending(entry, key, channels)
    for channel in pending:
        if channel not in report.attempted:
            delivered[channel] = True  # superseded by a failover success


def _deliver_started(target: Target, incident: Incident,
                     entry: Optional[Dict[str, object]], state: AlertState,
                     mc: MultiChannelSender, channels: List[str]
                     ) -> Tuple[int, List[Tuple[str, str]]]:
    """Send ``incident.started`` through channels that haven't delivered it.

    Returns ``(started_count, failures)``; the entry is created only when
    at least one channel delivers, so a total failure leaves nothing
    behind and the transition is retried on the next run.
    """
    pending = _pending(entry, "started_delivered", channels) if entry else list(channels)
    report = mc.broadcast(build_payload("incident.started", target, incident),
                          only_channels=pending)
    failures = [(r.destination, r.error) for r in report.results if r.error]
    created = entry is None
    previously = (not created) and any(
        (entry.get("started_delivered") or {}).get(c, False)
        for c in channels)
    if created:
        state.mark_open(target.name, target, incident, channels)
        entry = state.open_for(target.name)
        assert entry is not None
    _mark_outcomes(entry, "started_delivered", channels, report)
    sd = entry.get("started_delivered")
    delivered_any = isinstance(sd, dict) and any(
        sd.get(c, False) for c in channels)
    if created and not delivered_any:
        # Total failure: leave no entry behind so the transition is
        # retried cleanly on the next run.
        state.clear(target.name)
        return 0, failures
    # Count the transition once — when it first goes out on any channel.
    return (1 if delivered_any and not previously else 0), failures


def _deliver_resolved(target: Target, history: History,
                      entry: Dict[str, object], state: AlertState,
                      mc: MultiChannelSender, channels: List[str]
                      ) -> Tuple[int, List[Tuple[str, str]], bool]:
    """Send ``incident.resolved`` through channels that haven't delivered it.

    A resolved alert supersedes any still-pending started delivery for the
    same incident. Returns ``(resolved_count, failures, fully_delivered)``;
    the state entry is cleared only once every channel has the resolved
    alert.
    """
    old = _ended_incident(history, target.name, float(entry["started"]))
    if old is not None and old.ended is not None:
        ended_inc = Incident(target=target.name, started=old.started,
                             ended=old.ended, failures=old.failures,
                             error=old.error)
    else:
        # Incident aged out of the history window; report what we know.
        ended_inc = Incident(
            target=target.name, started=float(entry["started"]),
            ended=time.time(), failures=int(entry.get("failures", 0)),
            error=entry.get("error"),  # type: ignore[arg-type]
        )
    # A resolved alert supersedes a started alert that never went out.
    sd = entry.get("started_delivered")
    if isinstance(sd, dict):
        for c in channels:
            sd[c] = True
    entry["ended"] = ended_inc.ended
    rd_before = entry.get("resolved_delivered")
    previously = isinstance(rd_before, dict) and any(
        rd_before.get(c, False) for c in channels)
    pending = _pending(entry, "resolved_delivered", channels,
                       legacy_delivered=False)
    report = mc.broadcast(
        build_payload("incident.resolved", target, ended_inc,
                      ended=ended_inc.ended),
        only_channels=pending)
    failures = [(r.destination, r.error) for r in report.results if r.error]
    _mark_outcomes(entry, "resolved_delivered", channels, report)
    rd = entry.get("resolved_delivered")
    delivered_any = isinstance(rd, dict) and any(
        rd.get(c, False) for c in channels)
    done = not _pending(entry, "resolved_delivered", channels,
                        legacy_delivered=False)
    if done:
        state.clear(target.name)
    # Count the transition once — when it first goes out on any channel.
    return (1 if delivered_any and not previously else 0), failures, done


def process_alerts(targets: List[Target], history: History,
                   state: AlertState, sender: WebhookSender,
                   email_sender: Optional[EmailSender] = None,
                   mode: str = "fanout", primary: str = "webhook"
                   ) -> Tuple[int, int, List[Tuple[str, str]]]:
    """Send started/resolved alerts for incident transitions.

    Delivery is routed through a :class:`MultiChannelSender`: ``mode`` is
    ``"fanout"`` (every configured channel, in parallel) or ``"failover"``
    (primary channel first, the other only on failure); ``primary`` picks
    the failover order. Delivery is tracked per channel in ``state``, so a
    retry only re-sends through channels that have not delivered yet and an
    incident event is never double-sent through the same channel.

    Returns ``(started, resolved, failures)`` where failures is the list of
    ``(destination, error)`` delivery errors across all channels. Never
    raises: a failing channel only lands in ``failures`` (and the
    transition is retried on the next call).
    """
    mc = MultiChannelSender(sender, email_sender, mode=mode, primary=primary)
    channels = mc.available_channels()
    if not channels:
        return 0, 0, []

    started = resolved = 0
    delivery_failures: List[Tuple[str, str]] = []

    for target in targets:
        ongoing: Optional[Incident] = None
        for inc in find_incidents(target.name, history.results_for(target.name)):
            if inc.ongoing:
                ongoing = inc
        prev = state.open_for(target.name)

        if ongoing is not None:
            if prev is not None and prev.get("started") != ongoing.started:
                # Previous incident resolved between runs: alert on it first.
                count, errs, done = _deliver_resolved(target, history, prev,
                                                      state, mc, channels)
                resolved += count
                delivery_failures.extend(errs)
                if not done:
                    continue  # retry both transitions next run
                prev = None
            if prev is not None:
                state.refresh(target.name, ongoing)
                if not _pending(prev, "started_delivered", channels):
                    continue  # already fully notified → stay quiet
            count, errs = _deliver_started(target, ongoing, prev, state,
                                           mc, channels)
            started += count
            delivery_failures.extend(errs)
        elif prev is not None:
            count, errs, _done = _deliver_resolved(target, history, prev,
                                                  state, mc, channels)
            resolved += count
            delivery_failures.extend(errs)
    return started, resolved, delivery_failures
