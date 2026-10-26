"""Freeze publisher-title provenance at actual cache/signal write callers."""

from copy import deepcopy

import pytest

from core.asset_mapping import get_mapping_version
from core.source_contract import is_current_article_claim
from db import database
from db.event_core import upsert_security


PUBLISHER_TITLE = "浦发银行（600000）发布年度股东大会通知"
GENERATED_TITLE = "中际旭创获得大额订单，模型认为业绩改善"
GENERATED_SUMMARY = "中际旭创获得大额订单，模型生成的摘要不能成为原文证据"


@pytest.fixture
def initialized_source_database():
    database.init_db()
    with database.get_connection() as conn:
        upsert_security(conn, exchange="SSE", ticker="600000", name="浦发银行", now=100)
        upsert_security(conn, exchange="SZSE", ticker="300308", name="中际旭创", now=100)
        conn.commit()


def _signal():
    return {
        "link": "https://publisher.example/source-callers",
        "event_key": "generated-event-identity",
        "event_title": GENERATED_TITLE,
        "feed_url": "https://publisher.example/rss",
        "available_at": 2000,
        "scored_at": 2000,
        "published_at": 1940,
        "score": 8.0,
        "direction": "中性",
        "strategy_version": "strategy:caller-regression",
        "mapping_version": get_mapping_version(),
        "mapped_theme": "模型生成AI算力",
        "summary": GENERATED_SUMMARY,
        "logic": GENERATED_SUMMARY,
        "score_path": "llm",
        "aggregation_threshold": 7.0,
        "selected": True,
    }


def _write(caller, signal, raw_title):
    if caller == "signal":
        assert database.store_backtest_signal(signal)
        return
    event_id, event_version = database.set_cached_entry(
        signal["link"], title=GENERATED_TITLE, raw_title=raw_title,
        score=signal["score"], tag=signal["mapped_theme"], logic=signal["logic"],
        summary=GENERATED_SUMMARY, feed_url=signal["feed_url"], score_path="llm",
        strategy_version=signal["strategy_version"], direction=signal["direction"],
        created_at=2000, backtest_signal=signal,
    )
    assert event_id and event_version >= 1


def _source_rows():
    with database.get_connection() as conn:
        sources = conn.execute(
            "SELECT claim_key, claim_text, excerpt, snapshot_text FROM event_evidence "
            "WHERE mapping_id IS NULL"
        ).fetchall()
        direct = conn.execute(
            "SELECT security_id FROM event_securities WHERE relation_type='direct'"
        ).fetchall()
        signals = conn.execute("SELECT COUNT(*) FROM backtest_signals").fetchone()[0]
    return sources, direct, signals


@pytest.mark.parametrize("caller", ["signal", "cache_with_signal"])
@pytest.mark.parametrize("missing", [None, "", "   "])
def test_missing_raw_title_at_actual_callers_does_not_mint_source_from_generated_fields(
    initialized_source_database, caller, missing,
):
    signal = _signal()
    if missing is not None:
        signal["raw_title"] = missing
    before = deepcopy(signal)
    _write(caller, signal, missing or "")

    sources, direct, signal_count = _source_rows()
    assert signal == before  # In particular, no caller mutates the supplied provenance.
    assert signal_count == 1  # The news/score can persist while source provenance is absent.
    assert sources == []  # event_title, mapped_theme and display title are not originals.
    assert direct == []  # Generated summary/company names cannot become original mentions.


@pytest.mark.parametrize("caller", ["signal", "cache_with_signal"])
def test_explicit_raw_title_at_actual_callers_mints_only_publisher_title_evidence(
    initialized_source_database, caller,
):
    signal = _signal()
    if caller == "signal":
        signal["raw_title"] = PUBLISHER_TITLE
    # For the cache path the original is supplied through its public argument;
    # stable_signal must inherit precisely that, never the generated display title.
    before = deepcopy(signal)
    _write(caller, signal, PUBLISHER_TITLE)

    sources, direct, signal_count = _source_rows()
    assert signal == before
    assert signal_count == 1
    assert len(sources) == 1  # Cache + signal paths share an idempotent source identity.
    claim_key, claim_text, excerpt, snapshot_text = sources[0]
    assert is_current_article_claim(claim_key)
    assert (claim_text, excerpt, snapshot_text) == (PUBLISHER_TITLE, "", PUBLISHER_TITLE)
    assert len(direct) == 1
    with database.get_connection() as conn:
        ticker = conn.execute(
            "SELECT ticker FROM securities WHERE security_id=?", (direct[0][0],)
        ).fetchone()[0]
    assert ticker == "600000"
