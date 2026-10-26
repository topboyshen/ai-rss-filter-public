import hashlib
import json
import time

import pytest

from db import database
from db.event_core import EventCoreConflictError, upsert_security


STRATEGY = "quality-gate-v1"
QUARANTINE_STRATEGY = "quality-quarantine:ufffd-v1"
LINK = "https://example.com/quality-repair"
RAW_TITLE = "央行流动性安排"
SUMMARY = "clean frozen summary"


def _snapshot(
    *,
    score=6.0,
    score_path="llm",
    signal_stage="base",
    selected=False,
    event_id=None,
    event_version=0,
    mapping_version="asset-map:test",
    is_similarity_copy=False,
):
    now = int(time.time())
    return {
        "event_id": event_id,
        "event_version": event_version,
        "event_key": LINK,
        "link": LINK,
        "feed_url": "feed-a",
        "raw_title": RAW_TITLE,
        "event_type": "宏观政策",
        "logic": "流动性环境变化",
        "summary": SUMMARY,
        "source_tier": "primary_news",
        "available_at": now,
        "scored_at": now,
        "published_at": now - 60,
        "score": score,
        "direction": "中性",
        "score_confidence": 0.5,
        "strategy_version": STRATEGY,
        "mapping_version": mapping_version,
        "mapped_theme": "宏观政策",
        "backtest_symbol": None,
        "mapping_confidence": 0.0,
        "mapping_relevance": 0.0,
        "expected_gap": None,
        "security_candidates": [],
        "market_references": [],
        "score_path": score_path,
        "revision_key": "base",
        "signal_stage": signal_stage,
        "aggregation_threshold": 7.0,
        "selected": selected,
        "is_similarity_copy": is_similarity_copy,
    }


def _prepare_quarantined_row(*, security_candidates=None, is_similarity_copy=False):
    database.init_db()
    base = _snapshot(
        score_path="similarity" if is_similarity_copy else "llm",
        is_similarity_copy=is_similarity_copy,
    )
    if security_candidates:
        with database.get_connection() as conn:
            upsert_security(
                conn,
                exchange="SSE",
                ticker="600000",
                name="测试银行",
                security_id="equity-test",
                asset_type="equity",
                aliases=["测试银行", "600000"],
            )
            conn.commit()
        base["security_candidates"] = list(security_candidates)
    event_id, event_version = database.set_cached_entry(
        LINK,
        "original formatted title",
        RAW_TITLE,
        base["score"],
        "宏观政策",
        base["logic"],
        SUMMARY,
        feed_url="feed-a",
        score_path=base["score_path"],
        strategy_version=STRATEGY,
        direction="中性",
        backtest_signal=base,
    )
    quarantine = {
        **base,
        "event_id": event_id,
        "event_version": event_version,
        "mapped_theme": "质量隔离",
        "score_path": "quality_quarantine",
        "signal_stage": "quality_quarantine",
        "revision_key": "quality_quarantine:auto",
        "selected": False,
        "auto_revision": True,
    }
    assert database.store_backtest_signal(quarantine) is True
    with database.get_connection() as conn:
        conn.execute(
            "UPDATE news_cache SET title = raw_title, score = -1, "
            "tag = '质量隔离', logic = '等待显式重评分', "
            "score_path = 'quality_quarantine', error_type = 'invalid_model_output', "
            "strategy_version = ?, score_confidence = 0, direction = '中性' "
            "WHERE link = ?",
            (QUARANTINE_STRATEGY, LINK),
        )
        conn.commit()
    return event_id, event_version


def _repair_snapshot(event_id, event_version, **updates):
    repair = {
        **_snapshot(
            score=8.2,
            score_path="llm",
            signal_stage="quality_repair",
            selected=True,
            event_id=event_id,
            event_version=event_version,
        ),
        "revision_key": "quality_repair:auto",
        "auto_revision": True,
        "require_insert": True,
    }
    repair.update(updates)
    return repair


def _cache_state():
    with database.get_connection() as conn:
        return dict(
            conn.execute(
                "SELECT title, raw_title, summary, score, score_path, "
                "strategy_version, event_id, event_version FROM news_cache "
                "WHERE link = ?",
                (LINK,),
            ).fetchone()
        )


def _counts():
    with database.get_connection() as conn:
        return (
            int(conn.execute("SELECT COUNT(*) FROM backtest_signals").fetchone()[0]),
            int(
                conn.execute(
                    "SELECT COUNT(*) FROM integration_signal_outbox"
                ).fetchone()[0]
            ),
        )


def _provenance_state():
    ordering = {
        "article_sources": "link, feed_url",
        "event_evidence": "event_id, evidence_id",
        "event_identities": "identity_type, identity_key",
    }
    with database.get_connection() as conn:
        state = {}
        for table, order_by in ordering.items():
            rows = [
                dict(row)
                for row in conn.execute(
                    f"SELECT * FROM {table} ORDER BY {order_by}"
                ).fetchall()
            ]
            encoded = json.dumps(
                rows,
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
            ).encode("utf-8")
            state[table] = {
                "count": len(rows),
                "rows_sha256": hashlib.sha256(encoded).hexdigest(),
            }
        return state


def _write_repair(
    repair,
    *,
    title="repaired formatted title",
    tag="宏观政策",
    logic=None,
    score_path=None,
    model_name="qwen",
    error_type="",
    stage1_model="qwen",
    stage2_model="",
    feed_url="feed-a",
):
    return database.set_cached_entry(
        LINK,
        title,
        RAW_TITLE,
        repair["score"],
        tag,
        repair["logic"] if logic is None else logic,
        SUMMARY,
        feed_url=feed_url,
        score_path=score_path or repair["score_path"],
        model_name=model_name,
        error_type=error_type,
        strategy_version=STRATEGY,
        stage1_model=stage1_model,
        stage2_model=stage2_model,
        score_confidence=repair["score_confidence"],
        direction="中性",
        backtest_signal=repair,
    )


def test_quality_repair_atomically_updates_cache_and_appends_exactly_one_revision():
    event_id, event_version = _prepare_quarantined_row()
    with database.get_connection() as conn:
        current_event_id, current_event_version = database._resolve_article_event(
            conn,
            link=LINK,
            title="央行流动性安排后续",
            event_type="宏观政策",
            direction="中性",
            priority=7.0,
            strategy_version=STRATEGY,
            observed_at=int(time.time()) + 1,
        )
        conn.commit()
    assert current_event_id == event_id
    assert current_event_version > event_version
    before_counts = _counts()
    before_provenance = _provenance_state()

    persisted = _write_repair(
        _repair_snapshot(event_id, event_version),
        feed_url="feed-b-must-not-be-recorded",
    )

    assert persisted == (current_event_id, current_event_version)
    cache = _cache_state()
    assert cache["title"] == "repaired formatted title"
    assert cache["summary"] == SUMMARY
    assert cache["score_path"] == "llm"
    assert cache["strategy_version"] == STRATEGY
    assert (cache["event_id"], cache["event_version"]) == (
        current_event_id,
        current_event_version,
    )
    assert _counts() == (before_counts[0] + 1, before_counts[1] + 1)
    assert _provenance_state() == before_provenance
    with database.get_connection() as conn:
        latest = conn.execute(
            "SELECT signal_stage, revision_seq, aggregation_threshold, selected, "
            "event_id, event_version FROM backtest_signals WHERE link = ? "
            "AND strategy_version = ? ORDER BY available_at DESC, id DESC LIMIT 1",
            (LINK, STRATEGY),
        ).fetchone()
        outbox = conn.execute(
            "SELECT signal_stage, revision_seq FROM integration_signal_outbox "
            "ORDER BY seq DESC LIMIT 1"
        ).fetchone()
    assert latest["signal_stage"] == "quality_repair"
    assert latest["revision_seq"] == 2
    assert latest["aggregation_threshold"] == 7.0
    assert latest["selected"] == 1
    assert (latest["event_id"], latest["event_version"]) == (
        event_id,
        event_version,
    )
    assert tuple(outbox) == ("quality_repair", 2)


def test_quality_quarantine_with_redacted_summary_preserves_source_provenance():
    database.init_db()
    base = _snapshot()
    event_id, event_version = database.set_cached_entry(
        LINK,
        "original formatted title",
        RAW_TITLE,
        base["score"],
        base["event_type"],
        base["logic"],
        SUMMARY,
        feed_url="feed-a",
        score_path=base["score_path"],
        strategy_version=STRATEGY,
        direction=base["direction"],
        backtest_signal=base,
    )
    before_provenance = _provenance_state()
    quarantine = {
        **base,
        "event_id": event_id,
        "event_version": event_version,
        "summary": "",
        "mapped_theme": "quality quarantine",
        "score_path": "quality_quarantine",
        "signal_stage": "quality_quarantine",
        "revision_key": "quality_quarantine:auto",
        "selected": False,
        "auto_revision": True,
    }

    assert database.store_backtest_signal(quarantine) is True

    assert _provenance_state() == before_provenance


@pytest.mark.parametrize(
    ("repair_updates", "write_updates", "message"),
    [
        (
            {"score": -1.0, "score_path": "parse_error"},
            {"error_type": "parse_error"},
            "unusable",
        ),
        (
            {"score": 4.2, "score_path": "fallback_local"},
            {
                "model_name": "fallback_local",
                "error_type": "llm_backends_exhausted",
                "stage1_model": "fallback_local",
            },
            "successful model score",
        ),
        (
            {"score": 8.2, "score_path": "fast_pass"},
            {"model_name": "", "stage1_model": ""},
            "successful model score",
        ),
        (
            {"score": 2.0, "score_path": "local_reject"},
            {"model_name": "", "stage1_model": ""},
            "successful model score",
        ),
        (
            {"score": 4.0, "score_path": "llm_safe_neutral_contract"},
            {},
            "successful model score",
        ),
        (
            {"score": 4.0, "score_path": "stage2_safe_neutral_contract"},
            {"stage2_model": "qwen"},
            "successful model score",
        ),
        (
            {"score": 4.0, "score_path": "llm_safe_neutral_grounding"},
            {},
            "successful model score",
        ),
        (
            {"score": 4.0, "score_path": "stage2_safe_neutral_grounding"},
            {"stage2_model": "qwen"},
            "successful model score",
        ),
        (
            {"score": 8.2, "score_path": "llm"},
            {"model_name": "", "stage1_model": ""},
            "successful model score",
        ),
    ],
)
def test_quality_repair_rejects_non_model_or_invalid_results_atomically(
    repair_updates, write_updates, message
):
    event_id, event_version = _prepare_quarantined_row()
    before_cache = _cache_state()
    before_counts = _counts()
    repair = _repair_snapshot(event_id, event_version, **repair_updates)

    with pytest.raises(EventCoreConflictError, match=message):
        _write_repair(repair, **write_updates)

    assert _cache_state() == before_cache
    assert _counts() == before_counts


def test_quality_repair_persists_but_does_not_export_a_frozen_similarity_copy():
    event_id, event_version = _prepare_quarantined_row(is_similarity_copy=True)
    before_counts = _counts()
    repair = _repair_snapshot(
        event_id,
        event_version,
        score=8.2,
        score_path="similarity",
        is_similarity_copy=True,
    )

    _write_repair(
        repair,
        model_name="",
        stage1_model="",
    )

    assert _cache_state()["score_path"] == "similarity"
    assert _counts() == (before_counts[0] + 1, before_counts[1])


@pytest.mark.parametrize("predecessor_is_copy", [False, True])
def test_quality_repair_cannot_change_frozen_direct_copy_semantics(
    predecessor_is_copy
):
    event_id, event_version = _prepare_quarantined_row(
        is_similarity_copy=predecessor_is_copy
    )
    before_cache = _cache_state()
    before_counts = _counts()
    repair_is_copy = not predecessor_is_copy
    repair = _repair_snapshot(
        event_id,
        event_version,
        score_path="similarity" if repair_is_copy else "llm",
        is_similarity_copy=repair_is_copy,
    )
    write_updates = (
        {"model_name": "", "stage1_model": ""}
        if repair_is_copy
        else {}
    )

    with pytest.raises(EventCoreConflictError, match="direct/copy semantics"):
        _write_repair(repair, **write_updates)

    assert _cache_state() == before_cache
    assert _counts() == before_counts


def test_required_repair_insert_conflict_rolls_back_cache(monkeypatch):
    event_id, event_version = _prepare_quarantined_row()
    before_cache = _cache_state()
    before_counts = _counts()
    monkeypatch.setattr(database, "_insert_backtest_signal", lambda *_args: False)

    with pytest.raises(RuntimeError, match="required backtest signal revision"):
        _write_repair(
            _repair_snapshot(event_id, event_version),
            title="must not escape quarantine",
        )

    assert _cache_state() == before_cache
    assert _counts() == before_counts


def test_repair_requires_latest_quarantine_and_mapping_compatibility():
    event_id, event_version = _prepare_quarantined_row()
    before_cache = _cache_state()
    before_counts = _counts()

    with pytest.raises(EventCoreConflictError, match="mapping version"):
        _write_repair(
            _repair_snapshot(
                event_id,
                event_version,
                mapping_version="asset-map:changed",
            )
        )
    assert _cache_state() == before_cache
    assert _counts() == before_counts

    # A non-quarantine latest revision is also a hard conflict, even if the
    # mutable cache row still carries the quarantine marker.
    non_quarantine = {
        **_snapshot(
            score=6.5,
            score_path="radar",
            signal_stage="radar",
            event_id=event_id,
            event_version=event_version,
        ),
        "auto_revision": True,
    }
    assert database.store_backtest_signal(non_quarantine) is True
    after_radar_counts = _counts()
    with pytest.raises(EventCoreConflictError, match="latest strategy revision"):
        _write_repair(_repair_snapshot(event_id, event_version))
    assert _cache_state() == before_cache
    assert _counts() == after_radar_counts


def test_outbox_refusal_rolls_back_repair_ledger_and_cache(monkeypatch):
    event_id, event_version = _prepare_quarantined_row()
    before_cache = _cache_state()
    before_counts = _counts()
    monkeypatch.setattr(database, "enqueue_signal_revision", lambda *_args, **_kwargs: False)

    with pytest.raises(RuntimeError, match="Outbox row"):
        _write_repair(_repair_snapshot(event_id, event_version))

    assert _cache_state() == before_cache
    assert _counts() == before_counts


def test_repair_rejects_changed_frozen_security_mapping_semantics():
    frozen_candidate = {
        "security_id": "equity-test",
        "relation_type": "direct",
        "direction": "中性",
        "confidence": 0.9,
        "relevance": 10.0,
        "expected_gap": None,
        "rationale": "原文直接点名测试银行",
    }
    event_id, event_version = _prepare_quarantined_row(
        security_candidates=[frozen_candidate]
    )
    before_cache = _cache_state()
    before_counts = _counts()
    changed_candidate = {
        **frozen_candidate,
        "relation_type": "upstream",
    }
    repair = _repair_snapshot(event_id, event_version)
    repair["security_candidates"] = [changed_candidate]

    with pytest.raises(EventCoreConflictError, match="frozen real mappings"):
        _write_repair(repair)

    assert _cache_state() == before_cache
    assert _counts() == before_counts


def test_repair_accepts_identical_frozen_security_mapping_semantics():
    frozen_candidate = {
        "security_id": "equity-test",
        "relation_type": "direct",
        "direction": "中性",
        "confidence": 0.9,
        "relevance": 10.0,
        "expected_gap": None,
        "rationale": "原文直接点名测试银行",
    }
    event_id, event_version = _prepare_quarantined_row(
        security_candidates=[frozen_candidate]
    )
    before_counts = _counts()
    repair = _repair_snapshot(event_id, event_version)
    repair["security_candidates"] = [frozen_candidate]

    _write_repair(repair)

    assert _cache_state()["score_path"] == "llm"
    assert _counts() == (before_counts[0] + 1, before_counts[1] + 1)


def test_normal_cache_upsert_does_not_require_a_new_ledger_row():
    database.init_db()
    base = _snapshot()
    database.set_cached_entry(
        LINK,
        "first title",
        RAW_TITLE,
        base["score"],
        "宏观政策",
        base["logic"],
        SUMMARY,
        score_path=base["score_path"],
        strategy_version=STRATEGY,
        backtest_signal=base,
    )

    database.set_cached_entry(
        LINK,
        "ordinary cache refresh",
        RAW_TITLE,
        9.0,
        "宏观政策",
        "new mutable cache text",
        SUMMARY,
        score_path=base["score_path"],
        strategy_version=STRATEGY,
        backtest_signal={**base, "score": 9.0},
    )

    assert _cache_state()["title"] == "ordinary cache refresh"
    assert _counts() == (1, 1)


@pytest.mark.parametrize(
    "polluted_field", ["title", "raw_title", "tag", "logic", "summary"]
)
def test_ordinary_cache_write_rejects_replacement_character_without_side_effects(
    polluted_field
):
    database.init_db()
    signal = _snapshot()
    title = "ordinary formatted title"
    raw_title = RAW_TITLE
    tag = "宏观政策"
    logic = signal["logic"]
    summary = SUMMARY
    if polluted_field == "title":
        title = f"{title}�"
    elif polluted_field == "raw_title":
        raw_title = f"{RAW_TITLE}�"
        signal["raw_title"] = raw_title
    elif polluted_field == "tag":
        tag = f"{tag}�"
        signal["event_type"] = tag
    elif polluted_field == "logic":
        logic = f"{logic}�"
        signal["logic"] = logic
    elif polluted_field == "summary":
        summary = f"{SUMMARY}�"
        signal["summary"] = summary
    before_counts = _counts()

    with pytest.raises(ValueError, match="replacement character"):
        database.set_cached_entry(
            LINK,
            title,
            raw_title,
            signal["score"],
            tag,
            logic,
            summary,
            score_path="llm",
            strategy_version=STRATEGY,
            backtest_signal=signal,
        )

    with database.get_connection() as conn:
        assert conn.execute(
            "SELECT 1 FROM news_cache WHERE link = ?", (LINK,)
        ).fetchone() is None
    assert _counts() == before_counts


@pytest.mark.parametrize(
    "polluted_field",
    [
        "raw_title",
        "summary",
        "event_type",
        "logic",
        "mapped_theme",
        "score_path",
        "nested_mapping",
    ],
)
def test_ordinary_ledger_write_rejects_replacement_character_without_outbox(
    polluted_field
):
    database.init_db()
    signal = _snapshot()
    if polluted_field == "nested_mapping":
        signal["security_candidates"] = [{"rationale": "polluted� rationale"}]
    else:
        signal[polluted_field] = f"{signal[polluted_field]}�"
    before_counts = _counts()

    with pytest.raises(ValueError, match="replacement character"):
        database.store_backtest_signal(signal)

    assert _counts() == before_counts
