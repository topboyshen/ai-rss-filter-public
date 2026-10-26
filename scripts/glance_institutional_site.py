"""Add explicit shared chrome without widening the frozen homepage CSS."""

from copy import deepcopy
from html import escape
from pathlib import Path
from urllib.parse import quote

if __package__:
    from .glance_institutional import MASTHEAD_CLASS, _base_url, _marker_occurrences, masthead_widget, page_slug
    from .prepare_glance_reading import reading_layout
else:
    from glance_institutional import MASTHEAD_CLASS, _base_url, _marker_occurrences, masthead_widget, page_slug
    from prepare_glance_reading import reading_layout


STYLE_ID = "ai-filter-institutional-site-v1"
SHELL_CLASS = "ai-site-shell"
_STYLE_MARKER = "ai-filter-institutional-site"
_PAGE_MARKER = "ai-site-"
_PAGE_COPY = {
    "events": ("EVENT RADAR", "事件重要度与个股映射仅供研究参考，不构成投资建议。"),
    "quality": ("MODEL QUALITY", "统计样本、窗口与口径以各模块说明为准，不代表未来表现。"),
    "ops": ("OPERATIONS", "运行状态以各模块记录时间为准；未知状态不等同于运行正常。"),
}


def site_style_block() -> str:
    css = Path(__file__).with_name("glance_institutional_site.css").read_text(encoding="utf-8")
    return f'<style id="{STYLE_ID}">\n{css}</style>'


def _target_pages(config: dict) -> dict:
    pages = config.get("pages")
    if not isinstance(pages, list) or not pages or any(not isinstance(page, dict) for page in pages):
        raise ValueError("Expected Glance pages for the institutional site")
    result = {}
    for slug in _PAGE_COPY:
        matches = [page for page in pages[1:] if page_slug(page) == slug]
        if len(matches) != 1:
            raise ValueError("Institutional site requires unique events, quality and ops pages")
        result[slug] = matches[0]
    return result


def site_masthead(config: dict, slug: str | None = None) -> dict:
    """Reuse the verified v1 chrome, changing only page identity and active nav."""
    widget = masthead_widget(config)
    source = widget["source"]
    if slug is None:
        widget["source"] = source.replace(
            f'class="{MASTHEAD_CLASS}"', f'class="{MASTHEAD_CLASS} {SHELL_CLASS}"', 1,
        )
        return widget
    page = _target_pages(config)[slug]
    href = escape(_base_url(config) + "/" + quote(page_slug(page), safe="/-._~"), quote=True)
    source = source.replace(
        f'class="{MASTHEAD_CLASS}"', f'class="{SHELL_CLASS} ai-site-page-{slug}"', 1,
    ).replace(' aria-current="page"', "", 1)
    source = source.replace(f'<a href="{href}">', f'<a href="{href}" aria-current="page">', 1)
    kicker, disclaimer = _PAGE_COPY[slug]
    source = source.replace("<h1>新闻与研究</h1>", f'<h1>{escape(page["name"])}</h1>', 1)
    source = source.replace(
        'class="ai-institutional-kicker">INTELLIGENCE BRIEF</p>',
        f'class="ai-institutional-kicker">{kicker}</p>', 1,
    ).replace(
        'class="ai-institutional-disclaimer">分数仅表示阅读优先级，不构成投资建议。</p>',
        f'class="ai-institutional-disclaimer">{disclaimer}</p>', 1,
    )
    widget["source"] = source
    return widget


def _header_column(config: dict, slug: str) -> dict:
    return {"size": "full", "widgets": [site_masthead(config, slug)]}


def _restore_verified_v1(config: dict) -> None:
    """Peel off only an exact, complete site installation before v1 validation."""
    pages = config.get("pages", [])
    document = config.get("document", {})
    head = document.get("head", "") if isinstance(document, dict) else ""
    markers = _marker_occurrences(pages, _PAGE_MARKER)
    style_markers = head.count(_STYLE_MARKER) if isinstance(head, str) else 0
    if not markers and not style_markers:
        return
    message = "Existing institutional site header or style differs; review before replacing"
    suffix = "\n" + site_style_block()
    if not isinstance(head, str) or style_markers != 1 or not head.endswith(suffix):
        raise ValueError(message)
    targets = _target_pages(config)
    home = pages[0].get("columns", [])
    expected_home = site_masthead(config)
    if not home or not home[0].get("widgets") or home[0]["widgets"][0] != expected_home:
        raise ValueError(message)
    expected_markers = _marker_occurrences(expected_home, _PAGE_MARKER)
    for slug, page in targets.items():
        expected = _header_column(config, slug)
        columns = page.get("columns", [])
        if not columns or columns[0] != expected:
            raise ValueError(message)
        expected_markers += _marker_occurrences(expected, _PAGE_MARKER)
    if markers != expected_markers:
        raise ValueError(message)
    document["head"] = head[:-len(suffix)]
    home[0]["widgets"][0] = masthead_widget(config)
    for page in targets.values():
        page["columns"].pop(0)


def _check_column_capacity(targets: dict) -> None:
    for page in targets.values():
        columns = page.get("columns")
        maximum = 2 if page.get("width") == "slim" else 3
        if not isinstance(columns, list) or not columns or len(columns) >= maximum:
            raise ValueError("Institutional site needs room for one masthead column")
        if any(not isinstance(column, dict) for column in columns) or sum(
            column.get("size") == "full" for column in columns
        ) >= 2:
            raise ValueError("Institutional site needs room for one masthead column")


def site_layout(config: dict) -> dict:
    """Normalize with the frozen v1 generator, then add only shared page chrome."""
    candidate = deepcopy(config)
    _restore_verified_v1(candidate)
    candidate = reading_layout(candidate, institutional_style=True)
    targets = _target_pages(candidate)
    _check_column_capacity(targets)
    candidate["pages"][0]["columns"][0]["widgets"][0] = site_masthead(candidate)
    for slug, page in targets.items():
        page["columns"].insert(0, _header_column(candidate, slug))
    candidate["document"]["head"] += "\n" + site_style_block()
    return candidate
