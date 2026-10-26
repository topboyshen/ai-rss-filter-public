"""Local neutral results retain their audit identity and never become signals."""

import time
from unittest.mock import Mock

import pytest

from core import processor
from core.scorer import ScoringResult
from db import database
from db.integration_outbox import enqueue_signal_revision
from scoring_provenance import (
    SAFE_NEUTRAL_CONTRACT_VALUES,
    SAFE_NEUTRAL_SCORE_PATHS_BY_RESOLUTION,
)
from utils.helpers import FeedEntry


LOCAL_MODEL = "local_safe_neutral"
STRATEGY = "local-neutral-persistence"
FEED = "https://feeds.example.test/news"
RESOLUTIONS = [
    ("safe_neutral_contract", "contract"),
    ("safe_neutral_grounding", "facts"),
    ("safe_neutral_grounding", "theme"),
]


def _neutral_write(stage, resolution, reason):
    now = int(time.time())
    path = SAFE_NEUTRAL_SCORE_PATHS_BY_RESOLUTION[resolution][stage]
    link = f"https://example.test/local-neutral/{stage}/{reason}"
    signal = {
        "link": link,
        "event_key": link,
        "feed_url": FEED,
        "raw_title": "公司发布日常经营信息",
        "event_type": "其他",
        "logic": SAFE_NEUTRAL_CONTRACT_VALUES[2],
        "score": 4.0,
        "score_path": path,
        "direction": "中性",
        "score_confidence": 0.0,
        "strategy_version": STRATEGY,
        "mapping_version": "asset-map:local-neutral-test",
        "available_at": now,
        "scored_at": now,
        "aggregation_threshold": 1.0,
        "selected": False,
        "is_similarity_copy": False,
    }
    return {
        "link": link,
        "title": "公司发布日常经营信息",
        "raw_title": "公司发布日常经营信息",
        "score": 4.0,
        "tag": "其他",
        "logic": SAFE_NEUTRAL_CONTRACT_VALUES[2],
        "feed_url": FEED,
        "score_path": path,
        "model_name": LOCAL_MODEL,
        "strategy_version": STRATEGY,
        "stage1_score": 7.0 if stage == "stage2" else 4.0,
        "stage1_model": "backup-stage1-model" if stage == "stage2" else LOCAL_MODEL,
        "stage2_score": 4.0 if stage == "stage2" else None,
        "stage2_model": LOCAL_MODEL if stage == "stage2" else "",
        "score_confidence": 0.0,
        "direction": "中性",
        "safe_neutral_reason": reason,
        "created_at": now,
        "backtest_signal": signal,
    }


@pytest.mark.parametrize("stage", ["stage1", "stage2"])
@pytest.mark.parametrize("resolution,reason", RESOLUTIONS)
def test_local_neutral_persists_bound_cache_and_nonexportable_ledger(
    stage, resolution, reason,
):
    database.init_db()
    write = _neutral_write(stage, resolution, reason)

    database.set_cached_entry(**write)

    with database.get_connection() as conn:
        cache = conn.execute(
            "SELECT * FROM news_cache WHERE link = ?", (write["link"],)
        ).fetchone()
        ledger = conn.execute(
            "SELECT * FROM backtest_signals WHERE link = ?", (write["link"],)
        ).fetchone()
        assert cache is not None and ledger is not None
        for row in (cache, ledger):
            assert row["score_path"] == write["score_path"]
            assert row["score"] == 4.0
            assert row["direction"] == "中性"
            assert row["score_confidence"] == 0.0
        assert cache["tag"] == "其他"
        assert cache["logic"] == SAFE_NEUTRAL_CONTRACT_VALUES[2]
        assert cache["model_name"] == LOCAL_MODEL
        assert cache["safe_neutral_reason"] == reason
        assert cache[f"{stage}_model"] == LOCAL_MODEL
        assert cache[f"{stage}_score"] == 4.0
        assert ledger["selected"] == 0
        assert ledger["aggregation_threshold"] == 1.0
        assert enqueue_signal_revision(
            conn,
            ledger_signal_id=ledger["id"],
            signal=dict(ledger),
            signal_revision_key=ledger["signal_key"],
        ) is False
        assert conn.execute(
            "SELECT COUNT(*) FROM integration_signal_outbox"
        ).fetchone()[0] == 0


@pytest.mark.parametrize("stage", ["stage1", "stage2"])
@pytest.mark.parametrize(
    "corruption",
    ["score", "tag", "logic", "confidence", "selected", "stage_model", "stage_score"],
)
def test_local_neutral_rejects_corrupt_tuple_confidence_and_stage_provenance(
    stage, corruption,
):
    database.init_db()
    write = _neutral_write(stage, "safe_neutral_contract", "contract")
    if corruption == "score":
        write["score"] = write["backtest_signal"]["score"] = 5.0
    elif corruption == "tag":
        write["tag"] = write["backtest_signal"]["event_type"] = "公司公告"
    elif corruption == "logic":
        write["logic"] = write["backtest_signal"]["logic"] = "公司新增投资催化"
    elif corruption == "confidence":
        write["score_confidence"] = write["backtest_signal"]["score_confidence"] = 0.5
    elif corruption == "selected":
        write["backtest_signal"]["selected"] = True
    elif corruption == "stage_model":
        write[f"{stage}_model"] = "some-paid-model"
    else:
        write[f"{stage}_score"] = 8.0

    with pytest.raises(ValueError):
        database.set_cached_entry(**write)

    with database.get_connection() as conn:
        for table in ("news_cache", "backtest_signals", "integration_signal_outbox"):
            assert conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0] == 0


@pytest.mark.parametrize("path", ["llm", "stage2"])
def test_local_neutral_identity_cannot_be_written_as_substantive_model(path):
    database.init_db()
    stage = "stage1" if path == "llm" else "stage2"
    write = _neutral_write(stage, "safe_neutral_contract", "contract")
    write.update(score=8.0, tag="公司公告", logic="经营信息", score_path=path)
    write[f"{stage}_score"] = 8.0
    write["backtest_signal"].update(
        score=8.0, event_type="公司公告", logic="经营信息", score_path=path,
        selected=True,
    )

    with pytest.raises(ValueError, match="local"):
        database.set_cached_entry(**write)

    with database.get_connection() as conn:
        assert conn.execute("SELECT COUNT(*) FROM news_cache").fetchone()[0] == 0
        assert conn.execute("SELECT COUNT(*) FROM backtest_signals").fetchone()[0] == 0
        assert conn.execute("SELECT COUNT(*) FROM integration_signal_outbox").fetchone()[0] == 0


@pytest.mark.parametrize("stage", ["stage1", "stage2"])
def test_local_neutral_cannot_claim_an_initial_model_neutral_result(stage):
    database.init_db()
    write = _neutral_write(stage, "safe_neutral_initial", "initial")

    with pytest.raises(ValueError, match="initial"):
        database.set_cached_entry(**write)

    with database.get_connection() as conn:
        assert conn.execute("SELECT COUNT(*) FROM news_cache").fetchone()[0] == 0
        assert conn.execute("SELECT COUNT(*) FROM backtest_signals").fetchone()[0] == 0


class _BoundLocalScorer:
    def __init__(self, stage, resolution, reason):
        self.stage = stage
        self.resolution = resolution
        self.reason = reason
        self.calls = []
        self.summarize = Mock(side_effect=AssertionError("neutral must not summarize"))

    def _neutral(self):
        return ScoringResult(
            SAFE_NEUTRAL_CONTRACT_VALUES, LOCAL_MODEL, self.resolution, self.reason,
        )

    def score_stage1_with_identity(self, *_args, **_kwargs):
        self.calls.append("stage1")
        if self.stage == "stage1":
            return self._neutral()
        return ScoringResult((7.0, "公司公告", "经营信息", "⚖️"), "backup-stage1-model")

    def score_stage2_with_identity(self, *_args, **_kwargs):
        self.calls.append("stage2")
        assert self.stage == "stage2", "neutral stage1 must skip stage2"
        return self._neutral()


@pytest.mark.parametrize("stage", ["stage1", "stage2"])
@pytest.mark.parametrize("resolution,reason", RESOLUTIONS)
def test_processor_local_neutral_stays_filtered_and_unexported_at_low_thresholds(
    monkeypatch, stage, resolution, reason,
):
    database.init_db()
    cfg = {
        "scoring_strategy_version": STRATEGY,
        "threshold": {"pass": 1.0, "notify": 1.0, "similarity": 0.6},
        "similarity_window_hours": 72,
        "_blacklist_re": None,
        "cache_rejected_entries": True,
        "generate_summary": True,
        "summary_min_score": 1.0,
        "local_reject_rules": {"enabled": False, "rules": []},
        "fast_pass": {"enabled": True, "skip_llm": False},
        "two_stage_scoring": {"enabled": True},
        "feeds": [{"url": FEED, "tier": "primary_news", "aggregation_threshold": 1.0}],
    }
    similar = (
        "https://example.test/valid-model-parent", "日常经营信息进展",
        "日常经营信息进展", 9.0, "公司公告", "经营信息", "看多",
    )
    monkeypatch.setattr(processor, "find_similar_article", lambda *_a, **_kw: (similar, 0.99))
    monkeypatch.setattr(processor, "_should_rescore_similar", lambda *_a, **_kw: True)
    monkeypatch.setattr(
        processor, "_match_fast_pass", lambda *_a, **_kw: (9.0, "公司公告", "候选提示", "📈", False),
    )
    monkeypatch.setattr(processor, "_get_time_bonus", lambda *_a, **_kw: (2.0, "boost"))
    monkeypatch.setattr(processor, "map_article_to_assets", lambda *_a, **_kw: {})
    distribution = Mock()
    notify = Mock()
    monkeypatch.setattr(processor.DB, "record_score", distribution)
    monkeypatch.setattr(processor, "send_telegram_notification", notify)
    scorer = _BoundLocalScorer(stage, resolution, reason)
    entry = FeedEntry("公司发布日常经营信息", f"https://example.test/processor/{stage}/{reason}")

    assert processor.process_entry(
        entry, [similar], "prompt", cfg, scorer=scorer, feed_url=FEED,
    ) == (None, "filtered")
    assert scorer.calls == (["stage1"] if stage == "stage1" else ["stage1", "stage2"])
    assert entry.scoring_meta["model_name"] == LOCAL_MODEL
    assert entry.scoring_meta["safe_neutral_reason"] == reason
    assert entry.scoring_meta["score_confidence"] == 0.0
    distribution.assert_not_called()
    notify.assert_not_called()
    scorer.summarize.assert_not_called()

    with database.get_connection() as conn:
        cache = conn.execute("SELECT * FROM news_cache WHERE link = ?", (entry.link,)).fetchone()
        ledger = conn.execute("SELECT * FROM backtest_signals WHERE link = ?", (entry.link,)).fetchone()
        assert cache["score"] == ledger["score"] == 4.0
        assert cache["logic"] == SAFE_NEUTRAL_CONTRACT_VALUES[2]
        assert cache["score_path"] == ledger["score_path"] == entry.scoring_meta["score_path"]
        assert cache[f"{stage}_model"] == LOCAL_MODEL
        assert ledger["selected"] == 0
        assert conn.execute("SELECT COUNT(*) FROM integration_signal_outbox").fetchone()[0] == 0
        assert conn.execute("SELECT COUNT(*) FROM llm_call_logs").fetchone()[0] == 0

    assert database.get_recent_entries(cfg=cfg) == []
    assert database.get_entries_for_cached_rss(cfg, feed_url=FEED, min_score=1.0) == []
    replay = FeedEntry("公司发布日常经营信息", entry.link)
    assert processor.process_entry(
        replay, [], "prompt", cfg, scorer=scorer, feed_url=FEED,
    ) == (None, "filtered")
    assert scorer.calls == (["stage1"] if stage == "stage1" else ["stage1", "stage2"])
    notify.assert_not_called()
