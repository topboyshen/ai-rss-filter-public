"""Lock validation output, read-only behavior and first-failure precedence."""

import pytest

from db import database
from db import event_migration as migration


def test_empty_validation_output_and_stage_order_are_stable():
    database.init_db()
    with database.get_connection() as conn:
        statements = []
        conn.set_trace_callback(statements.append)
        before = conn.total_changes
        result = migration.validate_stable_event_core(conn)
        conn.set_trace_callback(None)
        assert result == {
            "validated_news_rows": 0,
            "validated_ledger_rows": 0,
            "validated_signal_mapping_rows": 0,
            "validated_mapping_rows": 0,
            "validated_outbox_rows": 0,
            "validated_future_price_rows": 0,
            "validated_outcome_price_references": 0,
            "outbox_floor_seq": 1,
            "outbox_last_seq": 0,
            "foreign_key_violations": 0,
        }
        assert conn.total_changes == before
        assert not conn.in_transaction
    normalized = [" ".join(sql.split()) for sql in statements]
    phases = [
        "PRAGMA foreign_keys",
        "SELECT 1 FROM integration_signal_outbox WHERE",
        "SELECT COUNT(*) FROM news_cache",
        "SELECT COUNT(*) FROM future_prices",
        "SELECT outcome.*",
        "SELECT event_id FROM events ORDER BY event_id",
        "PRAGMA foreign_key_check",
        "SELECT COUNT(*) FROM integration_signal_outbox",
    ]
    positions = [
        next(i for i, sql in enumerate(normalized) if sql.startswith(phase))
        for phase in phases
    ]
    assert positions == sorted(positions)
    assert all(sql.upper().startswith(("SELECT ", "PRAGMA ")) for sql in normalized)


@pytest.mark.parametrize("value", ["not-a-time", "-1"])
def test_score_marker_error_precedes_other_schema_failures(value):
    database.init_db()
    with database.get_connection() as conn:
        conn.execute(
            "INSERT INTO app_meta (k,v) VALUES (?,?)",
            (migration.SCORE_DIMENSIONS_META_KEY, value),
        )
        conn.commit()
        before = conn.total_changes
        with pytest.raises(RuntimeError, match="^score dimensions marker is invalid$"):
            migration.validate_stable_event_core(conn)
        assert conn.total_changes == before


def test_outbox_marker_error_precedes_missing_compatibility_trigger():
    database.init_db()
    with database.get_connection() as conn:
        conn.execute(
            "UPDATE app_meta SET v='invalid' WHERE k=?",
            (migration.MAPPING_DIMENSIONS_BACKFILL_META,),
        )
        conn.execute("DROP TRIGGER trg_news_cache_event_insert")
        conn.commit()
        before = conn.total_changes
        with pytest.raises(
            RuntimeError, match="^integration outbox mapping dimensions marker is invalid$"
        ):
            migration.validate_stable_event_core(conn)
        assert conn.total_changes == before


def test_missing_snapshot_trigger_error_precedes_invalid_news_reference():
    database.init_db()
    with database.get_connection() as conn:
        assert conn.execute(
            "SELECT v FROM app_meta WHERE k='mapping_validation_started_at'"
        ).fetchone() is not None
        conn.execute("DROP TRIGGER trg_backtest_signal_securities_delete")
        conn.execute("INSERT INTO news_cache(link,title) VALUES ('fixture:invalid','title')")
        conn.commit()
        before = conn.total_changes
        with pytest.raises(
            RuntimeError,
            match="^mapping validation missing trigger\\(s\\): trg_backtest_signal_securities_delete$",
        ):
            migration.validate_stable_event_core(conn)
        assert conn.total_changes == before


def test_disabled_foreign_keys_stops_before_other_reads():
    database.init_db()
    with database.get_connection() as conn:
        conn.execute("PRAGMA foreign_keys=OFF")
        statements = []
        conn.set_trace_callback(statements.append)
        with pytest.raises(
            RuntimeError, match="^foreign-key enforcement is required for validation$"
        ):
            migration.validate_stable_event_core(conn)
        conn.set_trace_callback(None)
        assert statements == ["PRAGMA foreign_keys"]
