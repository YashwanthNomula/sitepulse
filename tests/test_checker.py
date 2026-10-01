"""Tests for the HTTP checking engine (against a local server)."""
from __future__ import annotations

from sitepulse.checker import check_all, check_one
from sitepulse.models import Target


def test_fast_ok(server):
    r = check_one(Target(name="fast", url=server + "/fast"))
    assert r.ok and r.status == 200
    assert r.latency_ms is not None and r.latency_ms < 2000
    assert r.error is None


def test_server_error_is_down(server):
    r = check_one(Target(name="boom", url=server + "/boom"))
    assert not r.ok
    assert r.status == 500
    assert "500" in (r.error or "")


def test_custom_expect_status(server):
    r = check_one(Target(name="missing", url=server + "/missing", expect=[404]))
    assert r.ok and r.status == 404


def test_keyword_match(server):
    good = check_one(Target(name="kw", url=server + "/secret", keyword="swordfish"))
    assert good.ok and good.keyword_ok
    bad = check_one(Target(name="kw2", url=server + "/secret", keyword="hunter2"))
    assert not bad.ok
    assert "keyword" in (bad.error or "")


def test_connection_refused_is_down():
    r = check_one(Target(name="dead", url="http://127.0.0.1:1/", timeout_s=2))
    assert not r.ok
    assert r.status is None
    assert r.error  # e.g. "connection failed (...)"


def test_timeout_is_down(server):
    r = check_one(Target(name="slow", url=server + "/slow", timeout_s=0.05))
    assert not r.ok
    assert r.error == "timeout"


def test_check_all_preserves_order(server):
    targets = [
        Target(name="a", url=server + "/fast"),
        Target(name="b", url=server + "/boom"),
        Target(name="c", url=server + "/fast"),
    ]
    results = check_all(targets)
    assert [r.name for r in results] == ["a", "b", "c"]
    assert [r.ok for r in results] == [True, False, True]


def test_check_all_empty():
    assert check_all([]) == []
