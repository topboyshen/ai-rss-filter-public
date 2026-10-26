"""Forward-only source identities, tested against real isolated SQLite rows."""
import sqlite3

import pytest

from core.source_contract import ARTICLE_CLAIM_PREFIX, MAPPING_CLAIM_PREFIX
from db import database
from db.event_core import (
    init_event_core_schema, resolve_or_create_event, upsert_event_evidence,
    upsert_event_security, upsert_security,
)


@pytest.fixture
def source_conn():
    conn = sqlite3.connect(":memory:")
    init_event_core_schema(conn)
    yield conn
    conn.close()


def _event(conn):
    return resolve_or_create_event(conn, {"wire": "source-contract"},
                                   title="浦发银行发布公告", now=100)


def _article(conn, event_id, title="浦发银行发布公告"):
    database._record_article_evidence(
        conn, event_id=event_id, link="https://example.test/news",
        raw_title=title, summary="模型摘要中的中际旭创获得订单",
        feed_url="https://example.test/rss", announcement_at=100, observed_at=200,
    )


def test_same_url_title_does_not_relabel_or_overwrite_old_evidence(source_conn):
    conn = source_conn
    event = _event(conn)
    old_id = upsert_event_evidence(
        conn, event, source_uri="https://example.test/news",
        claim_key="article_snapshot:oldhash", claim_text="浦发银行发布公告",
        excerpt="旧模型摘要", snapshot_text="浦发银行发布公告\n旧模型摘要",
        observed_at=150, now=150,
    )
    before = conn.execute("SELECT * FROM event_evidence WHERE evidence_id=?", (old_id,)).fetchone()
    _article(conn, event)
    _article(conn, event)  # idempotent natural duplicate
    assert conn.execute("SELECT * FROM event_evidence WHERE evidence_id=?", (old_id,)).fetchone() == before
    rows = conn.execute("SELECT claim_key, claim_text, excerpt, snapshot_text FROM event_evidence").fetchall()
    assert len(rows) == 2
    new = next(row for row in rows if row[0].startswith(ARTICLE_CLAIM_PREFIX))
    assert new[1:] == ("浦发银行发布公告", "", "浦发银行发布公告")


@pytest.mark.parametrize("title", ["", "   "])
def test_missing_title_never_mints_new_verified_source_identity(source_conn, title):
    _article(source_conn, _event(source_conn), title)
    assert source_conn.execute("SELECT COUNT(*) FROM event_evidence").fetchone()[0] == 0


def test_forward_mapping_binds_new_source_even_if_old_source_is_later(source_conn):
    conn = source_conn
    event = _event(conn)
    security = upsert_security(conn, exchange="SSE", ticker="600000", name="浦发银行", now=100)
    _article(conn, event)
    upsert_event_evidence(
        conn, event, source_uri="https://example.test/news", claim_key="legacy-later",
        claim_text="旧模型写的公司", excerpt="旧生成摘要", observed_at=300, now=300,
    )
    mapping = upsert_event_security(
        conn, event, security, mapping_version="asset-map:title-only-v2:fixture", now=400,
    )
    bound = database._bind_mapping_evidence(
        conn, event_id=event, mapping_id=mapping, security_id=security,
        source_uri="https://example.test/news",
    )
    row = conn.execute("SELECT claim_key, claim_text, excerpt, snapshot_text FROM event_evidence WHERE evidence_id=?",
                       (bound,)).fetchone()
    assert row[0].startswith(MAPPING_CLAIM_PREFIX)
    assert row[1:] == ("浦发银行发布公告", "", "浦发银行发布公告")


def test_forward_retirement_leaves_legacy_payload_and_current_pointer_alone(source_conn):
    conn = source_conn
    event = _event(conn)
    old_security = upsert_security(conn, exchange="SSE", ticker="600000", name="浦发银行", now=100)
    new_security = upsert_security(conn, exchange="SSE", ticker="600030", name="中信证券", now=100)
    old_mapping = upsert_event_security(conn, event, old_security, mapping_version="asset-map:old", now=100)
    new_mapping = upsert_event_security(conn, event, new_security,
                                      mapping_version="asset-map:title-only-v2:fixture", now=200)
    before = conn.execute("SELECT * FROM event_securities WHERE mapping_id=?", (old_mapping,)).fetchone()
    assert database.retire_stale_rule_event_mappings(
        conn, event_id=event, keep_mapping_ids=[],
        current_mapping_version="asset-map:title-only-v2:fixture",
    ) == 1
    assert conn.execute("SELECT * FROM event_securities WHERE mapping_id=?", (old_mapping,)).fetchone() == before
    assert conn.execute("SELECT is_current FROM event_securities WHERE mapping_id=?", (new_mapping,)).fetchone()[0] == 0


@pytest.mark.parametrize("key", [ARTICLE_CLAIM_PREFIX + "bad", ARTICLE_CLAIM_PREFIX + "z" * 64])
def test_forward_binding_rejects_malformed_source_identity(source_conn, key):
    conn = source_conn
    event = _event(conn)
    security = upsert_security(conn, exchange="SSE", ticker="600000", name="浦发银行", now=100)
    upsert_event_evidence(conn, event, source_uri="https://example.test/news",
                          claim_key=key, claim_text="旧摘要冒充原文", now=100)
    mapping = upsert_event_security(conn, event, security,
                                   mapping_version="asset-map:title-only-v2:fixture", now=200)
    assert database._bind_mapping_evidence(conn, event_id=event, mapping_id=mapping,
                                           security_id=security,
                                           source_uri="https://example.test/news") is None
