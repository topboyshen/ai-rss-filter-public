"""A holding match is attention metadata, never a buy signal or model bypass."""

import re
import time
from types import SimpleNamespace

import pytest

from core import processor
from db import database
from scoring_explanation import EXPLANATION_FIELDS
from scoring_provenance import SAFE_NEUTRAL_CONTRACT_VALUES
from utils.helpers import FeedEntry


def _holdings_config(form="tier", code="990002"):
    if form == "legacy":
        return {
            "enabled": True,
            "skip_llm": True,
            "watchlist": [code],
            "watchlist_score": 8.5,
            "watchlist_tag": "持仓命中",
        }
    return {
        "enabled": True,
        "skip_llm": True,
        "tiers": [
            {
                "name": "watchlist",
                "score": 8.5,
                "tag": "持仓命中",
                "logic": "持仓股代码命中",
                "skip_llm": True,
                "keywords": [code],
            }
        ],
    }


def _outcome(score=3.0, *, model="model-one", sentiment="⚖️", resolution=""):
    return SimpleNamespace(
        values=SAFE_NEUTRAL_CONTRACT_VALUES
        if resolution
        else (
            score,
            "经营变化",
            "固定来源披露的经营变化",
            sentiment,
        ),
        model_name=model,
        semantic_resolution=resolution,
    )


class FixedHoldingScorer:
    def __init__(self, stage1=None, stage2=None):
        self.stage1 = stage1 or _outcome()
        self.stage2 = stage2
        self.calls = []

    def _resolve(self, stage):
        self.calls.append(stage)
        outcome = getattr(self, stage)
        if isinstance(outcome, Exception):
            raise outcome
        assert outcome is not None, f"unexpected model call: {stage}"
        return outcome

    def score_stage1_with_identity(self, *_args, **_kwargs):
        return self._resolve("stage1")

    def score_stage2_with_identity(self, *_args, **_kwargs):
        return self._resolve("stage2")

    def _fallback_local_score(self, *_args, **_kwargs):
        self.calls.append("fallback")
        return 2.0, "本地保守", "无法取得后端结果", "⚖️"


@pytest.fixture
def holding_pipeline(monkeypatch):
    database.init_db()
    monkeypatch.setattr(processor, "find_similar_article", lambda *_a, **_k: (None, 0))
    monkeypatch.setattr(
        processor,
        "map_article_to_assets",
        lambda _article: {
            "mapped_theme": "",
            "security_candidates": [],
            "market_references": [],
        },
    )
    monkeypatch.setattr(processor, "get_mapping_version", lambda: "holding-test")
    monkeypatch.setattr(
        processor,
        "send_telegram_notification",
        lambda *_a, **_k: pytest.fail("holding alone must not send a notification"),
    )
    cfg = {
        "_blacklist_re": re.compile(r"$^"),
        "threshold": {"pass": 7.0, "notify": 99.0, "similarity": 0.9},
        "scoring_strategy_version": "holding-hint:test",
        "few_shot_feedback": {"enabled": False},
        "generate_summary": False,
        "fast_pass": _holdings_config(),
        "feeds": [{"url": "holding-feed", "aggregation_threshold": 7.0}],
    }
    return SimpleNamespace(cfg=cfg, scorer=FixedHoldingScorer())


def _process(state, title="990002公告经营变化"):
    entry = FeedEntry(title, "https://example.com/holding-news", "公告中的原始证据")
    entry.published_parsed = time.gmtime(time.time() - 60)
    result, reason = processor.process_entry(
        entry,
        [],
        "fixed prompt",
        state.cfg,
        scorer=state.scorer,
        feed_url="holding-feed",
    )
    with database.get_connection() as conn:
        cache = dict(
            conn.execute(
                "SELECT * FROM news_cache WHERE link=?", (entry.link,)
            ).fetchone()
        )
        ledger = dict(
            conn.execute(
                "SELECT * FROM backtest_signals WHERE link=?", (entry.link,)
            ).fetchone()
        )
        assert conn.execute("SELECT COUNT(*) FROM backtest_signals").fetchone()[0] == 1
    return entry, result, reason, cache, ledger


def _assert_diagnostics(entry, cache, ledger, *, raw, delta=None, hint=8.5):
    expected = {
        "raw_score": raw,
        "score_adjustment": 0.0,
        "score_margin": raw - 7.0,
        "stage1_stage2_delta": delta,
        "fast_pass_hint_score": hint,
        "score_threshold": 7.0,
    }
    for source in (entry.scoring_meta, cache, ledger):
        assert {field: source[field] for field in EXPLANATION_FIELDS} == expected


@pytest.mark.parametrize("form", ["tier", "legacy"])
@pytest.mark.parametrize("code", ["990001", "990002", "990003"])
def test_current_and_legacy_holdings_never_bypass_even_with_explicit_true(form, code):
    match = processor._match_fast_pass(
        f"{code}公告利润下降", {"fast_pass": _holdings_config(form, code)}
    )

    assert match is not None
    assert match[0] == 8.5
    assert match[1] == "持仓命中"
    assert match[3:] == ("⚖️", False)


@pytest.mark.parametrize("form", ["tier", "legacy"])
@pytest.mark.parametrize(
    "title", ["19900029成交量", "9900021万元", "1990002亿元", "A990002Z编号"]
)
def test_holding_code_does_not_match_inside_long_numbers_or_unrelated_ids(form, title):
    assert (
        processor._match_fast_pass(title, {"fast_pass": _holdings_config(form)}) is None
    )


@pytest.mark.parametrize("configured", ["990002", "SZ990002", "990002.SZ"])
@pytest.mark.parametrize("title", ["公司990002公告", "SZ990002公告", "990002.SZ公告"])
def test_supported_exchange_wrappers_still_produce_only_a_hint(configured, title):
    match = processor._match_fast_pass(
        title, {"fast_pass": _holdings_config(code=configured)}
    )
    assert match is not None and match[3:] == ("⚖️", False)


@pytest.mark.parametrize("form", ["tier", "legacy"])
@pytest.mark.parametrize(("sentiment", "direction"), [("⚖️", "中性"), ("📉", "看空")])
def test_low_and_negative_holdings_keep_model_score_and_direction(
    holding_pipeline, form, sentiment, direction
):
    state = holding_pipeline
    state.cfg["fast_pass"] = _holdings_config(form)
    state.scorer = FixedHoldingScorer(_outcome(3.0, sentiment=sentiment))

    entry, result, reason, cache, ledger = _process(state)

    assert result is None and reason == "filtered"
    assert state.scorer.calls == ["stage1"]
    assert cache["score"] == ledger["score"] == 3.0
    assert cache["score_path"] == ledger["score_path"] == "llm"
    assert cache["direction"] == ledger["direction"] == direction
    assert ledger["selected"] == 0
    _assert_diagnostics(entry, cache, ledger, raw=3.0)


@pytest.mark.parametrize(
    ("first", "second", "direction"),
    [(8.0, 5.0, "看空"), (2.0, 3.0, "中性"), (7.0, 7.5, "中性")],
)
def test_holding_hint_uses_normal_two_stage_flow_and_real_signed_delta(
    holding_pipeline, first, second, direction
):
    state = holding_pipeline
    state.cfg["two_stage_scoring"] = {"enabled": True}
    sentiment = "📉" if direction == "看空" else "⚖️"
    state.scorer = FixedHoldingScorer(
        _outcome(first),
        _outcome(second, model="model-two", sentiment=sentiment),
    )

    entry, result, reason, cache, ledger = _process(state)

    assert state.scorer.calls == ["stage1", "stage2"]
    assert result is (entry if second >= 7.0 else None)
    assert reason == ("scored" if second >= 7.0 else "filtered")
    assert cache["score"] == ledger["score"] == second
    assert cache["score_path"] == ledger["score_path"] == "stage2"
    assert cache["direction"] == ledger["direction"] == direction
    assert "fast_pass_hint" in cache["review_reason"]
    _assert_diagnostics(entry, cache, ledger, raw=second, delta=second - first)


@pytest.mark.parametrize("stage", ["stage1", "stage2"])
@pytest.mark.parametrize(
    "resolution", ["safe_neutral_contract", "safe_neutral_grounding"]
)
def test_safe_neutral_holding_is_never_promoted_by_hint(
    holding_pipeline, stage, resolution
):
    state = holding_pipeline
    state.cfg["two_stage_scoring"] = {"enabled": True}
    neutral = _outcome(
        model="model-two" if stage == "stage2" else "model-one", resolution=resolution
    )
    state.scorer = FixedHoldingScorer(
        neutral if stage == "stage1" else _outcome(8), neutral
    )

    entry, result, reason, cache, ledger = _process(state)

    assert result is None and reason == "filtered"
    assert state.scorer.calls == (
        ["stage1"] if stage == "stage1" else ["stage1", "stage2"]
    )
    assert cache["score"] == ledger["score"] == 4.0
    assert cache["direction"] == ledger["direction"] == "中性"
    assert (
        cache["score_path"]
        == ledger["score_path"]
        == f"{'llm' if stage == 'stage1' else stage}_{resolution}"
    )
    assert ledger["selected"] == 0
    for source in (entry.scoring_meta, cache, ledger):
        assert all(source[field] is None for field in EXPLANATION_FIELDS)


def test_fallback_after_holding_review_cannot_claim_a_model_delta(holding_pipeline):
    state = holding_pipeline
    state.cfg["two_stage_scoring"] = {"enabled": True}
    state.scorer = FixedHoldingScorer(_outcome(8), _outcome(3, model="fallback_local"))

    entry, result, reason, cache, ledger = _process(state)

    assert result is None and reason == "filtered"
    assert state.scorer.calls == ["stage1", "stage2"]
    assert cache["score"] == ledger["score"] == 3.0
    assert cache["score_path"] == ledger["score_path"] == "fallback_local"
    _assert_diagnostics(entry, cache, ledger, raw=3.0)


@pytest.mark.parametrize("tier", [False, True])
def test_ordinary_keywords_without_explicit_skip_use_model_hint_by_default(
    holding_pipeline, tier
):
    state = holding_pipeline
    rule = {"score": 8.5, "keywords": ["政策安排"], "tag": "政策提示"}
    state.cfg["fast_pass"] = {
        "enabled": True,
        **({"tiers": [rule], "skip_llm": True} if tier else rule),
    }
    state.scorer = FixedHoldingScorer(_outcome(3.0))

    entry, result, reason, cache, ledger = _process(state, "今日政策安排")

    assert result is None and reason == "filtered"
    assert state.scorer.calls == ["stage1"]
    assert cache["score"] == ledger["score"] == 3.0
    assert cache["score_path"] == "llm"
    _assert_diagnostics(entry, cache, ledger, raw=3.0)


@pytest.mark.parametrize("tier", [False, True])
def test_explicit_nonholding_rule_still_bypasses_models(holding_pipeline, tier):
    state = holding_pipeline
    rule = {"score": 9.0, "keywords": ["明确降准"], "tag": "宏观政策", "skip_llm": True}
    state.cfg["fast_pass"] = {"enabled": True, **({"tiers": [rule]} if tier else rule)}

    entry, result, reason, cache, ledger = _process(state, "央行明确降准")

    assert result is entry and reason == "fast_pass"
    assert state.scorer.calls == []
    assert cache["score"] == ledger["score"] == 9.0
    assert cache["score_path"] == ledger["score_path"] == "fast_pass"
    _assert_diagnostics(entry, cache, ledger, raw=9.0, hint=9.0)


def test_long_numeric_article_has_no_holding_hint_or_unnecessary_stage2(
    holding_pipeline,
):
    state = holding_pipeline
    state.cfg["two_stage_scoring"] = {"enabled": True}
    state.scorer = FixedHoldingScorer(_outcome(3.0))

    entry, result, reason, cache, ledger = _process(state, "成交金额为19900029万元")

    assert result is None and reason == "filtered"
    assert state.scorer.calls == ["stage1"]
    assert cache["score_path"] == ledger["score_path"] == "llm"
    _assert_diagnostics(entry, cache, ledger, raw=3.0, hint=None)
