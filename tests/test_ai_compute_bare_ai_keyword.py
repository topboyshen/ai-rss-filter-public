"""Bare token AI must not attach the AI算力 concept stock pool.

Regression for AI-pharma / AI-safety headlines that mention AI without any
compute-supply anchors (算力 / GPU / 大模型 / etc.).
"""

import pytest

from core.asset_mapping import map_article_to_assets, match_asset_rule

_COMPUTE_POOL = ("中际旭创", "新易盛", "浪潮信息", "寒武纪", "工业富联")


@pytest.mark.parametrize(
    "title",
    [
        "AI制药龙头金斯瑞生物科技盘中大涨，机构看好ADC管线出海",
        "字节系AI制药公司获新一轮融资，加速分子筛选平台落地",
        "国安提醒警惕境外AI智能体渗透，加强数据安全防护",
    ],
)
def test_ai_pharma_or_safety_titles_do_not_attach_ai_compute_pool(title):
    rule = match_asset_rule(title)
    assert rule is None or rule.theme != "AI算力"
    for trusted_only in (False, True):
        mapping = map_article_to_assets(
            {"title": title, "raw_title": title},
            trusted_only=trusted_only,
        )
        assert mapping.get("mapped_theme") != "AI算力"
        stocks = set(mapping.get("stocks") or [])
        candidates = {
            str(item.get("name") or "")
            for item in (mapping.get("security_candidates") or [])
        }
        leaked = stocks.intersection(_COMPUTE_POOL) | candidates.intersection(
            _COMPUTE_POOL
        )
        assert not leaked, f"unexpected compute-pool attach: {sorted(leaked)}"


@pytest.mark.parametrize(
    "title",
    [
        "智谱称大模型训练面临算力瓶颈，供需缺口仍在扩大",
        "Nebius GPU云提价，全球算力供给持续紧张",
    ],
)
def test_genuine_compute_anchors_still_map_to_ai_compute_pool(title):
    rule = match_asset_rule(title)
    assert rule is not None
    assert rule.theme == "AI算力"
    for trusted_only in (False, True):
        mapping = map_article_to_assets(
            {"title": title, "raw_title": title},
            trusted_only=trusted_only,
        )
        assert mapping["mapped_theme"] == "AI算力"
        stocks = set(mapping.get("stocks") or [])
        assert stocks.issuperset(_COMPUTE_POOL)
        candidates = {
            str(item.get("name") or "")
            for item in (mapping.get("security_candidates") or [])
        }
        assert candidates.issuperset(_COMPUTE_POOL)
