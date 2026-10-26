"""A completed transport-neutral decision survives failed writes without new LLM calls."""

import sqlite3
import threading
from collections import OrderedDict
from concurrent.futures import ThreadPoolExecutor

import httpx
import pytest
from openai import APITimeoutError

from core import processor
from core.scorer import Scorer
from db import database
from tests.test_scorer import (
    POLLUTED_TECH_EXAMPLE,
    _test_backend,
    _valid_quality_circuit_response,
)
from utils.helpers import FeedEntry

FEED = "https://feed.invalid/transport-persist"
LINK = "https://article.invalid/transport-persist"
TITLE = "company reports stronger demand"


@pytest.fixture
def pipeline(monkeypatch):
    database.init_db()
    monkeypatch.setattr(processor, "_transport_retry_counts", OrderedDict())
    monkeypatch.setattr(processor, "_transport_pending_writes", {})
    monkeypatch.setattr(processor, "map_article_to_assets", lambda *_a, **_kw: {})
    monkeypatch.setattr(processor, "send_telegram_notification", lambda *_a, **_kw: None)
    scorer = Scorer()
    scorer.clear_runtime_cache()
    monkeypatch.setattr(scorer, "backends", [
        _test_backend("stage1"), _test_backend("first"), _test_backend("second"),
    ])
    calls, control = [], {"recover": False}

    def fake_transport(backend, messages, **_kwargs):
        calls.append(backend.name)
        if backend.name == "stage1" or control["recover"]:
            return _valid_quality_circuit_response()
        if backend.name == "first":
            return POLLUTED_TECH_EXAMPLE
        raise APITimeoutError(request=httpx.Request("POST", "https://second.invalid/v1"))

    monkeypatch.setattr(scorer, "_call_llm", fake_transport)
    cfg = {
        "scoring_strategy_version": "transport-persist-test",
        "llm_semantic_repair": {"enabled": True},
        "llm_score_cache": {"enabled": True, "ttl_seconds": 3600},
        "llm_routing": {"stage1": ["first", "second"], "stage2": ["first", "second"]},
        "threshold": {"pass": 7.0, "notify": 9.0, "similarity": 0.6},
        "_blacklist_re": None,
        "cache_rejected_entries": True,
        "generate_summary": False,
        "fast_pass": {"enabled": False},
        "two_stage_scoring": {"enabled": False},
        "feeds": [{"url": FEED, "tier": "primary_news"}],
    }

    def run(link=LINK, title=TITLE):
        return processor.process_entry(
            FeedEntry(title, link), [], "prompt", cfg, scorer=scorer, feed_url=FEED,
        )

    yield cfg, calls, control, run
    scorer.clear_runtime_cache()


def _rows(link=LINK):
    with database.get_connection() as conn:
        cache = conn.execute("SELECT * FROM news_cache WHERE link=?", (link,)).fetchall()
        signals = conn.execute("SELECT * FROM backtest_signals WHERE link=?", (link,)).fetchall()
    return cache, signals


def _defer_twice(run):
    for _ in range(2):
        assert run() == (None, processor.SCORE_DEFERRED_REASON)
        assert _rows() == ([], [])


@pytest.mark.parametrize("stage2", [False, True])
@pytest.mark.parametrize("cache_rejected", [False, True])
def test_terminal_neutral_retries_only_write_and_retains_decision(
    monkeypatch, pipeline, stage2, cache_rejected,
):
    cfg, calls, _control, run = pipeline
    cfg["cache_rejected_entries"] = cache_rejected
    if stage2:
        cfg["llm_routing"]["stage1"] = ["stage1"]
        cfg["two_stage_scoring"] = {"enabled": True}
    write_name = "set_cached_entry" if cache_rejected else "store_backtest_signal"
    real_write = getattr(processor.DB, write_name)
    writes = []

    def locked_then_write(*args, **kwargs):
        writes.append((args, kwargs))
        if len(writes) <= 2:
            raise sqlite3.OperationalError("database is locked")
        return real_write(*args, **kwargs)

    monkeypatch.setattr(processor.DB, write_name, locked_then_write)
    _defer_twice(run)
    with pytest.raises(sqlite3.OperationalError, match="locked"):
        run()
    paid = len(calls)
    assert paid == (7 if stage2 else 6)
    with pytest.raises(sqlite3.OperationalError, match="locked"):
        run(title="changed source while the original decision awaits its commit")
    assert len(calls) == paid
    assert run() == (None, "filtered")
    assert len(calls) == paid
    cache, signals = _rows()
    assert len(cache) == int(cache_rejected) and len(signals) == 1
    signal = dict(signals[0])
    written = writes[-1][1]["backtest_signal"] if cache_rejected else writes[-1][0][0]
    assert written["raw_title"] == TITLE
    if cache_rejected:
        assert dict(cache[0])["raw_title"] == TITLE
    assert signal["score"] == 4.0 and signal["selected"] == 0
    assert signal["score_path"] == (
        "stage2_safe_neutral_grounding" if stage2 else "llm_safe_neutral_grounding"
    )
    assert processor._transport_retry_counts == {}
    assert processor._transport_pending_writes == {}
    assert run() == (None, "filtered") and len(calls) == paid


def test_failed_write_does_not_backdate_availability(monkeypatch, pipeline):
    _cfg, calls, _control, run = pipeline
    real_write = processor.DB.set_cached_entry
    _defer_twice(run)
    clock = [1792000000]
    monkeypatch.setattr(processor.time, "time", lambda: clock[0])
    monkeypatch.setattr(
        processor.DB, "set_cached_entry",
        lambda *_a, **_kw: (_ for _ in ()).throw(sqlite3.OperationalError("locked")),
    )
    with pytest.raises(sqlite3.OperationalError):
        run()
    clock[0] += 60
    monkeypatch.setattr(processor.DB, "set_cached_entry", real_write)
    assert run() == (None, "filtered")
    signal = dict(_rows()[1][0])
    assert signal["scored_at"] == 1792000000
    assert signal["available_at"] == 1792000060
    assert len(calls) == 6


def test_commit_succeeded_but_ack_failed_reuses_frozen_ledger(monkeypatch, pipeline):
    _cfg, calls, _control, run = pipeline
    real_write = processor.DB.set_cached_entry

    def committed_then_failed(*args, **kwargs):
        real_write(*args, **kwargs)
        raise sqlite3.OperationalError("lost acknowledgement after commit")

    _defer_twice(run)
    monkeypatch.setattr(processor.DB, "set_cached_entry", committed_then_failed)
    with pytest.raises(sqlite3.OperationalError, match="acknowledgement"):
        run()
    ledger_before = tuple(_rows()[1][0])
    assert run() == (None, "filtered")
    assert len(calls) == 6
    assert len(_rows()[1]) == 1 and tuple(_rows()[1][0]) == ledger_before
    assert processor._transport_retry_counts == {}
    assert processor._transport_pending_writes == {}


def test_full_registry_preserves_pending_write_and_defers_new_link_before_model(
    monkeypatch, pipeline,
):
    _cfg, calls, _control, run = pipeline
    real_write = processor.DB.set_cached_entry
    monkeypatch.setattr(processor, "_TRANSPORT_RETRY_TRACKED_MAX", 1)
    _defer_twice(run)
    monkeypatch.setattr(
        processor.DB, "set_cached_entry",
        lambda *_a, **_kw: (_ for _ in ()).throw(sqlite3.OperationalError("locked")),
    )
    with pytest.raises(sqlite3.OperationalError):
        run()
    assert run(link=LINK + "/new") == (None, processor.SCORE_DEFERRED_REASON)
    assert len(calls) == 6
    assert len(processor._transport_retry_counts) == len(processor._transport_pending_writes) == 1
    monkeypatch.setattr(processor.DB, "set_cached_entry", real_write)
    assert run() == (None, "filtered") and len(calls) == 6
    assert run(link=LINK + "/new") == (None, processor.SCORE_DEFERRED_REASON)
    assert len(calls) == 8 and len(processor._transport_retry_counts) == 1


def test_recovered_backend_keeps_its_model_result_and_releases_retry_slot(pipeline):
    _cfg, calls, control, run = pipeline
    assert run() == (None, processor.SCORE_DEFERRED_REASON)
    control["recover"] = True
    entry, reason = run()
    assert reason == "scored" and entry.scoring_meta["model_name"] == "first-model"
    assert len(calls) == 3
    assert dict(_rows()[1][0])["score"] == 8.0
    assert processor._transport_retry_counts == {}
    assert processor._transport_pending_writes == {}


def test_similarity_commit_releases_retry_slot_for_an_independent_link(monkeypatch, pipeline):
    cfg, calls, control, run = pipeline
    monkeypatch.setattr(processor, "_TRANSPORT_RETRY_TRACKED_MAX", 1)
    assert run() == (None, processor.SCORE_DEFERRED_REASON)
    assert len(processor._transport_retry_counts) == 1
    recent = [(LINK + "/source", TITLE, TITLE, 8.0, "demand", TITLE, "neutral")]
    assert processor.process_entry(
        FeedEntry(TITLE, LINK), recent, "prompt", cfg, feed_url=FEED,
    ) == (None, "similarity")
    cache, signals = _rows()
    assert len(cache) == len(signals) == 1
    assert dict(signals[0])["score_path"] == "similarity"
    assert processor._transport_retry_counts == {}
    assert processor._transport_pending_writes == {}
    assert len(calls) == 2
    control["recover"] = True
    entry, reason = run(link=LINK + "/independent")
    assert reason == "scored" and entry.scoring_meta["model_name"] == "first-model"
    assert len(calls) == 3


def test_same_link_pending_commit_is_single_flight(monkeypatch, pipeline):
    _cfg, calls, _control, run = pipeline
    real_write = processor.DB.set_cached_entry
    _defer_twice(run)
    monkeypatch.setattr(
        processor.DB, "set_cached_entry",
        lambda *_a, **_kw: (_ for _ in ()).throw(sqlite3.OperationalError("locked")),
    )
    with pytest.raises(sqlite3.OperationalError):
        run()
    entered, release = threading.Event(), threading.Event()
    writes = []

    def paused_commit(*args, **kwargs):
        writes.append(True)
        entered.set()
        assert release.wait(2)
        return real_write(*args, **kwargs)

    monkeypatch.setattr(processor.DB, "set_cached_entry", paused_commit)
    with ThreadPoolExecutor(max_workers=2) as pool:
        first = pool.submit(run)
        assert entered.wait(2)
        second = pool.submit(run)
        release.set()
        assert first.result(timeout=2) == second.result(timeout=2) == (None, "filtered")
    assert writes == [True] and len(calls) == 6
    assert len(_rows()[0]) == len(_rows()[1]) == 1
