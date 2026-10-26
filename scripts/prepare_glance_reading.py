#!/usr/bin/env python3
"""Prepare a local Glance reading-home candidate; never install or reload it."""

from __future__ import annotations

import argparse
import os
from copy import deepcopy
from pathlib import Path
from urllib.parse import urlsplit

import yaml

if __package__:
    from .prepare_glance_event_changes import retained_event_changes
    from .glance_institutional import (
        add_institutional_style, page_slug, prepare_masthead,
    )
else:
    from prepare_glance_event_changes import retained_event_changes
    from glance_institutional import (
        add_institutional_style, page_slug, prepare_masthead,
    )


_MOBILE_STYLE_ID = "ai-filter-reading-mobile-v1"
_SKIN_STYLE_ID = "ai-filter-reading-skin-v1"


def _style_block(style_id: str, filename: str) -> str:
    css = Path(__file__).with_name(filename).read_text(encoding="utf-8")
    return f'<style id="{style_id}">\n{css}</style>'


def _add_mobile_reading_style(candidate: dict) -> None:
    """Keep existing head/footer markup; add one versioned, home-scoped style."""
    document = candidate.setdefault("document", {})
    if not isinstance(document, dict):
        raise ValueError("Expected Glance document mapping")
    head = document.get("head", "")
    if not isinstance(head, str):
        raise ValueError("Expected Glance document.head HTML string")
    block = _style_block(_MOBILE_STYLE_ID, "glance_reading_mobile.css")
    if _MOBILE_STYLE_ID in head:
        if head.count(_MOBILE_STYLE_ID) != 1 or not head.endswith(block):
            raise ValueError("Existing reading mobile style differs; review before replacing")
        return
    document["head"] = head + ("\n" if head else "") + block


def _add_reading_skin(candidate: dict) -> None:
    """Opt in to the home-only skin without replacing user or mobile styles."""
    head = candidate["document"]["head"]
    mobile = _style_block(_MOBILE_STYLE_ID, "glance_reading_mobile.css")
    skin = _style_block(_SKIN_STYLE_ID, "glance_reading_skin.css")
    # Keep the mobile block last: the existing generator validates that suffix.
    if not head.endswith(mobile):
        raise ValueError("Expected the verified reading mobile style suffix")
    prefix = head[:-len(mobile)]
    if _SKIN_STYLE_ID in head:
        if head.count(_SKIN_STYLE_ID) != 1 or not prefix.endswith(skin + "\n"):
            raise ValueError("Existing reading skin differs; review before replacing")
        return
    candidate["document"]["head"] = prefix + skin + "\n" + mobile


def _endpoint(widget: dict) -> str:
    # URLs can contain Glance environment placeholders; only inspect the path.
    url = str(widget.get("url") or "")
    if url.startswith("${"):
        url = "http://placeholder.invalid" + url.partition("}")[2]
    return urlsplit(url).path


def _check_existing_destinations(pages: list, indexed: dict, resolve_slug, preserved_events=None) -> None:
    profiles = (
        ("events", "/event-radar-card", "limit=8", {"limit=30"}),
        ("quality", "/score-backtest", "days=90&view=compact", {"days=90", "days=90&view=compact"}),
    )
    for slug, endpoint, home_query, destination_queries in profiles:
        if endpoint not in indexed:
            continue
        if indexed[endpoint] is preserved_events:
            continue  # Explicit opt-in already validated against its events-page source.
        if urlsplit(indexed[endpoint]["url"]).query != home_query:
            raise ValueError("Custom home data profile must be reviewed, not discarded")
        page = next(page for page in pages[1:] if resolve_slug(page) == slug)
        destinations = [widget for column in page.get("columns", [])
                        for widget in column.get("widgets", [])]
        if not any(widget.get("type") == "extension" and _endpoint(widget) == endpoint
                   and urlsplit(widget.get("url", "")).query in destination_queries
                   for widget in destinations):
            raise ValueError("Destination page must retain the existing research capability")


def reading_layout(config: dict, *, reading_skin: bool = False,
                   institutional_style: bool = False) -> dict:
    """Reorder the known home; retain other pages, moving home monitors to ops."""
    if reading_skin and institutional_style:
        raise ValueError("Choose either reading skin or institutional style")
    candidate = deepcopy(config)
    preserved_events = retained_event_changes(candidate)
    pages = candidate.get("pages")
    if not isinstance(pages, list) or not pages:
        raise ValueError("Missing Glance pages")
    home = pages[0]
    widgets = [widget for column in home.get("columns", [])
               for widget in column.get("widgets", [])]
    if any(not isinstance(widget, dict) for widget in widgets):
        raise ValueError("Invalid homepage widget")
    widgets, institutional = prepare_masthead(candidate, widgets, requested=institutional_style)
    if institutional and reading_skin:
        raise ValueError("Installed institutional style cannot switch to reading skin")
    extensions = [widget for widget in widgets if widget.get("type") == "extension"]
    indexed = {}
    allowed = {"/glance-sse", "/briefing-card", "/quotes",
               "/event-radar-card", "/score-backtest"}
    for widget in extensions:
        endpoint = _endpoint(widget)
        if endpoint not in allowed or endpoint in indexed:
            raise ValueError("Unexpected or duplicate homepage extension")
        indexed[endpoint] = widget
    if not {"/glance-sse", "/briefing-card", "/quotes"}.issubset(indexed):
        raise ValueError("Missing news, briefing or quotes widget")
    if any(widget.get("type") not in {"extension", "bookmarks", "monitor"}
           for widget in widgets):
        raise ValueError("Unknown homepage widget; review it without dropping it")
    # These capabilities must remain reachable before removing home duplicates.
    resolve_slug = page_slug if institutional else lambda page: page.get("slug")
    slugs = {resolve_slug(page) for page in pages[1:]}
    if not {"events", "quality", "ops"}.issubset(slugs):
        raise ValueError("Existing events, quality and ops pages are required")
    _check_existing_destinations(pages, indexed, resolve_slug, preserved_events)
    news, brief, quotes = (indexed[key] for key in
                           ("/glance-sse", "/briefing-card", "/quotes"))
    if urlsplit(news["url"]).query or urlsplit(brief["url"]).query != "min_score=7&limit=12":
        raise ValueError("Keep the fixed news and Briefing snapshot profiles")
    quote_query = urlsplit(quotes["url"]).query
    if quote_query not in {"", "view=reading"}:
        raise ValueError("Unexpected quotes profile")
    news.update(title="近期新闻", **{"css-class": "ai-home-news"})
    brief.update(title="今日简报", **{"css-class": "ai-home-briefing"})
    quotes.update(title="市场背景", **{"css-class": "ai-home-market"})
    quotes["url"] = quotes["url"].split("?", 1)[0] + "?view=reading"
    bookmarks = [widget for widget in widgets if widget.get("type") == "bookmarks"]
    home["columns"] = [
        {"size": "full", "widgets": [news]},
        {"size": "small", "widgets": [brief, *([preserved_events] if preserved_events else []), quotes, *bookmarks]},
    ]
    # The homepage name/slug remain stable so existing navigation URLs work.
    monitors = [widget for widget in widgets if widget.get("type") == "monitor"]
    if monitors:
        ops = next(page for page in pages[1:] if resolve_slug(page) == "ops")
        columns = ops.get("columns") or []
        destination = next((col for col in columns if col.get("size") == "full"), None)
        if destination is None:
            raise ValueError("Existing ops full column is required")
        destination.setdefault("widgets", []).extend(monitors)
    _add_mobile_reading_style(candidate)
    if reading_skin:
        _add_reading_skin(candidate)
    if institutional:
        add_institutional_style(
            candidate,
            _style_block(_MOBILE_STYLE_ID, "glance_reading_mobile.css"),
            _style_block(_SKIN_STYLE_ID, "glance_reading_skin.css"),
        )
    return candidate


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("source", type=Path)
    parser.add_argument("output", type=Path)
    styles = parser.add_mutually_exclusive_group()
    styles.add_argument("--reading-skin", action="store_true",
                        help="Opt in to the light, home-scoped reading style")
    styles.add_argument("--institutional-style", action="store_true",
                        help="Opt in to the home-only institutional news and research style")
    args = parser.parse_args(argv)
    if args.source.resolve() == args.output.resolve():
        parser.error("Output must differ from source; production install is not supported")
    with args.source.open(encoding="utf-8") as handle:
        config = yaml.safe_load(handle)
    if not isinstance(config, dict):
        parser.error("Expected a Glance configuration mapping")
    candidate = reading_layout(config, reading_skin=args.reading_skin,
                               institutional_style=args.institutional_style)
    # Exclusive creation avoids overwriting either a source or earlier candidate.
    fd = os.open(args.output, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as handle:
        yaml.safe_dump(candidate, handle, allow_unicode=True, sort_keys=False)
    print("Local candidate written; not installed, no services reloaded.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
