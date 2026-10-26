"""Explicit adjacent Hong Kong codes must not resolve to an A-share namesake."""

import re
import sqlite3

import pytest

import db.database as database
import sse_engine
from services.news_mentions import attach_title_mentions


@pytest.fixture
def hk_code_master(monkeypatch):
    monkeypatch.setattr(database, "_EQUITY_NAME_CACHE", ("", (0, 0, 0), 0.0, []))
    monkeypatch.setattr(database, "_EQUITY_MENTION_INDEX_CACHE", (None, None))
    with sqlite3.connect(database.DB_PATH) as conn:
        conn.execute(
            "CREATE TABLE securities (security_id TEXT, name TEXT, ticker TEXT, "
            "exchange TEXT, is_active INTEGER DEFAULT 1, asset_type TEXT DEFAULT 'equity', "
            "updated_at INTEGER DEFAULT 100, version INTEGER DEFAULT 1)"
        )

    def add(name="工商银行", ticker="601398", exchange="SSE"):
        with sqlite3.connect(database.DB_PATH) as conn:
            conn.execute(
                "INSERT INTO securities (security_id, name, ticker, exchange) VALUES (?, ?, ?, ?)",
                (f"{exchange}:{ticker}", name, ticker, exchange),
            )

    return add


def _resolve(title):
    return database.get_direct_equity_mentions_batch([title])[0]


def _icbc_mention():
    return {
        "security_id": "SSE:601398", "name": "工商银行",
        "ticker": "601398", "exchange": "SSE",
    }


@pytest.mark.parametrize("code", [
    "01398.HK", "HK01398", "HKEX:01398", "01398.HKEX", "港01398", "01398港",
    "1398.HK", "HK1398", "HK:1398", "HKEX:1398", "1398.HKEX",
])
@pytest.mark.parametrize("brackets", [("（", "）"), ("(", ")")])
def test_explicit_hk_code_rejects_a_share_namesake(hk_code_master, code, brackets):
    hk_code_master()
    title = f"工商银行{brackets[0]}{code}{brackets[1]}跌超3%"

    assert _resolve(title) == {
        "raw_title": title, "status": "ok", "reason": "ambiguous", "mentions": [],
    }


@pytest.mark.parametrize("title", [
    "工商银行（股票代码：01398.HK）发布业绩",
    "工商银行(证券代码:HK01398)发布业绩",
    "工商银行，股票代码：01398.HK，发布业绩",
    "工商银行:证券代码=HKEX01398，发布业绩",
    "工商银行证券代码：港01398发布业绩",
    "工商银行（ 01398 . hK ）发布业绩",
    " 工商银行 ( hK 01398 ) 发布业绩 ",
    "工商银行（ hKeX ： 01398 ）发布业绩",
    "工商银行（ 01398 ： 港 ）发布业绩",
    "工商银行（股票代码：1398.HK）发布业绩",
    "工商银行(证券代码:HK1398)发布业绩",
    "工商银行，股票代码：1398.HK，发布业绩",
    "工商银行:证券代码=HKEX1398，发布业绩",
    "工商银行（ 1398 . hK ）发布业绩",
    " 工商银行 ( hK ： 1398 ) 发布业绩 ",
    "工商银行（ hKeX ： 1398 ）发布业绩",
])
def test_hk_code_labels_whitespace_and_case(hk_code_master, title):
    hk_code_master()
    assert _resolve(title) == {
        "raw_title": title, "status": "ok", "reason": "ambiguous", "mentions": [],
    }


@pytest.mark.parametrize("title", [
    "工商银行证券代码：01398港",
    "工商银行证券代码：01398港，发布业绩",
    "工商银行证券代码：01398港；发布业绩",
])
def test_chinese_hk_suffix_with_clear_boundary(hk_code_master, title):
    hk_code_master()
    assert _resolve(title) == {
        "raw_title": title, "status": "ok", "reason": "ambiguous", "mentions": [],
    }


@pytest.mark.parametrize("title", [
    "工商银行（601398.SH）发布业绩",
    "工商银行(SH601398)发布业绩",
    "工商银行（股票代码：601398.SH）发布业绩",
    "工商银行证券代码：601398.SH发布业绩",
])
def test_matching_a_share_code_keeps_existing_identity(hk_code_master, title):
    hk_code_master()
    assert _resolve(title) == {
        "raw_title": title, "status": "ok", "reason": "matched",
        "mentions": [_icbc_mention()],
    }


@pytest.mark.parametrize("title", [
    "工商银行发布业绩；另一家公司（01398.HK）跌超3%",
    "另一家公司（HK01398）跌超3%；工商银行发布业绩",
    "工商银行发布业绩，恒生银行（00011.HK）跌超3%",
    "工商银行发布业绩；港股代码01398.HK成交活跃",
    "工商银行发布业绩；另一家公司（1398.HK）跌超3%",
    "另一家公司（HK:1398）跌超3%；工商银行发布业绩",
])
def test_other_issuer_or_clause_hk_code_is_not_a_global_veto(hk_code_master, title):
    hk_code_master()
    assert _resolve(title) == {
        "raw_title": title, "status": "ok", "reason": "matched",
        "mentions": [_icbc_mention()],
    }


@pytest.mark.parametrize("title", [
    "工商银行（01398.HK）跌超3%；中际旭创发布业绩",
    "中际旭创发布业绩；工商银行（HK01398）跌超3%",
    "工商银行（1398.HK）跌超3%；中际旭创发布业绩",
    "中际旭创发布业绩；工商银行（HKEX:1398）跌超3%",
])
def test_hk_conflict_only_rejects_the_qualified_issuer(hk_code_master, title):
    hk_code_master()
    hk_code_master("中际旭创", "300308", "SZSE")
    assert _resolve(title) == {
        "raw_title": title, "status": "ok", "reason": "ambiguous",
        "mentions": [{
            "security_id": "SZSE:300308", "name": "中际旭创",
            "ticker": "300308", "exchange": "SZSE",
        }],
    }


@pytest.mark.parametrize("title", [
    "工商银行（01398）发布业绩",
    "工商银行新增12345个服务网点",
    "工商银行（订单01398）完成处理",
    "工商银行（HK01398A）项目启动",
    "工商银行（01398.HKEXTRA）项目启动",
    "工商银行（HK0139800）项目启动",
    "工商银行（10000港元起存）推出存款产品",
    "工商银行（50000港币优惠）推出新产品",
    "工商银行（1398）发布业绩",
    "工商银行（1398港元起存）推出存款产品",
    "工商银行（1398港币优惠）推出新产品",
    "工商银行（HK1398A）项目启动",
    "工商银行（1398.HKEXTRA）项目启动",
    "工商银行（HK1398000）项目启动",
])
def test_nonsecurity_numbers_and_longer_tokens_do_not_become_hk_codes(hk_code_master, title):
    hk_code_master()
    assert _resolve(title) == {
        "raw_title": title, "status": "ok", "reason": "matched",
        "mentions": [_icbc_mention()],
    }


@pytest.mark.parametrize("title", [
    "工商银行H股跌超3%",
    "工商银行（H股）跌超3%",
    "工商银行(H股)发布业绩",
    "工商银行 H股 发布业绩",
    "H股工商银行跌超3%",
    "工商银行H股（01398.HK）跌超3%",
])
def test_adjacent_h_share_qualifier_rejects_a_share_namesake(hk_code_master, title):
    hk_code_master()
    assert _resolve(title) == {
        "raw_title": title, "status": "ok", "reason": "ambiguous", "mentions": [],
    }


@pytest.mark.parametrize("title", [
    "工商银行发布业绩；另一家公司H股跌超3%",
    "工商银行：关于刊发H股招股说明书的公告",
])
def test_non_adjacent_h_share_word_does_not_veto_a_share_name(hk_code_master, title):
    hk_code_master()
    assert _resolve(title) == {
        "raw_title": title, "status": "ok", "reason": "matched",
        "mentions": [_icbc_mention()],
    }


@pytest.mark.parametrize("reading", [False, True])
def test_h_share_qualifier_reaches_both_card_layouts(hk_code_master, reading):
    hk_code_master()
    title = "工商银行（H股）跌超3%"
    entry = {
        "title": title, "raw_title": title, "score": 8.8,
        "importance_score": 8.8, "direction": "看多",
        "link": "https://news.example/h-share", "source": "测试来源",
        "feed_url": "https://feed.example/a", "time": 2_000_000_000 - 60,
        "_selection_threshold": 7.2, "_realtime_section": "fresh_strong",
    }
    prepared = attach_title_mentions([entry])
    html = sse_engine._render_cards(prepared, reading=reading)

    assert prepared[0]["_title_equity_mentions"]["reason"] == "ambiguous"
    assert "601398" not in html
    assert "标题提及：证券身份有歧义，暂不关联" in html
    assert "标题提及：工商银行" not in html


@pytest.mark.parametrize("qualifier", ["", "（01398.HK）"])
def test_same_name_in_multiple_markets_remains_ambiguous(hk_code_master, qualifier):
    hk_code_master()
    hk_code_master("工商银行", "01398", "HKEX")
    title = f"工商银行{qualifier}发布业绩"
    assert _resolve(title) == {
        "raw_title": title, "status": "ok", "reason": "ambiguous", "mentions": [],
    }


@pytest.mark.parametrize("reading", [False, True])
@pytest.mark.parametrize("code", ["01398.HK", "HK01398", "1398.HK", "HK:1398"])
def test_hk_code_rejection_reaches_both_card_layouts(hk_code_master, reading, code):
    hk_code_master()
    title = f"工商银行（{code}）跌超3%"
    entry = {
        "title": title, "raw_title": title, "score": 8.8,
        "importance_score": 8.8, "direction": "看多",
        "link": "https://news.example/hk-code", "source": "测试来源",
        "feed_url": "https://feed.example/a", "time": 2_000_000_000 - 60,
        "_selection_threshold": 7.2, "_realtime_section": "fresh_strong",
    }
    prepared = attach_title_mentions([entry])
    html = sse_engine._render_cards(prepared, reading=reading)

    assert prepared[0]["_title_equity_mentions"]["reason"] == "ambiguous"
    assert prepared[0]["_title_equity_mentions"]["mentions"] == []
    assert title in html
    assert "标题提及：证券身份有歧义，暂不关联" in html
    assert "601398" not in html
    assert "标题提及：工商银行" not in html
    assert sse_engine._UNMAPPED_TITLE_NOTE not in html
    assert not re.search(r"<details[^>]*\bopen\b", html)
    if reading:
        assert '<details class="home-news-context"' in html
        assert "方向与入选依据" in html
        assert "看多" in html.split("<details", 1)[1]
    else:
        assert "方向与入选依据" not in html
        assert "看多" in html
