"""A broker's quoted stock code identifies the speaker, not its investment case."""

import pytest

from db import database
from db.event_core import upsert_security


@pytest.fixture
def security_connection():
    # The shared autouse fixture assigns a fresh test-only database per case.
    database.init_db()
    with database.get_connection() as conn:
        for ticker, name in (
            ("600030", "中信证券"),
            ("601377", "兴业证券"),
            ("601881", "中国银河"),
            ("601689", "拓普集团"),
        ):
            upsert_security(
                conn,
                exchange="SSE",
                ticker=ticker,
                name=name,
                security_id=f"SSE:{ticker}",
                asset_type="equity",
                aliases=[name, ticker],
                now=100,
            )
        conn.commit()
        yield conn


def _mentions(conn, text):
    return [
        (item["security_id"], item["relation_type"], item["confidence"])
        for item in database._direct_equity_mentions(conn, text, limit=10)
    ]


@pytest.mark.parametrize(
    "text",
    [
        "中信证券（600030）：汽车行业账期新规有望改善零部件现金流",
        "中信证券(600030)：汽车行业账期新规有望改善零部件现金流",
        "中信证券(600030.SH)：汽车行业账期新规有望改善零部件现金流",
        "中信证券（SH600030）：汽车行业账期新规有望改善零部件现金流",
        "兴业证券（601377）展望后市：汽车零部件板块有望修复",
        "中国银河证券(601881)：汽车行业账期新规有望改善零部件现金流",
        "中国银河证券股份有限公司（601881）：看好算力产业链",
        "中信证券（600030）分析师张三：看好汽车供应链",
        "中信证券（600030）：看好汽车。中信证券(600030.SH)：看好电子板块",
        "中信证券（ 600030 . SH ）：汽车供应链回款有望改善",
        "中信证券（600030）：看好某公司股价上涨",
        "中信证券（600030）：银行板块股价有望修复",
        "中信证券（600030）：谈论某公司被证监会立案调查",
    ],
)
def test_broker_byline_code_does_not_create_direct_investee(security_connection, text):
    assert _mentions(security_connection, text) == []


@pytest.mark.parametrize(
    ("text", "security_id"),
    [
        ("中信证券（600030）：公司发布半年度业绩公告", "SSE:600030"),
        ("中信证券(600030)：公司拟回购股份", "SSE:600030"),
        ("中信证券(600030.SH)：公司发布半年度业绩公告", "SSE:600030"),
        ("中信证券（SH600030）：公司拟回购股份", "SSE:600030"),
        ("中国银河证券(601881)：公司发布半年度业绩公告", "SSE:601881"),
        ("证券代码600030：公司拟回购股份", "SSE:600030"),
        ("中信证券（600030）：股价上涨5%", "SSE:600030"),
        ("中信证券（600030）：股价下跌5%", "SSE:600030"),
        ("中信证券（600030）：被证监会立案调查", "SSE:600030"),
        ("中信证券（600030）：2026年上半年净利润同比增长", "SSE:600030"),
        ("中信证券（600030）：2026年一季度净利润同比增长", "SSE:600030"),
    ],
)
def test_broker_own_event_keeps_its_exact_code_confidence(
    security_connection, text, security_id
):
    assert _mentions(security_connection, text) == [(security_id, "direct", 0.96)]


@pytest.mark.parametrize(
    "text",
    [
        "中信证券（600030）：汽车行业账期新规有望改善零部件现金流。"
        "拓普集团（601689）公告获得重大订单。",
        "中信证券（600030）：拓普集团（601689）已公告获得重大订单。",
    ],
)
def test_broker_byline_does_not_hide_another_named_company(security_connection, text):
    assert _mentions(security_connection, text) == [("SSE:601689", "direct", 0.96)]


@pytest.mark.parametrize(
    "text",
    [
        "中信证券（600030）：汽车行业账期新规有望改善零部件现金流。"
        "证券代码600030：公司拟回购股份。",
        "中信证券（600030）：公司发布半年度业绩公告。"
        "中信证券（600030）：汽车行业账期新规有望改善零部件现金流。",
    ],
)
def test_separate_own_event_code_survives_same_broker_byline(security_connection, text):
    assert _mentions(security_connection, text) == [("SSE:600030", "direct", 0.96)]


def test_separate_name_only_own_event_does_not_borrow_byline_code_confidence(
    security_connection,
):
    text = (
        "中信证券（600030）：汽车行业账期新规有望改善零部件现金流。"
        "中信证券公告净利润增长。"
    )
    assert _mentions(security_connection, text) == [("SSE:600030", "direct", 0.90)]


def test_broker_byline_cannot_claim_or_remove_another_issuers_code(security_connection):
    assert _mentions(
        security_connection, "中信证券（601689）：汽车供应链回款有望改善"
    ) == []


def test_ticker_attribution_does_not_modify_source_text_or_unrelated_candidates(
    security_connection,
):
    text = "中信证券（600030）：看好汽车产业链。拓普集团（601689）公告获得订单。"
    before = security_connection.total_changes
    resolved = database._resolved_real_event_mapping_candidates(
        security_connection, article_text=text
    )
    assert [row[0] for _item, row in resolved] == ["SSE:601689"]
    assert security_connection.total_changes == before
    assert "中信证券（600030）" in text
