"""The title-only write contract never silently rematerializes legacy evidence."""

import hashlib

import pytest

from core.source_contract import (
    ARTICLE_CLAIM_PREFIX,
    MAPPING_CLAIM_PREFIX,
    is_current_source_claim,
    is_current_mapping_writer,
    mapping_contract_status,
)
from db import database
from db.event_core import (
    resolve_or_create_event,
    upsert_event_evidence,
    upsert_event_security,
    upsert_security,
    record_event_mapping_replay,
)
from services import event_enrichment as enrichment


@pytest.fixture
def master():
    database.init_db()
    with database.get_connection() as conn:
        for ticker, name in (("600000", "浦发银行"), ("601398", "工商银行")):
            upsert_security(
                conn, exchange="SSE", ticker=ticker, name=name,
                security_id=f"SSE:{ticker}", asset_type="equity", now=100,
            )
        conn.commit()


def _event(conn, key):
    return resolve_or_create_event(
        conn, {"article_url_sha256": key}, title=key, event_type="公司公告",
        direction="中性", priority=8, occurred_at=1_000, available_at=1_000,
        strategy_version="strategy-v1", model_version="test-model", now=1_000,
    )


def _evidence(conn, event_id, title, *, legacy=False, source="https://example.test/article"):
    snapshot = title + "\n模型生成内容：中际旭创获得大额订单" if legacy else title
    digest = hashlib.sha256(snapshot.encode()).hexdigest()
    claim_key = f"article_snapshot:{digest}" if legacy else ARTICLE_CLAIM_PREFIX + digest
    return upsert_event_evidence(
        conn, event_id, source_uri=source, source_type="article",
        claim_key=claim_key, claim_text=title,
        excerpt="模型生成内容：中际旭创获得大额订单" if legacy else "",
        snapshot_text=snapshot, snapshot_hash=digest, stance="supports",
        reliability=0.8, observed_at=1_100 if legacy else 2_000, now=2_000,
    )


def _old_mapping(conn, event_id):
    return upsert_event_security(
        conn, event_id, "SSE:600000", relation_type="direct", direction="看多",
        confidence=0.9, relevance=10, rationale="旧结果：原文直接点名浦发银行",
        mapping_version="asset-map:legacy-v0", now=1_200,
    )


def _row(conn, table, identifier, key):
    return tuple(conn.execute(f"SELECT * FROM {table} WHERE {identifier} = ?", (key,)).fetchone())


def test_legacy_only_dirty_group_is_not_selected_seeded_replayed_or_retired(master):
    with database.get_connection() as conn:
        event = _event(conn, "legacy-only")
        evidence = _evidence(conn, event, "浦发银行获得订单", legacy=True)
        mapping = _old_mapping(conn, event)
        record_event_mapping_replay(
            conn, evidence, "asset-map:legacy-v0", evidence_version=1,
            root_event_id=event, root_event_version=1,
            input_fingerprint="a" * 64, output_fingerprint="b" * 64,
            status="mapped", mapped_count=1, now=1_300,
        )
        conn.execute(
            "INSERT OR IGNORE INTO event_mapping_replay_dirty_roots(root_event_id, reason, dirty_at) "
            "VALUES (?, 'legacy-dirty', 1)", (event,),
        )
        frozen_evidence = _row(conn, "event_evidence", "evidence_id", evidence)
        frozen_mapping = _row(conn, "event_securities", "mapping_id", mapping)
        frozen_ledger = tuple(conn.execute("SELECT * FROM event_mapping_replay_ledger").fetchone())
        conn.commit()
    result = enrichment.enrich_event_security_mappings(since=0, limit=2)
    assert result["forward_only"] is True
    assert result["processed_evidence"] == result["selected_groups"] == 0
    assert result["migration_seeded_groups"] == result["retired_mappings"] == 0
    assert result["migration_complete"] is False
    assert result["forward_complete"] is True
    assert result["migration"]["scope"] == "forward_only"
    assert result["migration"]["legacy_preserved"] == 1
    assert result["migration"]["pending_evidence"] == result["migration"]["dirty_groups"] == 0
    with database.get_connection() as conn:
        assert _row(conn, "event_evidence", "evidence_id", evidence) == frozen_evidence
        assert _row(conn, "event_securities", "mapping_id", mapping) == frozen_mapping
        assert tuple(conn.execute("SELECT * FROM event_mapping_replay_ledger").fetchone()) == frozen_ledger
        assert conn.execute("SELECT COUNT(*) FROM event_mapping_replay_dirty_roots").fetchone()[0] > 0


def test_mixed_group_uses_only_new_evidence_and_preserves_old_rows(master):
    with database.get_connection() as conn:
        event = _event(conn, "mixed-group")
        old_evidence = _evidence(conn, event, "浦发银行获得订单", legacy=True)
        old_mapping = _old_mapping(conn, event)
        new_evidence = _evidence(conn, event, "工商银行发布公告")
        frozen_evidence = _row(conn, "event_evidence", "evidence_id", old_evidence)
        frozen_mapping = _row(conn, "event_securities", "mapping_id", old_mapping)
        conn.commit()
    result = enrichment.enrich_event_security_mappings(since=0, limit=10)
    assert result["processed_evidence"] == 1
    assert is_current_mapping_writer(result["mapping_contract"])
    assert len(result["mapping_contract"]) <= 255
    with database.get_connection() as conn:
        assert _row(conn, "event_evidence", "evidence_id", old_evidence) == frozen_evidence
        assert _row(conn, "event_securities", "mapping_id", old_mapping) == frozen_mapping
        assert [row[0] for row in conn.execute("SELECT evidence_id FROM event_mapping_replay_ledger")] == [new_evidence]
        bound = conn.execute(
            "SELECT claim_key,claim_text,excerpt,snapshot_text FROM event_evidence "
            "WHERE mapping_id IS NOT NULL AND security_id = 'SSE:601398'",
        ).fetchall()
        assert bound and all(is_current_source_claim(row[0]) for row in bound)
        assert all(tuple(row[1:]) == ("工商银行发布公告", "", "工商银行发布公告") for row in bound)
    second = enrichment.enrich_event_security_mappings(since=0, limit=10)
    assert second["processed_evidence"] == 0


def test_old_dirty_backlog_does_not_use_forward_batch_budget(master):
    with database.get_connection() as conn:
        for number in range(5):
            event = _event(conn, f"legacy-backlog-{number}")
            _evidence(conn, event, f"浦发银行旧公告{number}", legacy=True, source=f"https://example.test/old/{number}")
        new_event = _event(conn, "new-after-backlog")
        new_evidence = _evidence(conn, new_event, "工商银行发布公告")
        conn.commit()
    result = enrichment.enrich_event_security_mappings(since=0, limit=1)
    assert result["processed_evidence"] == 1
    assert result["selected_groups"] == 1
    with database.get_connection() as conn:
        assert [row[0] for row in conn.execute("SELECT evidence_id FROM event_mapping_replay_ledger")] == [new_evidence]


def test_new_writer_identity_and_legacy_opt_in_identity_are_disjoint(master):
    with database.get_connection() as conn:
        forward, _ = enrichment._mapping_contract_version(conn)
        historical, _ = enrichment._mapping_contract_version(conn, include_legacy=True)
    assert forward != historical
    assert is_current_mapping_writer(forward)
    assert not is_current_mapping_writer(historical)
    assert mapping_contract_status(forward, current_versions=(forward,)) == "current"
    assert mapping_contract_status(forward, current_versions=()) == "historical"
    assert mapping_contract_status(historical, current_versions=(historical,)) == "legacy_unknown"


def test_new_observation_may_handover_current_without_rewriting_old_payload(master):
    with database.get_connection() as conn:
        event = _event(conn, "natural-handover")
        old_evidence = _evidence(conn, event, "浦发银行获得旧订单", legacy=True)
        old_mapping = _old_mapping(conn, event)
        _evidence(conn, event, "浦发银行发布新公告")
        frozen_evidence = _row(conn, "event_evidence", "evidence_id", old_evidence)
        old_row = dict(conn.execute("SELECT * FROM event_securities WHERE mapping_id=?", (old_mapping,)).fetchone())
        conn.commit()
    result = enrichment.enrich_event_security_mappings(since=0, limit=10)
    assert result["processed_evidence"] == 1
    with database.get_connection() as conn:
        after = dict(conn.execute("SELECT * FROM event_securities WHERE mapping_id=?", (old_mapping,)).fetchone())
        assert after.pop("is_current") == 0
        assert old_row.pop("is_current") == 1
        assert after == old_row
        assert _row(conn, "event_evidence", "evidence_id", old_evidence) == frozen_evidence
        current = conn.execute("SELECT mapping_version FROM event_securities WHERE event_id=? AND is_current=1", (event,)).fetchone()
        assert is_current_mapping_writer(current[0])


@pytest.mark.parametrize("title,legacy,expected_concept", [
    ("浦发银行发布年度股东大会通知", False, False),
    ("人工智能算力需求增长", False, True),
    ("浦发银行发布年度股东大会通知", True, True),
])
def test_forward_rule_mapping_uses_title_not_generated_event_theme(master, title, legacy, expected_concept):
    with database.get_connection() as conn:
        upsert_security(
            conn, exchange="SZSE", ticker="300308", name="中际旭创",
            security_id="SZSE:300308", asset_type="equity", now=100,
        )
        event = _event(conn, "source-title-vs-generated-theme")
        conn.execute("UPDATE events SET event_type='AI算力', direction='看多' WHERE event_id=?", (event,))
        _evidence(conn, event, title, legacy=legacy)
        conn.commit()
    result = enrichment.enrich_event_security_mappings(since=0, limit=10, include_legacy=legacy)
    assert result["processed_evidence"] == 1
    with database.get_connection() as conn:
        concepts = conn.execute(
            "SELECT mapping_version FROM event_securities WHERE event_id=? "
            "AND security_id='SZSE:300308' AND relation_type='concept' AND is_current=1",
            (event,),
        ).fetchall()
        directions = conn.execute(
            "SELECT direction FROM event_securities WHERE event_id=? AND is_current=1",
            (event,),
        ).fetchall()
    assert bool(concepts) is expected_concept
    assert all(is_current_mapping_writer(row[0]) is not legacy for row in concepts)
    assert directions and all(row[0] == "看多" for row in directions)


@pytest.mark.parametrize("key", [
    "", "article_snapshot:" + "a" * 64,
    ARTICLE_CLAIM_PREFIX + "a" * 63, ARTICLE_CLAIM_PREFIX + "x" * 64,
    MAPPING_CLAIM_PREFIX + "missing-identities",
])
def test_unversioned_or_malformed_claims_are_never_current_source(key):
    assert not is_current_source_claim(key)


@pytest.mark.parametrize("key", [
    "articleXsnapshot:publisher-title-v2:" + "a" * 64,
    ARTICLE_CLAIM_PREFIX + "x" * 64,
    ARTICLE_CLAIM_PREFIX + "a" * 63,
])
def test_malformed_stored_claim_identity_never_enters_forward_lane(master, key):
    with database.get_connection() as conn:
        event = _event(conn, "malformed-prefix")
        upsert_event_evidence(
            conn, event, source_uri="https://example.test/malformed",
            claim_key=key, claim_text="工商银行发布公告", excerpt="",
            snapshot_text="工商银行发布公告", stance="supports",
            reliability=0.8, observed_at=2_000, now=2_000,
        )
        conn.commit()
    result = enrichment.enrich_event_security_mappings(since=0, limit=1)
    assert result["processed_evidence"] == 0
    assert result["selected_groups"] == 0
