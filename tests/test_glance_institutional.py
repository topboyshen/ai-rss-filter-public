"""Local candidate safety and navigation coverage for the institutional home."""

from copy import deepcopy
from html.parser import HTMLParser
from pathlib import Path
import subprocess
import sys

import pytest
import yaml

from scripts.glance_institutional import MASTHEAD_CLASS, STYLE_ID
from scripts.prepare_glance_reading import main, reading_layout
from test_glance_reading_skin import _conflicting_config as _conflicting_skin, _skin_style_block
from test_home_glance_layout import _config, _custom_config, _mobile_style_block


CSS_PATH = Path(__file__).resolve().parents[1] / "scripts" / "glance_institutional.css"


def _style_block():
    return f'<style id="{STYLE_ID}">\n{CSS_PATH.read_text(encoding="utf-8")}</style>'


def _masthead(config):
    return config["pages"][0]["columns"][0]["widgets"][0]


class _Links(HTMLParser):
    def __init__(self, source):
        super().__init__()
        self.links = []
        self.tags = []
        self.feed(source)

    def handle_starttag(self, tag, attrs):
        self.tags.append(tag)
        if tag == "a":
            self.links.append(dict(attrs))


@pytest.mark.parametrize("document", [None, {}, {"head": ""}])
def test_minimal_institutional_candidate_is_explicit_and_idempotent(document):
    config = _config()
    if document is not None:
        config["document"] = document
    saved = deepcopy(config)
    regular = reading_layout(config)

    result = reading_layout(config, institutional_style=True)

    assert STYLE_ID not in regular["document"]["head"]
    assert result["document"]["head"] == _style_block() + "\n" + _mobile_style_block()
    assert _masthead(result)["type"] == "html"
    assert "css-class" not in _masthead(result)
    # Glance v0.8.4 emits the HTML source directly, with no widget wrapper.
    source = _masthead(result)["source"]
    assert source.startswith(f'<div class="{MASTHEAD_CLASS}">\n<header class="ai-institutional-masthead">')
    assert source.endswith('</header>\n</div>')
    assert source.count(MASTHEAD_CLASS) == 1
    assert "<h1>新闻与研究</h1>" in _masthead(result)["source"]
    assert "分数仅表示阅读优先级，不构成投资建议。" in _masthead(result)["source"]
    assert result["pages"][0]["columns"][0]["widgets"][1:] == (
        regular["pages"][0]["columns"][0]["widgets"]
    )
    assert reading_layout(result) == result
    assert reading_layout(result, institutional_style=True) == result
    assert config == saved


def test_upgrade_retains_legacy_skin_profiles_auth_all_other_pages_and_html():
    config = reading_layout(_custom_config(), reading_skin=True)
    config["auth"] = {"secret-key": "${SYNTHETIC_SESSION_SECRET}"}
    config["server"] = {"base-url": "/reader/", "proxied": True}
    for page in config["pages"]:
        for column in page["columns"]:
            for widget in column["widgets"]:
                if widget["type"] == "extension":
                    widget["headers"] = {"Authorization": "Bearer ${SYNTHETIC_WIDGET_TOKEN}"}
    saved = deepcopy(config)

    result = reading_layout(config, institutional_style=True)

    expected = deepcopy(config)
    expected["pages"][0]["columns"][0]["widgets"].insert(0, _masthead(result))
    head = expected["document"]["head"]
    expected["document"]["head"] = head[:-len(_mobile_style_block())] + _style_block() + "\n" + _mobile_style_block()
    assert result == expected
    assert _skin_style_block() in result["document"]["head"]
    assert result["document"]["head"].endswith(_mobile_style_block())
    assert result["pages"][1:] == config["pages"][1:]
    assert result["branding"]["custom-footer"].encode() == config["branding"]["custom-footer"].encode()
    assert reading_layout(result) == result
    assert reading_layout(result, institutional_style=True) == result
    assert config == saved


@pytest.mark.parametrize("base", ["", "/reader/", "https://example.test/reader/"])
def test_navigation_preserves_all_existing_destinations_base_path_and_escaped_names(base):
    config = _config()
    config["server"] = {"base-url": base}
    config["pages"][0]["name"] = '  News\t & <Research> "Desk"  '
    config["pages"][1]["name"] = 'Events <img src=x onerror=alert(1)>'
    config["pages"].append({"name": "个人观察", "columns": [{"size": "full", "widgets": []}]})

    result = reading_layout(config, institutional_style=True)

    source = _masthead(result)["source"]
    parsed = _Links(source)
    hrefs = [link["href"] for link in parsed.links]
    prefix = base.rstrip("/")
    assert hrefs == [prefix + "/news-%26-%3Cresearch%3E-%22desk%22"] * 2 + [
        prefix + "/events", prefix + "/quality", prefix + "/ops",
        prefix + "/%E4%B8%AA%E4%BA%BA%E8%A7%82%E5%AF%9F",
    ]
    assert sum(link.get("aria-current") == "page" for link in parsed.links) == 1
    assert "&lt;img src=x onerror=alert(1)&gt;" in source
    assert "img" not in parsed.tags and "script" not in parsed.tags
    assert config["pages"][-1] == result["pages"][-1]


def test_institutional_navigation_accepts_explicit_home_slug_and_implicit_research_slugs():
    config = _config()
    config["pages"][0]["slug"] = "custom-home"
    for page in config["pages"][1:]:
        page["name"] = " " + page.pop("slug").upper() + " "

    with pytest.raises(ValueError, match="pages are required"):
        reading_layout(config)
    result = reading_layout(config, institutional_style=True)

    assert [link["href"] for link in _Links(_masthead(result)["source"]).links] == [
        "/custom-home", "/custom-home", "/events", "/quality", "/ops",
    ]
    assert reading_layout(result) == result


@pytest.mark.parametrize("slug", ["/other.example", "//other.example", "../events", "./events",
                                  "news/../events", "news/./events", "news\\events"])
@pytest.mark.parametrize("base", ["", "/reader"])
@pytest.mark.parametrize("output_exists", [False, True])
def test_unsafe_navigation_path_fails_without_mutation_or_output(tmp_path, slug, base, output_exists):
    config = _config()
    config["server"] = {"base-url": base}
    config["pages"][0]["slug"] = slug
    saved = deepcopy(config)
    source, output = tmp_path / "source.yml", tmp_path / "candidate.yml"
    source.write_text(yaml.safe_dump(config), encoding="utf-8")
    source_bytes = source.read_bytes()
    previous = b"Existing candidate must remain unchanged.\n"
    if output_exists:
        output.write_bytes(previous)

    with pytest.raises(ValueError, match="relative page path"):
        reading_layout(config, institutional_style=True)
    with pytest.raises(ValueError, match="relative page path"):
        main([str(source), str(output), "--institutional-style"])

    assert config == saved
    assert source.read_bytes() == source_bytes
    assert output.read_bytes() == previous if output_exists else not output.exists()


def test_institutional_navigation_retains_valid_nested_relative_page_path():
    config = _config()
    config["server"] = {"base-url": "/reader"}
    config["pages"][0]["slug"] = "research/news-desk"

    result = reading_layout(config, institutional_style=True)

    assert _Links(_masthead(result)["source"]).links[0]["href"] == "/reader/research/news-desk"


@pytest.mark.parametrize("field", ["slug", "name"])
@pytest.mark.parametrize("output_exists", [False, True])
def test_unresolved_navigation_placeholders_fail_without_mutation_or_output(tmp_path, field, output_exists):
    config = _config()
    config["pages"][0][field] = "${HOME_PAGE}"
    saved = deepcopy(config)
    source, output = tmp_path / "source.yml", tmp_path / "candidate.yml"
    source.write_text(yaml.safe_dump(config), encoding="utf-8")
    source_bytes = source.read_bytes()
    previous = b"Existing candidate must remain unchanged.\n"
    if output_exists:
        output.write_bytes(previous)

    with pytest.raises(ValueError, match="resolved page names and paths"):
        reading_layout(config, institutional_style=True)
    with pytest.raises(ValueError, match="resolved page names and paths"):
        main([str(source), str(output), "--institutional-style"])

    assert config == saved
    assert source.read_bytes() == source_bytes
    assert output.read_bytes() == previous if output_exists else not output.exists()


def test_base_url_placeholder_is_retained_for_glance_to_resolve():
    config = _config()
    config["server"] = {"base-url": "${GLANCE_BASE_URL}"}
    config["pages"][0]["slug"] = "home"

    result = reading_layout(config, institutional_style=True)

    assert _Links(_masthead(result)["source"]).links[0]["href"] == "${GLANCE_BASE_URL}/home"
    assert result["server"] == config["server"]
    assert reading_layout(result) == result


@pytest.mark.parametrize("conflict", ["different-style", "duplicate", "marker-only", "misplaced-style"])
def test_institutional_upgrade_does_not_overwrite_modified_legacy_skin(conflict):
    config = _conflicting_skin(conflict)
    saved = deepcopy(config)

    with pytest.raises(ValueError, match="reading skin"):
        reading_layout(config, institutional_style=True)

    assert config == saved


def _conflicting_config(change):
    config = reading_layout(_custom_config(), institutional_style=True)
    widget = _masthead(config)
    if change == "widget-modified":
        widget["source"] += "<!-- user edit -->"
    elif change == "widget-duplicate":
        config["pages"][0]["columns"][0]["widgets"].append(deepcopy(widget))
    elif change == "widget-unknown":
        widget["source"] = widget["source"].replace(MASTHEAD_CLASS, MASTHEAD_CLASS + "-v2")
    elif change == "widget-source-duplicate":
        widget["source"] += widget["source"]
    elif change == "widget-marker-only":
        widget["source"] = f'<div class="{MASTHEAD_CLASS}"></div>'
    elif change == "widget-legacy":
        widget["source"] = widget["source"].removeprefix(f'<div class="{MASTHEAD_CLASS}">\n').removesuffix('\n</div>')
        widget["css-class"] = MASTHEAD_CLASS
    elif change == "widget-other-page":
        config["pages"][1]["columns"][0]["widgets"].append(deepcopy(widget))
    elif change == "widget-removed":
        config["pages"][0]["columns"][0]["widgets"].pop(0)
    elif change == "style-modified":
        config["document"]["head"] = config["document"]["head"].replace(_style_block(), _style_block() + "<!-- user edit -->")
    elif change == "style-body-modified":
        config["document"]["head"] = config["document"]["head"].replace("body:has", "main:has", 1)
    elif change == "style-marker-only":
        config["document"]["head"] = config["document"]["head"].replace(_style_block(), f"<!-- {STYLE_ID} -->")
    elif change == "style-duplicate":
        config["document"]["head"] = _style_block() + "\n" + config["document"]["head"]
    elif change == "style-unknown":
        config["document"]["head"] = config["document"]["head"].replace(STYLE_ID, "ai-filter-institutional-v2")
    elif change == "style-removed":
        config["document"]["head"] = config["document"]["head"].replace(_style_block() + "\n", "")
    elif change == "unknown-widget":
        config["pages"][0]["columns"][0]["widgets"].append({"type": "html", "source": "User-owned HTML"})
    return config


CONFLICTS = ["widget-modified", "widget-duplicate", "widget-unknown", "widget-other-page",
             "widget-source-duplicate", "widget-marker-only", "widget-legacy",
             "widget-removed", "style-modified", "style-body-modified", "style-marker-only",
             "style-duplicate", "style-unknown",
             "style-removed", "unknown-widget"]


@pytest.mark.parametrize("change", CONFLICTS)
@pytest.mark.parametrize("requested", [False, True])
def test_corrupted_or_unknown_markers_fail_closed_without_mutating_input(change, requested):
    config = _conflicting_config(change)
    saved = deepcopy(config)

    with pytest.raises(ValueError):
        reading_layout(config, institutional_style=requested)

    assert config == saved


@pytest.mark.parametrize("change", CONFLICTS)
@pytest.mark.parametrize("output_exists", [False, True])
def test_corrupt_candidate_cli_leaves_source_and_existing_output_untouched(tmp_path, change, output_exists):
    source, output = tmp_path / "source.yml", tmp_path / "candidate.yml"
    source.write_text(yaml.safe_dump(_conflicting_config(change)), encoding="utf-8")
    source_bytes = source.read_bytes()
    previous = b"Existing candidate must remain unchanged.\n"
    if output_exists:
        output.write_bytes(previous)

    with pytest.raises(ValueError):
        main([str(source), str(output), "--institutional-style"])

    assert source.read_bytes() == source_bytes
    assert output.read_bytes() == previous if output_exists else not output.exists()


def test_institutional_cli_creates_private_candidate_exclusively_and_flags_are_exclusive(tmp_path):
    source, output = tmp_path / "source.yml", tmp_path / "candidate.yml"
    config = _custom_config()
    source.write_text(yaml.safe_dump(config), encoding="utf-8")
    saved = source.read_bytes()
    script = Path(__file__).resolve().parents[1] / "scripts" / "prepare_glance_reading.py"

    result = subprocess.run([sys.executable, str(script), str(source), str(output),
                             "--institutional-style"], capture_output=True, text=True, check=True)

    assert "not installed" in result.stdout
    assert output.stat().st_mode & 0o777 == 0o600
    candidate = output.read_bytes()
    assert yaml.safe_load(candidate) == reading_layout(config, institutional_style=True)
    with pytest.raises(FileExistsError):
        main([str(source), str(output), "--institutional-style"])
    with pytest.raises(SystemExit):
        main([str(source), str(source), "--institutional-style"])
    with pytest.raises(SystemExit):
        main([str(source), str(output), "--reading-skin", "--institutional-style"])
    with pytest.raises(ValueError, match="either"):
        reading_layout(config, reading_skin=True, institutional_style=True)
    assert source.read_bytes() == saved
    assert output.read_bytes() == candidate
