"""Shared deterministic theme names and subscription-slice rules."""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any, Mapping, Optional


_CANONICAL_THEME_ALIASES = {
    "国家级政策": "国家产业政策",
    "国家级产业政策": "国家产业政策",
    "国家产业政策": "国家产业政策",
    "ai算力": "AI 算力链",
    "ai算力链": "AI 算力链",
    "算力基建": "AI 算力链",
    "数字中国": "AI 算力链",
}

_BROAD_MACRO_TAG_PATTERNS = (
    "宏观放水", "宏观宽松", "宏观政策", "宏观流动性", "政策/流动性",
)

_MAX_THEME_IDENTITY_CHARS = 64
_MAX_THEME_DISPLAY_CHARS = 16


def _compact_theme_name(value: object) -> str:
    return re.sub(r"\s+", "", str(value or "").strip()).casefold()


def canonical_theme_name(value: Any) -> str:
    """Return the bounded, full theme identity used for grouping and lookup.

    Display truncation is intentionally handled by :func:`theme_display_name`.
    Mixing those two concepts made configured names longer than 16 characters
    unrequestable and caused distinct names with the same prefix to collide.
    """
    raw = str(value or "未分类").strip() or "未分类"
    alias = _CANONICAL_THEME_ALIASES.get(_compact_theme_name(raw))
    return str(alias or raw)[:_MAX_THEME_IDENTITY_CHARS]


def theme_display_name(value: Any, max_chars: int = _MAX_THEME_DISPLAY_CHARS) -> str:
    """Return a compact label without changing the theme's full identity."""
    try:
        limit = int(max_chars)
    except (TypeError, ValueError, OverflowError):
        limit = _MAX_THEME_DISPLAY_CHARS
    limit = max(1, min(_MAX_THEME_IDENTITY_CHARS, limit))
    return canonical_theme_name(value)[:limit]


@dataclass(frozen=True)
class ConfiguredTheme:
    """One configured, canonical subscription theme and its inclusion terms."""

    name: str
    terms: tuple[str, ...]


class InvalidThemeError(ValueError):
    """The request cannot be represented safely as a bounded feed label."""


def is_subscription_only_theme(
    cfg: Optional[Mapping[str, Any]], value: object
) -> bool:
    """Return whether ``value`` is a compatibility-only subscription name."""
    lookup = _compact_theme_name(value)
    if not lookup:
        return False
    for rule in (cfg or {}).get("briefing_themes", []) or []:
        if not isinstance(rule, Mapping) or not bool(rule.get("subscription_only")):
            continue
        configured = str(rule.get("name") or "").strip()
        if lookup in {
            _compact_theme_name(configured),
            _compact_theme_name(canonical_theme_name(configured)),
        }:
            return True
    return False


def is_broad_macro_theme_tag(value: object) -> bool:
    """Identify broad model labels that cannot own a factual theme bucket."""
    compact = re.sub(r"\s+", "", str(value or ""))
    return any(pattern in compact for pattern in _BROAD_MACRO_TAG_PATTERNS)


def article_primary_theme_text(
    article: Mapping[str, Any], cfg: Optional[Mapping[str, Any]]
) -> str:
    """Build primary-theme evidence without legacy union tags leaking in."""
    values = [article.get("title"), article.get("raw_title")]
    tag = str(article.get("tag") or "").strip()
    if (
        tag
        and not is_subscription_only_theme(cfg, tag)
        and not is_broad_macro_theme_tag(tag)
    ):
        values.append(tag)
    return " ".join(str(value or "") for value in values)


def resolve_configured_theme(
    cfg: Optional[dict], value: object
) -> Optional[ConfiguredTheme]:
    """Resolve a requested theme against ``briefing_themes``.

    A blank request means no theme filter. Unknown names raise ``KeyError`` so
    callers can distinguish them from omission.
    Duplicate canonical rules merge their keywords. Theme membership is
    intentionally non-exclusive: one article can match multiple subscriptions.
    """
    raw = str(value or "").strip()
    if not raw:
        return None
    if (
        len(raw) > 64
        or any(ord(ch) < 32 for ch in raw)
        or any(ch in {"\ufffe", "\uffff"} for ch in raw)
    ):
        raise InvalidThemeError("theme must be safe text of at most 64 characters")

    merged: dict[str, dict[str, object]] = {}
    for rule in (cfg or {}).get("briefing_themes", []) or []:
        if not isinstance(rule, dict):
            continue
        configured_name = str(rule.get("name") or "").strip()
        if not configured_name:
            continue
        canonical = canonical_theme_name(configured_name)
        key = _compact_theme_name(canonical)
        bucket = merged.setdefault(
            key,
            {"name": canonical, "terms": [], "lookup": set()},
        )
        raw_keywords = rule.get("keywords") or []
        if isinstance(raw_keywords, str):
            raw_keywords = [raw_keywords]
        elif not isinstance(raw_keywords, (list, tuple, set)):
            raw_keywords = []
        lookup = bucket["lookup"]
        terms = bucket["terms"]
        assert isinstance(lookup, set)
        assert isinstance(terms, list)
        for term in [canonical, configured_name, *raw_keywords]:
            text = str(term or "").strip()
            if not text or len(text) > 100:
                continue
            term_key = text.casefold()
            if term_key not in lookup:
                lookup.add(term_key)
                terms.append(text)

    direct_key = _compact_theme_name(raw)
    bucket = merged.get(direct_key)
    if bucket is None:
        # Only explicit aliases may bridge a request to another configured
        # name. Display labels are never whitelist keys because two complete
        # identities may deliberately share the same 16-character prefix.
        alias = _CANONICAL_THEME_ALIASES.get(direct_key)
        bucket = merged.get(_compact_theme_name(alias)) if alias else None
    if bucket is None:
        raise KeyError(raw)
    return ConfiguredTheme(
        name=str(bucket["name"]),
        terms=tuple(str(term) for term in bucket["terms"]),
    )
