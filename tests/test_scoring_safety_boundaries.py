"""Synthetic safety regressions, not human Gold labels or accuracy evidence."""

import json

import pytest

from core.scorer import LLMBackend, Scorer, ScoringResult, _score_grounding_failure
from db.integration_outbox import is_exportable_score_path
from scoring_provenance import (
    NON_SUBSTANTIVE_SCORE_PATHS,
    SAFE_NEUTRAL_CONTRACT_VALUES,
    SAFE_NEUTRAL_SCORE_PATHS_BY_RESOLUTION,
)
from services.score_quality import score_quality_snapshot


@pytest.mark.parametrize("stage", ["stage1", "stage2"])
def test_initial_fixed_neutral_has_identity_and_survives_cache(monkeypatch, stage):
    scorer = Scorer()
    scorer.clear_runtime_cache()
    backend = LLMBackend("fixture", "", "https://invalid.test", "fixture")
    monkeypatch.setattr(backend, "client", object())
    monkeypatch.setattr(scorer, "backends_for", lambda *a, **kw: [backend])
    monkeypatch.setattr("core.scorer.record_score_validation", lambda **kw: None)
    calls = []

    def respond(*args, **kwargs):
        calls.append(kwargs)
        return json.dumps(
            dict(score=4, tag="其他", logic="来源未显示明确投资催化", sentiment="中性")
        )

    monkeypatch.setattr(scorer, "_call_structured_llm", respond)
    cfg = {"scoring_strategy_version": "fixture", "llm_score_cache": {"enabled": True}}
    result = scorer._score_with_identity("今年投资催化明显", "", "", cfg, stage=stage)
    cached = scorer._score_with_identity("今年投资催化明显", "", "", cfg, stage=stage)
    assert result == cached and len(calls) == 1
    assert result.values == SAFE_NEUTRAL_CONTRACT_VALUES
    assert result.semantic_resolution == "safe_neutral_initial"
    path = SAFE_NEUTRAL_SCORE_PATHS_BY_RESOLUTION[result.semantic_resolution][stage]
    assert path in NON_SUBSTANTIVE_SCORE_PATHS
    assert not is_exportable_score_path(path)


def test_near_neutral_not_reclassified_or_whitelisted(monkeypatch):
    with pytest.raises(ValueError, match="exact contract tuple"):
        ScoringResult(
            (3.5, "其他", "来源未显示明确投资催化", "⚖️"),
            "fixture",
            "safe_neutral_initial",
        )
    scorer = Scorer()
    backend = LLMBackend("fixture", "", "https://invalid.test", "fixture")
    result = scorer._finish_valid_score(
        (4, "其他", "常规消息", "⚖️"), backend, "initial", "", {}, "", "stage1"
    )
    assert result.semantic_resolution == ""
    assert _score_grounding_failure(
        (4, "其他", "甲公司投资6001亿元", "⚖️"), "甲公司投资6000亿元", ""
    )


def test_initial_neutral_counts_as_non_substantive(monkeypatch):
    monkeypatch.setattr(
        "services.score_quality.read_score_quality",
        lambda *a: {
            "available": True,
            "validation": [],
            "final_paths": {
                "llm": 1,
                "llm_safe_neutral_initial": 1,
                "stage2_safe_neutral_initial": 1,
            },
        },
    )
    data = score_quality_snapshot("fixture")
    assert data["final"]["usable"] == 1
    assert data["final"]["safe_neutral"] == 2


@pytest.mark.parametrize(
    "source,logic",
    [
        ("甲公司计划建设工厂", "甲公司已建设工厂"),
        ("甲公司预计投产", "甲公司已投产"),
        ("预计甲公司投产", "甲公司已投产"),
        ("甲公司未获批新药上市", "甲公司获批新药上市，预计利好业绩"),
        ("甲公司未获批新药上市", "甲公司获批新药上市预计利好业绩"),
        ("甲公司不会投产", "甲公司投产"),
        ("甲公司投产", "乙公司投产"),
        ("甲公司计划建设工厂，预计利好业绩", "甲公司建设工厂"),
    ],
)
def test_literal_modality_is_not_erased_by_inference(source, logic):
    assert (
        _score_grounding_failure((8, "公司公告", logic, "📈"), source, "")
        == "ungrounded_score_facts"
    )


@pytest.mark.parametrize(
    "source",
    [
        "甲公司已建设工厂",
        "甲公司计划建设工厂",
        "甲公司预计投产",
        "甲公司投产",
        "甲公司未获批新药上市",
        "甲公司获批新药上市，预计利好业绩",
    ],
)
def test_literal_state_preserved(source):
    assert not _score_grounding_failure((6, "公司公告", source, "⚖️"), source, "")


@pytest.mark.parametrize(
    "source,valid",
    [
        ("甲公司订单3亿元", True),
        ("甲公司订单2亿元", False),
        ("乙公司订单3亿元", False),
        ("甲公司预计订单3亿元", False),
        ("甲公司未有订单3亿元", False),
        ("甲公司订单超过3亿元", False),
    ],
)
def test_order_paraphrase_is_narrow_and_keeps_money_subject_state(source, valid):
    assert (
        not _score_grounding_failure(
            (6, "公司公告", "甲公司的订单为3亿元", "📈"), source, ""
        )
    ) is valid


@pytest.mark.parametrize(
    "title,tag,logic,valid",
    [
        ("甲公司发布服务器新产品", "服务器", "甲公司发布服务器新产品", True),
        ("甲公司发布服务器新产品", "AI算力", "算力需求大增", False),
        ("甲公司发布服务器新产品", "光模块", "光模块需求大增", False),
        ("公司不涉及服务器业务", "服务器", "服务器业务发展", False),
        ("证监会发布资本市场改革政策", "宏观政策", "证监会发布资本市场改革政策", True),
        ("证监会发布资本市场改革政策", "宏观放水", "降准释放流动性", False),
        ("证监会查处违法案件", "宏观政策", "流动性改善", False),
    ],
)
def test_literal_theme_is_separate_from_inferred_exposure(title, tag, logic, valid):
    assert (not _score_grounding_failure((6, tag, logic, "📈"), title, "")) is valid
