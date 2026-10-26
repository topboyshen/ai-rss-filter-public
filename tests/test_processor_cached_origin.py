"""Preserve cache-origin repair ordering and its two failure policies."""

import re
import time
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from core import processor
from db import database
from utils.helpers import FeedEntry


@pytest.fixture
def cached_entry(monkeypatch):
    # process_entry re-reads this link's live cache row; give it a schema.
    database.init_db()
    entry = FeedEntry("原始标题", "https://example.com/cached-origin", "来源摘要")
    entry.published_parsed = time.gmtime(1_750_000_000)
    cfg = {
        "_blacklist_re": re.compile(r"$^"),
        "threshold": {"pass": 7.0, "notify": 9.0, "similarity": 0.8},
        "scoring_strategy_version": "test:cached-origin",
        "feeds": [{"url": "feed-b", "aggregation_threshold": 9.0}],
    }
    cached = (
        "缓存标题",
        "原始标题",
        8.5,
        "政策",
        "缓存逻辑",
        1_749_999_000,
        "缓存摘要",
        cfg["scoring_strategy_version"],
        "llm",
        0.75,
        "看多",
        "",
        0,
    )
    origin = Mock()
    monkeypatch.setattr(processor.DB, "canonical_feed_url", lambda _cfg, url: url)
    monkeypatch.setattr(processor.DB, "update_cached_entry_origin", origin)
    monkeypatch.setattr(processor, "_persisted_event_identity", lambda _entry: ("", 0))
    monkeypatch.setattr(
        processor.DB,
        "get_latest_signal_score_path",
        Mock(side_effect=AssertionError("a cache hit must not reach ledger replay")),
    )
    return SimpleNamespace(
        entry=entry,
        cfg=cfg,
        cached=cached,
        origin=origin,
        scorer=Mock(),
    )


def _process_cached(state, *, feed_url="feed-b"):
    return processor.process_entry(
        state.entry,
        [],
        "prompt",
        state.cfg,
        scorer=state.scorer,
        link_cache={state.entry.link: state.cached},
        feed_url=feed_url,
    )


def test_cache_without_origin_metadata_does_not_attempt_repair(
    cached_entry, monkeypatch
):
    cached_entry.entry.published_parsed = None
    aggregation = Mock(side_effect=AssertionError("no origin repair is needed"))
    monkeypatch.setattr(processor, "_aggregation_threshold_for_feed", aggregation)

    result, reason = _process_cached(cached_entry, feed_url="")

    assert result is cached_entry.entry
    assert reason == "cache"
    cached_entry.origin.assert_not_called()
    aggregation.assert_not_called()
    assert cached_entry.scorer.mock_calls == []


@pytest.mark.parametrize(
    ("score_path", "expected_reason"),
    [
        ("llm", "cache"),
        ("similarity", "similarity"),
        ("llm_safe_neutral_contract", "filtered"),
    ],
)
def test_metadata_only_failure_preserves_cached_path(
    cached_entry,
    caplog,
    score_path,
    expected_reason,
):
    cached_entry.cached = (
        *cached_entry.cached[:8],
        score_path,
        *cached_entry.cached[9:],
    )
    if score_path != "llm":
        cached_entry.cfg["feeds"][0]["aggregation_threshold"] = 7.0
    cached_entry.origin.side_effect = OSError("metadata write failed")

    result, reason = _process_cached(cached_entry)

    assert reason == expected_reason
    assert result is (cached_entry.entry if expected_reason == "cache" else None)
    cached_entry.origin.assert_called_once_with(
        cached_entry.entry.link,
        created_at=1_750_000_000,
        feed_url="feed-b",
        backtest_selection=None,
    )
    assert "Failed to refresh cached entry origin" in caplog.text
    assert cached_entry.scorer.mock_calls == []


def test_selection_upgrade_failure_propagates_without_emitting_cache(cached_entry):
    cached_entry.cfg["feeds"][0]["aggregation_threshold"] = 7.0
    failure = OSError("selection transaction failed")
    cached_entry.origin.side_effect = failure

    with pytest.raises(OSError) as caught:
        _process_cached(cached_entry)

    assert caught.value is failure
    cached_entry.origin.assert_called_once_with(
        cached_entry.entry.link,
        created_at=1_750_000_000,
        feed_url="feed-b",
        backtest_selection={
            "strategy_version": "test:cached-origin",
            "aggregation_threshold": 7.0,
        },
    )
    assert cached_entry.entry.title == "原始标题"
    assert cached_entry.scorer.mock_calls == []


@pytest.mark.parametrize("score", [6.99, 7.0])
def test_cache_selection_upgrade_keeps_exact_threshold_boundary(cached_entry, score):
    cached_entry.cfg["feeds"][0]["aggregation_threshold"] = 7.0
    cached_entry.cached = (*cached_entry.cached[:2], score, *cached_entry.cached[3:])

    result, reason = _process_cached(cached_entry)

    selected = score >= 7.0
    assert reason == ("cache" if selected else "filtered")
    assert result is (cached_entry.entry if selected else None)
    assert cached_entry.origin.call_args.kwargs["backtest_selection"] == (
        {"strategy_version": "test:cached-origin", "aggregation_threshold": 7.0}
        if selected
        else None
    )
    assert cached_entry.scorer.mock_calls == []
