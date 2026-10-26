"""The institutional visual layer is inert outside its explicit homepage marker."""

from pathlib import Path
import re

from test_glance_reading_skin import _selector_branches


CSS = Path(__file__).resolve().parents[1] / "scripts" / "glance_institutional.css"


def test_institutional_css_is_marker_scoped_and_has_no_network_or_scripts():
    css = re.sub(r"/\*.*?\*/", "", CSS.read_text(encoding="utf-8"), flags=re.S)
    assert not re.search(r"@import\b|url\s*\(|<\s*/?\s*script\b|expression\s*\(", css, re.I)
    rules = [match.strip() for match in re.findall(r"([^{}]+)\{", css)]
    selectors = [branch for rule in rules if not rule.startswith("@")
                 for branch in _selector_branches(rule)]
    assert selectors
    assert all(".ai-home-institutional-masthead" in selector for selector in selectors)


def test_first_pair_requires_adjacency_and_does_not_reorder_news_or_expand_details():
    css = re.sub(r"/\*.*?\*/", "", CSS.read_text(encoding="utf-8"), flags=re.S)
    assert ".home-news-card:nth-of-type(1) + .home-news-card:nth-of-type(2)" in css
    assert not re.search(r"grid-auto-flow\s*:[^;}]*dense", css)
    assert not re.search(r"\.home-news-card[^{}]*\{[^{}]*\border\s*:", css)
    assert not re.search(r"details\s*>\s*[^{}]*\{[^{}]*display\s*:\s*(?:block|contents)", css)
