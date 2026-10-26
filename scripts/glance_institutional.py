"""Opt-in, presentation-only masthead and stylesheet for the Glance home."""

from html import escape
from pathlib import Path
import re
from urllib.parse import quote, urlsplit


STYLE_ID = "ai-filter-institutional-v1"
MASTHEAD_CLASS = "ai-home-institutional-masthead"
_STYLE_MARKER = "ai-filter-institutional"
_WIDGET_MARKER = "ai-home-institutional"


def page_slug(page: dict) -> str:
    """Resolve the configured page path, including Glance's name fallback."""
    slug = page.get("slug")
    if not slug:
        name = page.get("name", "")
        if not isinstance(name, str):
            raise ValueError("Expected Glance page name string")
        slug = re.sub(r"[\t\n\f\r ]+", "-", name.lower()).strip("-")
    if not isinstance(slug, str) or not slug:
        raise ValueError("Expected Glance page slug or name")
    if "${" in slug:
        raise ValueError("Institutional navigation requires resolved page names and paths")
    if slug.startswith("/") or "\\" in slug or any(part in {".", ".."} for part in slug.split("/")):
        raise ValueError("Institutional navigation requires a relative page path without traversal")
    return slug


def _base_url(config: dict) -> str:
    server = config.get("server", {})
    if not isinstance(server, dict):
        raise ValueError("Expected Glance server mapping")
    base = server.get("base-url", "")
    if not isinstance(base, str):
        raise ValueError("Expected Glance server.base-url string")
    parsed = urlsplit(base)
    if (parsed.query or parsed.fragment or base.startswith("//")
            or (parsed.scheme and parsed.scheme not in {"http", "https"})
            or (base and not base.startswith(("/", "http://", "https://", "${")))):
        raise ValueError("Expected a Glance base URL or absolute base path")
    # A base placeholder is copied verbatim into HTML for Glance to resolve.
    return base.rstrip("/")


def masthead_widget(config: dict) -> dict:
    """Build static chrome using only the existing page names and destinations."""
    pages = config["pages"]
    base = _base_url(config)
    for slug in ("events", "quality", "ops"):
        matches = [page for page in pages[1:] if page_slug(page) == slug]
        if len(matches) != 1:
            raise ValueError("Institutional navigation requires unique events, quality and ops pages")
    links = []
    for index, page in enumerate(pages):
        name = page.get("name")
        if not isinstance(name, str) or not name:
            raise ValueError("Institutional navigation requires page names")
        if "${" in name:
            raise ValueError("Institutional navigation requires resolved page names and paths")
        href = escape(base + "/" + quote(page_slug(page), safe="/-._~"), quote=True)
        current = ' aria-current="page"' if index == 0 else ""
        links.append(f'    <a href="{href}"{current}>{escape(name)}</a>')
    home = escape(base + "/" + quote(page_slug(pages[0]), safe="/-._~"), quote=True)
    # Glance's HTML widget emits source directly, without a css-class wrapper.
    source = (
        f'<div class="{MASTHEAD_CLASS}">\n'
        '<header class="ai-institutional-masthead">\n'
        '  <div class="ai-institutional-utility"><span>NEWS &amp; RESEARCH</span>'
        '<span>阅读优先级辅助</span></div>\n'
        '  <div class="ai-institutional-brand">\n'
        f'    <a class="ai-institutional-wordmark" href="{home}">AI Filter</a>\n'
        '    <span class="ai-institutional-tagline">NEWS &amp; RESEARCH</span>\n'
        '  </div>\n'
        '  <nav class="ai-institutional-nav" aria-label="主要导航">\n'
        + "\n".join(links) + '\n  </nav>\n'
        '  <div class="ai-institutional-intro">\n'
        '    <p class="ai-institutional-kicker">INTELLIGENCE BRIEF</p>\n'
        '    <h1>新闻与研究</h1>\n'
        '    <p class="ai-institutional-disclaimer">分数仅表示阅读优先级，不构成投资建议。</p>\n'
        '  </div>\n'
        '</header>\n</div>'
    )
    return {"type": "html", "source": source}


def _marker_occurrences(value, marker: str) -> int:
    if isinstance(value, str):
        return value.count(marker)
    if isinstance(value, dict):
        return sum(_marker_occurrences(item, marker) for item in value.values())
    if isinstance(value, list):
        return sum(_marker_occurrences(item, marker) for item in value)
    return 0


def prepare_masthead(config: dict, widgets: list, *, requested: bool) -> tuple[list, bool]:
    """Recognize only our exact widget; never replace user or unknown markup."""
    marker_count = _marker_occurrences(config["pages"], _WIDGET_MARKER)
    document = config.get("document", {})
    head = document.get("head", "") if isinstance(document, dict) else ""
    style_count = head.count(_STYLE_MARKER) if isinstance(head, str) else 0
    installed = bool(marker_count or style_count)
    if not requested and not installed:
        return widgets, False
    expected = masthead_widget(config)
    matches = [widget for widget in widgets if _marker_occurrences(widget, _WIDGET_MARKER)]
    if installed and (marker_count != 1 or style_count != 1 or matches != [expected]):
        raise ValueError("Existing institutional masthead or style differs; review before replacing")
    previous = matches[0] if matches else None
    return [widget for widget in widgets if widget is not previous], True


def add_institutional_style(config: dict, mobile: str, skin: str) -> None:
    """Preserve user HTML and a verified legacy skin before the mobile suffix."""
    head = config["document"]["head"]
    if not head.endswith(mobile):
        raise ValueError("Expected the verified reading mobile style suffix")
    css = Path(__file__).with_name("glance_institutional.css").read_text(encoding="utf-8")
    block = f'<style id="{STYLE_ID}">\n{css}</style>'
    prefix = head[:-len(mobile)]
    if _STYLE_MARKER in head:
        if head.count(_STYLE_MARKER) != 1 or not prefix.endswith(block + "\n"):
            raise ValueError("Existing institutional style differs; review before replacing")
        prefix = prefix[:-len(block + "\n")]
    if "ai-filter-reading-skin" in prefix:
        if prefix.count("ai-filter-reading-skin") != 1 or not prefix.endswith(skin + "\n"):
            raise ValueError("Existing reading skin differs; review before replacing")
    config["document"]["head"] = prefix + block + "\n" + mobile
    config["pages"][0]["columns"][0]["widgets"].insert(0, masthead_widget(config))
