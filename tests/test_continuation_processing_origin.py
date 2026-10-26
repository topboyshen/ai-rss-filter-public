"""Actual entry points retain origin while sharing a durable scoring budget."""

import copy
import json
import os
import re
import subprocess
import sys
import textwrap
import threading
from concurrent.futures import ThreadPoolExecutor
from html import escape
from pathlib import Path

import httpx
import pytest
from feedparser import FeedParserDict
from openai import APITimeoutError

from core import processor
from db import database
from services import natural_score_continuations as store
from services import natural_scoring, news_processing_trace as tracing, refresh
from tests.test_news_processing_trace import (
    FEED, LINK, TITLE, _response, _table, pipeline,  # noqa: F401
)
from tests.test_scorer import POLLUTED_TECH_EXAMPLE
from tests.test_webhook_durable_spool import isolated_spool  # noqa: F401

FIRST_ROUND = "1" * 32
SECOND_ROUND = "2" * 32


def _entry(link=LINK):
    return FeedParserDict(title=TITLE, link=link, summary="Publisher demand context")


def _run(p, link=LINK, *, persist=False):
    return processor.process_entry(
        _entry(link), [], "prompt", p.cfg, scorer=p.scorer,
        feed_url=FEED, persist_deferred=persist,
    )


def _record(p):
    with store.locked_record(p.cfg["scoring_strategy_version"], LINK) as record:
        return copy.deepcopy(record.data) if record is not None else None


def _make_due(p):
    with store.locked_record(p.cfg["scoring_strategy_version"], LINK) as record:
        data = record.data
        data["next_attempt_at"] = data["created_at"]
        record.save(data)


def _defer(p):
    p.cfg.setdefault("similarity_window_hours", 72)
    def respond(name, _kwargs):
        if name == "primary":
            return _response(POLLUTED_TECH_EXAMPLE)
        raise APITimeoutError(request=httpx.Request("POST", "https://backup.invalid"))

    p.control["respond"] = respond
    with tracing.processing_origin("natural_feed", FIRST_ROUND):
        assert _run(p, persist=True) == (None, processor.SCORE_DEFERRED_REASON)
    assert _record(p)["attempts"] == 1
    assert len(p.requests) == 2


def _resume(p):
    def run_entry(entry, recent, prompt, cfg, **kwargs):
        return processor.process_entry(entry, recent, prompt, cfg, scorer=p.scorer, **kwargs)

    return natural_scoring.resume_continuations(p.cfg, run_entry=run_entry)


def _configure_refresh(monkeypatch, p, entries):
    p.cfg.update({
        "cache_ttl": 60, "prompts": {"test": "prompt"},
        "feeds": [{"url": FEED, "name": "Synthetic publisher", "prompt": "test"}],
    })

    def rss_bytes(*_args, **_kwargs):
        items = "".join(
            f"<item><title>{escape(item.title)}</title><link>{escape(item.link)}</link>"
            f"<description>{escape(item.summary)}</description></item>" for item in entries
        )
        return ("<?xml version='1.0'?><rss version='2.0'><channel><title>publisher</title>"
                + items + "</channel></rss>").encode()

    monkeypatch.setattr(processor, "Scorer", lambda: p.scorer)
    monkeypatch.setattr(processor, "find_similar_article", lambda *_a, **_kw: (None, 0))
    monkeypatch.setattr(processor, "_load_feed_bytes", rss_bytes)
    monkeypatch.setattr(processor, "resolve_scoring_contract", lambda cfg, **_kw: cfg)
    monkeypatch.setattr(refresh, "resolve_scoring_contract", lambda cfg, **_kw: cfg)
    monkeypatch.setattr(refresh, "_cfg", lambda: p.cfg)
    monkeypatch.setattr(refresh, "get_metrics", lambda: None)
    monkeypatch.setattr(refresh, "flush_stats", lambda *_a, **_kw: None)
    monkeypatch.setattr(refresh, "cleanup_old_entries", lambda *_a, **_kw: None)
    monkeypatch.setattr(refresh, "set_cached", lambda *_a, **_kw: None)


def _configure_webhook(monkeypatch, p, webhook):
    monkeypatch.setattr(webhook, "Scorer", lambda: p.scorer)
    monkeypatch.setattr(webhook, "_cfg", lambda: p.cfg)
    monkeypatch.setattr(webhook, "resolve_scoring_contract", lambda cfg, **_kw: cfg)
    monkeypatch.setattr(webhook, "resolve_prompt", lambda *_a: ("test", "prompt"))
    monkeypatch.setattr(webhook, "_queue_committed_dashboard_inputs", lambda: None)
    return {"feed_url": FEED, "entries": [{"title": TITLE, "url": LINK, "content": "Publisher demand context"}]}


def _assert_paid_chain(row, p, *, expected_calls):
    trace = database.DB.get_news_processing_trace(row["processing_id"])
    assert row["model_attempts"] == len(trace["calls"]) == expected_calls
    assert {call["processing_id"] for call in trace["calls"]} == {row["processing_id"]}
    assert {call["article_key"] for call in trace["calls"]} == {row["article_key"]}
    assert {call["strategy_version"] for call in trace["calls"]} == {p.cfg["scoring_strategy_version"]}
    signal, = [signal for signal in _table("backtest_signals") if signal["signal_key"] == row["signal_key"]]
    assert (row["link"], row["strategy_version"], row["score"], row["score_path"], row["aggregation_selected"]) == (
        signal["link"], signal["strategy_version"], signal["score"], signal["score_path"], signal["selected"],
    )
    assert row["signal_association"] == "written_in_processing"


def test_real_refresh_links_opening_and_feed_to_one_round_and_next_round_is_distinct(monkeypatch, pipeline):  # noqa: F811
    p = pipeline
    _defer(p)
    _make_due(p)
    p.control["respond"] = lambda *_a: _response()
    current = [_entry(LINK + "/new")]
    _configure_refresh(monkeypatch, p, current)
    first = refresh._execute_feed_refresh()
    assert first["status"] == "success" and first["score_continuations"]["completed"] == 1
    assert re.fullmatch(r"[0-9a-f]{32}", first["refresh_id"])
    persisted = json.loads(database.DB.get_app_meta("last_refresh_result"))
    assert persisted["refresh_id"] == first["refresh_id"]
    opening, ordinary = _table("news_processing_logs")[-2:]
    assert opening["trigger_source"] == "natural_continuation"
    assert ordinary["trigger_source"] == "natural_feed"
    assert opening["refresh_id"] == ordinary["refresh_id"] == first["refresh_id"]
    assert opening["continuation_key"] == opening["article_key"]
    assert opening["continuation_attempt"] == 2
    assert ordinary["continuation_key"] == "" and ordinary["continuation_attempt"] is None
    _assert_paid_chain(opening, p, expected_calls=1)
    _assert_paid_chain(ordinary, p, expected_calls=1)
    second = refresh._execute_feed_refresh()
    assert second["refresh_id"] != first["refresh_id"]
    reused = _table("news_processing_logs")[-1]
    assert reused["trigger_source"] == "natural_feed" and reused["refresh_id"] == second["refresh_id"]
    assert reused["model_attempts"] == 0 and reused["reuse_kind"] == "cache"
    assert len(p.requests) == 4


@pytest.mark.parametrize("winner", ["opening", "webhook"])
def test_concurrent_opening_webhook_and_feed_keep_actual_origins_with_one_score(
    monkeypatch, pipeline, isolated_spool, winner,  # noqa: F811
):
    p, webhook = pipeline, isolated_spool
    _defer(p)
    _make_due(p)
    task = _configure_webhook(monkeypatch, p, webhook)
    entered, release = threading.Event(), threading.Event()

    def respond(*_args):
        entered.set()
        assert release.wait(4)
        return _response()

    p.control["respond"] = respond

    def opening():
        with tracing.processing_origin("natural_feed", SECOND_ROUND):
            return _resume(p)

    def feed():
        with tracing.processing_origin("natural_feed", SECOND_ROUND):
            return _run(p, persist=True)

    first, second = (opening, lambda: webhook._score_in_background(task)) if winner == "opening" else (
        lambda: webhook._score_in_background(task), opening,
    )
    with ThreadPoolExecutor(max_workers=3) as pool:
        one = pool.submit(first)
        assert entered.wait(4)
        two, three = pool.submit(second), pool.submit(feed)
        release.set()
        results = [one.result(8), two.result(8), three.result(8)]
    assert all(result is not False for result in results)
    rows = _table("news_processing_logs")[1:]
    expected_sources = {"natural_feed", "webhook"}
    if winner == "opening":
        expected_sources.add("natural_continuation")
    else:
        # Webhook already reserved the journal, moving its due time forward;
        # the opening sweep must not invent a processing execution for it.
        assert results[1]["attempted"] == 0
    assert len(rows) == len(expected_sources)
    assert {row["trigger_source"] for row in rows} == expected_sources
    paid, = [row for row in rows if row["model_attempts"]]
    assert paid["trigger_source"] == ("natural_continuation" if winner == "opening" else "webhook")
    assert paid["continuation_key"] == paid["article_key"] and paid["continuation_attempt"] == 2
    _assert_paid_chain(paid, p, expected_calls=1)
    for row in rows:
        assert row["refresh_id"] == ("" if row["trigger_source"] == "webhook" else SECOND_ROUND)
        assert row["continuation_key"] == paid["article_key"] and row["continuation_attempt"] == 2
        if row is not paid:
            assert row["model_attempts"] == 0 and row["signal_association"] == "existing_at_completion"
    assert _record(p)["attempts"] == 2 and _record(p)["state"] == "completed"
    assert len(p.requests) == len(_table("llm_call_logs")) == 3
    assert len(_table("news_cache")) == len(_table("backtest_signals")) == 1


def test_new_process_recovers_journal_with_new_round_and_preserved_budget(pipeline):  # noqa: F811
    p = pipeline
    _defer(p)
    _make_due(p)
    before = _record(p)
    configuration = {key: value for key, value in p.cfg.items() if key != "_blacklist_re"}
    child = textwrap.dedent("""
        import json, re, sys
        from core import processor
        from core.scorer import Scorer
        from db import database
        from services import natural_scoring, news_processing_trace as tracing
        from tests.test_news_processing_trace import _backend, _response
        cfg = json.loads(sys.argv[1])
        cfg['_blacklist_re'] = re.compile(r'$^')
        database.init_db()
        processor.map_article_to_assets = lambda *_a, **_kw: {}
        processor.send_telegram_notification = lambda *_a, **_kw: None
        scorer = Scorer()
        scorer.backends = [_backend('primary', lambda **_kwargs: _response())]
        def run(entry, recent, prompt, cfg, **kwargs):
            return processor.process_entry(entry, recent, prompt, cfg, scorer=scorer, **kwargs)
        with tracing.processing_origin('natural_feed', '22222222222222222222222222222222'):
            result = natural_scoring.resume_continuations(cfg, run_entry=run)
        print(json.dumps(result))
    """)
    completed = subprocess.run(
        [sys.executable, "-B", "-c", child, json.dumps(configuration)],
        cwd=Path(__file__).resolve().parents[1], env=os.environ.copy(),
        capture_output=True, text=True, timeout=20, check=False,
    )
    assert completed.returncode == 0, completed.stderr
    assert json.loads(completed.stdout.strip().splitlines()[-1])["completed"] == 1, completed.stderr
    row = _table("news_processing_logs")[-1]
    assert row["trigger_source"] == "natural_continuation" and row["refresh_id"] == SECOND_ROUND
    assert row["continuation_key"] == before["article_key"] and row["continuation_attempt"] == 2
    after = _record(p)
    assert after["created_at"] == before["created_at"] and after["attempts"] == 2
    assert after["state"] == "completed"
    _assert_paid_chain(row, p, expected_calls=1)


def test_duplicate_webhook_reuses_completed_score_without_inheriting_refresh(monkeypatch, pipeline, isolated_spool):  # noqa: F811
    p, webhook = pipeline, isolated_spool
    _defer(p)
    _make_due(p)
    p.control["respond"] = lambda *_a: _response()
    with tracing.processing_origin("natural_feed", SECOND_ROUND):
        assert _resume(p)["completed"] == 1
    before, journal = copy.deepcopy(_table("backtest_signals")), _record(p)
    task = _configure_webhook(monkeypatch, p, webhook)
    # The actual webhook entry point must clear even an ambient refresh scope.
    with tracing.processing_origin("natural_feed", SECOND_ROUND):
        for _ in range(2):
            assert webhook._score_in_background(task) is True
    for row in _table("news_processing_logs")[-2:]:
        assert row["trigger_source"] == "webhook" and row["refresh_id"] == ""
        assert row["continuation_key"] == row["article_key"] and row["continuation_attempt"] == 2
        assert row["model_attempts"] == 0 and row["reuse_kind"] == "cache"
        assert row["signal_association"] == "existing_at_completion"
        assert database.DB.get_news_processing_trace(row["processing_id"])["calls"] == []
    assert len(p.requests) == 3 and _record(p) == journal
    assert _table("backtest_signals") == before


def test_direct_resume_has_no_invented_refresh_and_nested_origin_is_restored(pipeline):  # noqa: F811
    p = pipeline
    _defer(p)
    _make_due(p)
    p.control["respond"] = lambda *_a: _response()
    assert _resume(p)["completed"] == 1
    row = _table("news_processing_logs")[-1]
    assert row["trigger_source"] == "natural_continuation" and row["refresh_id"] == ""
    with tracing.processing_origin("natural_feed", FIRST_ROUND):
        with pytest.raises(RuntimeError, match="synthetic interruption"):
            with tracing.processing_origin("webhook"):
                raise RuntimeError("synthetic interruption")
        assert _run(p)[1] == "cache"
    row = _table("news_processing_logs")[-1]
    assert row["trigger_source"] == "natural_feed" and row["refresh_id"] == FIRST_ROUND
    assert _run(p)[1] == "cache"
    row = _table("news_processing_logs")[-1]
    assert row["trigger_source"] == "unknown" and row["refresh_id"] == ""


def test_unavailable_continuation_diagnostics_preserves_score_and_signal(monkeypatch, pipeline):  # noqa: F811
    p = pipeline

    def unavailable():
        raise RuntimeError("synthetic diagnostic failure")

    monkeypatch.setattr(natural_scoring, "current_processing_metadata", unavailable)
    with tracing.processing_origin("natural_feed", FIRST_ROUND):
        entry, reason = _run(p)
    assert reason == "scored" and entry is not None
    signal, = _table("backtest_signals")
    assert signal["score"] == 8.0 and len(p.requests) == 1
    assert len(_table("llm_call_logs")) == 1
    assert signal["link"] == LINK and signal["strategy_version"] == p.cfg["scoring_strategy_version"]
    # Existing best-effort tracing may drop diagnostics; it cannot alter scoring.
    assert _table("news_processing_logs") == []
