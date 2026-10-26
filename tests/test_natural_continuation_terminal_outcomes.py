"""Terminal unfinished scores are neither pending work nor successful filtering."""

import copy
import re
import sqlite3
import time
from html import escape
from types import SimpleNamespace

import pytest

from core import processor
from services import natural_score_continuations as store
from services import natural_scoring, refresh
from tests.test_natural_score_continuation import (
    FEED,
    LINK,
    PROMPT,
    _entry,
    _make_due,
    _record,
    _rows,
    pipeline,  # noqa: F401
)

TERMINAL = "score_terminal_unscored"


def _set_record_state(cfg, state, *, attempts=None):
    with store.locked_record(cfg["scoring_strategy_version"], LINK) as record:
        assert record is not None
        data = record.data
        data["state"] = state
        if attempts is not None:
            data["attempts"] = attempts
        data["next_attempt_at"] = data["created_at"]
        record.save(data)


def _advance_expiry(monkeypatch):
    later = time.time() + store.TTL_SECONDS + 1
    monkeypatch.setattr(store, "time", SimpleNamespace(time=lambda: later))
    monkeypatch.setattr(natural_scoring, "time", SimpleNamespace(time=lambda: later))


def _restart(p):
    processor._transport_retry_counts.clear()
    processor._transport_pending_writes.clear()
    p.scorer.clear_runtime_cache()


def _real_refresh(monkeypatch, p):
    cfg = p.cfg
    cfg.update({
        "_blacklist_re": re.compile(r"(?!)"), "cache_ttl": 60,
        "prompts": {"test": PROMPT},
        "feeds": [{"url": FEED, "name": "Synthetic publisher", "prompt": "test", "max_entries": 5}],
    })
    current_entries = [_entry()]
    stats, recorded, counts, cached = [], [], [], []
    real_feed, real_record = refresh.process_feed, refresh.DB.record_refresh_attempt

    def rss_bytes(*_args, **_kwargs):
        items = "".join(
            f"<item><title>{escape(item.title)}</title><link>{escape(item.link)}</link>"
            f"<description>{escape(item.summary)}</description></item>"
            for item in current_entries
        )
        return ("<?xml version='1.0'?><rss version='2.0'><channel><title>publisher</title>"
                + items + "</channel></rss>").encode()

    def process_feed(*args, **kwargs):
        result = real_feed(*args, **kwargs)
        stats.append(copy.deepcopy(result[1]))
        return result

    def record_refresh(result, successful_at=None):
        recorded.append((copy.deepcopy(result), successful_at))
        return real_record(result, successful_at=successful_at)

    monkeypatch.setattr(processor, "Scorer", lambda: p.scorer)
    monkeypatch.setattr(processor, "_load_feed_bytes", rss_bytes)
    monkeypatch.setattr(processor, "resolve_scoring_contract", lambda configuration, **_kw: configuration)
    monkeypatch.setattr(processor, "bump_stat", lambda _db, **kw: counts.append(kw))
    monkeypatch.setattr(refresh, "resolve_scoring_contract", lambda configuration, **_kw: configuration)
    monkeypatch.setattr(refresh, "_cfg", lambda: cfg)
    monkeypatch.setattr(refresh, "get_metrics", lambda: None)
    monkeypatch.setattr(refresh, "flush_stats", lambda *_a, **_kw: None)
    monkeypatch.setattr(refresh, "cleanup_old_entries", lambda *_a, **_kw: None)
    monkeypatch.setattr(refresh, "set_cached", lambda *args, **_kw: cached.append(args))
    monkeypatch.setattr(refresh, "process_feed", process_feed)
    monkeypatch.setattr(refresh.DB, "record_refresh_attempt", record_refresh)
    return stats, recorded, counts, current_entries, cached


@pytest.mark.parametrize("state", ["expired", "exhausted", "contract_changed", "invalid"])
@pytest.mark.parametrize("persist", [False, True])
def test_terminal_arrival_is_unscored_not_pending_or_filtered(pipeline, state, persist):  # noqa: F811
    p = pipeline
    assert p.run()[1] == processor.SCORE_DEFERRED_REASON
    _set_record_state(p.cfg, state, attempts=3 if state == "exhausted" else 1)
    saved, paid = _record(p.cfg), len(p.calls)
    for _ in range(2):
        _restart(p)
        assert p.run(persist=persist) == (None, TERMINAL)
        assert _record(p.cfg) == saved
        assert _rows() == ([], []) and len(p.calls) == paid
    assert saved["state"] == state and "payload" not in saved


@pytest.mark.parametrize("state", ["expired", "exhausted"])
def test_terminal_news_in_two_real_refreshes_is_not_waiting_or_complete(
    monkeypatch, pipeline, state,  # noqa: F811
):
    p = pipeline
    assert p.run()[1] == processor.SCORE_DEFERRED_REASON
    _set_record_state(p.cfg, state, attempts=3 if state == "exhausted" else 1)
    saved, paid = _record(p.cfg), len(p.calls)
    stats, recorded, counts, _, cached = _real_refresh(monkeypatch, p)
    for _ in range(2):
        _restart(p)
        result = refresh._execute_feed_refresh()
        assert result["status"] == "partial"
        assert result["partial_sources"] == 1
        assert result["succeeded_sources"] == result["failed_sources"] == 0
        assert result["score_deferred_entries"] == 0
        assert result["score_terminal_unscored_entries"] == 1
        assert result["score_continuations"]["remaining"]["active"] == 0
        assert stats[-1]["score_deferred"] == 0
        assert stats[-1]["score_terminal_unscored"] == 1
        assert stats[-1]["passed"] == stats[-1]["api_calls"] == 0
        assert counts[-1]["filtered_count"] == counts[-1]["passed_count"] == 0
        assert cached == []
        assert recorded[-1] == (result, None)
        assert "score_terminal_unscored=1" in result["failures"][0]["error"]
        assert _record(p.cfg) == saved and _rows() == ([], [])
        assert len(p.calls) == paid


def test_pending_news_stays_deferred_through_two_real_refreshes(monkeypatch, pipeline):  # noqa: F811
    p = pipeline
    assert p.run()[1] == processor.SCORE_DEFERRED_REASON
    saved, paid = _record(p.cfg), len(p.calls)
    stats, recorded, counts, _, cached = _real_refresh(monkeypatch, p)
    for _ in range(2):
        result = refresh._execute_feed_refresh()
        assert result["status"] == "partial" and result["partial_sources"] == 1
        assert result["score_deferred_entries"] == 1
        assert result["score_terminal_unscored_entries"] == 0
        assert result["score_continuations"]["remaining"]["active"] == 1
        assert stats[-1]["score_deferred"] == 1 and stats[-1]["score_terminal_unscored"] == 0
        assert counts[-1]["filtered_count"] == 0 and recorded[-1] == (result, None)
        assert cached == []
        assert _record(p.cfg) == saved and len(p.calls) == paid
        assert _rows() == ([], [])


def test_newly_expired_slid_out_news_is_reported_once_not_hidden_or_permanently_pending(
    monkeypatch, pipeline,  # noqa: F811
):
    p = pipeline
    assert p.run()[1] == processor.SCORE_DEFERRED_REASON
    _advance_expiry(monkeypatch)
    _, recorded, _, current_entries, _ = _real_refresh(monkeypatch, p)
    current_entries[:] = [_entry(LINK + "/new-after-expiry")]
    p.control["mode"] = "recovered"
    first = refresh._execute_feed_refresh()
    assert first["status"] == "partial"
    assert first["score_continuations"]["terminal_unscored"] == 1
    assert first["score_continuations"]["deferred"] == 0
    assert first["score_deferred_entries"] == first["score_terminal_unscored_entries"] == 0
    assert first["score_continuations"]["remaining"]["active"] == 0
    assert first["score_continuations"]["remaining"]["expired"] == 1
    assert recorded[-1] == (first, None)
    assert _record(p.cfg)["state"] == "expired" and _rows() == ([], [])
    assert len(_rows(LINK + "/new-after-expiry")[1]) == 1
    preserved = copy.deepcopy(first)
    paid = len(p.calls)
    second = refresh._execute_feed_refresh()
    assert second["status"] == "success"
    assert second["score_continuations"]["terminal_unscored"] == 0
    assert second["score_continuations"]["remaining"]["expired"] == 1
    assert recorded[-1] == (second, second["finished_at"])
    assert len(p.calls) == paid and _rows() == ([], [])
    assert first == preserved and first["status"] == "partial"


def test_crashed_last_reservation_is_terminal_not_completed_or_deferred(pipeline):  # noqa: F811
    p = pipeline
    assert p.run()[1] == processor.SCORE_DEFERRED_REASON
    _set_record_state(p.cfg, "running", attempts=store.MAX_ATTEMPTS)
    paid = len(p.calls)
    _restart(p)
    resumed = p.resume()
    assert resumed["attempted"] == resumed["terminal_unscored"] == 1
    assert resumed["completed"] == resumed["deferred"] == resumed["errors"] == 0
    assert _record(p.cfg)["state"] == "exhausted"
    assert _record(p.cfg)["attempts"] == store.MAX_ATTEMPTS
    assert p.run() == (None, TERMINAL)
    assert p.resume()["attempted"] == 0
    assert len(p.calls) == paid and _rows() == ([], [])


@pytest.mark.parametrize("cache_rejected", [False, True])
@pytest.mark.parametrize("terminal_neutral", [False, True])
def test_completed_score_remains_cache_or_ledger_reuse_not_terminal_unscored(
    pipeline, cache_rejected, terminal_neutral,  # noqa: F811
):
    p = pipeline
    p.cfg["cache_rejected_entries"] = cache_rejected
    assert p.run()[1] == processor.SCORE_DEFERRED_REASON
    if terminal_neutral:
        _make_due(p.cfg)
        assert p.resume()["deferred"] == 1
    else:
        p.control["mode"] = "recovered"
    _make_due(p.cfg)
    assert p.resume()["completed"] == 1
    before, saved, paid = _rows(), _record(p.cfg), len(p.calls)
    assert saved["state"] == "completed" and len(before[1]) == 1
    assert len(before[0]) == int(cache_rejected or not terminal_neutral)
    _restart(p)
    _, reason = p.run()
    assert reason == ("filtered" if terminal_neutral else "cache")
    assert p.resume()["attempted"] == 0
    assert len(p.calls) == paid and _rows() == before and _record(p.cfg) == saved


@pytest.mark.parametrize("expire_via_sweep", [False, True])
@pytest.mark.parametrize("restart", [False, True])
def test_expired_frozen_write_is_terminal_without_new_attempt_or_signal(
    monkeypatch, pipeline, expire_via_sweep, restart,  # noqa: F811
):
    p = pipeline
    assert p.run()[1] == processor.SCORE_DEFERRED_REASON
    _make_due(p.cfg)
    assert p.resume()["deferred"] == 1
    _make_due(p.cfg)
    real_write = processor.DB.set_cached_entry

    def fail(*_args, **_kwargs):
        raise sqlite3.OperationalError("database is locked")

    monkeypatch.setattr(processor.DB, "set_cached_entry", fail)
    assert p.resume()["errors"] == 1
    assert processor._transport_pending_writes
    assert _record(p.cfg)["state"] == "commit_pending"
    assert _record(p.cfg)["attempts"] == store.MAX_ATTEMPTS
    _advance_expiry(monkeypatch)
    if expire_via_sweep:
        assert store.pending(p.cfg) == []
    if restart:
        _restart(p)
    paid = len(p.calls)
    monkeypatch.setattr(processor.DB, "set_cached_entry", real_write)
    assert p.run(persist=False) == (None, TERMINAL)
    saved = _record(p.cfg)
    assert saved["state"] == "expired" and saved["attempts"] == store.MAX_ATTEMPTS
    assert "frozen_result" not in saved and "payload" not in saved
    assert len(p.calls) == paid and _rows() == ([], [])
    assert processor._transport_pending_writes == {}
