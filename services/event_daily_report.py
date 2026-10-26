"""Deterministic Event Radar daily change report.

This is a display/aggregation read model only.  It derives "what changed on
one Beijing calendar day" from rows that already exist in the stable event
core: article evidence (insert-once, ``observed_at`` is the ingestion time),
the audited merge history, and frozen security mapping revisions.

It never calls a model, never rescores, never writes, and never reads price
outcomes.  Report count is not used as importance, an older supporting
record never hides a later denial, and missing data is reported as
``证据不足`` instead of "nothing happened".
"""

from __future__ import annotations

import logging
import re
import sqlite3
import time
import unicodedata
from collections import OrderedDict
from contextlib import contextmanager
from contextvars import ContextVar
from datetime import date, datetime, timedelta
from functools import lru_cache
from heapq import merge
from itertools import chain
from typing import Any, Iterable
from urllib.parse import unquote, urlencode, urlsplit
from zoneinfo import ZoneInfo

from core.asset_mapping import is_direct_security_mention
# Read-only publisher helpers shared with the digest; nothing here writes.
from core.digest_engine import _SOURCE_PROVIDERS
from db.database import get_read_only_connection
from services.event_radar_query import (
    _PUBLIC_SCORE_PATH_VALUES,
    EventRadarQueryError,
    EventRadarUnavailableError,
    _public_cache_evidence_predicate,
    _safe_external_url,
)
from services.provenance_shadow import (
    _host as _publisher_host,
    _is_internal_host as _internal_publisher_host,
    _normalize_registry as _normalize_publisher_registry,
    _resolve_publisher_identity,
    resolve_publisher,
)
from utils.report_paging import parse_report_paging
from utils.helpers import plain_text_from_markup

logger = logging.getLogger(__name__)

REPORT_TZ = ZoneInfo("Asia/Shanghai")
BLOCK_ORDER = ("new", "advanced", "correction", "pending")
BLOCK_LABELS = {
    "new": "新增",
    "advanced": "推进",
    "correction": "澄清／更正线索",
    "pending": "待验证",
}
NO_CHANGE_LABEL = "无实质更新"
NO_LINKED_PROGRESS_LABEL = "未识别到已关联事件的新进展"
LINKAGE_LIMIT_NOTE = (
    "事件只按规范化标题完全一致关联，跨标题的后续报道无法自动识别为同一事件的进展"
)
HISTORICAL_VIEW_LABEL = "按历史入库记录重建的视图，非当日封存日报"
INSUFFICIENT_LABEL = "证据不足"
# Existing event score (written at ingestion), never the number of reports.
DISPLAY_MIN_PRIORITY = 6.0
MAX_ITEMS_PER_BLOCK = 20
MAX_SOURCES_PER_ITEM = 5
MAX_DAY_EVIDENCE_ROWS = 20_000
MAX_HISTORY_DAYS = 366
REPEAT_LOOKBACK_SECONDS = 7 * 86_400
MAX_LOOKBACK_EVIDENCE_ROWS = 60_000
STALE_AFTER_SECONDS = 6 * 3600
_CHUNK = 400
_TEXT_CACHE_SIZE = 8192
_REPORT_TEXT_CACHE = ContextVar("event_report_text_cache", default=None)
_REPORT_PUBLISHER_STATE = ContextVar("event_report_publisher_state", default=None)
_PUBLISHER_URL_CONTROL_RE = re.compile(r"[\x00-\x1f\x7f]")

# Rule lexicons over publisher headlines.  A hit is a deterministic,
# unverified reading of the headline, not a fact check.
# A regulator/exchange asking for clarification is not a denial or correction.
_CLARIFICATION_REQUEST_RE = re.compile(
    r"(?:要求|责令|督促|请).{0,12}澄清|"
    r"(?:要求|责令|督促|请).{0,12}(?:回复|答复|回应).{0,12}"
    r"(?:问询函|关注函|监管函)|"
    r"(?:交易所|上交所|深交所|北交所|港交所|证监会|证监局|监管).{0,12}"
    r"(?:要求|责令|督促).{0,12}(?:说明|核实|回复|解释|澄清)|"
    r"(?:交易所|上交所|深交所|北交所|港交所|证监会|证监局|监管).{0,8}问询|"
    r"问询函|关注函|监管工作函|监管函"
)
# Routine replies to an inquiry letter are neither a request nor a denial.
_INQUIRY_REPLY_RE = re.compile(
    r"(?:问询函|关注函|监管函).{0,60}(?:回复|答复|回应|专项说明|核查意见)|"
    r"(?:回复|答复|回应).{0,20}(?:问询函|关注函|监管函)"
)
_REQUEST_DIRECTIVE_RE = re.compile(r"要求|责令|督促|请")
_INQUIRY_RECEIPT_RE = re.compile(
    r"(?:收到|接到|收悉).{0,30}(?:问询函|关注函|监管工作函|监管函)(?:之后|后)"
)
_DENIAL_ACTION_RE = re.compile(r"否认|辟谣|澄清|更正|勘误")
# "澄清" is a denial/correction only as the party's own action; a hypothetical
# or an appeal for clarification ("如果…能作出澄清") is not a denial.
_PARTY_CLARIFICATION_RE = re.compile(
    r"澄清(?:公告|声明|说明)|发布.{0,8}澄清|予以澄清|澄清称|澄清[：:]"
)
# Ambiguous words can also be part of the party's name.  Recognize "希望"
# only at a speech boundary or after a reporting verb, and require an
# explicit conditional construction for the single character "若".
_CLARIFICATION_APPEAL_RE = re.compile(
    r"(?:如果|如能|假如|倘若|呼吁|应当|应该|需要|有必要|能否|是否|"
    r"若(?:能|需|要|将|是)).{0,30}澄清|"
    r"(?:^|[\s：:（(“\"「]|表示|称|说)希望.{0,30}澄清"
)
_STRONG_DENIAL_RE = re.compile(
    r"否认|辟谣|不实|失实|不属实|更正|勘误|没有此事|纯属谣言|子虚乌有|并非事实"
)
# Mask only an information-governance noun phrase or its explicit construction,
# never a bare "澄清机制": in "予以澄清机制调整传闻", 澄清 is
# the real action and 机制 starts its object. Other denials in the clause survive.
_CLARIFICATION_MECHANISM_RE = re.compile(
    r"(?:(?:不实|失实|虚假)(?:信息|消息|传闻)的?|"
    r"(?:建立|健全|建设|完善|构建|修订|制定|优化)(?:相关|相应|信息)?)"
    r"(?:快速|及时|主动)?(?:澄清|辟谣|更正|勘误)(?:工作)?(?:机制|制度|流程)|"
    # A published construction plan is also a noun phrase, not an event
    # correction. Require its explicit plan suffix: bare mechanism wording
    # can still be the object of a real clarification (e.g. 调整传闻).
    r"(?<!予以)(?:澄清|辟谣|更正|勘误)(?:工作)?(?:机制|制度|流程)"
    r"(?:建设|建立|健全|完善)(?:工作)?方案"
)
# Only these adjacent planned actions are excluded, not every planned clause.
# "按/按照/依照计划" alone does not assert future intent; retain it for the
# existing action/request checks rather than suppressing an actual response.
_PLANNED_MECHANISM_ACTION_RE = re.compile(
    r"(?:(?P<arrangement>(?:按(?:照)?|依照)(?:原|既定)?)?计划|拟)(?:予以|发布)?"
    r"(?:快速|及时|主动)?(?:澄清|辟谣|更正|勘误)(?:工作)?(?:机制|制度|流程)"
)
# A response after a request needs its own subject, completed action and
# explicit contrast/sequence boundary. A question quoting a denial is not
# the party's response; masking a mechanism must not turn it into one.
_POST_REQUEST_RESPONSE_RE = re.compile(
    r"(?:但|然而|随后|此后)(?P<subject>[\w\u4e00-\u9fff]{0,20}?(?:公司|集团|企业))"
    r"已(?:经)?(?:明确|公开|正式)?"
    r"(?:否认|辟谣|予以澄清|发布澄清(?:公告|声明)|澄清称)"
)
_REQUEST_RESPONSE_PARTY_RE = re.compile(
    r"(?:要求|责令|督促|请)(?P<party>[\w\u4e00-\u9fff]{0,20}?(?P<kind>公司|集团|企业))"
)
_QUOTED_REQUEST_TEXT_RE = re.compile(
    r"“[^”]*(?:”|$)|‘[^’]*(?:’|$)|「[^」]*(?:」|$)|『[^』]*(?:』|$)|"
    r"\"[^\"]*(?:\"|$)|'[^']*(?:'|$)"
)
_RESPONSE_UNCERTAINTY_RE = re.compile(r"是否|能否|有无|真实性|属实与否|要求核实")
_RESPONSE_HYPOTHETICAL_RE = re.compile(r"如果|假如|倘若|如能|若能")
# Every positive correction/request rule above requires at least one of these
# cues. The broad regulator cues deliberately allow false positives here;
# only a definite absence may bypass the clause-level rules.
_CORRECTION_CUE_WORDS = (
    "澄清", "问询", "关注函", "监管", "交易所", "上交所", "深交所", "北交所", "港交所",
    "证监会", "证监局", "否认", "辟谣", "不实", "失实", "不属实", "更正", "勘误",
    "没有此事", "纯属谣言", "子虚乌有", "并非事实",
)
_PENDING_RE = re.compile(
    r"传闻|据传|网传|传言|据悉|知情人士|消息人士|有望|预计|或将|可能|考虑|"
    r"酝酿|筹划|计划|将于|(?<![模虚])拟"
)
_COMPANY_RELATIONS = frozenset({"direct", "direct_impact", "affected"})
_RULE_RELATIONS = frozenset({"concept", "upstream", "downstream", "ecosystem"})


def _clock_label(timestamp: int | None, fmt: str = "%Y-%m-%d %H:%M") -> str:
    if timestamp is None or int(timestamp) <= 0:
        return ""
    return datetime.fromtimestamp(int(timestamp), REPORT_TZ).strftime(fmt)


_SIGN_TRANSLATION = str.maketrans({
    "\u2212": "-", "\u2013": "-", "\u2014": "-",
    # Comparison variants that NFKC leaves apart.
    "\u2266": "≤", "\u2267": "≥", "\u2a7d": "≤", "\u2a7e": "≥",
})
_SIGN_TRANSLATION_CHARS = tuple(map(chr, _SIGN_TRANSLATION))
# Numbers keep their sign, decimal point and unit, and comparison symbols are
# kept as tokens; other punctuation and whitespace are ignored.
# "1.5%" != "15%", "-5%" != "+5%", "≤5亿" != "≥5亿".  Word forms such as
# 不低于/不超过 are already kept as text tokens.
_KEY_TOKEN_RE = re.compile(
    r"[+-]?\d+(?:[.,]\d+)*(?:[%‰‱]|bp|bps)?|[^\W\d_]+|[%‰‱$¥€£°]|[<>=≤≥≠]"
)


def _normalized_key(text: object) -> str:
    cache = _REPORT_TEXT_CACHE.get()
    if cache is not None and isinstance(text, str):
        return cache[0](text)
    return _normalize_headline(text)


def _normalize_headline(text: object) -> str:
    """Width/case/punctuation-insensitive headline identity, numbers intact."""
    return " ".join(_KEY_TOKEN_RE.findall(_headline_key_text(text)))


def _headline_key_text(text: object) -> str:
    """The exact common input to full and membership-only tokenization."""
    value = unicodedata.normalize("NFKC", plain_text_from_markup(text or ""))
    # Sparse Unicode translation otherwise performs a Python table lookup for
    # every character, even when a long headline contains no mapped signs.
    if any(sign in value for sign in _SIGN_TRANSLATION_CHARS):
        value = value.translate(_SIGN_TRANSLATION)
    return value.casefold()


def parse_report_date(value: object, *, now: int) -> date:
    """Parse ``YYYY-MM-DD`` (Beijing) within the bounded history window."""
    today = datetime.fromtimestamp(now, REPORT_TZ).date()
    raw = "" if value is None else str(value).strip()
    if not raw:
        return today
    if not re.fullmatch(r"\d{4}-\d{2}-\d{2}", raw):
        raise EventRadarQueryError("date must be YYYY-MM-DD")
    try:
        parsed = date.fromisoformat(raw)
    except ValueError as exc:
        raise EventRadarQueryError("date is invalid") from exc
    if parsed > today or parsed < today - timedelta(days=MAX_HISTORY_DAYS):
        raise EventRadarQueryError("date is out of range")
    return parsed


def report_window(day: date, now: int) -> dict[str, Any]:
    """Return the half-open Beijing day and its inclusive read cutoff."""
    start = int(datetime(day.year, day.month, day.day, tzinfo=REPORT_TZ).timestamp())
    end = start + 86_400
    cutoff = min(end - 1, int(now))
    return {"start": start, "end": end, "cutoff": cutoff, "partial": cutoff < end - 1}


def _chunks(values: Iterable[str]) -> Iterable[list[str]]:
    items = list(values)
    for index in range(0, len(items), _CHUNK):
        yield items[index:index + _CHUNK]


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


_EVIDENCE_COLUMNS = (
    "ev.evidence_id, ev.event_id, ev.source_uri, ev.source_name, ev.claim_text, "
    "ev.stance, ev.announcement_at, ev.observed_at"
)


def _load_window_evidence(
    conn: sqlite3.Connection, lower: int, upper: int, limit: int
) -> tuple[list[dict[str, Any]], bool]:
    """Article evidence first observed in ``[lower, upper]`` (index-bounded)."""
    rows = conn.execute(
        f"SELECT {_EVIDENCE_COLUMNS} FROM event_evidence AS ev "
        "INDEXED BY idx_event_evidence_unbound_observed "
        "WHERE ev.mapping_id IS NULL AND ev.observed_at >= ? AND ev.observed_at <= ? "
        f"AND {_public_cache_evidence_predicate('ev')} "
        "ORDER BY ev.observed_at, ev.evidence_id LIMIT ?",
        (lower, upper, *_PUBLIC_SCORE_PATH_VALUES, limit + 1),
    ).fetchall()
    return [_evidence_dict(row) for row in rows[:limit]], len(rows) > limit


def _resolve_roots(
    conn: sqlite3.Connection, event_ids: set[str], cutoff: int
) -> dict[str, str]:
    """Follow audited merges that had happened by ``cutoff``."""
    parent: dict[str, str] = {}
    frontier = set(event_ids)
    seen = set(event_ids)
    while frontier:
        found: dict[str, str] = {}
        for chunk in _chunks(frontier):
            placeholders = ",".join("?" for _ in chunk)
            for row in conn.execute(
                "SELECT source_event_id, target_event_id FROM event_merge_history "
                f"WHERE source_event_id IN ({placeholders}) AND merged_at <= ?",
                (*chunk, cutoff),
            ):
                found[str(row[0])] = str(row[1])
        parent.update(found)
        frontier = {target for target in found.values() if target not in seen}
        seen |= frontier
    roots: dict[str, str] = {}
    for event_id in event_ids:
        current, hops = event_id, 0
        while current in parent and hops < 64:
            current, hops = parent[current], hops + 1
        roots[event_id] = current
    return roots


def _load_members(
    conn: sqlite3.Connection, roots: set[str], cutoff: int
) -> dict[str, set[str]]:
    """Return every event merged into each root by ``cutoff`` (inclusive)."""
    members = {root: {root} for root in roots}
    owner = {root: root for root in roots}
    frontier = set(roots)
    while frontier:
        discovered: set[str] = set()
        for chunk in _chunks(frontier):
            placeholders = ",".join("?" for _ in chunk)
            for row in conn.execute(
                "SELECT source_event_id, target_event_id FROM event_merge_history "
                f"WHERE target_event_id IN ({placeholders}) AND merged_at <= ?",
                (*chunk, cutoff),
            ):
                child, target = str(row[0]), str(row[1])
                if child in owner:
                    continue
                owner[child] = owner[target]
                members[owner[target]].add(child)
                discovered.add(child)
        frontier = discovered
    return members


def _load_prior_evidence(
    conn: sqlite3.Connection, event_ids: set[str], before: int
) -> dict[str, list[dict[str, Any]]]:
    result: dict[str, list[dict[str, Any]]] = {}
    for chunk in _chunks(event_ids):
        placeholders = ",".join("?" for _ in chunk)
        rows = conn.execute(
            f"SELECT {_EVIDENCE_COLUMNS} FROM event_evidence AS ev "
            "INDEXED BY idx_event_evidence_event_time "
            f"WHERE ev.event_id IN ({placeholders}) AND ev.mapping_id IS NULL "
            f"AND ev.observed_at < ? AND {_public_cache_evidence_predicate('ev')}",
            (*chunk, before, *_PUBLIC_SCORE_PATH_VALUES),
        ).fetchall()
        for row in rows:
            item = _evidence_dict(row)
            result.setdefault(item["event_id"], []).append(item)
    return result


def _load_event_rows(
    conn: sqlite3.Connection, event_ids: set[str]
) -> dict[str, dict[str, Any]]:
    result: dict[str, dict[str, Any]] = {}
    for chunk in _chunks(event_ids):
        placeholders = ",".join("?" for _ in chunk)
        for row in conn.execute(
            "SELECT event_id, title, event_type, priority FROM events "
            f"WHERE event_id IN ({placeholders})",
            tuple(chunk),
        ):
            result[str(row["event_id"])] = {
                "title": plain_text_from_markup(row["title"])[:1000],
                "event_type": str(row["event_type"] or ""),
                "priority": float(row["priority"]),
            }
    return result


def _load_associations(
    conn: sqlite3.Connection, event_ids: set[str], cutoff: int
) -> dict[str, list[dict[str, str]]]:
    """Latest mapping revision per (event, security, relation) as of cutoff."""
    result: dict[str, list[dict[str, str]]] = {}
    for chunk in _chunks(event_ids):
        placeholders = ",".join("?" for _ in chunk)
        rows = conn.execute(
            "SELECT mapping.event_id, mapping.security_id, "
            "lower(mapping.relation_type) AS relation_type, security.name "
            "FROM event_securities AS mapping "
            "JOIN securities AS security ON security.security_id = mapping.security_id "
            f"WHERE mapping.event_id IN ({placeholders}) AND mapping.created_at <= ? "
            "AND lower(security.asset_type) = 'equity' AND mapping.revision = ("
            " SELECT MAX(prior.revision) FROM event_securities AS prior "
            " WHERE prior.event_id = mapping.event_id "
            " AND prior.security_id = mapping.security_id "
            " AND prior.relation_type = mapping.relation_type "
            " AND prior.created_at <= ?)",
            (*chunk, cutoff, cutoff),
        ).fetchall()
        for row in rows:
            result.setdefault(str(row["event_id"]), []).append(
                {
                    "security_id": str(row["security_id"]),
                    "relation_type": str(row["relation_type"]),
                    "name": str(row["name"] or ""),
                }
            )
    return result


def _load_security_names(conn: sqlite3.Connection) -> frozenset[str]:
    """Known security short names, used only to reject partial-name hits."""
    return frozenset(
        _compact(str(row[0])) for row in conn.execute("SELECT name FROM securities")
        if str(row[0] or "").strip()
    )


def _coverage(conn: sqlite3.Connection, cutoff: int) -> dict[str, int | None]:
    row = conn.execute(
        "SELECT MIN(observed_at) AS first_at FROM event_evidence "
        "INDEXED BY idx_event_evidence_unbound_observed WHERE mapping_id IS NULL"
    ).fetchone()
    latest = conn.execute(
        "SELECT MAX(observed_at) FROM event_evidence "
        "INDEXED BY idx_event_evidence_unbound_observed "
        "WHERE mapping_id IS NULL AND observed_at <= ?",
        (cutoff,),
    ).fetchone()
    return {
        "first_at": None if row is None or row[0] is None else int(row[0]),
        "latest_at": None if latest is None or latest[0] is None else int(latest[0]),
    }


def _post_request_completed_response(text: str, request: re.Match[str]) -> bool:
    # Keep positions stable while ignoring quoted request contents, including
    # an unmatched opening quotation mark. Completed responses outside quotes
    # may still quote the disputed event in their own object.
    visible = _QUOTED_REQUEST_TEXT_RE.sub(lambda match: " " * len(match[0]), text)
    party = _REQUEST_RESPONSE_PARTY_RE.search(visible[request.start():request.end()])
    if party is None:
        return False
    subjects = {party["party"], party["kind"], "该" + party["kind"], "本" + party["kind"]}
    for response in _POST_REQUEST_RESPONSE_RE.finditer(visible, request.end()):
        # Bind to the actual requested party, not arbitrary words ending in
        # 公司 (e.g. 不代表甲公司 / 无法确认甲公司 / 有人猜测甲公司).
        if response["subject"] not in subjects:
            continue
        if _RESPONSE_HYPOTHETICAL_RE.search(visible[:response.end()]):
            continue
        if not _RESPONSE_UNCERTAINTY_RE.search(visible[response.start():]):
            return True
    return False


def _clause_correction_kind(text: str) -> str:
    # Equal-length masking retains request/response boundaries and leaves
    # the original evidence untouched for titles, sources and identity.
    mechanism_context = "机制" in text or "制度" in text or "流程" in text
    if mechanism_context:
        text = _CLARIFICATION_MECHANISM_RE.sub(lambda match: " " * len(match[0]), text)
    # A request may quote words such as "失实" or "否认" as the question to
    # investigate. They are not an answer from the company in this clause.
    text = _INQUIRY_RECEIPT_RE.sub("", text, count=1)
    request = _CLARIFICATION_REQUEST_RE.search(text)
    # Preserve an actual request for this clarification, but do not treat a
    # planned action before the request as the company's completed response.
    if mechanism_context:
        text = _PLANNED_MECHANISM_ACTION_RE.sub(
            lambda match: match[0] if match["arrangement"] else " " * len(match[0]), text,
        )
    if request:
        letter_reply = (
            _INQUIRY_REPLY_RE.search(text)
            and not _REQUEST_DIRECTIVE_RE.search(request.group())
        )
        if not letter_reply:
            # An explicit action before the request belongs to the company's
            # response, not to the text the regulator is asking it to check.
            if (_DENIAL_ACTION_RE.search(text[:request.start()])
                    or (mechanism_context and _post_request_completed_response(text, request))):
                return "denial"
            return "request"
    if _STRONG_DENIAL_RE.search(text):
        return "denial"
    if _PARTY_CLARIFICATION_RE.search(text) and not _CLARIFICATION_APPEAL_RE.search(text):
        return "denial"
    return ""


def _correction_kind(item: dict[str, Any]) -> str:
    """Rule reading of one headline: ``denial``, ``request`` or ``""``.

    A regulator or exchange asking a company to clarify is a request, never a
    denial; an explicit denial/correction wording or a recorded
    ``contradicts`` stance is a denial/correction.
    """
    cache = _REPORT_TEXT_CACHE.get()
    classify = cache[1] if cache is not None else _headline_correction_kind
    return classify(item["claim_text"], item["stance"])


def _semantic_key(item: dict[str, Any]) -> tuple[str, str]:
    """Equal headlines retain distinct support, denial and request meanings."""
    return _normalized_key(item["claim_text"]), _correction_kind(item)


def _headline_correction_kind(claim_text: str, stance: str) -> str:
    if stance == "contradicts":
        return "denial"
    if not any(cue in claim_text for cue in _CORRECTION_CUE_WORDS):
        return ""
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
    # Registry validation is substantial and identical for every source in a
    # report. Prepare it lazily, keeping the mutable registry request-local.
    publisher_state: list[_ReportPublisherResolver | None] = [None]
    publisher_token = _REPORT_PUBLISHER_STATE.set(publisher_state)
    try:
        yield
    finally:
        _REPORT_PUBLISHER_STATE.reset(publisher_token)
        if publisher_state[0] is not None:
            publisher_state[0].clear()
        publisher_state.clear()
        _REPORT_TEXT_CACHE.reset(token)
        for cache in caches:
            cache.cache_clear()


_PUBLISHER_REGISTRY = {
    "rules": [
        {"publisher_group": key, "domains": domains, "rss_routes": routes}
        for key, _label, domains, routes in _SOURCE_PROVIDERS
    ]
}


def _publisher_url(value: object) -> str:
    """Only well-formed HTTP(S) identities may supply a trusted RSS route."""
    raw = str(value or "").strip()
    return _publisher_url_parts(raw)[0]


def _publisher_url_parts(raw: str) -> tuple[str, str, str]:
    """Validate once, retaining only the parsed fields used for identity."""
    if _PUBLISHER_URL_CONTROL_RE.search(raw):
        return "", "", ""
    try:
        parts = urlsplit(raw)
        # userinfo exists exactly when the authority contains a literal @.
        # Read hostname once: its property reparses the authority each time.
        if parts.scheme not in {"http", "https"} or "@" in parts.netloc:
            return "", "", ""
        hostname, path = parts.hostname, parts.path
        if not hostname:
            return "", "", ""
        # A dot segment can only exist literally or through percent decoding.
        if (("." in path or "%" in path)
                and any(segment in {".", ".."} for segment in unquote(path).split("/"))):
            return "", "", ""
        if ":" in parts.netloc:
            _ = parts.port  # Validate malformed/out-of-range ports as well.
    except ValueError:
        return "", "", ""
    return raw, hostname, path


def _publisher_host_identity(hostname: str) -> tuple[str, bool]:
    host = _publisher_host(hostname)
    return host, _internal_publisher_host(host)


class _ReportPublisherResolver:
    """One request's pure identity work, sharing one fixed 8192-entry bound.

    URL validation, host normalization and registry matching share the same
    LRU, so reuse does not multiply the existing per-cache capacity. Unique
    article paths never become registry keys on a public transport.
    """

    def __init__(self):
        self.registry = _normalize_publisher_registry(_PUBLISHER_REGISTRY)
        self.cache: OrderedDict[tuple, Any] = OrderedDict()
        self._cache_miss = object()

    def clear(self):
        self.cache.clear()

    def _cached(self, key: tuple, calculate, *args):
        value = self.cache.get(key, self._cache_miss)
        if value is self._cache_miss:
            value = calculate(*args)
            if len(self.cache) >= _TEXT_CACHE_SIZE:
                self.cache.popitem(last=False)
            self.cache[key] = value
        else:
            self.cache.move_to_end(key)
        return value

    def _parse_identity(self, raw: str) -> tuple[str, str]:
        valid, hostname, path = _publisher_url_parts(raw)
        if not valid:
            return "", ""
        host, internal = self._cached(
            ("host", hostname), _publisher_host_identity, hostname,
        )
        return host, re.sub(r"/{2,}", "/", path) if internal and path else ""

    def _resolve_identity(self, host: str, paths: tuple[str, ...]) -> str:
        identity = _resolve_publisher_identity(host, paths, registry=self.registry)
        key = str(identity["publisher_group"])
        return "" if key == "unknown" else key

    def outlet_key(self, source_uri: object, source_name: object) -> str:
        uri = str(source_uri or "").strip()
        name = str(source_name or "").strip()
        host, uri_path = self._cached(("url", uri), self._parse_identity, uri)
        _name_host, name_path = self._cached(("url", name), self._parse_identity, name)
        paths = tuple(sorted({path for path in (uri_path, name_path) if path}))
        return self._cached(
            ("identity", host, paths), self._resolve_identity, host, paths,
        )


def _outlet_key(item: dict[str, Any]) -> str:
    """Media outlet behind one record, or "" when it cannot be determined.

    Several feeds of one outlet (e.g. three wallstreetcn RSSHub routes) are
    one publisher: known outlets resolve through the shared provider table,
    other article links through their registrable domain.  A feed route
    only names an outlet when it matches a registered, trusted internal route.
    """
    state = _REPORT_PUBLISHER_STATE.get()
    if state is None:
        identity = resolve_publisher(
            _publisher_url(item["source_uri"]), _publisher_url(item["source_name"]),
            config=_PUBLISHER_REGISTRY,
        )
        key = str(identity["publisher_group"])
        return "" if key == "unknown" else key
    if state[0] is None:
        state[0] = _ReportPublisherResolver()
    return state[0].outlet_key(item["source_uri"], item["source_name"])


def _publisher_summary(items: Iterable[dict[str, Any]]) -> tuple[int, int]:
    """(identified outlets, distinct links whose outlet is unknown)."""
    outlets: set[str] = set()
    unknown_links: set[str] = set()
    for item in items:
        key = _outlet_key(item)
        if key:
            outlets.add(key)
        else:
            unknown_links.add(item["source_uri"])
    return len(outlets), len(unknown_links)


def _publisher_note(identified: int, unknown: int) -> str:
    unknown_note = f"；另有 {unknown} 个链接来源媒体未能识别，未计入独立来源" if unknown else ""
    if identified == 0:
        return f"来源媒体未能识别，发布方数量未知（{unknown} 个链接）；不能据此判断独立来源"
    if identified == 1:
        return "仅 1 个来源发布方，未见独立来源" + unknown_note
    return (
        f"{identified} 个来源发布方（按媒体识别，同一媒体的多个订阅源只算 1 个；"
        f"转载关系未核验，数量不代表重要程度）{unknown_note}"
    )


def _build_groups(
    today: list[dict[str, Any]],
    roots: dict[str, str],
    members: dict[str, set[str]],
    prior: dict[str, list[dict[str, Any]]],
    events: dict[str, dict[str, Any]],
    associations: dict[str, list[dict[str, str]]],
) -> list[dict[str, Any]]:
    """One record per canonical root group, as of the report cutoff."""
    groups: dict[str, dict[str, Any]] = {}
    for item in today:
        root = roots.get(item["event_id"], item["event_id"])
        group = groups.get(root)
        if group is None:
            meta = events.get(root, {"title": item["claim_text"], "event_type": "",
                                     "priority": 0.0})
            group_members = members.get(root, {root})
            group = {
                "root_ids": [root],
                "title": meta["title"] or item["claim_text"],
                "event_type": meta["event_type"],
                "priority": float(meta["priority"]),
                "today": [],
                "prior": [row for member in group_members for row in prior.get(member, [])],
                "associations": [
                    row for member in group_members
                    for row in associations.get(member, [])
                ],
            }
            groups[root] = group
        group["today"].append(item)
    for group in groups.values():
        # Current event titles are display metadata, not historical evidence.
        # Choose identity before folding groups or matching prior headlines.
        first = min(group["prior"] + group["today"], key=_ingest_order)
        group["identity_title"] = first["claim_text"]
    return list(groups.values())


def _ingest_order(item: dict[str, Any]) -> tuple[int, str]:
    return (item["observed_at"], item["evidence_id"])


def _merge_duplicate_groups(groups: list[dict[str, Any]]) -> tuple[list[dict[str, Any]], int]:
    """Merge display groups whose headline identity is the same.

    The stable resolver already merges exact normalized titles inside fixed
    windows; this only folds punctuation/width variants that it kept apart.
    Numbers keep sign, decimals and units, so a changed figure stays apart.
    Merging is display-only and never changes any stored event.
    """
    merged: dict[str, dict[str, Any]] = {}
    folded = 0
    for group in groups:
        key = _normalized_key(group.get("identity_title", group["title"])) or group["root_ids"][0]
        existing = merged.get(key)
        if existing is None:
            merged[key] = group
            continue
        folded += 1
        if group["priority"] > existing["priority"]:
            existing["title"], existing["event_type"] = group["title"], group["event_type"]
        existing["priority"] = max(existing["priority"], group["priority"])
        existing["root_ids"].extend(group["root_ids"])
        for field in ("today", "prior", "associations"):
            existing[field].extend(group[field])
    for group in merged.values():
        # Concatenated groups must read in ingest order again, so "latest"
        # always means the latest record across every folded group.
        for field in ("today", "prior"):
            group[field].sort(key=_ingest_order)
    return list(merged.values()), folded


def _lookback_index(
    rows: list[dict[str, Any]],
    *, headline_keys: set[str] | None = None,
) -> dict[str, tuple[dict[str, Any], ...]]:
    """Keep semantic endpoints only for headlines the caller can look up.

    None retains the full index for existing callers. Filtering happens after
    the unchanged bounded read, so coverage/truncation still describe all rows.
    An empty normalized headline is a valid key, not a reason to drop evidence.
    """
    if headline_keys is not None and not headline_keys:
        return {}
    endpoints: dict[tuple[str, str], tuple[dict[str, Any], dict[str, Any]]] = {}
    for row in rows:
        headline = _normalized_key(row["claim_text"])
        if headline_keys is not None and headline not in headline_keys:
            continue
        key = (headline, _correction_kind(row))
        first, latest = endpoints.get(key, (row, row))
        endpoints[key] = (
            min(first, row, key=_ingest_order),
            max(latest, row, key=_ingest_order),
        )
    # Matching still uses normalized headlines, but a denial between two
    # same-headline supporting records must survive the bounded index.
    index: dict[str, tuple[dict[str, Any], ...]] = {}
    for (headline, _kind), pair in endpoints.items():
        index[headline] = index.get(headline, ()) + pair
    return index


def _matching_headline_keys(group: dict[str, Any]) -> set[str]:
    """Exactly the lookback keys consumed by classification, before display caps."""
    return {_normalized_key(group.get("identity_title", group["title"]))} | {
        _normalized_key(item["claim_text"]) for item in group["today"]
    }


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


def _correction_change_text(state: dict[str, Any], *, period_label: str = "今日") -> str:
    support = state["prior_support"]
    if state["denials"]:
        denial = max(state["denials"], key=_ingest_order)["claim_text"]
        if support:
            return (
                f"此前有 {len(support)} 条支持记录（最早 "
                f"{_clock_label(support[0]['observed_at'])}）；{period_label}出现否认/更正：{denial}。"
                "以最新否认/更正为准待核，旧支持记录不覆盖此变化"
            )
        return f"{period_label}出现否认/更正：{denial}"
    request = max(state["corrections"], key=_ingest_order)["claim_text"]
    return (
        f"{period_label}出现要求澄清/问询：{request}。"
        "这是监管或交易所的要求，不是当事方的否认或更正"
    )


def _change_text(
    group: dict[str, Any], state: dict[str, Any], *, period_label: str = "今日",
) -> str:
    block = state["block"]
    if block == "correction":
        return _correction_change_text(state, period_label=period_label)
    if state["history"]:
        claim = state["fresh"][0]["claim_text"]
        verb = "新的计划/传闻/预期表述" if block == "pending" else "新表述"
        return f"已有事件出现{verb}：{claim}"
    if block == "pending":
        return "首次出现，表述为计划/传闻/预期，尚未确认已发生"
    return "首次出现（此前 7 天内无同标题记录）"


def _correction_notes(state: dict[str, Any]) -> list[str]:
    if not state["denials"]:
        return [
            "监管或交易所要求澄清/说明，当事方尚未在这些来源中否认或更正",
            "要求澄清/问询由标题措辞规则识别，未经人工或模型核验",
        ]
    notes = [
        "支持与否认/更正并存，旧支持记录不覆盖否认" if state["prior_support"]
        else f"被否认/更正的原始报道未能自动关联：{INSUFFICIENT_LABEL}"
    ]
    if any(item["stance"] != "contradicts" for item in state["denials"]):
        notes.append("否认/更正由标题措辞规则识别，未经人工或模型核验")
    return notes


def _uncertainties(
    group: dict[str, Any], state: dict[str, Any],
    *, publisher_summary: tuple[int, int] | None = None,
) -> list[str]:
    if publisher_summary is None:
        publisher_summary = _publisher_summary(chain(group["today"], state["history"]))
    notes = [_publisher_note(*publisher_summary)]
    # Inside a denial, rumor wording describes the claim being denied.
    if state["pending_language"] and state["block"] != "correction":
        notes.append("标题含计划/传闻/预期措辞，事项尚未发生或未经确认")
    if state["block"] == "correction":
        notes.extend(_correction_notes(state))
    if state["background_corrections"]:
        when = _clock_label(state["background_corrections"][-1]["observed_at"])
        notes.append(f"此前已有否认/更正记录（{when}，仅作背景），新的报道不推翻该否认")
    return notes


def _next_check(state: dict[str, Any], identified_publishers: int) -> str:
    if state["block"] == "correction":
        return (
            "核对被否认/更正的原始报道与当事方正式公告" if state["denials"]
            else "等待当事方的澄清或回复公告"
        )
    if state["pending_language"]:
        return "等待当事方公告、官方文件或实际落地记录"
    if identified_publishers == 0:
        return "核实来源媒体或查找一手公告"
    if identified_publishers == 1:
        return "等待第二个独立发布方或一手公告"
    return "关注后续是否出现新事实、更正或否认"


def _source_rank(item: dict[str, Any]) -> tuple[int, int, str]:
    kind = _correction_kind(item)
    order = 0 if kind == "denial" else 1 if kind == "request" else 2
    return (order, -item["observed_at"], item["evidence_id"])


def _published_label(item: dict[str, Any]) -> str:
    announced = item["announcement_at"]
    if announced is None:
        return f"发布时间：{INSUFFICIENT_LABEL}（仅有入库时间）"
    if announced > item["observed_at"] + 600:
        return f"发布 {_clock_label(announced)}（晚于入库时间，时间戳待核）"
    return f"发布 {_clock_label(announced)}"


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
    shown = chosen[:MAX_SOURCES_PER_ITEM]
    history_by_url: dict[str, list[dict[str, Any]]] = {
        item["source_uri"]: [] for item in shown
    }
    for row in history:
        matching = history_by_url.get(row["source_uri"])
        if matching is not None:
            matching.append(row)
    views = []
    for item in shown:
        previous_claim = ""
        item_key = _normalized_key(item["claim_text"])
        # The output is the last qualifying row in original list order, not
        # the newest timestamp. Search that order backwards and stop as soon
        # as the exact output is known, without normalizing every old revision.
        for row in chain(reversed(history_by_url[item["source_uri"]]),
                         reversed(by_url[item["source_uri"]])):
            if (row["observed_at"] <= item["observed_at"]
                    and _normalized_key(row["claim_text"]) != item_key):
                previous_claim = row["claim_text"]
                break
        views.append({
            "url": _safe_external_url(item["source_uri"]),
            "source_name": item["source_name"],
            "claim_text": item["claim_text"],
            "stance": item["stance"],
            "correction_kind": _correction_kind(item),
            "outlet_known": bool(_outlet_key(item)),
            "same_link_previous": previous_claim,
            "published_label": _published_label(item),
            "observed_label": f"入库 {_clock_label(item['observed_at'])}",
        })
    return views, max(0, len(chosen) - MAX_SOURCES_PER_ITEM)


def _compact(text: str) -> str:
    return re.sub(r"\s+", "", unicodedata.normalize("NFKC", text)).casefold()


def _relevant_known_names(known_names: frozenset[str], targets: set[str]) -> set[str]:
    """Known names that can cover an actual company candidate on this card."""
    lengths = sorted({len(target) for target in targets if target})
    relevant: set[str] = set()
    for known in known_names:
        for length in lengths:
            if length > len(known):
                break
            if any(known[start:start + length] in targets
                   for start in range(len(known) - length + 1)):
                relevant.add(known)
                break
    return relevant


class _AssociationTexts(list[str]):
    """Card-local normalized text and candidate-presence lookup, no span cache."""

    def __init__(
        self, texts: list[str], known_names: frozenset[str],
        *, targets: dict[str, str] | None = None,
    ):
        # Duplicate title/evidence text cannot change an any-mention result.
        super().__init__(dict.fromkeys(texts))
        self.known_names = known_names
        self.targets = targets
        self.target_values = set(targets.values()) if targets is not None else set(known_names)
        relevant = (
            _relevant_known_names(known_names, self.target_values)
            if targets is not None else set(known_names)
        )
        self.longer_by_length: dict[int, list[str]] = {}
        minimum = min((len(target) for target in self.target_values if target), default=0)
        for known in relevant:
            if len(known) > minimum:
                self.longer_by_length.setdefault(len(known), []).append(known)
        self.longer_by_target: dict[str, tuple[str, ...]] = {}
        self.known_text_indices: dict[str, list[int]] | None = None
        self.prepared: list[tuple[str, str] | None] = [None] * len(self)

    def _text_at(self, index: int) -> tuple[str, str]:
        value = self.prepared[index]
        if value is None:
            text = self[index]
            value = (text, _compact(text))
            self.prepared[index] = value
        return value

    def matching_texts(self, target: str):
        if len(self.target_values) <= 1:
            # Most cards name one issuer. Preserve the old early return
            # without compacting or retaining every later evidence title.
            for index in range(len(self)):
                text, compact = self._text_at(index)
                if target in compact:
                    yield text, compact
            return
        if self.known_text_indices is None:
            self.known_text_indices = {}
            for index in range(len(self)):
                _text, compact = self._text_at(index)
                for candidate in self.target_values:
                    if candidate and candidate in compact:
                        self.known_text_indices.setdefault(candidate, []).append(index)
        for index in self.known_text_indices.get(target, ()):
            yield self._text_at(index)

    def longer_names(self, target: str) -> tuple[str, ...]:
        if target not in self.longer_by_target:
            self.longer_by_target[target] = tuple(
                known for length, names in self.longer_by_length.items()
                if length > len(target) for known in names if target in known
            )
        return self.longer_by_target[target]


def _has_uncovered_name(target: str, compact: str, longer: tuple[str, ...]) -> bool:
    if not longer:
        return True  # Presence was already checked; exact self spans cannot cover it.
    # Merge each longer name's nonoverlapping regex occurrences in start order.
    # A streaming maximum end avoids retaining millions of repeated-name spans.
    spans = merge(*(
        map(lambda match: match.span(), re.finditer(re.escape(known), compact))
        for known in longer
    ))
    span = next(spans, None)
    farthest = -1
    for occurrence in re.finditer(re.escape(target), compact):
        while span is not None and span[0] <= occurrence.start():
            farthest = max(farthest, span[1])
            span = next(spans, None)
        if farthest < occurrence.end():
            return True
    return False


def _is_named(name: str, texts: list[str], known_names: frozenset[str]) -> bool:
    """Whole-name mention that is not part of a longer known term.

    Uses the write path's entity check (e.g. "数字人" in "数字人民币" is a
    currency, not the issuer) and rejects occurrences covered by a longer
    known security name (e.g. "平安" inside "平安银行").
    """
    prepared = texts if (
        isinstance(texts, _AssociationTexts) and texts.known_names is known_names
    ) else None
    target = (
        prepared.targets[name]
        if prepared is not None and prepared.targets is not None and name in prepared.targets
        else _compact(name)
    )
    if not target:
        return False
    if prepared is None or target not in prepared.target_values:
        prepared = _AssociationTexts(texts, known_names, targets={name: target})
    longer = prepared.longer_names(target)
    for text, compact in prepared.matching_texts(target):
        if not is_direct_security_mention(text, name):
            continue
        if _has_uncovered_name(target, compact, longer):
            return True
    return False


def _association_view(group: dict[str, Any], known_names: frozenset[str]) -> dict[str, Any]:
    """Keep title-named companies apart from rule-pool theme associations."""
    texts = group.get(
        "association_texts", [group["title"]] + [item["claim_text"] for item in group["today"]],
    )
    targets: dict[str, str] = {}
    for row in group["associations"]:
        if row["relation_type"] in _COMPANY_RELATIONS:
            name = row["name"].strip()
            if name not in targets:
                targets[name] = _compact(name)
    prepared = _AssociationTexts(texts, known_names, targets=targets) if targets else texts
    companies: list[str] = []
    company_names: set[str] = set()
    named: dict[str, bool] = {}
    rule_ids: set[str] = set()
    for row in group["associations"]:
        name = row["name"].strip()
        relation = row["relation_type"]
        if relation in _COMPANY_RELATIONS and name not in named:
            named[name] = _is_named(name, prepared, known_names)
        if relation in _COMPANY_RELATIONS and named[name]:
            if name not in company_names:
                companies.append(name)
                company_names.add(name)
        elif relation in _COMPANY_RELATIONS or relation in _RULE_RELATIONS:
            rule_ids.add(row["security_id"])
    company_ids = {
        row["security_id"] for row in group["associations"] if row["name"].strip() in company_names
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
    publisher_summary = _publisher_summary(chain(group["today"], state["history"]))
    identified, _unknown = publisher_summary
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
        "uncertainties": _uncertainties(group, state, publisher_summary=publisher_summary),
        "next_check": _next_check(state, identified),
        "latest_at": max(item["observed_at"] for item in group["today"]),
        **_association_view(group, known_names),
    }


def _data_state(
    coverage: dict[str, int | None], window: dict[str, Any], evidence_count: int,
    truncated: bool, lookback_truncated: bool = False,
) -> tuple[str, str]:
    first_at, latest_at = coverage["first_at"], coverage["latest_at"]
    if first_at is None or first_at > window["cutoff"]:
        return "before_coverage", (
            f"{INSUFFICIENT_LABEL}：该日期早于事件数据覆盖起点，不代表当日没有发生事件"
        )
    if evidence_count == 0:
        return "no_ingestion", (
            f"{INSUFFICIENT_LABEL}：当日没有任何入库记录（可能是采集中断），不代表没有发生"
        )
    if truncated:
        return "truncated", (
            f"{INSUFFICIENT_LABEL}：当日证据超过读取上限 {MAX_DAY_EVIDENCE_ROWS} 条，报告不完整"
        )
    if lookback_truncated:
        return "truncated", (
            f"{INSUFFICIENT_LABEL}：此前 7 天的对照记录超过读取上限，"
            "无法确认哪些是重复报道"
        )
    if latest_at is not None and window["cutoff"] - latest_at > STALE_AFTER_SECONDS:
        return "stale", (
            f"{INSUFFICIENT_LABEL}：最近一次入库为 {_clock_label(latest_at)}（北京时间），"
            "之后的变化未覆盖"
        )
    return "ok", ""


def _empty_label(key: str, state: str, state_label: str, hidden: int) -> str:
    if state != "ok":
        return state_label
    hidden_note = (
        f"另有 {hidden} 个低于 {DISPLAY_MIN_PRIORITY:.0f} 分的变化未展开" if hidden else ""
    )
    if key == "advanced":
        # An empty block here mostly reflects missing cross-title linkage,
        # not evidence that known events stood still.
        return f"{NO_LINKED_PROGRESS_LABEL}（" + "；".join(
            part for part in (hidden_note, LINKAGE_LIMIT_NOTE) if part
        ) + "）"
    if hidden:
        return f"{NO_CHANGE_LABEL}（达到展示门槛的为 0；{hidden_note}）"
    return NO_CHANGE_LABEL


def eligible_report_items(items: list[dict[str, Any]], key: str) -> list[dict[str, Any]]:
    """Share the existing report display gate and deterministic ordering."""
    eligible = [
        item for item in items if item["block"] == key
        and (item.get("correction_kind") == "denial" or item["priority"] >= DISPLAY_MIN_PRIORITY)
    ]
    return sorted(eligible, key=lambda item: (-item["priority"], -item["latest_at"], item["event_id"]))


def _assemble_blocks(
    items: list[dict[str, Any]], state: str, state_label: str,
    *, block_pages: dict[str, int] | None = None,
) -> list[dict[str, Any]]:
    blocks = []
    for key in BLOCK_ORDER:
        candidates = [item for item in items if item["block"] == key]
        # Denials/corrections stay visible regardless of score: a denial of a
        # low-score story must not disappear behind an older supporting
        # headline.  Clarification requests follow the normal score gate.
        eligible = eligible_report_items(items, key)
        page = (block_pages or {}).get(key, 1)
        offset = (page - 1) * MAX_ITEMS_PER_BLOCK
        shown = eligible[offset:offset + MAX_ITEMS_PER_BLOCK]
        hidden_low = len(candidates) - len(eligible)
        blocks.append({
            "key": key,
            "label": BLOCK_LABELS[key],
            "entries": shown,
            "total": len(candidates),
            "hidden_low_priority": hidden_low,
            "hidden_overflow": len(eligible) - len(shown),
            "empty_label": "" if shown else _empty_label(key, state, state_label, hidden_low),
        })
        if block_pages is not None:
            blocks[-1]["pagination"] = {
                "page": page,
                "page_count": max(1, (len(eligible) + MAX_ITEMS_PER_BLOCK - 1) // MAX_ITEMS_PER_BLOCK),
                "eligible_total": len(eligible),
                "start_index": offset + 1 if shown else 0,
                "end_index": offset + len(shown) if shown else 0,
            }
    return blocks


def _page_daily_blocks(
    blocks: list[dict[str, Any]], day: str, selected: str | None,
) -> list[dict[str, Any]]:
    """Attach date-preserving page links; keep overview vs single-block views."""
    if selected is not None:
        blocks = [block for block in blocks if block["key"] == selected]
    for block in blocks:
        paging = block["pagination"]
        if paging["page"] > paging["page_count"]:
            raise EventRadarQueryError("daily report page no longer exists")
        for name, step in (("previous_url", -1), ("next_url", 1)):
            page = paging["page"] + step
            paging[name] = (
                "/dashboard/events/daily?"
                + urlencode({"date": day, "block": block["key"], "page": page})
                if 1 <= page <= paging["page_count"] else ""
            )
    return blocks


def _cheap_daily_items(
    groups: list[dict[str, Any]], lookback: dict[str, tuple[dict[str, Any], ...]],
    *, group_keys: list[set[str]] | None = None,
) -> tuple[list[dict[str, Any]], dict[str, tuple[dict[str, Any], dict[str, Any]]], int]:
    """Classify every group, but defer source and association rendering."""
    items: list[dict[str, Any]] = []
    candidates: dict[str, tuple[dict[str, Any], dict[str, Any]]] = {}
    repeats = 0
    for position, group in enumerate(groups):
        state = _classify(
            group, lookback,
            headline_keys=None if group_keys is None else group_keys[position],
        )
        if state["block"] == "repeat":
            repeats += 1
            continue
        event_id = group["root_ids"][0]
        candidates[event_id] = (group, state)
        items.append({
            "event_id": event_id,
            "block": state["block"],
            "correction_kind": (
                "denial" if state["denials"] else
                "request" if state["corrections"] else ""
            ),
            "priority": group["priority"],
            "latest_at": max(item["observed_at"] for item in group["today"]),
        })
    return items, candidates, repeats


def _materialize_daily_blocks(
    conn: sqlite3.Connection,
    blocks: list[dict[str, Any]],
    candidates: dict[str, tuple[dict[str, Any], dict[str, Any]]],
    members: dict[str, set[str]],
    cutoff: int,
    *, primary_source_ids: dict[str, str] | None = None,
) -> None:
    """Load sources and associations only for cards on the selected page."""
    shown = [item for block in blocks for item in block["entries"]]
    shown_members = {
        member
        for item in shown
        for root in candidates[item["event_id"]][0]["root_ids"]
        for member in members.get(root, {root})
    }
    associations = _load_associations(conn, shown_members, cutoff) if shown_members else {}
    known_names = _load_security_names(conn) if associations else frozenset()
    for block in blocks:
        views: list[dict[str, Any]] = []
        for item in block["entries"]:
            group, state = candidates[item["event_id"]]
            group["associations"] = [
                row for root in group["root_ids"]
                for member in members.get(root, {root})
                for row in associations.get(member, [])
            ]
            views.append(_item_view(
                group, state, known_names,
                primary_source_id=(primary_source_ids or {}).get(item["event_id"], ""),
            ))
        block["entries"] = views


def _freshness(coverage: dict[str, int | None], window: dict[str, Any]) -> dict[str, Any]:
    latest = coverage["latest_at"]
    if latest is None:
        return {"latest_label": INSUFFICIENT_LABEL, "age_label": "", "stale": True}
    age = max(0, window["cutoff"] - latest)
    hours, minutes = divmod(age // 60, 60)
    return {
        "latest_label": f"{_clock_label(latest)}（北京时间）",
        "age_label": f"距截止 {hours} 小时 {minutes} 分钟" if hours else f"距截止 {minutes} 分钟",
        "stale": age > STALE_AFTER_SECONDS,
    }


def _report_header(day: date, window: dict[str, Any], now: int) -> dict[str, Any]:
    today = datetime.fromtimestamp(now, REPORT_TZ).date()
    cutoff_label = _clock_label(window["cutoff"], "%Y-%m-%d %H:%M:%S")
    return {
        "date": day.isoformat(),
        "prev_date": (day - timedelta(days=1)).isoformat(),
        "next_date": "" if day >= today else (day + timedelta(days=1)).isoformat(),
        "min_date": (today - timedelta(days=MAX_HISTORY_DAYS)).isoformat(),
        "max_date": today.isoformat(),
        "is_today": day == today,
        "cutoff": window["cutoff"],
        "cutoff_label": (
            f"截止 {cutoff_label}（北京时间"
            + ("，当日未结束）" if window["partial"] else "）")
        ),
        "generated_at_label": f"生成于 {_clock_label(now, '%Y-%m-%d %H:%M:%S')}（北京时间）",
        "coverage_label": (
            "覆盖范围：已入库且通过公开评分路径的 RSS 原文标题；事件身份为原文链接，"
            "仅按规范化标题完全一致合并，不做跨标题关联；未入库的来源不在范围内"
        ),
        "is_historical": day != today,
        "historical_note": "" if day == today else (
            f"{HISTORICAL_VIEW_LABEL}：来源与合并关系按当时的入库与合并时间还原；"
            "事件标题与事件评分为当前值，未保存当日版本"
        ),
        "notes": (
            "每个事件只归入一个板块；同一标题的重复报道合并展示，报道数量不代表重要程度。",
            "来源立场为采集默认值，不等于事实核验；否认/更正与要求澄清均按标题措辞规则识别，"
            "要求澄清/问询不等于当事方否认。",
            "只有当天首次出现的内容才算当天的变化；此前已出现过的否认/更正只作背景。",
            f"{LINKAGE_LIMIT_NOTE}。",
            "来源发布方按媒体计：已知媒体按共用的来源表识别，其余按文章链接的注册域名识别，"
            "同一媒体的多个订阅源只算 1 个；媒体未能识别的链接不计入独立来源。",
            "原文点名公司与主题规则池关联分开列示，均不构成受益判断或推荐。",
            "本报告不评价投资胜率；事件进展不等于股价表现。",
        ),
    }


def prepare_daily_report(conn: sqlite3.Connection, day: date, now: int) -> tuple:
    """Classify bounded daily evidence without materializing display cards.

    The authenticated report and worker-published homepage use the same
    identity, correction, coverage and display rules.
    """
    window = report_window(day, now)
    coverage = _coverage(conn, window["cutoff"])
    today, truncated = _load_window_evidence(
        conn, window["start"], window["cutoff"], MAX_DAY_EVIDENCE_ROWS
    )
    lookback_rows, lookback_truncated = _load_window_evidence(
        conn, window["start"] - REPEAT_LOOKBACK_SECONDS, window["start"] - 1,
        MAX_LOOKBACK_EVIDENCE_ROWS,
    )
    roots = _resolve_roots(conn, {item["event_id"] for item in today}, window["cutoff"])
    members = _load_members(conn, set(roots.values()), window["cutoff"])
    all_members = {member for group in members.values() for member in group}
    groups = _build_groups(
        today, roots, members,
        _load_prior_evidence(conn, all_members, window["start"]),
        _load_event_rows(conn, set(roots.values())),
        {},
    )
    groups, folded = _merge_duplicate_groups(groups)
    # Include every classified group, including low-score/repeated groups and
    # historical identities. Event IDs or only the five visible home cards
    # would miss cross-root reposts and change correction/background semantics.
    # Reuse the per-group keys in classification too: do not normalize today's
    # titles twice when most historical headlines are relevant (or caches churn).
    group_keys = [_matching_headline_keys(group) for group in groups]
    headline_keys = {key for keys in group_keys for key in keys}
    lookback = _lookback_index(lookback_rows, headline_keys=headline_keys)
    items, candidates, repeats = _cheap_daily_items(groups, lookback, group_keys=group_keys)
    data_state, state_label = _data_state(
        coverage, window, len(today), truncated, lookback_truncated
    )
    metadata = {
        **_report_header(day, window, now),
        "data_state": data_state,
        "data_state_label": state_label,
        "freshness": _freshness(coverage, window),
        "evidence_count": len(today),
        "event_group_count": len(groups),
        "duplicate_groups_folded": folded,
        "repeat_event_count": repeats,
        "display_min_priority": DISPLAY_MIN_PRIORITY,
        "overview_url": f"/dashboard/events/daily?date={day.isoformat()}",
    }
    return metadata, items, candidates, members


def build_daily_report(
    conn: sqlite3.Connection, day: date, now: int, *, block=None, page=1,
) -> dict[str, Any]:
    """Aggregate one Beijing day from a read-only snapshot (no writes)."""
    metadata, items, candidates, members = prepare_daily_report(conn, day, now)
    blocks = _assemble_blocks(
        items, metadata["data_state"], metadata["data_state_label"],
        block_pages={block: page} if block else {},
    )
    blocks = _page_daily_blocks(blocks, day.isoformat(), block)
    _materialize_daily_blocks(conn, blocks, candidates, members, metadata["cutoff"])
    return {
        **metadata,
        "blocks": blocks,
        "pagination_block": block,
        "pagination_notice": (
            "每次翻页按当前已有记录重建，排序可能变化；这不是固定快照。"
            if block else
            "固定快照不存在。首屏每板块最多 20 条；其余通过查看更多进入单板块分页。"
        ),
    }


def load_daily_report(
    date_value: object = None, *, now: int | None = None, block=None, page=None,
) -> dict[str, Any]:
    """Open one read-only snapshot and build the report for ``date_value``."""
    current = int(time.time()) if now is None else int(now)
    try:
        selected, number = parse_report_paging(block, page)
        if selected is not None and (
            not isinstance(date_value, str)
            or not re.fullmatch(r"\d{4}-\d{2}-\d{2}", date_value)
        ):
            raise ValueError("paging requires an explicit date")
    except ValueError as exc:
        raise EventRadarQueryError("invalid daily report paging") from exc
    day = parse_report_date(date_value, now=current)
    try:
        with get_read_only_connection() as conn:
            conn.execute("BEGIN")
            try:
                return build_daily_report(
                    conn, day, current,
                    block=selected, page=number if selected else 1,
                )
            finally:
                if conn.in_transaction:
                    conn.rollback()
    except sqlite3.Error as exc:
        logger.warning("Event daily report read failed: %s", type(exc).__name__)
        raise EventRadarUnavailableError("event daily report is unavailable") from exc


__all__ = [
    "BLOCK_LABELS",
    "BLOCK_ORDER",
    "DISPLAY_MIN_PRIORITY",
    "INSUFFICIENT_LABEL",
    "NO_CHANGE_LABEL",
    "build_daily_report",
    "load_daily_report",
    "parse_report_date",
    "report_window",
]
