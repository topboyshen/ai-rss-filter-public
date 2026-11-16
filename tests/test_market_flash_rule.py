"""A terse index/asset move is a low substantive score, not 'insufficient input'."""

import json

import pytest

from core.scorer import LLMBackend, Scorer, _is_market_flash_title
from db import database
from scoring_provenance import (
    MARKET_FLASH_VALUES,
    SEMANTIC_RESOLUTION_INSUFFICIENT_INPUT,
    SEMANTIC_RESOLUTION_MARKET_FLASH,
)

STRATEGY = "market-flash-test"
SENTINEL = (0.0, "信息不足", "缺少点名的公司、机构、国家或资产", "⚖️")


def _sentinel_json():
    return json.dumps(
        {"score": 0, "tag": "信息不足", "logic": SENTINEL[2], "sentiment": "中性"},
        ensure_ascii=False,
    )


def _cfg():
    return {
        "scoring_strategy_version": STRATEGY,
        "llm_score_cache": {"enabled": False},
        "llm_semantic_repair": {"enabled": False},
    }


@pytest.mark.parametrize("title,expected", [
    ("科创50指数跌超6%。", True),
    ("中国台湾加权指数下跌4.8%，或将创7月以来最大单日跌幅。", True),
    ("中国上证综指11月16日（周一）收盘下跌31.78点，跌幅：0.87%", True),
    ("英伟达(NVDA.O)在美股夜盘上涨1%，此前媒体报道称黄仁勋考虑重返英伟达担任CEO。", True),
    ("日经225指数跌幅扩大", True),
    ("大消费板块逆势上涨 后市能否反复活跃？|市场辩论会", False),
    ("南卫股份第五届董事会第九次会议决议公告", False),
    ("央行释放稳汇率信号", False),
    ("A股午评来了！", False),
])
def test_market_flash_title_guard(title, expected):
    assert _is_market_flash_title(title) is expected


def _score(title, monkeypatch):
    database.init_db()
    monkeypatch.setattr(Scorer, "_instance", None)
    backend = LLMBackend("fixture", "", "https://invalid.test", "fixture-model")
    monkeypatch.setattr(backend, "client", object())
    monkeypatch.setattr(Scorer, "backends_for", lambda self, *a, **kw: [backend])
    monkeypatch.setattr(Scorer, "_call_structured_llm", lambda *_a, **_kw: _sentinel_json())
    return Scorer().score_stage1_with_identity(title, "", "prompt", _cfg())


def test_index_move_overrides_sentinel(monkeypatch):
    result = _score("科创50指数跌超6%。", monkeypatch)
    assert result.values == MARKET_FLASH_VALUES
    assert result.semantic_resolution == SEMANTIC_RESOLUTION_MARKET_FLASH
    assert result.model_name == "local_market_flash_rule"


def test_plain_sentinel_is_unchanged(monkeypatch):
    result = _score("A股午评来了！", monkeypatch)
    assert result.values == SENTINEL
    assert result.semantic_resolution == SEMANTIC_RESOLUTION_INSUFFICIENT_INPUT


def test_market_flash_signal_persists_unselected():
    database.init_db()
    database.store_backtest_signal({
        "link": "https://example.test/market-flash",
        "score_path": "market_flash_rule",
        "score": MARKET_FLASH_VALUES[0],
        "aggregation_threshold": 6,
        "event_type": MARKET_FLASH_VALUES[1],
        "logic": MARKET_FLASH_VALUES[2],
        "direction": "中性",
        "selected": False,
        "strategy_version": STRATEGY,
    })
    row = database.get_latest_signal_score_path(
        "https://example.test/market-flash", STRATEGY
    )
    assert row == "market_flash_rule"
