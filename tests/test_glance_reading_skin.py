"""Regression coverage for the optional, presentation-only homepage skin."""

from copy import deepcopy
from pathlib import Path
import re

import pytest
import yaml

from scripts.prepare_glance_reading import main, reading_layout
from test_home_glance_layout import (
    MOBILE_STYLE_ID,
    _config,
    _custom_config,
    _mobile_style_block,
)


SKIN_STYLE_ID = "ai-filter-reading-skin-v1"
SKIN_PATH = Path(__file__).resolve().parents[1] / "scripts" / "glance_reading_skin.css"


def _skin_style_block():
    return f'<style id="{SKIN_STYLE_ID}">\n{SKIN_PATH.read_text(encoding="utf-8")}</style>'


def _conflicting_config(conflict):
    config = reading_layout(_custom_config())
    skin = _skin_style_block()
    conflicts = {
        "different-style": f'<style id="{SKIN_STYLE_ID}">.custom {{ color: red; }}</style>',
        "duplicate": skin + "\n" + skin,
        "marker-only": f"<!-- {SKIN_STYLE_ID} -->",
        "misplaced-style": skin + "\n<!-- Added after the generated skin. -->",
    }
    head = config["document"]["head"]
    mobile = _mobile_style_block()
    config["document"]["head"] = head[:-len(mobile)] + conflicts[conflict] + "\n" + mobile
    return config


def _selector_branches(selectors):
    """Split selector lists while respecting commas inside :is() and :has()."""
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


def test_reading_skin_is_opt_in_and_default_head_stays_unchanged():
    config = _custom_config()
    saved = deepcopy(config)

    default = reading_layout(config)

    assert default == reading_layout(config, reading_skin=False)
    assert default["document"]["head"] == (
        config["document"]["head"] + "\n" + _mobile_style_block()
    )
    assert SKIN_STYLE_ID not in default["document"]["head"]
    assert config == saved


def test_skin_changes_only_head_and_preserves_every_profile_and_custom_setting():
    config = _custom_config()
    config["auth"] = {"secret-key": "${SYNTHETIC_SESSION_SECRET}"}
    for page in config["pages"]:
        for column in page["columns"]:
            for widget in column["widgets"]:
                if widget["type"] == "extension":
                    widget["headers"] = {"Authorization": "Bearer ${SYNTHETIC_WIDGET_TOKEN}"}
    saved = deepcopy(config)
    regular = reading_layout(config)

    result = reading_layout(config, reading_skin=True)

    mobile, skin = _mobile_style_block(), _skin_style_block()
    expected = deepcopy(regular)
    expected["document"]["head"] = regular["document"]["head"][:-len(mobile)] + skin + "\n" + mobile
    assert result == expected
    assert result["document"]["head"].startswith(config["document"]["head"])
    assert result["document"]["head"].endswith(mobile)
    assert result["document"]["head"].count(SKIN_STYLE_ID) == 1
    assert result["document"]["head"].count(MOBILE_STYLE_ID) == 1
    assert result["branding"]["custom-footer"].encode() == config["branding"]["custom-footer"].encode()
    assert config == saved


@pytest.mark.parametrize("document", [None, {}, {"head": ""}])
def test_skin_supports_absent_or_empty_document_head(document):
    config = _config()
    if document is not None:
        config["document"] = document
    saved = deepcopy(config)

    result = reading_layout(config, reading_skin=True)

    assert result["document"]["head"] == _skin_style_block() + "\n" + _mobile_style_block()
    assert config == saved


def test_existing_reading_layout_upgrades_once_and_default_retains_installed_skin():
    regular = reading_layout(_custom_config())
    saved = deepcopy(regular)

    result = reading_layout(regular, reading_skin=True)

    assert result["pages"] == regular["pages"]
    assert reading_layout(result, reading_skin=True) == result
    assert reading_layout(result) == result
    assert reading_layout(result, reading_skin=False) == result
    assert regular == saved


@pytest.mark.parametrize("conflict", ["different-style", "duplicate", "marker-only", "misplaced-style"])
def test_conflicting_skin_is_rejected_without_mutating_config(conflict):
    config = _conflicting_config(conflict)
    saved = deepcopy(config)

    with pytest.raises(ValueError, match="reading skin"):
        reading_layout(config, reading_skin=True)

    assert config == saved


def test_reading_skin_cli_writes_private_candidate_and_never_overwrites(tmp_path):
    source, output = tmp_path / "source.yml", tmp_path / "candidate.yml"
    config = _custom_config()
    source.write_text(yaml.safe_dump(config, allow_unicode=True), encoding="utf-8")
    source_bytes = source.read_bytes()

    assert main([str(source), str(output), "--reading-skin"]) == 0

    candidate_bytes = output.read_bytes()
    assert yaml.safe_load(candidate_bytes) == reading_layout(config, reading_skin=True)
    assert output.stat().st_mode & 0o777 == 0o600
    with pytest.raises(FileExistsError):
        main([str(source), str(output), "--reading-skin"])
    with pytest.raises(SystemExit):
        main([str(source), str(source), "--reading-skin"])
    assert source.read_bytes() == source_bytes
    assert output.read_bytes() == candidate_bytes


@pytest.mark.parametrize("conflict", ["different-style", "duplicate", "marker-only", "misplaced-style"])
@pytest.mark.parametrize("output_exists", [False, True])
def test_conflicting_skin_cli_preserves_source_and_output(tmp_path, conflict, output_exists):
    source, output = tmp_path / "source.yml", tmp_path / "candidate.yml"
    source.write_text(yaml.safe_dump(_conflicting_config(conflict), allow_unicode=True), encoding="utf-8")
    source_bytes = source.read_bytes()
    previous_candidate = b"Existing candidate must remain unchanged.\n"
    if output_exists:
        output.write_bytes(previous_candidate)

    with pytest.raises(ValueError, match="reading skin"):
        main([str(source), str(output), "--reading-skin"])

    assert source.read_bytes() == source_bytes
    if output_exists:
        assert output.read_bytes() == previous_candidate
    else:
        assert not output.exists()


def test_skin_css_is_home_scoped_and_does_not_add_scripts_or_network_requests():
    css = SKIN_PATH.read_text(encoding="utf-8")
    css = re.sub(r"/\*.*?\*/", "", css, flags=re.S)

    assert not re.search(r"@import\b|url\s*\(|<\s*/?\s*script\b|expression\s*\(", css, re.I)
    rules = [match.strip() for match in re.findall(r"([^{}]+)\{", css)]
    selectors = [branch for rule in rules if not rule.startswith("@")
                 for branch in _selector_branches(rule)]
    assert selectors, "The skin must contain actual style rules"
    assert all(re.search(r"\.ai-home-news\b", selector) for selector in selectors), selectors
