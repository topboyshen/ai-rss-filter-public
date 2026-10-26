"""Presentation-only helpers for the homepage's news reading surface."""

import re

from scoring_provenance import NON_SUBSTANTIVE_SCORE_PATHS
from utils.helpers import escape_html, plain_text_from_markup


_PATH_LABELS = {
    "llm": "模型评分", "stage2": "模型二阶段评分",
    "fast_pass": "本地快速规则", "local_reject": "本地拒绝规则",
    "fallback_local": "本地兜底", "similarity": "相似结果复用",
    "radar": "雷达路径", "parse_error": "解析失败",
    "quality_quarantine": "质量隔离",
}


def render_evidence_context(entry: dict) -> str:
    """Show only persisted path metadata; do not infer per-field score ownership."""
    raw_title = str(entry.get("raw_title") or "")
    path = str(entry.get("score_path") or "").strip().casefold()
    if path in NON_SUBSTANTIVE_SCORE_PATHS:
        path_label = "系统兜底：保守中性"
    else:
        path_label = "记录的处理路径：" + _PATH_LABELS.get(path, "未知")
    title_label = "标题已记录" if raw_title.strip() else "标题缺失"
    lines = (
        title_label,
        "评分时使用的正文范围未记录",
        path_label,
        "当前展示分数与方向的逐项生成来源未确认；分数仅为阅读优先级",
        "标题点名仅说明提及，不支持投资结论；主题背景与规则池不是本文点名或受益证据",
    )
    return (
        '<details class="home-news-evidence" style="margin-top:8px;">'
        '<summary style="cursor:pointer;font-size:11px;">证据与处理范围</summary>'
        '<div style="margin-top:6px;font-size:11px;line-height:1.7;">'
        + "<br>".join(escape_html(line) for line in lines)
        + '</div></details>'
    )


def preferred_news_title(raw_title: str = "", title: str = "") -> str:
    """Clean persisted title candidates without clipping or inventing text."""
    def clean(value):
        text = plain_text_from_markup(value)
        text = re.sub(r'^\[⭐[\d.]+\s*[^\]]*\]\s*', '', text)
        return re.sub(r'\s*💡.*$', '', text).strip()

    raw_clean, title_clean = clean(raw_title), clean(title)
    if not raw_clean:
        return title_clean
    raw_stem, title_stem = raw_clean.removesuffix("…"), title_clean.removesuffix("…")
    if title_stem.startswith(raw_stem) and len(title_stem) > len(raw_stem):
        return title_clean
    return raw_clean


def _reading_owner_timestamp(entry: dict) -> int:
    """Mirror the deduper's forgiving timestamp rank after card preparation."""
    try:
        return int(entry.get("created_at") or entry.get("time") or entry.get("time_ts") or 0)
    except (TypeError, ValueError, OverflowError):
        return 0


def _same_reading_identity(candidate: dict, selected: dict) -> bool:
    """Legacy title groups cross links; unresolved persisted IDs must not."""
    source_id = str(candidate.get("event_id") or "")
    selected_id = str(selected.get("event_id") or "")
    if selected_id:
        return source_id == selected_id
    if not source_id:
        return True
    return candidate.get("link") == selected.get("link")


def _bound_score_source_version(entry: dict, field: str, value) -> str:
    """An unbound or malformed cached sidecar cannot establish provenance."""
    sources = entry.get("_score_sources")
    source = sources.get(field) if isinstance(sources, dict) else None
    if not isinstance(source, dict) or "value" not in source or source["value"] != value:
        return ""
    return str(source.get("version") or "").strip()


def _restore_score_sources(entry: dict, owners: list[dict]) -> dict:
    """Keep display-score ownership and restore swapped semantic provenance.

    Dedupe preserves ``importance_score`` from its first copy but replaces
    direction/logic with its semantic owner. Never copy a whole sidecar from
    the freshest publisher or treat conflicting owner identities as current.
    """
    sources = dict(entry.get("_score_sources") or {})
    for field in ("direction", "logic"):
        identities = {
            _bound_score_source_version(owner, field, owner.get(field))
            for owner in owners if owner.get("logic") == entry.get("logic")
        }
        sources[field] = {
            "version": next(iter(identities)) if len(identities) == 1 else "",
            "value": entry.get(field),
        }
    return sources


def _score_contract_state(entry: dict, current_version: str) -> str:
    """Compare full identities bound to the supplied, actually displayed values."""
    if not current_version:
        return "unknown"
    versions = []
    values = {"score": entry.get("importance_score", entry.get("score", 0)),
              "direction": entry.get("direction"), "logic": entry.get("logic")}
    for field, value in values.items():
        version = _bound_score_source_version(entry, field, value)
        if not version:
            return "unknown"
        versions.append(version)
    return "current" if all(version == current_version for version in versions) else "historical"


def attach_score_contract_labels(entries: list[dict], cfg: dict) -> list[dict]:
    """Prepare pure display metadata; do not change scoring or public selection."""
    current = str((cfg or {}).get("scoring_strategy_version") or "").strip()
    return [{**entry, "_score_contract_current_version": current,
             "_score_contract_state": _score_contract_state(entry, current)}
            for entry in entries]


def score_contract_label(entry: dict) -> str:
    """Missing cached provenance is unknown, never a default current result."""
    labels = {"current": "当前评分契约", "historical": "历史评分，未按当前契约重算"}
    return "评分来源：" + labels.get(
        _score_contract_state(entry, str(entry.get("_score_contract_current_version") or "")),
        "评分契约未记录，当前有效性未知",
    )


def restore_reading_titles(selected: list[dict], candidates: list[dict]) -> list[dict]:
    """Reattach full titles to their scoring owner after unchanged deduplication.

    Dedupe owns compact ``title`` and scoring fields, while its freshest copy
    owns the link/source/time. A display-only sidecar must follow the scoring
    owner rather than whichever copy happened to be seen first. This lookup
    changes no selection, identity, mapping, or stored input fields.
    """
    semantic_fields = ("title", "raw_title", "score", "direction")
    restored = []
    for entry in selected:
        owners = [
            candidate for candidate in candidates
            if all(candidate.get(field) == entry.get(field) for field in semantic_fields)
            and _same_reading_identity(candidate, entry)
        ]
        newest = max((_reading_owner_timestamp(owner) for owner in owners), default=0)
        titles = {
            preferred_news_title(title=owner.get("_reading_title")) or preferred_news_title(
                owner.get("raw_title", ""), owner.get("title", "")
            )
            for owner in owners if _reading_owner_timestamp(owner) == newest
        }
        # Equal semantic ranks with conflicting full titles cannot establish an
        # owner. Keep selected source text; never infer an unseen continuation.
        title = next(iter(titles)) if len(titles) == 1 else preferred_news_title(
            entry.get("raw_title", ""), entry.get("title", "")
        )
        result = {**entry, "_reading_title": title}
        if "_score_sources" in entry or any("_score_sources" in owner for owner in owners):
            result["_score_sources"] = _restore_score_sources(entry, owners)
        restored.append(result)
    return restored


def render_news_card(
    *, title, link, source, time_label, score, direction, selection_reason,
    level, evidence_note, duplicate_note, rule_pool_html, score_source_label="",
    evidence_context_html="",
) -> str:
    """Render supplied card facts; analysis stays in native closed details."""
    metadata = " · ".join(str(value) for value in (source, time_label) if value)
    explanation = " · ".join(
        str(value) for value in (direction, level, selection_reason, evidence_note, score_source_label)
        if value
    ) + duplicate_note
    return (
        '<article class="home-news-card" style="padding:18px 0 20px;'
        'border-bottom:1px solid rgba(148,163,184,.18);">'
        '<h3 style="margin:0;font-size:18px;font-weight:650;line-height:1.65;overflow-wrap:anywhere;">'
        f'<a href="{escape_html(link)}" target="_blank" rel="noopener noreferrer" '
        'style="display:block;color:var(--color-text-base,inherit);text-decoration:none;">'
        f'{escape_html(title)}</a></h3>'
        '<div class="home-news-meta" style="margin-top:10px;font-size:13px;line-height:1.7;'
        'color:var(--color-text-subdue,inherit);overflow-wrap:anywhere;">'
        f'{escape_html(metadata)}</div>'
        '<div class="home-news-priority" style="margin-top:4px;font-size:11px;line-height:1.7;'
        'color:var(--color-text-subdue,inherit);">'
        f'阅读优先级 {score:.1f}/10</div>'
        '<details class="home-news-context" style="margin-top:10px;">'
        '<summary style="cursor:pointer;color:var(--color-text-subdue,inherit);'
        'font-size:11px;line-height:1.7;">方向与入选依据</summary>'
        '<div style="margin-top:6px;font-size:12px;line-height:1.7;'
        'color:var(--color-text-subdue,inherit);">'
        f'{escape_html(explanation)}</div></details>'
        f'{evidence_context_html}{rule_pool_html}</article>'
    )
