"""Request-log table I/O. Schema init stays in database.init_db."""

from __future__ import annotations

from typing import List, Optional, Tuple


def log_request(
    timestamp: int,
    url: str,
    total: int,
    passed: int,
    cache_hits: int,
    similarity_hits: int,
    blacklist_hits: int,
    fast_pass_hits: int,
    api_calls: int,
    duration_ms: int,
    error: Optional[str] = None,
) -> None:
    from db.database import db_lock, get_connection

    with db_lock:
        with get_connection() as conn:
            conn.execute(
                "INSERT INTO request_logs (timestamp, url, articles_total, articles_passed, "
                "cache_hits, similarity_hits, blacklist_hits, fast_pass_hits, api_calls, duration_ms, error) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    timestamp,
                    url,
                    total,
                    passed,
                    cache_hits,
                    similarity_hits,
                    blacklist_hits,
                    fast_pass_hits,
                    api_calls,
                    duration_ms,
                    error,
                ),
            )
            conn.commit()


def get_request_logs(limit: int = 50) -> List[Tuple]:
    from db.database import db_lock, get_connection

    with db_lock:
        with get_connection() as conn:
            rows = conn.execute(
                "SELECT timestamp, url, articles_total, articles_passed, cache_hits, "
                "similarity_hits, blacklist_hits, fast_pass_hits, api_calls, duration_ms, error "
                "FROM request_logs ORDER BY timestamp DESC LIMIT ?",
                (limit,),
            ).fetchall()
            return [tuple(r) for r in rows]
