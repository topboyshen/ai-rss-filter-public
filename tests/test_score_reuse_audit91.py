"""Keep issuer, metric and counterparty evidence through similarity reuse."""

import json
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from core.processor import _process_entry_similarity
from core.score_evidence import event_reuse_conflicts
from core.scorer import LLMBackend, Scorer, _score_grounding_verdict
from scoring_provenance import SAFE_NEUTRAL_CONTRACT_VALUES
from utils.helpers import title_similarity


@pytest.mark.parametrize("old,new", [
    ("贵州茅台前三季度净利润同比增长20%", "五粮液前三季度净利润同比增长20%"),
    ("甲公司前三季度净利润同比增长20%", "乙公司前三季度净利润同比增长20%"),
    ("甲公司发布年度报告", "乙公司发布年度报告"),
    ("甲公司营收增长20%", "甲集团营收增长20%"),
    ("甲公司净利润增长10%，营收下降20%", "甲公司净利润下降10%，营收增长20%"),
    ("甲公司净利润增长10%，营收增长20%", "甲公司净利润增长20%，营收增长10%"),
    ("甲公司净利润增长10%", "甲公司净利润增长20%"),
    ("甲公司净利润增长10%", "甲公司毛利润增长10%"),
    ("甲公司净利润同比增长10%", "甲公司净利润环比增长10%"),
    ("甲公司获得乙公司订单", "甲公司获得丙公司订单"),
    ("甲公司获得乙公司订单", "甲公司获得订单"),
    ("甲公司已签订采购合同", "甲公司拟签署采购合同"),
    ("贵州茅台净利润增长20%", "茅台净利润增长20%"),
    ("甲公司10月1日获得订单", "甲公司10月2日获得订单"),
    ("甲公司净利润增长10美元，营收增长10欧元", "甲公司净利润增长10欧元，营收增长10美元"),
    ("甲公司前三季度净利润增长20%", "甲公司三季度净利润增长20%"),
    ("以岭药业净利润增长20%", "岭药业净利润增长20%"),
    ("甲公司净利润预计增长10%，营收预计下降20%", "甲公司净利润预计下降10%，营收预计增长20%"),
    ("甲公司净利润：增长10%，营收：下降20%", "甲公司净利润：下降10%，营收：增长20%"),
    ("甲公司净利润预计同比增长10%，营收预计同比下降20%", "甲公司净利润预计同比下降10%，营收预计同比增长20%"),
    ("甲公司净利润预计增长10%", "甲公司净利润增长10%"),
    ("甲公司净利润被预测将增长10%，营收被预测将下降20%", "甲公司净利润被预测将下降10%，营收被预测将增长20%"),
    ("甲公司净利润走势为增长，营收走势为下降", "甲公司净利润走势为下降，营收走势为增长"),
])
def test_changed_or_uncertain_facts_require_rescoring(old, new):
    assert event_reuse_conflicts(new, old)
    assert event_reuse_conflicts(old, new)


@pytest.mark.parametrize("old,new", [
    ("贵州茅台前三季度净利润同比增长20%", "贵州茅台前三季度净利润同比增长20%"),
    ("甲公司签署乙公司采购合同", "甲公司签订乙公司采购合同"),
    ("甲公司取得乙公司订单", "甲公司获得乙公司订单"),
    ("甲公司拟并购乙公司", "甲公司拟收购乙公司"),
    ("甲公司营收同比增长20%", "甲公司营业收入同比增加20%"),
    ("甲公司净利润增长10%，营收下降20%", "甲公司净利润增加10%；营业收入下滑20%"),
    ("甲公司获得3亿元订单", "甲公司取得30000万元订单"),
    ("财联社：甲公司取得乙公司订单", "华尔街见闻：甲公司获得乙公司订单"),
    ("【格隆汇】甲公司：签署采购合同", "证券时报：甲公司签订采购合同。"),
    ("甲公司：营收同比增长20%", "甲公司营业收入同比增加20%"),
    ("甲公司发布年度报告", "金十数据：甲公司发布年度报告。"),
    ("甲公司并购乙公司取得订单", "甲公司收购乙公司获得订单"),
    ("贵州茅台前三季度净利润同比增长20%", "贵州茅台：前三季度净利润同比增20%"),
    ("贵州茅台前三季度净利润同比增长20%", "前三季度贵州茅台净利润同比增加20%"),
    ("甲公司净利润增长10%，营收下降20%", "甲公司营收下滑20%；净利润增加10%"),
    ("央行流动性安排", "央行流动性安排进展"),
    ("甲公司建设新厂并获得项目订单", "甲公司获得项目订单"),
    ("甲公司净利润预计增长10%，营收预计下降20%", "甲公司净利润：预计增加10%；营业收入：预计下滑20%"),
    ("甲公司净利润预计同比增长20%", "甲公司净利润同比预计增20%"),
    ("甲公司预计净利润同比增长20%", "甲公司净利润同比预计增20%"),
    ("甲公司获得乙公司订单", "甲公司获得乙公司的订单"),
    ("甲公司签署乙公司采购合同", "甲公司签订与乙公司的采购合同"),
    ("甲公司净利润被预测将增长10%，营收被预测将下降20%", "甲公司净利润被预测将增长10%，营收被预测将下降20%"),
])
def test_same_subject_and_facts_allow_bounded_paraphrase_reuse(old, new):
    assert not event_reuse_conflicts(new, old)
    assert not event_reuse_conflicts(old, new)


@pytest.mark.parametrize("source,logic,valid", [
    ("甲公司获得乙公司订单", "甲公司获得丙公司订单", False),
    ("甲公司获得乙公司订单", "甲公司获得订单", True),
    ("甲公司获得项目订单", "甲公司获得订单", True),
    ("甲公司获得订单", "甲公司获得乙公司订单", False),
    ("甲公司获得乙公司订单", "甲公司获得乙公司独家订单", False),
    ("甲公司获得乙公司订单", "甲公司获得乙公司长期订单", False),
    ("甲公司获得乙公司订单", "甲公司获得乙公司3亿元订单", False),
    ("甲公司获得乙公司非独家订单", "甲公司获得乙公司独家订单", False),
    ("甲公司收购乙公司控股子公司", "甲公司收购乙公司", False),
    ("甲公司签署乙公司采购合同", "甲公司签订合同", True),
    ("甲公司获得大额订单", "甲公司取得重大订单", True),
    ("甲公司获得乙公司订单", "甲公司取得乙公司订单", True),
    ("甲公司获得项目订单", "甲公司获得订单利好业绩", True),
    ("甲公司获得乙公司订单", "甲公司获得乙公司的订单", True),
    ("甲公司签署乙公司采购合同", "甲公司签订与乙公司的采购合同", True),
    ("甲公司获得项目订单", "甲公司获得该项目订单", True),
    ("甲公司获得乙公司订单", "甲公司获得丙公司的订单", False),
    ("甲公司签署乙公司采购合同", "甲公司签订与丙公司的采购合同", False),
    ("甲公司签署乙公司采购合同", "甲公司签订与乙公司的独家采购合同", False),
    ("甲公司签署乙公司采购合同", "甲公司签订与乙公司的长期采购合同", False),
    ("甲公司获得乙公司订单", "甲公司获得乙公司的3亿元订单", False),
])
def test_grounding_allows_only_supported_object_generalization(source, logic, valid):
    reason, _ = _score_grounding_verdict((9, "公司公告", logic, "看多"), source, "")
    assert (not reason) is valid


@pytest.mark.parametrize("old,new,logic,can_reuse", [
    ("贵州茅台前三季度净利润同比增长20%", "五粮液前三季度净利润同比增长20%", "净利润增长改善盈利预期", False),
    ("甲公司净利润增长10%，营收下降20%", "甲公司净利润下降10%，营收增长20%", "净利润增长改善盈利预期", False),
    ("甲公司取得乙公司订单", "甲公司获得乙公司订单", "甲公司获得订单", True),
    ("甲公司营收同比增长20%", "甲公司营业收入同比增加20%", "盈利预期改善", True),
    ("财联社：甲公司取得乙公司订单", "华尔街见闻：甲公司获得乙公司订单", "甲公司获得订单", True),
    ("【格隆汇】甲公司：签署乙公司年度设备采购合同", "证券时报：甲公司签订乙公司年度设备采购合同。", "甲公司签订合同", True),
    ("甲公司净利润预计增长10%，营收预计下降20%", "甲公司净利润预计下降10%，营收预计增长20%", "净利润增长改善盈利预期", False),
    ("甲公司净利润：增长10%，营收：下降20%", "甲公司净利润：下降10%，营收：增长20%", "净利润增长改善盈利预期", False),
    ("甲公司净利润被预测将增长10%，营收被预测将下降20%", "甲公司净利润被预测将下降10%，营收被预测将增长20%", "净利润增长改善盈利预期", False),
    ("甲公司获得乙公司订单", "甲公司获得乙公司的订单", "甲公司获得乙公司订单", True),
    ("甲公司签署乙公司采购合同", "甲公司签订与乙公司的采购合同", "甲公司签订乙公司采购合同", True),
])
def test_similarity_retains_rejected_candidate_for_stage2(monkeypatch, old, new, logic, can_reuse):
    # Reuse now also needs a readable master; retain every existing outcome.
    monkeypatch.setattr("core.processor.DB.get_similarity_identity_rows", lambda: [
        ("SSE:600001", "甲公司", "600001", "SSE", "[]"),
        ("SSE:600002", "乙公司", "600002", "SSE", "[]"),
    ])
    row = ("old-link", old, old, 9.0, "公司公告", logic, "看多")
    assert title_similarity(old, new) >= 0.6
    ctx = SimpleNamespace(
        raw_title=new, source_summary="", feed_url="",
        cfg={"threshold": {"similarity": 0.6}}, entry=SimpleNamespace(link="new-link"),
    )
    persist = Mock(return_value=(None, "similarity"))
    monkeypatch.setattr("core.processor._persist_similar_entry", persist)
    info, terminal = _process_entry_similarity(ctx, [row], 99)
    assert info is not None and info["score"] == 9.0
    if can_reuse:
        assert terminal == (None, "similarity")
        persist.assert_called_once_with(ctx, info, 99)
    else:
        assert terminal is None
        persist.assert_not_called()


def test_counterparty_hallucination_uses_local_neutral_without_paid_repair(monkeypatch):
    backend = LLMBackend(name="test", model="stub", api_key="", base_url="", rate_limit=0)
    backend.client = object()
    scorer = object.__new__(Scorer)
    scorer.backends = [backend]
    response = json.dumps({
        "score": 9, "tag": "公司订单", "logic": "甲公司获得丙公司订单", "sentiment": "看多",
    }, ensure_ascii=False)
    call = Mock(return_value=response)
    monkeypatch.setattr(scorer, "_call_structured_llm", call)
    monkeypatch.setattr("core.scorer.record_score_validation", Mock())
    result = scorer._score_with_identity(
        "甲公司获得乙公司订单", "", "规则",
        {"llm_semantic_repair": {"enabled": True}, "llm_score_cache": {"enabled": False}},
    )
    assert result.values == SAFE_NEUTRAL_CONTRACT_VALUES
    assert result.model_name == "local_safe_neutral"
    assert result.semantic_resolution == "safe_neutral_grounding"
    assert call.call_count == 1
