"""Forward score snapshots may not mint title mappings from generated text."""
from copy import deepcopy
from types import SimpleNamespace

import pytest

from core.processor import _build_backtest_signal_snapshot


def _snapshot(title, *, tag="公司公告", logic="保持原来的评分理由", summary="", frozen=None):
    return _build_backtest_signal_snapshot(
        entry=SimpleNamespace(link="https://news.example/one"), raw_title=title,
        score=8.0, tag=tag, logic=logic, summary=summary, direction="看多",
        score_path="llm", score_confidence=0.8, strategy_version="test-contract",
        feed_url="https://news.example/rss", scored_at=100,
        cfg={"threshold": {"aggregation": 7}}, frozen_signal=frozen,
    )


@pytest.mark.parametrize("fields", [
    {"tag": "AI算力"},
    {"logic": "人工智能算力带来投资催化"},
    {"summary": "中际旭创获得AI算力订单"},
])
def test_generated_fields_cannot_create_forward_rule_candidates(fields):
    signal = _snapshot("浦发银行发布年度股东大会通知", **fields)
    assert signal["security_candidates"] == []
    assert signal["market_references"] == []
    assert signal["mapping_version"].startswith("asset-map:title-only-v2:")
    assert signal["score"] == 8.0 and signal["direction"] == "看多"
    if "logic" in fields:
        assert signal["logic"] == fields["logic"]
    if "summary" in fields:
        assert signal["summary"] == fields["summary"]


def test_source_title_still_creates_theme_candidates_with_original_direction():
    signal = _snapshot("人工智能算力需求增长", tag="其他", summary="生成摘要保持原值")
    assert signal["security_candidates"]
    assert all(row["direction"] == "看多" for row in signal["security_candidates"])
    assert signal["summary"] == "生成摘要保持原值"


def test_frozen_historical_mapping_is_not_recomputed_or_relabelled():
    frozen = {"mapping_version": "asset-map:old-frozen", "mapped_theme": "AI算力",
              "event_key": "old-event", "security_candidates": [{"ticker": "300308"}]}
    before = deepcopy(frozen)
    signal = _snapshot("浦发银行发布年度股东大会通知", frozen=frozen)
    assert signal["mapping_version"] == "asset-map:old-frozen"
    assert signal["security_candidates"] == before["security_candidates"]
    assert frozen == before
