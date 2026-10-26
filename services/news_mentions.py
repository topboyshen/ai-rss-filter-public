"""Read-only title mentions, attached after selection and rendered without I/O."""

import logging

from utils.helpers import escape_html


logger = logging.getLogger(__name__)
TITLE_MENTIONS_READING_STYLE = (
    "body:has(.ai-home-institutional-masthead) #sse-container "
    "> .home-news-card:nth-of-type(1) > .home-news-title-mentions "
    "{color:#d0dbe1 !important;}"
)
_MENTIONS_KEY = "_title_equity_mentions"


def attach_title_mentions(entries: list[dict]) -> list[dict]:
    """Bind one batch result to each final source title, never a display title."""
    if not entries:
        return []
    titles = [str(entry.get("raw_title") or "") for entry in entries]
    try:
        from db.database import get_direct_equity_mentions_batch

        results = get_direct_equity_mentions_batch(titles)
        if len(results) != len(titles):
            raise ValueError("Title mention batch length mismatch")
    except Exception as exc:
        # No source text, database path, or exception payload enters public HTML.
        logger.warning("Title mentions unavailable (%s)", type(exc).__name__)
        results = [{} for _ in titles]
    attached = []
    for entry, title, result in zip(entries, titles, results):
        if not isinstance(result, dict) or result.get("raw_title") != title:
            result = {
                "raw_title": title, "status": "unavailable", "mentions": [],
                "reason": "master_unavailable" if title.strip() else "missing_title",
            }
        attached.append({**entry, _MENTIONS_KEY: result})
    return attached


def _mention_line(text: str) -> str:
    return (
        '<div class="home-news-title-mentions" '
        'style="margin-top:8px;color:var(--color-text-subdue,inherit);'
        'font-size:11px;line-height:1.5;overflow-wrap:anywhere;">'
        f'{escape_html(text)}</div>'
    )


def render_title_mentions(entry: dict) -> tuple[str, bool]:
    """Return HTML and whether it replaces the generic no-association note.

    Legacy pure-render callers without a sidecar keep their theme-only result.
    Missing source text and stale/malformed attached results never count as a
    completed company lookup. The production caller always attaches a batch.
    """
    title = str(entry.get("raw_title") or "")
    if not title.strip():
        return _mention_line("标题提及：原始标题未记录，暂无法识别公司"), True
    result = entry.get(_MENTIONS_KEY)
    if result is None:
        return "", False
    if (
        not isinstance(result, dict)
        or result.get("raw_title") != title
        or result.get("status") != "ok"
    ):
        return _mention_line("标题提及：识别暂不可用"), True
    names = []
    for mention in result.get("mentions") or []:
        if not isinstance(mention, dict):
            continue
        name, ticker, exchange = (
            str(mention.get(field) or "").strip()
            for field in ("name", "ticker", "exchange")
        )
        if name and ticker and exchange:
            names.append(f"{name}（{ticker}·{exchange}）")
    if names:
        return _mention_line(
            "标题提及：" + "、".join(names) + "；仅说明提及，不代表推荐或受益"
        ), True
    if result.get("reason") == "ambiguous":
        return _mention_line("标题提及：证券身份有歧义，暂不关联"), True
    return "", False
