"""Config include expansion: relative, bounded and invisible to runtime consumers."""

from pathlib import Path

import pytest

import core.config as config_module
from core.config import ConfigError, load_config, load_config_file

ROOT = Path(__file__).resolve().parents[1]


def _write(root: Path, name: str, text: str) -> Path:
    path = root / name
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")
    return path


def test_includes_merge_in_order_and_the_including_file_wins(tmp_path):
    _write(tmp_path, "rules/a.yaml", "threshold:\n  pass: 5\n  notify: 8\nfeeds:\n  - url: a\n")
    _write(tmp_path, "rules/b.yaml", "threshold:\n  pass: 6\nmax_entries: 20\n")
    main = _write(
        tmp_path,
        "config.yaml",
        "include:\n  - rules/a.yaml\n  - rules/b.yaml\nthreshold:\n  notify: 9\n",
    )

    cfg = load_config_file(main)

    assert cfg == {
        "threshold": {"pass": 6, "notify": 9},
        "feeds": [{"url": "a"}],
        "max_entries": 20,
    }
    assert "include" not in cfg


def test_nested_includes_are_resolved_relative_to_their_own_file(tmp_path):
    _write(tmp_path, "rules/deep/base.yaml", "blacklist:\n  - 新股申购\n")
    _write(tmp_path, "rules/outer.yaml", "include:\n  - deep/base.yaml\nfast_pass:\n  enabled: true\n")
    main = _write(tmp_path, "config.yaml", "include:\n  - rules/outer.yaml\n")

    cfg = load_config_file(main)

    assert cfg == {"blacklist": ["新股申购"], "fast_pass": {"enabled": True}}


def test_empty_include_and_null_include_contribute_nothing(tmp_path):
    _write(tmp_path, "rules/empty.yaml", "")
    main = _write(tmp_path, "config.yaml", "include:\n  - rules/empty.yaml\nmax_entries: 9\n")
    assert load_config_file(main) == {"max_entries": 9}

    null_main = _write(tmp_path, "null.yaml", "include:\nmax_entries: 10\n")
    assert load_config_file(null_main) == {"max_entries": 10}


def test_missing_include_file_is_rejected(tmp_path):
    main = _write(tmp_path, "config.yaml", "include:\n  - rules/missing.yaml\n")

    with pytest.raises(ConfigError, match="不存在"):
        load_config_file(main)


@pytest.mark.parametrize("value", ["/etc/passwd", "../outside.yaml", "rules/../../outside.yaml"])
def test_absolute_and_parent_paths_are_rejected(tmp_path, value):
    main = _write(tmp_path, "config.yaml", f"include:\n  - {value}\n")

    with pytest.raises(ConfigError, match="相对路径"):
        load_config_file(main)


def test_symlink_escape_is_rejected(tmp_path):
    outside = tmp_path / "outside.yaml"
    outside.write_text("max_entries: 99\n", encoding="utf-8")
    base = tmp_path / "base"
    base.mkdir()
    link = base / "link.yaml"
    try:
        link.symlink_to(outside)
    except (OSError, NotImplementedError):
        pytest.skip("symlinks unavailable")
    main = _write(base, "config.yaml", "include:\n  - link.yaml\n")

    with pytest.raises(ConfigError, match="越出配置目录"):
        load_config_file(main)


def test_include_cycle_is_rejected(tmp_path):
    _write(tmp_path, "a.yaml", "include:\n  - b.yaml\n")
    _write(tmp_path, "b.yaml", "include:\n  - a.yaml\n")
    main = _write(tmp_path, "config.yaml", "include:\n  - a.yaml\n")

    with pytest.raises(ConfigError, match="循环引用"):
        load_config_file(main)


def test_include_depth_is_bounded(tmp_path):
    for index in range(9):
        _write(tmp_path, f"level{index}.yaml", f"include:\n  - level{index + 1}.yaml\n")
    _write(tmp_path, "level9.yaml", "max_entries: 1\n")
    main = _write(tmp_path, "config.yaml", "include:\n  - level0.yaml\n")

    with pytest.raises(ConfigError, match="嵌套超过"):
        load_config_file(main)


@pytest.mark.parametrize("text", ["include: rules/a.yaml\n", "include:\n  - 7\n"])
def test_include_must_be_a_string_list(tmp_path, text):
    main = _write(tmp_path, "config.yaml", text)

    with pytest.raises(ConfigError, match="include 必须"):
        load_config_file(main)


def test_non_mapping_include_file_is_rejected(tmp_path):
    _write(tmp_path, "rules/list.yaml", "- not\n- a\n- mapping\n")
    main = _write(tmp_path, "config.yaml", "include:\n  - rules/list.yaml\n")

    with pytest.raises(TypeError, match="YAML mapping"):
        load_config_file(main)


def test_load_config_drops_the_directive_and_applies_defaults(tmp_path, monkeypatch):
    _write(tmp_path, "rules/filters.yaml", "blacklist:\n  - 新股申购\n")
    main = _write(
        tmp_path,
        "config.yaml",
        "include:\n  - rules/filters.yaml\nscoring_strategy_version: include-test\n",
    )
    monkeypatch.setattr(config_module, "CONFIG_PATH", str(main))
    monkeypatch.delenv("AI_FILTER_API_KEY", raising=False)
    monkeypatch.delenv("API_KEY", raising=False)

    cfg = load_config()

    assert cfg["blacklist"] == ["新股申购"]
    assert "include" not in cfg
    assert cfg["max_entries"] == config_module.DEFAULTS["max_entries"]
    assert cfg["scoring_strategy_version"].startswith("include-test:")


def test_repository_config_is_split_and_still_resolves():
    raw = (ROOT / "config.yaml").read_text(encoding="utf-8")
    assert "include:" in raw
    assert (ROOT / "config" / "rules" / "feeds.yaml").is_file()

    cfg = load_config_file(ROOT / "config.yaml")

    for section in (
        "blacklist",
        "highlight_keywords",
        "local_reject_rules",
        "local_scoring",
        "fast_pass",
        "feeds",
        "briefing_themes",
        "prompts",
        "prompt_matchers",
    ):
        assert section in cfg, section
    assert cfg["feeds"]
    assert "include" not in cfg
