"""Fixed SQLite startup changes; the production runner owns every transaction.

This deliberately does not call init_db, import historical exports or trim the
retained outbox. Historical export backfill must already be recorded, or the
legacy ledger must be empty. Unsupported data transitions fail before writes.
"""
from __future__ import annotations

import hashlib
import json

from db import database as db
from db import event_migration, integration_outbox

STEP_IDS = ("base-tables", *(f"news-schema-{n}" for n in range(1, 8)),
            "event-core-backfill", "event-runtime", "market-ledgers",
            "operational-tables", "feed-health", "final-verification")
MAX_ROWS = 100_000


def require(value, message):
    if not value:
        raise RuntimeError(message)


def table_exists(conn, name):
    return conn.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (name,)).fetchone() is not None


def retained_outbox(conn):
    digest = hashlib.sha256()
    count = 0
    if table_exists(conn, "integration_signal_outbox"):
        for row in conn.execute("SELECT seq,ledger_signal_id,event_id FROM integration_signal_outbox ORDER BY seq"):
            count += 1
            require(count <= MAX_ROWS, "retained outbox exceeds migration inspection bound")
            digest.update(json.dumps(list(row), separators=(",", ":")).encode() + b"\n")
    return {"rows": count, "identity_sha256": digest.hexdigest()}


def preflight(conn):
    """No schema repair, PRAGMA mutation, historical import or retention here."""
    db._event_core_startup_preflight(conn)
    tables = {row[0] for row in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    ledger_present = "backtest_signals" in tables
    populated = ledger_present and conn.execute("SELECT 1 FROM backtest_signals LIMIT 1").fetchone() is not None
    marker = None
    if "app_meta" in tables:
        marker = conn.execute("SELECT v FROM app_meta WHERE k=?", (integration_outbox.BACKFILL_META,)).fetchone()
    require(not populated or (marker is not None and marker[0] == "1" and "integration_signal_outbox" in tables),
            "historical export backfill requires separate reviewed migration")
    # Missing backfill markers with existing event/compatibility data and
    # incompatible event schemas are refused by the read-only preflight above.
    # They need a separately reviewed legacy migration, not this startup plan.
    return {"outbox": retained_outbox(conn), "ledger_empty": not populated,
            "historical_export_backfill": "already_complete" if populated else "no_historical_rows"}


def _base(conn):
    cursor = conn.cursor()
    db._init_app_meta(cursor)
    db._init_news_cache(cursor)
    db._init_article_sources(cursor)


def _news(conn, version):
    cursor = conn.cursor()
    groups = db.startup_schema_migration_groups()
    require(len(groups) == 7, "news migration plan changed")
    for column, definition in groups[version - 1]:
        db._add_column(cursor, "news_cache", column, definition)
    row = cursor.execute("SELECT v FROM app_meta WHERE k='schema_version'").fetchone()
    current = int(row[0]) if row else 0
    cursor.execute("INSERT OR REPLACE INTO app_meta(k,v) VALUES('schema_version',?)",
                   (str(max(version, min(7, current))),))
    require(all(db._column_exists(cursor, "news_cache", column) for group in groups[:version]
                for column, _ in group), "news schema postcondition failed")


def _event_runtime(conn, now):
    db.init_event_core_schema(conn)
    db.init_event_report_indexes(conn)
    repaired = db.reconcile_event_group_priorities(conn, now=now)
    conn.execute("INSERT OR REPLACE INTO app_meta(k,v) VALUES('event_mapping_replay_schema_version','2')")
    return {"reconciled_event_priorities": repaired}


def _market_ledgers(conn, now):
    cursor = conn.cursor()
    for initializer in (db._init_market_prices, db._init_future_prices, db._init_backtest_signals,
                        db.init_score_explanation_storage, db._init_backtest_signal_securities,
                        db._init_score_bridge, db._init_event_reference_triggers):
        initializer(cursor)
    integration_outbox.init_integration_signal_outbox(conn, now=now, backfill=False, trim=False)
    # There is no historical work to import in the only marker-less start we
    # support. Recording that fact prevents a future normal initializer from
    # importing newly created business rows as historical data.
    if conn.execute("SELECT 1 FROM backtest_signals LIMIT 1").fetchone() is None:
        conn.execute("INSERT OR IGNORE INTO app_meta(k,v) VALUES(?, '1')", (integration_outbox.BACKFILL_META,))


def _operational_tables(conn):
    cursor = conn.cursor()
    for initializer in (db._init_api_stats, db._init_score_distribution, db._init_request_logs,
                        db._init_feed_health, db._init_llm_call_logs, db.init_news_processing_storage,
                        db.init_score_quality_storage, db.init_first_rejection_storage,
                        db._init_article_feedback, db._init_feedback_example_snapshots,
                        db._init_scoring_contract_snapshots):
        initializer(cursor)
    db._ensure_news_cache_indexes(conn)


def verify_final(conn):
    db.verify_event_core_schema(conn)
    db.verify_event_core_backfill_state(conn)
    db.verify_integration_outbox_schema(conn)
    require(conn.execute("PRAGMA foreign_key_check").fetchone() is None, "foreign key postcondition failed")
    row = conn.execute("SELECT v FROM app_meta WHERE k='schema_version'").fetchone()
    require(row is not None and row[0] == "7", "news schema marker differs")
    required = {"app_meta", "news_cache", "backtest_signals", "news_processing_logs", "llm_call_logs", "feed_health"}
    present = {row[0] for row in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    require(required <= present, "startup schema incomplete")


def apply_step(conn, step_id, *, now):
    require(conn.in_transaction and step_id in STEP_IDS, "fixed migration step needs an existing transaction")
    require(conn.execute("PRAGMA synchronous").fetchone()[0] == 2, "migration durability must remain FULL")
    result = {}
    if step_id == "base-tables":
        _base(conn)
    elif step_id.startswith("news-schema-"):
        _news(conn, int(step_id.rsplit("-", 1)[1]))
    elif step_id == "event-core-backfill":
        result = event_migration.backfill_stable_event_core_in_transaction(conn, now=now, force=False)
    elif step_id == "event-runtime":
        result = _event_runtime(conn, now)
    elif step_id == "market-ledgers":
        _market_ledgers(conn, now)
    elif step_id == "operational-tables":
        _operational_tables(conn)
    elif step_id == "feed-health":
        db._sync_feed_health_from_config(conn.cursor())
    else:
        verify_final(conn)
    require(conn.in_transaction and conn.execute("PRAGMA synchronous").fetchone()[0] == 2,
            "step escaped the controlling transaction or changed durability")
    return result or {}
