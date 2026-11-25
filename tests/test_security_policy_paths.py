"""Check URL normalization, client quotas and webhook signatures with synthetic requests."""

from __future__ import annotations

import hashlib
import hmac

import pytest
from flask import Flask

from routes import security


@pytest.fixture(autouse=True)
def _clean_limiters():
    security._rate_limit_store.clear()
    security._refresh_rate_limit_store.clear()
    yield
    security._rate_limit_store.clear()
    security._refresh_rate_limit_store.clear()


def test_parse_http_endpoint_normalises_accepted_url():
    endpoint, error = security._parse_http_endpoint("  HTTPS://Example.COM?x=1  ")
    assert error == ""
    assert (endpoint.scheme, endpoint.host, endpoint.port, endpoint.path, endpoint.query) == (
        "https", "example.com", 443, "/", "x=1",
    )


def _ctx(remote_addr, headers=None):
    return Flask(__name__).test_request_context(
        "/", environ_base={"REMOTE_ADDR": remote_addr}, headers=headers or {}
    )


class _Clock:
    def __init__(self, now=1_000_000.0):
        self.now = now

    def __call__(self):
        return self.now


@pytest.fixture
def clock(monkeypatch):
    c = _Clock()
    monkeypatch.setattr(security.time, "time", c)
    monkeypatch.delenv("AI_FILTER_TRUSTED_PROXY_HOST", raising=False)
    return c


def _limited_app():
    app = Flask(__name__)

    @app.route("/limited")
    @security.rate_limit(max_requests=2, window_seconds=30)
    def limited():
        return "ok"

    return app.test_client()


def test_rate_limit_blocks_after_quota_and_resets_after_window(clock):
    client = _limited_app()
    assert [client.get("/limited").status_code for _ in range(2)] == [200, 200]
    blocked = client.get("/limited")
    assert blocked.status_code == 429
    assert blocked.headers["Retry-After"] == "30"
    assert blocked.get_json() == {"error": "Rate limit exceeded"}
    clock.now += 31
    assert client.get("/limited").status_code == 200


def test_rate_limit_buckets_are_per_client(clock):
    client = _limited_app()
    for _ in range(3):
        client.get("/limited")
    other = client.get("/limited", environ_base={"REMOTE_ADDR": "192.0.2.9"})
    assert other.status_code == 200


def test_rate_limit_store_evicts_stale_entries_when_large(clock):
    for index in range(4097):
        security._rate_limit_store[("old", str(index))] = (clock.now - 7200, 1)
    security._rate_limit_store[("recent", "x")] = (clock.now - 10, 1)
    assert _limited_app().get("/limited").status_code == 200
    assert not any(key[0] == "old" for key in security._rate_limit_store)
    assert ("recent", "x") in security._rate_limit_store


def test_refresh_limit_is_one_per_minute_per_client(clock):
    with _ctx("192.0.2.1"):
        assert security.check_refresh_rate_limit() == (True, "")
        clock.now += 15
        ok, message = security.check_refresh_rate_limit()
        assert ok is False and message == "请求过于频繁，请 45 秒后重试"
        clock.now += 45
        assert security.check_refresh_rate_limit() == (True, "")
    with _ctx("192.0.2.2"):
        assert security.check_refresh_rate_limit() == (True, "")


def test_refresh_store_evicts_stale_clients(clock):
    for index in range(4097):
        security._refresh_rate_limit_store[f"old{index}"] = clock.now - 120
    with _ctx("192.0.2.5"):
        assert security.check_refresh_rate_limit() == (True, "")
    assert len(security._refresh_rate_limit_store) == 1


# --- Miniflux signature -----------------------------------------------------

BODY = b'{"event_type":"new_entries"}'


def _signed(secret, body=BODY):
    return hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()


def _verify(headers, body=BODY):
    with Flask(__name__).test_request_context("/", method="POST", data=body, headers=headers):
        return security.verify_miniflux_signature()


def test_signature_binds_body(monkeypatch):
    monkeypatch.setenv("MINIFLUX_WEBHOOK_SECRET", "test-fixture-key")
    headers = {"X-Miniflux-Signature": _signed("test-fixture-key")}
    assert _verify(headers) is True
    assert _verify(headers, body=BODY + b" ") is False
