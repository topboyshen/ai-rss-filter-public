"""Bounded public projection of today's existing daily-report classifications.

Only worker/live builds call this loader. Production Web serves its published
HTML snapshot; there is no model invocation, scoring or persistence here.
"""

from __future__ import annotations

from datetime import datetime, timedelta
from functools import lru_cache
import ipaddress
import logging
import re
import sqlite3
import time
from typing import Any
import unicodedata
from urllib.parse import parse_qsl, unquote, urlsplit

from db.database import get_read_only_connection
from services import event_daily_report as daily
from services.event_radar_query import EventRadarUnavailableError, _safe_external_url
from utils.helpers import plain_text_from_markup

logger = logging.getLogger(__name__)
MAX_HOME_READ_SECONDS = 5.0
MAX_HIGHLIGHTS = 3
MAX_CORRECTIONS = 2

# Only explicit monetary/percentage decimals are presentation-equivalent.
# Never Decimal-normalize bare dates, versions, identifiers, or units themselves.
_UNIT_DECIMAL_RE = re.compile(
    r"(?<![\dA-Za-z_.])([+-]?\d+\.\d+)(万亿元|亿元|万元|元|[%‰‱])"
)
_METRIC_COPULA_RE = re.compile(
    r"(归母净利润|扣非净利润|净利润|营业收入)为"
    r"(?=[+-]?\d+(?:\.\d+)?(?:万亿元|亿元|万元|元|[%‰‱]))"
)
# Keep the report tokens plus standalone signs/questions. Unmatched Unicode
# math symbols are retained below, rather than treated as cosmetic punctuation.
_SLOT_TOKEN_RE = re.compile(daily._KEY_TOKEN_RE.pattern + r"|[+\-?]")


def _headline_slot_key(text: str) -> str:
    """Conservative home-only spelling key, not an event identity or fuzzy match.

    Keep the whole headline, polarity, period, metric, numbers and units. Only
    trim decimal trailing zeroes beside explicit units and the optional 为
    directly between a financial metric and a number. Original text is kept.
    """
    value = unicodedata.normalize("NFKC", plain_text_from_markup(text))
    value = value.translate(daily._SIGN_TRANSLATION)

    def trim_decimal(match: re.Match[str]) -> str:
        integer, fraction = match[1].split(".")
        fraction = fraction.rstrip("0")
        return integer + ("." + fraction if fraction else "") + match[2]

    value = _UNIT_DECIMAL_RE.sub(trim_decimal, value)
    value = _METRIC_COPULA_RE.sub(r"\1", value)
    value = value.casefold()
    tokens: list[str] = []
    end = 0
    for match in _SLOT_TOKEN_RE.finditer(value):
        tokens.extend(char for char in value[end:match.start()] if unicodedata.category(char) == "Sm")
        tokens.append(match[0])
        end = match.end()
    tokens.extend(char for char in value[end:] if unicodedata.category(char) == "Sm")
    return " ".join(tokens)


def _select_distinct_items(
    items: list[dict[str, Any]], candidates: dict[str, tuple], limit: int,
) -> list[dict[str, Any]]:
    """Skip provable spelling duplicates before the cap, then fill in order.

    Use the primary *source* headline, never mutable event metadata, a model
    summary, or company-only similarity. Reports and stored groups are intact.
    """
    selected: list[dict[str, Any]] = []
    seen: set[tuple] = set()
    # Bounded to this selection; no new persistent cache of source text.
    headline_key = lru_cache(maxsize=8192)(_headline_slot_key)
    for item in items:
        state = candidates[item["event_id"]][1]
        primary = _primary_observation(state)
        headline = headline_key(primary["claim_text"])
        fields = ("fresh", "history", "prior_support", "background_corrections")
        # The shared read model caps headlines at 1000 characters. At that
        # boundary missing trailing objects cannot prove claim equivalence.
        incomplete = any(len(row["claim_text"]) >= 1000 for field in fields for row in state[field])
        # Identical new wording must not hide another group's denial history
        # or other facts behind a generic latest title. Do not pool evidence.
        context = tuple(tuple(sorted({
            (headline_key(row["claim_text"]), daily._correction_kind(row))
            for row in state[field]
        })) for field in fields)
        key = (
            item["block"], daily._correction_kind(primary), headline or item["event_id"],
            context, item["event_id"] if incomplete else "",
        )
        if key in seen:
            continue
        seen.add(key)
        selected.append(item)
        if len(selected) >= limit:
            break
    return selected


def _check_deadline(deadline: float) -> None:
    if time.monotonic() >= deadline:
        raise sqlite3.OperationalError("home event digest read deadline exceeded")


def _sensitive_query_key(key: str) -> bool:
    # parse_qsl decodes one layer already. Reject still-encoded keys after a
    # bounded extra decode instead of risking disclosure through odd encodings.
    for _ in range(3):
        decoded = unquote(key)
        if decoded == key:
            break
        key = decoded
    if "%" in key:
        return True
    normalized = re.sub(r"[^a-z0-9]", "", unicodedata.normalize("NFKC", key).casefold())
    return normalized in {"key", "sig", "auth"} or any(
        word in normalized for word in (
            "apikey", "accesskey", "token", "authorization", "password", "passwd",
            "secret", "signature", "credential", "session", "jwt", "bearer",
        )
    )


def _public_article_url(value: object) -> str:
    """Stricter public-card URL boundary; never change global URL semantics.

    No DNS/network lookup occurs. Private address literals and local hostnames
    are rejected, as are credential-bearing parameters. Safe query identities
    (including CLS ai_filter_item) remain byte-for-byte intact.
    """
    url = _safe_external_url(value)
    if not url:
        return ""
    try:
        parsed = urlsplit(url)
        host = (parsed.hostname or "").casefold().rstrip(".")
        if "%" in host or not host:
            return ""
        try:
            address = ipaddress.ip_address(host)
        except ValueError:
            if (
                "." not in host
                or host.endswith((".localhost", ".local", ".internal", ".lan", ".home", ".localdomain"))
                or re.fullmatch(r"(?:0x[0-9a-f]+|[0-9]+)(?:\.(?:0x[0-9a-f]+|[0-9]+))*", host)
            ):
                return ""
        else:
            if not address.is_global:
                return ""
        for component in (parsed.query, parsed.fragment):
            parameters = parse_qsl(component.replace(";", "&"), keep_blank_values=True, max_num_fields=256)
            if any(_sensitive_query_key(key) for key, _value in parameters):
                return ""
    except (ValueError, UnicodeError):
        return ""
    return url


def _public_source(source: dict[str, Any]) -> dict[str, str]:
    """Expose only the safe article link and source-bound display fields.

    source_name can be a private feed URL; never expose it on the public card.
    The article hostname is a safe fallback label, not an independent-publisher
    or corroboration claim.
    """
    url = _public_article_url(source.get("url"))
    return {
        "url": url,
        "publisher": (urlsplit(url).hostname or "来源") if url else "来源链接不可用",
        "claim_text": str(source.get("claim_text") or ""),
        "observed_label": str(source.get("observed_label") or ""),
        "published_label": str(source.get("published_label") or ""),
        "correction_kind": str(source.get("correction_kind") or ""),
    }


def _public_item(item: dict[str, Any]) -> dict[str, Any]:
    sources = [_public_source(source) for source in item["sources"][:1]]
    source = sources[0] if sources else {}
    return {
        "event_id": item["event_id"],
        "title": source.get("claim_text") or "原文标题不可用",
        "block": item["block"],
        "block_label": daily.BLOCK_LABELS[item["block"]],
        "correction_kind": item["correction_kind"],
        "change": item["change"],
        "background_note": item.get("background_note", ""),
        "priority": item["priority"],
        "priority_label": item["priority_label"],
        "latest_at": item["latest_at"],
        "observed_label": f"最近入库 {daily._clock_label(item['latest_at'])}",
        "companies": item["companies"],
        "sources": sources,
    }


def _primary_observation(state: dict[str, Any]) -> dict[str, Any]:
    """Bind the compact headline, change and source to the same fresh claim."""
    if state["block"] == "correction":
        candidates = state["denials"] or state["corrections"]
    elif state["block"] == "pending":
        candidates = [item for item in state["fresh"] if daily._PENDING_RE.search(item["claim_text"])]
    else:
        candidates = state["fresh"]
    return max(candidates, key=daily._ingest_order)


def _build_home_event_digest(conn: sqlite3.Connection, now: int, deadline: float) -> dict[str, Any]:
    day = datetime.fromtimestamp(now, daily.REPORT_TZ).date()
    metadata, items, candidates, members = daily.prepare_daily_report(conn, day, now)
    _check_deadline(deadline)
    highlights = [
        item for key in ("new", "advanced", "pending")
        for item in daily.eligible_report_items(items, key)
    ]
    highlights.sort(key=lambda item: (-item["priority"], -item["latest_at"], item["event_id"]))
    selected = [
        {"key": "highlights", "entries": _select_distinct_items(highlights, candidates, MAX_HIGHLIGHTS)},
        {"key": "corrections", "entries": _select_distinct_items(
            daily.eligible_report_items(items, "correction"), candidates, MAX_CORRECTIONS,
        )},
    ]
    primary_source_ids = {}
    for block in selected:
        for item in block["entries"]:
            event_id = item["event_id"]
            group, state = candidates[event_id]
            # Only the headline whose source is actually shown can support a
            # public company chip; mutable metadata or a hidden older source
            # must not make that visible headline appear to name the issuer.
            primary = _primary_observation(state)
            group["association_texts"] = [primary["claim_text"]]
            primary_source_ids[event_id] = primary["evidence_id"]
            candidates[event_id] = (group, {
                **state,
                "fresh": [primary] + [row for row in state["fresh"] if row is not primary],
            })
    daily._materialize_daily_blocks(
        conn, selected, candidates, members, metadata["cutoff"], primary_source_ids=primary_source_ids,
    )
    for block in selected:
        for item in block["entries"]:
            if candidates[item["event_id"]][1]["background_corrections"]:
                item["background_note"] = "此前已有否认／更正，新增报道不代表推翻"
    _check_deadline(deadline)
    week = (day - timedelta(days=day.weekday())).isoformat()
    return {
        **{key: metadata[key] for key in (
            "date", "cutoff", "cutoff_label", "freshness", "data_state", "data_state_label",
        )},
        "week": week,
        "highlights": [_public_item(item) for item in selected[0]["entries"]],
        "corrections": [_public_item(item) for item in selected[1]["entries"]],
        "overview_url": "/dashboard/events?tab=overview",
        "daily_url": metadata["overview_url"],
        "weekly_url": f"/dashboard/events/weekly?week={week}",
        "empty_label": "暂无达到首页展示门槛的事件变化，更多内容见日报",
    }


def load_home_event_digest(*, now: int | None = None) -> dict[str, Any]:
    """One read-only transaction with a five-second SQL/phase budget.

    Python phases are checked at boundaries, not preempted mid-instruction.
    A failed build raises so the publisher retains its last successful card.
    """
    current = int(time.time()) if now is None else int(now)
    deadline = time.monotonic() + MAX_HOME_READ_SECONDS
    try:
        with get_read_only_connection() as conn:
            conn.execute("PRAGMA busy_timeout=100")
            conn.set_progress_handler(lambda: int(time.monotonic() >= deadline), 1000)
            try:
                conn.execute("BEGIN")
                with daily._report_text_cache():
                    result = _build_home_event_digest(conn, current, deadline)
                _check_deadline(deadline)
                return result
            finally:
                conn.set_progress_handler(None, 0)
                if conn.in_transaction:
                    conn.rollback()
    except (sqlite3.Error, OSError, RuntimeError) as exc:
        logger.warning("Home event digest read failed: %s", type(exc).__name__)
        raise EventRadarUnavailableError("home event digest is unavailable") from exc
