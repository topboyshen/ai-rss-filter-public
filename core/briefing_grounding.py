"""Deterministic grounding for LLM-owned Briefing commentary.

The helpers in this module are deliberately pure.  They do not read config,
call a model, or fetch evidence.  Callers must pass source-owned evidence text
(normally ``raw_title`` or a frozen excerpt) and a deterministic fallback.

Grounding is evaluated against one evidence item at a time.  Hits from two
different articles are never combined, which prevents a model from splicing a
number from one source with a policy or entity from another.
"""

from __future__ import annotations

import copy
import re
import unicodedata
from collections.abc import Iterable, Mapping
from typing import Any, Dict, List, Optional, Sequence, Tuple


COMMENTARY_FIELDS = (
    "insight",
    "why_it_matters",
    "market_impact",
)
DEFAULT_MIN_DISTINCT_HITS = 2
DEFAULT_REQUIRE_STRONG_HIT = True

# These words are too broad to establish that a particular factual claim is
# supported.  They are removed before ordinary lexical coverage is measured.
# Strong anchors (numbers, named policies, etc.) are extracted first.
_GENERIC_TERMS = tuple(sorted({
    "市场影响", "市场表现", "市场预期", "后续影响", "相关政策",
    "值得关注", "需要关注", "持续关注", "可能影响", "产生影响",
    "市场", "影响", "政策", "资金", "数据", "行业", "板块", "公司",
    "企业", "相关", "方面", "情况", "当前", "今日", "昨日", "近期",
    "目前", "后续", "未来", "表示", "认为", "预计", "可能", "有望",
    "关注", "风险", "机会", "重要", "显著", "持续", "进一步", "主要",
    "整体", "一定", "相对", "明显", "变化", "消息", "事件", "情报",
    "利好", "利空", "看多", "看空", "中性", "带来", "形成", "推动",
    "提升", "改善", "增强", "作用", "层面", "领域", "部分", "其中",
}, key=len, reverse=True))
# Fact canonicalization may discard grammatical particles, but must retain
# tense, modality, alternatives, conditions, and conjunctions.  In
# particular, deleting ``将/或/已/正/仍`` made materially different claims
# collapse to the same source quote (for example ``已投产`` and ``正投产``).
_FUNCTION_WORD_RE = re.compile(r"(?:的|了)")
_HAN_RUN_RE = re.compile(r"[\u3400-\u4dbf\u4e00-\u9fff]{2,}")
_GENERIC_ENGLISH_TERMS = {
    "ai", "api", "company", "data", "etf", "fund", "funds", "industry",
    "llm", "market", "news", "policy", "report", "risk", "sector", "share",
    "shares", "stock", "stocks", "usd", "rmb", "cny", "hkd", "sh", "sz",
    "bj",
}

_NUMBER_BODY = r"[+-]?\d[\d,]*(?:\.\d+)?"
_DATE_PATTERNS = (
    re.compile(
        r"(?<!\d)(?P<year>(?:19|20)\d{2})"
        r"(?:年|[-/.])(?P<month>\d{1,2})"
        r"(?:(?:月|[-/.])(?P<day>\d{1,2})日?)?(?!\d)"
    ),
    re.compile(r"(?<!\d)(?P<month>\d{1,2})月(?P<day>\d{1,2})日"),
    re.compile(r"(?<!\d)(?P<year>(?:19|20)\d{2})年"),
)
_POLICY_RE = re.compile(r"《(?P<name>[^》\r\n]{2,100})》")
_PERCENT_RE = re.compile(
    rf"(?<![\d.])(?P<value>{_NUMBER_BODY})\s*"
    r"(?P<unit>%|个百分点|个基点|基点|bps?|BP[S]?)(?![A-Za-z])",
    re.IGNORECASE,
)
_MONEY_RE = re.compile(
    rf"(?:"
    rf"(?P<symbol>[$¥￥€£])\s*(?P<symbol_value>{_NUMBER_BODY})"
    rf"(?:\s*(?P<symbol_scale>trillion|billion|million|"
    rf"万亿|千亿|百亿|亿|千万|百万|万|千))?"
    rf"|"
    rf"(?P<value>{_NUMBER_BODY})\s*"
    rf"(?P<scale>万亿|千亿|百亿|亿|千万|百万|万|千)?\s*"
    rf"(?P<currency>万亿元|亿元|万元|美元|港元|欧元|日元|英镑|人民币|元|"
    rf"USD|RMB|CNY|HKD|EUR|JPY|GBP)"
    rf")",
    re.IGNORECASE,
)
_CODE_RE = re.compile(r"(?<!\d)\d{6}(?!\d)")
_ENGLISH_ENTITY_RE = re.compile(
    r"(?<![A-Za-z0-9])(?P<entity>[A-Za-z][A-Za-z0-9&.+-]{1,})(?![A-Za-z0-9])"
)
_NUMBER_RE = re.compile(rf"(?<![A-Za-z0-9.]){_NUMBER_BODY}(?![A-Za-z0-9.])")
_NEGATION_RE = re.compile(
    r"(?:否认|驳斥|取消|终止|并未|尚未|未曾|未能|未获|未签|未完成|没有|不会|"
    r"\bnot\b|\bno\b|\bnever\b|\bden(?:y|ies|ied)\b|\bcancel(?:s|led)?\b)",
    re.IGNORECASE,
)
_HEDGE_RE = re.compile(
    r"(?:据悉|消息称|传闻|拟议|拟将|即将|或将|或|拟|将|计划|考虑|寻求|可能|有望|预计|尚待|"
    r"意向|洽谈|\breportedly\b|\bplans?\s+to\b|\bconsidering\b|\bseeking\b|"
    r"\bmay\b|\bmight\b|\bcould\b|\bexpected\b|\bpending\b|\bproposed\b)",
    re.IGNORECASE,
)
_CONDITIONAL_RE = re.compile(
    r"(?:如果|假如|假设|一旦|除非|前提是|倘若|若(?!干)|"
    r"\bif\b|\bunless\b|\bprovided\s+that\b|\bassum(?:e|ing)\b)",
    re.IGNORECASE,
)
_QUESTION_RE = re.compile(
    r"(?:[?？]|是否|能否|会否|真相如何|是真的吗|属实吗|何时|为何)",
    re.IGNORECASE,
)
_RUMOR_RE = re.compile(
    r"(?:网传|传闻|传言|据传|知情人士|消息人士|未经(?:官方)?确认|未经证实|"
    r"传出消息|有报道称|据报道|报道称|所谓|"
    r"\brumou?rs?\b|\brumou?red\b|\bunverified\b)",
    re.IGNORECASE,
)
_DEBUNK_RE = re.compile(
    r"(?:否认|驳斥|澄清|辟谣|不属实|不实|误读|假消息|虚假消息|谣言|"
    r"尚无证据表明|"
    r"无法证实|说法不准确|消息存疑|"
    r"\bfake\s+news\b|\bfalse\s+reports?\b|\bdebunk(?:ed|ing)?\b)",
    re.IGNORECASE,
)
_FACT_PREDICATES = (
    "签署", "宣布", "发布", "出台", "通过", "批准", "开展", "实施", "落地",
    "收购", "出售", "投资", "投入", "建设", "扩建", "增产", "减产", "增持",
    "减持", "回购", "上涨", "下跌", "增长", "下降", "增加", "减少", "上修",
    "下修", "终止", "取消", "否认", "签约", "中标", "开工", "投产",
    "signed", "announced", "published", "approved", "acquired", "invested",
    "increased", "decreased", "denied", "cancelled", "terminated",
)


def _normalize_text(value: object) -> str:
    return unicodedata.normalize("NFKC", str(value or "")).casefold().strip()


def _compact_anchor_value(value: object) -> str:
    text = _normalize_text(value).replace(",", "")
    return re.sub(r"\s+", "", text)


def _overlaps(span: Tuple[int, int], occupied: Sequence[Tuple[int, int]]) -> bool:
    start, end = span
    return any(start < other_end and other_start < end for other_start, other_end in occupied)


def _canonical_date(match: re.Match[str]) -> str:
    year = match.groupdict().get("year")
    month = match.groupdict().get("month")
    day = match.groupdict().get("day")
    if year and month and day:
        return f"{int(year):04d}-{int(month):02d}-{int(day):02d}"
    if year and month:
        return f"{int(year):04d}-{int(month):02d}"
    if month and day:
        return f"{int(month):02d}-{int(day):02d}"
    return str(int(year)) if year else _compact_anchor_value(match.group(0))


def _canonical_percent(match: re.Match[str]) -> str:
    value = _compact_anchor_value(match.group("value"))
    unit = _compact_anchor_value(match.group("unit"))
    unit = {"bp": "基点", "bps": "基点"}.get(unit, unit)
    return f"{value}{unit}"


def _canonical_money(match: re.Match[str]) -> str:
    if match.groupdict().get("symbol"):
        parts = (
            match.group("symbol"),
            match.group("symbol_value"),
            match.groupdict().get("symbol_scale") or "",
        )
    else:
        parts = (
            match.group("value"),
            match.groupdict().get("scale") or "",
            match.group("currency"),
        )
    return "".join(_compact_anchor_value(part) for part in parts)


def _extract_strong_anchors(text: object) -> List[Dict[str, Any]]:
    """Return non-overlapping, normalized high-information claim anchors."""
    normalized = _normalize_text(text)
    anchors: List[Dict[str, Any]] = []
    occupied: List[Tuple[int, int]] = []

    def add(kind: str, match: re.Match[str], value: str) -> None:
        span = match.span()
        if _overlaps(span, occupied):
            return
        anchors.append({
            "kind": kind,
            "value": value,
            "token": f"{kind}:{value}",
            "span": span,
        })
        occupied.append(span)

    # More specific anchors win over their numeric or English substrings.
    for match in _POLICY_RE.finditer(normalized):
        add("policy", match, _compact_anchor_value(match.group("name")))
    for pattern in _DATE_PATTERNS:
        for match in pattern.finditer(normalized):
            add("date", match, _canonical_date(match))
    for match in _PERCENT_RE.finditer(normalized):
        add("percent", match, _canonical_percent(match))
    for match in _MONEY_RE.finditer(normalized):
        add("money", match, _canonical_money(match))
    for match in _CODE_RE.finditer(normalized):
        add("code", match, match.group(0))
    for match in _ENGLISH_ENTITY_RE.finditer(normalized):
        entity = _compact_anchor_value(match.group("entity"))
        if entity not in _GENERIC_ENGLISH_TERMS:
            add("entity", match, entity)
    for match in _NUMBER_RE.finditer(normalized):
        add("number", match, _compact_anchor_value(match.group(0)))

    anchors.sort(key=lambda item: (item["span"][0], item["span"][1]))
    return anchors


def extract_strong_claim_tokens(text: object) -> Tuple[str, ...]:
    """Expose normalized strong tokens for diagnostics and focused tests."""
    return tuple(anchor["token"] for anchor in _extract_strong_anchors(text))


def _mask_spans(text: str, spans: Sequence[Tuple[int, int]]) -> str:
    if not spans:
        return text
    chars = list(text)
    for start, end in spans:
        chars[start:end] = " " * (end - start)
    return "".join(chars)


def _content_runs(text: object, anchors: Sequence[Dict[str, Any]]) -> List[str]:
    normalized = _normalize_text(text)
    normalized = _mask_spans(
        normalized,
        [tuple(anchor["span"]) for anchor in anchors],
    )
    for term in _GENERIC_TERMS:
        normalized = normalized.replace(term, " ")
    normalized = _FUNCTION_WORD_RE.sub(" ", normalized)
    return [match.group(0) for match in _HAN_RUN_RE.finditer(normalized)]


def _maximal_common_spans(left: str, right: str) -> List[Tuple[int, int, str]]:
    """Find disjoint maximal common Han spans, never counting n-gram overlap."""
    candidates: List[Tuple[int, int, int, int, str]] = []
    previous = [0] * (len(right) + 1)
    for left_index, left_char in enumerate(left, start=1):
        current = [0] * (len(right) + 1)
        for right_index, right_char in enumerate(right, start=1):
            if left_char != right_char:
                continue
            length = previous[right_index - 1] + 1
            current[right_index] = length
            if length >= 2:
                candidates.append((
                    left_index - length,
                    left_index,
                    right_index - length,
                    right_index,
                    left[left_index - length:left_index],
                ))
        previous = current

    # Longest spans first.  Once selected, shorter overlapping n-grams do not
    # inflate the distinct-hit count.
    selected: List[Tuple[int, int, int, int, str]] = []
    for candidate in sorted(
        candidates,
        key=lambda item: (-len(item[4]), item[0], item[2], item[4]),
    ):
        if any(
            _overlaps((candidate[0], candidate[1]), [(item[0], item[1])])
            or _overlaps((candidate[2], candidate[3]), [(item[2], item[3])])
            for item in selected
        ):
            continue
        selected.append(candidate)
    selected.sort(key=lambda item: (item[0], item[2]))
    return [(item[0], item[1], item[4]) for item in selected]


def _lexical_hits(
    claim: object,
    evidence: object,
    claim_anchors: Sequence[Dict[str, Any]],
    evidence_anchors: Sequence[Dict[str, Any]],
) -> List[str]:
    claim_runs = _content_runs(claim, claim_anchors)
    evidence_runs = _content_runs(evidence, evidence_anchors)
    hits: List[str] = []
    seen = set()
    for claim_run in claim_runs:
        candidates: List[Tuple[int, int, str]] = []
        for evidence_run in evidence_runs:
            candidates.extend(_maximal_common_spans(claim_run, evidence_run))
        selected_claim_spans: List[Tuple[int, int]] = []
        for start, end, span in sorted(
            candidates,
            key=lambda item: (-(item[1] - item[0]), item[0], item[2]),
        ):
            if span in seen or _overlaps((start, end), selected_claim_spans):
                continue
            seen.add(span)
            selected_claim_spans.append((start, end))
            hits.append(f"text:{span}")
    return hits


def _coerce_evidence_texts(evidence_texts: object) -> Tuple[str, ...]:
    if isinstance(evidence_texts, str):
        candidates: Iterable[object] = (evidence_texts,)
    elif isinstance(evidence_texts, Iterable):
        candidates = evidence_texts
    else:
        candidates = ()
    return tuple(
        str(value).strip()
        for value in candidates
        if isinstance(value, str) and value.strip()
    )


def _compact_fact_text(value: object) -> str:
    normalized = _normalize_text(value)
    anchors = _extract_strong_anchors(normalized)
    parts = []
    cursor = 0
    for anchor in anchors:
        start, end = anchor["span"]
        parts.append(normalized[cursor:start])
        parts.append(str(anchor["token"]))
        cursor = end
    parts.append(normalized[cursor:])
    canonical = _FUNCTION_WORD_RE.sub("", "".join(parts))
    return re.sub(r"[^0-9a-z\u3400-\u4dbf\u4e00-\u9fff]+", "", canonical)


def _is_source_quote(claim: object, evidence: object) -> bool:
    claim_text = _compact_fact_text(claim)
    evidence_text = _compact_fact_text(evidence)
    return len(claim_text) >= 8 and claim_text in evidence_text


def _has_fact_relation(
    claim: object,
    evidence: object,
    lexical_hits: Sequence[str],
) -> bool:
    if any(
        token.startswith("text:") and len(token.removeprefix("text:")) >= 4
        for token in lexical_hits
    ):
        return True
    claim_text = _normalize_text(claim)
    evidence_text = _normalize_text(evidence)
    return any(
        predicate in claim_text and predicate in evidence_text
        for predicate in _FACT_PREDICATES
    )


def _source_qualification_conflict(claim: object, evidence: object) -> str:
    """Return the source qualifier omitted by an affirmative claim.

    A source can mention all the same entities, amounts, and predicates while
    still stating only a condition, question, rumor, or debunking.  Such a
    source must never prove an unqualified affirmative sentence.  Matching
    the qualifier in the claim is intentionally required; this is a local,
    fail-closed rule rather than a semantic guess.
    """

    claim_text = _normalize_text(claim)
    evidence_text = _normalize_text(evidence)
    checks = (
        ("debunk", _DEBUNK_RE),
        ("rumor", _RUMOR_RE),
        ("conditional", _CONDITIONAL_RE),
        ("question", _QUESTION_RE),
    )
    for label, pattern in checks:
        if pattern.search(evidence_text) and not pattern.search(claim_text):
            return label
    return ""


def _validate_options(
    min_distinct_hits: object,
    require_strong_hit: object,
) -> Tuple[int, bool]:
    if isinstance(min_distinct_hits, bool) or not isinstance(min_distinct_hits, int):
        raise ValueError("min_distinct_hits must be an integer")
    if min_distinct_hits < 1:
        raise ValueError("min_distinct_hits must be at least 1")
    if not isinstance(require_strong_hit, bool):
        raise ValueError("require_strong_hit must be a boolean")
    return min_distinct_hits, require_strong_hit


def evaluate_commentary_grounding(
    commentary: object,
    evidence_texts: object,
    *,
    min_distinct_hits: int = DEFAULT_MIN_DISTINCT_HITS,
    require_strong_hit: bool = DEFAULT_REQUIRE_STRONG_HIT,
) -> Dict[str, Any]:
    """Evaluate a comment against each evidence item independently.

    A passing evidence item must contain *every* strong token claimed by the
    commentary and enough distinct anchors in total.  Ordinary lexical spans
    exclude generic financial words.  The returned evidence index refers to
    the caller-provided, non-empty evidence sequence.
    """
    min_distinct_hits, require_strong_hit = _validate_options(
        min_distinct_hits,
        require_strong_hit,
    )
    claim = str(commentary or "").strip()
    evidence = _coerce_evidence_texts(evidence_texts)
    claim_anchors = _extract_strong_anchors(claim)
    claim_tokens = tuple(anchor["token"] for anchor in claim_anchors)
    base = {
        "grounded": False,
        "evidence_index": None,
        "distinct_hits": (),
        "strong_claim_tokens": claim_tokens,
        "matched_strong_tokens": (),
        "fact_relation_hit": False,
        "source_qualification": "",
    }
    if not claim:
        return {**base, "reason": "empty_commentary"}
    if not evidence:
        return {**base, "reason": "no_evidence"}
    best: Optional[Dict[str, Any]] = None
    for evidence_index, evidence_text in enumerate(evidence):
        evidence_anchors = _extract_strong_anchors(evidence_text)
        evidence_tokens = {anchor["token"] for anchor in evidence_anchors}
        matched_strong = tuple(
            token for token in claim_tokens if token in evidence_tokens
        )
        missing_strong = tuple(
            token for token in claim_tokens if token not in evidence_tokens
        )
        lexical_hits = _lexical_hits(
            claim,
            evidence_text,
            claim_anchors,
            evidence_anchors,
        )
        source_quote = _is_source_quote(claim, evidence_text)
        quote_hits = ("source_quote",) if source_quote else ()
        distinct_hits = tuple(dict.fromkeys((*matched_strong, *quote_hits, *lexical_hits)))
        strong_requirement_met = bool(claim_tokens) or source_quote
        polarity_conflict = bool(_NEGATION_RE.search(_normalize_text(claim))) != bool(
            _NEGATION_RE.search(_normalize_text(evidence_text))
        )
        modality_conflict = (
            bool(_HEDGE_RE.search(_normalize_text(evidence_text)))
            and not bool(_HEDGE_RE.search(_normalize_text(claim)))
        )
        source_qualification = _source_qualification_conflict(claim, evidence_text)
        fact_relation_hit = _has_fact_relation(claim, evidence_text, lexical_hits)
        candidate = {
            "grounded": (
                not missing_strong
                and (not require_strong_hit or strong_requirement_met)
                and not source_qualification
                and not polarity_conflict
                and not modality_conflict
                and source_quote
                and len(distinct_hits) >= min_distinct_hits
                and fact_relation_hit
            ),
            "evidence_index": evidence_index,
            "distinct_hits": distinct_hits,
            "strong_claim_tokens": claim_tokens,
            "matched_strong_tokens": matched_strong,
            "fact_relation_hit": fact_relation_hit,
            "source_qualification": source_qualification,
            "reason": "grounded",
        }
        if source_qualification:
            candidate["reason"] = "source_qualification_conflict"
        elif missing_strong:
            candidate["reason"] = "strong_anchor_mismatch"
        elif require_strong_hit and not strong_requirement_met:
            candidate["reason"] = "strong_anchor_required"
        elif polarity_conflict:
            candidate["reason"] = "polarity_conflict"
        elif modality_conflict:
            candidate["reason"] = "modality_conflict"
        elif not source_quote:
            candidate["reason"] = "source_quote_required"
        elif len(distinct_hits) < min_distinct_hits:
            candidate["reason"] = "insufficient_distinct_hits"
        elif not fact_relation_hit:
            candidate["reason"] = "fact_relation_required"
        if candidate["grounded"]:
            return candidate
        if best is None or (
            len(candidate["matched_strong_tokens"]),
            len(candidate["distinct_hits"]),
            int(candidate["fact_relation_hit"]),
        ) > (
            len(best["matched_strong_tokens"]),
            len(best["distinct_hits"]),
            int(best["fact_relation_hit"]),
        ):
            best = candidate

    return best or {**base, "reason": "no_evidence"}


def _normalized_policy(policy: Optional[Mapping[str, Any]]) -> Dict[str, Any]:
    if policy is None:
        raw: Mapping[str, Any] = {}
    elif isinstance(policy, Mapping):
        raw = policy
    else:
        raise ValueError("policy must be a mapping")
    enabled = raw.get("enabled", True)
    if not isinstance(enabled, bool):
        raise ValueError("policy.enabled must be a boolean")
    min_distinct_hits, require_strong_hit = _validate_options(
        raw.get("min_distinct_hits", DEFAULT_MIN_DISTINCT_HITS),
        raw.get("require_strong_hit", DEFAULT_REQUIRE_STRONG_HIT),
    )
    return {
        "enabled": enabled,
        "min_distinct_hits": min_distinct_hits,
        "require_strong_hit": require_strong_hit,
    }


def ground_theme_commentary(
    theme: Mapping[str, Any],
    evidence_texts: object,
    policy: Optional[Mapping[str, Any]],
    fallback: Optional[Mapping[str, Any]],
) -> Dict[str, Any]:
    """Ground factual prose and keep the trade action wholly deterministic.

    The input mappings are never mutated.  When the policy is disabled, a
    deep copy identical in shape and value to ``theme`` is returned so callers
    can preserve the current production contract.  With grounding enabled,
    field-level diagnostics are attached under ``grounding``.
    """
    if not isinstance(theme, Mapping):
        raise ValueError("theme must be a mapping")
    if fallback is not None and not isinstance(fallback, Mapping):
        raise ValueError("fallback must be a mapping")
    normalized_policy = _normalized_policy(policy)
    result = copy.deepcopy(dict(theme))
    if not normalized_policy["enabled"]:
        return result

    deterministic = fallback or {}
    evidence = _coerce_evidence_texts(evidence_texts)
    field_states: Dict[str, Dict[str, Any]] = {}
    grounded_count = 0
    for field in COMMENTARY_FIELDS:
        evaluation = evaluate_commentary_grounding(
            result.get(field, ""),
            evidence,
            min_distinct_hits=normalized_policy["min_distinct_hits"],
            require_strong_hit=normalized_policy["require_strong_hit"],
        )
        if evaluation["grounded"]:
            status = "grounded"
            grounded_count += 1
        else:
            status = "fallback"
            result[field] = copy.deepcopy(deterministic.get(field, ""))
        field_states[field] = {
            "status": status,
            "reason": evaluation["reason"],
            "evidence_index": evaluation["evidence_index"],
            "distinct_hits": evaluation["distinct_hits"],
            "strong_claim_tokens": evaluation["strong_claim_tokens"],
            "matched_strong_tokens": evaluation["matched_strong_tokens"],
            "fact_relation_hit": evaluation["fact_relation_hit"],
            "source_qualification": evaluation["source_qualification"],
        }

    # Token overlap can support a factual clause, never a buy/sell command.
    # Production callers may supply a deterministic watch instruction; the
    # model-owned action is otherwise blanked.
    result["action"] = copy.deepcopy(deterministic.get("action", ""))
    field_states["action"] = {
        "status": "local_derived" if result["action"] else "hidden",
        "reason": "action_not_llm_owned",
        "evidence_index": None,
        "distinct_hits": (),
        "strong_claim_tokens": (),
        "matched_strong_tokens": (),
        "fact_relation_hit": False,
        "source_qualification": "",
    }

    if grounded_count == len(COMMENTARY_FIELDS):
        overall_status = "grounded_with_local_action"
    elif grounded_count:
        overall_status = "partial"
    else:
        overall_status = "fallback"
    result["grounding"] = {
        "status": overall_status,
        "fields": field_states,
        "min_distinct_hits": normalized_policy["min_distinct_hits"],
        "require_strong_hit": normalized_policy["require_strong_hit"],
    }
    return result
