"""Exercise punctuation normalization through adopted ScoringResult paths.

These are constructed responses, not archived model output. Model calls and
the two persistence hooks are replaced; parsing, validation and adoption run.
"""

import json
from collections import deque

import pytest

import core.scorer as scorer_module
from core.scorer import LLMBackend, Scorer
from scoring_provenance import SAFE_NEUTRAL_CONTRACT_VALUES

TITLE = "甲公司订单3亿元"
NEUTRAL = {
    "score": 4,
    "tag": "其他",
    "logic": "来源未显示明确投资催化。",
    "sentiment": "中性",
}
FORMAL = {
    "score": 8,
    "tag": "公司订单",
    "logic": "甲公司订单3亿元。",
    "sentiment": "看多",
}
BAD_FACT = {**FORMAL, "logic": "甲公司订单4亿元"}
BAD_THEME = {
    **FORMAL,
    "tag": "算力",
    "logic": "英伟达算力需求增长",
}


def _raw(fields):
    return json.dumps(fields, ensure_ascii=False)


@pytest.fixture
def scoring_session(monkeypatch):
    """No configured clients, network, cache or evidence/metrics persistence."""
    monkeypatch.setattr(Scorer, "_instance", None)
    monkeypatch.setattr(Scorer, "_init_backends", lambda self: None)
    scorer = Scorer()
    scorer.backends = [
        LLMBackend(
            name=name,
            model=f"{name}-model",
            api_key="",
            base_url="https://invalid.test",
            client=object(),
        )
        for name in ("first", "second")
    ]
    responses = deque()
    calls = []
    records = []

    def respond(backend, *args, **kwargs):
        if not responses:
            pytest.fail("unexpected additional model call")
        expected_backend, raw = responses.popleft()
        assert backend.name == expected_backend
        calls.append((backend.name, kwargs["purpose"]))
        return raw

    def unexpected_fallback(*args, **kwargs):
        pytest.fail("semantic punctuation case must not use local fallback")

    monkeypatch.setattr(scorer, "_call_structured_llm", respond)
    monkeypatch.setattr(scorer, "_fallback_local_score", unexpected_fallback)
    monkeypatch.setattr(scorer_module, "capture_validation_attempt", lambda **kw: None)
    monkeypatch.setattr(
        scorer_module, "record_score_validation", lambda **kw: records.append(kw)
    )
    cfg = {
        "scoring_strategy_version": "neutral-punctuation-path-test",
        "llm_semantic_repair": {"enabled": True},
        "llm_score_cache": {"enabled": False},
        "llm_routing": {
            "stage1": ["first", "second"],
            "stage2": ["first", "second"],
        },
    }

    def score(plan, stage="stage1"):
        assert not responses
        responses.extend(plan)
        method = getattr(scorer, f"score_{stage}_with_identity")
        result = method(TITLE, "", "fixture prompt", cfg)
        assert not responses
        assert scorer.last_backend_name == result.model_name
        return result

    return score, calls, records


@pytest.mark.parametrize("stage", ["stage1", "stage2"])
def test_initial_sentence_stop_is_recorded_as_initial_neutral(scoring_session, stage):
    score, calls, records = scoring_session
    result = score([("first", _raw(NEUTRAL))], stage)

    assert result.values == SAFE_NEUTRAL_CONTRACT_VALUES
    assert result.model_name == "first-model"
    assert (result.semantic_resolution, result.neutral_reason) == (
        "safe_neutral_initial", "initial"
    )
    assert calls == [("first", stage)]
    assert [(r["attempt"], r["accepted"], r["reason"]) for r in records] == [
        ("initial", True, "")
    ]


@pytest.mark.parametrize("stage", ["stage1", "stage2"])
@pytest.mark.parametrize(
    "rejected,trigger",
    [
        ("{", "invalid_score_contract"),
        (_raw(BAD_FACT), "ungrounded_score_facts"),
        (_raw(BAD_THEME), "ungrounded_score_theme"),
    ],
)
def test_backup_sentence_stop_has_initial_reason_and_its_own_identity(
    scoring_session, stage, rejected, trigger
):
    score, calls, records = scoring_session
    result = score([("first", rejected), ("second", _raw(NEUTRAL))], stage)

    assert result.values == SAFE_NEUTRAL_CONTRACT_VALUES
    assert result.model_name == "second-model"
    assert (result.semantic_resolution, result.neutral_reason) == (
        "safe_neutral_initial", "initial"
    )
    assert calls == [("first", stage), ("second", stage)]
    assert records[0]["reason"].split(":", 1)[0] == trigger
    assert records[0]["accepted"] is False
    assert records[1]["attempt"] == "initial"
    assert records[1]["accepted"] is True
    assert records[1]["reason"] == ""
    assert [r["model_name"] for r in records] == ["first-model", "second-model"]


@pytest.mark.parametrize("adopted", [NEUTRAL, FORMAL], ids=["neutral", "formal"])
def test_next_backend_does_not_inherit_semantic_rejection_reason(scoring_session, adopted):
    score, calls, records = scoring_session
    result = score([
        ("first", _raw(BAD_FACT)),
        ("second", _raw(adopted)),
    ])

    assert calls == [("first", "stage1"), ("second", "stage1")]
    assert result.model_name == "second-model"
    assert records[0]["reason"] == "ungrounded_score_facts"
    assert records[0]["accepted"] is False
    assert records[1]["attempt"] == "initial"
    assert records[1]["model_name"] == "second-model"
    assert records[1]["accepted"] is True
    if adopted is NEUTRAL:
        assert result.values == SAFE_NEUTRAL_CONTRACT_VALUES
        assert (result.semantic_resolution, result.neutral_reason) == (
            "safe_neutral_initial", "initial"
        )
    else:
        assert result.values == (8.0, "公司订单", "甲公司订单3亿元。", "📈")
        assert (result.semantic_resolution, result.neutral_reason) == ("", "")


def test_later_formal_result_does_not_reuse_prior_neutral_reason(scoring_session):
    score, calls, _records = scoring_session
    neutral = score([("first", _raw(BAD_FACT)), ("second", _raw(BAD_FACT))])
    formal = score([("first", _raw(FORMAL))])

    assert (neutral.semantic_resolution, neutral.neutral_reason) == (
        "safe_neutral_grounding", "facts"
    )
    assert (formal.semantic_resolution, formal.neutral_reason) == ("", "")
    assert formal.values == (8.0, "公司订单", "甲公司订单3亿元。", "📈")
    assert neutral.model_name == "local_safe_neutral"
    assert formal.model_name == "first-model"
    assert calls == [("first", "stage1"), ("second", "stage1"), ("first", "stage1")]
