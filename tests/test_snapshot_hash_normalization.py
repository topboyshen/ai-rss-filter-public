"""Article snapshots must hash the same canonical text that is verified on write.

Regression for the 2026-10-13 webhook dead letter: a headline containing the CJK
compatibility ideograph U+F90A was hashed raw, while ``upsert_event_evidence``
normalises the text (NFC + strip) before re-hashing, so every retry raised
``snapshot_hash does not match snapshot_text``.  Real isolated SQLite rows.
"""
import hashlib
import sqlite3
import unicodedata

import pytest

from core.source_contract import ARTICLE_CLAIM_PREFIX, MAPPING_CLAIM_PREFIX
from db import database
from db.event_core import (
    init_event_core_schema, normalize_evidence_text, resolve_or_create_event,
    snapshot_digest, upsert_event_evidence, upsert_event_security, upsert_security,
)

COMPAT_JIN = "\uf90a"  # CJK COMPATIBILITY IDEOGRAPH-F90A, NFC -> U+91D1 (金)
LINK = "https://example.test/live/1"


@pytest.fixture
def conn():
    c = sqlite3.connect(":memory:")
    init_event_core_schema(c)
    yield c
    c.close()


def _event(c):
    return resolve_or_create_event(c, {"wire": "nfc"}, title="华润置地销售", now=100)


def _article(c, event_id, title):
    database._record_article_evidence(
        c, event_id=event_id, link=LINK, raw_title=title, summary="模型摘要",
        feed_url="https://example.test/rss", announcement_at=100, observed_at=200,
    )


def _sha(text):
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _rows(c):
    return c.execute(
        "SELECT claim_key, claim_text, snapshot_text, snapshot_hash FROM event_evidence"
    ).fetchall()


@pytest.mark.parametrize("title", [
    f"华润置地：9月总合同销售{COMPAT_JIN}额约155.0亿元，同比减少11.9%",
    "Cafe\u0301 并购公告",  # combining acute accent composes under NFC
    f"  {COMPAT_JIN}融街控股公告  ",
])
def test_non_nfc_headline_is_stored_with_digest_of_normalised_text(conn, title):
    expected = unicodedata.normalize("NFC", title).strip()
    assert expected != title  # the fixture really is not canonical
    _article(conn, _event(conn), title)
    rows = _rows(conn)
    assert len(rows) == 1
    claim_key, claim_text, snapshot_text, snapshot_hash = rows[0]
    assert snapshot_text == expected
    assert claim_text == expected
    assert snapshot_hash == _sha(expected) == _sha(snapshot_text)
    assert claim_key == f"{ARTICLE_CLAIM_PREFIX}{_sha(expected)}"


def test_non_nfc_headline_is_idempotent_on_retry(conn):
    event = _event(conn)
    title = f"销售{COMPAT_JIN}额公告"
    _article(conn, event, title)
    _article(conn, event, title)  # a webhook retry must not fail or duplicate
    assert len(_rows(conn)) == 1


def test_raw_and_canonical_spelling_share_one_source_identity(conn):
    event = _event(conn)
    _article(conn, event, f"销售{COMPAT_JIN}额公告")
    _article(conn, event, "销售金额公告")
    assert len(_rows(conn)) == 1


def test_whitespace_at_title_cut_does_not_break_the_digest(conn):
    title = "甲" * 3999 + " " + "乙" * 10  # the 4000-char cut ends in a space
    _article(conn, _event(conn), title)
    (_, _, snapshot_text, snapshot_hash), = _rows(conn)
    assert snapshot_text == "甲" * 3999
    assert snapshot_hash == _sha(snapshot_text)


def test_mapping_evidence_copies_a_normalised_article_snapshot(conn):
    event = _event(conn)
    security = upsert_security(conn, exchange="SSE", ticker="600000", name="浦发银行", now=100)
    _article(conn, event, f"浦发银行{COMPAT_JIN}融公告")
    mapping = upsert_event_security(
        conn, event, security, mapping_version="asset-map:title-only-v2:fixture", now=400,
    )
    bound = database._bind_mapping_evidence(
        conn, event_id=event, mapping_id=mapping, security_id=security, source_uri=LINK,
    )
    row = conn.execute(
        "SELECT claim_key, snapshot_text, snapshot_hash FROM event_evidence WHERE evidence_id=?",
        (bound,),
    ).fetchone()
    assert row[0].startswith(MAPPING_CLAIM_PREFIX)
    assert row[1] == "浦发银行金融公告"
    assert row[2] == _sha(row[1])


def test_shared_digest_is_what_the_writer_verifies(conn):
    raw = f" 销售{COMPAT_JIN}额 "
    assert normalize_evidence_text(raw) == "销售金额"
    assert snapshot_digest(raw) == _sha("销售金额")
    # The verifier accepts the shared digest for the raw spelling ...
    upsert_event_evidence(
        conn, _event(conn), source_uri=LINK, claim_key="k1", claim_text="销售金额",
        snapshot_hash=snapshot_digest(raw), snapshot_text=raw, observed_at=1, now=1,
    )
    # ... and still rejects a digest of the raw, unnormalised text.
    with pytest.raises(database.EventCoreConflictError):
        upsert_event_evidence(
            conn, _event(conn), source_uri=LINK + "x", claim_key="k2", claim_text="销售金额",
            snapshot_hash=_sha(raw.strip()), snapshot_text=raw, observed_at=1, now=1,
        )
