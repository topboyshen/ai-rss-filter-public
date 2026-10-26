"""Static boundaries for the opt-in site skin; actual layout has visual QA."""

from pathlib import Path
import re


CSS_PATH = Path(__file__).resolve().parents[1] / "scripts" / "glance_institutional_site.css"


def _css():
    return re.sub(r"/\*.*?\*/", "", CSS_PATH.read_text(encoding="utf-8"), flags=re.S)


def _selector_branches(selectors):
    depth, start = 0, 0
    for index, char in enumerate(selectors):
        if char in "([":
            depth += 1
        elif char in ")]":
            depth -= 1
        elif char == "," and depth == 0:
            yield selectors[start:index].strip()
            start = index + 1
    yield selectors[start:].strip()


def _rules():
    return [
        (branch, declarations)
        for selectors, declarations in re.findall(r"([^{}]+)\{([^{}]*)\}", _css())
        if not selectors.strip().startswith("@")
        for branch in _selector_branches(selectors.strip())
    ]


def test_site_css_requires_explicit_marker_on_every_selector():
    rules = _rules()
    assert rules
    assert all(re.search(r"\.ai-site-shell\b", selector) for selector, _ in rules)


def test_site_css_is_self_contained_presentation_without_generated_content():
    css = _css()
    assert not re.search(r"@import\b|url\s*\(|<\s*/?\s*script\b|expression\s*\(", css, re.I)
    assert not re.search(r"(?:^|[;{])\s*content\s*:", css)
    assert "ai-filter-institutional-site" not in css
    assert css.count("{") == css.count("}")


def test_inner_columns_do_not_rewrite_home_news_layout_or_dom_order():
    css = _css()
    assert ".home-news-card" not in css
    assert "#sse-container" not in css
    assert "display: contents" not in css
    assert not re.search(r"(?:^|[;{])\s*order\s*:", css)
    layout_rules = [(selector, body) for selector, body in _rules() if ".page-columns" in selector]
    assert layout_rules
    for selector, _ in layout_rules:
        assert ":not(:has(.ai-home-institutional-masthead))" in selector or ".ai-site-page-ops" in selector
    assert "grid-template-columns: 300px minmax(0, 1fr)" in css
    assert "grid-column: 1 / -1" in css


def test_all_inner_columns_remain_available_and_navigation_remains_accessible():
    css = _css()
    assert "@media (max-width: 1190px)" in css
    assert "@media (max-width: 900px)" in css
    assert "@media (max-width: 550px)" in css
    assert "display: block !important" in css
    assert "grid-template-columns: minmax(0, 1fr)" in css
    assert "minmax(min(100%, 200px), 1fr)" in css
    assert "footer.footer" in css and "position: static !important" in css
    for selector, declarations in _rules():
        if re.search(r"display\s*:\s*none", declarations):
            assert ".header-container" in selector or ".mobile-navigation-label:has(> .mobile-navigation-input)" in selector
        assert not (".ai-institutional-disclaimer" in selector and "visibility: hidden" in declarations)


def test_metric_values_are_not_restyled_as_quality_scores():
    rules = _rules()
    metric_values = [body for selector, body in rules if selector.endswith(" .mq-metric-value")]
    assert len(metric_values) == 1
    assert "font-size: 15px" in metric_values[0]
    assert "color: var(--ai-ink)" in metric_values[0]
    for selector, declarations in rules:
        if ".ops-summary-value" in selector and re.search(r"(?:^|[;{])\s*color\s*:", declarations):
            assert ".ops-summary-card:not(:first-child)" in selector or '[style*="color:' in selector
        if ".ops-summary-status" in selector and re.search(r"(?:^|[;{])\s*color\s*:", declarations):
            assert '[style*="color:' in selector
    assert any(".event-radar-summary-direction.is-bull" in selector and "#a42b35" in body for selector, body in rules)
    assert any(".event-radar-summary-direction.is-bear" in selector and "#245690" in body for selector, body in rules)


def test_unknown_and_research_cautions_keep_visible_contrasting_hues():
    css = _css()
    assert '[style*="color:#fbbf24"]' in css
    assert "color: #8a5900 !important" in css
    assert '[style*="color:#d97706"]' in css
    assert "color: #935000 !important" in css
    assert ".mq-note" in css and ".mq-assessment" in css
    assert ".quality-market-research > summary" in css
    assert ".quality-full-statistics > summary" in css


def test_feed_rows_reflow_without_clipping_or_reordering_their_content():
    css = _css()
    grid_contract = 'div[style*="grid-template-columns:minmax(150px,1.3fr) 82px 52px 100px 88px"]'
    renderer = (CSS_PATH.parents[1] / "routes" / "dashboard_routes.py").read_text(encoding="utf-8")
    assert "grid-template-columns:minmax(150px,1.3fr) 82px 52px 100px 88px" in renderer
    feed_rules = [(selector, body) for selector, body in _rules() if grid_contract in selector]
    assert feed_rules
    assert "@media (max-width: 750px)" in css
    assert all(".ai-site-page-ops" in selector for selector, _ in feed_rules)
    assert any("grid-template-columns: repeat(3, minmax(0, 1fr)) !important" in body for _, body in feed_rules)
    assert any(selector.endswith(" > div:first-child") and "grid-column: 1 / 3" in body for selector, body in feed_rules)
    assert any("white-space: normal !important" in body and "overflow: visible !important" in body for _, body in feed_rules)
    assert not any("overflow: hidden" in body or "display: none" in body for _, body in feed_rules)
