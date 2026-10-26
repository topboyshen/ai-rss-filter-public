"""Regressions for reuse, plan scope, subjectless claims, and identity."""

import sqlite3
from types import SimpleNamespace

import pytest

from core.asset_mapping import (
    is_direct_security_ticker_mention,
    map_article_to_assets,
)
from core.processor import _process_entry_similarity
from core.score_evidence import event_reuse_conflicts, explicit_event_claims_are_grounded
from core.scorer import _score_grounding_verdict
from db import database
from db.event_core import init_event_core_schema, resolve_or_create_event, upsert_security


def test_similarity_candidate_conflicts_when_negation_flips():
    assert event_reuse_conflicts("甲公司未获得订单", "甲公司获得订单")
    assert not event_reuse_conflicts("甲公司获得订单", "甲公司获得订单")


def test_similarity_does_not_reuse_rumor_denial_or_issuer_swap():
    assert event_reuse_conflicts("订单传闻被否认", "甲公司获得订单")
    assert event_reuse_conflicts("甲公司订单传闻被否认", "甲公司获得订单")
    assert event_reuse_conflicts("乙公司增持", "甲公司增持")
    assert event_reuse_conflicts("乙公司营收下降10%", "甲公司营收增长20%")
    assert event_reuse_conflicts("甲公司获得订单", "甲公司获得3亿元订单")


def test_process_entry_keeps_similar_candidate_when_rescoring(monkeypatch):
    row = (
        "old-link",
        "甲公司获得订单",
        "甲公司获得订单",
        9.0,
        "公司订单",
        "订单落地改善业绩",
        "看多",
    )
    monkeypatch.setattr("core.processor.find_similar_article", lambda *a: (row, 0.95))
    monkeypatch.setattr("core.processor._should_rescore_similar", lambda *a: False)
    ctx = SimpleNamespace(
        raw_title="订单传闻被否认",
        cfg={"threshold": {"similarity": 0.8}},
        entry=SimpleNamespace(link="new-link"),
        feed_url="",
    )
    similar_info, similar_result = _process_entry_similarity(ctx, [row], 8)
    assert similar_result is None
    assert similar_info is not None
    assert similar_info["score"] == 9.0


def test_plan_modifier_applies_only_to_the_next_action():
    source = "甲公司建设工厂并计划获得订单"
    assert _score_grounding_verdict(
        (8, "公司公告", "甲公司计划获得订单", "看多"), source, ""
    ) == ("", "")
    assert _score_grounding_verdict(
        (8, "公司公告", "甲公司获得订单", "看多"), source, ""
    ) == ("ungrounded_score_facts", "event_fact_failure")
    assert _score_grounding_verdict(
        (8, "公司公告", "甲公司获得监管批复", "看多"),
        "甲公司并购计划未获得监管批复",
        "",
    ) == ("ungrounded_score_facts", "event_fact_failure")
    assert _score_grounding_verdict(
        (8, "公司公告", "甲公司计划获得补助", "看多"),
        "甲公司计划建设工厂并计划获得补助",
        "",
    ) == ("", "")


def test_subjectless_order_claim_is_not_skipped():
    assert _score_grounding_verdict(
        (8, "公司公告", "获得大额订单", "看多"),
        "海天味业发布年度股东大会通知",
        "公司将于近期召开年度股东大会。",
    ) == ("ungrounded_score_facts", "event_fact_failure")


def test_subjectless_claim_cannot_borrow_unrelated_or_ambiguous_facts():
    assert explicit_event_claims_are_grounded("获得大额订单", "公司获得补助") is False
    assert explicit_event_claims_are_grounded(
        "获得订单", "甲公司获得订单；乙公司获得订单"
    ) is False
    assert explicit_event_claims_are_grounded("获得订单", "甲公司获得订单") is True


def test_model_summary_is_not_a_direct_source_mention():
    mapping = map_article_to_assets({
        "title": "普通公司公告",
        "raw_title": "普通公司公告",
        "summary": "寒武纪发布新品",
        "tag": "AI算力",
        "logic": "寒武纪受益",
    })
    rationales = [
        item.get("rationale")
        for item in mapping.get("security_candidates") or []
        if item.get("name") == "寒武纪"
    ]
    assert rationales
    assert all("原文直接点名" not in str(item) for item in rationales)


def test_amount_is_not_a_security_code_without_identity():
    assert not is_direct_security_ticker_mention(
        "补贴600000元", "浦发银行", "600000", exchange="SSE"
    )
    assert not is_direct_security_ticker_mention(
        "新增300308名用户", "中际旭创", "300308", exchange="SZSE"
    )
    assert not is_direct_security_ticker_mention(
        "补贴600000美元", "浦发银行", "600000", exchange="SSE"
    )
    assert not is_direct_security_ticker_mention(
        "金额600000.25元", "浦发银行", "600000", exchange="SSE"
    )
    assert not is_direct_security_ticker_mention(
        "快递单号600000已签收", "浦发银行", "600000", exchange="SSE"
    )
    assert not is_direct_security_ticker_mention(
        "快递单号（600000）已签收", "浦发银行", "600000", exchange="SSE"
    )
    assert is_direct_security_ticker_mention(
        "浦发银行（600000）上涨", "浦发银行", "600000", exchange="SSE"
    )


@pytest.fixture
def identity_mapping_connection():
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    init_event_core_schema(conn)
    for ticker, name in (("600000", "浦发银行"), ("600030", "中信证券")):
        upsert_security(
            conn,
            exchange="SSE",
            ticker=ticker,
            name=name,
            security_id=f"SSE:{ticker}",
            now=100,
        )
    try:
        yield conn
    finally:
        conn.close()


@pytest.mark.parametrize(
    ("title", "security_id"),
    [
        ("产品代码600000已停产", None),
        ("运单代码600000已签收", None),
        ("产品代码：600000已停产", None),
        ("运单代码=600000已签收", None),
        ("产品代码（600000）已停产", None),
        ("运单代码 600000 已签收", None),
        ("证券代码600000：公司发布年度业绩", "SSE:600000"),
        ("股票代码：600000：公司发布年度业绩", "SSE:600000"),
        ("A股代码=600000：公司发布年度业绩", "SSE:600000"),
        ("浦发银行（600000）发布年度业绩", "SSE:600000"),
        ("浦发银行代码600000：公司发布年度业绩", "SSE:600000"),
        ("SH600000发布年度业绩", "SSE:600000"),
        ("600000.SH发布年度业绩", "SSE:600000"),
        ("沪600000发布年度业绩", "SSE:600000"),
        ("SZ600000发布年度业绩", None),
        ("600000.SZ发布年度业绩", None),
        ("中信证券（600030）：看好电子板块", None),
        ("中信证券（600030）：公司发布半年报", "SSE:600030"),
        ("证券代码600030：公司拟回购股份", "SSE:600030"),
    ],
)
def test_mapping_writer_requires_security_identity_not_generic_code(
    identity_mapping_connection, title, security_id
):
    conn = identity_mapping_connection
    event_id = resolve_or_create_event(conn, {"test": title}, title=title, now=100)
    mapping_ids = database.upsert_real_event_mappings(
        conn,
        event_id=event_id,
        article_text=title,
        source_text=title,
        source_uri="https://example.invalid/identity",
        direction="中性",
        mapping_version="asset-map:test-code-identity",
        now=200,
    )
    rows = conn.execute(
        "SELECT security_id, relation_type FROM event_securities WHERE event_id = ?",
        (event_id,),
    ).fetchall()
    if security_id is None:
        assert mapping_ids == []
        assert rows == []
    else:
        assert len(mapping_ids) == 1
        assert [tuple(row) for row in rows] == [(security_id, "direct")]


def test_event_mapping_does_not_treat_summary_as_source_naming(tmp_path, monkeypatch):
    monkeypatch.setattr(database, "DB_PATH", str(tmp_path / "mentions.sqlite"))
    database.init_db()
    with database.get_connection() as conn:
        upsert_security(
            conn,
            exchange="SZSE",
            ticker="300308",
            name="中际旭创",
            security_id="SZSE:300308",
            asset_type="equity",
            aliases=["中际旭创", "300308"],
            now=100,
        )
        conn.commit()
        polluted = database._resolved_real_event_mapping_candidates(
            conn,
            article_text="普通公司公告 中际旭创发布新品",
            source_text="普通公司公告",
        )
        assert polluted == []
        named = database._resolved_real_event_mapping_candidates(
            conn,
            article_text="中际旭创发布新品",
            source_text="中际旭创发布新品",
        )
        rationales = [item.get("rationale") for item, _row in named]
        assert any("原文直接点名中际旭创" in str(item) for item in rationales)


def test_radar_template_does_not_present_excerpt_as_original_source():
    from pathlib import Path

    text = Path("templates/event_radar.html").read_text(encoding="utf-8")
    assert "存档摘录（不作为原文证据）" in text
    assert "selected.summary or selected.excerpt" not in text
