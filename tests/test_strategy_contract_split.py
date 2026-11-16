"""Contract buckets stay stable while the rule patch remains exact metadata."""

import importlib
import time

import pytest

from db import database
from scoring_provenance import scoring_contract_bucket, scoring_rule_patch

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


def _load(tmp_path, monkeypatch, text):
    path = tmp_path / "config.yaml"
    path.write_text(text, encoding="utf-8")
    monkeypatch.setattr(config_module, "CONFIG_PATH", str(path))
    for name in _ENV_VARS:
        monkeypatch.delenv(name, raising=False)
    return config_module.load_config()


def test_manual_version_is_the_contract_and_fingerprint_is_the_patch(
    tmp_path, monkeypatch
):
    cfg = _load(
        tmp_path,
        monkeypatch,
        "scoring_strategy_version: 2026-11-17-rrp-v9\nfeeds: []\n",
    )

    patch = cfg["scoring_strategy_fingerprint"]
    assert patch
    assert cfg["scoring_rule_patch"] == patch
    assert cfg["scoring_contract_version"] == "2026-11-17-rrp-v9"
    assert cfg["scoring_strategy_version"] == f"2026-11-17-rrp-v9:{patch}"
    assert scoring_contract_bucket(cfg["scoring_strategy_version"]) == "2026-11-17-rrp-v9"
    assert scoring_rule_patch(cfg["scoring_strategy_version"]) == patch


def test_without_a_manual_version_the_fingerprint_is_the_contract(
    tmp_path, monkeypatch
):
    cfg = _load(tmp_path, monkeypatch, "feeds: []\n")

    patch = cfg["scoring_strategy_fingerprint"]
    assert cfg["scoring_contract_version"] == patch
    assert cfg["scoring_rule_patch"] == patch
    assert cfg["scoring_strategy_version"] == patch
    assert scoring_contract_bucket(cfg["scoring_strategy_version"]) == patch
    assert scoring_rule_patch(cfg["scoring_strategy_version"]) == ""


@pytest.mark.parametrize(
    ("full", "bucket", "patch"),
    [
        ("", "", ""),
        (None, "", ""),
        ("manual", "manual", ""),
        ("manual:patch", "manual", "patch"),
        ("manual:patch:fb-abc", "manual", "patch"),
        ("  manual:patch  ", "manual", "patch"),
    ],
)
def test_bucket_and_patch_extraction(full, bucket, patch):
    assert scoring_contract_bucket(full) == bucket
    assert scoring_rule_patch(full) == patch


def _signal(link: str, version: str, now: int) -> dict:
    return {
        "event_key": link,
        "independence_key": link,
        "link": link,
        "feed_url": "feed-a",
        "available_at": now,
        "scored_at": now,
        "published_at": now - 60,
        "score": 9.0,
        "direction": "看多",
        "score_confidence": 0.7,
        "strategy_version": version,
        "mapping_version": "asset-map:test",
        "mapped_theme": "AI算力",
        "backtest_symbol": "sz399006",
        "score_path": "stage2",
        "revision_key": "base",
        "signal_stage": "base",
        "aggregation_threshold": 8.0,
        "selected": True,
        "is_similarity_copy": False,
    }


def test_backtest_contract_bucket_keeps_patch_history(isolate_ai_filter_db):
    database.init_db()
    now = int(time.time())
    assert database.store_backtest_signal(
        _signal("https://example.test/a", "2026-11-17-rrp-v9:patch-a", now)
    )
    assert database.store_backtest_signal(
        _signal("https://example.test/b", "2026-11-17-rrp-v9:patch-b", now)
    )
    assert database.store_backtest_signal(
        _signal("https://example.test/c", "2026-11-16-rrp-v8:patch-a", now)
    )

    contract_rows = database.get_backtest_signals(
        cutoff=0, strategy_contract="2026-11-17-rrp-v9", min_score=0
    )
    assert {row["link"] for row in contract_rows} == {
        "https://example.test/a",
        "https://example.test/b",
    }

    exact_rows = database.get_backtest_signals(
        cutoff=0, strategy_version="2026-11-17-rrp-v9:patch-b", min_score=0
    )
    assert [row["link"] for row in exact_rows] == ["https://example.test/b"]

    stats = database.get_backtest_signal_stats(strategy_contract="2026-11-17-rrp-v9")
    assert stats["count"] == 2
