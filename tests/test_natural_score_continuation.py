"""Natural retry journals retain real scoring inputs and bounded model budgets.

Only the transport boundary is fake. Processor, scorer, validation, cache,
ledger, processing diagnostics and continuation journal use their actual APIs;
the suite-wide fixture redirects SQLite and private journal files to tmp_path.
"""

import copy
import re
import threading
import time
from collections import OrderedDict
from concurrent.futures import ThreadPoolExecutor
from html import escape
from types import SimpleNamespace

import feedparser
import httpx
import pytest
from openai import APITimeoutError

from core import processor
from core.scorer import Scorer
from db import database
from services import natural_score_continuations as store
from services import natural_scoring
from tests.test_scorer import (
    POLLUTED_TECH_EXAMPLE,
    _test_backend,
    _valid_quality_circuit_response,
)

FEED = "https://feed.invalid/natural-continuation"
OTHER_FEED = "https://feed.invalid/natural-continuation-secondary"
LINK = "https://article.invalid/natural-continuation"
TITLE = "company reports stronger demand"
SUMMARY = "Original publisher context retained for the unfinished decision."
PROMPT = "Use the original publisher demand context."


def _entry(link=LINK, title=TITLE, summary=SUMMARY):
    return feedparser.FeedParserDict(title=title, link=link, summary=summary)


def _rows(link=LINK):
    with database.get_connection() as connection:
        return tuple([
            dict(row) for row in connection.execute(
                "SELECT * FROM " + table + " WHERE link=?", (link,),
            )
        ] for table in ("news_cache", "backtest_signals"))


def _record(cfg, link=LINK):
    with store.locked_record(cfg["scoring_strategy_version"], link) as record:
        return copy.deepcopy(record.data) if record is not None else None


def _make_due(cfg, link=LINK):
    with store.locked_record(cfg["scoring_strategy_version"], link) as record:
        assert record is not None
        data = record.data
        data["next_attempt_at"] = data["created_at"]
        record.save(data)


@pytest.fixture
def pipeline(monkeypatch):
    database.init_db()
    monkeypatch.setattr(processor, "_transport_retry_counts", OrderedDict())
    monkeypatch.setattr(processor, "_transport_pending_writes", {})
    monkeypatch.setattr(processor, "map_article_to_assets", lambda *_a, **_kw: {})
    monkeypatch.setattr(processor, "send_telegram_notification", lambda *_a, **_kw: None)
    scorer = Scorer()
    scorer.clear_runtime_cache()
    monkeypatch.setattr(scorer, "backends", [_test_backend("first"), _test_backend("second")])
    # A synthetic 21-record batch is about journal limits, not the unrelated
    # backend's five-timeout circuit; the dedicated scorer tests cover that.
    for backend in scorer.backends:
        backend.max_fail_count = 100
    calls, control = [], {"mode": "unverdicted", "pause": None, "errors": []}

    def transport(backend, messages, **_kwargs):
        calls.append({"backend": backend.name, "messages": copy.deepcopy(messages)})
        if control["pause"] is not None:
            entered, release = control["pause"]
            entered.set()
            assert release.wait(3)
        if control["mode"] == "recovered":
            return _valid_quality_circuit_response()
        if backend.name == "first":
            return POLLUTED_TECH_EXAMPLE
        if control["mode"] == "fallback_recovered":
            return _valid_quality_circuit_response()
        raise APITimeoutError(request=httpx.Request("POST", "https://second.invalid/v1"))

    monkeypatch.setattr(scorer, "_call_llm", transport)
    cfg = {
        "scoring_strategy_version": "natural-continuation-test",
        "llm_semantic_repair": {"enabled": True},
        "llm_score_cache": {"enabled": True, "ttl_seconds": 3600},
        "llm_routing": {"stage1": ["first", "second"], "stage2": ["first", "second"]},
        "threshold": {"pass": 7.0, "notify": 9.0, "similarity": 0.6},
        "_blacklist_re": None,
        "cache_rejected_entries": True,
        "generate_summary": False,
        "fast_pass": {"enabled": False},
        "two_stage_scoring": {"enabled": False},
        "max_entries": 5,
        "similarity_window_hours": 72,
        "feeds": [{"url": FEED, "tier": "primary_news"},
                  {"url": OTHER_FEED, "tier": "primary_news"}],
    }

    def run(link=LINK, title=TITLE, summary=SUMMARY, *, feed=FEED, prompt=PROMPT, persist=True):
        return processor.process_entry(
            _entry(link, title, summary), [], prompt, cfg,
            scorer=scorer, feed_url=feed, persist_deferred=persist,
        )

    def run_entry(entry, recent_entries, prompt_text, configuration, **kwargs):
        kwargs["scorer"] = scorer
        try:
            return processor.process_entry(entry, recent_entries, prompt_text, configuration, **kwargs)
        except Exception as error:
            control["errors"].append(error)
            raise

    def resume(configuration=None):
        return natural_scoring.resume_continuations(configuration or cfg, run_entry=run_entry)

    yield SimpleNamespace(cfg=cfg, scorer=scorer, calls=calls, control=control, run=run, resume=resume)
    scorer.clear_runtime_cache()


def test_natural_deferred_article_is_resumed_after_sliding_out_of_feed_top_five(monkeypatch, pipeline):
    p = pipeline
    assert p.run() == (None, processor.SCORE_DEFERRED_REASON)
    assert len(p.calls) == 2 and _rows() == ([], [])
    data = _record(p.cfg)
    assert data["state"] == "pending" and data["attempts"] == 1
    assert data["payload"]["title"] == TITLE
    assert data["payload"]["summary"] == SUMMARY
    assert data["payload"]["prompt"] == PROMPT
    assert data["payload"]["feed_url"] == FEED
    # This item is no longer among the publisher's current five candidates.
    latest = [_entry(LINK + f"/new-{number}") for number in range(5)]
    assert LINK not in [entry.link for entry in latest[:p.cfg["max_entries"]]]
    monkeypatch.setattr(
        processor, "fetch_feed", lambda *_a, **_kw: pytest.fail("resume must not fetch an RSS feed"),
    )
    _make_due(p.cfg)
    p.control["mode"] = "recovered"
    summary = p.resume()
    assert summary["attempted"] == summary["completed"] == 1, p.control["errors"]
    assert summary["deferred"] == summary["errors"] == 0
    cache, signals = _rows()
    assert len(cache) == len(signals) == 1
    assert cache[0]["raw_title"] == TITLE and signals[0]["score"] == 8.0
    assert len(p.calls) == 3 and _record(p.cfg)["state"] == "completed"
    assert store.pending(p.cfg["scoring_strategy_version"]) == []


def test_duplicate_natural_delivery_does_not_consume_an_immediate_extra_attempt(pipeline):
    p = pipeline
    entered, release = threading.Event(), threading.Event()
    p.control["pause"] = (entered, release)
    with ThreadPoolExecutor(max_workers=2) as pool:
        first = pool.submit(p.run)
        assert entered.wait(2)
        second = pool.submit(p.run)
        release.set()
        assert first.result(timeout=3) == second.result(timeout=3) == (None, processor.SCORE_DEFERRED_REASON)
    assert len(p.calls) == 2 and _rows() == ([], [])
    assert _record(p.cfg)["attempts"] == 1


def test_webhook_and_natural_resume_share_original_input_and_one_completion(pipeline):
    p = pipeline
    assert p.run() == (None, processor.SCORE_DEFERRED_REASON)
    _make_due(p.cfg)
    p.control["mode"] = "recovered"
    entered, release = threading.Event(), threading.Event()
    p.control["pause"] = (entered, release)
    with ThreadPoolExecutor(max_workers=2) as pool:
        webhook = pool.submit(
            p.run, title="different publisher claim must not replace the original input",
            summary="Different source summary", feed=OTHER_FEED,
            prompt="different prompt must not replace original", persist=False,
        )
        assert entered.wait(2)
        natural = pool.submit(p.resume)
        release.set()
        assert webhook.result(timeout=3)[1] != processor.SCORE_DEFERRED_REASON
        assert natural.result(timeout=3)["errors"] == 0
    assert len(p.calls) == 3
    sent = str(p.calls[-1]["messages"])
    assert TITLE in sent and SUMMARY in sent and PROMPT in sent
    assert "Different source summary" not in sent and "different prompt" not in sent
    cache, signals = _rows()
    assert len(cache) == len(signals) == 1
    assert cache[0]["raw_title"] == TITLE and signals[0]["feed_url"] == FEED
    assert _record(p.cfg)["state"] == "completed" and _record(p.cfg)["attempts"] == 2


def test_restart_does_not_reset_three_attempt_budget_and_terminal_neutral_is_written(pipeline):
    p = pipeline
    assert p.run() == (None, processor.SCORE_DEFERRED_REASON)
    for expected_attempts in (2, 3):
        processor._transport_retry_counts.clear()
        processor._transport_pending_writes.clear()
        p.scorer.clear_runtime_cache()
        _make_due(p.cfg)
        summary = p.resume()
        assert summary["attempted"] == 1 and summary["errors"] == 0
        assert _record(p.cfg)["attempts"] == expected_attempts
        if expected_attempts == 2:
            assert summary["deferred"] == 1 and _rows() == ([], [])
    cache, signals = _rows()
    assert len(cache) == len(signals) == 1 and len(p.calls) == 6
    assert signals[0]["score"] == 4.0 and signals[0]["selected"] == 0
    assert signals[0]["score_path"] == "llm_safe_neutral_grounding"
    assert _record(p.cfg)["state"] == "completed"
    processor._transport_retry_counts.clear()
    assert p.resume()["attempted"] == 0
    assert p.run(persist=False)[1] == "filtered" and len(p.calls) == 6


def test_crash_after_third_reservation_exhausts_without_another_model_call(pipeline):
    p = pipeline
    assert p.run() == (None, processor.SCORE_DEFERRED_REASON)
    with store.locked_record(p.cfg["scoring_strategy_version"], LINK) as record:
        data = record.data
        data.update(state="running", attempts=3, next_attempt_at=data["created_at"])
        record.save(data)
    processor._transport_retry_counts.clear()
    p.scorer.clear_runtime_cache()
    summary = p.resume()
    assert summary["attempted"] == summary["terminal_unscored"] == 1
    assert summary["deferred"] == summary["completed"] == summary["errors"] == 0
    assert len(p.calls) == 2 and _rows() == ([], [])
    assert _record(p.cfg)["state"] == "exhausted"
    assert p.resume()["attempted"] == 0


def test_recovered_fallback_preserves_real_verdict_instead_of_terminal_neutral(pipeline):
    p = pipeline
    assert p.run() == (None, processor.SCORE_DEFERRED_REASON)
    _make_due(p.cfg)
    p.control["mode"] = "fallback_recovered"
    summary = p.resume()
    assert summary["attempted"] == summary["completed"] == 1 and summary["errors"] == 0
    cache, signals = _rows()
    assert len(cache) == len(signals) == 1 and signals[0]["score"] == 8.0
    assert signals[0]["score_path"] == "llm" and len(p.calls) == 4
    assert _record(p.cfg)["state"] == "completed"


def test_completed_journal_and_duplicate_delivery_never_call_model_again(pipeline):
    p = pipeline
    assert p.run() == (None, processor.SCORE_DEFERRED_REASON)
    _make_due(p.cfg)
    p.control["mode"] = "recovered"
    assert p.resume()["completed"] == 1
    before = _rows()
    paid = len(p.calls)
    assert p.resume()["attempted"] == 0
    p.run(persist=False)
    assert len(p.calls) == paid and _rows() == before
    data = _record(p.cfg)
    assert data["state"] == "completed" and "payload" not in data


def test_webhook_only_deferral_does_not_start_a_natural_continuation(pipeline):
    p = pipeline
    assert p.run(persist=False) == (None, processor.SCORE_DEFERRED_REASON)
    assert len(p.calls) == 2 and _rows() == ([], [])
    assert _record(p.cfg) is None and not store.journal_directory().exists()
    assert p.resume()["attempted"] == 0


def test_changed_contract_never_resumes_original_journal_under_new_strategy(pipeline):
    p = pipeline
    assert p.run() == (None, processor.SCORE_DEFERRED_REASON)
    _make_due(p.cfg)
    changed = {**p.cfg, "scoring_strategy_version": "different-natural-contract"}
    summary = p.resume(changed)
    assert summary["attempted"] == summary["completed"] == summary["errors"] == 0
    assert len(p.calls) == 2 and _rows() == ([], [])
    assert _record(p.cfg)["state"] == "contract_changed"


def test_expired_journal_never_spends_another_model_attempt(monkeypatch, pipeline):
    p = pipeline
    assert p.run() == (None, processor.SCORE_DEFERRED_REASON)
    later = time.time() + store.TTL_SECONDS + 1
    # Patch each service's clock object, not Python's shared time module; the
    # database and test runner clocks retain their actual wall time.
    monkeypatch.setattr(store, "time", SimpleNamespace(time=lambda: later))
    monkeypatch.setattr(natural_scoring, "time", SimpleNamespace(time=lambda: later))
    summary = p.resume()
    assert summary["attempted"] == summary["completed"] == summary["errors"] == 0
    assert len(p.calls) == 2 and _rows() == ([], [])
    assert _record(p.cfg)["state"] == "expired"


def test_full_private_journal_stays_deferred_and_never_claims_a_completed_score(monkeypatch, pipeline):
    p = pipeline
    monkeypatch.setattr(store, "MAX_ACTIVE_RECORDS", 1)
    assert p.run() == (None, processor.SCORE_DEFERRED_REASON)
    other = LINK + "/cannot-enqueue"
    assert p.run(link=other) == (None, processor.SCORE_DEFERRED_REASON)
    assert _rows(other) == ([], []) and _record(p.cfg, other) is None
    assert _record(p.cfg)["state"] == "pending"


def test_admission_after_capacity_recovers_preserves_prior_attempts_across_restart(monkeypatch, pipeline):
    p = pipeline
    monkeypatch.setattr(store, "MAX_ACTIVE_RECORDS", 1)
    occupant = LINK + "/occupies-private-capacity"
    assert store.enqueue(p.cfg["scoring_strategy_version"], occupant, {
        "title": TITLE, "link": occupant, "summary": SUMMARY,
        "prompt": PROMPT, "feed_url": FEED, "few_shot_context": "",
    }) == "stored"
    assert p.run() == (None, processor.SCORE_DEFERRED_REASON)
    assert len(p.calls) == 2 and _rows() == ([], [])
    assert _record(p.cfg) is None  # Full admission is not durable success.
    with store.locked_record(p.cfg["scoring_strategy_version"], occupant) as record:
        data = record.data
        data["state"] = "completed"
        record.save(data)
    assert p.run() == (None, processor.SCORE_DEFERRED_REASON)
    assert len(p.calls) == 4 and _rows() == ([], [])
    assert _record(p.cfg)["state"] == "pending" and _record(p.cfg)["attempts"] == 2
    processor._transport_retry_counts.clear()
    processor._transport_pending_writes.clear()
    p.scorer.clear_runtime_cache()
    _make_due(p.cfg)
    summary = p.resume()
    assert summary["attempted"] == summary["completed"] == 1
    assert summary["deferred"] == summary["errors"] == 0
    cache, signals = _rows()
    assert len(cache) == len(signals) == 1 and len(p.calls) == 6
    assert signals[0]["score"] == 4.0 and signals[0]["selected"] == 0
    assert signals[0]["score_path"] == "llm_safe_neutral_grounding"
    assert _record(p.cfg)["state"] == "completed" and _record(p.cfg)["attempts"] == 3
    assert p.run(persist=False)[1] == "filtered" and p.resume()["attempted"] == 0
    assert len(p.calls) == 6 and len(_rows()[1]) == 1


def test_one_natural_resume_is_bounded_to_twenty_due_articles(pipeline):
    p = pipeline
    links = [LINK + f"/bounded-{number}" for number in range(21)]
    for link in links:
        assert p.run(link=link) == (None, processor.SCORE_DEFERRED_REASON)
        _make_due(p.cfg, link)
    p.control["mode"] = "recovered"
    summary = p.resume()
    assert summary["attempted"] == summary["completed"] == 20
    assert summary["deferred"] == summary["errors"] == 0
    assert sum(bool(_rows(link)[1]) for link in links) == 20
    assert len(store.pending(p.cfg["scoring_strategy_version"])) == 1
    assert p.resume()["completed"] == 1
    assert sum(bool(_rows(link)[1]) for link in links) == 21


def test_real_refresh_resumes_article_missing_from_next_feeds_first_five(monkeypatch, pipeline):
    from services import refresh

    p = pipeline
    cfg = p.cfg
    cfg.update({
        "_blacklist_re": re.compile(r"(?!)"), "cache_ttl": 60,
        "prompts": {"test": PROMPT},
        "feeds": [{"url": FEED, "name": "Synthetic publisher", "prompt": "test", "max_entries": 5}],
    })
    current_entries = [_entry()]

    def rss_bytes(*_args, **_kwargs):
        items = "".join(
            f"<item><title>{escape(item.title)}</title><link>{escape(item.link)}</link>"
            f"<description>{escape(item.summary)}</description></item>"
            for item in current_entries
        )
        return ("<?xml version='1.0'?><rss version='2.0'><channel><title>publisher</title>"
                + items + "</channel></rss>").encode()

    monkeypatch.setattr(processor, "Scorer", lambda: p.scorer)
    monkeypatch.setattr(processor, "_load_feed_bytes", rss_bytes)
    monkeypatch.setattr(processor, "resolve_scoring_contract", lambda configuration, **_kw: configuration)
    monkeypatch.setattr(refresh, "resolve_scoring_contract", lambda configuration, **_kw: configuration)
    monkeypatch.setattr(refresh, "_cfg", lambda: cfg)
    monkeypatch.setattr(refresh, "get_metrics", lambda: None)
    monkeypatch.setattr(refresh, "flush_stats", lambda *_a, **_kw: None)
    monkeypatch.setattr(refresh, "cleanup_old_entries", lambda *_a, **_kw: None)
    monkeypatch.setattr(refresh, "set_cached", lambda *_a, **_kw: None)
    first = refresh._execute_feed_refresh()
    preserved_first = copy.deepcopy(first)
    assert first["status"] == "partial" and first["score_deferred_entries"] == 1
    assert first["partial_sources"] == 1 and first["succeeded_sources"] == 0
    assert _record(cfg)["state"] == "pending" and _rows() == ([], [])
    assert len(p.calls) == 2
    current_entries[:] = [_entry(LINK + f"/new-{number}") for number in range(5)]
    assert LINK not in [item.link for item in current_entries[:5]]
    _make_due(cfg)
    p.control["mode"] = "recovered"
    second = refresh._execute_feed_refresh()
    assert second["status"] == "success" and second["succeeded_sources"] == 1
    assert second["score_continuations"]["completed"] == 1
    assert second["score_continuations"]["errors"] == 0
    assert second["score_deferred_entries"] == 0
    assert _record(cfg)["state"] == "completed"
    assert len(_rows()[0]) == len(_rows()[1]) == 1
    assert _rows()[1][0]["score"] == 8.0
    assert first == preserved_first and first["status"] == "partial"
