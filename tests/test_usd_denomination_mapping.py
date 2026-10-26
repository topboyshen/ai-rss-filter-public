"""Synthetic boundary cases; no private diagnostic or Gold records included."""

from copy import deepcopy
import hashlib

import pytest

from core.asset_mapping import map_article_to_assets, match_asset_rule


@pytest.mark.parametrize(
    "title",
    [
        "某企业目标估值约3万亿美元",
        "某公司完成50亿美元融资",
        "某公司营收达到2.5 万亿美元",
        "某产品售价３０美元",
        "某公司签订五十亿美元合同",
        "该公司披露以美元计价的营收",
        "某企业每股收益从1美元升至2美元",
        "某公司完成5亿美元兑付",
        "某企业30亿美元兑付款项已到账",
        "某产品从5美元下跌到3美元",
        "某企业每股收益从五美元上涨至六美元",
    ],
)
def test_dollar_denomination_alone_does_not_supply_a_macro_anchor(title):
    assert match_asset_rule(title) is None
    mapping = map_article_to_assets({"title": title}, trusted_only=True)
    assert mapping["mapping_confidence"] == 0
    assert mapping["asset_classes"] == []
    assert mapping["stocks"] == []


@pytest.mark.parametrize(
    ("title", "expected_theme"),
    [
        ("英伟达签订60亿美元服务器合同", "AI算力"),
        ("全球半导体季度营收达3000亿美元", "半导体国产化"),
        ("原油每桶价格降至70美元", "能源通胀"),
        ("黄金每盎司报价达2500美元", "贵金属"),
        ("创新药授权交易规模达十亿美元", "创新药"),
    ],
)
def test_dollar_unit_does_not_preempt_an_existing_industry_rule(title, expected_theme):
    assert match_asset_rule(title).theme == expected_theme
    for trusted_only in (False, True):
        mapping = map_article_to_assets({"title": title}, trusted_only=trusted_only)
        assert mapping["mapped_theme"] == expected_theme
        assert "美元指数" not in mapping["asset_classes"]


@pytest.mark.parametrize(
    "title",
    [
        "美元指数回落",
        "美元汇率波动加剧",
        "美元流动性趋紧",
        "美元走强",
        "美元走弱",
        "美元升值",
        "美元贬值",
        "美元兑日元上涨",
        "人民币兑美元中间价上调",
        "美联储隔夜逆回购余额降至500亿美元",
        "RRP余额下降",
        "SOFR利率上升",
        "TGA余额增至8000亿美元",
        "美债短端收益率上行",
        "隔夜逆回购协议规模回落",
        "美元上涨",
        "美元下跌至年内低点",
        "美元大幅走强",
        "美元短缺推动融资成本上升",
        "美元实际利率上升",
        "欧元兑美元走低",
    ],
)
def test_explicit_usd_macro_anchors_remain_supported_even_with_amounts(title):
    assert match_asset_rule(title).theme == "美元流动性"
    mapping = map_article_to_assets({"title": title}, trusted_only=True)
    assert mapping["mapped_theme"] == "美元流动性"
    assert mapping["backtest_symbol"] is None


def test_trusted_raw_title_cannot_inherit_a_generated_usd_mapping():
    article = {
        "raw_title": "某企业完成50亿美元融资",
        "title": "美元流动性走强",
        "tag": "美元流动性",
        "logic": "美元指数将影响全球资产",
        "summary": "美联储政策预期变化",
        "score": 8.5,
        "direction": "看空",
    }
    before = deepcopy(article)
    mapping = map_article_to_assets(article, trusted_only=True)
    assert mapping["mapping_confidence"] == 0
    assert mapping["stocks"] == []
    assert article == before


@pytest.mark.parametrize("field", ["raw_title", "logic", "summary"])
def test_non_title_amount_fields_do_not_create_usd_mapping(field):
    article = {field: "某公司营收达到百亿美元"}
    assert map_article_to_assets(article)["mapping_confidence"] == 0


def test_amount_and_independent_macro_anchor_can_coexist():
    article = {
        "title": "某公司营收达到百亿美元",
        "summary": "美联储宣布利率决议",
    }
    assert map_article_to_assets(article)["mapped_theme"] == "美元流动性"
    assert map_article_to_assets(article, trusted_only=True)["mapping_confidence"] == 0


def test_usd_rule_change_has_own_persisted_mapping_generation():
    from services.event_enrichment import (
        EVENT_MAPPING_CONTRACT_VERSION,
        EVENT_MAPPING_SEMANTIC_FINGERPRINT,
    )

    # Removing the false USD hit can expose real industry stock candidates.
    # Source hashing alone does not version this persisted event projection.
    assert EVENT_MAPPING_CONTRACT_VERSION == "event-mapping-store-v7"
    assert (
        EVENT_MAPPING_SEMANTIC_FINGERPRINT
        == hashlib.sha256(
            b"event-mapping-store-v7:usd-denomination-anchor-v1"
        ).hexdigest()
    )
