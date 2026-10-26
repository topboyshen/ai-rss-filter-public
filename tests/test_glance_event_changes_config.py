"""Local-only opt-in configuration for the homepage event-changes card."""

from copy import deepcopy
from pathlib import Path
import subprocess
import sys

import pytest
import yaml

from scripts.prepare_glance_event_changes import EVENT_CHANGES_CLASS, event_changes_layout, main
from scripts.prepare_glance_reading import reading_layout
from test_home_glance_layout import _custom_config


def _reading():
    config = reading_layout(_custom_config())
    source = config["pages"][1]["columns"][0]["widgets"][0]
    source.update(headers={"X-API-Key": "${SYNTHETIC_API_KEY}"}, timeout="7s",
                  **{"hide-header": False, "css-class": "existing-research-class"})
    return config


def test_candidate_changes_only_home_sidebar_and_copies_source_connection_options():
    config = _reading()
    original = deepcopy(config)

    candidate = event_changes_layout(config)

    expected = deepcopy(original)
    widget = deepcopy(original["pages"][1]["columns"][0]["widgets"][0])
    widget.update(title="今日事件变化", cache="30s", **{"css-class": EVENT_CHANGES_CLASS})
    widget["url"] = widget["url"].replace("limit=30", "limit=8")
    expected["pages"][0]["columns"][1]["widgets"].insert(1, widget)
    assert candidate == expected
    assert config == original
    assert candidate["pages"][1:] == original["pages"][1:]
    assert candidate["pages"][0]["columns"][1]["widgets"][2]["css-class"] == "ai-home-market"
    assert event_changes_layout(candidate) == candidate
    assert reading_layout(candidate) == candidate


@pytest.mark.parametrize("style", [{}, {"reading_skin": True}, {"institutional_style": True}])
def test_old_layout_does_not_implicitly_opt_in_and_regeneration_keeps_the_new_card(style):
    old = reading_layout(_custom_config(), **style)
    assert not any(widget.get("css-class") == EVENT_CHANGES_CLASS
                   for column in old["pages"][0]["columns"] for widget in column["widgets"])
    candidate = event_changes_layout(old)
    assert reading_layout(candidate, **style) == candidate
    assert event_changes_layout(candidate) == candidate


@pytest.mark.parametrize("query", ["proxy=a%2Fb&limit=30", "limit=30&token=${SYNTHETIC_TOKEN}",
                                   "limit=30&view=compact", "limit=30&", "limit=%33%30"])
def test_unknown_query_parameters_are_rejected_not_copied_or_silently_stripped(query):
    config = _reading()
    source = config["pages"][1]["columns"][0]["widgets"][0]
    source["url"] = "${PROBE_ORIGIN}:5000/event-radar-card?" + query
    original = deepcopy(config)
    with pytest.raises(ValueError, match="without extra query"):
        event_changes_layout(config)
    assert config == original


def test_unrelated_custom_page_is_not_reinterpreted_by_reading_regeneration():
    config = _reading()
    config["pages"].append({"name": "custom", "slug": "custom", "columns": [{"custom-option": True}]})
    assert reading_layout(config) == config
    candidate = event_changes_layout(config)
    assert candidate["pages"][-1] == config["pages"][-1]
    assert reading_layout(candidate) == candidate


@pytest.mark.parametrize("url", [
    "https://service/event-radar-card", "https://service/event-radar-card?limit=8",
    "https://service/event-radar-card?limit=30&limit=30",
    "https://service/event-radar-card?limit=30&%6cimit=30",
    "https://service/event-radar-card?limit=30#different", "javascript:/event-radar-card?limit=30",
    "/event-radar-card?limit=30",
])
def test_source_profile_conflicts_are_rejected_without_modifying_configuration(url):
    config = _reading()
    config["pages"][1]["columns"][0]["widgets"][0]["url"] = url
    original = deepcopy(config)
    with pytest.raises(ValueError):
        event_changes_layout(config)
    assert config == original


@pytest.mark.parametrize("change", [
    "news-marker", "brief-marker", "market-marker", "brief-endpoint", "market-endpoint",
    "sidebar-size", "sidebar-order", "events-missing", "events-duplicate", "source-duplicate",
    "html-disabled", "home-radar", "news-duplicate",
])
def test_unknown_or_ambiguous_source_and_destination_fail_closed(change):
    config = _reading()
    home = config["pages"][0]
    sidebar = home["columns"][1]
    source = config["pages"][1]["columns"][0]["widgets"][0]
    if change == "news-marker":
        home["columns"][0]["widgets"][0].pop("css-class")
    elif change in {"brief-marker", "market-marker"}:
        sidebar["widgets"][0 if change == "brief-marker" else 1].pop("css-class")
    elif change in {"brief-endpoint", "market-endpoint"}:
        sidebar["widgets"][0 if change == "brief-endpoint" else 1]["url"] = "http://service/other"
    elif change == "sidebar-size":
        sidebar["size"] = "full"
    elif change == "sidebar-order":
        sidebar["widgets"][0], sidebar["widgets"][1] = sidebar["widgets"][1], sidebar["widgets"][0]
    elif change == "events-missing":
        config["pages"].pop(1)
    elif change == "events-duplicate":
        config["pages"].append(deepcopy(config["pages"][1]))
    elif change == "source-duplicate":
        config["pages"][1]["columns"][0]["widgets"].append(deepcopy(source))
    elif change == "html-disabled":
        source["allow-potentially-dangerous-html"] = False
    elif change == "news-duplicate":
        home["columns"][0]["widgets"].append(deepcopy(home["columns"][0]["widgets"][0]))
    else:
        sidebar["widgets"].insert(1, deepcopy(source))
    original = deepcopy(config)
    with pytest.raises(ValueError):
        event_changes_layout(config)
    assert config == original


@pytest.mark.parametrize("change", ["title", "limit", "query", "headers", "extra-class", "duplicate", "position", "other-page"])
def test_existing_opt_in_conflicts_are_never_overwritten_or_silently_dropped(change):
    config = event_changes_layout(_reading())
    sidebar = config["pages"][0]["columns"][1]["widgets"]
    widget = sidebar[1]
    if change == "title":
        widget["title"] = "Custom title"
    elif change == "limit":
        widget["url"] = widget["url"].replace("limit=8", "limit=9")
    elif change == "query":
        widget["url"] += "&proxy=custom"
    elif change == "headers":
        widget["headers"] = {"X-API-Key": "${DIFFERENT_TOKEN}"}
    elif change == "extra-class":
        widget["css-class"] += " custom-class"
    elif change == "duplicate":
        sidebar.append(deepcopy(widget))
    elif change == "position":
        sidebar.append(sidebar.pop(1))
    else:
        config["pages"][2]["columns"][0]["widgets"].append(sidebar.pop(1))
    original = deepcopy(config)
    for transform in (event_changes_layout, reading_layout):
        with pytest.raises(ValueError, match="event-changes"):
            transform(config)
        assert config == original


def test_cli_creates_private_candidate_without_source_changes_or_overwriting(tmp_path, capsys):
    source, output = tmp_path / "source.yml", tmp_path / "candidate.yml"
    config = _reading()
    source.write_text(yaml.safe_dump(config, allow_unicode=True), encoding="utf-8")
    original = source.read_bytes()
    assert main([str(source), str(output)]) == 0
    content = output.read_bytes()
    assert yaml.safe_load(content) == event_changes_layout(config)
    assert output.stat().st_mode & 0o777 == 0o600
    assert "not installed, no services reloaded" in capsys.readouterr().out
    with pytest.raises(FileExistsError):
        main([str(source), str(output)])
    with pytest.raises(SystemExit):
        main([str(source), str(source)])
    alias = tmp_path / "alias.yml"
    alias.symlink_to(source)
    with pytest.raises(SystemExit):
        main([str(source), str(alias)])
    assert source.read_bytes() == original
    assert output.read_bytes() == content


def test_cli_failure_leaves_no_candidate_and_does_not_disclose_configuration(tmp_path, capsys):
    source, output = tmp_path / "source.yml", tmp_path / "candidate.yml"
    config = _reading()
    config["pages"][1]["columns"][0]["widgets"][0]["url"] += "&limit=99"
    source.write_text(yaml.safe_dump(config), encoding="utf-8")
    original = source.read_bytes()
    with pytest.raises(ValueError):
        main([str(source), str(output)])
    assert source.read_bytes() == original
    assert not output.exists()
    assert "SYNTHETIC_API_KEY" not in capsys.readouterr().out


def test_script_entry_point_works_without_installing_project_package(tmp_path):
    source, output = tmp_path / "source.yml", tmp_path / "candidate.yml"
    source.write_text(yaml.safe_dump(_reading(), allow_unicode=True), encoding="utf-8")
    script = Path(__file__).resolve().parents[1] / "scripts" / "prepare_glance_event_changes.py"
    result = subprocess.run([sys.executable, str(script), str(source), str(output)], cwd=tmp_path,
                            text=True, capture_output=True, timeout=10, check=False)
    assert result.returncode == 0, result.stderr
    assert yaml.safe_load(output.read_text(encoding="utf-8")) == event_changes_layout(_reading())
