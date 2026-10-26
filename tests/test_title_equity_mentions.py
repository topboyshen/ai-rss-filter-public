"""Card-only title identities use an isolated, read-only master snapshot."""

import sqlite3
from contextlib import contextmanager

import pytest

from db import database


@pytest.fixture
def master(monkeypatch):
    monkeypatch.setattr(database, "_EQUITY_NAME_CACHE", ("", (0, 0, 0), 0.0, []))
    monkeypatch.setattr(database, "_EQUITY_MENTION_INDEX_CACHE", (None, None))
    with sqlite3.connect(database.DB_PATH) as conn:
        conn.execute(
            "CREATE TABLE securities (security_id TEXT, name TEXT, ticker TEXT, "
            "exchange TEXT, is_active INTEGER DEFAULT 1, asset_type TEXT DEFAULT 'equity', "
            "updated_at INTEGER DEFAULT 100, version INTEGER DEFAULT 1)"
        )

    def add(name, ticker, exchange="SSE", security_id=None):
        with sqlite3.connect(database.DB_PATH) as conn:
            conn.execute(
                "INSERT INTO securities (security_id, name, ticker, exchange) VALUES (?, ?, ?, ?)",
                (security_id or f"{exchange}:{ticker}", name, ticker, exchange),
            )
    return add


def result(title, *, limit=5):
    return database.get_direct_equity_mentions_batch([title], limit=limit)[0]


def test_unmapped_theme_title_keeps_literal_company_identity(master):
    # This is a test-only master fixture, not a claim about production coverage.
    master("圣诺生物", "688117")
    title = " 圣诺生物：子公司被FDA纳入名单，未来可能影响经营业绩 "
    actual = result(title)
    assert actual == {
        "raw_title": title, "status": "ok", "reason": "matched",
        "mentions": [{"security_id": "SSE:688117", "name": "圣诺生物",
                      "ticker": "688117", "exchange": "SSE"}],
    }


@pytest.mark.parametrize("title", [
    "圣诺生物公告新产品获批", "圣诺生物（688117）公告新产品获批",
    "证券代码688117：公司公告新产品获批", "股票代码：688117，公司公告",
    "SH688117公告", "688117.SH公告", "SSE:688117公告", "沪688117公告",
])
def test_explicit_company_or_anchored_code_is_allowed(master, title):
    master("圣诺生物", "688117")
    assert [item["ticker"] for item in result(title)["mentions"]] == ["688117"]


@pytest.mark.parametrize("title", [
    "购买设备金额688117元", "统计人数688117人", "记录日期240921",
    "预计销售688117台", "2026688117年", "688117", "普通订单编号688117",
])
def test_six_digits_without_security_context_do_not_identify_company(master, title):
    master("圣诺生物", "688117")
    assert result(title) == {
        "raw_title": title, "status": "ok", "reason": "no_match", "mentions": [],
    }


@pytest.mark.parametrize("title", [
    "SZ688117公告", "688117.SZ公告", "HK688117公告", "688117.HK公告",
    "SZ688117.SH公告", "圣诺生物（688117.SZ）公告", "圣诺生物（600030）公告",
    "港股圣诺生物公告", "圣诺生物（HK）公告",
    "圣诺生物（688117.US）公告", "圣诺生物（688117.NYSE）公告",
    "圣诺生物（US688117）公告", "圣诺生物（NASDAQ:688117）公告", "圣诺生物（688117.XX）公告",
])
def test_explicit_market_or_issuer_conflict_is_rejected(master, title):
    master("圣诺生物", "688117")
    assert result(title)["mentions"] == []
    assert result(title)["reason"] == "ambiguous"


@pytest.mark.parametrize(("title", "name", "ticker", "exchange"), [
    ("人工智能是发展动力源", "动力源", "600405", "SSE"),
    ("机器人行业需求上升", "机器人", "300024", "SZSE"),
    ("全国农产品价格回落", "农产品", "000061", "SZSE"),
    ("中信证券：看好电子板块", "中信证券", "600030", "SSE"),
    ("中信证券（600030.SH）：看好电子板块", "中信证券", "600030", "SSE"),
    ("中信证券（证券代码600030）：看好电子板块", "中信证券", "600030", "SSE"),
    ("中国银河证券（601881）：看好电子板块", "中国银河", "601881", "SSE"),
    ("上海钢联发布数据显示，碳酸锂价格上涨", "上海钢联", "300226", "SZSE"),
])
def test_existing_common_word_and_provider_filters_are_reused(master, title, name, ticker, exchange):
    master(name, ticker, exchange)
    assert result(title)["mentions"] == []


@pytest.mark.parametrize("title", [
    "中信证券发布半年度业绩公告", "中信证券（600030）：公司拟回购股份",
    "证券代码600030：公司拟回购股份",
])
def test_broker_own_company_event_is_not_globally_excluded(master, title):
    master("中信证券", "600030")
    assert [item["name"] for item in result(title)["mentions"]] == ["中信证券"]


@pytest.mark.parametrize(("ticker", "exchange"), [
    ("688117", "HKEX"), ("688117", "SZSE"), ("100001", "SSE"),
    ("00001", "HKEX"), ("92001", "BSE"), ("1234567", "SSE"),
])
def test_unsupported_market_and_invalid_a_share_identity_rejected(master, ticker, exchange):
    master("目标公司", ticker, exchange)
    assert result("目标公司公告新产品获批")["mentions"] == []


@pytest.mark.parametrize(("ticker", "exchange"), [
    ("688117", "SSE"), ("300024", "SZSE"), ("838275", "BSE"), ("920000", "BSE"),
])
def test_valid_master_a_share_markets(master, ticker, exchange):
    master("目标公司", ticker, exchange)
    assert result("目标公司公告新产品获批")["mentions"][0]["exchange"] == exchange


def test_name_ambiguity_rejected_before_limit_even_with_explicit_code(master):
    master("同名公司", "600101")
    master("同名公司", "600102")
    for title in ("同名公司公告", "同名公司（600101）公告", "股票代码600101公告"):
        assert result(title, limit=1)["mentions"] == []
        assert result(title, limit=1)["reason"] == "ambiguous"


def test_code_ambiguity_checks_complete_master_before_market_filter(master):
    master("甲公司", "600101")
    master("乙公司", "600101", "SZSE")
    for title in ("证券代码600101公告", "甲公司公告"):
        assert result(title, limit=1)["mentions"] == []
        assert result(title, limit=1)["reason"] == "ambiguous"


@pytest.mark.parametrize("code", [
    "000001", "000001.SZ", "SZ000001", "SZ:000001", "证券代码000001", "股票代码：000001.SZ",
])
def test_conflicting_name_code_pair_rejects_both_issuers_not_other_mentions(master, code):
    master("示例药业", "688001")
    master("平安银行", "000001", "SZSE")
    master("正常公司", "600001")
    actual = result(f"示例药业（{code}）发布公告；正常公司公告业绩增长")
    assert [item["name"] for item in actual["mentions"]] == ["正常公司"]
    assert actual["reason"] == "ambiguous"


def test_name_followed_by_conflicting_code_label_rejects_both_issuers(master):
    master("示例药业", "688001")
    master("平安银行", "000001", "SZSE")
    assert result("示例药业：股票代码000001.SZ，发布公告")["mentions"] == []


def test_same_name_across_markets_is_not_resolved_by_filtering_one_market(master):
    master("示例药业", "688001")
    master("示例药业", "00001", "HKEX")
    assert result("示例药业公告业绩增长", limit=1)["mentions"] == []
    assert result("示例药业公告业绩增长", limit=1)["reason"] == "ambiguous"


def test_output_limit_does_not_hide_later_ambiguity(master):
    master("正常公司", "600001")
    master("同名公司", "600002")
    master("同名公司", "600003")
    actual = result("正常公司和同名公司公告", limit=1)
    assert [item["name"] for item in actual["mentions"]] == ["正常公司"]
    assert actual["reason"] == "ambiguous"


def test_input_order_and_exact_title_binding_with_duplicate_titles(master):
    master("圣诺生物", "688117")
    titles = [" 圣诺生物公告 ", "无公司标题", "", " 圣诺生物公告 "]
    results = database.get_direct_equity_mentions_batch(titles)
    assert [item["raw_title"] for item in results] == titles
    assert [item["reason"] for item in results] == ["matched", "no_match", "missing_title", "matched"]


def test_missing_titles_do_not_open_database(monkeypatch):
    def forbidden():
        raise AssertionError("an empty title must not trigger I/O")
    monkeypatch.setattr(database, "get_read_only_connection", forbidden)
    assert database.get_direct_equity_mentions_batch([]) == []
    assert [item["reason"] for item in database.get_direct_equity_mentions_batch(["", "  "])] == [
        "missing_title", "missing_title",
    ]


def test_empty_master_is_unavailable_not_successful_no_match(master):
    assert result("圣诺生物公告")["status"] == "unavailable"
    assert result("圣诺生物公告")["reason"] == "master_empty"


def test_missing_database_is_unavailable_without_creating_a_database():
    from pathlib import Path
    assert not Path(database.DB_PATH).exists()
    actual = database.get_direct_equity_mentions_batch(["圣诺生物公告", ""])
    assert [item["reason"] for item in actual] == ["master_unavailable", "missing_title"]
    assert not Path(database.DB_PATH).exists()


def test_master_read_error_is_unavailable(master, monkeypatch):
    def broken(_conn):
        raise sqlite3.OperationalError("test-only query failure")
    monkeypatch.setattr(database, "_equity_mention_index", broken)
    assert result("圣诺生物公告")["reason"] == "master_unavailable"


@pytest.mark.parametrize("count", [1, 20, 100])
def test_one_read_only_connection_and_index_preparation_per_batch(master, monkeypatch, count):
    master("圣诺生物", "688117")
    calls = {"connection": 0, "index": 0, "rows": 0}
    original_connection = database.get_read_only_connection
    original_index = database._equity_mention_index
    original_rows = database._equity_name_rows

    @contextmanager
    def counted_connection():
        calls["connection"] += 1
        with original_connection() as conn:
            with pytest.raises(sqlite3.OperationalError):
                conn.execute("DELETE FROM securities")
            yield conn

    def counted_index(conn):
        calls["index"] += 1
        return original_index(conn)

    def counted_rows(conn):
        calls["rows"] += 1
        return original_rows(conn)

    monkeypatch.setattr(database, "get_read_only_connection", counted_connection)
    monkeypatch.setattr(database, "_equity_mention_index", counted_index)
    monkeypatch.setattr(database, "_equity_name_rows", counted_rows)
    assert len(database.get_direct_equity_mentions_batch(["圣诺生物公告"] * count)) == count
    assert calls == {"connection": 1, "index": 1, "rows": 1}
