#!/usr/bin/env python3
"""Prepare a private local home event-changes candidate; never install or reload it."""

from __future__ import annotations

import argparse
from copy import deepcopy
import os
from pathlib import Path
from urllib.parse import urlsplit

import yaml

if __package__:
    from .glance_institutional import page_slug
else:
    from glance_institutional import page_slug


EVENT_CHANGES_CLASS = "ai-home-event-changes"


def _widgets(page: dict) -> list[dict]:
    columns = page.get("columns")
    if not isinstance(columns, list) or any(not isinstance(col, dict) for col in columns):
        raise ValueError("Expected Glance page columns")
    result = []
    for column in columns:
        widgets = column.get("widgets")
        if not isinstance(widgets, list) or any(not isinstance(widget, dict) for widget in widgets):
            raise ValueError("Expected Glance column widgets")
        result.extend(widgets)
    return result


def _has_class(widget: dict, name: str) -> bool:
    classes = widget.get("css-class", "")
    return isinstance(classes, str) and name in classes.split()


def _marked_widgets(value) -> list[dict]:
    if isinstance(value, dict):
        own = [value] if _has_class(value, EVENT_CHANGES_CLASS) else []
        return own + [widget for child in value.values() for widget in _marked_widgets(child)]
    if isinstance(value, list):
        return [widget for child in value for widget in _marked_widgets(child)]
    return []


def _url_parts(widget: dict):
    url = widget.get("url")
    if not isinstance(url, str) or not url:
        raise ValueError("Expected extension URL string")
    # Inspect only a synthetic host; copy the original placeholder verbatim.
    probe = "http://placeholder.invalid" + url.partition("}")[2] if url.startswith("${") else url
    parsed = urlsplit(probe)
    if parsed.scheme not in {"http", "https"} or not parsed.netloc or parsed.fragment:
        raise ValueError("Expected absolute extension URL without fragment")
    return parsed


def _home_parts(config: dict) -> tuple[dict, list, int]:
    pages = config.get("pages")
    if not isinstance(pages, list) or not pages or any(not isinstance(page, dict) for page in pages):
        raise ValueError("Expected Glance pages")
    home = pages[0]
    widgets = _widgets(home)
    news = [widget for widget in widgets if _has_class(widget, "ai-home-news")]
    if len(news) != 1 or news[0].get("type") != "extension" or _url_parts(news[0]).path != "/glance-sse":
        raise ValueError("Existing reading home with one ai-home-news extension is required")
    briefs = [widget for widget in widgets if _has_class(widget, "ai-home-briefing")]
    markets = [widget for widget in widgets if _has_class(widget, "ai-home-market")]
    if len(briefs) != 1 or len(markets) != 1:
        raise ValueError("Existing reading briefing and market widgets are required")
    if (briefs[0].get("type") != "extension" or markets[0].get("type") != "extension"
            or _url_parts(briefs[0]).path != "/briefing-card" or _url_parts(markets[0]).path != "/quotes"):
        raise ValueError("Reading widget markers must match their endpoints")
    columns = [col for col in home["columns"] if col.get("size") == "small"
               and any(widget is briefs[0] for widget in col["widgets"])
               and any(widget is markets[0] for widget in col["widgets"])]
    if len(columns) != 1:
        raise ValueError("Briefing and market must share one small sidebar")
    sidebar = columns[0]["widgets"]
    insertion = sidebar.index(briefs[0]) + 1
    if sidebar.index(markets[0]) < insertion:
        raise ValueError("Market must follow briefing in the sidebar")
    return home, sidebar, insertion


def _expected_widget(config: dict) -> dict:
    pages = [page for page in config["pages"][1:] if page_slug(page) == "events"]
    if len(pages) != 1:
        raise ValueError("Exactly one existing events page is required")
    sources = [widget for widget in _widgets(pages[0])
               if widget.get("type") == "extension" and _url_parts(widget).path == "/event-radar-card"]
    if len(sources) != 1:
        raise ValueError("Exactly one events-page radar extension is required")
    source = sources[0]
    parts = _url_parts(source)
    # The endpoint rejects unknown query keys, and the cheap snapshot fast path
    # requires the exact limit=8 profile. Never guess that a proxy strips extras.
    if parts.query != "limit=30":
        raise ValueError("Existing events profile must be exactly limit=30 without extra query parameters")
    if source.get("allow-potentially-dangerous-html") is not True:
        raise ValueError("Existing events extension must already allow HTML")
    widget = deepcopy(source)
    widget.update(title="今日事件变化", cache="30s", **{"css-class": EVENT_CHANGES_CLASS})
    widget["url"] = source["url"].partition("?")[0] + "?limit=8"
    return widget


def retained_event_changes(config: dict) -> dict | None:
    """Let reading-layout regeneration preserve only a verified explicit opt-in."""
    pages = config.get("pages")
    if not isinstance(pages, list) or not pages:
        return None
    marked = _marked_widgets(pages)
    if not marked:
        return None
    home, sidebar, insertion = _home_parts(config)
    expected = _expected_widget(config)
    radar = [widget for widget in _widgets(home) if widget.get("type") == "extension"
             and _url_parts(widget).path == "/event-radar-card"]
    if (len(marked) != 1 or len(radar) != 1 or marked[0] is not radar[0]
            or marked[0] != expected or insertion >= len(sidebar) or sidebar[insertion] is not marked[0]):
        raise ValueError("Existing home event-changes widget conflicts; review without replacing it")
    return marked[0]


def event_changes_layout(config: dict) -> dict:
    """Copy the configured event endpoint into the reading sidebar, without other edits."""
    candidate = deepcopy(config)
    home, sidebar, insertion = _home_parts(candidate)
    widget = _expected_widget(candidate)
    if retained_event_changes(candidate) is not None:
        return candidate
    if any(item.get("type") == "extension" and _url_parts(item).path == "/event-radar-card"
           for item in _widgets(home)):
        raise ValueError("Unmarked home radar widget exists; review without replacing it")
    sidebar.insert(insertion, widget)
    return candidate


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("source", type=Path)
    parser.add_argument("output", type=Path)
    args = parser.parse_args(argv)
    if args.source.resolve() == args.output.resolve():
        parser.error("Output must differ from source; production install is not supported")
    with args.source.open(encoding="utf-8") as handle:
        config = yaml.safe_load(handle)
    if not isinstance(config, dict):
        parser.error("Expected a Glance configuration mapping")
    candidate = event_changes_layout(config)
    fd = os.open(args.output, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as handle:
        yaml.safe_dump(candidate, handle, allow_unicode=True, sort_keys=False)
    print("Local event-changes candidate written; not installed, no services reloaded.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
