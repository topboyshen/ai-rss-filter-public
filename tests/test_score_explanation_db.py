import sqlite3
import time

import pytest

from db import database
from db.score_explanation import (
    EXPLANATION_FIELDS,
    TOKEN_FIELD,
    init_score_explanation_storage,
)


@pytest.fixture
def explained_db(tmp_path, monkeypatch):
    monkeypatch.setattr(database, "DB_PATH", str(tmp_path / "explained.db"))
    database.init_db()
    return database


def _explanation(score=8, raw=9, threshold=6):
    return dict(
        zip(
            EXPLANATION_FIELDS,
            (raw, score - raw, score - threshold, None, None, threshold),
        )
    )


def _signal(link="https://example.com/explained", score=8, **overrides):
    now = int(time.time())
    return {
        "link": link,
        "raw_title": "国内经济政策",
        "score": score,
        "score_path": "llm",
        "direction": "中性",
        "score_confidence": 0.7,
        "strategy_version": "explanation-test",
        "mapping_version": "asset-map:test",
        "available_at": now,
        "scored_at": now,
        "aggregation_threshold": 7,
        **_explanation(score),
        **overrides,
    }


def _cache(db, signal, explanation=None):
    return db.set_cached_entry(
        signal["link"],
        "国内经济政策",
        "国内经济政策",
        signal["score"],
        "宏观",
        "政策影响待观察",
        score_path=signal["score_path"],
        strategy_version=signal["strategy_version"],
        score_confidence=signal["score_confidence"],
        direction=signal["direction"],
        backtest_signal=signal,
        score_explanation=explanation
        if explanation is not None
        else {field: signal.get(field) for field in EXPLANATION_FIELDS},
    )


def _stored(db):
    with db.get_connection() as conn:
        cache = dict(conn.execute("SELECT * FROM news_cache").fetchone())
        ledger = dict(
            conn.execute(
                "SELECT * FROM backtest_signals ORDER BY id DESC LIMIT 1"
            ).fetchone()
        )
    return cache, ledger


@pytest.mark.parametrize("stored_version", [6, 7])
def test_v7_migration_is_nullable_and_repairs_legacy_numeric_version(stored_version):
    conn = sqlite3.connect(":memory:")
    cursor = conn.cursor()
    database._init_app_meta(cursor)
    database._init_news_cache(cursor)
    database._init_backtest_signals(cursor)
    cursor.execute("INSERT INTO news_cache(link,title,score) VALUES ('old','old',8)")
    cursor.execute(
        "INSERT INTO app_meta VALUES ('schema_version',?)", (str(stored_version),)
    )
    database._run_migrations(conn, cursor)
    init_score_explanation_storage(cursor)
    database._run_migrations(conn, cursor)
    init_score_explanation_storage(cursor)
    assert (
        cursor.execute("SELECT v FROM app_meta WHERE k='schema_version'").fetchone()[0]
        == "7"
    )
    assert (
        cursor.execute(
            "SELECT " + ",".join(EXPLANATION_FIELDS) + " FROM news_cache"
        ).fetchone()
        == (None,) * 6
    )
    for table in ("news_cache", "backtest_signals"):
        columns = {row[1]: row for row in cursor.execute(f"PRAGMA table_info({table})")}
        assert all(columns[field][3:5] == (0, None) for field in EXPLANATION_FIELDS)
    conn.close()


def test_cache_ledger_metadata_and_batch_tuple_keep_the_same_explanation(explained_db):
    db = explained_db
    signal = _signal()
    _cache(db, signal)
    cache, ledger = _stored(db)
    assert {field: cache[field] for field in EXPLANATION_FIELDS} == _explanation()
    assert all(
        cache[field] == ledger[field] for field in (*EXPLANATION_FIELDS, TOKEN_FIELD)
    )
    assert cache[TOKEN_FIELD]
    assert cache["score_confidence"] == ledger["score_confidence"] == 0.7
    cached = db.get_cached_entry(signal["link"])
    assert len(cached) == 19
    assert cached[2] == 8 and cached[7] == signal["strategy_version"]
    assert cached[13:] == tuple(_explanation().values())
    assert db.get_cached_entries_by_links([signal["link"]])[signal["link"]] == cached
    assert all(
        db.get_cached_entry_meta(signal["link"])[field] == cache[field]
        for field in EXPLANATION_FIELDS
    )
    _cache(db, signal)
    repeated_cache, repeated_ledger = _stored(db)
    assert (
        repeated_cache[TOKEN_FIELD]
        == repeated_ledger[TOKEN_FIELD]
        == ledger[TOKEN_FIELD]
    )


def test_missing_explanation_stays_unknown_and_outbox_contract_is_unchanged(
    explained_db,
):
    db = explained_db
    signal = _signal(**dict.fromkeys(EXPLANATION_FIELDS))
    _cache(db, signal)
    cache, ledger = _stored(db)
    assert all(
        cache[field] is None and ledger[field] is None for field in EXPLANATION_FIELDS
    )
    with db.get_connection() as conn:
        outbox_columns = {
            row[1]
            for row in conn.execute("PRAGMA table_info(integration_signal_outbox)")
        }
    assert not set(EXPLANATION_FIELDS) & outbox_columns


def test_cache_ledger_diagnostic_mismatch_is_rejected_before_write(explained_db):
    db = explained_db
    signal = _signal()
    _cache(db, signal)
    before = _stored(db)
    with pytest.raises(ValueError, match="score explanations differ"):
        _cache(db, signal, _explanation(raw=8))
    assert _stored(db) == before


def test_base_replay_cannot_silently_replace_frozen_diagnostic(explained_db):
    db = explained_db
    _cache(db, _signal())
    before = _stored(db)
    with pytest.raises(ValueError, match="replay changes score explanation"):
        _cache(db, _signal(**_explanation(raw=8)))
    assert _stored(db) == before


@pytest.mark.parametrize(
    "assignment",
    [
        "score=7",
        "score_path='radar'",
        "strategy_version='old-writer'",
        "stage1_score=5",
        "score_confidence=0.2",
        "logic='changed inference'",
    ],
)
def test_old_writer_core_change_invalidates_only_mutable_explanation(
    explained_db, assignment
):
    db = explained_db
    _cache(db, _signal())
    _, frozen = _stored(db)
    with db.get_connection() as conn:
        conn.execute("UPDATE news_cache SET " + assignment)
        conn.commit()
    cache, ledger = _stored(db)
    assert all(cache[field] is None for field in (*EXPLANATION_FIELDS, TOKEN_FIELD))
    assert ledger == frozen


def test_origin_only_old_update_does_not_discard_valid_diagnostic(explained_db):
    db = explained_db
    _cache(db, _signal())
    before, _ = _stored(db)
    with db.get_connection() as conn:
        conn.execute("UPDATE news_cache SET created_at=created_at+1")
        conn.commit()
    cache, _ = _stored(db)
    assert all(
        cache[field] == before[field] for field in (*EXPLANATION_FIELDS, TOKEN_FIELD)
    )


def test_explanation_write_failure_rolls_back_cache_ledger_and_outbox(
    explained_db, monkeypatch
):
    db = explained_db
    _cache(db, _signal())
    before = _stored(db)
    with db.get_connection() as conn:
        old_outbox = [
            tuple(row)
            for row in conn.execute("SELECT * FROM integration_signal_outbox")
        ]

    def fail(*_args):
        raise RuntimeError("injected explanation persistence failure")

    monkeypatch.setattr(db, "write_cache_score_explanation", fail)
    with pytest.raises(RuntimeError, match="injected"):
        _cache(db, _signal(link="https://example.com/second"))
    assert _stored(db) == before
    with db.get_connection() as conn:
        assert conn.execute("SELECT COUNT(*) FROM news_cache").fetchone()[0] == 1
        assert conn.execute("SELECT COUNT(*) FROM backtest_signals").fetchone()[0] == 1
        assert [
            tuple(row)
            for row in conn.execute("SELECT * FROM integration_signal_outbox")
        ] == old_outbox


def test_radar_rebases_and_replay_preserves_frozen_base(explained_db):
    db = explained_db
    signal = _signal()
    _cache(db, signal)
    _, base = _stored(db)
    revised = {
        **base,
        "auto_revision": True,
        "score": 9,
        "score_path": "radar",
        "signal_stage": "radar",
    }
    revised.pop("revision_state_key")
    update = {
        "link": signal["link"],
        "title": "雷达新闻",
        "score": 9,
        "score_path": "radar",
        "direction": "中性",
        "score_confidence": 0.7,
        "backtest_signal": revised,
    }
    assert db.update_cached_entry_score_states([update]) == 1
    cache, ledger = _stored(db)
    assert cache["score_adjustment"] == ledger["score_adjustment"] == 0
    assert cache["score_margin"] == ledger["score_margin"] == 3
    assert cache[TOKEN_FIELD] == ledger[TOKEN_FIELD]
    assert db.update_cached_entry_score_states([update]) == 0
    with db.get_connection() as conn:
        assert (
            conn.execute(
                "SELECT score_adjustment FROM backtest_signals WHERE id=?",
                (base["id"],),
            ).fetchone()[0]
            == -1
        )


def test_diagnostic_change_participates_in_revision_identity(explained_db):
    db = explained_db
    signal = _signal()
    assert db.store_backtest_signal(signal)
    revision = {
        **signal,
        "auto_revision": True,
        "signal_stage": "radar",
        "score_path": "radar",
    }
    assert db.store_backtest_signal(revision)
    assert not db.store_backtest_signal(revision)
    assert db.store_backtest_signal({**revision, "raw_score": 8})
    with db.get_connection() as conn:
        previous_key = conn.execute(
            "SELECT revision_state_key FROM backtest_signals ORDER BY id DESC LIMIT 1"
        ).fetchone()[0]
    assert db.store_backtest_signal(
        {**revision, "raw_score": 7, "revision_state_key": previous_key}
    )


def test_radar_replay_after_old_writer_invalidates_cache_remains_noop(explained_db):
    db = explained_db
    signal = _signal()
    _cache(db, signal)
    _, base = _stored(db)
    revision = {
        **base,
        "auto_revision": True,
        "score": 9,
        "score_path": "radar",
        "signal_stage": "radar",
    }
    update = {
        "link": signal["link"],
        "title": "雷达新闻",
        "score": 9,
        "score_path": "radar",
        "direction": "中性",
        "score_confidence": 0.7,
        "backtest_signal": revision,
    }
    assert db.update_cached_entry_score_states([update]) == 1
    with db.get_connection() as conn:
        conn.execute("UPDATE news_cache SET summary='legacy context update'")
        conn.commit()
    invalidated = _stored(db)
    assert all(invalidated[0][field] is None for field in EXPLANATION_FIELDS)
    assert invalidated[1]["raw_score"] == 9
    assert db.update_cached_entry_score_states([update]) == 0
    assert _stored(db) == invalidated
    with db.get_connection() as conn:
        assert conn.execute("SELECT COUNT(*) FROM backtest_signals").fetchone()[0] == 2


def test_cache_selection_does_not_reinterpret_frozen_pass_threshold(explained_db):
    db = explained_db
    signal = _signal(aggregation_threshold=9, selected=False)
    _cache(db, signal)
    db.update_cached_entry_origin(
        signal["link"],
        feed_url="looser-feed",
        backtest_selection={
            "strategy_version": signal["strategy_version"],
            "aggregation_threshold": 7,
        },
    )
    cache, ledger = _stored(db)
    assert ledger["selected"] == 1 and ledger["aggregation_threshold"] == 7
    assert cache["score_threshold"] == ledger["score_threshold"] == 6
    assert cache["score_margin"] == ledger["score_margin"] == 2


def test_ledger_only_initializer_does_not_require_or_create_news_cache():
    conn = sqlite3.connect(":memory:")
    cursor = conn.cursor()
    database._init_app_meta(cursor)
    database._init_backtest_signals(cursor)
    init_score_explanation_storage(cursor)
    tables = {
        row[0]
        for row in cursor.execute("SELECT name FROM sqlite_master WHERE type='table'")
    }
    assert "news_cache" not in tables
    assert set(EXPLANATION_FIELDS) <= {
        row[1] for row in cursor.execute("PRAGMA table_info(backtest_signals)")
    }
    conn.close()


def test_legacy_same_context_replay_keeps_cache_and_ledger_explanation(explained_db):
    db = explained_db
    signal = _signal()
    _cache(db, signal)
    before = _stored(db)
    legacy = {
        key: value for key, value in signal.items() if key not in EXPLANATION_FIELDS
    }
    _cache(db, legacy)
    cache, ledger = _stored(db)
    assert all(
        cache[field] == ledger[field] == before[0][field]
        for field in (*EXPLANATION_FIELDS, TOKEN_FIELD)
    )
    assert ledger == before[1]


def test_legacy_changed_core_context_does_not_keep_stale_explanation(explained_db):
    db = explained_db
    _cache(db, _signal())
    legacy = _signal(**dict.fromkeys(EXPLANATION_FIELDS))
    legacy["score"] = 7
    _cache(db, legacy)
    cache, ledger = _stored(db)
    assert all(cache[field] is None for field in EXPLANATION_FIELDS)
    assert cache["score"] == 7 and ledger["score"] == 8
    assert ledger["raw_score"] == 9


def test_safe_neutral_reason_roundtrip_and_unknown_dropped(explained_db):
    db = explained_db
    facts = _signal(
        link="https://example.com/neutral-facts",
        score=4,
        score_path="llm_safe_neutral_grounding",
        score_confidence=0.0,
        tag="其他",
        logic="来源未显示明确投资催化",
        selected=0,
        stage1_score=4.0,
        stage1_model="Qwen/Qwen2.5-7B-Instruct",
    )
    unknown = _signal(
        link="https://example.com/neutral-unknown",
        score=4,
        score_path="llm_safe_neutral_grounding",
        score_confidence=0.0,
        tag="其他",
        logic="来源未显示明确投资催化",
        selected=0,
        stage1_score=4.0,
        stage1_model="Qwen/Qwen2.5-7B-Instruct",
    )
    for signal, reason in ((facts, "facts"), (unknown, "model wrote this")):
        db.set_cached_entry(
            signal["link"],
            "国内经济政策",
            "国内经济政策",
            4.0,
            "其他",
            "来源未显示明确投资催化",
            score_path=signal["score_path"],
            model_name="Qwen/Qwen2.5-7B-Instruct",
            stage1_score=4.0,
            stage1_model="Qwen/Qwen2.5-7B-Instruct",
            strategy_version=signal["strategy_version"],
            score_confidence=signal["score_confidence"],
            direction=signal["direction"],
            backtest_signal=signal,
            score_explanation={field: signal.get(field) for field in EXPLANATION_FIELDS},
            safe_neutral_reason=reason,
        )
    with db.get_connection() as conn:
        rows = dict(
            conn.execute("SELECT link, safe_neutral_reason FROM news_cache").fetchall()
        )
    assert rows["https://example.com/neutral-facts"] == "facts"
    assert rows["https://example.com/neutral-unknown"] == ""
