"""Terminal scoring is neither a pending score nor a successful filter."""
import re
import time
from concurrent.futures import Future
from unittest.mock import Mock

import pytest

from core import processor
from db import database
from services import natural_score_continuations as store
from services import news_processing_trace as tracing
from tests.test_natural_score_continuation import (
    FEED, LINK, PROMPT, SUMMARY, TITLE, _entry, _record, _rows,
    pipeline,  # noqa: F401
)
from tests.test_webhook_durable_spool import (
    _pending_files, isolated_spool,  # noqa: F401
)
from utils.helpers import FeedEntry

_SOURCE_REJECTIONS = [
    ("changed blacklist title", SUMMARY, "blacklist"),
    ("[⭐8.0] previously marked", SUMMARY, "already_scored_title"),
    ("[★8.0] previously marked", SUMMARY, "already_scored_title"),
    ("", SUMMARY, "empty_title"),
    ("<b> </b>", SUMMARY, "empty_title"),
    ("company\ufffdtitle", SUMMARY, "invalid_source_text"),
    (TITLE, "publisher\ufffdsummary", "invalid_source_text"),
]


def _terminal_record(cfg, state):
    strategy = cfg["scoring_strategy_version"]
    payload = {
        "title": TITLE, "link": LINK, "summary": SUMMARY, "prompt": PROMPT,
        "feed_url": FEED, "few_shot_context": "",
    }
    assert store.enqueue(strategy, LINK, payload, initial_attempts=3) == "stored"
    with store.locked_record(strategy, LINK) as record:
        data = record.data
        data.update(state=state, updated_at=time.time())
        record.save(data)
    return _record(cfg)


def _processing_rows():
    with database.get_connection() as connection:
        return [
            dict(row) for row in connection.execute(
                "SELECT * FROM news_processing_logs ORDER BY rowid",
            )
        ]


@pytest.mark.parametrize("terminal", [
    "expired", "exhausted", "contract_changed", "invalid", "completed",
])
def test_real_terminal_processing_records_unscored_error_without_calls(pipeline, terminal):  # noqa: F811
    state = pipeline
    before = _terminal_record(state.cfg, terminal)
    for _ in range(2):
        result = processor.process_entry(
            _entry(), [], PROMPT, state.cfg, scorer=state.scorer, feed_url=FEED,
        )
        assert result == (None, processor.SCORE_TERMINAL_UNSCORED_REASON)
    rows = _processing_rows()
    assert len(rows) == 2 and state.calls == []
    assert _rows() == ([], [])
    assert _record(state.cfg) == before
    for row in rows:
        assert row["state"] == "error"
        assert row["outcome"] == processor.SCORE_TERMINAL_UNSCORED_REASON
        assert row["error_type"] == ""  # No fabricated exception.
        assert row["model_attempts"] == row["result_returned"] == 0
        assert row["signal_key"] is row["score"] is row["score_path"] is None
        assert row["aggregation_selected"] is None and row["signal_association"] == "none"


@pytest.mark.parametrize("terminal", ["expired", "exhausted"])
@pytest.mark.parametrize("title,summary,source_reason", _SOURCE_REJECTIONS)
def test_terminal_same_link_source_rejection_cannot_become_filtered_completion(
    pipeline, terminal, title, summary, source_reason,  # noqa: F811
):
    state = pipeline
    state.cfg["_blacklist_re"] = re.compile("changed blacklist title")
    before = _terminal_record(state.cfg, terminal)
    for _ in range(2):
        result = state.run(title=title, summary=summary)
        assert result == (None, processor.SCORE_TERMINAL_UNSCORED_REASON)
        row = _processing_rows()[-1]
        assert row["state"] == "error"
        assert row["outcome"] == processor.SCORE_TERMINAL_UNSCORED_REASON
        assert row["outcome"] != source_reason
        assert row["error_type"] == ""
        assert row["signal_key"] is row["score"] is row["score_path"] is None
        assert row["signal_association"] == "none"
        assert row["model_attempts"] == row["result_returned"] == 0
        assert _record(state.cfg) == before
        assert state.calls == [] and _rows() == ([], [])


@pytest.mark.parametrize("title,summary,source_reason", _SOURCE_REJECTIONS)
def test_unjournaled_source_rejection_keeps_existing_input_protection(
    pipeline, title, summary, source_reason,  # noqa: F811
):
    state = pipeline
    state.cfg["_blacklist_re"] = re.compile("changed blacklist title")
    result = state.run(title=title, summary=summary)
    assert result == (None, source_reason)
    row, = _processing_rows()
    assert row["state"] == "completed" and row["outcome"] == source_reason
    assert row["model_attempts"] == row["result_returned"] == 0
    assert row["signal_key"] is None
    assert _record(state.cfg) is None
    assert state.calls == [] and _rows() == ([], [])


def test_terminal_diagnostic_never_borrows_existing_same_article_signal(pipeline):  # noqa: F811
    state = pipeline
    state.control["mode"] = "recovered"
    result = processor.process_entry(
        _entry(), [], PROMPT, state.cfg, scorer=state.scorer, feed_url=FEED,
    )
    assert result[1] == "scored"
    cache_before, signals_before = _rows()
    with tracing.trace_news_processing(FeedEntry(TITLE, LINK), state.cfg, FEED) as trace:
        trace["result"] = (None, processor.SCORE_TERMINAL_UNSCORED_REASON)
    row = _processing_rows()[-1]
    assert row["state"] == "error" and row["error_type"] == ""
    assert row["outcome"] == processor.SCORE_TERMINAL_UNSCORED_REASON
    assert row["article_key"] == signals_before[0]["signal_key"]
    assert row["signal_key"] is row["score"] is row["score_path"] is None
    assert row["aggregation_selected"] is None and row["signal_association"] == "none"
    assert _rows() == (cache_before, signals_before)


@pytest.mark.parametrize("terminal", ["expired", "exhausted"])
def test_terminal_webhook_archives_unscored_outcome_not_filtered_success(
    pipeline, isolated_spool, monkeypatch, terminal,  # noqa: F811
):
    state, webhook = pipeline, isolated_spool
    before = _terminal_record(state.cfg, terminal)
    monkeypatch.setattr(webhook, "Scorer", lambda: state.scorer)
    monkeypatch.setattr(webhook, "_cfg", lambda: state.cfg)
    monkeypatch.setattr(webhook, "resolve_scoring_contract", lambda cfg, **_kw: cfg)
    monkeypatch.setattr(webhook, "resolve_prompt", lambda *_a: ("p", PROMPT))
    monkeypatch.setattr(webhook, "_queue_committed_dashboard_inputs", lambda: None)
    monkeypatch.setattr(webhook, "_webhook_max_attempts", 5)
    health, metadata = Mock(), Mock()
    monkeypatch.setattr(webhook, "_record_webhook_feed_health", health)
    monkeypatch.setattr(webhook.DB, "set_app_meta", metadata)
    task = {
        "feed_url": FEED,
        "entries": [
            {"title": TITLE, "url": LINK, "content": SUMMARY},
        ],
    }
    assert webhook._persist_webhook_task(task) == "stored"
    claimed = webhook._claim_next_spooled_task(now=time.time() + 100_000)
    assert claimed is not None
    path, envelope = claimed
    assert envelope["attempts"] == 1
    outcome = webhook._execute_claimed_spooled_task(path, envelope)
    assert outcome == webhook._WEBHOOK_PARTIAL and outcome is not True
    current = webhook._load_spooled_task(path)
    assert current["next_entry_index"] == 1
    assert current["progress"] == {"passed": 0, "filtered": 0, "terminal_unscored": 1}
    future = Future()
    future.set_result(outcome)
    assert webhook._webhook_slots.acquire(blocking=False)
    webhook._finish_spooled_task(future, path, envelope["digest"], envelope["owner"])
    with webhook._webhook_inflight_lock:
        webhook._webhook_inflight.clear()
        webhook._webhook_claims.clear()
        webhook._webhook_retry_state.clear()
    assert _pending_files(webhook) == []
    dead = list((webhook._ensure_spool_dir() / "dead-letter").glob("*.json"))
    assert len(dead) == 1
    failed = webhook._load_spooled_task(dead[0])
    assert failed["attempts"] == 1 and failed["last_error"] == webhook._WEBHOOK_PARTIAL
    assert failed.get("score_transport_deferrals", 0) == 0
    assert failed["next_entry_index"] == 1
    assert failed["progress"] == {"passed": 0, "filtered": 0, "terminal_unscored": 1}
    row, = failed["terminal_unscored"]
    assert row["entry_index"] == 0
    assert row["article_key"] == before["article_key"]
    assert row["strategy_version"] == state.cfg["scoring_strategy_version"]
    assert row["outcome"] == processor.SCORE_TERMINAL_UNSCORED_REASON
    assert _record(state.cfg) == before
    assert state.calls == [] and _rows() == ([], [])
    assert len(_processing_rows()) == 1
    assert {row["state"] for row in _processing_rows()} == {"error"}
    health.assert_called_once()
    assert health.call_args.args[0] == FEED and health.call_args.args[-3:] == (0, 0, 1)
    metadata.assert_not_called()
    assert webhook._persist_webhook_task(task) == "duplicate"
    with webhook._locked_spool() as directory:
        assert webhook._load_receipts_unlocked(directory) == {}
