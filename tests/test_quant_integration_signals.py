import hashlib
import sqlite3
import threading
import time
import uuid
from pathlib import Path
from urllib.parse import urlencode

import pytest
import yaml
from flask import Flask

from db import database
from db.event_core import EventCoreConflictError
from db.integration_outbox import (
    BACKFILL_META,
    BACKFILL_MAX_ROWS,
    backfill_outbox_event_references,
    enqueue_signal_revision,
    get_integration_outbox_state,
    init_integration_signal_outbox,
    rotate_integration_outbox_epoch,
    trim_integration_signal_outbox,
)
from services.integration_signals import (
    OutboxIntegrityError,
    read_signal_revision_page,
)


TOKEN = "quant-integration-token-32-bytes-minimum"
AUTH = {"Authorization": f"Bearer {TOKEN}"}
PROJECT_ROOT = Path(__file__).resolve().parents[1]


@pytest.fixture(autouse=True)
def _reset_route_limits(monkeypatch):
    import routes.integration_routes as integration_routes

    monkeypatch.setattr(
        integration_routes, "_INTEGRATION_SLOTS", threading.BoundedSemaphore(2)
    )
    monkeypatch.setattr(integration_routes, "_RATE_WINDOW_STARTED", 0.0)
    monkeypatch.setattr(integration_routes, "_RATE_WINDOW_COUNT", 0)


def _init_ledger() -> None:
    """Create only the integration dependencies, not unrelated scorer config."""
    with database.db_lock:
        with database.get_connection() as conn:
            cursor = conn.cursor()
            database._init_app_meta(cursor)
            database._init_backtest_signals(cursor)
            init_integration_signal_outbox(conn)
            conn.commit()


def _app_client(monkeypatch, *, token: str = TOKEN):
    monkeypatch.delenv("AI_FILTER_INTEGRATION_TOKEN_FILE", raising=False)
    monkeypatch.setenv("AI_FILTER_INTEGRATION_TOKEN", token)
    from routes.integration_routes import integration_bp

    application = Flask(__name__)
    application.config.update(TESTING=True)
    application.register_blueprint(integration_bp)
    return application.test_client()


def _signal(
    link: str,
    *,
    available_at: int,
    revision_key: str = "base",
    revision_seq: int = 0,
    signal_stage: str = "base",
    selected: bool = True,
    similarity: bool = False,
    independence_key: str | None = None,
) -> dict:
    return {
        "event_key": f"event:{link}",
        "independence_key": independence_key or f"independent:{link}",
        "link": link,
        "feed_url": "https://feeds.example/a-share?private=source-secret",
        "available_at": available_at,
        "scored_at": available_at,
        "published_at": available_at - 3600,
        "score": 9.2,
        "direction": "看多",
        "score_confidence": 0.81,
        "strategy_version": "strategy-v1",
        "mapping_version": "asset-map:v1",
        "mapped_theme": "AI算力",
        "backtest_symbol": "sz399006",
        "score_path": "stage2",
        "revision_key": revision_key,
        "revision_seq": revision_seq,
        "signal_stage": signal_stage,
        "aggregation_threshold": 8.0,
        "selected": selected,
        "is_similarity_copy": similarity,
    }


def _safe_neutral_signal(link: str, *, available_at: int, score_path: str) -> dict:
    signal = _signal(link, available_at=available_at, selected=False)
    signal.update(
        score=4.0,
        direction="中性",
        score_confidence=0.0,
        score_path=score_path,
        event_type="其他",
        logic="来源未显示明确投资催化",
        is_similarity_copy=False,
    )
    return signal


def _cursor_query(
    cursor: dict,
    *,
    after_seq: int | None = None,
    limit: int = 20,
    schema_version: str | None = None,
):
    query = {
        "ledger_id": cursor["ledger_id"],
        "epoch": cursor["epoch"],
        "after_seq": cursor["next_after_seq"] if after_seq is None else after_seq,
        "limit": limit,
    }
    if schema_version is not None:
        query["schema_version"] = schema_version
    return urlencode(query)


def test_service_auth_fails_closed_before_query_or_quota(monkeypatch):
    import routes.integration_routes as integration_routes

    monkeypatch.delenv("AI_FILTER_INTEGRATION_TOKEN", raising=False)
    monkeypatch.delenv("AI_FILTER_INTEGRATION_TOKEN_FILE", raising=False)
    monkeypatch.setattr(
        integration_routes,
        "read_signal_revision_page",
        lambda **_kwargs: pytest.fail("closed auth must not query the outbox"),
    )
    monkeypatch.setattr(
        integration_routes,
        "_rate_limit_allowed",
        lambda: pytest.fail("closed auth must not consume the business quota"),
    )
    application = Flask(__name__)
    application.register_blueprint(integration_routes.integration_bp)

    response = application.test_client().get("/integration/v1/signals")

    assert response.status_code == 503
    assert response.get_json() == {"error": "integration_auth_unavailable"}
    assert response.headers["Cache-Control"] == "no-store"


@pytest.mark.parametrize(
    ("headers", "query"),
    [
        ({}, ""),
        ({"Authorization": "Bearer wrong-token-with-more-than-32-bytes"}, ""),
        ({"X-API-Key": TOKEN}, ""),
        ({}, f"?key={TOKEN}"),
    ],
)
def test_bad_auth_never_queries_or_exhausts_authenticated_rate_limit(
    monkeypatch, headers, query
):
    import routes.integration_routes as integration_routes

    monkeypatch.setenv("AI_FILTER_INTEGRATION_TOKEN", TOKEN)
    monkeypatch.setattr(
        integration_routes,
        "read_signal_revision_page",
        lambda **_kwargs: pytest.fail("bad auth must not query the outbox"),
    )
    client = _app_client(monkeypatch)

    for _ in range(25):
        response = client.get(f"/integration/v1/signals{query}", headers=headers)

    assert response.status_code == 401
    assert response.get_json() == {"error": "Unauthorized"}
    assert response.headers["WWW-Authenticate"] == "Bearer"
    assert integration_routes._RATE_WINDOW_COUNT == 0


def test_token_file_is_fail_closed_secure_and_takes_precedence(monkeypatch, tmp_path):
    _init_ledger()
    client = _app_client(monkeypatch)
    secret = tmp_path / "integration-token"
    secret.write_text(TOKEN + "\n", encoding="utf-8")
    secret.chmod(0o600)
    monkeypatch.setenv("AI_FILTER_INTEGRATION_TOKEN", "x" * 40)
    monkeypatch.setenv("AI_FILTER_INTEGRATION_TOKEN_FILE", str(secret))

    assert client.get("/integration/v1/signals", headers=AUTH).status_code == 200

    secret.chmod(0o622)
    response = client.get("/integration/v1/signals", headers=AUTH)
    assert response.status_code == 503
    assert response.get_json()["error"] == "integration_auth_unavailable"

    secret.unlink()
    assert client.get("/integration/v1/signals", headers=AUTH).status_code == 503


def test_signal_and_outbox_append_are_atomic_idempotent_and_revision_ordered():
    _init_ledger()
    now = int(time.time())
    base = _signal("https://example.com/revision", available_at=now)
    revision = _signal(
        "https://example.com/revision",
        available_at=now + 5,
        revision_key="radar:1",
        revision_seq=1,
        signal_stage="radar",
        selected=False,
        similarity=False,
    )

    assert database.store_backtest_signal(base) is True
    assert database.store_backtest_signal(base) is False
    assert database.store_backtest_signal(revision) is True

    with database.get_connection() as conn:
        assert conn.execute("SELECT COUNT(*) FROM backtest_signals").fetchone()[0] == 2
        rows = conn.execute(
            "SELECT seq, event_id, domain_event_id, event_version, "
            "event_mapping_id, revision_key FROM integration_signal_outbox "
            "ORDER BY seq"
        ).fetchall()
    assert [(row["seq"], row["revision_key"]) for row in rows] == [
        (1, "base"),
        (2, "radar:1"),
    ]
    assert all(len(row["event_id"]) == 64 for row in rows)
    assert rows[0]["event_id"] != rows[1]["event_id"]
    assert {str(uuid.UUID(row["domain_event_id"])) for row in rows} == {
        rows[0]["domain_event_id"]
    }
    assert all(int(row["event_version"]) >= 1 for row in rows)
    assert all(
        str(uuid.UUID(row["event_mapping_id"])) == row["event_mapping_id"]
        for row in rows
    )


def test_outbox_failure_rolls_back_the_signal_ledger_transaction():
    _init_ledger()
    with database.get_connection() as conn:
        conn.execute(
            "CREATE TRIGGER reject_integration_outbox BEFORE INSERT "
            "ON integration_signal_outbox BEGIN SELECT RAISE(ABORT, 'outbox down'); END"
        )
        conn.commit()

    with pytest.raises(sqlite3.IntegrityError, match="outbox down"):
        database.store_backtest_signal(
            _signal("https://example.com/atomic", available_at=int(time.time()))
        )

    with database.get_connection() as conn:
        assert conn.execute("SELECT COUNT(*) FROM backtest_signals").fetchone()[0] == 0
        assert (
            conn.execute("SELECT COUNT(*) FROM integration_signal_outbox").fetchone()[0]
            == 0
        )


@pytest.mark.parametrize(
    "score_path",
    [
        "llm_safe_neutral_initial",
        "stage2_safe_neutral_initial",
        "llm_safe_neutral_contract",
        "stage2_safe_neutral_contract",
        "llm_safe_neutral_grounding",
        "stage2_safe_neutral_grounding",
    ],
)
def test_safe_neutral_path_is_frozen_in_cache_and_ledger_but_never_exported(
    score_path,
):
    database.init_db()
    now = int(time.time())
    link = f"https://example.com/{score_path}"
    signal = _safe_neutral_signal(
        link,
        available_at=now,
        score_path=score_path,
    )
    stage2 = score_path.startswith("stage2_")

    database.set_cached_entry(
        link,
        "safe neutral",
        "safe neutral",
        4.0,
        "其他",
        "来源未显示明确投资催化",
        score_path=score_path,
        model_name="qwen",
        strategy_version="strategy-v1",
        stage1_score=7.0 if stage2 else 4.0,
        stage1_model="qwen-stage1" if stage2 else "qwen",
        stage2_score=4.0 if stage2 else None,
        stage2_model="qwen" if stage2 else "",
        score_confidence=0.0,
        direction="中性",
        backtest_signal=signal,
    )

    with database.get_connection() as conn:
        cache_path = conn.execute(
            "SELECT score_path FROM news_cache WHERE link = ?", (link,)
        ).fetchone()[0]
        ledger = conn.execute(
            "SELECT id, signal_key, score_path FROM backtest_signals WHERE link = ?", (link,)
        ).fetchone()
        assert cache_path == score_path
        assert ledger["score_path"] == score_path
        assert enqueue_signal_revision(
            conn,
            ledger_signal_id=int(ledger["id"]),
            signal=dict(
                conn.execute(
                    "SELECT * FROM backtest_signals WHERE id = ?", (ledger["id"],)
                ).fetchone()
            ),
            signal_revision_key=str(ledger["signal_key"]),
        ) is False
        poisoned = dict(
            conn.execute(
                "SELECT * FROM backtest_signals WHERE id = ?", (ledger["id"],)
            ).fetchone()
        )
        poisoned["score_path"] = "llm"
        with pytest.raises(ValueError, match="score_path does not match ledger"):
            enqueue_signal_revision(
                conn,
                ledger_signal_id=int(ledger["id"]),
                signal=poisoned,
                signal_revision_key=str(ledger["signal_key"]),
            )
        conn.execute("DELETE FROM app_meta WHERE k = ?", (BACKFILL_META,))
        init_integration_signal_outbox(conn, now=now, trim=False)
        assert (
            conn.execute("SELECT COUNT(*) FROM integration_signal_outbox").fetchone()[0]
            == 0
        )
        conn.commit()


@pytest.mark.parametrize(
    "score_path",
    ["parse_error", "quality_quarantine", "similarity"],
)
def test_nonexportable_score_state_is_frozen_but_never_enqueued(score_path):
    _init_ledger()
    signal = _signal(
        f"https://example.com/nonexportable-{score_path}",
        available_at=int(time.time()),
        selected=False,
        similarity=score_path == "similarity",
    )
    signal["score_path"] = score_path

    assert database.store_backtest_signal(signal) is True

    with database.get_connection() as conn:
        ledger = conn.execute(
            "SELECT score_path FROM backtest_signals WHERE link = ?",
            (signal["link"],),
        ).fetchone()
        assert ledger["score_path"] == score_path
        assert conn.execute(
            "SELECT COUNT(*) FROM integration_signal_outbox"
        ).fetchone()[0] == 0


def test_safe_neutral_score_path_and_identity_are_canonicalized_and_defended():
    database.init_db()
    now = int(time.time())
    link = "https://example.com/canonical-safe-neutral"
    signal = _safe_neutral_signal(
        link,
        available_at=now,
        score_path=" LLM_SAFE_NEUTRAL_CONTRACT ",
    )
    signal["direction"] = " 中性 "

    database.set_cached_entry(
        link,
        "safe neutral",
        "safe neutral",
        4.0,
        "其他",
        "来源未显示明确投资催化",
        score_path=" LLM_SAFE_NEUTRAL_CONTRACT ",
        model_name=" qwen ",
        error_type=" ",
        strategy_version="strategy-v1",
        stage1_score=4.0,
        stage1_model=" qwen ",
        score_confidence=0.0,
        direction=" 中性 ",
        backtest_signal=signal,
    )

    with database.get_connection() as conn:
        cache = conn.execute(
            "SELECT score_path, model_name, stage1_model, error_type, direction "
            "FROM news_cache WHERE link = ?",
            (link,),
        ).fetchone()
        ledger = conn.execute(
            "SELECT id, score_path, direction FROM backtest_signals WHERE link = ?",
            (link,),
        ).fetchone()
        assert tuple(cache) == (
            "llm_safe_neutral_contract",
            "qwen",
            "qwen",
            "",
            "中性",
        )
        assert (ledger["score_path"], ledger["direction"]) == (
            "llm_safe_neutral_contract",
            "中性",
        )

        # Defensive reads also reject a historical/tampered mixed-case path.
        mixed_path = " LLM_SAFE_NEUTRAL_CONTRACT "
        conn.execute(
            "UPDATE news_cache SET score_path = ? WHERE link = ?",
            (mixed_path, link),
        )
        conn.execute(
            "UPDATE backtest_signals SET score_path = ? WHERE id = ?",
            (mixed_path, int(ledger["id"])),
        )
        conn.execute("DELETE FROM app_meta WHERE k = ?", (BACKFILL_META,))
        init_integration_signal_outbox(conn, now=now, trim=False)
        conn.commit()

    assert database.get_recent_entries(
        cfg={
            "similarity_window_hours": 72,
            "scoring_strategy_version": "strategy-v1",
        }
    ) == []
    assert database.get_score_fallback_stats()["model_scored"] == 0
    assert database.get_backtest_signal_stats("strategy-v1")["revision_count"] == 0
    with database.get_connection() as conn:
        assert conn.execute(
            "SELECT COUNT(*) FROM integration_signal_outbox"
        ).fetchone()[0] == 0


def test_score_state_batch_rejects_non_revision_and_false_idempotency():
    database.init_db()
    now = int(time.time())

    safe_link = "https://example.com/batch-safe-base-replay"
    safe_signal = _safe_neutral_signal(
        safe_link,
        available_at=now,
        score_path="llm_safe_neutral_contract",
    )
    database.set_cached_entry(
        safe_link,
        "safe neutral",
        "safe neutral",
        4.0,
        "其他",
        "来源未显示明确投资催化",
        score_path="llm_safe_neutral_contract",
        model_name="qwen",
        strategy_version="strategy-v1",
        stage1_score=4.0,
        stage1_model="qwen",
        direction="中性",
        backtest_signal=safe_signal,
    )
    with pytest.raises(ValueError, match="auto revision"):
        database.update_cached_entry_score_states([{
            "link": safe_link,
            "title": "laundered",
            "score": 4.0,
            "score_path": "llm_safe_neutral_contract",
            "direction": "中性",
            "score_confidence": 0.0,
            "backtest_signal": safe_signal,
            "update_cache": True,
        }])

    normal_link = "https://example.com/batch-false-idempotency"
    base = _signal(normal_link, available_at=now)
    database.set_cached_entry(
        normal_link,
        "base",
        "base",
        base["score"],
        "AI算力",
        "算力需求改善",
        score_path=base["score_path"],
        strategy_version=base["strategy_version"],
        direction=base["direction"],
        score_confidence=base["score_confidence"],
        backtest_signal=base,
    )
    with database.get_connection() as conn:
        frozen_state_key = conn.execute(
            "SELECT revision_state_key FROM backtest_signals WHERE link = ?",
            (normal_link,),
        ).fetchone()[0]
    poisoned = {
        **base,
        "score": 8.0,
        "score_path": "radar",
        "score_confidence": 0.8,
        "selected": True,
        "signal_stage": "radar",
        "auto_revision": True,
        "revision_state_key": frozen_state_key,
    }
    with pytest.raises(EventCoreConflictError, match="idempotent ledger"):
        database.update_cached_entry_score_states([{
            "link": normal_link,
            "title": "poisoned",
            "score": 8.0,
            "score_path": "radar",
            "direction": base["direction"],
            "score_confidence": 0.8,
            "backtest_signal": poisoned,
            "update_cache": True,
        }])

    with database.get_connection() as conn:
        safe_cache = conn.execute(
            "SELECT score, score_path FROM news_cache WHERE link = ?",
            (safe_link,),
        ).fetchone()
        normal_cache = conn.execute(
            "SELECT title, score, score_path FROM news_cache WHERE link = ?",
            (normal_link,),
        ).fetchone()
        assert tuple(safe_cache) == (4.0, "llm_safe_neutral_contract")
        assert tuple(normal_cache) == ("base", 9.2, "stage2")
        assert conn.execute(
            "SELECT COUNT(*) FROM backtest_signals WHERE link = ?",
            (normal_link,),
        ).fetchone()[0] == 1


@pytest.mark.parametrize(
    ("cache_updates", "signal_updates"),
    [
        (
            {"score_path": "llm_safe_neutral_contract"},
            {"score_path": "llm"},
        ),
        (
            {"score_path": "llm"},
            {"score_path": "llm_safe_neutral_contract"},
        ),
        ({"score": 3.9}, {}),
        ({"tag": "公司"}, {}),
        ({"logic": "公司信息需要复核"}, {}),
        ({"direction": "看多"}, {}),
        ({"score_confidence": 0.1}, {}),
        ({}, {"score": 3.9}),
        ({}, {"selected": True}),
        ({}, {"is_similarity_copy": True}),
        ({}, {"auto_revision": True}),
    ],
)
def test_safe_neutral_cache_and_ledger_drift_fails_before_any_write(
    cache_updates, signal_updates,
):
    database.init_db()
    now = int(time.time())
    link = f"https://example.com/safe-drift-{uuid.uuid4()}"
    signal = _safe_neutral_signal(
        link,
        available_at=now,
        score_path="llm_safe_neutral_contract",
    )
    signal.update(signal_updates)
    cache = {
        "score": 4.0,
        "tag": "其他",
        "logic": "来源未显示明确投资催化",
        "score_path": "llm_safe_neutral_contract",
        "direction": "中性",
        "score_confidence": 0.0,
    }
    cache.update(cache_updates)

    with pytest.raises(ValueError):
        database.set_cached_entry(
            link,
            "safe neutral",
            "safe neutral",
            cache["score"],
            cache["tag"],
            cache["logic"],
            score_path=cache["score_path"],
            model_name="qwen",
            strategy_version="strategy-v1",
            stage1_score=4.0,
            stage1_model="qwen",
            score_confidence=cache["score_confidence"],
            direction=cache["direction"],
            backtest_signal=signal,
        )

    with database.get_connection() as conn:
        assert conn.execute(
            "SELECT COUNT(*) FROM news_cache WHERE link = ?", (link,)
        ).fetchone()[0] == 0
        assert conn.execute(
            "SELECT COUNT(*) FROM backtest_signals WHERE link = ?", (link,)
        ).fetchone()[0] == 0
        assert (
            conn.execute("SELECT COUNT(*) FROM integration_signal_outbox").fetchone()[0]
            == 0
        )


@pytest.mark.parametrize(
    "signal_updates",
    [
        {"score": 3.9},
        {"event_type": "公司"},
        {"logic": "公司信息需要复核"},
        {"direction": "看多"},
        {"score_confidence": 0.1},
        {"selected": True},
        {"is_similarity_copy": True},
        {"auto_revision": True},
    ],
)
def test_direct_safe_neutral_ledger_write_rejects_drift(signal_updates):
    database.init_db()
    link = f"https://example.com/direct-safe-drift-{uuid.uuid4()}"
    signal = _safe_neutral_signal(
        link,
        available_at=int(time.time()),
        score_path="llm_safe_neutral_grounding",
    )
    signal.update(signal_updates)

    with pytest.raises(ValueError):
        database.store_backtest_signal(signal)

    with database.get_connection() as conn:
        assert conn.execute(
            "SELECT COUNT(*) FROM backtest_signals WHERE link = ?", (link,)
        ).fetchone()[0] == 0
        assert (
            conn.execute("SELECT COUNT(*) FROM integration_signal_outbox").fetchone()[0]
            == 0
        )


def test_safe_neutral_base_cannot_be_laundered_by_replay_or_auto_revision():
    database.init_db()
    now = int(time.time())
    link = "https://example.com/non-substantive-replay-guard"
    safe_signal = _safe_neutral_signal(
        link,
        available_at=now,
        score_path="llm_safe_neutral_grounding",
    )
    database.set_cached_entry(
        link,
        "safe neutral",
        "safe neutral",
        4.0,
        "其他",
        "来源未显示明确投资催化",
        score_path="llm_safe_neutral_grounding",
        model_name="qwen",
        strategy_version="strategy-v1",
        stage1_score=4.0,
        stage1_model="qwen",
        score_confidence=0.0,
        direction="中性",
        backtest_signal=safe_signal,
    )

    replay = _signal(link, available_at=now + 1)
    replay.update(score_path="llm", event_type="公司", logic="模型独立评分")
    with pytest.raises(EventCoreConflictError, match="non-substantive"):
        database.set_cached_entry(
            link,
            "normal score",
            "normal score",
            8.0,
            "公司",
            "模型独立评分",
            score_path="llm",
            model_name="qwen",
            strategy_version="strategy-v1",
            stage1_score=8.0,
            stage1_model="qwen",
            direction="看多",
            backtest_signal=replay,
        )

    radar = {
        **safe_signal,
        "score": 8.0,
        "score_path": "radar",
        "direction": "看多",
        "score_confidence": 0.8,
        "selected": True,
        "auto_revision": True,
        "revision_key": "radar:1",
        "signal_stage": "radar",
    }
    with pytest.raises(EventCoreConflictError, match="non-substantive"):
        database.update_cached_entry_score_states(
            [
                {
                    "link": link,
                    "title": "radar score",
                    "score": 8.0,
                    "score_path": "radar",
                    "direction": "看多",
                    "score_confidence": 0.8,
                    "backtest_signal": radar,
                    "update_cache": True,
                }
            ]
        )

    with database.get_connection() as conn:
        cache = conn.execute(
            "SELECT score, score_path FROM news_cache WHERE link = ?", (link,)
        ).fetchone()
        ledger = conn.execute(
            "SELECT score, score_path FROM backtest_signals WHERE link = ?", (link,)
        ).fetchall()
        assert tuple(cache) == (4.0, "llm_safe_neutral_grounding")
        assert [tuple(row) for row in ledger] == [
            (4.0, "llm_safe_neutral_grounding")
        ]
        assert (
            conn.execute("SELECT COUNT(*) FROM integration_signal_outbox").fetchone()[0]
            == 0
        )


def test_direct_outbox_write_requires_persisted_event_uuid_and_version():
    _init_ledger()
    assert database.store_backtest_signal(
        _signal("https://example.com/domain-identity", available_at=int(time.time()))
    )

    with database.get_connection() as conn:
        frozen = dict(
            conn.execute("SELECT * FROM backtest_signals LIMIT 1").fetchone()
        )
        for revision_key, poisoned in (
            ("missing-event", {**frozen, "event_id": ""}),
            ("missing-version", {**frozen, "event_version": 0}),
        ):
            with pytest.raises(ValueError, match="event_id|event_version"):
                enqueue_signal_revision(
                    conn,
                    ledger_signal_id=int(frozen["id"]),
                    signal=poisoned,
                    signal_revision_key=str(frozen["signal_key"]),
                )
        assert (
            conn.execute("SELECT COUNT(*) FROM integration_signal_outbox").fetchone()[0]
            == 1
        )


def test_legacy_outbox_identity_is_repaired_only_from_retained_signal_ledger():
    _init_ledger()
    assert database.store_backtest_signal(
        _signal("https://example.com/repairable", available_at=int(time.time()))
    )

    with database.get_connection() as conn:
        expected = conn.execute(
            "SELECT event_id, event_version, event_mapping_id "
            "FROM backtest_signals LIMIT 1"
        ).fetchone()
        conn.execute(
            "UPDATE integration_signal_outbox SET domain_event_id = NULL, "
            "event_version = NULL, event_mapping_id = NULL"
        )

        assert backfill_outbox_event_references(conn) == 0
        repaired = conn.execute(
            "SELECT domain_event_id, event_version, event_mapping_id "
            "FROM integration_signal_outbox LIMIT 1"
        ).fetchone()
        assert tuple(repaired) == tuple(expected)
        assert backfill_outbox_event_references(conn) == 0


def test_unrepairable_legacy_outbox_is_preserved_and_page_fails_closed(monkeypatch):
    _init_ledger()
    assert database.store_backtest_signal(
        _signal("https://example.com/unrepairable", available_at=int(time.time()))
    )

    with database.get_connection() as conn:
        delivery_key = conn.execute(
            "SELECT event_id FROM integration_signal_outbox LIMIT 1"
        ).fetchone()[0]
        conn.execute(
            "UPDATE integration_signal_outbox SET domain_event_id = NULL, "
            "event_version = NULL, event_mapping_id = NULL"
        )
        conn.execute("DELETE FROM backtest_signals")

        assert backfill_outbox_event_references(conn) == 1
        retained = conn.execute(
            "SELECT event_id, domain_event_id, event_version, event_mapping_id "
            "FROM integration_signal_outbox LIMIT 1"
        ).fetchone()
        assert tuple(retained) == (delivery_key, None, None, None)
        conn.commit()

    response = _app_client(monkeypatch).get(
        "/integration/v1/signals", headers=AUTH
    )
    assert response.status_code == 503
    assert response.get_json() == {"error": "integration_temporarily_unavailable"}


def test_bootstrap_replay_contract_is_contiguous_and_minimal(monkeypatch):
    _init_ledger()
    now = int(time.time())
    for link in (
        "https://Example.com/news/item?q=query-secret#fragment",
        "http://127.0.0.1/admin?token=ssrf-secret",
        "https://example.com/third",
    ):
        assert database.store_backtest_signal(_signal(link, available_at=now))

    client = _app_client(monkeypatch)
    first = client.get("/integration/v1/signals?limit=2", headers=AUTH)
    replay = client.get("/integration/v1/signals?limit=2", headers=AUTH)

    assert first.status_code == replay.status_code == 200
    body = first.get_json()
    assert body == replay.get_json()
    assert body["schema_version"] == "ai-filter.integration.signals.v3"
    assert body["delivery_semantics"] == "at_least_once"
    assert body["apply_order_field"] == "seq"
    assert body["event_time_field"] == "available_at"
    assert body["idempotency_field"] == "signal_revision_id"
    assert [item["seq"] for item in body["items"]] == [1, 2]
    assert set(body["items"][0]) == {
        "seq",
        "signal_revision_id",
        "event_id",
        "event_version",
        "event_mapping_id",
        "available_at",
        "score",
        "direction",
        "score_confidence",
        "strategy_version",
        "mapping_version",
        "mapped_theme",
        "backtest_symbol",
        "score_path",
        "revision_key",
        "signal_stage",
        "revision_seq",
        "revision_state_key",
        "independence_id",
        "aggregation_threshold",
        "selected",
        "is_similarity_copy",
    }
    for item in body["items"]:
        assert str(uuid.UUID(item["event_id"])) == item["event_id"]
        assert len(item["signal_revision_id"]) == 64
        assert int(item["signal_revision_id"], 16) >= 0
        assert item["event_version"] >= 1
        assert str(uuid.UUID(item["event_mapping_id"])) == item["event_mapping_id"]
    rendered = first.get_data(as_text=True)
    for forbidden in (
        "feed_url",
        "article_url",
        "event_key_sha256",
        "example.com",
        "query-secret",
        "fragment",
        "ssrf-secret",
        "feeds.example",
        "ledger_signal_id",
        "published_at",
    ):
        assert forbidden not in rendered

    cursor = body["cursor"]
    assert cursor["next_after_seq"] == 2
    assert cursor["has_more"] is True
    final = client.get(
        "/integration/v1/signals?" + _cursor_query(cursor, limit=2), headers=AUTH
    ).get_json()
    assert [item["seq"] for item in final["items"]] == [3]
    assert final["cursor"]["next_after_seq"] == 3
    assert final["cursor"]["has_more"] is False


def test_v4_replay_contract_and_cursor_progression(monkeypatch):
    _init_ledger()
    now = int(time.time())
    first_signal = _signal("https://example.com/v4-first", available_at=now)
    first_signal.update(
        mapping_confidence=0.73,
        mapping_relevance=8.4,
        expected_gap=-2.5,
    )
    second_signal = _signal(
        "https://example.com/v4-second", available_at=now + 1
    )
    second_signal.update(
        mapping_confidence=0.61,
        mapping_relevance=7.2,
        expected_gap=1.75,
    )
    assert database.store_backtest_signal(first_signal)
    assert database.store_backtest_signal(second_signal)

    client = _app_client(monkeypatch)
    first = client.get(
        "/integration/v1/signals?schema_version=v4&limit=1", headers=AUTH
    )

    assert first.status_code == 200
    body = first.get_json()
    assert body["schema_version"] == "ai-filter.integration.signals.v4"
    assert body["cursor"]["next_after_seq"] == 1
    assert body["cursor"]["has_more"] is True
    assert body["items"][0]["importance_score"] == 9.2
    assert body["items"][0]["mapping_confidence"] == 0.73
    assert body["items"][0]["mapping_relevance"] == 8.4
    assert body["items"][0]["expected_gap"] == -2.5

    followup = client.get(
        "/integration/v1/signals?"
        + _cursor_query(body["cursor"], limit=1, schema_version="v4"),
        headers=AUTH,
    )
    assert followup.status_code == 200
    final = followup.get_json()
    assert final["schema_version"] == "ai-filter.integration.signals.v4"
    assert [item["seq"] for item in final["items"]] == [2]
    assert final["items"][0]["mapping_confidence"] == 0.61
    assert final["items"][0]["mapping_relevance"] == 7.2
    assert final["items"][0]["expected_gap"] == 1.75
    assert final["cursor"]["next_after_seq"] == 2
    assert final["cursor"]["has_more"] is False


@pytest.mark.parametrize(
    "query",
    [
        "?after_seq=0",
        "?ledger_id=" + "a" * 32,
        "?epoch=" + "b" * 32,
        "?after_seq=-1",
        "?after_seq=abc",
        "?after_seq=1&after_seq=2",
        "?limit=0",
        "?limit=51",
        "?limit=1.5",
        "?key=forbidden",
        "?schema_version=v2",
        "?schema_version=v3&schema_version=v4",
    ],
)
def test_cursor_query_is_strict_and_bounded(monkeypatch, query):
    _init_ledger()
    response = _app_client(monkeypatch).get(
        f"/integration/v1/signals{query}", headers=AUTH
    )

    assert response.status_code == 400
    assert response.get_json()["error"] == "invalid_query"


def test_cursor_mismatch_ahead_and_expiry_are_explicit(monkeypatch):
    _init_ledger()
    now = int(time.time())
    assert database.store_backtest_signal(
        _signal("https://example.com/cursor", available_at=now)
    )
    client = _app_client(monkeypatch)
    cursor = client.get("/integration/v1/signals", headers=AUTH).get_json()["cursor"]

    wrong_ledger = dict(cursor, ledger_id="a" * 32)
    assert client.get(
        "/integration/v1/signals?" + _cursor_query(wrong_ledger), headers=AUTH
    ).status_code == 409
    wrong_epoch = dict(cursor, epoch="b" * 32)
    assert client.get(
        "/integration/v1/signals?" + _cursor_query(wrong_epoch), headers=AUTH
    ).status_code == 409
    assert client.get(
        "/integration/v1/signals?" + _cursor_query(cursor, after_seq=2), headers=AUTH
    ).status_code == 409

    for number in range(2, 102):
        assert database.store_backtest_signal(
            _signal(f"https://example.com/{number}", available_at=now + number)
        )
    with database.db_lock:
        with database.get_connection() as conn:
            assert trim_integration_signal_outbox(
                conn, now=now, retention_days=None, max_rows=100
            ) == 1
            conn.commit()
    expired = client.get(
        "/integration/v1/signals?" + _cursor_query(cursor, after_seq=0), headers=AUTH
    )
    assert expired.status_code == 410
    assert expired.get_json()["error"] == "cursor_expired"


def test_backtest_cleanup_hole_cannot_break_outbox_cursor(monkeypatch):
    _init_ledger()
    now = int(time.time())
    links = (
        "https://example.com/first",
        "https://test.com/remove-only-from-ledger",
        "https://example.com/third",
    )
    for link in links:
        assert database.store_backtest_signal(_signal(link, available_at=now))

    removed = database.cleanup_test_records()
    assert removed["backtest_signals"] == 1
    with database.get_connection() as conn:
        assert conn.execute("SELECT COUNT(*) FROM backtest_signals").fetchone()[0] == 2
        assert (
            conn.execute("SELECT COUNT(*) FROM integration_signal_outbox").fetchone()[0]
            == 3
        )

    body = _app_client(monkeypatch).get(
        "/integration/v1/signals?limit=10", headers=AUTH
    ).get_json()
    assert [item["seq"] for item in body["items"]] == [1, 2, 3]


def test_retention_trims_only_continuous_prefix_when_clock_moves_backwards():
    _init_ledger()
    now = 2_000_000_000
    for number in range(3):
        assert database.store_backtest_signal(
            _signal(f"https://example.com/clock-{number}", available_at=now)
        )
    cutoff = now - 7 * 86400
    with database.db_lock:
        with database.get_connection() as conn:
            conn.execute(
                "UPDATE integration_signal_outbox SET enqueued_at = CASE seq "
                "WHEN 1 THEN ? WHEN 2 THEN ? ELSE ? END",
                (cutoff - 10, cutoff + 10, cutoff - 20),
            )
            deleted = trim_integration_signal_outbox(
                conn, now=now, retention_days=7, max_rows=None
            )
            state = get_integration_outbox_state(conn)
            remaining = [
                row[0]
                for row in conn.execute(
                    "SELECT seq FROM integration_signal_outbox ORDER BY seq"
                )
            ]
            conn.commit()

    assert deleted == 1
    assert state["floor_seq"] == 2
    assert remaining == [2, 3]


def test_epoch_rotation_invalidates_pre_restore_cursor(monkeypatch, tmp_path):
    _init_ledger()
    now = int(time.time())
    for number in range(2):
        database.store_backtest_signal(
            _signal(f"https://example.com/restore-{number}", available_at=now)
        )
    backup = tmp_path / "cache-backup.db"
    with database.get_connection() as source, sqlite3.connect(backup) as destination:
        source.backup(destination)

    client = _app_client(monkeypatch)
    before_restore = client.get(
        "/integration/v1/signals?limit=1", headers=AUTH
    ).get_json()["cursor"]
    assert database.store_backtest_signal(
        _signal("https://example.com/newer-than-backup", available_at=now)
    )

    with database.db_lock:
        with sqlite3.connect(backup) as source, database.get_connection() as destination:
            source.backup(destination)
            new_epoch = rotate_integration_outbox_epoch(destination)
            destination.commit()
    assert new_epoch != before_restore["epoch"]

    stale = client.get(
        "/integration/v1/signals?" + _cursor_query(before_restore, after_seq=1),
        headers=AUTH,
    )
    assert stale.status_code == 409
    assert stale.get_json()["error"] == "cursor_epoch_mismatch"


def test_internal_outbox_gap_fails_closed_instead_of_skipping(monkeypatch):
    _init_ledger()
    now = int(time.time())
    for number in range(3):
        database.store_backtest_signal(
            _signal(f"https://example.com/gap-{number}", available_at=now)
        )
    with database.get_connection() as conn:
        conn.execute("DELETE FROM integration_signal_outbox WHERE seq = 2")
        conn.commit()

    response = _app_client(monkeypatch).get(
        "/integration/v1/signals", headers=AUTH
    )
    assert response.status_code == 503
    assert response.get_json() == {"error": "integration_temporarily_unavailable"}


@pytest.mark.parametrize("wire_version", [3, 4])
@pytest.mark.parametrize(
    ("target", "column", "value"),
    [
        ("outbox", "score_path", "future_unknown"),
        ("outbox", "score_path", "llm_safe_neutral_contract"),
        ("outbox", "score_path", "quality_quarantine"),
        ("outbox", "is_similarity_copy", 1),
        ("ledger", "score_path", "future_unknown"),
        ("ledger", "score_path", "llm_safe_neutral_contract"),
        ("ledger", "is_similarity_copy", 1),
    ],
)
def test_v3_v4_page_fails_closed_on_nonexportable_or_copy(
    wire_version, target, column, value
):
    _init_ledger()
    now = int(time.time())
    assert database.store_backtest_signal(
        _signal("https://example.com/exportable-first", available_at=now)
    )
    assert database.store_backtest_signal(
        _signal("https://example.com/poisoned-second", available_at=now + 1)
    )

    with database.get_connection() as conn:
        if target == "outbox":
            conn.execute(
                f"UPDATE integration_signal_outbox SET {column} = ? WHERE seq = 2",
                (value,),
            )
        else:
            conn.execute(
                f"UPDATE backtest_signals SET {column} = ? WHERE id = ("
                "SELECT ledger_signal_id FROM integration_signal_outbox "
                "WHERE seq = 2)",
                (value,),
            )
        conn.commit()

    with pytest.raises(OutboxIntegrityError, match="not exportable"):
        read_signal_revision_page(
            ledger_id=None,
            epoch=None,
            after_seq=None,
            limit=2,
            wire_version=wire_version,
        )


@pytest.mark.parametrize("wire_version", [3, 4])
@pytest.mark.parametrize(
    ("column", "value"),
    [
        ("score", 1.25),
        ("direction", "看空"),
        ("mapped_theme", "tampered-theme"),
        ("domain_event_id", str(uuid.uuid4())),
        ("mapping_confidence", 0.5),
    ],
)
def test_v3_v4_page_fails_closed_on_ledger_bound_field_drift(
    wire_version, column, value
):
    _init_ledger()
    now = int(time.time())
    assert database.store_backtest_signal(
        _signal("https://example.com/ledger-first", available_at=now)
    )
    assert database.store_backtest_signal(
        _signal("https://example.com/ledger-drift", available_at=now + 1)
    )
    with database.get_connection() as conn:
        conn.execute(
            f"UPDATE integration_signal_outbox SET {column} = ? WHERE seq = 2",
            (value,),
        )
        conn.commit()

    with pytest.raises(OutboxIntegrityError, match="does not match signal ledger"):
        read_signal_revision_page(
            ledger_id=None,
            epoch=None,
            after_seq=None,
            limit=2,
            wire_version=wire_version,
        )


@pytest.mark.parametrize("schema_version", ["v3", "v4"])
def test_api_integrity_failure_does_not_advance_cursor(monkeypatch, schema_version):
    _init_ledger()
    now = int(time.time())
    assert database.store_backtest_signal(
        _signal("https://example.com/cursor-valid", available_at=now)
    )
    assert database.store_backtest_signal(
        _signal("https://example.com/cursor-poison", available_at=now + 1)
    )
    client = _app_client(monkeypatch)
    first = client.get(
        f"/integration/v1/signals?schema_version={schema_version}&limit=1",
        headers=AUTH,
    )
    assert first.status_code == 200
    cursor = first.get_json()["cursor"]
    assert cursor["next_after_seq"] == 1

    with database.get_connection() as conn:
        conn.execute(
            "UPDATE integration_signal_outbox "
            "SET score_path = 'future_unknown' WHERE seq = 2"
        )
        conn.commit()
    request_path = (
        "/integration/v1/signals?"
        + _cursor_query(
            cursor, limit=1, schema_version=schema_version
        )
    )
    failed = client.get(request_path, headers=AUTH)
    assert failed.status_code == 503
    assert failed.get_json() == {"error": "integration_temporarily_unavailable"}
    assert "cursor" not in failed.get_json()

    with database.get_connection() as conn:
        conn.execute(
            "UPDATE integration_signal_outbox SET score_path = ("
            "SELECT ledger.score_path FROM backtest_signals AS ledger "
            "WHERE ledger.id = integration_signal_outbox.ledger_signal_id) "
            "WHERE seq = 2"
        )
        conn.commit()
    retry = client.get(request_path, headers=AUTH)
    assert retry.status_code == 200
    retry_body = retry.get_json()
    assert [item["seq"] for item in retry_body["items"]] == [2]
    assert retry_body["cursor"]["after_seq"] == 1
    assert retry_body["cursor"]["next_after_seq"] == 2


def test_page_metadata_and_rows_share_one_wal_snapshot(monkeypatch):
    import services.integration_signals as integration_signals

    _init_ledger()
    now = int(time.time())
    assert database.store_backtest_signal(
        _signal("https://example.com/snapshot-1", available_at=now)
    )
    original_state = integration_signals.get_integration_outbox_state
    writer_committed = False

    def state_then_cross_process_commit(reader):
        nonlocal writer_committed
        state = original_state(reader)
        if not writer_committed:
            writer = sqlite3.connect(database.DB_PATH, timeout=5)
            writer.row_factory = sqlite3.Row
            try:
                writer.execute("PRAGMA journal_mode=WAL")
                writer.execute("BEGIN IMMEDIATE")
                assert database._insert_backtest_signal(
                    writer,
                    _signal("https://example.com/snapshot-2", available_at=now + 1),
                )
                writer.commit()
                writer_committed = True
            finally:
                writer.close()
        return state

    monkeypatch.setattr(
        integration_signals,
        "get_integration_outbox_state",
        state_then_cross_process_commit,
    )
    page = integration_signals.read_signal_revision_page(
        ledger_id=None, epoch=None, after_seq=None, limit=10
    )

    assert writer_committed is True
    assert page["state"]["last_seq"] == 1
    assert [item["seq"] for item in page["items"]] == [1]

    followup = integration_signals.read_signal_revision_page(
        ledger_id=page["state"]["ledger_id"],
        epoch=page["state"]["epoch"],
        after_seq=page["next_after_seq"],
        limit=10,
    )
    assert followup["state"]["last_seq"] == 2
    assert [item["seq"] for item in followup["items"]] == [2]


def test_initial_backfill_is_bounded_recent_and_one_time():
    now = int(time.time())
    with database.db_lock:
        with database.get_connection() as conn:
            database._init_app_meta(conn.cursor())
            database._init_backtest_signals(conn.cursor())
            rows = [
                (
                    hashlib.sha256(f"legacy-{number}".encode()).hexdigest(),
                    f"https://example.com/legacy-{number}",
                    now,
                    now,
                    now,
                    9.0,
                    "看多",
                    8.0,
                    "stage2",
                )
                for number in range(BACKFILL_MAX_ROWS + 1)
            ]
            conn.executemany(
                    "INSERT INTO backtest_signals "
                    "(signal_key, link, available_at, scored_at, recorded_at, score, "
                    "direction, aggregation_threshold, score_path) "
                    "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                rows,
            )
            init_integration_signal_outbox(conn, now=now)
            assert (
                conn.execute("SELECT COUNT(*) FROM integration_signal_outbox").fetchone()[0]
                == BACKFILL_MAX_ROWS
            )
            trim_integration_signal_outbox(
                conn, now=now, retention_days=None, max_rows=100
            )
            init_integration_signal_outbox(conn, now=now)
            assert (
                conn.execute("SELECT COUNT(*) FROM integration_signal_outbox").fetchone()[0]
                == 100
            )
            conn.commit()


def test_poison_fields_and_page_budget_are_bounded(monkeypatch):
    _init_ledger()
    now = int(time.time())
    for number in range(50):
        link = (
            "http://127.0.0.1/private?token=never-export-this"
            if number == 0
            else f"https://example.com/item-{number}?secret=never-export-this"
        )
        signal = _signal(link, available_at=now + number)
        signal["mapped_theme"] = "主题" * 10_000
        signal["direction"] = "看多" * 1_000
        assert database.store_backtest_signal(signal)

    response = _app_client(monkeypatch).get(
        "/integration/v1/signals?limit=50", headers=AUTH
    )
    body = response.get_json()

    assert response.status_code == 200
    assert len(response.data) <= 64 * 1024
    assert 1 <= body["count"] <= 50
    assert body["cursor"]["next_after_seq"] == body["items"][-1]["seq"]
    assert body["cursor"]["has_more"] is (body["count"] < 50)
    assert len(body["items"][0]["mapped_theme"].encode()) <= 192
    assert body["items"][0]["direction"] == "中性"
    assert "article_url" not in response.get_data(as_text=True)
    assert "never-export-this" not in response.get_data(as_text=True)


@pytest.mark.parametrize(
    ("column", "value"),
    [("score", -1.1), ("score", 10.1), ("score_confidence", 1.1), ("aggregation_threshold", -0.1)],
)
def test_numeric_contract_poison_fails_closed(monkeypatch, column, value):
    _init_ledger()
    assert database.store_backtest_signal(
        _signal("https://example.com/numeric", available_at=int(time.time()))
    )
    with database.get_connection() as conn:
        conn.execute(
            f"UPDATE integration_signal_outbox SET {column} = ? WHERE seq = 1",
            (value,),
        )
        conn.commit()

    response = _app_client(monkeypatch).get(
        "/integration/v1/signals", headers=AUTH
    )
    assert response.status_code == 503
    assert response.get_json() == {"error": "integration_temporarily_unavailable"}


def test_parse_error_score_sentinel_remains_local_and_is_not_exported(monkeypatch):
    _init_ledger()
    signal = _signal(
        "https://example.com/parse-error", available_at=int(time.time()), selected=False
    )
    signal.update(score=-1.0, score_path="parse_error", direction="unexpected")

    assert database.store_backtest_signal(signal) is True
    body = _app_client(monkeypatch).get(
        "/integration/v1/signals", headers=AUTH
    ).get_json()

    assert body["items"] == []
    with database.get_connection() as conn:
        row = conn.execute(
            "SELECT score, score_path, direction FROM backtest_signals "
            "WHERE link = ?",
            (signal["link"],),
        ).fetchone()
    assert tuple(row) == (-1.0, "parse_error", "中性")


def test_capacity_and_authenticated_rate_limits_fail_fast(monkeypatch):
    import routes.integration_routes as integration_routes

    _init_ledger()
    client = _app_client(monkeypatch)
    assert integration_routes._INTEGRATION_SLOTS.acquire(blocking=False)
    assert integration_routes._INTEGRATION_SLOTS.acquire(blocking=False)
    try:
        response = client.get("/integration/v1/signals", headers=AUTH)
    finally:
        integration_routes._INTEGRATION_SLOTS.release()
        integration_routes._INTEGRATION_SLOTS.release()
    assert response.status_code == 503
    assert response.get_json()["error"] == "integration_capacity_reached"

    monkeypatch.setenv("AI_FILTER_INTEGRATION_RATE_LIMIT_PER_MINUTE", "10")
    integration_routes._RATE_WINDOW_STARTED = time.monotonic()
    integration_routes._RATE_WINDOW_COUNT = 10
    response = client.get("/integration/v1/signals", headers=AUTH)
    assert response.status_code == 429
    assert response.headers["Retry-After"]


def test_formal_app_never_grants_cors_and_options_is_405(monkeypatch):
    _init_ledger()
    monkeypatch.setenv("AI_FILTER_INTEGRATION_TOKEN", TOKEN)
    monkeypatch.delenv("AI_FILTER_INTEGRATION_TOKEN_FILE", raising=False)
    monkeypatch.setenv("AI_FILTER_CORS_ALLOWED_ORIGINS", "https://quant.example")
    import app as app_module

    client = app_module.app.test_client()
    headers = {**AUTH, "Origin": "https://quant.example"}
    get_response = client.get("/integration/v1/signals", headers=headers)
    options_response = client.options("/integration/v1/signals", headers=headers)

    assert get_response.status_code == 200
    assert get_response.headers["Vary"] == "Authorization"
    assert options_response.status_code == 405
    assert options_response.headers["Allow"] == "GET"
    for response in (get_response, options_response):
        assert not any(
            name.lower().startswith("access-control-")
            for name in response.headers.keys()
        )


def test_compose_mounts_ai_integration_secret_only_into_api():
    compose = yaml.safe_load(
        (PROJECT_ROOT / "docker-compose.yml").read_text(encoding="utf-8")
    )
    services = compose["services"]

    assert compose["secrets"]["ai_filter_integration_token"]["file"] == (
        "./secrets/ai_filter_integration_token"
    )
    assert "ai_filter_integration_token" in services["ai-filter-api"]["secrets"]
    assert services["ai-filter-api"]["environment"][
        "AI_FILTER_INTEGRATION_TOKEN_FILE"
    ] == "/run/secrets/ai_filter_integration_token"
    for name, service in services.items():
        if name != "ai-filter-api":
            assert "ai_filter_integration_token" not in (service.get("secrets") or [])
