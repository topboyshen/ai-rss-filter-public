import pytest
from flask import Flask

import routes.feed_routes as feed_routes


class _Slot:
    def __init__(self, available: bool):
        self.available = available
        self.acquired = 0
        self.released = 0

    def acquire(self, blocking: bool = True) -> bool:
        assert blocking is False
        self.acquired += 1
        return self.available

    def release(self) -> None:
        self.released += 1


@pytest.fixture
def client(monkeypatch):
    cfg = {
        "cache_ttl": 60,
        # Production historically allowed fan-out here.  The cold-path guard
        # must remain authoritative even when this legacy knob is greater than
        # one.
        "multi_max_workers": 3,
        "output": {"max_items": 30},
    }
    monkeypatch.setattr(feed_routes, "check_api_key", lambda: True)
    monkeypatch.setattr(feed_routes, "validate_target_url", lambda *_args: (True, ""))
    monkeypatch.setattr(feed_routes, "_cfg", lambda: cfg)
    monkeypatch.setattr(feed_routes.DB, "get_entries_for_cached_rss", lambda *_args, **_kwargs: [])
    app = Flask(__name__)
    app.config["TESTING"] = True
    app.register_blueprint(feed_routes.feed_bp)
    return app.test_client()


def test_single_cold_filter_rejects_before_fetch_when_capacity_is_full(
    client, monkeypatch
):
    slot = _Slot(False)
    monkeypatch.setattr(feed_routes, "_COLD_FILTER_SLOTS", slot)
    monkeypatch.setattr(feed_routes, "get_cached", lambda *_args: None)
    monkeypatch.setattr(
        feed_routes,
        "process_feed",
        lambda *_args, **_kwargs: pytest.fail("cold fetch must not start"),
    )

    response = client.get("/filter?url=https://example.com/feed")

    assert response.status_code == 503
    assert response.headers["Retry-After"] == "5"
    assert response.get_json()["error"] == "cold_filter_capacity_reached"
    assert slot.acquired == 1
    assert slot.released == 0


def test_cached_filter_does_not_consume_a_cold_slot(client, monkeypatch):
    slot = _Slot(False)
    xml = (
        "<rss><channel><item><title>cached</title>"
        "<pubDate>Sun, 19 Jul 2026 00:00:00 GMT</pubDate>"
        "</item></channel></rss>"
    )
    monkeypatch.setattr(feed_routes, "_COLD_FILTER_SLOTS", slot)
    monkeypatch.setattr(feed_routes, "get_cached", lambda *_args: xml)

    response = client.get("/filter?url=https://example.com/feed")

    assert response.status_code == 200
    assert b"cached" in response.data
    assert slot.acquired == 0


def test_multi_cold_filter_uses_the_same_capacity_guard(client, monkeypatch):
    slot = _Slot(False)
    monkeypatch.setattr(feed_routes, "_COLD_FILTER_SLOTS", slot)
    monkeypatch.setattr(feed_routes, "get_cached", lambda *_args: None)
    monkeypatch.setattr(
        feed_routes,
        "process_feed",
        lambda *_args, **_kwargs: pytest.fail("cold multi fetch must not start"),
    )

    response = client.get("/filter/multi?urls=https://example.com/a,https://example.com/b")

    assert response.status_code == 503
    assert response.headers["Retry-After"] == "5"
    assert slot.acquired == 1
    assert slot.released == 0


def test_multi_cold_filter_serializes_work_inside_one_capacity_slot(
    client, monkeypatch
):
    slot = _Slot(True)
    active = 0
    peak = 0
    calls = []
    monkeypatch.setattr(feed_routes, "_COLD_FILTER_SLOTS", slot)
    monkeypatch.setattr(feed_routes, "get_cached", lambda *_args: None)
    monkeypatch.setattr(
        feed_routes, "resolve_prompt", lambda url, *_args: ("default", url)
    )

    def process(url, *_args, **_kwargs):
        nonlocal active, peak
        active += 1
        peak = max(peak, active)
        calls.append(url)
        active -= 1
        return [], {}, None, "source"

    monkeypatch.setattr(feed_routes, "process_feed", process)

    response = client.get(
        "/filter/multi?urls=https://example.com/a,https://example.com/b,https://example.com/c"
    )

    assert response.status_code == 200
    assert calls == [
        "https://example.com/a",
        "https://example.com/b",
        "https://example.com/c",
    ]
    assert peak == 1
    assert slot.acquired == 1
    assert slot.released == 1


def test_cold_slot_is_released_when_processing_raises(client, monkeypatch):
    slot = _Slot(True)
    monkeypatch.setattr(feed_routes, "_COLD_FILTER_SLOTS", slot)
    monkeypatch.setattr(feed_routes, "get_cached", lambda *_args: None)
    monkeypatch.setattr(
        feed_routes, "resolve_prompt", lambda *_args: ("default", "prompt")
    )

    def fail(*_args, **_kwargs):
        raise RuntimeError("fetch failed")

    monkeypatch.setattr(feed_routes, "process_feed", fail)

    with pytest.raises(RuntimeError, match="fetch failed"):
        client.get("/filter?url=https://example.com/feed")

    assert slot.acquired == 1
    assert slot.released == 1
