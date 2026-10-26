"""Country/industry phrases are not literal mentions of 国新能源."""

import pytest

from core.asset_mapping import (
    is_direct_security_mention,
    is_direct_security_ticker_mention,
)
from db import database
from db.event_core import resolve_or_create_event, upsert_security
from services.news_mentions import attach_title_mentions
import sse_engine
from test_home_news_reading import _entry


NUMBER_TITLES = (
    "中国新能源（600617）产业发展报告",
    "中国新能源车项目编号（600617）已完成",
    # The middle case puts 国新能源 at the 16-character lookup window's
    # first position; checking that truncated window would lose the 中.
    *("中国新能源" + "甲" * (gap - 1) + "（600617）已完成" for gap in (11, 12, 13)),
    "中国新能源车测试项目内部编号值为（600617）已完成",
)
INDUSTRY_TITLES = (
    "中国新能源车9月销量同比转负12%，乘用车零售降24%",
    "中国新能源汽车销量同比增长",
    "上市公司中国新能源车业务增长",
    "中国新能源公司发布年度报告",
    "我国新能源产业发展加快",
    "全国新能源装机规模增长",
    "各国新能源产业政策比较",
    "中 国 新能源车销量增长",
) + NUMBER_TITLES
ISSUER_TITLES = (
    "国新能源发布年度业绩公告",
    "国新能源：公司拟回购股份",
    "看好国新能源发展前景",
    "国新能源（600617）发布年度业绩",
    "国新能源（600617.SH）发布年度业绩",
    "中国新能源车需求变化；国新能源发布年度业绩公告",
    "国新能源发布年度业绩公告；中国新能源车需求变化",
)
CODE_TITLES = (
    "证券代码600617：公司发布年度业绩",
    "SH600617发布年度业绩",
    "600617.SH发布年度业绩",
    "中国新能源行业报告；证券代码600617发布年度业绩",
)


@pytest.mark.parametrize("title", INDUSTRY_TITLES)
def test_country_industry_phrase_is_not_a_direct_issuer_name(title):
    assert not is_direct_security_mention(title, "国新能源")


@pytest.mark.parametrize("title", ISSUER_TITLES)
def test_independent_issuer_occurrence_is_preserved(title):
    assert is_direct_security_mention(title, "国新能源")


@pytest.mark.parametrize("title", NUMBER_TITLES)
def test_parenthesized_number_cannot_rescue_a_false_name_anchor(title):
    assert not is_direct_security_ticker_mention(
        title, "国新能源", "600617", exchange="SSE",
    )


def test_other_real_name_cannot_rescue_the_number_near_a_country_phrase():
    title = "国新能源发布公告；中国新能源（600617）销量变化"
    assert is_direct_security_mention(title, "国新能源")
    assert not is_direct_security_ticker_mention(
        title, "国新能源", "600617", exchange="SSE",
    )


@pytest.fixture
def new_energy_master(monkeypatch):
    monkeypatch.setattr(database, "_EQUITY_NAME_CACHE", ("", (0, 0, 0), 0.0, []))
    monkeypatch.setattr(database, "_EQUITY_MENTION_INDEX_CACHE", (None, None))
    database.init_db()
    with database.get_connection() as conn:
        upsert_security(
            conn, exchange="SSE", ticker="600617", name="国新能源",
            security_id="SSE:600617", asset_type="equity", now=100,
        )
        conn.commit()


@pytest.mark.parametrize("title,matched", (
    *((title, False) for title in INDUSTRY_TITLES),
    *((title, True) for title in ISSUER_TITLES + CODE_TITLES),
))
def test_card_lookup_and_persistent_event_writer_share_the_boundary(
    new_energy_master, title, matched,
):
    expected = ["SSE:600617"] if matched else []
    result = database.get_direct_equity_mentions_batch([title])[0]
    assert result["status"] == "ok"
    assert [item["security_id"] for item in result["mentions"]] == expected
    assert result["reason"] == ("matched" if matched else "no_match")
    with database.get_connection() as conn:
        event_id = resolve_or_create_event(
            conn, {"article_url_sha256": title}, title=title, event_type="公司公告",
            direction="中性", priority=8, occurred_at=1_000, available_at=1_000,
            strategy_version="strategy-v1", model_version="test-model", now=1_000,
        )
        mapping_ids = database.upsert_real_event_mappings(
            conn, event_id=event_id, article_text=title, source_text=title,
            source_uri="", direction="中性", mapping_version="mapping-v1", now=1_001,
        )
        assert len(mapping_ids) == int(matched)
        rows = conn.execute(
            "SELECT security_id FROM event_securities "
            "WHERE event_id = ? AND is_current = 1", (event_id,),
        ).fetchall()
        assert [row[0] for row in rows] == expected


@pytest.mark.parametrize("reading", [False, True])
@pytest.mark.parametrize("title,matched", (
    (INDUSTRY_TITLES[0], False),
    (ISSUER_TITLES[5], True),
))
def test_real_lookup_result_reaches_both_card_layouts(new_energy_master, reading, title, matched):
    html = sse_engine._render_cards(attach_title_mentions([_entry(title)]), reading=reading)
    assert title in html
    assert ("标题提及：国新能源（600617·SSE）" in html) is matched


def test_rejected_country_occurrence_does_not_hide_another_company(new_energy_master):
    with database.get_connection() as conn:
        upsert_security(
            conn, exchange="SSE", ticker="600519", name="贵州茅台",
            security_id="SSE:600519", asset_type="equity", now=101,
        )
        conn.commit()
    result = database.get_direct_equity_mentions_batch([
        "中国新能源车销量变化；贵州茅台发布年度业绩",
    ])[0]
    assert [item["security_id"] for item in result["mentions"]] == ["SSE:600519"]
