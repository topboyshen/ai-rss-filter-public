"""Characterize process_entry phase boundaries without network or database writes."""

import re
from types import SimpleNamespace
from unittest.mock import Mock

import pytest
from feedparser import FeedParserDict

from core import processor
from scoring_provenance import SAFE_NEUTRAL_CONTRACT_VALUES


@pytest.fixture
def phases(monkeypatch):
    calls = []
    state = SimpleNamespace(
        calls=calls,
        cached=None,
        latest_path="",
        similar=None,
        writes=[],
        snapshots=[],
        failures={},
        stages={"stage1": ((8.0, "政策", "来源证据", "📈"), "model-one", "", "")},
        entry=FeedParserDict(
            title="<b>原始政策标题</b>",
            link="https://example.com/phases",
            summary="<p>摘要</p>",
        ),
        cfg={
            "_blacklist_re": re.compile(r"$^"),
            "threshold": {"pass": 7.0, "notify": 8.0, "similarity": 0.9},
            "scoring_strategy_version": "phase:contract",
            "generate_summary": True,
        },
    )

    def call(name):
        calls.append(name)
        if name in state.failures:
            raise state.failures[name]

    def cached(_link):
        call("cache")
        return state.cached

    def ledger(*_args):
        call("ledger")
        return state.latest_path

    def similar(*_args):
        call("similarity")
        return state.similar, 0.95

    def score(_scorer, stage, *args, **kwargs):
        call(stage)
        assert args[:2] == ("原始政策标题", "摘要")
        return state.stages[stage]

    def snapshot(**kwargs):
        call("snapshot")
        state.snapshots.append(kwargs)
        return {}

    def write(*args, **kwargs):
        call("write")
        state.writes.append((args, kwargs))
        return {"event_id": "event-fixed", "event_version": 2}

    def identity(*_args):
        call("identity")
        return "event-fixed", 2

    def summary(*_args):
        call("summary")
        return "生成摘要"

    original_meta = processor._set_scoring_meta

    def meta(entry, **kwargs):
        call("meta")
        original_meta(entry, **kwargs)

    monkeypatch.setattr(processor.DB, "canonical_feed_url", lambda _cfg, url: url)
    monkeypatch.setattr(processor.DB, "get_cached_entry", cached)
    monkeypatch.setattr(processor.DB, "get_latest_signal_score_path", ledger)
    monkeypatch.setattr(processor.DB, "record_score", lambda _score: call("record"))
    monkeypatch.setattr(processor.DB, "set_cached_entry", write)
    monkeypatch.setattr(
        processor.DB, "store_backtest_signal", lambda _s: call("ledger_write")
    )
    monkeypatch.setattr(processor, "find_similar_article", similar)
    monkeypatch.setattr(processor, "_score_stage_with_identity", score)
    monkeypatch.setattr(processor, "_build_backtest_signal_snapshot", snapshot)
    monkeypatch.setattr(processor, "_persisted_event_identity", identity)
    monkeypatch.setattr(processor, "_set_scoring_meta", meta)
    monkeypatch.setattr(
        processor, "send_telegram_notification", lambda *_a: call("notify")
    )
    state.scorer = Mock()
    state.scorer.summarize.side_effect = summary
    state.scorer._fallback_local_score.side_effect = lambda *_a: (
        call("fallback") or (3.0, "本地", "固定保守结果", "⚖️")
    )
    return state


def run(state, **kwargs):
    return processor.process_entry(
        state.entry, [], "prompt", state.cfg, scorer=state.scorer, **kwargs
    )


def test_success_persists_before_mutation_and_notification(phases):
    result, reason = run(phases)

    assert result is phases.entry and reason == "scored"
    assert phases.calls == [
        "cache",
        "ledger",
        "similarity",
        "stage1",
        "record",
        "summary",
        "snapshot",
        "write",
        "identity",
        "meta",
        "notify",
    ]
    args, kwargs = phases.writes[0]
    assert args[:7] == (
        phases.entry.link,
        "[⭐8.0 📈 政策] 原始政策标题 💡来源证据",
        "原始政策标题",
        8.0,
        "政策",
        "来源证据",
        "生成摘要",
    )
    assert kwargs["score_path"] == "llm"
    assert phases.entry.ai_summary == "生成摘要"
    assert phases.entry.scoring_meta["event_id"] == "event-fixed"


@pytest.mark.parametrize("failure_at", ["record", "write", "identity", "notify"])
def test_side_effect_failure_does_not_advance_later_phases(phases, failure_at):
    failure = OSError(failure_at)
    phases.failures[failure_at] = failure
    with pytest.raises(OSError) as caught:
        run(phases)

    assert caught.value is failure
    assert phases.calls[-1] == failure_at
    if failure_at != "notify":
        assert phases.entry.title == "原始政策标题"
        assert "meta" not in phases.calls
    else:
        assert phases.entry.title.startswith("[⭐8.0")
        assert phases.calls[-2:] == ["meta", "notify"]


@pytest.mark.parametrize(
    "failure", [ValueError("invalid summary"), RuntimeError("summary unavailable")]
)
def test_summary_failure_is_best_effort_after_record_before_write(phases, failure):
    phases.failures["summary"] = failure
    result, reason = run(phases)

    assert result is phases.entry and reason == "scored"
    assert phases.calls[4:8] == ["record", "summary", "snapshot", "write"]
    assert phases.writes[0][0][6] == phases.entry.ai_summary == ""


@pytest.mark.parametrize("cache_rejected", [True, False])
def test_filtered_score_always_persists_ledger_but_never_summarizes_or_notifies(
    phases, cache_rejected
):
    phases.cfg["cache_rejected_entries"] = cache_rejected
    phases.stages["stage1"] = ((6.0, "政策", "来源证据", "📈"), "model-one", "", "")

    assert run(phases) == (None, "filtered")
    assert phases.calls == [
        "cache",
        "ledger",
        "similarity",
        "stage1",
        "record",
        "snapshot",
        "write" if cache_rejected else "ledger_write",
        "identity",
        "meta",
    ]
    assert phases.snapshots[0]["summary"] == ""
    assert phases.entry.title == "原始政策标题"


def test_safe_neutral_does_not_record_distribution_or_notify_even_below_threshold(
    phases,
):
    phases.cfg["threshold"]["pass"] = 1
    phases.cfg["threshold"]["notify"] = 1
    phases.stages["stage1"] = (
        SAFE_NEUTRAL_CONTRACT_VALUES,
        "model-one",
        "safe_neutral_contract",
        "contract",
    )

    assert run(phases) == (None, "filtered")
    assert phases.calls == [
        "cache",
        "ledger",
        "similarity",
        "stage1",
        "snapshot",
        "write",
        "identity",
        "meta",
    ]
    assert phases.entry.scoring_meta["score_path"] == "llm_safe_neutral_contract"
    assert phases.entry.scoring_meta["safe_neutral_reason"] == "contract"


@pytest.mark.parametrize(
    ("failure", "path", "model", "error"),
    [
        (TypeError("invalid stage2"), "parse_error", "model-one", "parse_error"),
        (
            RuntimeError("stage2 unavailable"),
            "fallback_local",
            "fallback_local",
            "score_error",
        ),
    ],
)
def test_stage2_failure_retains_completed_stage1_identity(
    phases, failure, path, model, error
):
    phases.cfg["two_stage_scoring"] = {"enabled": True}
    phases.failures["stage2"] = failure

    assert run(phases) == (None, "filtered")
    meta = phases.entry.scoring_meta
    assert meta["stage1_score"] == 8.0
    assert meta["stage1_model"] == "model-one"
    assert meta["stage2_score"] is None and meta["stage2_model"] == ""
    assert (meta["score_path"], meta["model_name"], meta["error_type"]) == (
        path,
        model,
        error,
    )
    assert phases.calls[3:5] == ["stage1", "stage2"]
    assert ("fallback" in phases.calls) == (path == "fallback_local")


@pytest.mark.parametrize("notification_failure", [False, True])
def test_similarity_writes_then_notifies_before_setting_metadata(
    phases, notification_failure
):
    # This test checks side-effect order, with a provably identical reprint.
    phases.similar = ("old-link", "原始政策标题", 0, 8.0, "政策", "旧证据")
    if notification_failure:
        phases.failures["notify"] = OSError("notify failed")
        with pytest.raises(OSError, match="notify failed"):
            run(phases)
    else:
        assert run(phases) == (None, "similarity")

    assert phases.calls == [
        "cache",
        "ledger",
        "similarity",
        "snapshot",
        "write",
        "identity",
        "notify",
    ] + ([] if notification_failure else ["meta"])
    assert phases.entry.title == "原始政策标题"
    assert phases.scorer.mock_calls == []


@pytest.mark.parametrize("path", ["llm", "similarity", "llm_safe_neutral_contract"])
def test_expired_cache_with_frozen_ledger_never_reaches_similarity_or_scoring(
    phases, path
):
    phases.latest_path = path

    assert run(phases) == (None, "similarity" if path == "similarity" else "filtered")
    assert phases.calls == ["cache", "ledger"]
    assert phases.scorer.mock_calls == []


def test_unresolved_contract_fails_before_canonicalization_or_title_mutation(
    phases, monkeypatch
):
    phases.cfg["few_shot_feedback"] = {"enabled": True}
    canonicalize = Mock()
    monkeypatch.setattr(processor.DB, "canonical_feed_url", canonicalize)
    original_title = phases.entry.title

    with pytest.raises(RuntimeError, match="was not resolved"):
        run(phases)

    canonicalize.assert_not_called()
    assert phases.calls == []
    assert phases.entry.title == original_title


def test_two_stage_keeps_review_reason_hint_order_and_stage_latencies(
    phases, monkeypatch
):
    phases.cfg["fast_pass"] = {
        "enabled": True,
        "keywords": ["政策"],
        "score": 9.0,
    }
    phases.cfg["two_stage_scoring"] = {
        "enabled": True,
        "high_value_source_tiers": ["official"],
    }
    phases.similar = ("old-link", "相似政策", 0, 9.0, "政策", "旧证据")
    phases.stages["stage2"] = ((8.25, "政策", "复核证据", "📈"), "model-two", "", "")
    monkeypatch.setattr(processor, "_source_tier", lambda *_args: "official")
    monkeypatch.setattr(processor, "_should_rescore_similar", lambda *_args: True)
    monkeypatch.setattr(
        processor,
        "get_metrics",
        lambda: SimpleNamespace(record_fast_pass=lambda _tag: None),
    )
    clock = Mock(side_effect=[100.0, 100.25, 101.0, 101.5, 102.0, 103.0])
    monkeypatch.setattr(processor, "time", SimpleNamespace(time=clock))

    result, reason = run(phases)

    assert result is phases.entry and reason == "scored"
    meta = phases.entry.scoring_meta
    assert (
        meta["review_reason"]
        == "boundary_score,fast_pass_hint,similar_higher_score,high_value_source"
    )
    assert (meta["stage1_score"], meta["stage2_score"], meta["latency_ms"]) == (
        8.0,
        8.25,
        750,
    )
    assert meta["model_name"] == "model-two" and meta["score_path"] == "stage2"
    assert phases.writes[0][0][5] == "复核证据; FastPass候选:FastPass; 相似旧分:9.0"
    assert phases.snapshots[0]["scored_at"] == 102
    assert phases.writes[0][1]["created_at"] == 103
    assert clock.call_count == 6


@pytest.mark.parametrize(
    "new_title, old_title, old_logic",
    [
        ("乙公司营收下降10%", "甲公司营收增长20%", "营收增长改善预期"),
        ("乙公司增持", "甲公司增持", "大股东增持提升信心"),
        ("甲公司获得订单", "甲公司获得3亿元订单", "获得3亿元订单"),
    ],
)
def test_similar_conflict_does_not_write_or_notify_old_score(
    phases, monkeypatch, new_title, old_title, old_logic
):
    def score(_scorer, stage, *args, **kwargs):
        phases.calls.append(stage)
        return phases.stages[stage]

    monkeypatch.setattr(processor, "_score_stage_with_identity", score)
    phases.entry.title = new_title
    phases.similar = (
        "old-link", old_title, old_title, 9.0, "业绩", old_logic, "看多",
    )
    result, reason = run(phases)
    assert result is phases.entry and reason == "scored"
    args, kwargs = phases.writes[0]
    assert kwargs.get("score_path") != "similarity"
    assert args[3] != 9.0
    assert old_logic not in str(args[5])
    assert "similarity" not in phases.calls[phases.calls.index("write"):]
