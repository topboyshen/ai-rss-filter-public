"""Pending scoring stays visible through real feed processing and cold HTTP."""
import re
import socket
from types import SimpleNamespace

import feedparser
import pytest

from core import processor
from routes import feed_routes
from test_filter_multi_source_fallback import A, _get, _row, _xml, multi_client  # noqa: F401


@pytest.fixture(autouse=True)
def forbid_network(monkeypatch):
    def forbidden(*_args, **_kwargs):
        raise AssertionError("This regression must not contact feeds or models")

    monkeypatch.setattr(socket.socket, "connect", forbidden)


@pytest.fixture
def real_cold_feed(multi_client, monkeypatch):  # noqa: F811
    client, state = multi_client
    state.pending_links = {"https://news.example/two"}
    state.health = []
    state.stat_updates = []
    cfg = feed_routes._cfg()
    cfg.update({"_blacklist_re": re.compile("never-match"), "max_entries": 15,
                "rss_max_fetch_attempts": 1, "rss_timeout": 1})
    raw = ("<rss version='2.0'><channel><title>test source</title>" + "".join(
        f"<item><title>news {key}</title><link>https://news.example/{key}</link>"
        "<pubDate>Sun, 18 Oct 2026 00:00:00 GMT</pubDate></item>"
        for key in ("one", "two")
    ) + "</channel></rss>").encode()
    monkeypatch.setattr(processor, "resolve_scoring_contract", lambda cfg, **_kw: cfg)
    monkeypatch.setattr(processor, "_load_feed_bytes", lambda *_a: raw)
    monkeypatch.setattr(processor, "_repair_cached_feed_origins", lambda *_a: {})
    monkeypatch.setattr(processor, "Scorer", lambda: SimpleNamespace(backends=[object()]))
    monkeypatch.setattr(processor.DB, "get_recent_entries", lambda **_kw: [])
    monkeypatch.setattr(processor.DB, "get_feed_thresholds", lambda *_a: {"notify": 9})
    monkeypatch.setattr(processor.DB, "update_feed_health", lambda *a: state.health.append(a))
    monkeypatch.setattr(processor, "bump_stat", lambda _fn, **kw: state.stat_updates.append(kw))
    monkeypatch.setattr(feed_routes.DB, "log_request", lambda **_kw: None)
    monkeypatch.setattr(feed_routes, "flush_stats", lambda *_a: None)
    monkeypatch.setattr(feed_routes, "process_feed", processor.process_feed)

    def score(entry, *_args, **_kwargs):
        if entry.link in state.pending_links:
            return None, processor.SCORE_DEFERRED_REASON
        entry.title = "[⭐8.0] " + entry.title
        return entry, "scored"

    monkeypatch.setattr(processor, "process_entry", score)
    return client, state, cfg


@pytest.mark.parametrize("all_pending", [False, True])
def test_pending_scores_are_not_filtered_or_entry_errors(real_cold_feed, all_pending):
    _client, state, cfg = real_cold_feed
    if all_pending:
        state.pending_links.add("https://news.example/one")
    entries, stats, error, _title = processor.process_feed(
        A, "prompt", cfg=cfg, check_output_cache=False,
    )
    assert stats["total"] == 2
    assert stats["score_deferred"] == 1 + all_pending
    assert stats["entry_errors"] == 0 and stats["api_calls"] == 0
    assert len(entries) == stats["passed"] == 1 - all_pending
    assert error is None
    assert state.stat_updates[-1]["filtered_count"] == 0
    assert state.health[-1][3] is True  # Usable publisher document was fetched.
    assert "待评分" in state.health[-1][-1]


def test_multi_cold_pending_is_partial_then_recovery_populates_complete_cache(real_cold_feed):
    client, state, _cfg = real_cold_feed
    response = _get(client, (A,))
    assert response.status_code == 200
    assert response.headers["X-Filter-Multi-State"] == "partial"
    assert response.headers["Cache-Control"] == "no-store"
    assert [entry.link for entry in feedparser.parse(response.data).entries] == [
        "https://news.example/one",
    ]
    assert state.cache_writes == []

    state.pending_links.clear()
    recovered = _get(client, (A,))
    assert recovered.headers["X-Filter-Multi-State"] == "complete"
    assert {entry.link for entry in feedparser.parse(recovered.data).entries} == {
        "https://news.example/one", "https://news.example/two",
    }
    assert len(state.cache_writes) == 1
    assert state.stat_updates[-1]["filtered_count"] == 0


def test_all_pending_cold_multi_is_not_cached_as_successfully_empty(real_cold_feed):
    client, state, _cfg = real_cold_feed
    state.pending_links.add("https://news.example/one")
    response = _get(client, (A,))
    assert response.status_code == 200
    assert response.headers["X-Filter-Multi-State"] == "partial"
    assert feedparser.parse(response.data).entries == []
    assert state.cache_writes == []


def test_single_cold_feed_keeps_pending_state_out_of_full_cache(real_cold_feed):
    client, state, _cfg = real_cold_feed
    response = client.get("/filter", query_string={"url": A})
    assert response.status_code == 200
    assert response.headers["X-Filter-State"] == "partial"
    assert response.headers["Cache-Control"] == "no-store"
    assert len(feedparser.parse(response.data).entries) == 1
    assert state.cache_writes == []

    state.pending_links.clear()
    recovered = client.get("/filter", query_string={"url": A})
    assert recovered.headers["X-Filter-State"] == "complete"
    assert len(feedparser.parse(recovered.data).entries) == 2
    assert len(state.cache_writes) == 1


@pytest.mark.parametrize("multi", [False, True])
def test_landed_subset_cannot_promote_pending_cold_result_to_complete(real_cold_feed, multi):
    client, state, _cfg = real_cold_feed

    def request():
        return _get(client, (A,)) if multi else client.get("/filter", query_string={"url": A})

    header = "X-Filter-Multi-State" if multi else "X-Filter-State"
    assert request().headers[header] == "partial"
    # The accepted first item has landed; the other item still awaits scoring.
    state.rows[A] = [_row("one")]
    processed = len(state.stat_updates)
    for _ in range(2):
        partial = request()
        assert partial.headers[header] == "partial"
        assert partial.headers["Cache-Control"] == "no-store"
        assert [e.link for e in feedparser.parse(partial.data).entries] == ["https://news.example/one"]
    assert state.pending_links == {"https://news.example/two"}
    assert len(state.stat_updates) == processed  # No refill, scoring, or expanded query.
    assert state.cache_writes == []

    # A complete natural refresh can supply the full source XML later.
    state.pending_links.clear()
    second_item = "<item><title>two</title><link>https://news.example/two</link>" \
        "<pubDate>Sun, 19 Jul 2026 00:00:00 GMT</pubDate></item>"
    state.cache[A] = _xml("one").replace("</channel>", second_item + "</channel>")
    recovered = request()
    assert recovered.headers[header] == "complete"
    assert len(feedparser.parse(recovered.data).entries) == 2
    assert len(state.stat_updates) == processed
