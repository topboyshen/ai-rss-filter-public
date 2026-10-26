import time

from db import database as database_module
from core.processor import _score_stage_with_identity
from core.scorer import ScoringResult
from scoring_provenance import SAFE_NEUTRAL_CONTRACT_VALUES


class _BoundScorer:
    last_backend_name = "qwen"

    def score_stage1_with_identity(self, *args, **kwargs):
        return ScoringResult(
            SAFE_NEUTRAL_CONTRACT_VALUES, "qwen", "safe_neutral_grounding", "facts",
        )


class _StaleScorer:
    last_backend_name = "qwen"

    def score_stage1_with_identity(self, *args, **kwargs):
        return ScoringResult((8.0, "宏观", "政策落地", "📈"), "qwen", "", "")


def test_bound_interface_propagates_the_final_neutral_reason():
    values, model, resolution, reason = _score_stage_with_identity(
        _BoundScorer(), "stage1", "t", "s", "p", {},
    )
    assert resolution == "safe_neutral_grounding"
    assert reason == "facts"
    assert values == SAFE_NEUTRAL_CONTRACT_VALUES

    _values, _model, plain_resolution, plain_reason = _score_stage_with_identity(
        _StaleScorer(), "stage1", "t", "s", "p", {},
    )
    assert (plain_resolution, plain_reason) == ("", "")


def test_exhausted_local_neutral_keeps_the_final_semantic_rejection_reason():
    from core.scorer import Scorer

    scorer = Scorer()
    facts = scorer._exhausted_local_neutral("ungrounded_score_facts", {}, "", "stage1")
    assert facts.model_name == "local_safe_neutral"
    assert facts.values == SAFE_NEUTRAL_CONTRACT_VALUES
    assert (facts.semantic_resolution, facts.neutral_reason) == (
        "safe_neutral_grounding", "facts")
    contract = scorer._exhausted_local_neutral("invalid_score_contract", {}, "", "stage2")
    assert contract.model_name == "local_safe_neutral"
    assert contract.values == SAFE_NEUTRAL_CONTRACT_VALUES
    assert (contract.semantic_resolution, contract.neutral_reason) == (
        "safe_neutral_contract", "contract")


def test_persisted_reason_is_readable_from_the_cache(tmp_path, monkeypatch):
    monkeypatch.setattr(database_module, "DB_PATH", str(tmp_path / "pipeline.db"))
    database_module.init_db()
    import db.database as database
    import sse_engine

    now = int(time.time())
    signal = {
        "link": "https://example.com/neutral",
        "raw_title": "标题",
        "score": 4,
        "score_path": "llm_safe_neutral_grounding",
        "direction": "中性",
        "score_confidence": 0.0,
        "strategy_version": "pipeline-test",
        "mapping_version": "asset-map:test",
        "available_at": now,
        "scored_at": now,
        "aggregation_threshold": 6,
        "tag": "其他",
        "logic": "来源未显示明确投资催化",
        "selected": 0,
        "stage1_score": 4.0,
        "stage1_model": "qwen",
    }
    database.set_cached_entry(
        "https://example.com/neutral",
        "标题",
        "标题",
        4.0,
        "其他",
        "来源未显示明确投资催化",
        score_path="llm_safe_neutral_grounding",
        strategy_version="pipeline-test",
        direction="中性",
        score_confidence=0.0,
        model_name="qwen",
        stage1_score=4.0,
        stage1_model="qwen",
        safe_neutral_reason="facts",
        created_at=now,
        backtest_signal=signal,
    )
    with database.get_connection() as conn:
        stored = conn.execute(
            "SELECT safe_neutral_reason FROM news_cache WHERE link = ?",
            ("https://example.com/neutral",),
        ).fetchone()[0]
    assert stored == "facts"

    entry = {
        "title": "标题", "link": "https://example.com/neutral", "score": 4.0,
        "importance_score": 4.0, "direction": "中性", "tag": "其他",
        "logic": "来源未显示明确投资催化",
        "score_path": "llm_safe_neutral_grounding",
        "safe_neutral_reason": stored, "time": now, "source": "测试源",
        "_selection_threshold": 6.0, "_realtime_section": "fresh_observe",
    }
    monkeypatch.setattr(
        "sse_engine.enrich_article_analysis", lambda article, *, trusted_only: article,
    )
    html = sse_engine._render_cards([entry])
    assert "保守处理说明：事实表述未通过现有来源校验" in html
