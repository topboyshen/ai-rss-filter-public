import sqlite3
import time

import pytest

from db import database
from db import news_processing as storage


@pytest.fixture
def initialized():
    database.init_db()
    return database


def _processing(processing_id="process-1", **changes):
    return {
        "processing_id": processing_id,
        "link": "https://example.test/article-1",
        "feed_url": "https://example.test/feed",
        "strategy_version": "strategy-1",
        "started_at": time.time() - 1,
        "finished_at": time.time(),
        "outcome": "filtered",
        "state": "completed",
        "model_attempts": 2,
        "result_returned": False,
        **changes,
    }


def _signal(db, row, key="revision-exact", *, score=8, available_at=100, path="llm"):
    # Minimal valid immutable-ledger fixture; no application calls or models.
    with db.get_connection() as conn:
        conn.execute(
            "INSERT INTO backtest_signals (signal_key,link,strategy_version,"
            "available_at,scored_at,recorded_at,score,aggregation_threshold,selected,score_path) "
            "VALUES (?,?,?,?,?,?,?,?,?,?)",
            (key, row["link"], row["strategy_version"], available_at,
             available_at, available_at, score, 7, int(score >= 7), path),
        )
        conn.commit()


def _call(db, processing_id, *, usage="complete", tokens=(10, 2, 12), **changes):
    db.log_llm_call(
        "stage1", "backend", "model", *tokens,
        processing_id=processing_id,
        article_key=db._backtest_signal_key("https://example.test/article-1", "strategy-1"),
        usage_status=usage, **changes,
    )


def test_additive_migration_keeps_every_historical_value(tmp_path, monkeypatch):
    path = tmp_path / "pre-linkage.db"
    monkeypatch.setattr(database, "DB_PATH", str(path))
    with sqlite3.connect(path) as conn:
        conn.execute(
            "CREATE TABLE llm_call_logs (id INTEGER PRIMARY KEY, timestamp INTEGER, "
            "purpose TEXT, backend_name TEXT, model_name TEXT, prompt_tokens INTEGER, "
            "completion_tokens INTEGER, total_tokens INTEGER, estimated_cost REAL, "
            "latency_ms INTEGER, success INTEGER, error_type TEXT, strategy_version TEXT)"
        )
        historical = (7, 1234, "stage1", "old", "old-model", 41, 3, 44, 0.1, 90, 0, "TimeoutError", "old")
        conn.execute("INSERT INTO llm_call_logs VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)", historical)
        database._init_llm_call_logs(conn.cursor())
        storage.init_news_processing_storage(conn.cursor())
        database._init_llm_call_logs(conn.cursor())
        storage.init_news_processing_storage(conn.cursor())
        row = conn.execute("SELECT * FROM llm_call_logs").fetchone()
        assert row[:len(historical)] == historical
        assert row[-2:] == ("", "")
        assert conn.execute("SELECT count(*) FROM news_processing_logs").fetchone()[0] == 0
        indexes = {row[1] for row in conn.execute("PRAGMA index_list(llm_call_logs)")}
        assert "idx_llm_call_logs_processing" in indexes


def test_result_links_exact_revision_without_modifying_ledger(initialized):
    db, row = initialized, _processing()
    _signal(db, row, "repair-1", score=6, available_at=100, path="stage2")
    _signal(db, row, "later-unrelated-revision", score=9, available_at=101)
    with db.get_connection() as conn:
        before = [tuple(item) for item in conn.execute("SELECT * FROM backtest_signals ORDER BY id")]

    assert db.DB.record_news_processing({**row, "signal_key_hint": "repair-1"})
    result = db.DB.get_news_processing_trace(row["processing_id"])["processing"]
    assert result["article_key"] == db._backtest_signal_key(row["link"], row["strategy_version"])
    assert result["signal_key"] == "repair-1"
    assert result["score"] == 6
    assert result["score_path"] == "stage2"
    assert result["aggregation_selected"] == 0
    assert result["signal_association"] == "written_in_processing"
    assert "published" not in result
    assert result["outcome"] == "filtered"
    assert result["result_returned"] == 0
    with db.get_connection() as conn:
        assert before == [tuple(item) for item in conn.execute("SELECT * FROM backtest_signals ORDER BY id")]


@pytest.mark.parametrize("hint", ["not-committed", "wrong-link", "wrong-contract"])
def test_missing_or_mismatched_exact_hint_never_uses_latest(initialized, hint):
    db, row = initialized, _processing(signal_key_hint=hint)
    _signal(db, row, "latest-matching")
    _signal(db, {**row, "link": "https://example.test/other"}, "wrong-link")
    _signal(db, {**row, "strategy_version": "other"}, "wrong-contract")
    assert db.record_news_processing(row)
    result = db.get_news_processing_trace(row["processing_id"])["processing"]
    assert result["signal_key"] is None
    assert result["score"] is None
    assert result["signal_association"] == "none"


def test_no_hint_is_explicitly_existing_at_completion(initialized):
    db, row = initialized, _processing(reuse_kind="cache")
    _signal(db, row, "earlier", available_at=100)
    _signal(db, row, "existing", score=6, available_at=101)
    assert db.record_news_processing(row)
    result = db.get_news_processing_trace(row["processing_id"])["processing"]
    assert result["signal_key"] == "existing"
    assert result["signal_association"] == "existing_at_completion"
    assert result["reuse_kind"] == "cache"


@pytest.mark.parametrize("state", ["error", "deferred"])
def test_uncompleted_processing_cannot_claim_signal(initialized, state):
    db, row = initialized, _processing(state=state, error_type="OperationalError", signal_key_hint="present")
    _signal(db, row, "present")
    assert db.record_news_processing(row)
    result = db.get_news_processing_trace(row["processing_id"])["processing"]
    assert result["signal_key"] is result["score"] is result["aggregation_selected"] is None
    assert result["signal_association"] == "none"
    assert result["error_type"] == "OperationalError"


def test_duplicate_identity_preserves_original_result(initialized):
    db, original = initialized, _processing()
    assert db.record_news_processing(original)
    before = db.get_news_processing_trace(original["processing_id"])["processing"]
    assert not db.record_news_processing({**original, "state": "error", "outcome": "changed"})
    assert db.get_news_processing_trace(original["processing_id"])["processing"] == before


def test_call_trace_preserves_unknown_and_partial_usage(initialized):
    db = initialized
    _call(db, "trace", usage="complete", tokens=(10, 2, 12), attempt_kind="initial")
    _call(db, "trace", usage="partial", tokens=(20, 0, 0), attempt_kind="repair")
    _call(db, "trace", usage="unavailable", tokens=(0, 0, 0), attempt_kind="fallback", success=False)
    _call(db, "trace", usage="legacy", tokens=(9, 9, 18))
    _call(db, "other", tokens=(999, 1, 1000))
    db.log_llm_call("summary", "backend", "model", total_tokens=100, usage_status="complete")
    assert db.record_news_processing(_processing("trace", model_attempts=4))
    trace = db.get_news_processing_trace("trace")
    assert trace["found"] and trace["calls_linked"]
    assert len(trace["calls"]) == 4
    assert trace["usage"]["complete_calls"] == 1
    assert trace["usage"]["partial_calls"] == 1
    assert trace["usage"]["unknown_calls"] == 2
    assert trace["usage"]["unknown_or_partial"] is True
    assert trace["usage"]["reported_prompt_tokens"] == 30
    assert trace["usage"]["reported_completion_tokens"] == 2
    assert trace["usage"]["reported_total_tokens"] == 12
    assert trace["usage"]["complete_total_tokens"] == 12
    assert trace["usage"]["processing_usage_complete"] is False
    assert trace["usage"]["unlogged_attempts"] == 0
    assert trace["unassociated_history"] == "not_attributable"
    assert [row["attempt_kind"] for row in trace["calls"]] == ["initial", "repair", "fallback", ""]


def test_missing_call_rows_are_not_treated_as_zero_usage(initialized):
    assert initialized.record_news_processing(_processing("missing-logs", model_attempts=2))
    usage = initialized.get_news_processing_trace("missing-logs")["usage"]
    assert usage["reported_total_tokens"] == 0
    assert usage["unlogged_attempts"] == 2
    assert usage["unknown_or_partial"] is True
    assert usage["processing_usage_complete"] is False


def test_all_calls_with_complete_usage_can_be_fully_accounted(initialized):
    _call(initialized, "complete")
    assert initialized.record_news_processing(_processing("complete", model_attempts=1))
    usage = initialized.get_news_processing_trace("complete")["usage"]
    assert usage["reported_total_tokens"] == 12
    assert usage["unlogged_attempts"] == 0
    assert usage["processing_usage_complete"] is True


def test_orphaned_calls_are_visible_without_invented_final_result(initialized):
    _call(initialized, "calls-only")
    trace = initialized.get_news_processing_trace("calls-only")
    assert not trace["found"]
    assert trace["processing"] is None
    assert trace["calls_linked"]
    assert len(trace["calls"]) == 1
    empty = initialized.get_news_processing_trace("")
    assert empty["status"] == "invalid_processing_id"
    assert empty["calls"] == []


def test_trace_limit_is_hard_capped_and_usage_scope_is_explicit(initialized):
    with initialized.get_connection() as conn:
        conn.executemany(
            "INSERT INTO llm_call_logs(timestamp,processing_id,total_tokens,usage_status) "
            "VALUES (1,'many',1,'complete')", [()] * 202,
        )
        conn.commit()
    trace = initialized.get_news_processing_trace("many", limit=100000)
    assert len(trace["calls"]) == 200
    assert trace["truncated"] is True
    assert trace["usage"]["reported_total_tokens"] == 200
    assert trace["usage"]["scope"] == "returned_call_rows_only"
    assert len(initialized.get_news_processing_trace("many", limit=2)["calls"]) == 2


def test_linkage_lookup_uses_indexes_and_skips_unassociated_history(initialized):
    with initialized.get_connection() as conn:
        index_sql = conn.execute(
            "SELECT sql FROM sqlite_master WHERE name='idx_llm_call_logs_processing'"
        ).fetchone()[0]
        assert "WHERE processing_id <> ''" in index_sql
        plan = conn.execute(
            "EXPLAIN QUERY PLAN SELECT * FROM llm_call_logs WHERE processing_id = ? "
            "AND processing_id <> '' ORDER BY id LIMIT ?", ("trace", 201),
        ).fetchall()
        assert any("idx_llm_call_logs_processing" in str(row[3]) for row in plan)
        signal_plan = conn.execute(
            "EXPLAIN QUERY PLAN SELECT signal_key,score,score_path,selected FROM backtest_signals "
            "WHERE strategy_version=? AND link=? ORDER BY available_at DESC,id DESC LIMIT 1",
            ("strategy-1", "https://example.test/article-1"),
        ).fetchall()
        assert any("idx_backtest_signals_link_revision" in str(row[3]) for row in signal_plan)


def test_result_write_python_lock_budget_is_bounded_and_quiet(initialized, caplog):
    initialized.db_lock.acquire()
    start = time.monotonic()
    try:
        assert not initialized.record_news_processing(_processing(link="https://secret.test/api-key"))
    finally:
        elapsed = time.monotonic() - start
        initialized.db_lock.release()
    assert elapsed < 0.8
    assert "diagnostic insert skipped" in caplog.text
    assert "secret.test" not in caplog.text
    assert "api-key" not in caplog.text
    assert not initialized.get_news_processing_trace("process-1")["found"]


def test_sqlite_busy_write_is_bounded_and_business_db_is_usable(initialized):
    blocker = sqlite3.connect(initialized.DB_PATH)
    blocker.execute("BEGIN IMMEDIATE")
    start = time.monotonic()
    try:
        assert not initialized.record_news_processing(_processing())
    finally:
        elapsed = time.monotonic() - start
        blocker.rollback()
        blocker.close()
    assert elapsed < 0.8
    assert initialized.record_news_processing(_processing())
    assert initialized.get_news_processing_trace("process-1")["found"]


@pytest.mark.parametrize("changes", [
    {"started_at": float("nan")}, {"finished_at": float("inf")},
    {"state": "source text"}, {"finished_at": 0}, {"model_attempts": "bad"},
])
def test_invalid_diagnostic_row_never_raises(initialized, changes):
    assert not initialized.record_news_processing(_processing(**changes))


def test_storage_failure_isolated_and_does_not_leak_exception(initialized, monkeypatch, caplog):
    def fail(*args, **kwargs):
        raise OSError("article text and private api-key")

    monkeypatch.setattr(storage, "_connection", fail)
    assert not initialized.record_news_processing(_processing())
    assert "private api-key" not in caplog.text
    assert initialized.get_news_processing_trace("process-1")["status"] == "unavailable"
    assert initialized.db_lock.acquire(blocking=False)
    initialized.db_lock.release()


def test_close_failure_never_escapes_or_leaves_business_lock_held(initialized, monkeypatch, caplog):
    original_connection = storage._connection

    class CloseFailure:
        def __init__(self, inner):
            self.inner = inner

        def __getattr__(self, name):
            return getattr(self.inner, name)

        def close(self):
            self.inner.close()
            raise OSError("private source key in close error")

    monkeypatch.setattr(
        storage, "_connection", lambda *args, **kwargs: CloseFailure(original_connection(*args, **kwargs)),
    )
    assert initialized.record_news_processing(_processing())
    assert initialized.db_lock.acquire(blocking=False)
    initialized.db_lock.release()
    assert initialized.get_news_processing_trace("process-1")["found"]
    assert "private source key" not in caplog.text


def test_current_cleanup_does_not_change_diagnostic_retention(initialized):
    ancient = time.time() - 400 * 86400
    assert initialized.record_news_processing(_processing(started_at=ancient - 1, finished_at=ancient))
    with initialized.get_connection() as conn:
        conn.execute(
            "INSERT INTO llm_call_logs(timestamp,processing_id,total_tokens,usage_status) "
            "VALUES (1,'process-1',10,'complete')"
        )
        conn.commit()
        initialized.cleanup_old_entries(conn, cfg={})
    trace = initialized.get_news_processing_trace("process-1")
    assert trace["found"]
    assert len(trace["calls"]) == 1


def test_unlinked_direct_calls_remain_unlinked_and_do_not_change_existing_stats(initialized):
    initialized.log_llm_call("direct", "backend", "model", 10, 2, 12, usage_status="complete")
    with initialized.get_connection() as conn:
        row = dict(conn.execute("SELECT * FROM llm_call_logs").fetchone())
    assert row["processing_id"] == row["article_key"] == ""
    assert initialized.get_stats()[1] == 1
    assert initialized.get_llm_cost_stats(hours=1)["calls"] == 1


def test_signal_hint_only_runs_for_an_insert_and_never_breaks_business(initialized, monkeypatch):
    from services import news_processing_trace

    signals = []
    monkeypatch.setattr(
        news_processing_trace, "record_news_processing_signal",
        lambda *identity: signals.append(identity),
    )
    signal = {
        "link": "https://example.test/real-write", "raw_title": "公司公告",
        "strategy_version": "strategy-1", "mapping_version": "asset-map:test",
        "score": 8, "score_path": "llm", "aggregation_threshold": 7,
    }
    assert initialized.store_backtest_signal(signal)
    assert signals == [(
        signal["link"], signal["strategy_version"],
        initialized._backtest_signal_key(signal["link"], signal["strategy_version"]),
    )]
    assert not initialized.store_backtest_signal(signal)
    assert len(signals) == 1

    def fail(*args):
        raise RuntimeError("private source text")

    monkeypatch.setattr(news_processing_trace, "record_news_processing_signal", fail)
    assert initialized.store_backtest_signal({**signal, "link": "https://example.test/other-write"})
