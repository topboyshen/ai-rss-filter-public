from copy import deepcopy
from pathlib import Path

import pytest
import yaml

from scripts.prepare_glance_reading import main, reading_layout


MOBILE_STYLE_ID = "ai-filter-reading-mobile-v1"


def _mobile_style_block():
    css = (Path(__file__).resolve().parents[1] / "scripts" /
           "glance_reading_mobile.css").read_text(encoding="utf-8")
    return f'<style id="{MOBILE_STYLE_ID}">\n{css}</style>'


def _config():
    def extension(path):
        return {"type": "extension", "url": "${PROBE_ORIGIN}:5000" + path,
                "cache": "30s", "allow-potentially-dangerous-html": True}

    return {"theme": {"light": False}, "pages": [
        {"name": "情报总控台", "columns": [
            {"size": "full", "widgets": [extension(path) for path in
             ("/quotes", "/briefing-card?min_score=7&limit=12", "/glance-sse",
              "/event-radar-card?limit=8", "/score-backtest?days=90&view=compact")]},
            {"size": "small", "widgets": [
                {"type": "bookmarks", "groups": [{"links": [{"url": "/private"}]}]},
                {"type": "monitor", "sites": [{"check-url": "http://service/health"}]},
            ]},
        ]},
        *[{"name": name, "slug": name, "columns": [{"size": "full", "widgets":
            [extension(path)] if path else []}]}
          for name, path in (("events", "/event-radar-card?limit=30"),
                             ("quality", "/score-backtest?days=90"), ("ops", ""))],
    ]}


def _custom_config():
    config = _config()
    config["document"] = {
        "head": '<meta name="existing" content="保留">\r\n'
                '<style>.existing { color: red; }</style>\n<!-- existing head -->\n',
        "base-url": "/reader/",
        "extension-options": {"preserve": ["all", "values"]},
    }
    config["theme"]["custom-css-file"] = "/assets/custom.css"
    config["branding"] = {
        "logo-text": "Existing branding",
        "custom-footer": '<div style="position:fixed;bottom:0">\n'
                         '  <a href="https://beian.miit.gov.cn/">备案号测试占位值</a>\n'
                         '  <a href="https://beian.mps.gov.cn/">公安备案测试占位值</a>\n'
                         '</div>\n<!-- Keep every footer byte. -->\n',
    }
    return config


def test_home_reorders_without_changing_profiles_or_other_pages():
    original = _config()
    saved = deepcopy(original)
    result = reading_layout(original)
    assert original == saved
    assert result["theme"] == original["theme"]
    assert result["pages"][1:3] == original["pages"][1:3]
    assert result["pages"][0]["name"] == original["pages"][0]["name"]
    columns = result["pages"][0]["columns"]
    news = columns[0]["widgets"]
    side = columns[1]["widgets"]
    assert len(news) == 1 and news[0]["url"].endswith("/glance-sse")
    assert side[0]["url"].endswith("/briefing-card?min_score=7&limit=12")
    assert side[1]["url"].endswith("/quotes?view=reading")
    assert all(widget["cache"] == "30s" for widget in [news[0], *side[:2]])
    assert side[2] == original["pages"][0]["columns"][1]["widgets"][0]
    assert result["pages"][3]["columns"][0]["widgets"][-1]["type"] == "monitor"
    assert reading_layout(result) == result


def test_mobile_style_preserves_existing_head_footer_theme_and_document_fields():
    original = _custom_config()
    saved = deepcopy(original)

    result = reading_layout(original)

    expected_document = deepcopy(original["document"])
    expected_document["head"] += "\n" + _mobile_style_block()
    assert result["document"] == expected_document
    assert result["branding"] == original["branding"]
    assert result["branding"]["custom-footer"].encode("utf-8") == (
        original["branding"]["custom-footer"].encode("utf-8")
    )
    assert result["theme"] == original["theme"]
    assert result["document"]["head"].count(MOBILE_STYLE_ID) == 1
    assert original == saved


@pytest.mark.parametrize("document", [None, {}, {"head": ""}])
def test_mobile_style_is_added_when_document_head_is_absent_or_empty(document):
    original = _config()
    if document is not None:
        original["document"] = document
    saved = deepcopy(original)

    result = reading_layout(original)

    assert result["document"]["head"] == _mobile_style_block()
    assert reading_layout(result) == result
    assert original == saved


def test_existing_reading_layout_is_upgraded_once_without_moving_widgets_again():
    original = reading_layout(_custom_config())
    # Model the deployed reading layout before mobile styles were introduced.
    original["document"]["head"] = _custom_config()["document"]["head"]
    saved = deepcopy(original)

    result = reading_layout(original)

    assert result["pages"] == original["pages"]
    assert result["document"]["head"] == original["document"]["head"] + "\n" + _mobile_style_block()
    assert result["branding"] == original["branding"]
    assert result["theme"] == original["theme"]
    assert reading_layout(result) == result
    assert result["document"]["head"].count(MOBILE_STYLE_ID) == 1
    assert original == saved


@pytest.mark.parametrize("document", [None, [], "head", {"head": None},
                                      {"head": []}, {"head": {}}, {"head": 12}])
def test_invalid_document_or_head_is_rejected_without_mutating_input(document):
    original = _config()
    original["document"] = document
    saved = deepcopy(original)

    with pytest.raises(ValueError, match="Expected Glance document"):
        reading_layout(original)

    assert original == saved


@pytest.mark.parametrize("conflict", ["different-style", "duplicate", "trailing-html", "marker-only"])
def test_conflicting_mobile_marker_is_rejected_without_mutating_input(conflict):
    original = _custom_config()
    block = _mobile_style_block()
    conflicts = {
        "different-style": f'<style id="{MOBILE_STYLE_ID}">.custom {{ color: red; }}</style>',
        "duplicate": block + "\n" + block,
        "trailing-html": block + "\n<!-- Added after generated style. -->",
        "marker-only": f"<!-- {MOBILE_STYLE_ID} -->",
    }
    original["document"]["head"] += "\n" + conflicts[conflict]
    saved = deepcopy(original)

    with pytest.raises(ValueError, match="Existing reading mobile style differs"):
        reading_layout(original)

    assert original == saved


@pytest.mark.parametrize("change", ["unknown", "duplicate", "profile", "missing-page"])
def test_unexpected_layout_is_not_silently_discarded(change):
    config = _config()
    widgets = config["pages"][0]["columns"][0]["widgets"]
    if change == "unknown":
        widgets.append({"type": "rss"})
    elif change == "duplicate":
        widgets.append(deepcopy(widgets[2]))
    elif change == "profile":
        widgets[1]["url"] = "http://service/briefing-card?limit=3"
    else:
        config["pages"].pop()
    with pytest.raises(ValueError):
        reading_layout(config)


@pytest.mark.parametrize("change", ["empty", "different", "custom-home"])
def test_research_widgets_are_not_discarded_without_equivalent_destination(change):
    config = _config()
    if change == "empty":
        config["pages"][1]["columns"][0]["widgets"] = []
    elif change == "different":
        config["pages"][2]["columns"][0]["widgets"][0]["url"] = "http://service/score-backtest?days=7"
    else:
        config["pages"][0]["columns"][0]["widgets"][3]["url"] += "&theme=custom"
    with pytest.raises(ValueError):
        reading_layout(config)


def test_prepare_only_writes_new_candidate_and_never_overwrites(tmp_path):
    source, output = tmp_path / "source.yml", tmp_path / "candidate.yml"
    config = _custom_config()
    source.write_text(yaml.safe_dump(config, allow_unicode=True), encoding="utf-8")
    source_bytes = source.read_bytes()
    assert main([str(source), str(output)]) == 0
    assert source.read_bytes() == source_bytes
    assert output.stat().st_mode & 0o777 == 0o600
    candidate_bytes = output.read_bytes()
    assert yaml.safe_load(output.read_text()) == reading_layout(config)
    with pytest.raises(FileExistsError):
        main([str(source), str(output)])
    with pytest.raises(SystemExit):
        main([str(source), str(source)])
    assert source.read_bytes() == source_bytes
    assert output.read_bytes() == candidate_bytes


@pytest.mark.parametrize("invalid", ["document", "head", "marker"])
@pytest.mark.parametrize("output_exists", [False, True])
def test_invalid_candidate_never_changes_source_or_output(tmp_path, invalid, output_exists):
    source, output = tmp_path / "source.yml", tmp_path / "candidate.yml"
    config = _custom_config()
    if invalid == "document":
        config["document"] = []
    elif invalid == "head":
        config["document"]["head"] = None
    else:
        config["document"]["head"] += f'\n<style id="{MOBILE_STYLE_ID}">old style</style>'
    source.write_text(yaml.safe_dump(config, allow_unicode=True), encoding="utf-8")
    source_bytes = source.read_bytes()
    previous_candidate = b"Existing candidate must remain unchanged.\n"
    if output_exists:
        output.write_bytes(previous_candidate)

    with pytest.raises(ValueError):
        main([str(source), str(output)])

    assert source.read_bytes() == source_bytes
    if output_exists:
        assert output.read_bytes() == previous_candidate
    else:
        assert not output.exists()
