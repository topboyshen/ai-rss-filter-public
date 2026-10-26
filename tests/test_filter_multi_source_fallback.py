"""Exercise per-source fallback through the real Flask multi-feed route."""

from types import SimpleNamespace

import feedparser
import pytest
from flask import Flask

import routes.feed_routes as feed_routes


A = "https://example.com/a"
B = "https://example.com/b"
C = "https://example.com/c"


def _xml(title=None):
    item = "" if title is None else (
        f"<item><title>{title}</title><link>https://news.example/{title}</link>"
        "<pubDate>Sun, 19 Jul 2026 00:00:00 GMT</pubDate></item>"
    )
    return f"<rss version='2.0'><channel><title>source</title>{item}</channel></rss>"


def _row(title):
    return (f"https://news.example/{title}", title, title, 8.5, "macro", "logic", "summary", 1)


@pytest.fixture
def multi_client(monkeypatch):
    state = SimpleNamespace(
        cache={}, rows={}, cold={}, cache_reads=[], db_reads=[], cold_reads=[],
        cache_writes=[], acquired=0, released=0, capacity=True,
    )
    cfg = {"cache_ttl": 60, "output": {"max_items": 30}, "multi_max_workers": 3}
    monkeypatch.setattr(feed_routes, "_cfg", lambda: cfg)
    monkeypatch.setattr(feed_routes, "resolve_scoring_contract", lambda *_args, **_kwargs: cfg)
    monkeypatch.setattr(feed_routes, "check_api_key", lambda: True)
    monkeypatch.setattr(feed_routes, "validate_target_url", lambda *_args: (True, ""))
    monkeypatch.setattr(feed_routes, "strategy_cache_key", lambda value, _cfg: value)
    monkeypatch.setattr(feed_routes, "resolve_feed_url_to_cache_key", lambda url, _cfg: url)
    monkeypatch.setattr(
        feed_routes, "resolve_prompt", lambda url, _cfg: ("default", url)
    )

    def get_cached(key, _ttl):
        state.cache_reads.append(key)
        return state.cache.get(key)

    def set_cached(key, value):
        state.cache_writes.append((key, value))
        state.cache[key] = value

    def db_read(_cfg, **kwargs):
        url = kwargs["feed_url"]
        state.db_reads.append(url)
        value = state.rows.get(url, [])
        if isinstance(value, Exception):
            raise value
        return value

    def process(url, _prompt, **kwargs):
        assert kwargs["check_output_cache"] is False
        state.cold_reads.append(url)
        value = state.cold.get(url, ([], {}, None, "source"))
        if isinstance(value, Exception):
            raise value
        return value

    def acquire():
        state.acquired += 1
        return state.capacity

    def release():
        state.released += 1

    monkeypatch.setattr(feed_routes, "get_cached", get_cached)
    monkeypatch.setattr(feed_routes, "set_cached", set_cached)
    monkeypatch.setattr(feed_routes.DB, "get_entries_for_cached_rss", db_read)
    monkeypatch.setattr(feed_routes, "process_feed", process)
    monkeypatch.setattr(feed_routes, "_try_acquire_cold_filter_slot", acquire)
    monkeypatch.setattr(feed_routes, "_release_cold_filter_slot", release)
    app = Flask(__name__)
    app.config["TESTING"] = True
    app.register_blueprint(feed_routes.feed_bp)
    return app.test_client(), state


def _get(client, urls=(A, B)):
    return client.get("/filter/multi", query_string={"urls": ",".join(urls)})


def _titles(response):
    return {entry.title for entry in feedparser.parse(response.data).entries}


def test_cached_source_does_not_hide_another_sources_db_rows(multi_client):
    client, state = multi_client
    state.cache[A] = _xml("cached-A")
    state.rows[B] = [_row("database-B")]

    response = _get(client)

    assert response.status_code == 200
    assert _titles(response) == {"cached-A", "database-B"}
    assert state.db_reads == [B]
    assert state.cold_reads == []
    assert response.headers["X-Filter-Multi-State"] == "partial"
    assert response.headers["Cache-Control"] == "no-store"
    assert state.cache_writes == []


def test_each_source_can_independently_use_cache_db_or_cold_path(multi_client):
    client, state = multi_client
    state.cache[A] = _xml("cached-A")
    state.rows[B] = [_row("database-B")]
    state.cold[C] = ([feed_routes.FeedEntry("cold-C", "https://news.example/c", "")], {}, None, "C")

    response = _get(client, (A, B, C))

    assert _titles(response) == {"cached-A", "database-B", "cold-C"}
    assert state.db_reads == [B, C]
    assert state.cold_reads == [C]
    assert state.acquired == state.released == 1
    assert response.headers["X-Filter-Multi-State"] == "partial"
    assert state.cache_writes == []


def test_all_cached_sources_skip_db_and_cold_capacity(multi_client):
    client, state = multi_client
    state.cache.update({A: _xml("cached-A"), B: _xml("cached-B")})

    response = _get(client)

    assert _titles(response) == {"cached-A", "cached-B"}
    assert state.db_reads == state.cold_reads == []
    assert state.acquired == state.released == 0
    assert state.cache_reads.count(A) == state.cache_reads.count(B) == 1


def test_successful_empty_xml_is_resolved_not_a_missing_source(multi_client):
    client, state = multi_client
    state.cache[A] = _xml()
    state.rows[B] = [_row("database-B")]

    response = _get(client)

    assert _titles(response) == {"database-B"}
    assert state.db_reads == [B]
    assert state.cold_reads == []
    assert response.headers["X-Filter-Multi-State"] == "partial"
    assert state.cache_writes == []


def test_successful_empty_cold_source_is_complete(multi_client):
    client, state = multi_client
    state.cache[A] = _xml("cached-A")
    state.cold[B] = ([], {}, None, "B")

    response = _get(client)

    assert _titles(response) == {"cached-A"}
    assert state.cold_reads == [B]
    assert response.headers["X-Filter-Multi-State"] == "complete"
    assert len(state.cache_writes) == 1


def test_all_successful_empty_cached_sources_avoid_cold_processing(multi_client):
    client, state = multi_client
    state.cache.update({A: _xml(), B: _xml()})

    response = _get(client)

    assert response.status_code == 200
    assert _titles(response) == set()
    assert response.headers["X-Filter-Multi-State"] == "complete"
    assert state.db_reads == state.cold_reads == []
    assert state.acquired == state.released == 0


def test_failed_empty_source_stays_distinct_from_successful_empty(multi_client):
    client, state = multi_client
    state.cache[A] = _xml()
    state.cold[B] = ([], {}, "upstream timeout", "B")

    response = _get(client)

    assert response.status_code == 200
    assert _titles(response) == set()
    assert response.headers["X-Filter-Multi-State"] == "partial"
    assert state.cold_reads == [B]
    assert state.cache_writes == []


@pytest.mark.parametrize("include_partial_entry", [False, True])
def test_failed_cold_source_never_populates_a_complete_combination_cache(
    multi_client, include_partial_entry
):
    client, state = multi_client
    state.cache[A] = _xml("cached-A")
    partial = [feed_routes.FeedEntry("partial-B", "https://news.example/partial", "")] if include_partial_entry else []
    state.cold[B] = (partial, {}, "upstream timeout", "B")

    response = _get(client)

    assert response.status_code == 200  # Keep the existing best-effort RSS response.
    assert "cached-A" in _titles(response)
    assert ("partial-B" in _titles(response)) is include_partial_entry
    assert state.cold_reads == [B]
    assert response.headers["X-Filter-Multi-State"] == "partial"
    assert state.cache_writes == []
    assert state.acquired == state.released == 1


@pytest.mark.parametrize("include_survivor", [False, True])
def test_partial_entry_processing_is_not_cached_as_complete(multi_client, include_survivor):
    client, state = multi_client
    state.cache[A] = _xml("cached-A")
    survivors = [feed_routes.FeedEntry("survivor-B", "https://news.example/b", "")] if include_survivor else []
    # process_feed deliberately returns no transport error when only some
    # entries failed; the entry_errors counter retains that degraded state.
    state.cold[B] = (survivors, {"total": 2, "entry_errors": 1}, None, "B")

    response = _get(client)

    assert response.status_code == 200
    assert "cached-A" in _titles(response)
    assert ("survivor-B" in _titles(response)) is include_survivor
    assert response.headers["X-Filter-Multi-State"] == "partial"
    assert state.cache_writes == []
    assert state.acquired == state.released == 1


@pytest.mark.parametrize("document", [
    "", "<html><head><title>Upstream error</title></head><body>Error</body></html>",
    "<response><message>Unavailable</message></response>",
])
def test_well_formed_non_feed_cache_falls_back_to_that_sources_db(multi_client, document):
    client, state = multi_client
    parsed = feedparser.parse(document)
    assert not parsed.bozo  # Well-formed input alone does not make an RSS/Atom feed.
    assert not parsed.get("version")
    state.cache.update({A: _xml("cached-A"), B: document})
    state.rows[B] = [_row("database-B")]

    response = _get(client)

    assert _titles(response) == {"cached-A", "database-B"}
    assert state.db_reads == [B]
    assert state.cold_reads == []
    assert response.headers["X-Filter-Multi-State"] == "partial"
    assert state.cache_writes == []


def test_successful_empty_atom_cache_is_also_a_resolved_source(multi_client):
    client, state = multi_client
    state.cache[A] = '<feed xmlns="http://www.w3.org/2005/Atom"><title>Empty feed</title></feed>'
    state.rows[B] = [_row("database-B")]

    response = _get(client)

    assert _titles(response) == {"database-B"}
    assert state.db_reads == [B]
    assert state.cold_reads == []
    assert response.headers["X-Filter-Multi-State"] == "partial"
    assert state.cache_writes == []


def test_duplicate_urls_do_not_repeat_source_reads(multi_client):
    client, state = multi_client
    state.cache[A] = _xml("cached-A")
    state.rows[B] = [_row("database-B")]

    response = _get(client, (A, A, B, B))

    assert _titles(response) == {"cached-A", "database-B"}
    assert state.cache_reads.count(A) == state.cache_reads.count(B) == 1
    assert state.db_reads == [B]


def test_duplicate_cold_urls_are_processed_only_once(multi_client):
    client, state = multi_client

    response = _get(client, (A, A, B, B))

    assert response.status_code == 200
    assert state.db_reads == state.cold_reads == [A, B]
    assert state.acquired == state.released == 1
    assert response.headers["X-Filter-Multi-State"] == "complete"


def test_malformed_single_source_cache_uses_that_sources_db_fallback(multi_client):
    client, state = multi_client
    state.cache.update({A: _xml("cached-A"), B: "<rss><channel><item>unterminated"})
    state.rows[B] = [_row("database-B")]

    response = _get(client)

    assert _titles(response) == {"cached-A", "database-B"}
    assert state.db_reads == [B]
    assert state.cold_reads == []
    assert response.headers["X-Filter-Multi-State"] == "partial"
    assert state.cache_writes == []


def test_old_potentially_partial_combination_cache_is_not_reused(multi_client):
    client, state = multi_client
    state.cache["multi:" + ",".join((A, B))] = _xml("old-partial-A")
    state.cache[A] = _xml("cached-A")
    state.rows[B] = [_row("database-B")]

    response = _get(client)

    assert _titles(response) == {"cached-A", "database-B"}
    assert state.cache_writes == []
    assert response.headers["X-Filter-Multi-State"] == "partial"


def test_complete_combination_cache_avoids_repeating_individual_reads(multi_client):
    client, state = multi_client
    state.cache.update({A: _xml("cached-A"), B: _xml("cached-B")})
    _get(client)
    state.cache_reads.clear()

    response = _get(client)

    assert _titles(response) == {"cached-A", "cached-B"}
    assert len(state.cache_reads) == 1
    assert response.headers["X-Filter-Multi-State"] == "complete"


@pytest.mark.parametrize("failure_location", ["database", "cold"])
def test_failure_preserves_existing_exception_and_does_not_cache_partial_output(
    multi_client, failure_location
):
    client, state = multi_client
    state.cache[A] = _xml("cached-A")
    if failure_location == "database":
        state.rows[B] = RuntimeError("database unavailable")
    else:
        state.cold[B] = RuntimeError("cold unavailable")

    with pytest.raises(RuntimeError, match="unavailable"):
        _get(client)

    assert state.cache_writes == []
    if failure_location == "cold":
        assert state.acquired == state.released == 1


def test_mixed_source_cold_fallback_still_obeys_capacity(multi_client):
    client, state = multi_client
    state.cache[A] = _xml("cached-A")
    state.capacity = False

    response = _get(client)

    assert response.status_code == 503
    assert response.headers["Retry-After"] == "5"
    assert state.cold_reads == state.cache_writes == []
    assert state.acquired == 1 and state.released == 0


def test_multi_source_authentication_still_precedes_reads(multi_client, monkeypatch):
    client, state = multi_client
    monkeypatch.setattr(feed_routes, "check_api_key", lambda: False)

    response = _get(client)

    assert response.status_code == 401
    assert state.cache_reads == state.db_reads == state.cold_reads == []
