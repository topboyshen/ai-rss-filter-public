"""P4: concept mappings disclose whether the source actually names the company."""

from pathlib import Path

from services.event_radar_query import _claim_mentions_security, _merge_stock_mapping


def _stock_row(**overrides):
    row = {
        "security_id": "SSE:600000",
        "exchange": "SSE",
        "ticker": "600000",
        "name": "浦发银行",
        "asset_type": "equity",
        "currency": "CNY",
        "aliases": '["浦发"]',
        "industry": "银行",
        "concepts": '["金融"]',
        "is_active": 1,
        "security_version": 2,
        "mapping_id": "mapping-1",
        "mapping_event_id": "event-1",
        "relation_type": "concept",
        "direction": "bullish",
        "expected_gap": 0.03,
        "confidence": 0.8,
        "relevance": 9.0,
        "rationale": "主题池",
        "mapping_version": "mapping-v1",
        "revision": 1,
        "created_at": 100,
        "candidate_count": 1,
        "candidate_event_count": 1,
        "direction_variant_count": 1,
    }
    row.update(overrides)
    return row


def _merge(evidence, row=None):
    stocks = {}
    _merge_stock_mapping(
        stocks,
        row or _stock_row(),
        {"SSE:600000": (9.0, 0.8, 0.03)},
        {"mapping-1": evidence},
    )
    return stocks["SSE:600000"]


def test_concept_without_a_named_source_is_marked_unbacked():
    stock = _merge([{"claim_text": "半导体板块集体异动，多只个股上涨。"}])

    relation = stock["relations"][0]
    assert relation["name_evidence"] is False
    assert relation["relation_label"] == "概念（来源未点名）"
    assert stock["concept_unbacked_all"] is True


def test_concept_with_a_named_source_stays_backed():
    stock = _merge([{"claim_text": "浦发银行发布新的金融产品。"}])

    relation = stock["relations"][0]
    assert relation["name_evidence"] is True
    assert relation["relation_label"] == "概念（原文点名）"
    assert stock["concept_unbacked_all"] is False


def test_alias_and_ticker_also_count_as_naming():
    assert _claim_mentions_security("浦发发布新品", "浦发银行", "600000", ["浦发"])
    assert _claim_mentions_security("600000公告", "浦发银行", "600000", ["浦发"])
    assert not _claim_mentions_security("银行板块上涨", "浦发银行", "600000", ["浦发"])


def test_direct_relation_is_never_marked_as_concept_without_text():
    stock = _merge([], row=_stock_row(relation_type="direct"))

    relation = stock["relations"][0]
    assert relation["name_evidence"] is True
    assert relation["relation_label"] == "直接影响"
    assert stock["concept_unbacked_all"] is False


def test_detail_template_discloses_unbacked_concept_mappings():
    template = (Path(__file__).resolve().parents[1] / "templates" / "event_radar.html").read_text(
        encoding="utf-8"
    )
    assert "来源未点名该公司" in template
    assert "source.stance_record_label" in template
