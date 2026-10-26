"""Whole-report equivalence to the pre-optimization implementation.

The readable function snapshots below were taken from 657fae6 (the local
baseline tree matching PR #231 head 292726bb). They are checked in here so
CI needs neither that local commit nor git. Only unchanged dependencies are
borrowed, with their function globals rebound into isolated oracle modules.
Changed classification, source rendering, publisher aggregation, association
matching, history loading and report assembly execute the frozen code.
"""

from contextvars import ContextVar
from datetime import date, datetime
from types import FunctionType, ModuleType
from uuid import NAMESPACE_URL, uuid5
from zoneinfo import ZoneInfo

import pytest

from services import event_daily_report as daily
from services import event_weekly_report as weekly
from services import provenance_shadow as provenance


# Publisher resolution was split into prepared-input helpers by this change.
# Keep the old resolver here as well: borrowing its new public entry point
# would otherwise let a shared regression agree in both whole-report outputs.
_FROZEN_PUBLISHER = r'''
def resolve_publisher(
    source_uri: object,
    source_name: object = "",
    *,
    config: Mapping[str, object] | None = None,
) -> dict[str, object]:
    """Resolve one publisher without DNS, HTTP, a model, or mutable state."""
    registry = _normalize_registry(config)
    host = _uri_host(source_uri)
    domain_matches = [
        (domain, metadata)
        for domain, metadata in registry["domains"].items()
        if host == domain or host.endswith("." + domain)
    ]
    if domain_matches:
        _match, metadata = max(domain_matches, key=lambda item: (len(item[0]), item[0]))
        group, source_class, scopes = metadata
        return {
            "publisher_group": group,
            "publisher_basis": "registry_domain",
            "source_class": source_class,
            "first_party_scopes": list(scopes),
        }

    route_matches: list[tuple[str, tuple[str, str, tuple[str, ...]]]] = []
    for path in _trusted_route_candidates(source_uri, source_name):
        route_matches.extend(
            (route, metadata)
            for route, metadata in registry["routes"].items()
            if _path_matches(path, route)
        )
    if route_matches:
        _match, metadata = max(route_matches, key=lambda item: (len(item[0]), item[0]))
        group, source_class, scopes = metadata
        return {
            "publisher_group": group,
            "publisher_basis": "registry_route",
            "source_class": source_class,
            "first_party_scopes": list(scopes),
        }

    publisher_domain = _registrable_domain(host)
    if publisher_domain:
        domain_group = f"domain:{publisher_domain}"
        if len(domain_group) > 128:
            domain_group = "domainhash:" + _fingerprint({"domain": publisher_domain})
        return {
            "publisher_group": domain_group,
            "publisher_basis": "registrable_domain",
            "source_class": "unknown",
            "first_party_scopes": [],
        }
    return {
        "publisher_group": "unknown",
        "publisher_basis": "unknown",
        "source_class": "unknown",
        "first_party_scopes": [],
    }
'''


# Frozen daily functions deliberately retain their original repeated work.
_FROZEN_DAILY = r'''
def _evidence_dict(row: sqlite3.Row) -> dict[str, Any]:
    announcement = row["announcement_at"]
    return {
        "evidence_id": str(row["evidence_id"]),
        "event_id": str(row["event_id"]),
        "source_uri": str(row["source_uri"]),
        "source_name": str(row["source_name"] or ""),
        "claim_text": plain_text_from_markup(row["claim_text"])[:1000],
        "stance": str(row["stance"]),
        "announcement_at": None if announcement is None else int(announcement),
        "observed_at": int(row["observed_at"]),
    }


def _publisher_url(value: object) -> str:
    raw = str(value or "").strip()
    if any(ord(char) < 32 or ord(char) == 127 for char in raw):
        return ""
    try:
        parts = urlsplit(raw)
        if (parts.scheme not in {"http", "https"} or not parts.hostname
                or parts.username is not None or parts.password is not None):
            return ""
        if any(segment in {".", ".."} for segment in unquote(parts.path).split("/")):
            return ""
        _ = parts.port
    except ValueError:
        return ""
    return raw


def _normalize_headline(text: object) -> str:
    """Width/case/punctuation-insensitive headline identity, numbers intact."""
    value = unicodedata.normalize("NFKC", plain_text_from_markup(text or ""))
    value = value.translate(_SIGN_TRANSLATION).casefold()
    return " ".join(_KEY_TOKEN_RE.findall(value))


def _headline_correction_kind(claim_text: str, stance: str) -> str:
    if stance == "contradicts":
        return "denial"
    # Keep an actual company denial in an adjacent clause even when the
    # headline also mentions the regulator's preceding request.
    kinds = {
        _clause_correction_kind(clause)
        for clause in re.split(r"[，,。；;！？!?]", claim_text)
    }
    return "denial" if "denial" in kinds else "request" if "request" in kinds else ""


@contextmanager
def _report_text_cache():
    """Bound pure headline work to one request, never cache rows or DB results.

    Separate context-local caches avoid changing freshness or sharing mutable
    state between simultaneous reports. The stance is part of the lexical
    classification key; equal text with different evidence stance stays apart.
    """
    caches = (
        lru_cache(maxsize=_TEXT_CACHE_SIZE)(_normalize_headline),
        lru_cache(maxsize=_TEXT_CACHE_SIZE)(_headline_correction_kind),
    )
    token = _REPORT_TEXT_CACHE.set(caches)
    try:
        yield
    finally:
        _REPORT_TEXT_CACHE.reset(token)
        for cache in caches:
            cache.cache_clear()


def _outlet_key(item: dict[str, Any]) -> str:
    """Media outlet behind one record, or "" when it cannot be determined.

    Several feeds of one outlet (e.g. three wallstreetcn RSSHub routes) are
    one publisher: known outlets resolve through the shared provider table,
    other article links through their registrable domain.  A feed route
    only names an outlet when it matches a registered, trusted internal route.
    """
    identity = resolve_publisher(
        _publisher_url(item["source_uri"]), _publisher_url(item["source_name"]),
        config=_PUBLISHER_REGISTRY,
    )
    key = str(identity["publisher_group"])
    return "" if key == "unknown" else key


def _publisher_summary(items: list[dict[str, Any]]) -> tuple[int, int]:
    """(identified outlets, distinct links whose outlet is unknown)."""
    identified = [(_outlet_key(item), item["source_uri"]) for item in items]
    outlets = {key for key, _url in identified}
    unknown_links = {url for key, url in identified if not key}
    return len(outlets - {""}), len(unknown_links)


def _classify(
    group: dict[str, Any],
    lookback: dict[str, tuple[dict[str, Any], ...]],
    *, headline_keys: set[str] | None = None,
) -> dict[str, Any]:
    """Assign exactly one block from content that first appeared today."""
    today, prior = group["today"], group["prior"]
    if headline_keys is None:
        headline_keys = _matching_headline_keys(group)
    matched = [
        row for key in headline_keys
        if key in lookback for row in lookback[key]
    ]
    # An endpoint may already be present in this root's history. Do not count
    # it twice, but retain later observations of the same title in other roots.
    history = sorted(
        {item["evidence_id"]: item for item in prior + matched}.values(), key=_ingest_order
    )
    known = {_semantic_key(item) for item in history}
    fresh = [item for item in today if _semantic_key(item) not in known]
    fresh_ids = {item["evidence_id"] for item in fresh}
    history_ids = {item["evidence_id"] for item in history}
    reposted = [item for item in today if item["evidence_id"] not in fresh_ids]
    corrections = sorted(
        (item for item in fresh if _correction_kind(item)), key=_ingest_order
    )
    denials = [item for item in corrections if _correction_kind(item) == "denial"]
    # Support recorded before the latest denial, including earlier the same
    # day, is the original report that the denial answers.
    support_cutoff = denials[-1]["observed_at"] if denials else None
    prior_support = [
        item for item in history + fresh
        if item["stance"] == "supports" and not _correction_kind(item)
        and (item["evidence_id"] in history_ids or (
            support_cutoff is not None and item["observed_at"] < support_cutoff
        ))
    ]
    state = {
        "history": history,
        "fresh": fresh,
        "corrections": corrections,
        "denials": denials,
        # Corrections already seen before today are background, not news.
        "background_corrections": [
            item for item in history + reposted if _correction_kind(item) == "denial"
        ],
        "prior_support": sorted(prior_support, key=_ingest_order),
        "pending_language": any(_PENDING_RE.search(item["claim_text"]) for item in fresh),
    }
    if corrections:
        state["block"] = "correction"
    elif not fresh:
        state["block"] = "repeat"
    elif state["pending_language"]:
        state["block"] = "pending"
    else:
        state["block"] = "advanced" if history else "new"
    return state


def _source_views(
    items: list[dict[str, Any]], history: list[dict[str, Any]],
    *, primary_source_id: str = "",
) -> tuple[list[dict[str, Any]], int]:
    """One row per link, corrections first, then newest.

    For a link observed more than once the correction (or else the newest
    record) represents it, and its earlier differing headline is kept as the
    same-link change instead of hiding the correction behind it.
    """
    by_url: dict[str, list[dict[str, Any]]] = {}
    for item in items:
        by_url.setdefault(item["source_uri"], []).append(item)
    def rank(item):
        return (item["evidence_id"] != primary_source_id, *_source_rank(item))

    chosen = [min(records, key=rank) for records in by_url.values()]
    chosen.sort(key=rank)
    views = []
    for item in chosen[:MAX_SOURCES_PER_ITEM]:
        earlier = [
            row for row in by_url[item["source_uri"]] + history
            if row["source_uri"] == item["source_uri"]
            and row["observed_at"] <= item["observed_at"]
            and _normalized_key(row["claim_text"]) != _normalized_key(item["claim_text"])
        ]
        views.append({
            "url": _safe_external_url(item["source_uri"]),
            "source_name": item["source_name"],
            "claim_text": item["claim_text"],
            "stance": item["stance"],
            "correction_kind": _correction_kind(item),
            "outlet_known": bool(_outlet_key(item)),
            "same_link_previous": earlier[-1]["claim_text"] if earlier else "",
            "published_label": _published_label(item),
            "observed_label": f"入库 {_clock_label(item['observed_at'])}",
        })
    return views, max(0, len(chosen) - MAX_SOURCES_PER_ITEM)


def _is_named(name: str, texts: list[str], known_names: frozenset[str]) -> bool:
    """Whole-name mention that is not part of a longer known term.

    Uses the write path's entity check (e.g. "数字人" in "数字人民币" is a
    currency, not the issuer) and rejects occurrences covered by a longer
    known security name (e.g. "平安" inside "平安银行").
    """
    target = _compact(name)
    if not target:
        return False
    longer = [known for known in known_names if len(known) > len(target) and target in known]
    for text in texts:
        compact = _compact(text)
        if target not in compact or not is_direct_security_mention(text, name):
            continue
        covered = [
            (match.start(), match.end()) for known in longer
            for match in re.finditer(re.escape(known), compact)
        ]
        for match in re.finditer(re.escape(target), compact):
            if not any(a <= match.start() and match.end() <= b for a, b in covered):
                return True
    return False


def _association_view(group: dict[str, Any], known_names: frozenset[str]) -> dict[str, Any]:
    """Keep title-named companies apart from rule-pool theme associations."""
    texts = group.get(
        "association_texts", [group["title"]] + [item["claim_text"] for item in group["today"]],
    )
    companies: list[str] = []
    rule_ids: set[str] = set()
    for row in group["associations"]:
        name = row["name"].strip()
        relation = row["relation_type"]
        if relation in _COMPANY_RELATIONS and _is_named(name, texts, known_names):
            if name not in companies:
                companies.append(name)
        elif relation in _COMPANY_RELATIONS or relation in _RULE_RELATIONS:
            rule_ids.add(row["security_id"])
    company_ids = {
        row["security_id"] for row in group["associations"] if row["name"].strip() in companies
    }
    return {
        "companies": companies[:5],
        "rule_association_count": len(rule_ids - company_ids),
    }


def _item_view(
    group: dict[str, Any], state: dict[str, Any], known_names: frozenset[str],
    *, period_label: str = "今日", primary_source_id: str = "",
) -> dict[str, Any]:
    sources, more = _source_views(
        group["today"], state["history"], primary_source_id=primary_source_id,
    )
    identified, _unknown = _publisher_summary(group["today"] + state["history"])
    # The baseline is the most recent prior record; the earliest is shown
    # separately only when it differs.
    previous = state["history"][-1] if state["history"] else None
    earliest = state["history"][0] if len(state["history"]) > 1 else None
    return {
        "block": state["block"],
        "correction_kind": (
            "denial" if state["denials"] else "request" if state["corrections"] else ""
        ),
        "event_id": group["root_ids"][0],
        "title": group["title"],
        "event_type": group["event_type"],
        "priority": group["priority"],
        "priority_label": f"{group['priority']:.1f}",
        "change": _change_text(group, state, period_label=period_label),
        "previous": None if previous is None else {
            "claim_text": previous["claim_text"],
            "time_label": _clock_label(previous["observed_at"]),
        },
        "earliest": None if earliest is None else {
            "claim_text": earliest["claim_text"],
            "time_label": _clock_label(earliest["observed_at"]),
        },
        "sources": sources,
        "sources_more": more,
        "report_count": len(group["today"]),
        "merged_group_count": len(group["root_ids"]),
        "uncertainties": _uncertainties(group, state),
        "next_check": _next_check(state, identified),
        "latest_at": max(item["observed_at"] for item in group["today"]),
        **_association_view(group, known_names),
    }


def _uncertainties(group: dict[str, Any], state: dict[str, Any]) -> list[str]:
    notes = [_publisher_note(*_publisher_summary(group["today"] + state["history"]))]
    # Inside a denial, rumor wording describes the claim being denied.
    if state["pending_language"] and state["block"] != "correction":
        notes.append("标题含计划/传闻/预期措辞，事项尚未发生或未经确认")
    if state["block"] == "correction":
        notes.extend(_correction_notes(state))
    if state["background_corrections"]:
        when = _clock_label(state["background_corrections"][-1]["observed_at"])
        notes.append(f"此前已有否认/更正记录（{when}，仅作背景），新的报道不推翻该否认")
    return notes

'''

# Frozen weekly entry point calls the old daily module and old phase helpers.
_FROZEN_WEEKLY = r'''
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
    # Seek the linked event identities instead of scanning every older unbound
    # article. The event index also contains mapping evidence and is ordered by
    # announcement time, so retain both the unbound filter and explicit ingest
    # ordering. Honor the connection's real parameter limit and shared cap.
    available = conn.getlimit(sqlite3.SQLITE_LIMIT_VARIABLE_NUMBER) - len(daily._PUBLIC_SCORE_PATH_VALUES) - 2
    if available < 1:
        raise sqlite3.OperationalError("weekly report requires a larger SQLite variable limit")
    chunk_size = min(20_000, available)
    identities = sorted(members)
    for offset in range(0, len(identities), chunk_size):
        chunk = identities[offset:offset + chunk_size]
        placeholders = ",".join("?" for _ in chunk)
        rows = conn.execute(
            f"SELECT {daily._EVIDENCE_COLUMNS} FROM event_evidence AS ev "
            "INDEXED BY idx_event_evidence_event_time "
            f"WHERE ev.event_id IN ({placeholders}) AND ev.mapping_id IS NULL "
            "AND ev.observed_at < ? "
            f"AND {daily._public_cache_evidence_predicate('ev')} "
            "ORDER BY ev.observed_at, ev.evidence_id LIMIT ?",
            (*chunk, older_before, *daily._PUBLIC_SCORE_PATH_VALUES, remaining + 1),
        ).fetchall()
        for row in rows[:remaining]:
            item = daily._evidence_dict(row)
            result.setdefault(item["event_id"], []).append(item)
        if len(rows) > remaining:
            return result, True
        remaining -= len(rows)
    recent = sorted(
        [row for row in lookback_rows or [] if row["event_id"] in members],
        key=daily._ingest_order,
    )
    for row in recent[:remaining]:
        result.setdefault(row["event_id"], []).append(row)
    return result, len(recent) > remaining


def _classify_week(group: dict[str, Any], lookback: dict) -> dict[str, Any]:
    state = daily._classify(group, lookback)
    # A later repost does not make an earlier denial the newest change again.
    # Keep first occurrence of each new headline, while source views retain
    # all observed links and publication/ingestion times.
    # A contradicts stance or an explicit clarification is a semantic change
    # even if punctuation-insensitive headline identity matches prior support.
    known = {daily._semantic_key(row) for row in state["history"]}
    fresh = [row for row in group["today"] if daily._semantic_key(row) not in known]
    first: dict[tuple[str, str], dict[str, Any]] = {}
    for row in sorted(fresh, key=daily._ingest_order):
        first.setdefault(daily._semantic_key(row), row)
    state["fresh"] = list(first.values())
    state["corrections"] = [row for row in state["fresh"] if daily._correction_kind(row)]
    state["denials"] = [
        row for row in state["corrections"] if daily._correction_kind(row) == "denial"
    ]
    if state["denials"]:
        cutoff = state["denials"][-1]["observed_at"]
        state["prior_support"] = sorted(
            [row for row in state["history"] + fresh
             if row["stance"] == "supports" and not daily._correction_kind(row)
             and row["observed_at"] < cutoff],
            key=daily._ingest_order,
        )
    state["background_corrections"] = sorted(
        [row for row in state["history"] + group["today"]
         if daily._semantic_key(row) in known and daily._correction_kind(row) == "denial"],
        key=daily._ingest_order,
    )
    state["pending_language"] = any(daily._PENDING_RE.search(row["claim_text"]) for row in state["fresh"])
    if state["corrections"]:
        state["block"] = "correction"
    elif not state["fresh"]:
        state["block"] = "repeat"
    elif state["pending_language"]:
        state["block"] = "pending"
    else:
        state["block"] = "advanced" if state["history"] else "new"
    return state


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
    lookback = daily._lookback_index(lookback_rows)
    items, repeats, repeated_rows = [], 0, 0
    candidates = {}
    for group in groups:
        _check_deadline(deadline)
        state = _classify_week(group, lookback)
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

'''


def _isolated_module(original):
    """Rebind unchanged local functions so they see frozen sibling helpers."""
    isolated = ModuleType("oracle_" + original.__name__)
    isolated.__dict__.update(vars(original))
    for name, value in vars(original).items():
        if isinstance(value, FunctionType) and value.__module__ == original.__name__:
            copied = FunctionType(
                value.__code__, isolated.__dict__, name,
                value.__defaults__, value.__closure__,
            )
            copied.__kwdefaults__ = value.__kwdefaults__
            isolated.__dict__[name] = copied
    return isolated


@pytest.fixture
def old_report():
    old_provenance = _isolated_module(provenance)
    exec(compile(_FROZEN_PUBLISHER, "<frozen-publisher>", "exec"), old_provenance.__dict__)
    old_daily = _isolated_module(daily)
    old_daily.resolve_publisher = old_provenance.resolve_publisher
    old_daily._REPORT_TEXT_CACHE = ContextVar("oracle_event_report_text_cache", default=None)
    exec(compile(_FROZEN_DAILY, "<frozen-daily-report>", "exec"), old_daily.__dict__)
    old_weekly = _isolated_module(weekly)
    old_weekly.daily = old_daily
    exec(compile(_FROZEN_WEEKLY, "<frozen-weekly-report>", "exec"), old_weekly.__dict__)
    # Preserve the actual request budget in both entry points.
    assert old_weekly.MAX_REPORT_SECONDS == weekly.MAX_REPORT_SECONDS == 5.0
    assert old_weekly._classify_week.__globals__["daily"] is old_daily
    assert old_daily._item_view.__globals__["_source_views"] is old_daily._source_views
    return old_weekly


def _ts(value):
    return int(datetime.fromisoformat(value).replace(tzinfo=ZoneInfo("Asia/Shanghai")).timestamp())


def _population_writers(conn, ids):
    from db.event_core import resolve_or_create_event, upsert_event_evidence

    def event(key, title, when="2026-10-05T09:00:00", priority=7.0):
        at = _ts(when)
        value = resolve_or_create_event(
            conn, {"equivalence": key}, title=title, event_type="policy",
            priority=priority, occurred_at=at, now=at,
        )
        ids[key] = value
        return value

    def evidence(key, owner, title, when, *, url=None, feed=None,
                 stance="supports", announced="default", cache_path=None):
        at = _ts(when)
        uri = url or f"https://news.example/equivalence/{key}"
        value = upsert_event_evidence(
            conn, owner, evidence_id=str(uuid5(NAMESPACE_URL, key)), source_uri=uri,
            source_name=feed or "https://news.example/rss",
            claim_key="equivalence:" + key, claim_text=title, stance=stance,
            announcement_at=at - 60 if announced == "default" else announced,
            observed_at=at, now=at,
        )
        if cache_path is not None:
            conn.execute(
                "INSERT INTO news_cache (link, title, score_path) VALUES (?, ?, ?)",
                (uri, title, cache_path),
            )
        return value
    return event, evidence


def _seed_linked_history(event, evidence):
    # Linked history spans the 7-day lookback boundary, and includes
    # both old denial background and multiple supporting observations.
    main = event("main", "平安银行项目实施公告", "2026-09-20T09:00:00", 9.5)
    shared = "https://www.cls.cn/detail/weekly-same-link"
    evidence("history-support", main, "平安银行项目已经落地", "2026-09-20T09:00:00",
             url=shared, feed="http://rsshub:1200/cls/telegraph")
    evidence("history-denial", main, "平安银行否认项目金额为100亿元",
             "2026-09-22T10:00:00", url="https://wallstreetcn.com/articles/old",
             feed="http://rsshub:1200/wallstreetcn/news/global")
    evidence("history-boundary", main, "平安银行补充披露项目方案",
             "2026-09-28T00:00:00", announced=None)
    evidence("history-request", main, "交易所要求平安银行说明项目进展",
             "2026-10-01T10:00:00")
    evidence("history-latest", main, "平安银行项目审计结果发布",
             "2026-10-04T10:00:00", url=shared)
    evidence("main-support", main, "平安银行项目新增实施进展",
             "2026-10-05T10:00:00", url=shared, cache_path=" LLM ")
    evidence("main-request", main, "交易所要求平安银行澄清项目传闻",
             "2026-10-06T10:00:00", url="https://www.sse.com.cn/disclosure/request",
             feed="http://rsshub:1200/sse/disclosure")
    evidence("a-tied-denial", main, "平安银行否认项目投资100亿元",
             "2026-10-07T10:00:00", url=shared, stance="contradicts",
             announced=_ts("2026-10-07T12:00:00"))
    evidence("z-tied-correction", main, "更正：平安银行项目金额为80亿元",
             "2026-10-07T10:00:00", url=shared)
    evidence("main-final-denial", main, "平安银行澄清：未参与境外收购",
             "2026-10-08T10:00:00", url="https://wallstreetcn.com/articles/new",
             feed="http://127.0.0.1:1200/wallstreetcn/live", announced=None)
    # Six newer repost links must not hide the primary new correction.
    for index in range(6):
        evidence(
            f"repost-{index}", main, "平安银行否认项目投资100亿元",
            f"2026-10-10T10:00:0{index}",
            url=shared if index == 5 else f"https://reprint.example/articles/{index}",
        )
    return main


def _seed_semantic_blocks_and_numbers(event, evidence):
    # Equal normalized text with changed evidence stance remains a denial.
    semantic = event("semantic", "乙公司项目情况说明", "2026-10-03T10:00:00", 1.0)
    evidence("semantic-old", semantic, "乙公司项目情况说明", "2026-10-03T10:00:00")
    evidence("semantic-new", semantic, "乙公司项目情况说明", "2026-10-06T10:00:00",
             stance="contradicts")

    # A pre-week denial repost in another root is background, not news.
    prior = event("prior-denial", "丙公司否认获得订单", "2026-10-02T10:00:00")
    evidence("prior-denial-row", prior, "丙公司否认获得订单", "2026-10-02T10:00:00")
    repeat = event("repeat-denial", "丙公司否认获得订单", priority=1.0)
    evidence("repeat-denial-row", repeat, "丙公司否认获得订单", "2026-10-09T10:00:00")

    for key, title, priority in (
        ("request", "深交所要求丁公司澄清传闻", 8.0),
        ("low-request", "交易所要求戊公司澄清传闻", 1.0),
        ("low-news", "己公司发布普通产品", 1.0),
        ("pending", "庚公司计划建设新厂房", 8.5),
        ("appeal", "辛公司希望有关方面澄清市场传闻", 8.0),
        ("reply", "壬公司关于问询函的回复公告", 8.0),
        ("adjacent", "交易所要求癸公司澄清，癸公司否认收购传闻", 1.0),
    ):
        value = event(key, title, priority=priority)
        evidence(key + "-row", value, title, "2026-10-07T11:00:00")

    # Display-only folding; sign, decimals, units and comparators stay distinct.
    for key, title, when in (
        ("fold-a", "联合项目正式签约！", "2026-10-05T12:00:00"),
        ("fold-b", "联合项目正式签约", "2026-10-06T12:00:00"),
        ("number-le-old", "某公司净利润≤5亿元", "2026-10-04T12:00:00"),
        ("number-ge", "某公司净利润≥5亿元", "2026-10-05T12:00:00"),
        ("number-decimal", "某公司增长1.5%", "2026-10-06T12:00:00"),
        ("number-integer", "某公司增长15%", "2026-10-06T12:00:00"),
        ("number-negative", "某公司增长−5%", "2026-10-06T12:00:00"),
        ("number-positive", "某公司增长+5%", "2026-10-06T12:00:00"),
    ):
        value = event(key, title, when)
        evidence(key + "-row", value, title, when)


def _seed_publishers(event, evidence):
    # Known media collapse multiple RSS routes; internal unknown and mixed
    # sources exercise unknown-link notes and fallback publisher domains.
    for key, rows in (
        ("same-media", (
            ("https://wallstreetcn.com/articles/a", "http://rsshub:1200/wallstreetcn/news/global"),
            ("https://api.wallstreetcn.com/articles/b", "http://127.0.0.1:1200/wallstreetcn/live"),
            ("http://localhost:1200/wallstreetcn/news/china", "http://rsshub:1200/wallstreetcn/news/china"),
        )),
        ("unknown-media", (
            ("http://rsshub:1200/telegram/a", "http://rsshub:1200/telegram/a"),
            ("http://rsshub:1200/telegram/b", "https://rsshub.evil.example/wallstreetcn/live"),
        )),
        ("mixed-media", (
            ("https://www.cls.cn/detail/mixed", "http://rsshub:1200/cls/telegraph"),
            ("http://rsshub:1200/telegram/c", "http://rsshub:1200/telegram/c"),
            ("https://finance.sample.co.uk/news/a", "https://finance.sample.co.uk/rss"),
        )),
    ):
        value = event(key, key + "政策发布", priority=9.0)
        for index, (url, feed) in enumerate(rows):
            evidence(f"{key}-{index}", value, key + "政策发布",
                     f"2026-10-08T12:00:0{index}", url=url, feed=feed)


def _seed_merges_and_associations(conn, event, evidence, main):
    from db.event_core import merge_events, upsert_event_security, upsert_security

    # Merge membership is reconstructed by cutoff, including old evidence
    # and a child's mapping. A later merge must not rewrite this week.
    child = event("merged-child", "平安银行历史子事件", "2026-09-21T10:00:00")
    evidence("merged-child-old", child, "平安银行项目历史支持",
             "2026-09-21T10:00:00")
    evidence("merged-child-week", child, "平安银行数字人民币业务落地",
             "2026-10-05T11:00:00")
    separate = event("future-merge", "本周另一个独立项目", priority=8.0)
    evidence("future-merge-row", separate, "本周另一个独立项目", "2026-10-06T12:00:00")

    securities = {}
    for ticker, name in (
        ("000001", "平安银行"), ("000002", "平安"),
        ("000003", "数字人"), ("000004", "规则主题"),
        ("000005", "未来关联"), ("000006", "显示标题公司"),
    ):
        securities[name] = upsert_security(
            conn, exchange="SZSE", ticker=ticker, name=name,
            now=_ts("2026-09-20T09:00:00"),
        )
    for owner, name, relation, when in (
        (main, "平安银行", "direct", "2026-10-05T09:00:00"),
        (main, "平安", "direct", "2026-10-05T09:00:00"),
        (child, "数字人", "affected", "2026-10-05T09:00:00"),
        (main, "规则主题", "concept", "2026-10-05T09:00:00"),
        (main, "未来关联", "direct", "2026-10-12T09:00:00"),
        (separate, "显示标题公司", "direct", "2026-10-05T09:00:00"),
    ):
        upsert_event_security(
            conn, owner, securities[name], relation_type=relation,
            confidence=0.8, relevance=8.0, mapping_version="oracle-test-v1",
            now=_ts(when),
        )
    # Current display title alone must not create an original-name mention.
    conn.execute("UPDATE events SET title=? WHERE event_id=?",
                 ("显示标题公司披露事项", separate))
    merge_events(conn, child, main, reason="oracle-cutoff", now=_ts("2026-10-06T12:00:00"))
    merge_events(conn, separate, main, reason="oracle-after-cutoff",
                 now=_ts("2026-10-12T12:00:00"))


def _seed_page_overflow(event, evidence):
    # Populate all dates and exceed the real 20-card first page. Priority
    # keeps the specialized cases visible while ordinary cards spill.
    for index in range(24):
        title = f"本周普通政策第{index:02d}项"
        when = f"2026-10-{5 + index % 7:02d}T23:59:59"
        value = event(f"page-{index}", title, when, 7.0)
        evidence(f"page-{index}-row", value, title, when)


@pytest.fixture
def report_database(monkeypatch):
    import db.database as database
    from core.scorer import Scorer

    database.init_db()
    monkeypatch.setattr(
        Scorer, "__init__",
        lambda *_args, **_kwargs: pytest.fail("weekly equivalence must not invoke a model"),
    )
    ids = {}
    with database.get_connection() as conn:
        event, evidence = _population_writers(conn, ids)
        main = _seed_linked_history(event, evidence)
        _seed_semantic_blocks_and_numbers(event, evidence)
        _seed_publishers(event, evidence)
        _seed_merges_and_associations(conn, event, evidence, main)
        _seed_page_overflow(event, evidence)
        conn.commit()
    return database, ids

def _entries(report):
    return [item for block in report["blocks"] for item in block["entries"]]


def _assert_report_pair(conn, old, *, now, block=None, page=None):
    kwargs = {"block": block, "page": page}
    expected = old.build_weekly_report(conn, date(2026, 10, 5), now, **kwargs)
    actual = weekly.build_weekly_report(conn, date(2026, 10, 5), now, **kwargs)
    # Includes headers, all nested sources/notes, coverage, counters, links,
    # highlights, score filtering and every pagination field.
    assert actual == expected
    return actual


@pytest.mark.parametrize("mode", [
    "complete", "partial", "lookback-cap", "linked-history-cap", "week-cap",
])
def test_full_weekly_report_and_every_page_equal_frozen_oracle(
    report_database, old_report, monkeypatch, mode,
):
    database, ids = report_database
    now = _ts("2026-10-08T15:00:00" if mode == "partial" else "2026-10-12T09:00:00")
    limits = {
        "lookback-cap": ("MAX_LOOKBACK_EVIDENCE_ROWS", 3),
        "linked-history-cap": ("MAX_LINKED_HISTORY_ROWS", 4),
        "week-cap": ("MAX_WEEK_EVIDENCE_ROWS", 9),
    }
    if mode in limits:
        attribute, value = limits[mode]
        monkeypatch.setattr(weekly, attribute, value)
        setattr(old_report, attribute, value)

    statements = []
    with database.get_read_only_connection() as conn:
        conn.set_trace_callback(statements.append)
        overview = _assert_report_pair(conn, old_report, now=now)
        for block in overview["blocks"]:
            for page in range(1, block["pagination"]["page_count"] + 1):
                report = _assert_report_pair(
                    conn, old_report, now=now, block=block["key"], page=page,
                )
                assert report["highlights"] == []
                assert report["pagination_block"] == block["key"]
        assert conn.total_changes == 0
    forbidden = ("INSERT", "UPDATE", "DELETE", "CREATE", "ALTER", "DROP", "REPLACE")
    assert not any(sql.lstrip().upper().startswith(forbidden) for sql in statements)

    if mode != "complete":
        return
    blocks = {block["key"]: block for block in overview["blocks"]}
    items = {item["event_id"]: item for item in _entries(overview)}
    assert blocks["new"]["pagination"]["page_count"] > 1
    assert overview["duplicate_groups_folded"] >= 1
    assert overview["repeat_event_count"] >= 1
    assert overview["repeat_evidence_count"] >= 6
    assert blocks["new"]["hidden_low_priority"] >= 1
    assert blocks["correction"]["hidden_low_priority"] >= 1
    assert items[ids["semantic"]]["correction_kind"] == "denial"
    main = items[ids["main"]]
    assert main["companies"] == ["平安银行"]
    assert main["rule_association_count"] == 3  # 平安、数字人、规则主题
    assert main["sources_more"] > 0
    assert main["sources"][0]["claim_text"] == "平安银行澄清：未参与境外收购"
    assert any(source["same_link_previous"] for source in main["sources"])
    assert "此前有" in main["change"]
    assert any("此前已有否认/更正" in note for note in main["uncertainties"])


def test_oracle_does_not_call_replaced_daily_or_weekly_helpers(
    report_database, old_report, monkeypatch,
):
    database, _ids = report_database
    now = _ts("2026-10-12T09:00:00")
    with database.get_read_only_connection() as conn:
        expected = old_report.build_weekly_report(conn, date(2026, 10, 5), now)
        def replaced(*_args, **_kwargs):
            pytest.fail("frozen oracle called a replacement production helper")

        for module, names in (
            (daily, ("resolve_publisher", "_evidence_dict", "_publisher_url", "_normalize_headline", "_headline_correction_kind", "_report_text_cache", "_outlet_key",
                     "_publisher_summary", "_classify", "_source_views", "_is_named",
                     "_association_view", "_item_view", "_uncertainties")),
            (provenance, ("resolve_publisher", "_resolve_publisher_with_registry",
                          "_resolve_publisher_identity", "_publisher_domain_identity",
                          "_publisher_route_identity")),
            (weekly, ("_linked_history", "_classify_week", "_materialize_blocks",
                      "_build_weekly_report", "build_weekly_report")),
        ):
            for name in names:
                monkeypatch.setattr(module, name, replaced)
        assert old_report.build_weekly_report(conn, date(2026, 10, 5), now) == expected


@pytest.mark.parametrize("history,today,changes,block,has_background", [
    pytest.param(
        [("共同 前缀", "supports"), ("共同 前缀 多余", "supports")],
        [("共同 前缀", "supports"), ("共同 前缀 分叉", "supports"),
         ("共同", "supports")],
        2, "advanced", False, id="complete-token-prefix-and-terminal",
    ),
    pytest.param(
        [("Alphabet项目", "supports"), ("prefix", "supports"),
         ("foo barx", "supports")],
        [("Alphabet项目进展", "supports"), ("prefixSuffix", "supports"),
         ("foo bar", "supports")],
        3, "advanced", False, id="prefix-inside-one-token-is-not-equal",
    ),
    pytest.param(
        [("&lt;b&gt;ＡＣＭＥ&lt;/b&gt;&amp;nbsp;增长 −５％", "supports"),
         ("Acme 利润≦５亿元", "supports"), ("Straße 项目ﬀ进展", "supports"),
         ("İSTANBUL 项目", "supports"), ("<em>Ａ</em>股 项目", "supports"),
         ("分段<div>项目</div>", "supports"),
         ("换行<br>项目", "supports"),
         ("<script>忽略脚本</script>脚本 项目", "supports")],
        [("ACME 增长 -5%", "supports"), ("acme 利润⩽5亿元", "supports"),
         ("STRASSE 项目ff进展", "supports"), ("i\u0307stanbul 项目", "supports"),
         ("A股 项目", "supports"), ("分段项目", "supports"),
         ("换行 项目", "supports"),
         ("脚本 项目", "supports"), ("ACME 增长 +5%", "supports")],
        1, "advanced", False, id="markup-nfkc-casefold-and-sign",
    ),
    pytest.param(
        [("公司 净利润≤5亿元", "supports"), ("公司 增长1.5%", "supports"),
         ("公司 业绩不超过5亿元", "supports"), ("公司 利润-5%", "supports"),
         ("公司 收益10bp", "supports"), ("公司 营收5亿元", "supports"),
         ("公司 研发投入<5%", "supports")],
        [("公司 净利润≥5亿元", "supports"), ("公司 增长15%", "supports"),
         ("公司 业绩不低于5亿元", "supports"), ("公司 利润+5%", "supports"),
         ("公司 收益10bps", "supports"), ("公司 营收5万元", "supports"),
         ("公司 研发投入>5%", "supports")],
        7, "advanced", False, id="numbers-comparators-qualifiers-and-units",
    ),
    pytest.param(
        [("共有标题", "supports"), ("另一标题", "contradicts"),
         ("交易所要求公司澄清", "supports")],
        [("共有标题", "contradicts"), ("另一标题", "context"),
         ("交易所要求公司澄清", "contradicts")],
        3, "correction", True, id="same-headline-distinct-correction-kinds",
    ),
    pytest.param(
        [("<i>!!!</i>", "supports"), ("<b>???</b>", "contradicts")],
        [("...", "supports"), ("___", "contradicts"),
         ("本周新增正式进度", "supports")],
        1, "advanced", True, id="empty-key-with-support-and-denial",
    ),
    pytest.param(
        [("历史原始支持", "supports"),
         ("无关历史长前缀 否认 项目收购消息", "supports")],
        [("完全不同的新进展", "supports")],
        1, "advanced", True, id="unmatched-history-denial-is-still-background",
    ),
])
def test_full_report_exact_history_identity_edges_match_frozen_oracle(
    old_report, monkeypatch, history, today, changes, block, has_background,
):
    """Compare all output, including rows a selective matcher may skip reading.

    Old and recent linked history both belong to the group. Similar token
    prefixes must neither hide a new title nor make an equal title fresh.
    The oracle computes its own complete normalized keys with frozen code.
    """
    import db.database as database
    from core.scorer import Scorer

    database.init_db()
    monkeypatch.setattr(
        Scorer, "__init__",
        lambda *_args, **_kwargs: pytest.fail("weekly equivalence must not invoke a model"),
    )
    ids = {}
    with database.get_connection() as conn:
        event, evidence = _population_writers(conn, ids)
        owner = event("identity-edge", "身份边界事件", "2026-09-20T09:00:00", 9.0)
        for index, (title, stance) in enumerate(history):
            evidence(
                f"edge-history-{index}", owner, title,
                f"2026-{'09-20' if index % 2 == 0 else '10-02'}T09:00:{index:02}",
                stance=stance, url=f"https://history{index}.example/edge",
            )
        # Insert in reverse order so rowids and ingest order disagree. Source
        # rows retain their original titles even when normalized keys match.
        for index in reversed(range(len(today))):
            title, stance = today[index]
            evidence(
                f"edge-today-{index}", owner, title, f"2026-10-07T10:00:{index:02}",
                stance=stance, url=f"https://www.cls.cn/detail/edge-{index}",
                feed="http://rsshub:1200/cls/telegraph",
            )
        conn.commit()
    with database.get_read_only_connection() as conn:
        overview = _assert_report_pair(conn, old_report, now=_ts("2026-10-12T09:00:00"))
        page = _assert_report_pair(
            conn, old_report, now=_ts("2026-10-12T09:00:00"), block=block, page=1,
        )
        assert conn.total_changes == 0
    item, = _entries(overview)
    assert _entries(page) == [item]
    assert item["block"] == block
    assert item["unique_change_count"] == changes
    assert item["report_count"] == len(today)
    assert item["previous"] is not None
    assert any("此前已有否认/更正" in note for note in item["uncertainties"]) == has_background
    assert overview["repeat_evidence_count"] == len(today) - changes


def test_full_report_first_denial_repost_strict_cutoff_and_source_history(old_report):
    """A later repost cannot move the denial cutoff or replace its primary link."""
    import db.database as database

    database.init_db()
    ids = {}
    shared = "https://www.cls.cn/detail/revised"
    with database.get_connection() as conn:
        event, evidence = _population_writers(conn, ids)
        owner = event("cutoff-edge", "订单事件", "2026-09-20T09:00:00", 9.0)
        evidence("cutoff-history", owner, "历史订单原始报道", "2026-09-20T09:00:00", url=shared)
        evidence("cutoff-background", owner, "历史无关否认收购消息", "2026-10-02T09:00:00")
        for key, title, when, url in reversed([
            ("support-first", "公司获得订单", "2026-10-07T09:00:00", shared),
            ("support-repost", "公司获得订单！", "2026-10-07T10:00:00", None),
            ("first-denial", "公司否认获得订单", "2026-10-07T11:00:00", shared),
            ("equal-cutoff", "同秒支持不应成为此前报道", "2026-10-07T11:00:00", None),
            ("after-cutoff", "之后支持不应成为此前报道", "2026-10-07T12:00:00", None),
            ("denial-repost", "公司否认获得订单！", "2026-10-07T13:00:00", None),
        ]):
            evidence(key, owner, title, when, url=url)
        conn.commit()
    with database.get_read_only_connection() as conn:
        report = _assert_report_pair(conn, old_report, now=_ts("2026-10-12T09:00:00"))
    item, = _entries(report)
    assert item["unique_change_count"] == 4
    assert item["sources"][0]["claim_text"] == "公司否认获得订单"
    assert item["sources"][0]["same_link_previous"] == "历史订单原始报道"
    assert "此前有 3 条支持记录" in item["change"]
    assert "同秒支持" not in item["change"] and "之后支持" not in item["change"]
    assert any("此前已有否认/更正" in note for note in item["uncertainties"])


@pytest.mark.parametrize("style", ["plain", "markup", "entities", "unicode"])
def test_long_history_loading_classification_and_publishers_equal_frozen_report(old_report, monkeypatch, style):
    """Wide raw rows preserve cleaning order, truncation and source identity."""
    import db.database as database
    from core.scorer import Scorer

    database.init_db()
    monkeypatch.setattr(
        Scorer, "__init__",
        lambda *_args, **_kwargs: pytest.fail("long-history equivalence must not invoke a model"),
    )
    shared = "https://www.cls.cn/detail/long-history-current"
    stems = {
        "plain": " \\u00a0中间  空白\\t普通长文本 ",
        "markup": "<b>甲公司</b><br>项目<script>应当忽略</script>Ａ股",
        "entities": "&lt;em&gt;甲公司&lt;/em&gt;&nbsp;项目&amp;增量&#x4E59;",
        "unicode": "甲公司Ａ股e\\u0301Straßeﬀ\\u00a0利润⩽５亿元−５％",
    }
    stem = stems[style].replace("\\u00a0", "\u00a0").replace("\\u0301", "\u0301").replace("\\t", "\t")
    with database.get_connection() as conn:
        event, evidence = _population_writers(conn, {})
        owner = event("long-oracle", "甲公司项目进展", "2026-09-20T09:00:00", 9.0)
        for index in range(96):
            cue = "否认" if index % 11 == 0 else "交易所要求澄清" if index % 13 == 0 else "进展"
            title = f"历史第{index:03d}项{cue}" + stem * (3500 // len(stem)) + "单位≥1.5亿元"
            evidence(
                f"long-history-{index}", owner, title,
                f"2026-09-20T10:{index // 60:02d}:{index % 60:02d}",
                url=f"https://news.example/long/{index}",
                feed="http://rsshub:1200/cls/telegraph" if index % 2 else "https://news.example/feed",
                stance="contradicts" if index % 17 == 0 else "supports",
            )
        evidence("long-known", owner, "<b>甲公司</b>已披露项目", "2026-10-02T22:00:00")
        evidence("long-shared", owner, "甲公司项目原始支持", "2026-10-02T23:00:00", url=shared)
        evidence("long-repost", owner, "甲公司已披露项目！", "2026-10-05T09:00:00")
        evidence("long-update", owner, "甲公司新增项目进展", "2026-10-06T09:00:00")
        evidence(
            "long-denial", owner, "甲公司否认获得订单", "2026-10-07T09:00:00", url=shared,
            feed="http://rsshub:1200/cls/telegraph",
        )
        conn.commit()
    with database.get_read_only_connection() as conn:
        report = _assert_report_pair(conn, old_report, now=_ts("2026-10-12T09:00:00"))
        page = _assert_report_pair(
            conn, old_report, now=_ts("2026-10-12T09:00:00"), block="correction", page=1,
        )
        assert conn.total_changes == 0
    item, = _entries(report)
    assert _entries(page) == [item]
    assert item["block"] == "correction"
    assert item["unique_change_count"] == 2
    assert item["sources"][0]["same_link_previous"] == "甲公司项目原始支持"
    assert any("此前已有否认/更正" in note for note in item["uncertainties"])
