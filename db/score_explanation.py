"""Additive score provenance storage, independent of the Quant export contract."""

import json
import uuid

from scoring_explanation import (
    EXPLANATION_FIELDS,
    normalize_score_explanation,
    rebase_score_explanation,
)


TOKEN_FIELD = "score_explanation_token"
EXPLANATION_COLUMNS = ", ".join(EXPLANATION_FIELDS)
SCHEMA_COLUMNS = [(field, "REAL") for field in EXPLANATION_FIELDS] + [
    (TOKEN_FIELD, "TEXT")
]


def init_score_explanation_storage(cursor):
    """Idempotent nullable columns; never reconstruct historical diagnostics."""
    for table in ("news_cache", "backtest_signals"):
        columns = {row[1] for row in cursor.execute(f"PRAGMA table_info({table})")}
        if not columns:
            # Ledger-only writers do not require or create a mutable cache.
            continue
        for name, declaration in SCHEMA_COLUMNS:
            if name not in columns:
                cursor.execute(f"ALTER TABLE {table} ADD COLUMN {name} {declaration}")
        watched = [
            "score",
            "score_path",
            "strategy_version",
            "score_confidence",
            "direction",
        ]
        if table == "news_cache":
            watched += [
                "stage1_score",
                "stage1_model",
                "stage2_score",
                "stage2_model",
                "model_name",
                "raw_title",
                "tag",
                "logic",
                "summary",
                "review_reason",
            ]
        changed = " OR ".join(f"NEW.{name} IS NOT OLD.{name}" for name in watched)
        cleared = ", ".join(f"{name}=NULL" for name, _ in SCHEMA_COLUMNS)
        key = "link" if table == "news_cache" else "id"
        cursor.execute(
            f"CREATE TRIGGER IF NOT EXISTS trg_{table}_legacy_score_explanation "
            f"AFTER UPDATE OF {', '.join(watched)} ON {table} "
            f"WHEN NEW.{TOKEN_FIELD} IS OLD.{TOKEN_FIELD} AND ({changed}) BEGIN "
            f"UPDATE {table} SET {cleared} WHERE {key}=NEW.{key}; END"
        )


def _normalized(payload, *, score, score_path):
    return normalize_score_explanation(
        payload or {}, score=score, score_path=score_path
    )


def bind_cache_score_explanation(payload, signal, *, score, score_path):
    """Compare diagnostic payloads before any cache or ledger mutation."""
    values = _normalized(payload, score=score, score_path=score_path)
    if signal is not None:
        other = _normalized(
            signal, score=signal.get("score"), score_path=signal.get("score_path")
        )
        if values != other:
            raise ValueError("cache and backtest signal score explanations differ")
    values[TOKEN_FIELD] = uuid.uuid4().hex
    if signal is not None:
        signal.update(values)
    return values


def prepare_signal_score_explanation(signal):
    payload = dict(signal)
    normalize = (
        rebase_score_explanation
        if payload.get("auto_revision")
        else normalize_score_explanation
    )
    payload.update(
        normalize(
            payload, score=payload.get("score"), score_path=payload.get("score_path")
        )
    )
    if any(payload.get(field) is not None for field in EXPLANATION_FIELDS):
        # A caller's old fingerprint cannot bypass newly frozen dimensions.
        payload.pop("revision_state_key", None)
    payload[TOKEN_FIELD] = payload.get(TOKEN_FIELD) or uuid.uuid4().hex
    return payload


def score_revision_payload(state, signal):
    """Unknown-only diagnostics preserve the existing idempotency fingerprint."""
    if any(signal.get(field) is not None for field in EXPLANATION_FIELDS):
        state["score_explanation"] = {
            field: signal.get(field) for field in EXPLANATION_FIELDS
        }
    return json.dumps(state, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _write_explanation(conn, table, key, value, explanation):
    fields = (*EXPLANATION_FIELDS, TOKEN_FIELD)
    conn.execute(
        f"UPDATE {table} SET "
        + ", ".join(f"{field}=?" for field in fields)
        + f" WHERE {key}=?",
        tuple(explanation.get(field) for field in fields) + (value,),
    )


def write_ledger_score_explanation(conn, ledger_id, signal):
    _write_explanation(conn, "backtest_signals", "id", ledger_id, signal)


def _existing_signal_explanation(conn, signal):
    if signal.get("auto_revision"):
        row = conn.execute(
            "SELECT * FROM backtest_signals WHERE link=? AND strategy_version=? ORDER BY id DESC LIMIT 1",
            (signal["link"], signal.get("strategy_version", "")),
        ).fetchone()
    else:
        row = conn.execute(
            "SELECT * FROM backtest_signals WHERE link=? AND strategy_version=? AND revision_key=?",
            (
                signal["link"],
                signal.get("strategy_version", ""),
                signal.get("revision_key") or "base",
            ),
        ).fetchone()
    return dict(row) if row else None


def check_score_explanation_replay(conn, signal):
    """Do not let a deduplicated ledger write silently drop new provenance."""
    if not any(signal.get(field) is not None for field in EXPLANATION_FIELDS):
        return
    existing = _existing_signal_explanation(conn, signal)
    if existing is None or any(
        existing.get(field) != signal.get(field) for field in EXPLANATION_FIELDS
    ):
        raise ValueError("idempotent ledger replay changes score explanation")


def write_cache_score_explanation(conn, link, explanation, signal=None):
    values = dict(explanation)
    if signal is not None:
        existing = _existing_signal_explanation(conn, signal)
        if existing and all(
            existing.get(field) == values.get(field) for field in EXPLANATION_FIELDS
        ):
            values[TOKEN_FIELD] = existing.get(TOKEN_FIELD)
    _write_explanation(conn, "news_cache", "link", link, values)


def retain_unchanged_score_explanation(conn, explanation, signal, cached, **context):
    """Legacy idempotent calls may retain only a fully matching frozen basis."""
    if cached is None or any(
        explanation.get(field) is not None for field in EXPLANATION_FIELDS
    ):
        return explanation
    cached = dict(cached)
    if any(cached.get(field) != value for field, value in context.items()):
        return explanation
    if not any(cached.get(field) is not None for field in EXPLANATION_FIELDS):
        return explanation
    if signal is not None:
        ledger = _existing_signal_explanation(conn, signal)
        bound_fields = (
            *EXPLANATION_FIELDS,
            "score",
            "score_path",
            "strategy_version",
            "direction",
            "score_confidence",
        )
        if ledger is None or any(
            ledger.get(field) != cached.get(field) for field in bound_fields
        ):
            return explanation
    retained = _normalized(
        cached, score=context["score"], score_path=context["score_path"]
    )
    retained[TOKEN_FIELD] = cached.get(TOKEN_FIELD)
    if signal is not None:
        signal.update(retained)
    return retained


def bind_revised_score_explanation(item, cache_row, latest_signal):
    signal = item["backtest_signal"]
    source = (
        signal
        if any(signal.get(field) is not None for field in EXPLANATION_FIELDS)
        else dict(latest_signal or cache_row or {})
    )
    explanation = rebase_score_explanation(
        source, score=signal["score"], score_path=signal["score_path"]
    )
    explanation[TOKEN_FIELD] = uuid.uuid4().hex
    signal.update(explanation)
    item["score_explanation"] = explanation


def check_cached_score_explanation_replay(cache_row, signal):
    # An old writer may invalidate only cache diagnostics. A replay must remain
    # a no-op, not restore provenance whose mutable source context has changed.
    # The caller still checks the complete legacy score state and title below.
    if all(cache_row[field] is None for field in EXPLANATION_FIELDS):
        return
    if any(cache_row[field] != signal.get(field) for field in EXPLANATION_FIELDS):
        raise ValueError("idempotent ledger replay cannot change cache explanation")


def update_revised_cache_score(conn, item):
    fields = (
        "title",
        "score",
        "score_path",
        "direction",
        "score_confidence",
        *EXPLANATION_FIELDS,
        TOKEN_FIELD,
    )
    values = {**item, **item["score_explanation"]}
    persisted = _existing_signal_explanation(conn, item["backtest_signal"])
    if persisted:
        values[TOKEN_FIELD] = persisted.get(TOKEN_FIELD)
    cursor = conn.execute(
        "UPDATE news_cache SET "
        + ", ".join(f"{field}=?" for field in fields)
        + " WHERE link=?",
        tuple(values.get(field) for field in fields) + (item["link"],),
    )
    if cursor.rowcount <= 0:
        raise ValueError(f"cached entry does not exist: {item['link']}")


def upsert_explained_cache_entry(conn, values):
    """Keep the existing cache UPSERT and primary-source policy unchanged."""
    fields = (
        "link",
        "title",
        "raw_title",
        "score",
        "tag",
        "logic",
        "created_at",
        "summary",
        "feed_url",
        "score_path",
        "prompt_key",
        "model_name",
        "latency_ms",
        "error_type",
        "source_tier",
        "strategy_version",
        "stage1_score",
        "stage1_model",
        "stage2_score",
        "stage2_model",
        "score_confidence",
        "review_reason",
        "direction",
        "event_id",
        "event_version",
        "safe_neutral_reason",
    )
    assignments = [
        "feed_url = CASE WHEN TRIM(COALESCE(news_cache.feed_url, '')) = '' "
        "THEN excluded.feed_url ELSE news_cache.feed_url END"
        if field == "feed_url"
        else f"{field} = excluded.{field}"
        for field in fields
        if field != "link"
    ]
    conn.execute(
        "INSERT INTO news_cache ("
        + ", ".join(fields)
        + ") VALUES ("
        + ", ".join("?" for _ in fields)
        + ") ON CONFLICT(link) DO UPDATE SET "
        + ", ".join(assignments),
        values,
    )
