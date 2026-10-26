"""A durable completed decision is retried as a write, never a paid score."""
import sqlite3
import time
from types import SimpleNamespace

import pytest

from core import processor
from services import natural_scoring
from services import natural_score_continuations as store
from tests.test_natural_score_continuation import (
    LINK, _make_due, _record, _rows, pipeline,  # noqa: F401
)


@pytest.mark.parametrize("terminal_neutral", [False, True])
@pytest.mark.parametrize("cache_rejected", [False, True])
def test_restart_after_failed_final_write_only_retries_frozen_decision(
    monkeypatch, pipeline, terminal_neutral, cache_rejected,  # noqa: F811
):
    p = pipeline
    p.cfg["cache_rejected_entries"] = cache_rejected
    assert p.run() == (None, processor.SCORE_DEFERRED_REASON)
    if terminal_neutral:
        _make_due(p.cfg)
        assert p.resume()["deferred"] == 1
    else:
        p.control["mode"] = "recovered"
    _make_due(p.cfg)
    name = "store_backtest_signal" if terminal_neutral and not cache_rejected else "set_cached_entry"
    real_write = getattr(processor.DB, name)

    def fail(*_args, **_kwargs):
        raise sqlite3.OperationalError("database is locked")

    monkeypatch.setattr(processor.DB, name, fail)
    assert p.resume()["errors"] == 1
    paid = len(p.calls)
    saved = _record(p.cfg)
    assert saved["state"] == "commit_pending" and saved["frozen_result"]["result"]["score"] == (
        4.0 if terminal_neutral else 8.0
    )
    assert _rows() == ([], [])
    processor._transport_retry_counts.clear()
    processor._transport_pending_writes.clear()
    p.scorer.clear_runtime_cache()
    monkeypatch.setattr(processor.DB, name, real_write)
    _make_due(p.cfg)
    assert p.resume()["completed"] == 1 and len(p.calls) == paid
    cache, signals = _rows()
    assert len(signals) == 1
    assert len(cache) == int(cache_rejected or not terminal_neutral)
    assert signals[0]["score"] == (4.0 if terminal_neutral else 8.0)
    assert _record(p.cfg)["state"] == "completed"


def test_committed_but_unacknowledged_write_is_reused_after_restart(monkeypatch, pipeline):  # noqa: F811
    p = pipeline
    assert p.run()[1] == processor.SCORE_DEFERRED_REASON
    p.control["mode"] = "recovered"
    _make_due(p.cfg)
    real_write = processor.DB.set_cached_entry

    def write_then_fail(*args, **kwargs):
        real_write(*args, **kwargs)
        raise sqlite3.OperationalError("acknowledgement lost")

    monkeypatch.setattr(processor.DB, "set_cached_entry", write_then_fail)
    assert p.resume()["errors"] == 1
    before, paid = _rows(), len(p.calls)
    processor._transport_retry_counts.clear()
    processor._transport_pending_writes.clear()
    p.scorer.clear_runtime_cache()
    _make_due(p.cfg)
    assert p.resume()["completed"] == 1
    assert _rows() == before and len(p.calls) == paid
    assert _record(p.cfg)["state"] == "completed"


def test_normal_unjournaled_io_failure_is_not_mislabelled_as_deferral(pipeline):  # noqa: F811
    p = pipeline

    def broken(*_args):
        raise OSError("ordinary failure")

    from tests.test_natural_score_continuation import FEED, PROMPT, _entry

    with pytest.raises(OSError, match="ordinary failure"):
        natural_scoring.run_with_continuation(
            _entry(), PROMPT, p.cfg, FEED, "", broken, capture=True,
        )
    assert _record(p.cfg) is None and _rows() == ([], [])


def test_not_due_journal_does_not_fill_process_local_retry_registry(pipeline):  # noqa: F811
    p = pipeline
    assert p.run()[1] == processor.SCORE_DEFERRED_REASON
    before = _record(p.cfg)
    processor._transport_retry_counts.clear()
    for _ in range(5):
        assert p.run(persist=False)[1] == processor.SCORE_DEFERRED_REASON
    assert len(p.calls) == 2 and processor._transport_retry_counts == {}
    assert _record(p.cfg) == before  # Duplicate arrivals cannot postpone due time forever.
    _make_due(p.cfg)
    p.control["mode"] = "recovered"
    assert p.resume()["completed"] == 1 and len(p.calls) == 3


@pytest.mark.parametrize("expire_via_sweep", [False, True])
def test_expired_frozen_result_cannot_be_written_by_webhook_or_legacy_memory(
    monkeypatch, pipeline, expire_via_sweep,  # noqa: F811
):
    p = pipeline
    assert p.run()[1] == processor.SCORE_DEFERRED_REASON
    _make_due(p.cfg)
    assert p.resume()["deferred"] == 1
    _make_due(p.cfg)
    real_write = processor.DB.set_cached_entry

    def fail(*_args, **_kwargs):
        raise sqlite3.OperationalError("database is locked")

    monkeypatch.setattr(processor.DB, "set_cached_entry", fail)
    assert p.resume()["errors"] == 1
    assert processor._transport_pending_writes
    paid = len(p.calls)
    later = time.time() + store.TTL_SECONDS + 1
    monkeypatch.setattr(store, "time", SimpleNamespace(time=lambda: later))
    monkeypatch.setattr(natural_scoring, "time", SimpleNamespace(time=lambda: later))
    if expire_via_sweep:
        assert store.pending(p.cfg) == []
    monkeypatch.setattr(processor.DB, "set_cached_entry", real_write)
    assert p.run(persist=False) == (None, processor.SCORE_TERMINAL_UNSCORED_REASON)
    assert len(p.calls) == paid and _rows() == ([], [])
    assert _record(p.cfg)["state"] == "expired"
    assert processor._transport_pending_writes == {}
