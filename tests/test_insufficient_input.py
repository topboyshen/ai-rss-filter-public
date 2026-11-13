"""Insufficient-input is a semantic sentinel, never a low score or a signal."""

import json
from unittest.mock import Mock

import pytest

from core import processor
from core.scorer import (
    LLMBackend,
    Scorer,
    ScoringResult,
    _score_grounding_verdict,
)
from db import database
from scoring_provenance import (
    SEMANTIC_RESOLUTION_INSUFFICIENT_INPUT,
    is_insufficient_input_fields,
)
from utils.helpers import FeedEntry

FEED = "https://feeds.example.test/insufficient"
STRATEGY = "insufficient-input-test"
SENTINEL = (0.0, "信息不足", "只有栏目名，缺少具体事件", "⚖️")


def test_parser_accepts_insufficient_sentinel():
    raw = json.dumps(
        {
            "score": 0,
            "tag": "信息不足",
            "logic": "只有栏目名，缺少具体事件",
            "sentiment": "中性",
        },
        ensure_ascii=False,
    )
    parsed, detail = Scorer._parse_score_result_with_detail(raw)
    assert parsed == SENTINEL
    assert detail == ""
    assert is_insufficient_input_fields(*parsed)


def test_scorer_marks_insufficient_sentinel_without_grounding(monkeypatch):
    monkeypatch.setattr(Scorer, "_instance", None)
    backend = LLMBackend("fixture", "", "https://invalid.test", "fixture-model")
    monkeypatch.setattr(backend, "client", object())
    monkeypatch.setattr(Scorer, "backends_for", lambda self, *a, **kw: [backend])
    monkeypatch.setattr(
        Scorer,
        "_call_structured_llm",
        lambda *_a, **_kw: json.dumps(
            {
                "score": 0,
                "tag": "信息不足",
                "logic": "只有栏目名，缺少具体事件",
                "sentiment": "中性",
            },
            ensure_ascii=False,
        ),
    )
    result = Scorer().score_stage1_with_identity(
        "A股午评来了！",
        "",
        "prompt",
        {
            "llm_score_cache": {"enabled": False},
            "llm_semantic_repair": {"enabled": False},
        },
    )
    assert result.values == SENTINEL
    assert result.semantic_resolution == SEMANTIC_RESOLUTION_INSUFFICIENT_INPUT
    assert result.input_status == "insufficient"
    assert result.model_name == "fixture-model"


def test_insufficient_result_exposes_input_status():
    result = ScoringResult(
        SENTINEL,
        "fake-model",
        SEMANTIC_RESOLUTION_INSUFFICIENT_INPUT,
        "insufficient",
    )
    assert result.input_status == "insufficient"
    assert (
        ScoringResult((7.0, "公司公告", "新增订单", "📈"), "fake-model").input_status
        == "ok"
    )


def test_insufficient_resolution_rejects_a_scored_tuple():
    with pytest.raises(ValueError, match="insufficient-input"):
        ScoringResult(
            (4.0, "信息不足", "只有栏目名", "⚖️"),
            "fake-model",
            SEMANTIC_RESOLUTION_INSUFFICIENT_INPUT,
            "insufficient",
        )


def test_insufficient_sentinel_skips_theme_and_fact_grounding():
    assert _score_grounding_verdict(SENTINEL, "A股午评来了！", "") == ("", "")


class _InsufficientScorer:
    def __init__(self):
        self.calls = []
        self.summarize = Mock(side_effect=AssertionError("must not summarize"))

    def score_stage1_with_identity(self, *_args, **_kwargs):
        self.calls.append("stage1")
        return ScoringResult(
            SENTINEL,
            "fake-model",
            SEMANTIC_RESOLUTION_INSUFFICIENT_INPUT,
            "insufficient",
        )

    def score_stage2_with_identity(self, *_args, **_kwargs):
        raise AssertionError("insufficient stage1 must skip stage2")


def _cfg():
    return {
        "scoring_strategy_version": STRATEGY,
        "threshold": {"pass": 6.0, "notify": 8.0, "similarity": 0.6},
        "similarity_window_hours": 72,
        "_blacklist_re": None,
        "cache_rejected_entries": True,
        "generate_summary": True,
        "summary_min_score": 1.0,
        "local_reject_rules": {"enabled": False, "rules": []},
        "fast_pass": {"enabled": False},
        "two_stage_scoring": {"enabled": False},
        "feeds": [{"url": FEED, "tier": "primary_news", "aggregation_threshold": 6.0}],
    }


def test_processor_persists_insufficient_input_as_filtered_and_unselected(monkeypatch):
    database.init_db()
    distribution = Mock()
    notify = Mock()
    monkeypatch.setattr(processor.DB, "record_score", distribution)
    monkeypatch.setattr(processor, "send_telegram_notification", notify)
    scorer = _InsufficientScorer()
    entry = FeedEntry("A股午评来了！", "https://example.test/insufficient/1")

    assert processor.process_entry(
        entry, [], "prompt", _cfg(), scorer=scorer, feed_url=FEED
    ) == (None, "filtered")
    assert scorer.calls == ["stage1"]
    assert entry.scoring_meta["score_path"] == "insufficient_input"
    assert entry.scoring_meta["safe_neutral_reason"] == "insufficient"
    assert entry.scoring_meta["score_confidence"] == 0.0
    distribution.assert_not_called()
    notify.assert_not_called()
    scorer.summarize.assert_not_called()

    with database.get_connection() as conn:
        cache = conn.execute(
            "SELECT * FROM news_cache WHERE link = ?", (entry.link,)
        ).fetchone()
        ledger = conn.execute(
            "SELECT * FROM backtest_signals WHERE link = ?", (entry.link,)
        ).fetchone()
        assert cache is not None and ledger is not None
        assert cache["score"] == ledger["score"] == 0.0
        assert cache["tag"] == "信息不足"
        assert cache["logic"] == SENTINEL[2]
        assert cache["score_path"] == ledger["score_path"] == "insufficient_input"
        assert cache["score_confidence"] == ledger["score_confidence"] == 0.0
        assert ledger["selected"] == 0
        assert (
            conn.execute("SELECT COUNT(*) FROM integration_signal_outbox").fetchone()[0]
            == 0
        )

    assert database.get_recent_entries(cfg=_cfg()) == []
    assert (
        database.get_entries_for_cached_rss(_cfg(), feed_url=FEED, min_score=1.0) == []
    )
