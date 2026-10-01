"""HTTP(S) checking engine. Stdlib only, concurrent via a thread pool."""
from __future__ import annotations

import socket
import time
import urllib.error
import urllib.request
from concurrent.futures import ThreadPoolExecutor, as_completed
from typing import Iterable, List

from .models import CheckResult, Target

USER_AGENT = "sitepulse/1.0 (+https://github.com/YashwanthNomula/sitepulse)"
MAX_BODY_READ = 256 * 1024  # enough for keyword matching, cheap on memory


def check_one(target: Target) -> CheckResult:
    """Run a single check against a target and return the result."""
    headers = {"User-Agent": USER_AGENT}
    headers.update(target.headers)
    req = urllib.request.Request(target.url, method=target.method, headers=headers)
    started = time.perf_counter()
    now = time.time()

    def elapsed_ms() -> float:
        return (time.perf_counter() - started) * 1000.0

    try:
        with urllib.request.urlopen(req, timeout=target.timeout_s) as resp:
            body = resp.read(MAX_BODY_READ)
            latency = elapsed_ms()
            status = resp.status
    except urllib.error.HTTPError as exc:  # non-2xx/3xx responses land here
        latency = elapsed_ms()
        try:
            body = exc.read(MAX_BODY_READ)
        except Exception:
            body = b""
        status = exc.code
        keyword_ok = _keyword_ok(target, body)
        ok = (not target.expect or status in target.expect) and keyword_ok
        return CheckResult(
            name=target.name,
            url=target.url,
            timestamp=now,
            ok=ok,
            status=status,
            latency_ms=latency,
            error=None if ok else f"HTTP {status}",
            keyword_ok=keyword_ok,
        )
    except (urllib.error.URLError, socket.timeout, TimeoutError, OSError) as exc:
        latency = elapsed_ms()
        reason = _short_reason(exc)
        return CheckResult(
            name=target.name,
            url=target.url,
            timestamp=now,
            ok=False,
            status=None,
            latency_ms=latency,
            error=reason,
            keyword_ok=None,
        )
    except Exception as exc:  # pragma: no cover - defensive
        return CheckResult(
            name=target.name,
            url=target.url,
            timestamp=now,
            ok=False,
            status=None,
            latency_ms=elapsed_ms(),
            error=f"{type(exc).__name__}: {exc}",
            keyword_ok=None,
        )

    keyword_ok = _keyword_ok(target, body)
    ok = (not target.expect or status in target.expect) and keyword_ok
    error = None
    if not ok:
        if target.expect and status not in target.expect:
            error = f"HTTP {status}"
        elif not keyword_ok:
            error = f'keyword "{target.keyword}" not found'
    return CheckResult(
        name=target.name,
        url=target.url,
        timestamp=now,
        ok=ok,
        status=status,
        latency_ms=latency,
        error=error,
        keyword_ok=keyword_ok,
    )


def _keyword_ok(target: Target, body: bytes) -> bool:
    if not target.keyword:
        return True
    try:
        text = body.decode("utf-8", errors="ignore")
    except Exception:
        text = ""
    return target.keyword in text


def _short_reason(exc: BaseException) -> str:
    if isinstance(exc, urllib.error.URLError) and exc.reason is not None:
        reason = exc.reason
        if isinstance(reason, socket.timeout):
            return "timeout"
        if isinstance(reason, OSError):
            return f"connection failed ({reason.strerror or reason})"
        return f"connection failed ({reason})"
    if isinstance(exc, (socket.timeout, TimeoutError)):
        return "timeout"
    if isinstance(exc, OSError):
        return f"connection failed ({exc.strerror or exc})"
    return f"{type(exc).__name__}: {exc}"


def check_all(targets: Iterable[Target], max_workers: int = 8) -> List[CheckResult]:
    """Check every target concurrently; results come back in target order."""
    targets = list(targets)
    results: List[CheckResult] = [None] * len(targets)  # type: ignore[list-item]
    if not targets:
        return []
    with ThreadPoolExecutor(max_workers=min(max_workers, len(targets))) as pool:
        future_to_idx = {pool.submit(check_one, t): i for i, t in enumerate(targets)}
        for future in as_completed(future_to_idx):
            results[future_to_idx[future]] = future.result()
    return results
