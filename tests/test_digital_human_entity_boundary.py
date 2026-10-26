"""Currency/product nouns must not become the listed issuer 数字人."""

import hashlib

import pytest

from core.asset_mapping import is_direct_security_mention
from core.source_contract import ARTICLE_CLAIM_PREFIX
from db import database
from db.event_core import (
    record_event_mapping_replay,
    resolve_or_create_event,
    upsert_event_evidence,
    upsert_event_security,
    upsert_security,
)
from services import event_enrichment as enrichment
from services.event_enrichment import _mapping_contract_version


COMMON_WORD_TITLES = (
    "数字人民币跨境联通合作范围稳步拓展",
    "央行：数字人民币试点范围进一步扩大",
    "A股数字人民币概念股表现活跃",
    "上市公司数字人民币业务发展加快",
    "数字人直播行业发展加快",
    "AI数字人技术应用范围扩大",
    "数字人：行业需求有望增长",
    "数字人发布新产品的技术门槛降低",
    "数字人拟人化技术提升",
    "上市公司数字人直播产业发展",
    "数字人民币项目编号（920670）已完成",
    "数字人直播项目编号（920670）已完成",
)
ISSUER_TITLES = (
    "数字人：公司发布年度业绩",
    "数字人公告拟回购股份",
    "数字人发布公告：上半年营收增长",
    "数字人触及涨停，成交额增长",
    "上市公司数字人推出新产品",
    "数字人（920670）发布年度业绩",
    "数字人（920670.BJ）发布年度业绩",
    "数字人民币业务拓展；数字人：公司发布年度业绩",
)


@pytest.mark.parametrize("title", COMMON_WORD_TITLES)
def test_direct_name_rejects_currency_and_digital_human_common_nouns(title):
    assert not is_direct_security_mention(title, "数字人")


@pytest.mark.parametrize("title", [title for title in ISSUER_TITLES if ".BJ" not in title])
def test_direct_name_keeps_independent_listed_issuer_mentions(title):
    assert is_direct_security_mention(title, "数字人")


@pytest.fixture
def digital_human_master():
    database.init_db()
    with database.get_connection() as conn:
        for exchange, ticker, name in (
            ("BSE", "920670", "数字人"), ("SZSE", "300024", "机器人"),
            ("SZSE", "000061", "农产品"), ("SSE", "600030", "中信证券"),
        ):
            upsert_security(conn, exchange=exchange, ticker=ticker, name=name,
                            security_id=f"{exchange}:{ticker}", asset_type="equity", now=100)
        conn.commit()


@pytest.mark.parametrize("title,matched", (
    *((title, False) for title in COMMON_WORD_TITLES),
    *((title, True) for title in ISSUER_TITLES),
    ("证券代码920670：公司发布年度业绩", True),
    ("920670.BJ发布年度业绩", True),
))
def test_card_lookup_and_event_writer_agree_on_digital_human_identity(
    digital_human_master, title, matched,
):
    result = database.get_direct_equity_mentions_batch([title])[0]
    assert result["status"] == "ok"
    assert [item["security_id"] for item in result["mentions"]] == (
        ["BSE:920670"] if matched else []
    )
    with database.get_connection() as conn:
        digest = hashlib.sha256(title.encode()).hexdigest()
        source = f"https://example.test/{digest}"
        event_id = resolve_or_create_event(
            conn, {"article_url_sha256": digest}, title=title,
            event_type="公司公告", direction="中性", priority=8,
            occurred_at=1_000, available_at=1_000,
            strategy_version="strategy-v1", model_version="test-model", now=1_000,
        )
        upsert_event_evidence(
            conn, event_id, source_uri=source, source_type="article",
            claim_key=ARTICLE_CLAIM_PREFIX + digest, claim_text=title,
            excerpt="", snapshot_text=title, snapshot_hash=digest,
            stance="supports", reliability=0.8, observed_at=1_000, now=1_000,
        )
        contract, _ = _mapping_contract_version(conn)
        mapping_ids = database.upsert_real_event_mappings(
            conn, event_id=event_id, article_text=title, source_text=title,
            source_uri=source, direction="中性", mapping_version=contract, now=1_001,
        )
        assert len(mapping_ids) == int(matched)
        rows = conn.execute(
            "SELECT security_id, rationale FROM event_securities "
            "WHERE event_id = ? AND is_current = 1", (event_id,),
        ).fetchall()
        assert [row[0] for row in rows] == (["BSE:920670"] if matched else [])
        if not matched:
            assert not any("原文直接点名数字人" in row[1] for row in rows)


@pytest.mark.parametrize("title,name", [
    ("机器人（920670.BJ）发布年度业绩", "机器人"),
    ("农产品（600030.SH）发布年度业绩", "农产品"),
    ("数字人（920670.BJ）发布年度业绩", "数字人"),
])
def test_qualified_code_is_not_a_pure_name_identity_without_master(title, name):
    assert not is_direct_security_mention(title, name)


@pytest.mark.parametrize("title,expected", [
    ("机器人（920670.BJ）发布年度业绩", []),
    ("农产品（600030.SH）发布年度业绩", []),
    ("数字人（600030.SH）发布年度业绩", []),
    ("数字人（920670.SZ）发布年度业绩", []),
    ("数字人（SZ920670）发布年度业绩", []),
    ("数字人（920670.HK）发布年度业绩", []),
    ("数字人（600030）发布年度业绩", []),
    ("机器人（300024.SH）发布年度业绩", []),
    ("机器人（300024.SZ）发布公告", ["SZSE:300024"]),
    ("农产品（000061.SZ）发布公告", ["SZSE:000061"]),
    ("数字人（920670.BJ）发布年度业绩", ["BSE:920670"]),
])
def test_card_and_persistent_writer_require_same_master_name_code_market(
    digital_human_master, title, expected,
):
    result = database.get_direct_equity_mentions_batch([title])[0]
    assert [item["security_id"] for item in result["mentions"]] == expected
    with database.get_connection() as conn:
        event_id = resolve_or_create_event(
            conn, {"article_url_sha256": title}, title=title, event_type="公司公告",
            direction="中性", priority=8, occurred_at=1_000, available_at=1_000,
            strategy_version="strategy-v1", model_version="test-model", now=1_000,
        )
        contract, _ = _mapping_contract_version(conn)
        mapping_ids = database.upsert_real_event_mappings(
            conn, event_id=event_id, article_text=title, source_text=title,
            source_uri="", direction="中性", mapping_version=contract, now=1_001,
            # A theme-proposed direct name must not bypass the same check.
            candidates=[{"name": title.split("（")[0], "relation_type": "direct"}],
        )
        assert len(mapping_ids) == len(expected)
        assert [row[0] for row in conn.execute(
            "SELECT security_id FROM event_securities WHERE event_id = ? AND is_current = 1",
            (event_id,),
        )] == expected


def _seed_frozen_wrong_mapping(conn, title, *, source):
    digest = hashlib.sha256(title.encode()).hexdigest()
    event = resolve_or_create_event(
        conn, {"article_url_sha256": source}, title=title, event_type="公司公告",
        direction="中性", priority=8, occurred_at=1_000, available_at=1_000,
        strategy_version="strategy-frozen", model_version="test-model", now=1_000,
    )
    evidence = upsert_event_evidence(
        conn, event, source_uri=source, source_type="article",
        claim_key=ARTICLE_CLAIM_PREFIX + digest, claim_text=title,
        excerpt="", snapshot_text=title, snapshot_hash=digest,
        stance="supports", reliability=0.8, observed_at=1_000, now=1_000,
    )
    old_contract = "asset-map:title-only-v2:old-rules"
    mapping = upsert_event_security(
        conn, event, "BSE:920670", relation_type="direct", direction="中性",
        confidence=0.9, relevance=10, rationale="原文直接点名数字人。",
        mapping_version=old_contract, now=1_001,
    )
    database._bind_mapping_evidence(
        conn, event_id=event, mapping_id=mapping,
        security_id="BSE:920670", source_uri=source,
    )
    record_event_mapping_replay(
        conn, evidence, old_contract, evidence_version=1,
        root_event_id=event, root_event_version=1,
        input_fingerprint="a" * 64, output_fingerprint="b" * 64,
        status="mapped", mapped_count=1, now=1_001,
    )
    return event, evidence, mapping


def test_exact_group_correction_preserves_frozen_payloads_and_other_events(digital_human_master):
    """Existing primitive can correct one root; ordinary refresh is not proof."""
    with database.get_connection() as conn:
        event, evidence, mapping = _seed_frozen_wrong_mapping(
            conn, COMMON_WORD_TITLES[0], source="https://example.test/currency",
        )
        _other_event, _other_evidence, other_mapping = _seed_frozen_wrong_mapping(
            conn, "数字人公告拟回购股份", source="https://example.test/issuer",
        )
        before = dict(conn.execute(
            "SELECT * FROM event_securities WHERE mapping_id = ?", (mapping,),
        ).fetchone())
        other_before = tuple(conn.execute(
            "SELECT * FROM event_securities WHERE mapping_id = ?", (other_mapping,),
        ).fetchone())
        evidence_before = [tuple(row) for row in conn.execute(
            "SELECT * FROM event_evidence WHERE event_id = ? ORDER BY evidence_id", (event,),
        )]
        old_ledger = tuple(conn.execute(
            "SELECT * FROM event_mapping_replay_ledger WHERE evidence_id = ?", (evidence,),
        ).fetchone())
        contract, _ = _mapping_contract_version(conn)
        root, rows = enrichment._mapping_group_rows(conn, event)
        result = enrichment._materialize_mapping_group(
            conn, root=root, evidence_rows=rows, mapping_contract=contract,
            input_fingerprint=enrichment._mapping_group_input_fingerprint(root, rows, contract),
            processing_time=2_000,
        )
        assert result == {"processed": 1, "mapped": 0, "mapping_rows": 0, "retired": 1}
        after = dict(conn.execute(
            "SELECT * FROM event_securities WHERE mapping_id = ?", (mapping,),
        ).fetchone())
        assert after.pop("is_current") == 0 and before.pop("is_current") == 1
        assert after == before
        assert tuple(conn.execute(
            "SELECT * FROM event_securities WHERE mapping_id = ?", (other_mapping,),
        ).fetchone()) == other_before
        assert [tuple(row) for row in conn.execute(
            "SELECT * FROM event_evidence WHERE event_id = ? ORDER BY evidence_id", (event,),
        )] == evidence_before
        assert tuple(conn.execute(
            "SELECT * FROM event_mapping_replay_ledger WHERE evidence_id = ? AND mapping_version = ?",
            (evidence, "asset-map:title-only-v2:old-rules"),
        ).fetchone()) == old_ledger
        assert tuple(conn.execute(
            "SELECT status, mapped_count FROM event_mapping_replay_ledger "
            "WHERE evidence_id = ? AND mapping_version = ?", (evidence, contract),
        ).fetchone()) == ("no_match", 0)
