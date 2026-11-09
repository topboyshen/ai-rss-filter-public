"""Config sections must be rejected at load time when their shape is wrong.

A null or scalar ``threshold``/``security``/``telegram`` section used to load
fine and then crash deep inside the scoring or notification path (or, for
``security``, inside ``_apply_secrets_from_env``).  These tests pin the fail-fast
behaviour: ``load_config`` raises ``ConfigError`` and names the offending path.
"""

import importlib
from pathlib import Path

import pytest

config_module = importlib.import_module("core.config")

_ENV_VARS = (
    "AI_FILTER_API_KEY",
    "API_KEY",
    "TELEGRAM_BOT_TOKEN",
    "TELEGRAM_CHAT_ID",
    "AI_FILTER_ALLOW_QUERY_API_KEY",
    "AI_FILTER_ALLOW_UNCONFIGURED_FEED_URLS",
    "AI_FILTER_CORS_ALLOWED_ORIGINS",
    "AI_FILTER_RSSHUB_BASE_URL",
    "AI_FILTER_WEB_BASE_URL",
)


def _clear_env(monkeypatch):
    for name in _ENV_VARS:
        monkeypatch.delenv(name, raising=False)


def _load(tmp_path, monkeypatch, text, name="config.yaml"):
    path = tmp_path / name
    path.write_text(text, encoding="utf-8")
    monkeypatch.setattr(config_module, "CONFIG_PATH", str(path))
    _clear_env(monkeypatch)
    return config_module.load_config()


@pytest.mark.parametrize(
    "text,path",
    [
        pytest.param("security:\n", "security", id="null-security-section"),
        pytest.param("threshold:\n", "threshold", id="null-threshold-section"),
        pytest.param('telegram: "on"\n', "telegram", id="scalar-telegram-section"),
        pytest.param("output:\n  - 1\n", "output", id="list-output-section"),
        pytest.param("blacklist: 123\n", "blacklist", id="scalar-blacklist"),
        pytest.param(
            "blacklist:\n  - 123\n", "blacklist[0]", id="non-str-blacklist-item"
        ),
        pytest.param(
            "highlight_keywords:\n  - {}\n",
            "highlight_keywords[0]",
            id="non-str-highlight-keyword",
        ),
        pytest.param(
            'prompt_matchers:\n  - "wallstreetcn"\n',
            "prompt_matchers[0]",
            id="non-mapping-prompt-matcher",
        ),
        pytest.param(
            'feeds:\n  - "http://127.0.0.1:1200/x"\n',
            "feeds[0]",
            id="non-mapping-feed-entry",
        ),
        pytest.param(
            "feeds:\n  - url: 123\n", "feeds[0].url", id="non-str-feed-url"
        ),
        pytest.param('max_workers: "5"\n', "max_workers", id="str-max-workers"),
        pytest.param(
            'threshold:\n  similarity: "0.42"\n',
            "threshold.similarity",
            id="str-similarity",
        ),
        pytest.param("threshold:\n  pass: null\n", "threshold.pass", id="null-pass"),
        pytest.param(
            'local_scoring:\n  enabled: "yes"\n',
            "local_scoring.enabled",
            id="str-boolean-flag",
        ),
        pytest.param(
            "briefing_assembly:\n  grounding: null\n",
            "briefing_assembly.grounding",
            id="null-grounding-section",
        ),
    ],
)
def test_invalid_configuration_shape_is_rejected(tmp_path, monkeypatch, text, path):
    with pytest.raises(config_module.ConfigError) as excinfo:
        _load(tmp_path, monkeypatch, text)

    assert path in str(excinfo.value)


@pytest.mark.parametrize(
    "text",
    [
        pytest.param("", id="empty-file"),
        pytest.param("threshold:\n  pass: 6.5\n  notify: 8\n", id="numeric-family"),
        pytest.param("telegram:\n  enabled: true\n", id="bool-section"),
        pytest.param(
            "feeds:\n  - url: http://127.0.0.1:1200/x\n    name: x\n    max_entries: 5\n",
            id="feed-entry",
        ),
        pytest.param("blacklist: []\n", id="empty-blacklist"),
        pytest.param("output: {}\n", id="empty-section-keeps-defaults"),
        pytest.param(
            "security:\n  cors_allowed_origins:\n    - https://a.example\n",
            id="cors-origin-list",
        ),
        pytest.param(
            "security:\n  cors_allowed_origins: https://a.example\n",
            id="cors-origin-string",
        ),
        pytest.param("prompts:\n  macro: |\n    自由文本\n", id="free-form-prompts"),
        pytest.param(
            "two_stage_scoring:\n  enabled: true\n", id="undeclared-section"
        ),
    ],
)
def test_well_formed_configuration_loads(tmp_path, monkeypatch, text):
    cfg = _load(tmp_path, monkeypatch, text)

    # 未覆盖的键仍由 DEFAULTS 补齐，校验不得改变合法配置的内容
    assert cfg["output"]["max_items"] == 10
    assert cfg["max_entries"] == 15


def test_repository_configuration_passes_validation(monkeypatch):
    repo_root = Path(config_module.__file__).resolve().parents[1]
    config_file = repo_root / "config.yaml"
    if not config_file.exists():
        pytest.skip("config.yaml is not present in this checkout")

    monkeypatch.setattr(config_module, "CONFIG_PATH", str(config_file))
    _clear_env(monkeypatch)

    cfg = config_module.load_config()

    assert isinstance(cfg["threshold"], dict)
    assert isinstance(cfg["feeds"], list)


def test_environment_overrides_are_applied_after_shape_validation(
    tmp_path, monkeypatch
):
    path = tmp_path / "config.yaml"
    path.write_text("", encoding="utf-8")
    monkeypatch.setattr(config_module, "CONFIG_PATH", str(path))
    _clear_env(monkeypatch)
    monkeypatch.setenv("AI_FILTER_CORS_ALLOWED_ORIGINS", "https://a.example,https://b.example")

    cfg = config_module.load_config()

    assert cfg["security"]["cors_allowed_origins"] == [
        "https://a.example",
        "https://b.example",
    ]


def test_every_offending_path_is_reported_together(tmp_path, monkeypatch):
    with pytest.raises(config_module.ConfigError) as excinfo:
        _load(tmp_path, monkeypatch, "threshold:\nsecurity:\nblacklist:\n  - 1\n")

    message = str(excinfo.value)
    assert "threshold" in message
    assert "security" in message
    assert "blacklist[0]" in message


def test_null_security_section_fails_before_env_overrides(tmp_path, monkeypatch):
    path = tmp_path / "config.yaml"
    path.write_text("security:\n", encoding="utf-8")
    monkeypatch.setattr(config_module, "CONFIG_PATH", str(path))
    _clear_env(monkeypatch)
    monkeypatch.setenv("AI_FILTER_API_KEY", "from_env")

    with pytest.raises(config_module.ConfigError) as excinfo:
        config_module.load_config()

    assert "security" in str(excinfo.value)


def test_config_holder_refuses_invalid_configuration(tmp_path, monkeypatch):
    path = tmp_path / "config.yaml"
    path.write_text("threshold:\n", encoding="utf-8")
    monkeypatch.setattr(config_module, "CONFIG_PATH", str(path))
    _clear_env(monkeypatch)

    with pytest.raises(config_module.ConfigError):
        config_module.ConfigHolder()


def test_reload_keeps_previous_configuration_when_new_one_is_invalid(
    tmp_path, monkeypatch
):
    good = tmp_path / "good.yaml"
    good.write_text("threshold:\n  pass: 6\n", encoding="utf-8")
    monkeypatch.setattr(config_module, "CONFIG_PATH", str(good))
    _clear_env(monkeypatch)
    holder = config_module.ConfigHolder()

    bad = tmp_path / "bad.yaml"
    bad.write_text("threshold:\n", encoding="utf-8")
    monkeypatch.setattr(config_module, "CONFIG_PATH", str(bad))

    with pytest.raises(config_module.ConfigError):
        holder.reload()
    assert holder.cfg["threshold"]["pass"] == 6
