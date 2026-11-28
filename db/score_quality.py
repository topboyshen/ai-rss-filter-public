"""Additive, observation-only scoring diagnostics; never rewrite score contracts."""
from __future__ import annotations

import logging
import re
import time

from scoring_provenance import scoring_contract_bucket

logger = logging.getLogger(__name__)


def _exact_clause(column: str, strategy_version: str) -> tuple[str, tuple]:
    """Formal statistics read one exact rule identity, not the contract bucket."""
    return f" AND {column} = ? ", (str(strategy_version or ""),)


def _bucket_clause(column: str, strategy_version: str) -> tuple[str, tuple]:
    """Cross-version trend keeps patch-only changes in one comparable series."""
    bucket = scoring_contract_bucket(strategy_version)
    if not bucket:
        return f" AND {column} = ? ", (str(strategy_version or ""),)
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
    clause, contract_params = _exact_clause("strategy_version", strategy_version)
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


def read_score_quality_history(
    hours: int,
    strategy_version: str,
    limit: int = 8,
) -> dict:
    """Cross-version trend inside one contract bucket; never feeds formal gates.

    Each patch is reported separately.  The formal read above stays exact, so a
    fresh patch starts with empty statistics while this history keeps the
    previous patches visible for comparison.
    """
    from db.database import get_read_only_connection
    cutoff = int(time.time()) - max(1, min(168, hours)) * 3600
    clause, bucket_params = _bucket_clause("strategy_version", strategy_version)
    params = (cutoff, *bucket_params)
    with get_read_only_connection() as conn:
        conn.execute('BEGIN')
        transport = conn.execute(
            "SELECT strategy_version,backend_name,model_name,purpose,COUNT(*),"
            "SUM(success),MAX(timestamp) "
            "FROM llm_call_logs WHERE timestamp>=?" + clause +
            "AND purpose IN ('score','stage1','stage2') "
            "GROUP BY strategy_version,backend_name,model_name,purpose",
            params,
        ).fetchall()
        validation = conn.execute(
            "SELECT strategy_version,COUNT(*),SUM(contract_valid),SUM(accepted),"
            "MAX(timestamp) FROM score_validation_logs "
            "WHERE timestamp>=?" + clause +
            "GROUP BY strategy_version",
            params,
        ).fetchall()
        final = conn.execute(
            "SELECT strategy_version,score_path,COUNT(*),MAX(scored_at) "
            "FROM backtest_signals WHERE scored_at>=?" + clause +
            "AND signal_stage='base' AND is_similarity_copy=0 "
            "GROUP BY strategy_version,score_path",
            params,
        ).fetchall()

    versions: dict[str, dict] = {}

    def bucket(version: str) -> dict:
        return versions.setdefault(version, {
            'strategy_version': version,
            'last_seen': 0,
            'transport_attempts': 0,
            'transport_succeeded': 0,
            'validation_observed': 0,
            'validation_accepted': 0,
            'final_usable': 0,
            'final_total': 0,
        })

    for version, _backend, _model, _purpose, attempts, succeeded, last_seen in transport:
        row = bucket(str(version or ''))
        row['transport_attempts'] += int(attempts or 0)
        row['transport_succeeded'] += int(succeeded or 0)
        row['last_seen'] = max(row['last_seen'], int(last_seen or 0))
    for version, observed, _contract_valid, accepted, last_seen in validation:
        row = bucket(str(version or ''))
        row['validation_observed'] += int(observed or 0)
        row['validation_accepted'] += int(accepted or 0)
        row['last_seen'] = max(row['last_seen'], int(last_seen or 0))
    for version, score_path, count, last_seen in final:
        row = bucket(str(version or ''))
        if score_path in ('llm', 'stage2'):
            row['final_usable'] += int(count or 0)
        row['final_total'] += int(count or 0)
        row['last_seen'] = max(row['last_seen'], int(last_seen or 0))

    ordered = sorted(
        (row for row in versions.values() if row['strategy_version']),
        key=lambda row: (row['last_seen'], row['strategy_version']),
        reverse=True,
    )[:max(1, min(50, int(limit)))]
    for row in ordered:
        row['usable_ratio'] = (
            row['final_usable'] / row['final_total'] if row['final_total'] else None
        )
    return {
        'hours': hours,
        'contract_version': scoring_contract_bucket(strategy_version),
        'versions': ordered,
    }
