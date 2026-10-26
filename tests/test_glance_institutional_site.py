"""Four-page chrome keeps the verified homepage and original content contracts."""

from copy import deepcopy
from html.parser import HTMLParser
from pathlib import Path
import subprocess
import sys
from urllib.parse import quote

import pytest
import yaml

from scripts.glance_institutional import MASTHEAD_CLASS, page_slug
from scripts.glance_institutional_site import SHELL_CLASS, STYLE_ID, site_layout, site_style_block
from scripts.prepare_glance_reading import reading_layout
from scripts.prepare_glance_site import main
from test_glance_institutional import _conflicting_config as _conflicting_home
from test_home_glance_layout import _config, _custom_config


class _HTML(HTMLParser):
    def __init__(self, source):
        super().__init__()
        self.elements = []
        self.feed(source)

    def handle_starttag(self, tag, attrs):
        self.elements.append((tag, dict(attrs)))


def _widget(config, index):
    return config["pages"][index]["columns"][0]["widgets"][0]


def _installed_home():
    config = _custom_config()
    config["server"] = {"base-url": "/reader/", "proxied": True}
    config["auth"] = {"secret-key": "${SYNTHETIC_SESSION_SECRET}"}
    for index in (1, 2, 3):
        sidebar = {"size": "small", "widgets": [
            {"type": "html", "source": f"<p>User-owned sidebar {index}</p>"},
        ]}
        if index == 3:
            config["pages"][index]["columns"].insert(0, sidebar)
        else:
            config["pages"][index]["columns"].append(sidebar)
    config["pages"].append({"name": "个人观察", "slug": "personal", "columns": [
        {"size": "full", "widgets": [{"type": "html", "source": "<p>Private user HTML</p>"}]},
    ]})
    for page in config["pages"]:
        for column in page["columns"]:
            for widget in column["widgets"]:
                if widget["type"] == "extension":
                    widget["headers"] = {"Authorization": "Bearer ${SYNTHETIC_WIDGET_TOKEN}"}
    return reading_layout(reading_layout(config, reading_skin=True), institutional_style=True)


@pytest.mark.parametrize("profile", ["original", "reading", "skin", "institutional", "skin-institutional"])
def test_site_accepts_known_inputs_and_is_idempotent(profile):
    config = _config()
    if profile in {"reading", "skin", "skin-institutional"}:
        config = reading_layout(config, reading_skin="skin" in profile)
    if profile in {"institutional", "skin-institutional"}:
        config = reading_layout(config, institutional_style=True)
    saved = deepcopy(config)
    normalized = reading_layout(config, institutional_style=True)

    result = site_layout(config)

    assert result["document"]["head"] == normalized["document"]["head"] + "\n" + site_style_block()
    assert result["document"]["head"].count(STYLE_ID) == 1
    assert site_layout(result) == result
    assert config == saved


def test_upgrade_only_adds_headers_and_site_style_preserving_original_column_order_and_values():
    config = _installed_home()
    saved = deepcopy(config)

    result = site_layout(config)

    expected = deepcopy(config)
    expected["document"]["head"] += "\n" + site_style_block()
    expected["pages"][0]["columns"][0]["widgets"][0]["source"] = (
        _widget(config, 0)["source"].replace(
            f'class="{MASTHEAD_CLASS}"', f'class="{MASTHEAD_CLASS} {SHELL_CLASS}"', 1,
        )
    )
    for index in (1, 2, 3):
        expected["pages"][index]["columns"].insert(0, result["pages"][index]["columns"][0])
        assert result["pages"][index]["columns"][1:] == config["pages"][index]["columns"]
        assert result["pages"][index]["columns"][0] == {"size": "full", "widgets": [_widget(result, index)]}
    assert result == expected
    assert result["pages"][4:] == config["pages"][4:]
    assert result["auth"] == config["auth"]
    assert result["theme"] == config["theme"]
    assert result["branding"]["custom-footer"].encode() == config["branding"]["custom-footer"].encode()
    assert result["document"]["head"].startswith(config["document"]["head"])
    assert site_layout(result) == result
    assert config == saved


@pytest.mark.parametrize("base", ["", "/reader/", "https://example.test/reader/", "${GLANCE_BASE_URL}"])
def test_raw_headers_keep_all_navigation_destinations_with_correct_active_page_and_escaped_titles(base):
    config = _custom_config()
    config["server"] = {"base-url": base}
    config["pages"][1]["name"] = '事件 <img src=x> "雷达"'
    config["pages"].append({"name": "额外页面", "slug": "extra", "columns": [{"size": "full", "widgets": []}]})
    result = site_layout(config)
    hrefs = [base.rstrip("/") + "/" + quote(page_slug(page), safe="/-._~") for page in config["pages"]]

    for index, slug in enumerate((None, "events", "quality", "ops")):
        widget = _widget(result, index)
        assert set(widget) == {"type", "source"}
        assert widget["type"] == "html"
        parsed = _HTML(widget["source"])
        expected_class = f"{MASTHEAD_CLASS} {SHELL_CLASS}" if slug is None else f"{SHELL_CLASS} ai-site-page-{slug}"
        assert parsed.elements[0] == ("div", {"class": expected_class})
        links = [attrs for tag, attrs in parsed.elements if tag == "a"]
        assert [attrs["href"] for attrs in links] == [hrefs[0], *hrefs]
        assert [attrs["href"] for attrs in links if attrs.get("aria-current") == "page"] == [hrefs[index]]
        assert widget["source"].count(SHELL_CLASS) == 1
        assert widget["source"].count(MASTHEAD_CLASS) == (1 if index == 0 else 0)
        assert not any(tag in {"img", "script"} for tag, _ in parsed.elements)
    assert '<h1>事件 &lt;img src=x&gt; &quot;雷达&quot;</h1>' in _widget(result, 1)["source"]
    assert result["pages"][-1] == config["pages"][-1]


def test_implicit_research_slugs_and_known_home_navigation_rules_are_reused():
    config = _config()
    for page in config["pages"][1:]:
        page["name"] = " " + page.pop("slug").upper() + " "
    assert site_layout(site_layout(config)) == site_layout(config)
    config["pages"][0]["slug"] = "/other.example"
    with pytest.raises(ValueError, match="relative page path"):
        site_layout(config)


def _conflicting_site(change):
    config = site_layout(_installed_home())
    head = config["document"]["head"]
    if change == "style-modified":
        config["document"]["head"] = head.replace(site_style_block(), site_style_block().replace("</style>", "/* edit */</style>"))
    elif change == "style-duplicate":
        config["document"]["head"] += "\n" + site_style_block()
    elif change == "style-unknown":
        config["document"]["head"] = head.replace(STYLE_ID, STYLE_ID + "-unknown")
    elif change == "style-removed":
        config["document"]["head"] = head.removesuffix("\n" + site_style_block())
    elif change == "style-misplaced":
        config["document"]["head"] += "\n<!-- user head -->"
    elif change == "header-modified":
        _widget(config, 1)["source"] += "<!-- user header -->"
    elif change == "header-unknown":
        _widget(config, 1)["source"] = _widget(config, 1)["source"].replace(SHELL_CLASS, SHELL_CLASS + "-unknown")
    elif change == "header-removed":
        config["pages"][1]["columns"].pop(0)
    elif change == "header-duplicate":
        config["pages"][1]["columns"].insert(0, deepcopy(config["pages"][1]["columns"][0]))
    elif change == "header-extra-page":
        config["pages"][-1]["columns"].insert(0, deepcopy(config["pages"][1]["columns"][0]))
    elif change == "header-column-modified":
        config["pages"][1]["columns"][0]["size"] = "small"
    elif change == "home-modified":
        _widget(config, 0)["source"] += "<!-- user home -->"
    elif change == "home-shell-removed":
        _widget(config, 0)["source"] = _widget(config, 0)["source"].replace(" " + SHELL_CLASS, "")
    elif change == "old-home-style-modified":
        config["document"]["head"] = head.replace("ai-filter-institutional-v1", "ai-filter-institutional-v1-unknown")
    return config


CONFLICTS = ["style-modified", "style-duplicate", "style-unknown", "style-removed", "style-misplaced",
             "header-modified", "header-unknown", "header-removed", "header-duplicate",
             "header-extra-page", "header-column-modified", "home-modified", "home-shell-removed",
             "old-home-style-modified"]


@pytest.mark.parametrize("change", CONFLICTS)
@pytest.mark.parametrize("output_exists", [False, True])
def test_modified_unknown_or_incomplete_site_never_changes_input_or_output(tmp_path, change, output_exists):
    config = _conflicting_site(change)
    saved = deepcopy(config)
    source, output = tmp_path / "source.yml", tmp_path / "candidate.yml"
    source.write_text(yaml.safe_dump(config), encoding="utf-8")
    source_bytes = source.read_bytes()
    previous = b"Existing private candidate.\n"
    if output_exists:
        output.write_bytes(previous)

    with pytest.raises(ValueError):
        site_layout(config)
    with pytest.raises(ValueError):
        main([str(source), str(output)])

    assert config == saved
    assert source.read_bytes() == source_bytes
    assert output.read_bytes() == previous if output_exists else not output.exists()


@pytest.mark.parametrize("change", ["widget-modified", "widget-legacy", "style-modified", "style-unknown"])
def test_site_does_not_overwrite_unknown_or_modified_v1_home(change):
    config = _conflicting_home(change)
    saved = deepcopy(config)
    with pytest.raises(ValueError):
        site_layout(config)
    assert config == saved


@pytest.mark.parametrize("width,sizes", [("default", ["full", "small", "small"]),
                                       ("slim", ["full", "small"]),
                                       ("default", ["full", "full"])])
def test_site_refuses_a_masthead_that_would_exceed_glance_column_capacity(width, sizes):
    config = _config()
    page = config["pages"][1]
    page["width"] = width
    page["columns"].extend({"size": size, "widgets": []} for size in sizes[1:])
    saved = deepcopy(config)
    with pytest.raises(ValueError, match="room for one masthead column"):
        site_layout(config)
    assert config == saved


def test_site_cli_private_exclusive_candidate_and_legacy_generator_refuses_site(tmp_path):
    source, output = tmp_path / "source.yml", tmp_path / "candidate.yml"
    config = _installed_home()
    source.write_text(yaml.safe_dump(config), encoding="utf-8")
    source_bytes = source.read_bytes()
    script = Path(__file__).resolve().parents[1] / "scripts" / "prepare_glance_site.py"
    result = subprocess.run([sys.executable, str(script), str(source), str(output)],
                            check=True, capture_output=True, text=True)

    assert "not installed" in result.stdout
    candidate_bytes = output.read_bytes()
    candidate = yaml.safe_load(candidate_bytes)
    assert candidate == site_layout(config)
    assert output.stat().st_mode & 0o777 == 0o600
    with pytest.raises(FileExistsError):
        main([str(source), str(output)])
    with pytest.raises(SystemExit):
        main([str(source), str(source)])
    with pytest.raises(ValueError):
        reading_layout(candidate, institutional_style=True)
    assert source.read_bytes() == source_bytes
    assert output.read_bytes() == candidate_bytes
