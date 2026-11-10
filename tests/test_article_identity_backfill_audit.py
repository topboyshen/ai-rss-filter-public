"""Dry-run audit for article identity rows that need a one-off digest backfill.

The audit must open the database read-only, classify every stored
``article_url_sha256`` identity into current / backfillable / conflict /
no-source, and never mutate a row.
"""

from __future__ import annotations

import json
import sqlite3

import pytest

from scripts import audit_article_identity_backfill as audit
from db.database import _normalized_article_identity


def _new_digest(link: str) -> str:
    return _normalized_article_identity(link)[1]


def _make_db(path, events, identities, evidence, cache):
    conn = sqlite3.connect(path)
    conn.execute("CREATE TABLE events (event_id TEXT PRIMARY KEY, merged_into TEXT)")
    conn.execute(
        "CREATE TABLE event_identities (identity_type TEXT, identity_key TEXT, "
        "event_id TEXT)"
    )
    conn.execute("CREATE TABLE event_evidence (event_id TEXT, source_uri TEXT)")
    conn.execute("CREATE TABLE news_cache (event_id TEXT, link TEXT)")
    conn.executemany("INSERT INTO events VALUES (?, ?)", events)
    conn.executemany(
        "INSERT INTO event_identities VALUES ('article_url_sha256', ?, ?)", identities
    )
    conn.executemany("INSERT INTO event_evidence VALUES (?, ?)", evidence)
    conn.executemany("INSERT INTO news_cache VALUES (?, ?)", cache)
    conn.commit()
    conn.close()


@pytest.fixture
def database(tmp_path):
    path = tmp_path / "cache.db"
    current = "https://example.com/news/story-1"
    merged = "https://example.com/news/story-9"
    backfill = "https://example.com/a/?utm_source=x"
    conflict = "https://example.com/b/"
    cached = "https://example.com/d/"
    no_source = "https://example.com/c/"
    _make_db(
        path,
        events=[
            ("e-current", None),
            ("e-backfill", None),
            ("e-conflict", None),
            ("e-other", None),
            ("e-nosource", None),
            ("e-cache", None),
            ("e-merged", "e-current"),
        ],
        identities=[
            (_new_digest(current), "e-current"),
            (_new_digest(merged), "e-merged"),
            (audit.legacy_identity(backfill), "e-backfill"),
            (audit.legacy_identity(conflict), "e-conflict"),
            (_new_digest(conflict), "e-other"),
            (audit.legacy_identity(no_source), "e-nosource"),
            (audit.legacy_identity(cached), "e-cache"),
        ],
        evidence=[
            ("e-current", current),
            ("e-merged", merged),
            ("e-backfill", backfill),
            ("e-conflict", conflict),
            ("e-other", "https://example.com/b"),
        ],
        cache=[("e-cache", cached)],
    )
    return path


def _scan(path):
    conn = audit.open_readonly(path)
    try:
        return audit.scan(conn)
    finally:
        conn.close()


def test_classifies_four_outcomes(database):
    result = _scan(database)

    assert result["counts"] == {
        "identity_rows": 7,
        "current": 3,
        "backfillable": 2,
        "conflict": 1,
        "no_source": 1,
    }


def test_samples_never_contain_source_urls(database):
    result = _scan(database)

    assert "http" not in json.dumps(result)


def test_read_only_connection_rejects_writes(database):
    conn = audit.open_readonly(database)
    try:
        with pytest.raises(sqlite3.OperationalError):
            conn.execute("DELETE FROM events")
    finally:
        conn.close()


def test_database_arg_is_required():
    with pytest.raises(SystemExit) as excinfo:
        audit.main([])

    assert excinfo.value.code == 2


def test_missing_tables_fail_closed(tmp_path, capsys):
    path = tmp_path / "partial.db"
    conn = sqlite3.connect(path)
    conn.execute("CREATE TABLE events (event_id TEXT PRIMARY KEY, merged_into TEXT)")
    conn.commit()
    conn.close()

    assert audit.main(["--db", str(path)]) == 2
    assert "missing tables" in capsys.readouterr().err


def test_json_report_is_machine_readable(database, capsys):
    assert audit.main(["--db", str(database), "--json"]) == 0

    payload = json.loads(capsys.readouterr().out)
    assert payload["counts"]["backfillable"] == 2
    assert set(payload["counts"]) >= {
        "identity_rows",
        "current",
        "backfillable",
        "conflict",
        "no_source",
    }


def test_legacy_formula_is_frozen():
    # Pre-2026-11-08 digest for a link that the current code normalises away.
    assert audit.legacy_identity("https://example.com/A/?utm_source=x") == (
        "523c092613245fec6bc11840389b0a296ba93f1eb47947495390d92df34e4f68"
    )
