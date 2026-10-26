"""Lock scoring behavior while carrying explanations through real persistence."""

import re
import time
from types import SimpleNamespace

import pytest

from core import processor
from db import database
from scoring_provenance import SAFE_NEUTRAL_CONTRACT_VALUES
from utils.helpers import FeedEntry


EXPLANATION_FIELDS = (
    "raw_score",
    "score_adjustment",
    "score_margin",
    "stage1_stage2_delta",
    "fast_pass_hint_score",
    "score_threshold",
)


def _outcome(score=8.0, model="test-stage1", resolution="", tag="宏观政策"):
    values = (
        SAFE_NEUTRAL_CONTRACT_VALUES
        if resolution
        else (score, tag, "固定新闻政策影响传导", "📈")
    )
    return SimpleNamespace(
        values=values, model_name=model, semantic_resolution=resolution
    )


class FixedScorer:
    """Exercise the existing bound-score adapter without making network calls."""

    def __init__(self, stage1=None, stage2=None):
        self.stage1 = stage1 if stage1 is not None else _outcome()
        self.stage2 = stage2
        self.calls = []

    def _resolve(self, stage):
        self.calls.append(stage)
        result = getattr(self, stage)
        if isinstance(result, Exception):
            raise result
        assert result is not None, f"unexpected {stage} invocation"
        return result

    def score_stage1_with_identity(self, *_args, **_kwargs):
        return self._resolve("stage1")

    def score_stage2_with_identity(self, *_args, **_kwargs):
        return self._resolve("stage2")

    def _fallback_local_score(self, *_args, **_kwargs):
        self.calls.append("fallback")
        return 3.0, "本地保守", "后端传输失败后的固定本地结果", "⚖️"


@pytest.fixture
def pipeline(monkeypatch):
    database.init_db()
    monkeypatch.setattr(processor, "find_similar_article", lambda *_a, **_k: (None, 0))
    monkeypatch.setattr(
        processor,
        "map_article_to_assets",
        lambda _article: {
            "mapped_theme": "",
            "backtest_symbol": None,
            "mapping_confidence": 0.0,
            "security_candidates": [],
            "market_references": [],
        },
    )
    monkeypatch.setattr(processor, "get_mapping_version", lambda: "mapping:test")
    monkeypatch.setattr(
        processor,
        "send_telegram_notification",
        lambda *_a, **_k: pytest.fail("fixture must not send a notification"),
    )
    cfg = {
        "_blacklist_re": re.compile(r"$^"),
        "threshold": {"pass": 6.0, "notify": 99.0, "similarity": 0.9},
        "scoring_strategy_version": "explanation:fixed-contract",
        "few_shot_feedback": {"enabled": False},
        "generate_summary": False,
        "feeds": [
            {"url": "feed-test", "pass_threshold": 7.0, "aggregation_threshold": 9.0}
        ],
    }
    return SimpleNamespace(cfg=cfg, scorer=FixedScorer())


def _entry():
    entry = FeedEntry("固定新闻政策安排", "https://example.com/explanation", "来源摘要")
    entry.published_parsed = time.gmtime(time.time() - 60)
    return entry


def _process(pipeline, entry=None, **kwargs):
    entry = entry or _entry()
    result, reason = processor.process_entry(
        entry,
        [],
        "fixed prompt",
        pipeline.cfg,
        scorer=pipeline.scorer,
        feed_url="feed-test",
        **kwargs,
    )
    return entry, result, reason


def _persisted_rows(link):
    with database.get_connection() as conn:
        cache = conn.execute(
            "SELECT * FROM news_cache WHERE link = ?", (link,)
        ).fetchone()
        signals = conn.execute(
            "SELECT * FROM backtest_signals WHERE link = ?", (link,)
        ).fetchall()
    assert cache is not None
    assert len(signals) == 1
    return dict(cache), dict(signals[0])


def _assert_explanation(entry, expected):
    cache, signal = _persisted_rows(entry.link)
    sources = {"entry": entry.scoring_meta, "cache": cache, "ledger": signal}
    for name, source in sources.items():
        assert {key: source[key] for key in EXPLANATION_FIELDS} == expected, name


def _expected(
    raw=None, adjustment=None, margin=None, delta=None, hint=None, threshold=7.0
):
    return dict(
        zip(EXPLANATION_FIELDS, (raw, adjustment, margin, delta, hint, threshold))
    )


def _enable_stage2(pipeline):
    pipeline.cfg["two_stage_scoring"] = {"enabled": True}


@pytest.mark.parametrize(
    ("case", "score", "path", "reason", "confidence", "calls"),
    [
        ("llm", 8.0, "llm", "scored", 1 / 3, ["stage1"]),
        ("stage2", 9.0, "stage2", "scored", 2 / 3, ["stage1", "stage2"]),
        ("cap_time", 5.0, "llm", "filtered", 2 / 3, ["stage1"]),
        ("fast_pass", 9.0, "fast_pass", "fast_pass", 0.0, []),
        ("local_rule", 2.0, "local_reject", "filtered", 0.0, []),
        ("neutral", 4.0, "llm_safe_neutral_contract", "filtered", 0.0, ["stage1"]),
        ("parse_failure", -1.0, "parse_error", "filtered", 0.0, ["stage1"]),
        (
            "transport_failure",
            3.0,
            "fallback_local",
            "filtered",
            0.0,
            ["stage1", "fallback"],
        ),
    ],
)
def test_legacy_score_path_selection_and_confidence_do_not_change(
    pipeline, case, score, path, reason, confidence, calls
):
    if case == "stage2":
        _enable_stage2(pipeline)
        pipeline.scorer = FixedScorer(_outcome(7), _outcome(9, "test-stage2"))
    elif case == "cap_time":
        pipeline.scorer = FixedScorer(_outcome(9, tag="无相关标签"))
        pipeline.cfg["time_weight"] = {"enabled": True, "rules": [{"bonus": 1.0}]}
    elif case == "fast_pass":
        pipeline.cfg["fast_pass"] = {
            "enabled": True,
            "keywords": ["固定新闻"],
            "score": 9.0,
            "skip_llm": True,
        }
    elif case == "local_rule":
        pipeline.cfg["local_reject_rules"] = {
            "enabled": True,
            "rules": [{"pattern": "固定新闻", "score": 2.0}],
        }
    elif case == "neutral":
        pipeline.scorer = FixedScorer(_outcome(resolution="safe_neutral_contract"))
    elif case == "parse_failure":
        pipeline.scorer = FixedScorer(ValueError("invalid model tuple"))
    elif case == "transport_failure":
        pipeline.scorer = FixedScorer(TimeoutError("model request timed out"))

    entry, result, actual_reason = _process(pipeline)
    cache, ledger = _persisted_rows(entry.link)

    assert actual_reason == reason
    assert result is (None if reason == "filtered" else entry)
    assert pipeline.scorer.calls == calls
    assert cache["score"] == ledger["score"] == score
    assert cache["score_path"] == ledger["score_path"] == path
    assert cache["score_confidence"] == pytest.approx(confidence)
    assert ledger["score_confidence"] == pytest.approx(confidence)
    assert ledger["aggregation_threshold"] == 9.0
    assert bool(ledger["selected"]) == (score >= 9.0)


def test_llm_explanation_uses_source_pass_threshold_and_no_unmatched_hint(pipeline):
    pipeline.cfg["fast_pass"] = {
        "enabled": True,
        "keywords": ["不会命中的词"],
        "score": 9,
    }

    entry, result, reason = _process(pipeline)

    assert result is entry and reason == "scored"
    _assert_explanation(entry, _expected(raw=8.0, adjustment=0.0, margin=1.0))
    assert entry.scoring_meta["score_confidence"] == pytest.approx(1 / 3)


def test_stage2_explanation_preserves_signed_difference_between_model_outputs(pipeline):
    _enable_stage2(pipeline)
    pipeline.scorer = FixedScorer(_outcome(8), _outcome(7, "test-stage2"))

    entry, result, reason = _process(pipeline)

    assert result is entry and reason == "scored"
    assert entry.scoring_meta["score_path"] == "stage2"
    _assert_explanation(
        entry, _expected(raw=7.0, adjustment=0.0, margin=0.0, delta=-1.0)
    )


def test_cap_then_time_adjustment_uses_raw_before_both_and_keeps_old_confidence(
    pipeline,
):
    pipeline.scorer = FixedScorer(_outcome(9, tag="无相关标签"))
    pipeline.cfg["time_weight"] = {"enabled": True, "rules": [{"bonus": 1.0}]}

    entry, result, reason = _process(pipeline)

    assert result is None and reason == "filtered"
    _assert_explanation(entry, _expected(raw=9.0, adjustment=-4.0, margin=-2.0))
    assert entry.scoring_meta["score_confidence"] == pytest.approx(2 / 3)


@pytest.mark.parametrize(
    ("skip_llm", "raw", "path", "reason", "calls"),
    [
        (False, 8.0, "llm", "scored", ["stage1"]),
        (True, 9.0, "fast_pass", "fast_pass", []),
    ],
)
def test_fast_pass_hint_is_explanatory_and_existing_skip_rule_is_unchanged(
    pipeline, skip_llm, raw, path, reason, calls
):
    pipeline.cfg["fast_pass"] = {
        "enabled": True,
        "keywords": ["固定新闻"],
        "score": 9.0,
        "skip_llm": skip_llm,
    }

    entry, result, actual_reason = _process(pipeline)

    assert result is entry and actual_reason == reason
    assert pipeline.scorer.calls == calls
    assert entry.scoring_meta["score_path"] == path
    _assert_explanation(
        entry, _expected(raw=raw, adjustment=0.0, margin=raw - 7.0, hint=9.0)
    )


def test_explicit_local_rule_records_rule_output_without_fabricating_llm_delta(
    pipeline,
):
    pipeline.cfg["local_reject_rules"] = {
        "enabled": True,
        "rules": [{"pattern": "固定新闻", "score": 2.0}],
    }

    entry, result, reason = _process(pipeline)

    assert result is None and reason == "filtered"
    assert pipeline.scorer.calls == []
    assert entry.scoring_meta["score_path"] == "local_reject"
    _assert_explanation(entry, _expected(raw=2.0, adjustment=0.0, margin=-5.0))


@pytest.mark.parametrize("stage", ["stage1", "stage2"])
@pytest.mark.parametrize(
    "resolution", ["safe_neutral_contract", "safe_neutral_grounding"]
)
def test_neutral_paths_never_claim_numeric_explanations(pipeline, stage, resolution):
    neutral = _outcome(model=f"test-{stage}", resolution=resolution)
    pipeline.scorer = FixedScorer(neutral)
    if stage == "stage2":
        _enable_stage2(pipeline)
        pipeline.scorer = FixedScorer(_outcome(8), neutral)

    entry, result, reason = _process(pipeline)

    assert result is None and reason == "filtered"
    assert (
        entry.scoring_meta["score_path"]
        == f"{'llm' if stage == 'stage1' else stage}_{resolution}"
    )
    assert entry.scoring_meta["score_confidence"] == 0
    _assert_explanation(entry, _expected(threshold=None))


@pytest.mark.parametrize("stage1_fallback", [False, True])
def test_fallback_stage_is_not_a_model_to_model_delta(pipeline, stage1_fallback):
    _enable_stage2(pipeline)
    pipeline.scorer = FixedScorer(
        _outcome(8, "fallback_local" if stage1_fallback else "test-stage1"),
        _outcome(7, "test-stage2" if stage1_fallback else "fallback_local"),
    )

    entry, result, reason = _process(pipeline)

    assert result is entry and reason == "scored"
    expected_path = "stage2" if stage1_fallback else "fallback_local"
    assert entry.scoring_meta["score_path"] == expected_path
    _assert_explanation(entry, _expected(raw=7.0, adjustment=0.0, margin=0.0))


def test_invalid_stage1_sentinel_cannot_become_delta_after_hint_triggers_stage2(
    pipeline,
):
    _enable_stage2(pipeline)
    pipeline.cfg["fast_pass"] = {
        "enabled": True,
        "keywords": ["固定新闻"],
        "score": 9.0,
        "skip_llm": False,
    }
    pipeline.scorer = FixedScorer(_outcome(-1), _outcome(7, "test-stage2"))

    entry, result, reason = _process(pipeline)

    assert result is entry and reason == "scored"
    assert pipeline.scorer.calls == ["stage1", "stage2"]
    assert entry.scoring_meta["score_path"] == "stage2"
    _assert_explanation(entry, _expected(raw=7.0, adjustment=0.0, margin=0.0, hint=9.0))


def test_transport_exception_records_fallback_output_not_an_unavailable_model_score(
    pipeline,
):
    pipeline.scorer = FixedScorer(TimeoutError("model timeout"))

    entry, result, reason = _process(pipeline)

    assert result is None and reason == "filtered"
    assert entry.scoring_meta["score_path"] == "fallback_local"
    _assert_explanation(entry, _expected(raw=3.0, adjustment=0.0, margin=-4.0))


def test_parse_failure_does_not_turn_minus_one_sentinel_into_raw_model_evidence(
    pipeline,
):
    pipeline.scorer = FixedScorer(ValueError("invalid tuple"))

    entry, result, reason = _process(pipeline)

    assert result is None and reason == "filtered"
    assert entry.scoring_meta["score_path"] == "parse_error"
    _assert_explanation(entry, _expected(threshold=None))


def test_cache_hit_reads_frozen_explanation_without_rescoring_or_rebinding_threshold(
    pipeline,
):
    original, _, _ = _process(pipeline)
    before = _persisted_rows(original.link)
    pipeline.cfg["feeds"][0]["pass_threshold"] = 6.0
    pipeline.scorer = FixedScorer(AssertionError("a cache hit must not rescore"))

    entry, result, reason = _process(pipeline)

    assert result is entry and reason == "cache"
    assert pipeline.scorer.calls == []
    _assert_explanation(entry, _expected(raw=8.0, adjustment=0.0, margin=1.0))
    after = _persisted_rows(entry.link)
    for old, new in zip(before, after):
        assert {key: old[key] for key in EXPLANATION_FIELDS} == {
            key: new[key] for key in EXPLANATION_FIELDS
        }


def test_prefetched_cache_tuple_carries_explanation_without_an_extra_metadata_lookup(
    pipeline, monkeypatch
):
    original, _, _ = _process(pipeline)
    link_cache = database.get_cached_entries_by_links([original.link])
    monkeypatch.setattr(
        processor.DB,
        "get_cached_entry_meta",
        lambda *_a, **_k: pytest.fail(
            "prefetched explanations must not add a DB lookup"
        ),
    )
    pipeline.scorer = FixedScorer(AssertionError("cache must not rescore"))

    entry, result, reason = _process(pipeline, link_cache=link_cache)

    assert result is entry and reason == "cache"
    assert len(link_cache[entry.link]) == 19
    assert tuple(link_cache[entry.link][13:19]) == (8.0, 0.0, 1.0, None, None, 7.0)
    _assert_explanation(entry, _expected(raw=8.0, adjustment=0.0, margin=1.0))


def test_rejected_entry_without_cache_still_keeps_complete_ledger_explanation(pipeline):
    pipeline.cfg["cache_rejected_entries"] = False
    pipeline.scorer = FixedScorer(_outcome(6.0))

    entry, result, reason = _process(pipeline)

    assert result is None and reason == "filtered"
    with database.get_connection() as conn:
        assert conn.execute("SELECT COUNT(*) FROM news_cache").fetchone()[0] == 0
        ledger = dict(conn.execute("SELECT * FROM backtest_signals").fetchone())
    expected = _expected(raw=6.0, adjustment=0.0, margin=-1.0)
    assert {key: ledger[key] for key in EXPLANATION_FIELDS} == expected
    assert {key: entry.scoring_meta[key] for key in EXPLANATION_FIELDS} == expected
    assert ledger["selected"] == 0


def test_cached_quarantine_remains_blocked_and_has_no_stale_explanation(pipeline):
    original, _, _ = _process(pipeline)
    with database.get_connection() as conn:
        conn.execute(
            "UPDATE news_cache SET score = -1, score_path = 'quality_quarantine' "
            "WHERE link = ?",
            (original.link,),
        )
        conn.execute(
            "UPDATE backtest_signals SET score = -1, score_path = 'quality_quarantine', "
            "selected = 0 WHERE link = ?",
            (original.link,),
        )
        conn.commit()
    pipeline.scorer = FixedScorer(
        AssertionError("quarantine is not permission to rescore")
    )

    entry, result, reason = _process(pipeline)

    assert result is None and reason == "quality_quarantine"
    assert pipeline.scorer.calls == []
    for row in _persisted_rows(entry.link):
        assert row["score_path"] == "quality_quarantine"
        assert {key: row[key] for key in EXPLANATION_FIELDS} == _expected(
            threshold=None
        )
