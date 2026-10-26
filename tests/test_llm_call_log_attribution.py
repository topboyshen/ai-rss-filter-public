import sqlite3

import pytest

from db import database


def _rows():
    with database.get_connection() as conn:
        conn.row_factory = sqlite3.Row
        return [dict(row) for row in conn.execute("SELECT * FROM llm_call_logs ORDER BY id")]


def test_fresh_llm_call_log_schema_has_attribution_defaults():
    database.init_db()
    with database.get_connection() as conn:
        columns = {
            row[1]: row for row in conn.execute("PRAGMA table_info(llm_call_logs)")
        }
    assert columns["request_id"][4] == "''"
    assert columns["attempt_kind"][4] == "''"
    assert columns["response_mode"][4] == "''"
    assert columns["format_retry"][4] == "0"
    assert columns["usage_status"][4] == "'legacy'"


def test_llm_call_log_migration_preserves_historical_usage(tmp_path, monkeypatch):
    db_path = tmp_path / "legacy.db"
    monkeypatch.setattr(database, "DB_PATH", str(db_path))
    historical = (
        1234, "stage1", "legacy-backend", "legacy-model", 101, 23, 124,
        0.0124, 987, 0, "TimeoutError", "legacy-strategy",
    )
    with sqlite3.connect(db_path) as conn:
        conn.execute(
            "CREATE TABLE llm_call_logs ("
            "id INTEGER PRIMARY KEY AUTOINCREMENT, timestamp INTEGER NOT NULL, "
            "purpose TEXT DEFAULT '', backend_name TEXT DEFAULT '', model_name TEXT DEFAULT '', "
            "prompt_tokens INTEGER DEFAULT 0, completion_tokens INTEGER DEFAULT 0, "
            "total_tokens INTEGER DEFAULT 0, estimated_cost REAL DEFAULT 0, "
            "latency_ms INTEGER DEFAULT 0, success INTEGER DEFAULT 1, "
            "error_type TEXT DEFAULT '', strategy_version TEXT DEFAULT '')"
        )
        conn.execute(
            "INSERT INTO llm_call_logs (timestamp, purpose, backend_name, model_name, "
            "prompt_tokens, completion_tokens, total_tokens, estimated_cost, latency_ms, "
            "success, error_type, strategy_version) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            historical,
        )

    database.init_db()
    database.init_db()

    rows = _rows()
    assert len(rows) == 1
    row = rows[0]
    historical_columns = (
        "timestamp", "purpose", "backend_name", "model_name", "prompt_tokens",
        "completion_tokens", "total_tokens", "estimated_cost", "latency_ms",
        "success", "error_type", "strategy_version",
    )
    assert tuple(row[column] for column in historical_columns) == historical
    assert row["id"] == 1
    assert row["request_id"] == row["attempt_kind"] == row["response_mode"] == ""
    assert row["format_retry"] == 0
    assert row["usage_status"] == "legacy"
    assert database.get_stats()[1] == 0


@pytest.mark.parametrize(
    "attempt_kind,response_mode,format_retry,usage_status,success,tokens",
    [
        ("initial", "json_schema", False, "unavailable", False, (0, 0, 0)),
        ("fallback", "json_object", True, "partial", True, (100, 0, 0)),
        ("repair", "prompt_only", False, "complete", True, (100, 20, 120)),
        ("direct", "json_object", False, "partial", False, (0, 20, 0)),
    ],
)
def test_llm_call_log_stores_success_and_failure_metadata_once(
    attempt_kind, response_mode, format_retry, usage_status, success, tokens,
):
    database.init_db()
    before = database.get_stats()[1]

    database.log_llm_call(
        "stage1", "backend", "model", *tokens, 0.0, 42, success,
        "" if success else "TimeoutError", "strategy",
        request_id="request-1", attempt_kind=attempt_kind,
        response_mode=response_mode, format_retry=format_retry,
        usage_status=usage_status,
    )

    rows = _rows()
    assert len(rows) == 1
    row = rows[0]
    assert row["request_id"] == "request-1"
    assert row["attempt_kind"] == attempt_kind
    assert row["response_mode"] == response_mode
    assert row["format_retry"] == int(format_retry)
    assert row["usage_status"] == usage_status
    assert row["success"] == int(success)
    assert row["error_type"] == ("" if success else "TimeoutError")
    assert tuple(row[key] for key in ("prompt_tokens", "completion_tokens", "total_tokens")) == tokens
    assert database.get_stats()[1] == before + 1
    assert database.get_llm_cost_stats(hours=1)["calls"] == 1


def test_same_request_keeps_each_physical_attempt_and_counter():
    database.init_db()
    before = database.get_stats()[1]
    for attempt_kind, response_mode, format_retry in (
        ("initial", "json_schema", False),
        ("initial", "json_object", True),
        ("repair", "prompt_only", False),
    ):
        database.log_llm_call(
            "stage1", "backend", "model", request_id="one-logical-request",
            attempt_kind=attempt_kind, response_mode=response_mode,
            format_retry=format_retry, usage_status="unavailable",
        )

    assert len(_rows()) == 3
    assert database.get_stats()[1] == before + 3
    assert database.get_llm_cost_stats(hours=1)["calls"] == 3


def test_existing_positional_callers_keep_legacy_attribution():
    database.init_db()
    database.log_llm_call(
        "summary", "backend", "model", 11, 4, 15, 0.25, 50, False,
        "RuntimeError", "old-strategy",
    )
    database.log_llm_call("digest", "backend", "model")

    rows = _rows()
    assert len(rows) == 2
    assert tuple(rows[0][key] for key in ("prompt_tokens", "completion_tokens", "total_tokens")) == (11, 4, 15)
    assert rows[0]["estimated_cost"] == 0.25
    for row in rows:
        assert row["request_id"] == row["attempt_kind"] == row["response_mode"] == ""
        assert row["format_retry"] == 0
        assert row["usage_status"] == "legacy"
    assert database.get_stats()[1] == 2


def test_attribution_values_are_bounded_and_new_arguments_are_keyword_only():
    database.init_db()
    database.log_llm_call(
        "stage1", "backend", "model", request_id="x" * 200,
        attempt_kind="source text", response_mode="api key",
        usage_status="estimated from source",
    )
    row = _rows()[0]
    assert row["request_id"] == "x" * 128
    assert row["attempt_kind"] == row["response_mode"] == ""
    assert row["usage_status"] == "legacy"
    with pytest.raises(TypeError):
        database.log_llm_call(
            "stage1", "backend", "model", 0, 0, 0, 0.0, 0, True, "", "", "request",
        )
    assert len(_rows()) == 1
    assert database.get_stats()[1] == 1


def test_cost_stats_expose_unknown_and_partial_usage_without_inference():
    database.init_db()
    assert database.get_llm_cost_stats(hours=1)["unknown_usage_calls"] == 0
    assert database.get_llm_cost_stats(hours=1)["partial_usage_calls"] == 0
    for usage_status, tokens, cost, success in (
        ("legacy", (7, 3, 10), 0.1, True),
        ("unavailable", (0, 0, 0), 0.0, False),
        ("partial", (20, 0, 0), 0.0, True),
        ("complete", (30, 10, 40), 0.4, True),
    ):
        database.log_llm_call(
            "stage1", "backend", "model", *tokens, cost, 50, success,
            strategy_version="current", usage_status=usage_status,
        )
    database.log_llm_call(
        "summary", "other-backend", "other-model", total_tokens=99,
        strategy_version="other", usage_status="legacy",
    )

    stats = database.get_llm_cost_stats(
        hours=1, cfg={"scoring_strategy_version": "current"},
    )
    assert stats["calls"] == 4
    assert stats["unknown_usage_calls"] == 2
    assert stats["partial_usage_calls"] == 1
    assert stats["total_tokens"] == 50
    assert stats["estimated_cost"] == 0.5
    assert stats["failures"] == 1
    assert stats["avg_latency_ms"] == 50.0
    assert stats["by_model"] == [("model", 4, 50, 0.5)]
    assert stats["by_purpose"] == [("stage1", 4, 50, 0.5)]

    all_stats = database.get_llm_cost_stats(hours=1)
    assert all_stats["unknown_usage_calls"] == 3
    assert all_stats["partial_usage_calls"] == 1
    assert all_stats["total_tokens"] == 149


def test_cost_window_is_rolling_bounded_and_can_include_all_contracts(monkeypatch):
    database.init_db()
    now = 1_791_600_000
    monkeypatch.setattr(database.time, "time", lambda: now)
    rows = [
        (now - 86401, "old", 999),
        (now - 86400, "old", 999),
        (now - 86399, "old", 20),
        (now, "current", 30),
        (now + 1, "current", 999),
    ]
    with database.get_connection() as conn:
        conn.executemany(
            "INSERT INTO llm_call_logs (timestamp,strategy_version,total_tokens,usage_status) "
            "VALUES (?,?,?,'complete')", rows,
        )
        conn.commit()
    stats = database.get_llm_cost_stats(
        hours=24, cfg={"scoring_strategy_version": "current"}, all_strategies=True,
    )
    assert stats["calls"] == 2
    assert stats["total_tokens"] == 50
    assert stats["all_strategies"] is True
    assert stats["window_hours"] == 24
    assert stats["window_start_utc"] == "2026-10-09T02:40:00Z"
    assert stats["window_end_utc"] == "2026-10-10T02:40:00Z"
    assert stats["budget_enforced"] is False
    filtered = database.get_llm_cost_stats(
        hours=24, cfg={"scoring_strategy_version": "current"},
    )
    assert filtered["calls"] == 1
    assert filtered["total_tokens"] == 30
