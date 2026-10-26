"""配置与环境变量中的密钥：覆盖逻辑可回归，无需启动完整 Flask。"""
import importlib
import re

import pytest
import yaml

from core.config import DEFAULTS, ConfigHolder, _apply_secrets_from_env


def test_apply_secrets_api_key_from_ai_filter_env(monkeypatch):
    cfg = {"security": {"api_key": "from_yaml"}}
    monkeypatch.setenv("AI_FILTER_API_KEY", "from_env")
    monkeypatch.delenv("API_KEY", raising=False)
    _apply_secrets_from_env(cfg)
    assert cfg["security"]["api_key"] == "from_env"


def test_apply_secrets_api_key_api_key_env_alias(monkeypatch):
    cfg = {"security": {"api_key": ""}}
    monkeypatch.delenv("AI_FILTER_API_KEY", raising=False)
    monkeypatch.setenv("API_KEY", "alias_key")
    _apply_secrets_from_env(cfg)
    assert cfg["security"]["api_key"] == "alias_key"


def test_apply_secrets_ai_filter_wins_over_api_key(monkeypatch):
    cfg = {"security": {"api_key": ""}}
    monkeypatch.setenv("AI_FILTER_API_KEY", "primary")
    monkeypatch.setenv("API_KEY", "secondary")
    _apply_secrets_from_env(cfg)
    assert cfg["security"]["api_key"] == "primary"


def test_apply_secrets_telegram_from_env(monkeypatch):
    cfg = {"telegram": {"bot_token": "", "chat_id": ""}}
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "t1")
    monkeypatch.setenv("TELEGRAM_CHAT_ID", "c1")
    _apply_secrets_from_env(cfg)
    assert cfg["telegram"]["bot_token"] == "t1"
    assert cfg["telegram"]["chat_id"] == "c1"


def test_apply_security_flags_from_env(monkeypatch):
    cfg = {
        "security": {
            "allow_query_api_key": True,
            "cors_allowed_origins": "*",
        }
    }
    monkeypatch.setenv("AI_FILTER_ALLOW_QUERY_API_KEY", "false")
    monkeypatch.setenv("AI_FILTER_CORS_ALLOWED_ORIGINS", "https://a.example, https://b.example")
    _apply_secrets_from_env(cfg)
    assert cfg["security"]["allow_query_api_key"] is False
    assert cfg["security"]["cors_allowed_origins"] == [
        "https://a.example",
        "https://b.example",
    ]


def test_load_config_env_overrides_file_key(tmp_path, monkeypatch):
    p = tmp_path / "test.yaml"
    p.write_text(
        "security:\n  api_key: in_file\n"
        "threshold:\n  pass: 5\n",
        encoding="utf-8",
    )
    monkeypatch.setenv("AI_FILTER_API_KEY", "from_env")
    # 显式获取配置模块，不依赖包级导出或其它测试的导入顺序。
    config_module = importlib.import_module("core.config")

    monkeypatch.setattr(config_module, "CONFIG_PATH", str(p))
    cfg = config_module.load_config()
    assert cfg["security"]["api_key"] == "from_env"
    assert cfg["threshold"]["pass"] == 5


def test_load_config_empty_env_keeps_file_key(tmp_path, monkeypatch):
    p = tmp_path / "test.yaml"
    p.write_text("security:\n  api_key: only_file\n", encoding="utf-8")
    monkeypatch.delenv("AI_FILTER_API_KEY", raising=False)
    monkeypatch.delenv("API_KEY", raising=False)
    config_module = importlib.import_module("core.config")

    monkeypatch.setattr(config_module, "CONFIG_PATH", str(p))
    cfg = config_module.load_config()
    assert cfg["security"]["api_key"] == "only_file"


def test_load_config_preserves_exact_runtime_feed_url_aliases(tmp_path, monkeypatch):
    config_module = importlib.import_module("core.config")
    p = tmp_path / "test.yaml"
    p.write_text(
        "feeds:\n"
        "  - url: http://127.0.0.1:1200/wallstreetcn/live?mode=fast\n"
        "  - url: http://127.0.0.1:12000/not-rsshub\n",
        encoding="utf-8",
    )
    monkeypatch.setattr(config_module, "CONFIG_PATH", str(p))
    monkeypatch.setenv("AI_FILTER_RSSHUB_BASE_URL", "http://rsshub:1200")

    cfg = config_module.load_config()

    rewritten = "http://rsshub:1200/wallstreetcn/live?mode=fast"
    original = "http://127.0.0.1:1200/wallstreetcn/live?mode=fast"
    assert cfg["feeds"][0]["url"] == rewritten
    assert cfg["_runtime_feed_url_aliases"] == {rewritten: [original]}
    assert cfg["feeds"][1]["url"] == "http://127.0.0.1:12000/not-rsshub"


def test_runtime_feed_rewrites_do_not_change_strategy_fingerprint(tmp_path, monkeypatch):
    config_module = importlib.import_module("core.config")
    p = tmp_path / "test.yaml"
    p.write_text(
        "scoring_strategy_version: manual-v1\n"
        "feeds:\n"
        "  - url: http://127.0.0.1:1200/cls/telegraph\n"
        "  - url: http://localhost:5000/jin10/rss\n",
        encoding="utf-8",
    )
    monkeypatch.setattr(config_module, "CONFIG_PATH", str(p))
    monkeypatch.delenv("AI_FILTER_RSSHUB_BASE_URL", raising=False)
    monkeypatch.delenv("AI_FILTER_WEB_BASE_URL", raising=False)
    canonical = config_module.load_config()

    monkeypatch.setenv("AI_FILTER_RSSHUB_BASE_URL", "http://rsshub:1200")
    monkeypatch.setenv("AI_FILTER_WEB_BASE_URL", "http://ai-filter-web:5000")
    rewritten = config_module.load_config()

    assert rewritten["scoring_strategy_fingerprint"] == canonical["scoring_strategy_fingerprint"]
    assert rewritten["scoring_strategy_version"] == canonical["scoring_strategy_version"]
    assert rewritten["scoring_strategy_version"].startswith("manual-v1:")
    assert rewritten["feeds"][0]["url"] == "http://rsshub:1200/cls/telegraph"
    assert rewritten["feeds"][1]["url"] == "http://ai-filter-web:5000/jin10/rss"
    assert rewritten["_runtime_feed_url_aliases"] == {
        "http://rsshub:1200/cls/telegraph": [
            "http://127.0.0.1:1200/cls/telegraph"
        ],
        "http://ai-filter-web:5000/jin10/rss": [
            "http://localhost:5000/jin10/rss"
        ],
    }


def test_canonical_feed_change_still_changes_strategy_fingerprint(tmp_path, monkeypatch):
    config_module = importlib.import_module("core.config")
    p = tmp_path / "test.yaml"
    monkeypatch.setattr(config_module, "CONFIG_PATH", str(p))
    monkeypatch.delenv("AI_FILTER_RSSHUB_BASE_URL", raising=False)

    p.write_text(
        "feeds:\n  - url: http://127.0.0.1:1200/cls/telegraph\n",
        encoding="utf-8",
    )
    first = config_module.load_config()["scoring_strategy_fingerprint"]
    p.write_text(
        "feeds:\n  - url: http://127.0.0.1:1200/cls/depth\n",
        encoding="utf-8",
    )
    second = config_module.load_config()["scoring_strategy_fingerprint"]

    assert first != second


def test_query_api_key_is_disabled_by_default():
    assert DEFAULTS["security"]["allow_query_api_key"] is False


def test_load_config_uses_fresh_nested_defaults_and_removes_deleted_keys(tmp_path, monkeypatch):
    config_module = importlib.import_module("core.config")
    p = tmp_path / "test.yaml"
    monkeypatch.setattr(config_module, "CONFIG_PATH", str(p))
    monkeypatch.delenv("AI_FILTER_ALLOW_QUERY_API_KEY", raising=False)

    p.write_text(
        "security:\n"
        "  allow_query_api_key: true\n"
        "  retired_key: old\n"
        "retired_top_level: old\n",
        encoding="utf-8",
    )
    holder = ConfigHolder()
    first = holder.cfg
    assert first["security"]["allow_query_api_key"] is True

    p.write_text("{}\n", encoding="utf-8")
    second = holder.reload()

    assert second["security"]["allow_query_api_key"] is False
    assert "retired_key" not in second["security"]
    assert "retired_top_level" not in second
    assert DEFAULTS["security"]["allow_query_api_key"] is False


def test_load_config_propagates_read_parse_and_top_level_errors(tmp_path, monkeypatch):
    config_module = importlib.import_module("core.config")
    p = tmp_path / "test.yaml"
    p.write_text("security: [\n", encoding="utf-8")
    monkeypatch.setattr(config_module, "CONFIG_PATH", str(p))

    with pytest.raises(yaml.YAMLError):
        config_module.load_config()

    p.write_text("- not\n- a mapping\n", encoding="utf-8")
    with pytest.raises(TypeError, match="YAML mapping"):
        config_module.load_config()

    def fail_open(*_args, **_kwargs):
        raise OSError("read failed")

    monkeypatch.setattr(config_module, "open", fail_open, raising=False)
    with pytest.raises(OSError, match="read failed"):
        config_module.load_config()


def test_config_reload_invalid_yaml_keeps_previous_runtime_state(tmp_path, monkeypatch):
    config_module = importlib.import_module("core.config")
    p = tmp_path / "test.yaml"
    p.write_text("blacklist:\n  - safe\nmarker: old\n", encoding="utf-8")
    monkeypatch.setattr(config_module, "CONFIG_PATH", str(p))
    holder = ConfigHolder()
    old_cfg = holder.cfg
    old_regex = holder.blacklist_re

    p.write_text("security: [\n", encoding="utf-8")
    with pytest.raises(yaml.YAMLError):
        holder.reload()

    assert holder.cfg is old_cfg
    assert holder.blacklist_re is old_regex


def test_config_reload_keeps_previous_pair_when_regex_compile_fails(monkeypatch):
    config_module = importlib.import_module("core.config")
    holder = ConfigHolder.__new__(ConfigHolder)
    old_cfg = {"marker": "old", "blacklist": ["safe"]}
    old_regex = re.compile("safe")
    holder._cfg = old_cfg
    holder._blacklist_re = old_regex

    monkeypatch.setattr(
        config_module,
        "load_config",
        lambda: {"marker": "new", "blacklist": ["("]},
    )

    with pytest.raises(re.error):
        holder.reload()
    assert holder.cfg is old_cfg
    assert holder.blacklist_re is old_regex
