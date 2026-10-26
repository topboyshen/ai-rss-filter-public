"""Nullable score diagnostics follow their score owner, not newer metadata."""

from contextlib import nullcontext
import hashlib
import json
import sqlite3
import time

import pytest

from core.digest_engine import dedupe_articles
from scoring_explanation import EXPLANATION_FIELDS
from services import backtest, quality_bridge


EXPLANATION = {
    "raw_score": 8.25,
    "score_adjustment": -0.25,
    "score_margin": 2.0,
    "stage1_stage2_delta": 0.0,
    "fast_pass_hint_score": None,
    "score_threshold": 6.0,
}


@pytest.mark.parametrize("explanation", [EXPLANATION, dict.fromkeys(EXPLANATION)])
def test_cache_reader_preserves_frozen_diagnostics_and_nulls(monkeypatch, explanation):
    with sqlite3.connect(":memory:") as connection:
        connection.row_factory = sqlite3.Row
        connection.execute(
            "CREATE TABLE news_cache (link, title, raw_title, score, tag, logic, "
            "summary, created_at, score_confidence, feed_url, direction, score_path, "
            "strategy_version, event_id, event_version, "
            + ", ".join(EXPLANATION_FIELDS)
            + ")"
        )
        row = {
            "link": "https://example.test/cache",
            "title": "测试新闻",
            "score": 8.0,
            "score_path": "stage2",
            "created_at": int(time.time()),
            **explanation,
        }
        connection.execute(
            "INSERT INTO news_cache ("
            + ", ".join(row)
            + ") VALUES ("
            + ", ".join("?" for _ in row)
            + ")",
            tuple(row.values()),
        )
        monkeypatch.setattr(backtest, "db_lock", nullcontext())
        monkeypatch.setattr(backtest, "get_connection", lambda: connection)
        articles = backtest._load_articles(2, 0, {"scoring_strategy_version": "test"})
    assert len(articles) == 1
    assert {field: articles[0][field] for field in EXPLANATION_FIELDS} == explanation


def test_ledger_reader_preserves_zero_null_and_missing_diagnostics(monkeypatch):
    now = int(time.time())
    rows = [
        {
            "link": "new",
            "score": 8.0,
            "score_path": "stage2",
            "available_at": now,
            "selected": 1,
            **EXPLANATION,
        },
        {
            "link": "legacy",
            "score": 8.0,
            "score_path": "llm",
            "available_at": now,
            "selected": 1,
        },
    ]
    monkeypatch.setattr(backtest, "get_backtest_signals", lambda **_kwargs: rows)
    monkeypatch.setattr(
        backtest, "get_backtest_signal_stats", lambda **_kwargs: {"count": 2}
    )
    monkeypatch.setattr(backtest, "_signal_cohort_history", lambda *_args: {})
    articles, population, _stats = backtest._snapshot_articles(
        2, None, {"scoring_strategy_version": "test"}, "mapping-test"
    )
    assert articles == population
    assert {field: articles[0][field] for field in EXPLANATION_FIELDS} == EXPLANATION
    assert articles[0]["stage1_stage2_delta"] == 0.0
    assert {field: articles[1][field] for field in EXPLANATION_FIELDS} == dict.fromkeys(
        EXPLANATION_FIELDS
    )


@pytest.mark.parametrize("owner_first", [True, False])
def test_digest_diagnostics_follow_score_owner_not_freshest_wire(owner_first):
    owner = {
        "title": "同一主题的原始新闻",
        "score": 8.0,
        "score_path": "stage2",
        "created_at": 100,
        "link": "old",
        **EXPLANATION,
    }
    newer = {
        "title": owner["title"],
        "score": 7.0,
        "score_path": "llm",
        "created_at": 200,
        "link": "new",
        **{field: 99.0 for field in EXPLANATION_FIELDS},
    }
    result = dedupe_articles([owner, newer] if owner_first else [newer, owner])[0]
    assert result["link"] == "new"
    assert result["score"] == 8.0
    assert {field: result[field] for field in EXPLANATION_FIELDS} == EXPLANATION


@pytest.mark.parametrize("owner_first", [True, False])
def test_digest_legacy_score_owner_does_not_borrow_other_rows_diagnostics(owner_first):
    owner = {
        "title": "同一主题的原始新闻",
        "score": 9.0,
        "created_at": 100,
        "link": "old",
    }
    newer = {
        "title": owner["title"],
        "score": 8.0,
        "created_at": 200,
        "link": "new",
        **EXPLANATION,
    }
    result = dedupe_articles([owner, newer] if owner_first else [newer, owner])[0]
    assert result["score"] == 9.0
    assert all(field not in result for field in EXPLANATION_FIELDS)


_V1_OUTPUT_FIELDS = (
    "ledger_signal_id",
    "signal_key",
    "link",
    "event_id",
    "event_version",
    "available_at",
    "scored_at",
    "recorded_at",
    "score",
    "direction",
    "score_confidence",
    "strategy_version",
    "mapping_version",
    "mapped_theme",
    "backtest_symbol",
    "score_path",
    "revision_key",
    "revision_seq",
    "revision_state_key",
    "aggregation_threshold",
    "selected",
    "is_similarity_copy",
)
_V1_PROVENANCE_FIELDS = (
    "raw_title",
    "summary",
    "feed_url",
    "score_path",
    "prompt_key",
    "model_name",
    "stage1_score",
    "stage1_model",
    "stage2_score",
    "stage2_model",
    "score_confidence",
    "review_reason",
    "direction",
    "cache_score",
    "cache_strategy_version",
)


def _write_v1_artifact(path):
    signal = dict.fromkeys(_V1_OUTPUT_FIELDS)
    signal.update(
        ledger_signal_id=1,
        signal_key="old",
        link="https://example.test/old",
        score=8.0,
        score_confidence=0.8,
        direction="看多",
        score_path="llm",
        strategy_version="source",
        mapping_version="mapping",
        selected=1,
        is_similarity_copy=0,
        available_at=1_700_000_000,
    )
    provenance = dict.fromkeys(_V1_PROVENANCE_FIELDS)
    provenance.update(
        score_path="llm",
        model_name="kimi",
        stage1_model="kimi",
        stage1_score=8.0,
        direction="看多",
        cache_score=8.0,
        cache_strategy_version="source",
    )
    meta = {
        "artifact_schema": "quality-salvage-provenance-v1",
        "preview_only": True,
        "formal_gate_effect": False,
        "population_rule": "latest link×strategy×mapping; non-similarity",
        "counts": {"signals": 1, "cache_provenance": 1, "explicit_gpt": 0},
    }
    with sqlite3.connect(path) as connection:
        connection.execute("CREATE TABLE meta (key, value)")
        connection.execute(
            "CREATE TABLE signals (" + ", ".join(_V1_OUTPUT_FIELDS) + ")"
        )
        connection.execute(
            "CREATE TABLE cache_provenance (ledger_signal_id, "
            + ", ".join(_V1_PROVENANCE_FIELDS)
            + ")"
        )
        for table in ("evidence", "frozen_mappings", "contract_snapshot"):
            connection.execute("CREATE TABLE " + table + " (id)")
        connection.executemany(
            "INSERT INTO meta VALUES (?, ?)",
            [(key, json.dumps(value)) for key, value in meta.items()],
        )
        connection.execute(
            "INSERT INTO signals VALUES (" + ", ".join("?" for _ in signal) + ")",
            tuple(signal.values()),
        )
        connection.execute(
            "INSERT INTO cache_provenance VALUES (?, "
            + ", ".join("?" for _ in provenance)
            + ")",
            (1, *provenance.values()),
        )
    return signal, provenance


def test_bridge_v1_remains_readable_and_hash_stable_with_unknown_diagnostics(
    tmp_path, monkeypatch
):
    path = tmp_path / "v1.sqlite3"
    signal, provenance = _write_v1_artifact(path)
    old_bytes = path.read_bytes()
    artifact_hash = hashlib.sha256(old_bytes).hexdigest()
    _meta, signals, provenance_by_id = quality_bridge._read_artifact(
        path, artifact_hash
    )
    assert quality_bridge._OUTPUT_FIELDS == _V1_OUTPUT_FIELDS
    assert quality_bridge._PROVENANCE_FIELDS == _V1_PROVENANCE_FIELDS
    assert signals == [signal]
    assert provenance_by_id == {1: provenance}
    assert quality_bridge._classify(signal, provenance, "source")[0] == "eligible"
    assert (
        quality_bridge._digest(signals[0])
        == hashlib.sha256(
            json.dumps(
                signal, sort_keys=True, ensure_ascii=False, separators=(",", ":")
            ).encode()
        ).hexdigest()
    )
    # New live diagnostic columns must not alter the frozen v1 comparison.
    with sqlite3.connect(":memory:") as connection:
        connection.row_factory = sqlite3.Row
        live = {
            "id": signal["ledger_signal_id"],
            **{key: signal[key] for key in _V1_OUTPUT_FIELDS[1:]},
            **EXPLANATION,
        }
        connection.execute("CREATE TABLE backtest_signals (" + ", ".join(live) + ")")
        connection.execute(
            "INSERT INTO backtest_signals VALUES ("
            + ", ".join("?" for _ in live)
            + ")",
            tuple(live.values()),
        )
        monkeypatch.setattr(quality_bridge.database, "db_lock", nullcontext())
        monkeypatch.setattr(
            quality_bridge.database, "get_connection", lambda: connection
        )
        quality_bridge._validate_live_source(signals, "source", "mapping")
    diagnostics = quality_bridge._preview_diagnostics([])[
        "score_explanation_diagnostics"
    ]
    assert diagnostics == {
        "status": "unknown",
        "reason": "artifact_v1_does_not_capture_score_explanation",
        "fields": dict.fromkeys(EXPLANATION_FIELDS),
    }
    assert path.read_bytes() == old_bytes
