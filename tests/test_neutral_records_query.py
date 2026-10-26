"""Real isolated SQLite coverage for the bounded safe-neutral record reader."""

from contextlib import contextmanager
import sqlite3

import pytest

from db import database, neutral_records
from scoring_provenance import NON_SUBSTANTIVE_SCORE_PATHS


NOW = 1_800_000_000
CONTRACT = "contract:current"
_COLUMN_TYPES = {
    "link": "TEXT PRIMARY KEY", "title": "TEXT", "raw_title": "TEXT",
    "feed_url": "TEXT", "created_at": "INTEGER", "score_path": "TEXT",
    "strategy_version": "TEXT COLLATE NOCASE", "safe_neutral_reason": "TEXT",
    "scored_at": "INTEGER",
}


def _create_cache(path, *, omit=()):
    columns = {name: kind for name, kind in _COLUMN_TYPES.items() if name not in omit}
    with sqlite3.connect(path) as conn:
        conn.execute("CREATE TABLE news_cache (" + ", ".join(
            f"{name} {kind}" for name, kind in columns.items()
        ) + ")")


def _insert(path, **overrides):
    row = {
        "link": "https://example.test/article", "title": "记录标题",
        "raw_title": "原始标题", "feed_url": "https://example.test/feed",
        "created_at": NOW - 1, "score_path": "llm_safe_neutral_grounding",
        "strategy_version": CONTRACT, "safe_neutral_reason": "facts",
        "scored_at": NOW - 1,
        **overrides,
    }
    with sqlite3.connect(path) as conn:
        columns = {item[1] for item in conn.execute("PRAGMA table_info(news_cache)")}
        row = {key: value for key, value in row.items() if key in columns}
        conn.execute(
            "INSERT INTO news_cache (" + ", ".join(row) + ") VALUES ("
            + ", ".join("?" for _ in row) + ")", tuple(row.values()),
        )


@pytest.fixture
def cache(isolate_ai_filter_db):
    _create_cache(isolate_ai_filter_db)
    return isolate_ai_filter_db


def test_exact_contract_and_safe_neutral_paths_are_required(cache):
    for index, path in enumerate(sorted(NON_SUBSTANTIVE_SCORE_PATHS)):
        _insert(cache, link=f"safe-{index}", score_path=path)
    for index, path in enumerate(("llm", "stage2", "fallback_local", "parse_error",
                                  "quality_quarantine", "similarity", "unknown", "")):
        _insert(cache, link=f"excluded-{index}", score_path=path)
    for version in ("contract:old", "", "CONTRACT:CURRENT", "contract:current "):
        _insert(cache, link=f"version-{version}", strategy_version=version)
    result = neutral_records.read_neutral_records(CONTRACT, now=NOW)
    assert result["available"] is True
    assert {row["score_path"] for row in result["records"]} == NON_SUBSTANTIVE_SCORE_PATHS
    assert all(row["link"].startswith("safe-") for row in result["records"])
    assert set(result) == {
        "available", "strategy_version", "hours", "limit", "has_more", "records",
    }
    assert (result["hours"], result["limit"], result["strategy_version"]) == (24, 50, CONTRACT)


def test_window_uses_created_at_including_cutoff_not_score_time(cache):
    _insert(cache, link="boundary", created_at=NOW - 86400, scored_at=1)
    _insert(cache, link="recent", created_at=NOW, scored_at=1)
    _insert(cache, link="expired", created_at=NOW - 86401, scored_at=NOW)
    _insert(cache, link="future", created_at=NOW + 1, scored_at=NOW)
    result = neutral_records.read_neutral_records(CONTRACT, now=NOW)
    assert [row["link"] for row in result["records"]] == ["recent", "boundary"]


@pytest.mark.parametrize("reason", ["theme", "facts", "contract", "initial", "", None,
                                    "<script>secret</script>", "unexpected"])
@pytest.mark.parametrize("path", sorted(NON_SUBSTANTIVE_SCORE_PATHS))
def test_reason_is_whitelisted_and_consistent_with_persisted_path(cache, path, reason):
    _insert(cache, score_path=path, safe_neutral_reason=reason)
    row = neutral_records.read_neutral_records(CONTRACT, now=NOW)["records"][0]
    allowed = {"theme", "facts"} if path.endswith("grounding") else {
        "contract" if path.endswith("contract") else "initial",
    }
    assert row["safe_neutral_reason"] == (reason if reason in allowed else "")
    assert set(row) == {
        "title", "raw_title", "link", "feed_url", "created_at", "score_path",
        "safe_neutral_reason",
    }


def test_legacy_optional_columns_remain_unknown(isolate_ai_filter_db):
    _create_cache(isolate_ai_filter_db, omit=("safe_neutral_reason", "raw_title", "feed_url"))
    _insert(isolate_ai_filter_db, score_path="stage2_safe_neutral_contract")
    result = neutral_records.read_neutral_records(CONTRACT, now=NOW)
    assert result["available"] is True
    row = result["records"][0]
    assert (row["safe_neutral_reason"], row["raw_title"], row["feed_url"]) == ("", "", "")


def test_score_path_normalization_preserves_compatible_reason(cache):
    _insert(cache, score_path=" LLM_SAFE_NEUTRAL_GROUNDING ", safe_neutral_reason=" facts ")
    row = neutral_records.read_neutral_records(CONTRACT, now=NOW)["records"][0]
    assert row["score_path"] == "llm_safe_neutral_grounding"
    assert row["safe_neutral_reason"] == "facts"


@pytest.mark.parametrize("count, expected_more", [(0, False), (50, False), (51, True)])
def test_latest_fifty_are_stably_sorted_with_limit_plus_one(cache, count, expected_more):
    for index in range(count):
        _insert(cache, link=f"article-{index:03}", created_at=NOW - index // 2)
    result = neutral_records.read_neutral_records(CONTRACT, now=NOW)
    assert result["available"] is True
    assert result["has_more"] is expected_more
    assert [row["link"] for row in result["records"]] == [
        f"article-{index:03}" for index in range(min(count, 50))
    ]


def test_read_only_connection_and_no_count_or_persistent_writes(cache, monkeypatch):
    _insert(cache)
    before = cache.read_bytes()
    statements = []
    actual_connect = neutral_records.get_read_only_connection

    @contextmanager
    def observed_connection():
        with actual_connect() as conn:
            assert conn.execute("PRAGMA query_only").fetchone()[0] == 1
            with pytest.raises(sqlite3.OperationalError, match="readonly"):
                conn.execute("DELETE FROM news_cache")
            conn.rollback()
            conn.set_trace_callback(statements.append)
            yield conn
            assert conn.total_changes == 0

    def forbidden(*_args, **_kwargs):
        pytest.fail("reader invoked a writer or initializer")

    monkeypatch.setattr(neutral_records, "get_read_only_connection", observed_connection)
    monkeypatch.setattr(database, "get_connection", forbidden)
    monkeypatch.setattr(database, "init_db", forbidden)
    assert neutral_records.read_neutral_records(CONTRACT, now=NOW)["available"] is True
    assert cache.read_bytes() == before
    assert all("COUNT(" not in statement.upper() for statement in statements)
    assert any("LIMIT 51" in statement for statement in statements)
    assert all(statement.split()[0] in {"PRAGMA", "BEGIN", "SELECT"} for statement in statements)


@pytest.mark.parametrize("column", ["title", "link", "created_at", "score_path", "strategy_version"])
def test_missing_required_column_is_unavailable(isolate_ai_filter_db, column):
    _create_cache(isolate_ai_filter_db, omit=(column,))
    result = neutral_records.read_neutral_records(CONTRACT, now=NOW)
    assert result["available"] is False
    assert result["records"] == []


def test_missing_table_is_unavailable(isolate_ai_filter_db):
    with sqlite3.connect(isolate_ai_filter_db) as conn:
        conn.execute("CREATE TABLE unrelated (id INTEGER)")
    assert neutral_records.read_neutral_records(CONTRACT, now=NOW)["available"] is False


def test_missing_database_is_not_created(isolate_ai_filter_db):
    assert not isolate_ai_filter_db.exists()
    result = neutral_records.read_neutral_records(CONTRACT, now=NOW)
    assert result["available"] is False
    assert result["records"] == []
    assert not isolate_ai_filter_db.exists()


def test_corrupt_database_is_unavailable_and_untouched(isolate_ai_filter_db):
    payload = b"not a sqlite database"
    isolate_ai_filter_db.write_bytes(payload)
    result = neutral_records.read_neutral_records(CONTRACT, now=NOW)
    assert result["available"] is False
    assert result["records"] == []
    assert isolate_ai_filter_db.read_bytes() == payload


@pytest.mark.parametrize("contract", ["", " ", None])
def test_absent_contract_is_unavailable_without_opening_database(contract, monkeypatch):
    def forbidden():
        pytest.fail("absent contract should not open a database")

    monkeypatch.setattr(neutral_records, "get_read_only_connection", forbidden)
    assert neutral_records.read_neutral_records(contract, now=NOW)["available"] is False


def test_sql_injection_in_contract_does_not_widen_result(cache):
    _insert(cache)
    result = neutral_records.read_neutral_records("' OR 1=1 --", now=NOW)
    assert result["available"] is True
    assert result["records"] == []


def test_instruction_budget_interrupts_a_scan_and_returns_unavailable(cache, monkeypatch):
    for index in range(100):
        _insert(cache, link=f"article-{index}")
    monkeypatch.setattr(neutral_records, "_MAX_READ_STEPS", 100)
    monkeypatch.setattr(neutral_records, "_PROGRESS_INTERVAL", 100)
    result = neutral_records.read_neutral_records(CONTRACT, now=NOW)
    assert result["available"] is False
    assert result["records"] == []


def test_time_budget_interrupts_a_scan_and_returns_unavailable(cache, monkeypatch):
    for index in range(100):
        _insert(cache, link=f"article-{index}")
    monkeypatch.setattr(neutral_records, "_READ_SECONDS", -1)
    monkeypatch.setattr(neutral_records, "_PROGRESS_INTERVAL", 100)
    result = neutral_records.read_neutral_records(CONTRACT, now=NOW)
    assert result["available"] is False
    assert result["records"] == []
