"""Compact homepage presentation of the existing, complete Briefing result."""

import re
from datetime import datetime
from html import escape
from typing import Any
from zoneinfo import ZoneInfo

from core.digest_engine import _briefing_source_label, render_themes_html
from core.theme_rules import theme_display_name
from utils.helpers import safe_http_url

_TZ = ZoneInfo("Asia/Shanghai")

_EVIDENCE_STYLE = (
    "<style>"
    ".briefing-reading-theme-tag{font-size:12px !important;font-weight:750;}"
    ".briefing-reading-news-title{font-size:17px !important;font-weight:650;line-height:1.5;"
    "overflow-wrap:anywhere;}"
    ".briefing-reading-meta,.briefing-reading-facts,.briefing-reading-coverage{"
    "font-size:12px !important;color:var(--color-text-subdue,inherit);overflow-wrap:anywhere;}"
    "</style>"
)


def _ingest_label(article: dict) -> str:
    """Explicit ingest timestamp first; a display string stays as-is."""
    for key in ("created_at", "time"):
        try:
            stamp = int(article.get(key))
        except (TypeError, ValueError):
            stamp = 0
        if stamp > 0:
            return "收录 " + datetime.fromtimestamp(stamp, _TZ).strftime("%m-%d %H:%M")
    text = str(article.get("time") or "").strip()
    if re.fullmatch(r"\d{2}-\d{2} \d{2}:\d{2}", text):
        return f"收录 {text}"
    return "收录时间未记录"


def _representative_article(theme: dict):
    """First stored representative item of the theme; never re-ranked."""
    articles = theme.get("articles")
    if isinstance(articles, list) and articles and isinstance(articles[0], dict):
        return articles[0]
    return None


def _render_theme(theme: dict) -> str:
    title = str(theme.get("theme_display_name") or theme_display_name(theme.get("theme", "")))
    direction = escape(str(theme.get("direction", "中性")))
    importance = escape(str(theme.get("importance", 0)))
    article = _representative_article(theme)
    parts = [
        '<div class="briefing-reading-theme" style="padding:9px 0;'
        'border-bottom:1px solid rgba(148,163,184,.16);">',
        f'<div class="briefing-reading-theme-tag" style="font-size:12px !important;font-weight:750;">'
        f"{escape(title)}</div>",
    ]
    if article is None:
        parts.append(
            '<div class="briefing-reading-news-title" style="font-size:17px !important;'
            'font-weight:650;line-height:1.5;overflow-wrap:anywhere;">暂无代表情报</div>'
        )
    else:
        news_title = escape(str(article.get("title") or "暂无代表情报"))
        raw_link = str(article.get("link") or article.get("url") or "").strip()
        link = safe_http_url(raw_link, "") if raw_link.startswith(("http://", "https://")) else ""
        if link == "#" or not link.startswith(("http://", "https://")):
            link = ""
        if link:
            parts.append(
                f'<a class="briefing-reading-news-title" style="font-size:17px !important;font-weight:650;'
                f'line-height:1.5;overflow-wrap:anywhere;" href="{escape(link)}" '
                f'target="_blank" rel="noopener noreferrer">{news_title}</a>'
            )
        else:
            parts.append(
                f'<div class="briefing-reading-news-title" style="font-size:17px !important;'
                f'font-weight:650;line-height:1.5;overflow-wrap:anywhere;">{news_title}</div>'
            )
        publisher = str(
            _briefing_source_label(article)
            or article.get("source_name") or article.get("source") or ""
        ).strip()
        prefix = f"{escape(publisher)} · " if publisher else ""
        parts.append(
            f'<div class="briefing-reading-meta" style="font-size:12px !important;">'
            f"{prefix}{_ingest_label(article)}</div>"
        )
    parts.append(
        f'<div class="briefing-reading-facts" style="font-size:12px !important;">'
        f"方向：{direction} · 主题重要度 {importance}/10</div>"
    )
    coverage = str(theme.get("source_confirmation") or "来源覆盖信息未记录")
    parts.append(
        '<div class="briefing-reading-coverage" style="font-size:12px !important;">转载关系未核验'
        '<details style="margin-top:4px;">'
        '<summary style="cursor:pointer;font-size:12px;">来源覆盖说明</summary>'
        f'<div style="margin-top:4px;">{escape(coverage)}</div></details></div>'
    )
    parts.append("</div>")
    return "".join(parts)


def render_briefing_reading_html(result: Any, period_hours: int) -> str:
    """Preview three existing themes with their first stored news item."""
    if not isinstance(result, dict):
        raise ValueError("Briefing result must be a mapping")
    themes = result.get("themes", [])
    if not isinstance(themes, list) or any(not isinstance(t, dict) for t in themes):
        raise ValueError("Briefing themes must be a list of mappings")
    full_html = render_themes_html(result, period_hours)
    shown = min(3, len(themes))
    counter = (
        f"展示前 {shown} 个，共 {len(themes)} 个主题"
        if len(themes) > shown else f"共 {len(themes)} 个主题"
    )
    parts = [
        _EVIDENCE_STYLE,
        '<section class="briefing-reading" '
        'style="font-family:system-ui,-apple-system,sans-serif;line-height:1.55;">',
        '<div style="font-size:12px;color:var(--color-text-subdue,inherit);margin-bottom:8px;">'
        f"{counter}</div>",
    ]
    if not themes:
        parts.append(
            '<p style="font-size:12px;">当前结果暂无主题；展开查看完整简报与数据时间。</p>'
        )
    for theme in themes[:3]:
        parts.append(_render_theme(theme))
    parts.append(
        '<details class="briefing-reading-full" style="margin-top:10px;">'
        '<summary style="cursor:pointer;font-size:12px;font-weight:650;">'
        f"查看全部 {len(themes)} 个主题</summary>\n{full_html}\n</details></section>"
    )
    return "\n".join(parts)
