"""Additive, observation-only scoring diagnostics; never rewrite score contracts."""
from __future__ import annotations

import logging
import re
import time

from scoring_provenance import scoring_contract_bucket

logger = logging.getLogger(__name__)


def _contract_clause(column: str, strategy_version: str) -> tuple[str, tuple]:
    """Keep the posterior series across patch-only identity changes."""
    bucket = scoring_contract_bucket(strategy_version)
    if not bucket:
        return f" AND {column} = ? ", (str(strategy_version),)
    return (
        f" AND ({column} = ? OR {column} LIKE ?) ",
        (bucket, f"{bucket}:%"),
    )


def init_score_quality_storage(cursor) -> None:
    cursor.execute("""CREATE TABLE IF NOT EXISTS score_validation_logs (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        timestamp INTEGER NOT NULL,
        strategy_version TEXT NOT NULL,
        backend_name TEXT NOT NULL,
        model_name TEXT NOT NULL,
        stage TEXT NOT NULL CHECK(stage IN ('score','stage1','stage2')),
        attempt TEXT NOT NULL CHECK(attempt IN ('initial','repair')),
        contract_valid INTEGER NOT NULL CHECK(contract_valid IN (0,1)),
        source_grounded INTEGER CHECK(source_grounded IN (0,1)),
        accepted INTEGER NOT NULL CHECK(accepted IN (0,1)),
        reason TEXT NOT NULL
    )""")
    cursor.execute("CREATE INDEX IF NOT EXISTS idx_score_validation_window "
                   "ON score_validation_logs(timestamp, strategy_version)")


def record_score_validation(*, strategy_version, backend_name, model_name, stage,
                            attempt, contract_valid, source_grounded, accepted, reason):
    """Best-effort telemetry failure must not change the scoring decision.

    Store bounded labels only: no source text, raw model response, links or secrets.
    Use NULL when grounding did not run, including invalid and insufficient input.
    """
    if (stage not in {'score', 'stage1', 'stage2'} or attempt not in {'initial', 'repair'}
            or len(str(strategy_version)) > 255):
        return
    try:
        from db.database import db_lock, get_connection
        with db_lock, get_connection() as conn:
            conn.execute(
                "INSERT INTO score_validation_logs (timestamp,strategy_version,backend_name,"
                "model_name,stage,attempt,contract_valid,source_grounded,accepted,reason) "
                "VALUES (?,?,?,?,?,?,?,?,?,?)",
                (int(time.time()), str(strategy_version), str(backend_name)[:80],
                 str(model_name)[:120], stage, attempt, int(bool(contract_valid)),
                 int(bool(source_grounded)) if contract_valid and source_grounded is not None else None,
                 int(bool(accepted)), re.sub(r'[^a-z0-9_:]', '', reason)[:100]),
            )
            conn.commit()
    except Exception:
        logger.warning("[ScoreQuality] validation observation unavailable", exc_info=False)


def read_score_quality(hours: int, strategy_version: str) -> dict:
    """One read transaction, explicit distinct units; old unobserved validations stay unknown."""
    from db.database import get_read_only_connection
    cutoff = int(time.time()) - max(1, min(168, hours)) * 3600
    clause, contract_params = _contract_clause("strategy_version", strategy_version)
    params = (cutoff, *contract_params)
    with get_read_only_connection() as conn:
        conn.execute('BEGIN')
        transport = conn.execute(
            "SELECT backend_name,model_name,purpose,COUNT(*),SUM(success) "
            "FROM llm_call_logs WHERE timestamp>=?" + clause +
            "AND purpose IN ('score','stage1','stage2') GROUP BY backend_name,model_name,purpose",
            params,
        ).fetchall()
        validation = conn.execute(
            "SELECT backend_name,model_name,stage,attempt,reason,COUNT(*),"
            "SUM(contract_valid),COUNT(source_grounded),SUM(source_grounded),SUM(accepted) "
            "FROM score_validation_logs WHERE timestamp>=?" + clause +
            "GROUP BY backend_name,model_name,stage,attempt,reason", params,
        ).fetchall()
        final = conn.execute(
            "SELECT score_path,COUNT(*) FROM backtest_signals "
            "WHERE scored_at>=?" + clause + "AND signal_stage='base' "
            "AND is_similarity_copy=0 GROUP BY score_path", params,
        ).fetchall()
    return {
        'available': True, 'hours': hours, 'strategy_version': strategy_version,
        'contract_version': scoring_contract_bucket(strategy_version),
        'transport': [dict(zip(('backend','model','stage','attempts','succeeded'), row)) for row in transport],
        'validation': [dict(zip(('backend','model','stage','attempt','reason','observed',
                                'contract_valid','grounding_observed','source_grounded','accepted'), row))
                       for row in validation],
        'final_paths': dict(final),
    }
