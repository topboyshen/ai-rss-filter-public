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
from db.score_quality import read_score_quality
from scoring_provenance import (
    INSUFFICIENT_INPUT_EXPLANATIONS,
    SEMANTIC_RESOLUTION_INSUFFICIENT_INPUT,
    is_insufficient_input_fields,
)
from utils.helpers import FeedEntry

FEED = "https://feeds.example.test/insufficient"
STRATEGY = "insufficient-input-test"
SENTINEL = (0.0, "信息不足", "缺少可识别事件", "⚖️")


def _score_text(fmt, *, score=0, tag="信息不足", logic=SENTINEL[2], sentiment="中性"):
    if fmt == "named":
        return f"分数: {score}\n标签: {tag}\n逻辑: {logic}\n情绪: {sentiment}"
    if fmt == "brackets":
        return f"[{score}]\n[{tag}]\n[{logic}]\n[{sentiment}]"
    return json.dumps(dict(score=score, tag=tag, logic=logic, sentiment=sentiment), ensure_ascii=False)


@pytest.mark.parametrize("fmt", ["json", "named", "brackets"])
@pytest.mark.parametrize("logic", INSUFFICIENT_INPUT_EXPLANATIONS)
def test_parser_accepts_insufficient_sentinel(fmt, logic):
    parsed, detail = Scorer._parse_score_result_with_detail(_score_text(fmt, logic=logic))
    assert parsed == (0.0, "信息不足", logic, "⚖️")
    assert detail == ""
    assert is_insufficient_input_fields(*parsed)


@pytest.mark.parametrize("fmt", ["json", "named", "brackets"])
@pytest.mark.parametrize("fields", [
    {"logic": "公司营业收入增长99%"},
    {"logic": "缺少可识别事件（公司营业收入增长99%）"},
    {"logic": "缺少可识别事件1"},
    {"score": 4},
    {"sentiment": "看空"},
])
def test_parser_rejects_malformed_reserved_result(fmt, fields):
    parsed, detail = Scorer._parse_score_result_with_detail(_score_text(fmt, **fields))
    assert parsed[0] == -1
    assert detail == "invalid_insufficient_input"


def test_ordinary_zero_score_keeps_existing_contract():
    parsed, detail = Scorer._parse_score_result_with_detail(
        _score_text("json", tag="常规行情", logic="仅有指数点位")
    )
    assert parsed == (0.0, "常规行情", "仅有指数点位", "⚖️")
    assert detail == ""
    assert not is_insufficient_input_fields(*parsed)


@pytest.mark.parametrize("fmt", ["json", "named", "brackets"])
def test_parser_requires_an_exact_zero_for_insufficient_input(fmt):
    if fmt == "json":
        payload = _score_text(fmt).replace('"score": 0', '"score": 1e-400')
    else:
        payload = _score_text(fmt, score="0." + "0" * 400 + "1")
    parsed, detail = Scorer._parse_score_result_with_detail(payload)
    assert parsed[0] == -1
    assert detail == "invalid_insufficient_input"


def test_json_parser_rejects_out_of_range_integer():
    parsed, detail = Scorer._parse_score_result_with_detail(_score_text("json", score=10**400))
    assert parsed[0] == -1
    assert detail == "invalid_score"


def test_writer_rejects_freeform_insufficient_explanation():
    database.init_db()
    with pytest.raises(ValueError, match="insufficient-input tuple"):
        database.set_cached_entry(
            "https://example.test/invalid-insufficient", "栏目名", "栏目名", 0,
            "信息不足", "公司营业收入增长99%", score_path="insufficient_input",
            backtest_signal={"score_path": "insufficient_input"},
        )


def test_ledger_rejects_an_underflowing_nonzero_score():
    database.init_db()
    with pytest.raises(ValueError, match="insufficient-input tuple"):
        database.store_backtest_signal({
            "link": "https://example.test/invalid-insufficient",
            "score_path": "insufficient_input",
            "score": "1e-400",
            "aggregation_threshold": 6,
            "event_type": SENTINEL[1],
            "logic": SENTINEL[2],
            "direction": "中性",
            "selected": False,
        })


def test_scorer_marks_insufficient_sentinel_without_grounding(monkeypatch):
    database.init_db()
    monkeypatch.setattr(Scorer, "_instance", None)
    backend = LLMBackend("fixture", "", "https://invalid.test", "fixture-model")
    monkeypatch.setattr(backend, "client", object())
    monkeypatch.setattr(Scorer, "backends_for", lambda self, *a, **kw: [backend])
    monkeypatch.setattr(
        Scorer,
        "_call_structured_llm",
        lambda *_a, **_kw: _score_text("json"),
    )
    result = Scorer().score_stage1_with_identity(
        "A股午评来了！",
        "",
        "prompt",
        {
            "scoring_strategy_version": STRATEGY,
            "llm_score_cache": {"enabled": False},
            "llm_semantic_repair": {"enabled": False},
        },
    )
    assert result.values == SENTINEL
    assert result.semantic_resolution == SEMANTIC_RESOLUTION_INSUFFICIENT_INPUT
    assert result.input_status == "insufficient"
    assert result.model_name == "fixture-model"
    [observation] = read_score_quality(24, STRATEGY)["validation"]
    assert observation["contract_valid"] == observation["accepted"] == 1
    assert observation["reason"] == "insufficient_input"
    assert observation["grounding_observed"] == 0
    assert observation["source_grounded"] is None


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
    def __init__(self, stage):
        self.stage = stage
        self.calls = []
        self.summarize = Mock(side_effect=AssertionError("must not summarize"))

    @staticmethod
    def _insufficient_result():
        return ScoringResult(
            SENTINEL,
            "fake-model",
            SEMANTIC_RESOLUTION_INSUFFICIENT_INPUT,
            "insufficient",
        )

    def score_stage1_with_identity(self, *_args, **_kwargs):
        self.calls.append("stage1")
        if self.stage == 1:
            return self._insufficient_result()
        return ScoringResult((7.0, "公司公告", "新增订单", "📈"), "fake-model")

    def score_stage2_with_identity(self, *_args, **_kwargs):
        assert self.stage == 2, "insufficient stage1 must skip stage2"
        self.calls.append("stage2")
        return self._insufficient_result()


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


@pytest.mark.parametrize(("stage", "score_path"), [
    (1, "insufficient_input"),
    (2, "stage2_insufficient_input"),
])
def test_processor_persists_insufficient_input_as_filtered_and_unselected(monkeypatch, stage, score_path):
    database.init_db()
    distribution = Mock()
    notify = Mock()
    monkeypatch.setattr(processor.DB, "record_score", distribution)
    monkeypatch.setattr(processor, "send_telegram_notification", notify)
    scorer = _InsufficientScorer(stage)
    entry = FeedEntry("A股午评来了！", "https://example.test/insufficient/1")
    cfg = _cfg()
    cfg["two_stage_scoring"]["enabled"] = True

    assert processor.process_entry(
        entry, [], "prompt", cfg, scorer=scorer, feed_url=FEED
    ) == (None, "filtered")
    assert scorer.calls == (["stage1"] if stage == 1 else ["stage1", "stage2"])
    assert entry.scoring_meta["score_path"] == score_path
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
        assert cache["score_path"] == ledger["score_path"] == score_path
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
