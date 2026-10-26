"""Longer organization names are not occurrences of a listed issuer."""

import hashlib

import pytest

from core.asset_mapping import (
    is_direct_security_mention,
    is_direct_security_ticker_mention,
)
from db import database
from db.event_core import resolve_or_create_event, upsert_security
from services.event_daily_report import _is_named


IDENTITIES = (
    ("中国建筑", "601668", "中国建筑业协会"),
    ("中国银行", "601988", "中国银行业协会"),
    ("中国化学", "601117", "中国化学会"),
    ("中国软件", "600536", "中国软件行业协会"),
)
ORGANIZATION_TEMPLATES = (
    "{organization}发布行业报告",
    "【{organization}】：公布行业名单",
    "『{organization}』发布行业报告",
    "{spaced}　\t发布行业报告",
    "{organization}（{ticker}）发布行业报告",
    "{organization}（证券代码：{ticker}）发布行业报告",
    "{organization}：股票代码{ticker}，发布行业报告",
    "{organization}（SH{ticker}）发布行业报告",
    "{organization}（{ticker}.SH）发布行业报告",
    "{organization}（SSE:{ticker}）发布行业报告",
    "『{organization}』（{ticker}.SH）发布行业报告",
    "【“{organization}”】（{ticker}.SH）发布行业报告",
    "《{organization}》（{ticker}.SH）发布行业报告",
    "{organization}[SH{ticker}]发布行业报告",
    "{organization}【证券代码{ticker}】发布行业报告",
    "{organization}（股票代码：SSE:{ticker}）发布行业报告",
)
ISSUER_TEMPLATES = (
    "{name}发布年度业绩公告",
    "{name}（{ticker}）发布年度业绩公告",
    "{name}（{ticker}.SH）发布年度业绩公告",
    "证券代码{ticker}：公司发布年度业绩公告",
    "{organization}发布报告；{name}发布年度业绩公告",
    "{name}发布年度业绩公告；{organization}发布报告",
    "{organization}发布报告；证券代码{ticker}发布年度业绩公告",
    "{organization}（{ticker}.SH）发布报告；证券代码{ticker}：公司发布年报",
    "{name}与{organization}开展合作",
    "{name}（{ticker}）与{organization}开展合作",
    "{name}参加行业协会活动",
)


def _title(template, name, ticker, organization):
    return template.format(
        name=name, ticker=ticker, organization=organization,
        spaced="　 ".join(organization),
    )


@pytest.fixture
def organization_master(monkeypatch):
    monkeypatch.setattr(database, "_EQUITY_NAME_CACHE", ("", (0, 0, 0), 0.0, []))
    monkeypatch.setattr(database, "_EQUITY_MENTION_INDEX_CACHE", (None, None))
    database.init_db()
    with database.get_connection() as conn:
        for name, ticker, _organization in IDENTITIES:
            upsert_security(
                conn, exchange="SSE", ticker=ticker, name=name,
                security_id=f"SSE:{ticker}", asset_type="equity", now=100,
            )
        conn.commit()


def _assert_card_and_written_identities(title, expected):
    result = database.get_direct_equity_mentions_batch([title])[0]
    assert result["raw_title"] == title and result["status"] == "ok"
    with database.get_connection() as conn:
        digest = hashlib.sha256(title.encode()).hexdigest()
        event_id = resolve_or_create_event(
            conn, {"article_url_sha256": digest}, title=title, event_type="公司公告",
            direction="中性", priority=8, occurred_at=1_000, available_at=1_000,
            strategy_version="strategy-v1", model_version="test-model", now=1_000,
        )
        mapping_ids = database.upsert_real_event_mappings(
            conn, event_id=event_id, article_text=title, source_text=title,
            source_uri="", direction="中性", mapping_version="mapping-v1", now=1_001,
        )
        rows = conn.execute(
            "SELECT security_id, rationale FROM event_securities "
            "WHERE event_id = ? AND is_current = 1", (event_id,),
        ).fetchall()
        assert (
            [item["security_id"] for item in result["mentions"]], [row[0] for row in rows],
        ) == (expected, expected)
        assert len(mapping_ids) == len(expected)
        return rows


@pytest.mark.parametrize("name,ticker,organization", IDENTITIES)
@pytest.mark.parametrize("template", ORGANIZATION_TEMPLATES)
def test_organization_span_is_not_an_issuer_or_ticker_anchor(
    organization_master, name, ticker, organization, template,
):
    title = _title(template, name, ticker, organization)
    _assert_card_and_written_identities(title, [])
    assert not is_direct_security_mention(title, name)
    assert not is_direct_security_ticker_mention(title, name, ticker, exchange="SSE")
    # The read-side report must also reject an existing stale mapping candidate.
    assert not _is_named(name, [title], frozenset(row[0] for row in IDENTITIES))


@pytest.mark.parametrize("name,ticker,organization", IDENTITIES)
@pytest.mark.parametrize("template", ISSUER_TEMPLATES)
def test_independent_issuer_or_code_survives_organization_elsewhere(
    organization_master, name, ticker, organization, template,
):
    title = _title(template, name, ticker, organization)
    _assert_card_and_written_identities(title, [f"SSE:{ticker}"])


@pytest.mark.parametrize("name,ticker,organization", IDENTITIES)
def test_real_name_elsewhere_does_not_validate_the_organization_attached_code(
    organization_master, name, ticker, organization,
):
    title = f"{name}发布业绩；{organization}（证券代码{ticker}.SH）公布名单"
    assert is_direct_security_mention(title, name)
    assert not is_direct_security_ticker_mention(title, name, ticker, exchange="SSE")
    rows = _assert_card_and_written_identities(title, [f"SSE:{ticker}"])
    assert rows[0][1] == f"原文直接点名{name}。"


@pytest.mark.parametrize("title", (
    "中国化学会同合作方签订合同",
    "中国化学会计政策变更公告",
    "中国化学（601117）与中国化学会开展合作",
    "中国银行与中国银行业协会开展合作",
    "中国建筑和中国建筑业协会开展合作",
))
def test_issuer_followed_by_verb_or_an_independent_organization(organization_master, title):
    ticker = "601988" if title.startswith("中国银行") else (
        "601668" if title.startswith("中国建筑") else "601117"
    )
    _assert_card_and_written_identities(title, [f"SSE:{ticker}"])


def test_rejected_organization_does_not_hide_a_different_issuer(organization_master):
    _assert_card_and_written_identities(
        "中国建筑业协会发布报告；中国银行（601988）发布年报", ["SSE:601988"],
    )


@pytest.mark.parametrize("name,ticker,organization", IDENTITIES)
@pytest.mark.parametrize("suffix", ("协会", "行业协会", "产业协会", "工业协会", "行业联合会", "商会"))
def test_adjacent_organization_grammar_covers_unlisted_full_names(
    organization_master, name, ticker, organization, suffix,
):
    title = f"{name}{suffix}发布行业报告"
    assert not is_direct_security_mention(title, name)
    _assert_card_and_written_identities(title, [])


@pytest.mark.parametrize("attached_code", ("601988.SH", "证券代码601988", "股票代码：SSE:601988"))
def test_organization_attached_code_cannot_name_a_different_company(
    organization_master, attached_code,
):
    title = f"【“中国建筑业协会”】（{attached_code}）发布行业报告"
    assert not is_direct_security_ticker_mention(title, "中国银行", "601988", exchange="SSE")
    _assert_card_and_written_identities(title, [])
    _assert_card_and_written_identities(
        title + "；中国银行（601988）发布年报", ["SSE:601988"],
    )
