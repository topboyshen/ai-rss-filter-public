"""Semantic rejection + backup without verdict is retryable, uncached, bounded."""
import httpx
import pytest
from openai import APIConnectionError, APITimeoutError, InternalServerError

from core.scorer import Scorer
from scoring_provenance import SAFE_NEUTRAL_CONTRACT_VALUES
from tests.test_scorer import (
    POLLUTED_TECH_EXAMPLE, _test_backend, _valid_quality_circuit_response,
)

CFG = {
    "llm_semantic_repair": {"enabled": True},
    "llm_score_cache": {"enabled": True, "ttl_seconds": 3600},
    "llm_routing": {"stage1": ["first", "second"]},
}
_REQUEST = httpx.Request("POST", "https://second.invalid/v1/chat/completions")


def _timeout():
    return APITimeoutError(request=_REQUEST)


def _server_error():
    return InternalServerError(
        "upstream 500", response=httpx.Response(500, request=_REQUEST), body=None,
    )


def _connection_error():
    return APIConnectionError(request=_REQUEST)


@pytest.fixture
def scorer(monkeypatch):
    scorer = Scorer()
    scorer.clear_runtime_cache()
    monkeypatch.setattr(scorer, "backends", [_test_backend("first"), _test_backend("second")])
    writes = []
    real_set = scorer._set_cached_score

    def spy(key, value):
        writes.append(value)
        return real_set(key, value)

    monkeypatch.setattr(scorer, "_set_cached_score", spy)
    scorer.cache_writes = writes
    yield scorer
    scorer.clear_runtime_cache()


def _route(monkeypatch, scorer, first, second):
    calls = []

    def respond(backend, messages, **_kw):
        calls.append(backend.name)
        value = first if backend.name == "first" else second
        if isinstance(value, Exception):
            raise value
        return value

    monkeypatch.setattr(scorer, "_call_llm", respond)
    return calls


@pytest.mark.parametrize("failure", [_timeout, _server_error, _connection_error])
def test_reject_then_transport_failure_is_retryable_and_not_cached(monkeypatch, scorer, failure):
    calls = _route(monkeypatch, scorer, POLLUTED_TECH_EXAMPLE, failure())
    result = scorer.score_stage1_with_identity("company demand update", "reported demand", "prompt", CFG)
    assert calls == ["first", "second"]
    assert result.transport_unverdicted is True
    assert result.model_name == "local_safe_neutral"
    assert result.semantic_resolution == "safe_neutral_grounding"
    assert result.values == SAFE_NEUTRAL_CONTRACT_VALUES
    assert scorer.cache_writes == []
    # Not cached: a retry reaches both backends again (bounded by the caller).
    calls.clear()
    scorer.score_stage1_with_identity("company demand update", "reported demand", "prompt", CFG)
    assert calls == ["first", "second"]


def test_pure_semantic_rejection_stays_final_safe_neutral_and_cached(monkeypatch, scorer):
    calls = _route(monkeypatch, scorer, POLLUTED_TECH_EXAMPLE, POLLUTED_TECH_EXAMPLE)
    result = scorer.score_stage1_with_identity("company demand update", "reported demand", "prompt", CFG)
    assert calls == ["first", "second"]
    assert result.transport_unverdicted is False
    assert result.semantic_resolution == "safe_neutral_grounding"
    assert len(scorer.cache_writes) == 1
    calls.clear()
    again = scorer.score_stage1_with_identity("company demand update", "reported demand", "prompt", CFG)
    assert calls == [] and again.semantic_resolution == "safe_neutral_grounding"


def test_pure_transport_failure_keeps_local_fallback(monkeypatch, scorer):
    calls = _route(monkeypatch, scorer, _timeout(), _server_error())
    result = scorer.score_stage1_with_identity("company demand update", "reported demand", "prompt", CFG)
    assert calls == ["first", "second"]
    assert result.model_name == "fallback_local"
    assert result.semantic_resolution == ""
    assert result.transport_unverdicted is False


def test_reject_then_valid_backup_is_unchanged(monkeypatch, scorer):
    _route(monkeypatch, scorer, POLLUTED_TECH_EXAMPLE, _valid_quality_circuit_response())
    result = scorer.score_stage1_with_identity("company demand update", "reported demand", "prompt", CFG)
    assert result.model_name == "second-model"
    assert result.transport_unverdicted is False and result.semantic_resolution == ""


# --- processor: bounded retry, nothing persisted while retryable -------------

class _Entry:
    def __init__(self, link):
        self.title = "company demand update"
        self.link = link
        self.summary = "reported demand"
        self.description = ""


def test_processor_defers_twice_without_writes_then_persists_final(monkeypatch, scorer):
    from core import processor

    processor._transport_retry_counts.clear()
    calls = _route(monkeypatch, scorer, POLLUTED_TECH_EXAMPLE, _timeout())
    writes = []
    monkeypatch.setattr(processor, "_persist_filtered_entry",
                        lambda ctx, result, meta, scored_at: writes.append(result) or (None, "filtered"))
    monkeypatch.setattr(processor.DB, "record_score", lambda *_a, **_k: None)

    class Ctx:
        strategy_version = "v-test"
        quality_repair = False
        entry = _Entry("https://example.test/a")

    from core.processor import _EntryScore

    def one_round():
        result = _EntryScore()
        outcome = processor._score_stage_with_identity(
            scorer, "stage1", "company demand update", "reported demand", "prompt", CFG,
        )
        result.score_path = "llm_safe_neutral_grounding"
        result.transport_unverdicted = outcome.transport_unverdicted
        processor._defer_unverdicted_safe_neutral(Ctx, result)
        return result

    for attempt in (1, 2):
        with pytest.raises(processor.ScoringTransportRetryable) as raised:
            one_round()
        assert raised.value.attempt == attempt
    final = one_round()  # third round: cap reached; a successful write may clear it
    assert final.transport_unverdicted is True
    assert processor._transport_retry_counts[("v-test", "https://example.test/a")] == 3
    assert calls == ["first", "second"] * (processor.TRANSPORT_RETRY_MAX + 1)
    assert scorer.cache_writes == []


def test_processor_clears_counter_when_backup_recovers(monkeypatch, scorer):
    from core import processor
    from core.processor import _EntryScore

    processor._transport_retry_counts.clear()

    class Ctx:
        strategy_version = "v-test"
        quality_repair = False
        entry = _Entry("https://example.test/b")

    result = _EntryScore(score_path="llm_safe_neutral_grounding", transport_unverdicted=True)
    with pytest.raises(processor.ScoringTransportRetryable):
        processor._defer_unverdicted_safe_neutral(Ctx, result)
    recovered = _EntryScore(score_path="llm", transport_unverdicted=False)
    processor._defer_unverdicted_safe_neutral(Ctx, recovered)
    assert processor._transport_retry_counts == {}


def test_process_entry_persists_nothing_until_retry_budget_is_spent(monkeypatch, scorer):
    from core import processor
    from db import database
    from utils.helpers import FeedEntry

    database.init_db()
    processor._transport_retry_counts.clear()
    feed = "https://feeds.example.test/unverdicted"
    cfg = {
        **CFG,
        "llm_score_cache": {"enabled": False},
        "scoring_strategy_version": "unverdicted-retry",
        "threshold": {"pass": 7.0, "notify": 8.0, "similarity": 0.6},
        "similarity_window_hours": 72,
        "_blacklist_re": None,
        "cache_rejected_entries": True,
        "generate_summary": False,
        "local_reject_rules": {"enabled": False, "rules": []},
        "fast_pass": {"enabled": False},
        "two_stage_scoring": {"enabled": False},
        "feeds": [{"url": feed, "tier": "primary_news"}],
    }
    monkeypatch.setattr(processor, "map_article_to_assets", lambda *_a, **_kw: {})
    monkeypatch.setattr(processor, "send_telegram_notification", lambda *_a, **_kw: None)
    calls = _route(monkeypatch, scorer, POLLUTED_TECH_EXAMPLE, _timeout())
    link = "https://example.test/unverdicted/1"

    def rows():
        with database.get_connection() as conn:
            return (
                conn.execute("SELECT COUNT(*) FROM news_cache WHERE link=?", (link,)).fetchone()[0],
                conn.execute("SELECT COUNT(*) FROM backtest_signals WHERE link=?", (link,)).fetchone()[0],
            )

    for attempt in range(1, processor.TRANSPORT_RETRY_MAX + 1):
        entry = FeedEntry("公司发布日常经营信息", link)
        assert processor.process_entry(
            entry, [], "prompt", cfg, scorer=scorer, feed_url=feed,
        ) == (None, processor.SCORE_DEFERRED_REASON)
        assert rows() == (0, 0), f"round {attempt} must not persist anything"
    entry = FeedEntry("公司发布日常经营信息", link)
    assert processor.process_entry(entry, [], "prompt", cfg, scorer=scorer, feed_url=feed) == (
        None, "filtered",
    )
    assert rows() == (1, 1)
    assert entry.scoring_meta["model_name"] == "local_safe_neutral"
    assert len(calls) == 2 * (processor.TRANSPORT_RETRY_MAX + 1)
    # The final safe-neutral is now reused: no further paid calls.
    processor.process_entry(FeedEntry("公司发布日常经营信息", link), [], "prompt", cfg,
                            scorer=scorer, feed_url=feed)
    assert len(calls) == 2 * (processor.TRANSPORT_RETRY_MAX + 1)
