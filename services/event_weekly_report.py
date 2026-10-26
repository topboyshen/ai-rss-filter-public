"""Read-only weekly change report over the existing article evidence store.

Read a Beijing Monday-to-Monday window before applying display limits. Shared
daily primitives retain the same event identity, source and headline rules;
neither report writes, calls a model, or changes a scoring contract.
"""

from __future__ import annotations

import logging
import re
import sqlite3
import time
import unicodedata
from collections import Counter
from datetime import date, datetime, timedelta
from typing import Any
from urllib.parse import urlencode

import db.database as database
from db.database import get_read_only_connection
from services import event_daily_report as daily
from services.event_radar_query import EventRadarQueryError, EventRadarUnavailableError
from services.event_report_cache import SingleFlightReportCache
from utils.report_paging import parse_weekly_paging

logger = logging.getLogger(__name__)

MAX_WEEK_EVIDENCE_ROWS = 140_000
MAX_LINKED_HISTORY_ROWS = 60_000
MAX_LOOKBACK_EVIDENCE_ROWS = 60_000
MAX_HIGHLIGHTS = 5
MAX_REPORT_SECONDS = 5.0
# The report is CPU-bound Python (about 70% of its time on the read-only
# snapshot), so identical concurrent reads share one build and distinct builds
# run one at a time. A hit is a private copy that keeps its original
# generated-at; the current week also depends on a cheap ingest marker.
CURRENT_WEEK_CACHE_SECONDS = 30.0
PAST_WEEK_CACHE_SECONDS = 120.0
_REPORT_CACHE = SingleFlightReportCache(max_entries=24, wait_seconds=15.0)
HISTORICAL_VIEW_LABEL = "按历史入库记录重建的视图，非当周封存周报"
_HISTORY_CORRECTION_CUE_RE = re.compile("|".join(map(re.escape, daily._CORRECTION_CUE_WORDS)))


def _week_bounds(now: int) -> tuple[date, date]:
    today = datetime.fromtimestamp(now, daily.REPORT_TZ).date()
    current = today - timedelta(days=today.weekday())
    oldest = today - timedelta(days=daily.MAX_HISTORY_DAYS)
    earliest = oldest + timedelta(days=(-oldest.weekday()) % 7)
    return earliest, current


def parse_report_week(value: object = None, *, now: int) -> date:
    """Accept a Monday ISO date; omission selects the last complete week."""
    earliest, current = _week_bounds(now)
    if value is None:
        return current - timedelta(days=7)
    if not isinstance(value, str) or not re.fullmatch(r"\d{4}-\d{2}-\d{2}", value):
        raise EventRadarQueryError("week must be a Monday in YYYY-MM-DD format")
    try:
        selected = date.fromisoformat(value)
    except ValueError as exc:
        raise EventRadarQueryError("week is invalid") from exc
    if selected.weekday() != 0:
        raise EventRadarQueryError("week must be a Monday")
    if selected < earliest or selected > current:
        raise EventRadarQueryError("week is out of range")
    return selected


def report_window(week_start: date, now: int) -> dict[str, Any]:
    start = int(datetime.combine(week_start, datetime.min.time(), daily.REPORT_TZ).timestamp())
    end = start + 7 * 86_400
    cutoff = min(end - 1, int(now))
    return {"start": start, "end": end, "cutoff": cutoff, "partial": cutoff < end - 1}


def _linked_history(
    conn: sqlite3.Connection, members: set[str], before: int,
    *, lookback_rows: list[dict[str, Any]] | None = None,
) -> tuple[dict[str, list[dict[str, Any]]], bool]:
    """Reuse complete lookback, then seek older rows for linked event members.

    None means lookback was not complete and all prior rows must be read.
    The shared cap retains older rows first, then complete lookback rows;
    any overflow is explicitly reported instead of claiming full history.
    """
    result: dict[str, list[dict[str, Any]]] = {}
    if not members:
        return result, False
    remaining = MAX_LINKED_HISTORY_ROWS
    older_before = before - daily.REPEAT_LOOKBACK_SECONDS if lookback_rows is not None else before
    # Seek only unbound evidence for each linked identity before the ingest
    # cutoff. The additive report index excludes mapping rows and can constrain
    # observed_at; the announcement-time index cannot do either. Keep explicit
    # ingest ordering across members and the existing parameter/shared cap.
    available = conn.getlimit(sqlite3.SQLITE_LIMIT_VARIABLE_NUMBER) - len(daily._PUBLIC_SCORE_PATH_VALUES) - 2
    if available < 1:
        raise sqlite3.OperationalError("weekly report requires a larger SQLite variable limit")
    chunk_size = min(20_000, available)
    identities = sorted(members)
    for offset in range(0, len(identities), chunk_size):
        chunk = identities[offset:offset + chunk_size]
        if len(chunk) == 1:
            # One identity is already in ingest order in the report index.
            # Stream its payload directly: there is no wide SQL sorter, and
            # no need to retain 60k narrow rows, re-read them in IN batches,
            # then sort the materialized payload again in Python.
            rows, cut = _single_member_history(conn, chunk[0], older_before, remaining)
            if rows:
                result[chunk[0]] = rows
            if cut:
                return result, True
            remaining -= len(rows)
            continue
        placeholders = ",".join("?" for _ in chunk)
        selected = conn.execute(
            "SELECT ev.evidence_id, ev.observed_at, ev.rowid FROM event_evidence AS ev "
            "INDEXED BY idx_event_evidence_unbound_event_observed "
            f"WHERE ev.event_id IN ({placeholders}) AND ev.mapping_id IS NULL "
            "AND ev.observed_at < ? "
            f"AND {daily._public_cache_evidence_predicate('ev')} "
            "ORDER BY ev.observed_at, ev.evidence_id LIMIT ?",
            (*chunk, older_before, *daily._PUBLIC_SCORE_PATH_VALUES, remaining + 1),
        ).fetchall()
        # Sort only narrow identities in SQLite. Sorting the title/source
        # payload of a single large event can spill hundreds of MiB to temp
        # storage even though the selected identities fit comfortably. Fetch
        # exactly the selected payloads by rowid, then restore the same order.
        rows = _history_payloads(conn, selected[:remaining])
        for item in rows:
            result.setdefault(item["event_id"], []).append(item)
        if len(selected) > remaining:
            return result, True
        remaining -= len(selected)
    recent = sorted(
        [row for row in lookback_rows or [] if row["event_id"] in members],
        key=daily._ingest_order,
    )
    for row in recent[:remaining]:
        result.setdefault(row["event_id"], []).append(row)
    return result, len(recent) > remaining


def _single_member_history(
    conn: sqlite3.Connection, member: str, before: int, limit: int,
) -> tuple[list[dict[str, Any]], bool]:
    """Stream one index-ordered member, retaining the same overflow sentinel."""
    cursor = conn.execute(
        f"SELECT {daily._EVIDENCE_COLUMNS} FROM event_evidence AS ev "
        "INDEXED BY idx_event_evidence_unbound_event_observed "
        "WHERE ev.event_id = ? AND ev.mapping_id IS NULL AND ev.observed_at < ? "
        f"AND {daily._public_cache_evidence_predicate('ev')} "
        "ORDER BY ev.observed_at, ev.evidence_id LIMIT ?",
        (member, before, *daily._PUBLIC_SCORE_PATH_VALUES, limit + 1),
    )
    rows = []
    try:
        for index, row in enumerate(cursor):
            if index == limit:
                return rows, True
            rows.append(_history_evidence_dict(row))
    finally:
        cursor.close()
    return rows, False


def _history_payloads(conn: sqlite3.Connection, identities: list) -> list[dict[str, Any]]:
    """Read selected wide rows without a second wide temporary SQL sort."""
    payloads = []
    limit = min(20_000, conn.getlimit(sqlite3.SQLITE_LIMIT_VARIABLE_NUMBER))
    for offset in range(0, len(identities), limit):
        chunk = identities[offset:offset + limit]
        placeholders = ",".join("?" for _ in chunk)
        rows = conn.execute(
            f"SELECT {daily._EVIDENCE_COLUMNS} FROM event_evidence AS ev "
            f"WHERE ev.rowid IN ({placeholders})",
            tuple(row[2] for row in chunk),
        )
        payloads.extend(_history_evidence_dict(row) for row in rows)
    payloads.sort(key=daily._ingest_order)
    return payloads


def _weekly_plain_text(value: object) -> str:
    """Skip markup work only when neither markup nor entities can be present."""
    if isinstance(value, str) and "<" not in value and "&" not in value:
        # split/join uses the same Unicode whitespace set as the shared
        # helper's \s+ replacement and strip, including its NBSP conversion.
        return " ".join(value.split())
    return daily.plain_text_from_markup(value)


def _history_evidence_dict(row: sqlite3.Row) -> dict[str, Any]:
    """The shared evidence projection, with a pure-text history fast path."""
    announcement = row["announcement_at"]
    return {
        "evidence_id": str(row["evidence_id"]),
        "event_id": str(row["event_id"]),
        "source_uri": str(row["source_uri"]),
        "source_name": str(row["source_name"] or ""),
        "claim_text": _weekly_plain_text(row["claim_text"])[:1000],
        "stance": str(row["stance"]),
        "announcement_at": None if announcement is None else int(announcement),
        "observed_at": int(row["observed_at"]),
    }


def _weekly_headline_key_text(text: object) -> str:
    """Keep full shared normalization, avoiding redundant pure-text parsing."""
    value = unicodedata.normalize("NFKC", _weekly_plain_text(text or ""))
    if any(sign in value for sign in daily._SIGN_TRANSLATION_CHARS):
        value = value.translate(daily._SIGN_TRANSLATION)
    return value.casefold()


def _history_correction_kind(row: dict[str, Any]) -> str:
    """Reject only cue-free historical text before invoking the shared rules."""
    if row["stance"] == "contradicts":
        return "denial"
    # One escaped literal union avoids scanning a long, unrelated headline
    # once for every cue. Positive cases still use the exact shared rules.
    if not _HISTORY_CORRECTION_CUE_RE.search(row["claim_text"]):
        return ""
    return daily._correction_kind(row)


def _classify_week(
    group: dict[str, Any], lookback: dict, *, headline_keys: set[str] | None = None,
) -> dict[str, Any]:
    """Compute weekly semantics once, retaining every historical evidence row."""
    if headline_keys is None:
        headline_keys = daily._matching_headline_keys(group)
    matched = [row for key in headline_keys if key in lookback for row in lookback[key]]
    history = sorted(
        {row["evidence_id"]: row for row in group["prior"] + matched}.values(),
        key=daily._ingest_order,
    )
    today_keys = [(row, daily._semantic_key(row)) for row in group["today"]]
    # Only membership of today's keys is needed. Keep historical corrections
    # and every evidence row, but do not fully tokenize an unrelated long
    # historical headline after its tokens have already ruled out a match.
    history_kinds, known = _weekly_history_keys(history, {key for _row, key in today_keys})
    fresh = [(row, key) for row, key in today_keys if key not in known]
    # A later repost does not make an earlier denial the newest change again.
    # Keep first occurrence of each new headline, while source views retain
    # all observed links and publication/ingestion times.
    # A contradicts stance or an explicit clarification is a semantic change
    # even if punctuation-insensitive headline identity matches prior support.
    first: dict[tuple[str, str], tuple[dict[str, Any], tuple[str, str]]] = {}
    for row, key in sorted(fresh, key=lambda pair: daily._ingest_order(pair[0])):
        first.setdefault(key, (row, key))
    corrections = [row for row, key in first.values() if key[1]]
    denials = [row for row, key in first.values() if key[1] == "denial"]
    history_ids = {row["evidence_id"] for row in history}
    cutoff = denials[-1]["observed_at"] if denials else None
    state = {
        "history": history,
        "fresh": [row for row, _key in first.values()],
        "corrections": corrections,
        "denials": denials,
        # Keep raw fresh support, including reposts, before the strict denial
        # cutoff. With no new denial preserve daily's history-ID membership.
        "prior_support": sorted(
            [row for row, kind in history_kinds + [(row, key[1]) for row, key in fresh]
             if row["stance"] == "supports" and not kind
             and (row["observed_at"] < cutoff if cutoff is not None
                  else row["evidence_id"] in history_ids)],
            key=daily._ingest_order,
        ),
        "background_corrections": sorted(
            [row for row, kind in history_kinds if kind == "denial"]
            + [row for row, key in today_keys if key in known and key[1] == "denial"],
            key=daily._ingest_order,
        ),
        "pending_language": any(
            daily._PENDING_RE.search(row["claim_text"]) for row, _key in first.values()
        ),
    }
    if state["corrections"]:
        state["block"] = "correction"
    elif not state["fresh"]:
        state["block"] = "repeat"
    elif state["pending_language"]:
        state["block"] = "pending"
    else:
        state["block"] = "advanced" if state["history"] else "new"
    return state


def _weekly_history_keys(history: list[dict[str, Any]], targets: set[tuple[str, str]]) -> tuple[list, set]:
    """Exact membership, with bounded token tries for small current groups.

    The trie is an algorithm choice, not an evidence/read limit. Large target
    sets use the same full normalizer as before. Empty keys and correction
    kinds remain distinct; all historical kinds are retained for card state.
    """
    kinds = [(row, _history_correction_kind(row)) for row in history]
    if len(targets) > 64:
        return kinds, {
            key for row, kind in kinds
            if (key := (daily._normalized_key(row["claim_text"]), kind)) in targets
        }
    tries: dict[str, dict] = {}
    for key in targets:
        node = tries.setdefault(key[1], {})
        for token in key[0].split() if key[0] else ():
            node = node.setdefault(token, {})
        node[None] = key
    known = set()
    for row, kind in kinds:
        if kind not in tries:
            continue
        node = tries[kind]
        for token in daily._KEY_TOKEN_RE.finditer(_weekly_headline_key_text(row["claim_text"])):
            node = node.get(token.group())
            if node is None:
                break
        else:
            if None in node:
                known.add(node[None])
    return kinds, known


def _day_coverage(
    conn: sqlite3.Connection, window: dict[str, Any], rows: list[dict[str, Any]],
    truncated: bool,
) -> list[dict[str, Any]]:
    """Check each elapsed day even when the bounded evidence read was cut off."""
    counts = Counter(daily._clock_label(row["observed_at"], "%Y-%m-%d") for row in rows)
    days = []
    for lower in range(window["start"], window["cutoff"] + 1, 86_400):
        upper = min(lower + 86_399, window["cutoff"])
        label = daily._clock_label(lower, "%Y-%m-%d")
        # A complete read already answers this with the same time window and
        # public-evidence predicate. Only a capped read needs separate probes.
        exists = bool(counts[label]) if not truncated else bool(conn.execute(
            "SELECT EXISTS(SELECT 1 FROM event_evidence AS ev "
            "INDEXED BY idx_event_evidence_unbound_observed "
            "WHERE ev.mapping_id IS NULL AND ev.observed_at >= ? AND ev.observed_at <= ? "
            f"AND {daily._public_cache_evidence_predicate('ev')} LIMIT 1)",
            (lower, upper, *daily._PUBLIC_SCORE_PATH_VALUES),
        ).fetchone()[0])
        days.append({
            "date": label,
            "evidence_count": None if truncated else counts[label],
            "has_evidence": exists,
            "partial": upper < lower + 86_399,
        })
    return days


def _data_state(
    coverage: dict[str, Any], window: dict[str, Any], evidence_count: int,
    missing_dates: list[str], truncated: bool, lookback_truncated: bool,
    history_truncated: bool,
) -> tuple[str, str]:
    first = coverage["first_at"]
    if first is None or first > window["cutoff"]:
        return "before_coverage", "证据不足：所选周早于事件数据覆盖起点，不代表没有发生事件"
    if not evidence_count:
        return "no_ingestion", "证据不足：本周截至截止时间没有公开入库记录，不代表没有发生事件"
    if truncated or lookback_truncated or history_truncated:
        parts = []
        if truncated:
            parts.append(f"本周证据超过读取上限 {MAX_WEEK_EVIDENCE_ROWS} 条")
        if lookback_truncated:
            parts.append("周前 7 天对照记录超过读取上限，无法完整判断重复报道")
        if history_truncated:
            parts.append("已关联事件的历史记录超过读取上限")
        return "truncated", "证据不足：" + "；".join(parts) + "；周报不完整"
    if missing_dates:
        return "incomplete", (
            "证据不足：以下已结束日期没有公开入库记录：" + "、".join(missing_dates)
            + "；可能未采集或未通过公开路径，不代表当天没有事件"
        )
    if first > window["start"]:
        return "incomplete", (
            f"证据不足：事件数据覆盖始于 {daily._clock_label(first)}（北京时间），未覆盖完整周初"
        )
    latest = coverage["latest_at"]
    if latest is not None and window["cutoff"] - latest > daily.STALE_AFTER_SECONDS:
        return "stale", (
            f"证据不足：最近一次入库为 {daily._clock_label(latest)}（北京时间），之后的变化未覆盖"
        )
    return "ok", ""


def _header(week_start: date, window: dict[str, Any], now: int) -> dict[str, Any]:
    earliest, current = _week_bounds(now)
    week_end = week_start + timedelta(days=6)
    cutoff = daily._clock_label(window["cutoff"], "%Y-%m-%d %H:%M:%S")
    historical = week_start != current
    return {
        "week": week_start.isoformat(),
        "start_date": week_start.isoformat(),
        "end_date": week_end.isoformat(),
        "prev_week": (week_start - timedelta(days=7)).isoformat() if week_start > earliest else "",
        "next_week": (week_start + timedelta(days=7)).isoformat() if week_start < current else "",
        "min_week": earliest.isoformat(),
        "max_week": current.isoformat(),
        "cutoff": window["cutoff"],
        "cutoff_label": f"截止 {cutoff}（北京时间" + ("，本周未结束）" if window["partial"] else "）"),
        "generated_at": int(now),
        "generated_at_label": f"生成于 {daily._clock_label(now, '%Y-%m-%d %H:%M:%S')}（北京时间）",
        "is_historical": historical,
        "is_current_week": not historical,
        "partial": window["partial"],
        "historical_note": (
            f"{HISTORICAL_VIEW_LABEL}：来源与合并关系按截止时的入库与合并时间还原；"
            "事件标题与事件评分为当前值，未保存当周版本"
        ) if historical else "事件标题与事件评分为当前值；本周视图尚未结束",
        "coverage_label": (
            "覆盖范围：北京时间周一至周日，按入库时间读取已通过公开评分路径的 RSS 原文标题；"
            "整周归并后统一展开，不拼接每日榜单；未入库来源不在范围内"
        ),
        "notes": (
            "仅按规范化标题完全一致关联；事件身份为原文链接，不做跨标题关联。",
            "每个事件只归入一个板块；同周同标题转载不增加内容变化数，报道数量不代表重要程度。",
            "本周变化相对周开始前的记录判断；跨事件同标题对照回看周前 7 天，已关联事件另读历史。",
            "周前已出现过的否认/更正只作背景；本周同标题否认转载不重复计作新变化。",
            "逐日覆盖仅确认当天存在公开入库记录，不代表覆盖所有事件或排除日内采集中断。",
            "来源立场为采集默认值；否认/更正与要求澄清均按标题措辞规则识别，未经人工或模型核验。",
            "来源发布方按媒体识别，同一媒体的多个订阅源只算 1 个；转载关系未核验。",
            "关注条目从已展开事件确定性选取，不是模型总结；原文点名公司与规则关联均不构成推荐。",
        ),
    }


def _highlights(blocks: list[dict[str, Any]]) -> list[dict[str, Any]]:
    shown = [item for block in blocks for item in block["entries"]]
    shown.sort(key=lambda item: (
        item["correction_kind"] != "denial", -item["priority"], -item["latest_at"], item["event_id"],
    ))
    return shown[:MAX_HIGHLIGHTS]


def _check_deadline(deadline: float) -> None:
    if time.monotonic() >= deadline:
        raise sqlite3.OperationalError("weekly report read deadline exceeded")


def _materialize_blocks(
    conn: sqlite3.Connection, blocks: list[dict[str, Any]],
    candidates: dict[str, tuple[dict[str, Any], dict[str, Any]]],
    members: dict[str, set[str]], cutoff: int, deadline: float,
) -> None:
    """Load display-only associations and source details for shown cards only."""
    shown = [item for block in blocks for item in block["entries"]]
    shown_members = {
        member for item in shown
        for root in candidates[item["event_id"]][0]["root_ids"]
        for member in members[root]
    }
    associations = daily._load_associations(conn, shown_members, cutoff)
    known_names = daily._load_security_names(conn) if associations else frozenset()
    for block in blocks:
        views = []
        for selected in block["entries"]:
            _check_deadline(deadline)
            group, state = candidates[selected["event_id"]]
            group["associations"] = [
                row for root in group["root_ids"] for member in members[root]
                for row in associations.get(member, [])
            ]
            group["association_texts"] = [row["claim_text"] for row in group["today"]]
            main_corrections = state["denials"] or state["corrections"]
            primary = main_corrections[-1]["evidence_id"] if main_corrections else ""
            item = daily._item_view(
                group, state, known_names, period_label="本周", primary_source_id=primary,
            )
            _check_deadline(deadline)
            item["unique_change_count"] = len(state["fresh"])
            views.append(item)
        block["entries"] = views


def _page_blocks(blocks: list[dict[str, Any]], week: str, selected: str | None) -> list[dict[str, Any]]:
    """Select a bounded reading page before loading associations and sources."""
    if selected is not None:
        blocks = [block for block in blocks if block["key"] == selected]
    for block in blocks:
        paging = block["pagination"]
        if paging["page"] > paging["page_count"]:
            raise EventRadarQueryError("weekly report page no longer exists")
        for direction, step in (("previous_url", -1), ("next_url", 1)):
            page = paging["page"] + step
            paging[direction] = (
                "/dashboard/events/weekly?" + urlencode({
                    "week": week, "block": block["key"], "page": page,
                })
                if 1 <= page <= paging["page_count"] else ""
            )
    return blocks


def _build_weekly_report(
    conn: sqlite3.Connection, week_start: date, now: int, deadline: float,
    *, block: str | None = None, page: int = 1,
) -> dict[str, Any]:
    week_start = parse_report_week(week_start.isoformat(), now=now)
    window = report_window(week_start, now)
    coverage = daily._coverage(conn, window["cutoff"])
    rows, truncated = daily._load_window_evidence(
        conn, window["start"], window["cutoff"], MAX_WEEK_EVIDENCE_ROWS,
    )
    lookback_rows, lookback_truncated = daily._load_window_evidence(
        conn, window["start"] - daily.REPEAT_LOOKBACK_SECONDS, window["start"] - 1,
        MAX_LOOKBACK_EVIDENCE_ROWS,
    )
    roots = daily._resolve_roots(conn, {row["event_id"] for row in rows}, window["cutoff"])
    members = daily._load_members(conn, set(roots.values()), window["cutoff"])
    all_members = {member for group in members.values() for member in group}
    history, history_truncated = _linked_history(
        conn, all_members, window["start"],
        lookback_rows=None if lookback_truncated else lookback_rows,
    )
    groups = daily._build_groups(
        rows, roots, members, history, daily._load_event_rows(conn, set(roots.values())),
        {},
    )
    groups, folded = daily._merge_duplicate_groups(groups)
    group_keys = [daily._matching_headline_keys(group) for group in groups]
    headline_keys = {key for keys in group_keys for key in keys}
    lookback = daily._lookback_index(lookback_rows, headline_keys=headline_keys)
    items, repeats, repeated_rows = [], 0, 0
    candidates = {}
    for group, keys in zip(groups, group_keys):
        _check_deadline(deadline)
        state = _classify_week(group, lookback, headline_keys=keys)
        _check_deadline(deadline)
        repeated_rows += len(group["today"]) - len(state["fresh"])
        if state["block"] == "repeat":
            repeats += 1
            continue
        event_id = group["root_ids"][0]
        candidates[event_id] = group, state
        # The same block policy and sorting operate on cheap selection fields.
        # Full evidence stays attached until the final visible cards are known.
        items.append({
            "event_id": event_id,
            "block": state["block"],
            "correction_kind": (
                "denial" if state["denials"] else "request" if state["corrections"] else ""
            ),
            "priority": group["priority"],
            "latest_at": max(row["observed_at"] for row in group["today"]),
        })
    days = _day_coverage(conn, window, rows, truncated)
    missing_dates = [day["date"] for day in days if not day["has_evidence"] and not day["partial"]]
    state, label = _data_state(
        coverage, window, len(rows), missing_dates, truncated, lookback_truncated, history_truncated,
    )
    blocks = daily._assemble_blocks(
        items, state, label, block_pages={block: page} if block else {},
    )
    blocks = _page_blocks(blocks, week_start.isoformat(), block)
    _materialize_blocks(conn, blocks, candidates, members, window["cutoff"], deadline)
    return {
        **_header(week_start, window, now),
        "data_state": state,
        "data_state_label": label,
        "freshness": daily._freshness(coverage, window),
        "coverage_days": days,
        "missing_dates": missing_dates,
        "coverage_complete": state == "ok" and not window["partial"],
        "blocks": blocks,
        "highlights": [] if block else _highlights(blocks),
        "highlights_label": "本周关注条目（优先否认/更正，其余按已有评分与入库时间选取）",
        "evidence_count": len(rows),
        "event_group_count": len(groups),
        "duplicate_groups_folded": folded,
        "repeat_event_count": repeats,
        "repeat_evidence_count": repeated_rows,
        "display_min_priority": daily.DISPLAY_MIN_PRIORITY,
        "pagination_block": block,
        "overview_url": "/dashboard/events/weekly?" + urlencode({"week": week_start.isoformat()}),
        "pagination_notice": "每次翻页按当前已有记录重建，排序可能变化；这不是固定快照。",
    }


def build_weekly_report(
    conn: sqlite3.Connection, week_start: date, now: int, *, block=None, page=None,
) -> dict[str, Any]:
    """Apply a SQL/phase time budget, not a hard deadline inside Python loops."""
    try:
        block, page = parse_weekly_paging(block, page)
    except ValueError as exc:
        raise EventRadarQueryError("invalid weekly report paging") from exc
    deadline = time.monotonic() + MAX_REPORT_SECONDS
    conn.set_progress_handler(lambda: int(time.monotonic() >= deadline), 1000)
    try:
        with daily._report_text_cache():
            result = _build_weekly_report(conn, week_start, now, deadline, block=block, page=page)
        _check_deadline(deadline)
        return result
    finally:
        conn.set_progress_handler(None, 0)


def clear_weekly_cache() -> None:
    _REPORT_CACHE.clear()


def _compute_weekly_report(week_start: date, block, page, now: int | None) -> dict[str, Any]:
    """Read one fresh report; ``now`` is taken at build time unless fixed."""
    current = int(time.time()) if now is None else int(now)
    try:
        with get_read_only_connection() as conn:
            conn.execute("BEGIN")
            try:
                return build_weekly_report(conn, week_start, current, block=block, page=page)
            finally:
                if conn.in_transaction:
                    conn.rollback()
    except sqlite3.Error as exc:
        logger.warning("Event weekly report read failed: %s", type(exc).__name__)
        raise EventRadarUnavailableError("event weekly report is unavailable") from exc


_MARKER_SQL = (
    "SELECT (SELECT COALESCE(MAX(rowid), 0) FROM event_securities), "
    "(SELECT COALESCE(MAX(merge_id), 0) FROM event_merge_history)"
)
_CURRENT_MARKER_SQL = _MARKER_SQL + (
    ", (SELECT COALESCE(MAX(rowid), 0) FROM event_evidence), "
    "(SELECT COALESCE(MAX(rowid), 0) FROM events), "
    "(SELECT COALESCE(MAX(observed_at), 0) FROM event_evidence "
    "INDEXED BY idx_event_evidence_unbound_observed WHERE mapping_id IS NULL)"
)


def _data_marker(current_week: bool) -> tuple[int, ...]:
    """Cheap append-only high-water marks read before any build.

    A completed week only changes through later mapping/merge rows, which are
    always inserted. The current week also follows new evidence and events.
    """
    try:
        with get_read_only_connection() as conn:
            row = conn.execute(_CURRENT_MARKER_SQL if current_week else _MARKER_SQL).fetchone()
    except sqlite3.Error as exc:
        logger.warning("Event weekly report marker failed: %s", type(exc).__name__)
        raise EventRadarUnavailableError("event weekly report is unavailable") from exc
    return tuple(int(value) for value in row)


def load_weekly_report(
    week_value: object = None, *, now: int | None = None, block=None, page=None,
) -> dict[str, Any]:
    current = int(time.time()) if now is None else int(now)
    week_start = parse_report_week(week_value, now=current)
    try:
        selected, number = parse_weekly_paging(block, page)
        if selected is not None and week_value is None:
            raise ValueError("paging requires an explicit week")
    except ValueError as exc:
        raise EventRadarQueryError("invalid weekly report paging") from exc
    page = number if selected else None
    if now is not None:
        # A caller-fixed clock is a deterministic reconstruction: never cached.
        return _compute_weekly_report(week_start, selected, page, now)
    is_current = week_start == _week_bounds(current)[1]
    key = (
        database.DB_PATH, week_start.isoformat(), selected, page,
        datetime.fromtimestamp(current, daily.REPORT_TZ).date().isoformat(),
    )
    return _REPORT_CACHE.get(
        key,
        ttl=CURRENT_WEEK_CACHE_SECONDS if is_current else PAST_WEEK_CACHE_SECONDS,
        marker=_data_marker(is_current),
        build=lambda: _compute_weekly_report(week_start, selected, page, None),
    )


__all__ = [
    "build_weekly_report", "clear_weekly_cache", "load_weekly_report", "parse_report_week",
]
