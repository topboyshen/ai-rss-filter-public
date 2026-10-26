"""Adjacent market qualifiers must not silently turn overseas news into A shares."""

import re
import sqlite3

import pytest

import db.database as database
import sse_engine
from services.news_mentions import attach_title_mentions
from test_home_news_reading import _entry


@pytest.fixture
def market_master(monkeypatch):
    monkeypatch.setattr(database, "_EQUITY_NAME_CACHE", ("", (0, 0, 0), 0.0, []))
    monkeypatch.setattr(database, "_EQUITY_MENTION_INDEX_CACHE", (None, None))
    with sqlite3.connect(database.DB_PATH) as conn:
        conn.execute(
            "CREATE TABLE securities (security_id TEXT, name TEXT, ticker TEXT, "
            "exchange TEXT, is_active INTEGER DEFAULT 1, asset_type TEXT DEFAULT 'equity', "
            "updated_at INTEGER DEFAULT 100, version INTEGER DEFAULT 1)"
        )

    def add(name, ticker, exchange):
        with sqlite3.connect(database.DB_PATH) as conn:
            conn.execute(
                "INSERT INTO securities (security_id, name, ticker, exchange) VALUES (?, ?, ?, ?)",
                (f"{exchange}:{ticker}", name, ticker, exchange),
            )

    return add


def _resolve(title):
    return database.get_direct_equity_mentions_batch([title])[0]


@pytest.mark.parametrize("title", [
    "中际旭创港股跌超3%",
    "港股中际旭创跌超3%",
    "中际旭创（港股）跌超3%",
    "中际旭创(港股)跌超3%",
    " 中际旭创 港股 跌超3% ",
    "中际旭创（ 港股 ）跌超3%",
    "中际旭创美股跌超3%",
    "中际旭创（美股）跌超3%",
    "中际旭创沪股跌超3%",
    "中际旭创（沪股）跌超3%",
    "中际旭创京股跌超3%",
    "中际旭创（京股）跌超3%",
])
def test_actual_company_with_conflicting_market_is_not_a_share(market_master, title):
    # The title is a real pre-release counterexample; the master here is isolated.
    market_master("中际旭创", "300308", "SZSE")
    actual = _resolve(title)
    assert actual == {
        "raw_title": title, "status": "ok", "reason": "ambiguous", "mentions": [],
    }


@pytest.mark.parametrize(("ticker", "exchange", "market"), [
    ("688117", "SSE", "深股"),
    ("920001", "BSE", "沪股"),
    ("920001", "BSE", "深股"),
])
@pytest.mark.parametrize("suffix", ["{market}", "（{market}）", "({market})"])
def test_domestic_market_mismatch_is_rejected(market_master, ticker, exchange, market, suffix):
    market_master("样例药业", ticker, exchange)
    actual = _resolve("样例药业" + suffix.format(market=market) + "发布公告")
    assert actual["mentions"] == []
    assert actual["reason"] == "ambiguous"


@pytest.mark.parametrize(("ticker", "exchange", "market"), [
    ("300308", "SZSE", "深股"),
    ("688117", "SSE", "沪股"),
    ("920001", "BSE", "京股"),
])
@pytest.mark.parametrize("template", [
    "{market}样例药业发布公告", "样例药业{market}发布公告",
    "样例药业（{market}）发布公告", "样例药业({market})发布公告",
])
def test_matching_market_keeps_company(market_master, ticker, exchange, market, template):
    market_master("样例药业", ticker, exchange)
    actual = _resolve(template.format(market=market))
    assert actual["reason"] == "matched"
    assert actual["mentions"] == [{
        "security_id": f"{exchange}:{ticker}", "name": "样例药业",
        "ticker": ticker, "exchange": exchange,
    }]


@pytest.mark.parametrize("title", [
    "中际旭创发布业绩，港股市场走低",
    "港股市场走低；中际旭创发布业绩",
    "中际旭创披露业绩；另一家公司港股跌超3%",
])
def test_other_clause_market_is_not_a_global_veto(market_master, title):
    market_master("中际旭创", "300308", "SZSE")
    actual = _resolve(title)
    assert actual["reason"] == "matched"
    assert [mention["ticker"] for mention in actual["mentions"]] == ["300308"]


@pytest.mark.parametrize("title", [
    "中际旭创发布业绩；新易盛港股跌超3%",
    "新易盛（港股）跌超3%；中际旭创发布业绩",
])
def test_conflict_only_rejects_the_qualified_company(market_master, title):
    market_master("中际旭创", "300308", "SZSE")
    market_master("新易盛", "300502", "SZSE")
    actual = _resolve(title)
    assert actual["reason"] == "ambiguous"
    assert [mention["ticker"] for mention in actual["mentions"]] == ["300308"]


@pytest.mark.parametrize("reading", [False, True])
def test_real_resolver_market_rejection_reaches_both_layouts(market_master, reading):
    market_master("中际旭创", "300308", "SZSE")
    title = "中际旭创港股跌超3%"
    prepared = attach_title_mentions([_entry(title)])
    html = sse_engine._render_cards(prepared, reading=reading)

    assert prepared[0]["_title_equity_mentions"]["mentions"] == []
    assert title in html
    assert "标题提及：证券身份有歧义，暂不关联" in html
    assert "300308" not in html
    assert "标题提及：中际旭创" not in html
    assert sse_engine._UNMAPPED_TITLE_NOTE not in html
    assert not re.search(r"<details[^>]*\bopen\b", html)
    if reading:
        assert '<details class="home-news-context"' in html
        assert "方向与入选依据" in html
        assert "看多" in html.split("<details", 1)[1]
    else:
        assert "方向与入选依据" not in html
        assert "看多" in html
