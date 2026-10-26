"""Deterministic, read-side candidate assembly for Briefing.

This module deliberately has no dependency on Flask, the LLM client, or the
scoring strategy contract.  It only decides which already-scored articles may
enter a Briefing prompt.  Callers remain responsible for event deduplication
before invoking :func:`assemble_briefing_candidates`.
"""

from __future__ import annotations

import hashlib
import inspect
import json
import math
from collections import Counter
from dataclasses import dataclass
from typing import Any, Callable, Mapping, Optional, Sequence

from core.asset_mapping import classify_policy_region
from core.theme_rules import (
    article_primary_theme_text,
    canonical_theme_name,
    is_broad_macro_theme_tag,
    is_subscription_only_theme,
)


_POLICY_SCHEMA_VERSION = "briefing-assembly-v2"
_DEFAULT_POLICY = {
    "enabled": False,
    "max_themes": 4,
    "max_articles_per_theme": 3,
    "max_share_ratio": 0.4,
    "reserved_non_head_slots": 1,
}
_DEFAULT_CANDIDATE_MULTIPLIER = 4
_DEFAULT_GROUNDING_POLICY = {
    "enabled": False,
    "min_distinct_hits": 2,
    "require_strong_hit": True,
    "validate_direction": True,
    "on_fail": "deterministic_fallback",
}
_MAX_THEMES = 32
_MAX_ARTICLES_PER_THEME = 100
_MAX_RESERVED_NON_HEAD_SLOTS = 100


def _as_bool(value: Any, default: bool) -> bool:
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        normalized = value.strip().casefold()
        if normalized in {"1", "true", "yes", "on"}:
            return True
        if normalized in {"0", "false", "no", "off", ""}:
            return False
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        try:
            if math.isfinite(float(value)):
                return bool(value)
        except (TypeError, ValueError, OverflowError):
            pass
    return default


def _bounded_int(value: Any, default: int, minimum: int, maximum: int) -> int:
    try:
        parsed = int(value)
    except (TypeError, ValueError, OverflowError):
        parsed = default
    return max(minimum, min(maximum, parsed))


def _bounded_ratio(value: Any, default: float) -> float:
    try:
        parsed = float(value)
    except (TypeError, ValueError, OverflowError):
        return default
    if not math.isfinite(parsed):
        return default
    return max(0.0, min(1.0, parsed))


def normalize_briefing_policy(cfg: Optional[Mapping[str, Any]]) -> dict[str, Any]:
    """Return a bounded policy without mutating the supplied configuration.

    An absent or malformed ``briefing_assembly`` block is disabled.  This is
    the compatibility boundary: merely deploying this module cannot alter the
    current Briefing order or candidate set.
    """

    root = cfg if isinstance(cfg, Mapping) else {}
    raw = root.get("briefing_assembly")
    raw = raw if isinstance(raw, Mapping) else {}
    return {
        "enabled": _as_bool(raw.get("enabled"), _DEFAULT_POLICY["enabled"]),
        "max_themes": _bounded_int(
            raw.get("max_themes"),
            _DEFAULT_POLICY["max_themes"],
            1,
            _MAX_THEMES,
        ),
        "max_articles_per_theme": _bounded_int(
            raw.get("max_articles_per_theme"),
            _DEFAULT_POLICY["max_articles_per_theme"],
            1,
            _MAX_ARTICLES_PER_THEME,
        ),
        "max_share_ratio": _bounded_ratio(
            raw.get("max_share_ratio"),
            _DEFAULT_POLICY["max_share_ratio"],
        ),
        "reserved_non_head_slots": _bounded_int(
            raw.get("reserved_non_head_slots", raw.get("reserved_slots")),
            _DEFAULT_POLICY["reserved_non_head_slots"],
            0,
            _MAX_RESERVED_NON_HEAD_SLOTS,
        ),
    }


def briefing_candidate_multiplier(cfg: Optional[Mapping[str, Any]]) -> int:
    """Return the bounded read-side overfetch factor used before quotas."""

    root = cfg if isinstance(cfg, Mapping) else {}
    raw = root.get("briefing_assembly")
    raw = raw if isinstance(raw, Mapping) else {}
    return _bounded_int(
        raw.get("candidate_multiplier"),
        _DEFAULT_CANDIDATE_MULTIPLIER,
        1,
        10,
    )


def normalize_briefing_grounding_policy(
    cfg: Optional[Mapping[str, Any]],
) -> dict[str, Any]:
    """Normalize the post-LLM evidence policy without touching score config."""

    root = cfg if isinstance(cfg, Mapping) else {}
    assembly = root.get("briefing_assembly")
    assembly = assembly if isinstance(assembly, Mapping) else {}
    raw = assembly.get("grounding")
    raw = raw if isinstance(raw, Mapping) else {}
    on_fail = str(raw.get("on_fail") or _DEFAULT_GROUNDING_POLICY["on_fail"]).strip()
    if on_fail not in {"deterministic_fallback", "blank_commentary"}:
        on_fail = _DEFAULT_GROUNDING_POLICY["on_fail"]
    return {
        "enabled": _as_bool(raw.get("enabled"), _DEFAULT_GROUNDING_POLICY["enabled"]),
        "min_distinct_hits": _bounded_int(
            raw.get("min_distinct_hits"),
            _DEFAULT_GROUNDING_POLICY["min_distinct_hits"],
            1,
            20,
        ),
        "require_strong_hit": _as_bool(
            raw.get("require_strong_hit"),
            _DEFAULT_GROUNDING_POLICY["require_strong_hit"],
        ),
        "validate_direction": _as_bool(
            raw.get("validate_direction"),
            _DEFAULT_GROUNDING_POLICY["validate_direction"],
        ),
        "on_fail": on_fail,
    }


def _normalized_theme_rules(cfg: Optional[Mapping[str, Any]]) -> list[dict[str, Any]]:
    root = cfg if isinstance(cfg, Mapping) else {}
    raw_rules = root.get("briefing_themes")
    if not isinstance(raw_rules, (list, tuple)):
        return []

    rules: list[dict[str, Any]] = []
    for raw_rule in raw_rules:
        if not isinstance(raw_rule, Mapping):
            continue
        if bool(raw_rule.get("subscription_only")):
            continue
        raw_name = str(raw_rule.get("name") or "").strip()
        if not raw_name:
            continue
        raw_keywords = raw_rule.get("keywords") or []
        if isinstance(raw_keywords, str):
            raw_keywords = [raw_keywords]
        elif not isinstance(raw_keywords, (list, tuple, set)):
            raw_keywords = []
        keywords: list[str] = []
        seen: set[str] = set()
        for raw_keyword in raw_keywords:
            keyword = str(raw_keyword or "").strip()
            lookup = keyword.casefold()
            if not keyword or lookup in seen:
                continue
            seen.add(lookup)
            keywords.append(keyword)
        if keywords:
            rules.append({
                "name": canonical_theme_name(raw_name),
                "keywords": keywords,
            })
    return rules


def briefing_policy_fingerprint(cfg: Optional[Mapping[str, Any]]) -> str:
    """Fingerprint only Briefing assembly inputs, never scoring configuration."""

    payload = {
        "schema": _POLICY_SCHEMA_VERSION,
        "policy": normalize_briefing_policy(cfg),
        "candidate_multiplier": briefing_candidate_multiplier(cfg),
        "grounding": normalize_briefing_grounding_policy(cfg),
        # Configured rule order is intentional because the first match owns the
        # article's one primary theme.
        "briefing_themes": _normalized_theme_rules(cfg),
    }
    encoded = json.dumps(
        payload,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )
    return hashlib.sha256(encoded.encode("utf-8")).hexdigest()


def _default_primary_theme(
    article: Mapping[str, Any], cfg: Optional[Mapping[str, Any]]
) -> str:
    policy_region = classify_policy_region(dict(article))
    if policy_region == "foreign":
        return "海外宏观政策"
    if policy_region == "mixed":
        return "海外宏观与市场联动"
    explicit = str(article.get("briefing_theme") or "").strip()
    if explicit:
        return canonical_theme_name(explicit)
    # Theme ownership must be derived from publisher/source text plus an
    # existing structured tag.  ``logic`` and ``summary`` are model-owned and
    # therefore cannot decide which quota bucket an article enters.
    text = article_primary_theme_text(article, cfg).casefold()
    for rule in _normalized_theme_rules(cfg):
        if any(keyword.casefold() in text for keyword in rule["keywords"]):
            return str(rule["name"])

    tag = str(article.get("tag") or "").strip()
    if is_broad_macro_theme_tag(tag):
        return "国内宏观政策"
    if (
        tag
        and tag not in {"FastPass", "未知"}
        and not tag.isdigit()
        and not is_subscription_only_theme(cfg, tag)
    ):
        return canonical_theme_name(tag)
    return "其他高分情报"


def _invoke_theme_resolver(
    resolver: Callable[..., Any],
    article: Mapping[str, Any],
    cfg: Optional[Mapping[str, Any]],
) -> Any:
    """Support existing ``resolver(article, cfg=...)`` and simple test hooks."""

    try:
        signature = inspect.signature(resolver)
    except (TypeError, ValueError):
        return resolver(article)

    parameters = signature.parameters.values()
    if "cfg" in signature.parameters or any(
        parameter.kind == inspect.Parameter.VAR_KEYWORD
        for parameter in parameters
    ):
        return resolver(article, cfg=cfg)
    if any(
        parameter.kind == inspect.Parameter.VAR_POSITIONAL
        for parameter in parameters
    ):
        return resolver(article, cfg)
    positional = [
        parameter
        for parameter in parameters
        if parameter.kind
        in (inspect.Parameter.POSITIONAL_ONLY, inspect.Parameter.POSITIONAL_OR_KEYWORD)
    ]
    if len(positional) >= 2:
        return resolver(article, cfg)
    return resolver(article)


def _primary_theme(
    article: Mapping[str, Any],
    cfg: Optional[Mapping[str, Any]],
    resolver: Optional[Callable[..., Any]],
) -> str:
    value = (
        _invoke_theme_resolver(resolver, article, cfg)
        if resolver is not None
        else _default_primary_theme(article, cfg)
    )
    text = str(value or "").strip()
    return canonical_theme_name(text) if text else "其他高分情报"


def _score(article: Mapping[str, Any]) -> float:
    try:
        value = float(article.get("score") or 0)
    except (TypeError, ValueError, OverflowError):
        return 0.0
    return value if math.isfinite(value) else 0.0


def _timestamp(article: Mapping[str, Any]) -> int:
    try:
        return int(article.get("created_at") or article.get("time_ts") or 0)
    except (TypeError, ValueError, OverflowError):
        return 0


def _article_identity(article: Mapping[str, Any]) -> str:
    event_id = str(article.get("event_id") or "").strip()
    if event_id:
        return f"event:{event_id}"
    link = str(article.get("link") or "").strip()
    return f"link:{link}" if link else ""


@dataclass(frozen=True)
class _Candidate:
    article: Any
    input_index: int
    theme: str
    score: float
    created_at: int
    identity: str

    @property
    def rank(self) -> tuple[Any, ...]:
        # Identity is ascending to make ties deterministic across input order;
        # the original index remains the final stable tie-break for legacy rows.
        return (-self.score, -self.created_at, self.identity, self.input_index)


def _normalized_limit(value: Any, article_count: int) -> int:
    if value is None:
        return article_count
    try:
        parsed = int(value)
    except (TypeError, ValueError, OverflowError):
        return article_count
    return max(0, min(article_count, parsed))


def assemble_briefing_candidates(
    articles: Sequence[Mapping[str, Any]],
    cfg: Optional[Mapping[str, Any]],
    limit: Optional[int],
    theme_resolver: Optional[Callable[..., Any]] = None,
) -> tuple[list[Any], dict[str, Any]]:
    """Select a stable, theme-bounded Briefing corpus and return an audit.

    ``max_share_ratio`` is computed against the bounded eligible candidate
    pool, matching the configuration contract that one theme may occupy at
    most a share of the candidate pool.  It is not recomputed against the
    smaller final result, which would make sparse multi-theme corpora unstable.
    ``reserved_non_head_slots`` further bounds the leading theme while always
    retaining at least one head slot.

    Articles are neither copied nor mutated.  With the feature disabled the
    function also avoids calling ``theme_resolver`` and returns the original
    input order (bounded only by ``limit``).
    """

    source_articles = list(articles or [])
    selected_limit = _normalized_limit(limit, len(source_articles))
    policy = normalize_briefing_policy(cfg)
    fingerprint = briefing_policy_fingerprint(cfg)

    base_audit: dict[str, Any] = {
        "enabled": policy["enabled"],
        "policy": policy,
        "policy_fingerprint": fingerprint,
        "candidate_multiplier": briefing_candidate_multiplier(cfg),
        "grounding_policy": normalize_briefing_grounding_policy(cfg),
        "scope": "briefing_presentation_only",
        "input_count": len(source_articles),
        "requested_limit": selected_limit,
    }
    if not policy["enabled"]:
        selected = source_articles[:selected_limit]
        return selected, {
            **base_audit,
            "target_slots": selected_limit,
            "selected_count": len(selected),
            "head_theme": "",
            "eligible_themes": [],
            "selected_primary_themes": [],
            "theme_counts": {},
            "share_cap": None,
            "share_basis": selected_limit,
            "effective_reserved_non_head_slots": 0,
            "excluded": {
                "target_limit": len(source_articles) - len(selected),
                "theme_limit": 0,
                "theme_cap": 0,
            },
        }

    candidates: list[_Candidate] = []
    for index, article in enumerate(source_articles):
        mapping = article if isinstance(article, Mapping) else {}
        candidates.append(_Candidate(
            article=article,
            input_index=index,
            theme=_primary_theme(mapping, cfg, theme_resolver),
            score=_score(mapping),
            created_at=_timestamp(mapping),
            identity=_article_identity(mapping),
        ))
    ranked = sorted(candidates, key=lambda candidate: candidate.rank)

    eligible_themes: list[str] = []
    for candidate in ranked:
        if candidate.theme in eligible_themes:
            continue
        eligible_themes.append(candidate.theme)
        if len(eligible_themes) >= policy["max_themes"]:
            break
    eligible_lookup = set(eligible_themes)
    head_theme = eligible_themes[0] if eligible_themes else ""

    available_counts = Counter(
        candidate.theme for candidate in ranked if candidate.theme in eligible_lookup
    )
    per_theme_cap = policy["max_articles_per_theme"]
    pre_share_capacity = sum(
        min(available_counts[theme], per_theme_cap) for theme in eligible_themes
    )
    target_slots = min(selected_limit, pre_share_capacity)
    share_basis = min(
        selected_limit,
        sum(available_counts[theme] for theme in eligible_themes),
    )
    share_cap = (
        max(1, math.floor(share_basis * policy["max_share_ratio"]))
        if share_basis
        else 0
    )
    general_theme_cap = min(per_theme_cap, share_cap) if target_slots else 0

    non_head_capacity = sum(
        min(available_counts[theme], general_theme_cap)
        for theme in eligible_themes
        if theme != head_theme
    )
    effective_reserved = min(
        policy["reserved_non_head_slots"],
        non_head_capacity,
        max(0, target_slots - 1),
    )
    theme_caps = {
        theme: general_theme_cap for theme in eligible_themes
    }
    if head_theme:
        theme_caps[head_theme] = min(
            general_theme_cap,
            max(1, target_slots - effective_reserved) if target_slots else 0,
        )

    selected_candidates: list[_Candidate] = []
    selected_counts: Counter[str] = Counter()
    excluded = {"target_limit": 0, "theme_limit": 0, "theme_cap": 0}
    for candidate in ranked:
        if candidate.theme not in eligible_lookup:
            excluded["theme_limit"] += 1
            continue
        if selected_counts[candidate.theme] >= theme_caps[candidate.theme]:
            excluded["theme_cap"] += 1
            continue
        if len(selected_candidates) >= target_slots:
            excluded["target_limit"] += 1
            continue
        selected_candidates.append(candidate)
        selected_counts[candidate.theme] += 1

    selected = [candidate.article for candidate in selected_candidates]
    return selected, {
        **base_audit,
        "target_slots": target_slots,
        "selected_count": len(selected),
        "head_theme": head_theme,
        "eligible_themes": eligible_themes,
        "selected_primary_themes": [
            candidate.theme for candidate in selected_candidates
        ],
        "theme_counts": {
            theme: selected_counts[theme] for theme in eligible_themes
        },
        "share_cap": share_cap,
        "share_basis": share_basis,
        "effective_reserved_non_head_slots": effective_reserved,
        "excluded": excluded,
    }


__all__ = [
    "assemble_briefing_candidates",
    "briefing_candidate_multiplier",
    "briefing_policy_fingerprint",
    "normalize_briefing_grounding_policy",
    "normalize_briefing_policy",
]
