"""Final multi-source RSS must preserve distinct facts and persisted events."""

import socket
import sqlite3
import uuid

import feedparser
import pytest

import db.database as database
import routes.feed_routes as feeds
from test_filter_multi_source_fallback import A, B, _get, _xml, multi_client as multi_client


OCT1 = "央行：10月1日，开展1000亿元7天期逆回购操作"
OCT2 = "央行：10月2日，开展1000亿元7天期逆回购操作"


@pytest.fixture(autouse=True)
def no_network(monkeypatch):
    def forbidden(*_args, **_kwargs):
        raise AssertionError("External network is forbidden")
    monkeypatch.setattr(socket.socket, "connect", forbidden)


def _event_ids(*, merged=False):
    first, second = [str(uuid.uuid5(uuid.NAMESPACE_URL, key)) for key in ("first", "second")]
    with sqlite3.connect(database.DB_PATH) as conn:
        conn.execute("CREATE TABLE events (event_id TEXT PRIMARY KEY, merge_state TEXT, merged_into TEXT)")
        conn.execute("INSERT INTO events VALUES (?, 'active', NULL)", (first,))
        conn.execute("INSERT INTO events VALUES (?, ?, ?)",
                     (second, "merged" if merged else "active", first if merged else None))
    return first, second


def _row(title, event_id="", *, link="", score=8.5, timestamp=2_000_000_000):
    return (link or f"https://news.example/{title}", title, title, score, "宏观", "理由", "摘要",
            timestamp, event_id, 1)


def _entries(response):
    assert response.status_code == 200
    return feedparser.parse(response.data).entries


@pytest.mark.parametrize("same_link", [False, True])
def test_distinct_confirmed_events_survive_final_rss_dedup(multi_client, same_link):
    client, state = multi_client
    first, second = _event_ids()
    link = "https://news.example/shared" if same_link else ""
    state.rows[A] = [_row(OCT1, first, link=link)]
    state.rows[B] = [_row(OCT2, second, link=link)]
    assert {entry.title for entry in _entries(_get(client))} == {OCT1, OCT2}


@pytest.mark.parametrize("merged", [False, True])
def test_same_canonical_event_uses_highest_persisted_score(multi_client, merged):
    client, state = multi_client
    first, second = _event_ids(merged=merged)
    state.rows[A] = [_row(OCT1, first, score=9.0, timestamp=100)]
    state.rows[B] = [_row("同一操作事件的转载标题", second if merged else first, score=8.0, timestamp=200)]
    entries = _entries(_get(client))
    assert len(entries) == 1
    assert entries[0].title == OCT1  # Stored score survives plain display titles.


@pytest.mark.parametrize("xml_cache", [False, True])
@pytest.mark.parametrize("same_link", [False, True])
def test_no_id_dated_events_are_not_guessed_identical(multi_client, xml_cache, same_link):
    client, state = multi_client
    link = "https://news.example/shared" if same_link else ""
    if xml_cache:
        state.cache[A] = _xml(OCT1)
        state.cache[B] = _xml(OCT2)
        if same_link:
            state.cache[A] = state.cache[A].replace(f"https://news.example/{OCT1}", link)
            state.cache[B] = state.cache[B].replace(f"https://news.example/{OCT2}", link)
    else:
        state.rows[A] = [_row(OCT1, link=link)]
        state.rows[B] = [_row(OCT2, link=link)]
    assert {entry.title for entry in _entries(_get(client))} == {OCT1, OCT2}


def test_identical_xml_article_still_deduplicates_but_equal_titles_on_other_links_do_not(multi_client):
    client, state = multi_client
    state.cache[A] = _xml(OCT1)
    state.cache[B] = _xml(OCT1)
    assert len(_entries(_get(client))) == 1
    state.cache.clear()
    state.cache[A] = _xml(OCT1)
    state.cache[B] = _xml(OCT1).replace(f"https://news.example/{OCT1}", "https://other.example/copy")
    assert len(_entries(_get(client))) == 2


def test_unresolved_explicit_ids_do_not_collapse_even_with_identical_title_and_link(multi_client):
    client, state = multi_client
    first, second = _event_ids()
    state.rows[A] = [_row(OCT1, first, link="https://news.example/same")]
    state.rows[B] = [_row(OCT1, second, link="https://news.example/same")]
    with sqlite3.connect(database.DB_PATH) as conn:
        conn.execute("DELETE FROM events")
    assert len(_entries(_get(client))) == 2


def test_idless_missing_links_and_old_destructive_combination_cache_do_not_hide_items(multi_client):
    first = feeds.FeedEntry(OCT1, "")
    second = feeds.FeedEntry(OCT1, "")
    assert feeds._dedupe_multi_source_entries([first, second]) == [first, second]
    client, state = multi_client
    state.cache["multi:source-complete-v1:" + ",".join((A, B))] = _xml("旧缓存已误合并")
    state.rows[A] = [_row(OCT1)]
    state.rows[B] = [_row(OCT2)]
    assert {entry.title for entry in _entries(_get(client))} == {OCT1, OCT2}
