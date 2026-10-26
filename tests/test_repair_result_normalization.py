"""Scorer v2 regression: bounded JSON normalization + repair/initial paths.

Scope: after strict JSON validation, only the complete fixed neutral tuple may
omit one final Chinese/ASCII sentence stop. Ordinary results remain unchanged.
Fixtures reproduce the parser boundary; they are not archived model responses.

Groups:
  A. parse-level: punctuation-only variants accepted, substantive variants rejected,
     contract violations unchanged.
  B. real validation paths (`_validate_scoring_attempt`) with side effects stubbed:
     initial scoring, grounding repair, contract repair.
  C. re-encoding idempotence (parse -> re-encode -> parse).
"""

import json

import pytest

import core.scorer as scorer
from core.scorer import Scorer, _grounding_repair_result_is_safe
from scoring_provenance import SAFE_NEUTRAL_CONTRACT_VALUES as FIXED

PUNCT_ACCEPT = [
    '{"score": 4, "tag": "其他", "logic": "来源未显示明确投资催化", "sentiment": "中性"}',
    '{"score": 4.0, "tag": "其他", "logic": "来源未显示明确投资催化。", "sentiment": "中性"}',
    '{"score": 4, "tag": "其他", "logic": "来源未显示明确投资催化.", "sentiment": "中性"}',
]

UNSAFE_REPAIR_RESULTS = [
    '{"score": 4, "tag": "其他", "logic": "来源未显示明确投资催化（但订单增长）", "sentiment": "中性"}',
    '{"score": 4, "tag": "其他", "logic": "来源未显示明确投资催化2亿元", "sentiment": "中性"}',
    '{"score": 4, "tag": "其他", "logic": "来源未显示明确投资催化。订单增长显著", "sentiment": "中性"}',
    '{"score": 4, "tag": "其他", "logic": "来源未显示明确投资催化，但并非没有价值", "sentiment": "中性"}',
    '{"score": 4, "tag": "其他", "logic": "来源未显示明确投资催化：不涉及AI", "sentiment": "中性"}',
    '{"score": 7, "tag": "宏观政策", "logic": "降准释放流动性利好银行", "sentiment": "看多"}',
    '{"score": 4, "tag": "其他", "logic": "来源未显示明确投资催化。", "sentiment": "看多"}',
]

CONTRACT_VIOLATIONS = [
    '{"score": 4, "tag": "其他", "logic": "%s", "sentiment": "中性"}'
    % ("来源未显示明确投资催化" + "x" * 90),
    '{"score": 11, "tag": "其他", "logic": "来源未显示明确投资催化。", "sentiment": "中性"}',
    '{"score": 4, "tag": "其他", "sentiment": "中性"}',
    '{"score": "4", "tag": "其他", "logic": "来源未显示明确投资催化。", "sentiment": "中性"}',
    '{"score": 4, "tag": "其他", "logic": "来源未显示明确投资催化 ", "sentiment": "中性"}',
    '{"score": 4, "tag": "其他。", "logic": "来源未显示明确投资催化", "sentiment": "中性"}',
    '{"score": 4, "tag": "其他", "logic": "来源未显示明确投资催化。", "sentiment": "震荡"}',
]

CROSS_GRAMMAR = [
    '{"score": 4, "tag": "其他", "logic": "来源未显示明确投资催化。", "sentiment": "中性"}',
    "分数：4\n标签：其他\n逻辑：来源未显示明确投资催化。\n情绪：中性",
    "[4.0]\n[其他]\n[来源未显示明确投资催化。]\n[中性]",
]

# ---- validation-path harness -------------------------------------------------
TITLE = "央行宣布降准0.5个百分点"
SUMMARY = "中国人民银行决定下调金融机构存款准备金率0.5个百分点，释放长期资金约1万亿元。"

FIXED_OK = '{"score": 4, "tag": "其他", "logic": "来源未显示明确投资催化", "sentiment": "中性"}'
FIXED_PERIOD = '{"score": 4, "tag": "其他", "logic": "来源未显示明确投资催化。", "sentiment": "中性"}'
PAREN_FACT = '{"score": 4, "tag": "其他", "logic": "来源未显示明确投资催化（但订单增长）", "sentiment": "中性"}'
POSITIVE = '{"score": 8, "tag": "宏观政策", "logic": "央行降准释放流动性利好银行股", "sentiment": "看多"}'
UNGROUNDED = '{"score": 8, "tag": "算力", "logic": "英伟达订单大增带动算力需求爆发", "sentiment": "看多"}'
BROKEN = (
    '{"score": 4, "tag": "其他", "logic": "来源未显示明确投资催化", "sentiment": "中性"'
)


class _Backend:
    name = "probe"
    model = "probe-model"


class _Stub:
    _parse_score_result = staticmethod(Scorer._parse_score_result)
    _parse_score_result_with_detail = staticmethod(
        Scorer._parse_score_result_with_detail
    )


@pytest.fixture
def side_effects(monkeypatch):
    """Stub the two persistence hooks; the validation logic itself stays real."""
    calls = {"capture": 0, "record": 0}

    def capture(**kwargs):
        calls["capture"] += 1

    def record(**kwargs):
        calls["record"] += 1

    monkeypatch.setattr(scorer, "capture_validation_attempt", capture)
    monkeypatch.setattr(scorer, "record_score_validation", record)
    return calls


def _validate(raw, attempt="initial", trigger=""):
    return Scorer._validate_scoring_attempt(
        _Stub(),
        raw,
        TITLE,
        SUMMARY,
        attempt,
        trigger,
        "stage1",
        _Backend(),
        {"scoring_strategy_version": "test-contract"},
        None,
    )


# ---- A. parse level ---------------------------------------------------------
@pytest.mark.parametrize("raw", PUNCT_ACCEPT)
def test_trailing_punctuation_is_normalized(raw):
    parsed = Scorer._parse_score_result(raw)
    assert parsed == FIXED, f"expected fixed tuple, got {parsed!r}"
    assert _grounding_repair_result_is_safe(parsed) is True


@pytest.mark.parametrize("raw", UNSAFE_REPAIR_RESULTS)
def test_nonfixed_results_are_not_accepted_as_grounding_repairs(raw):
    assert _grounding_repair_result_is_safe(Scorer._parse_score_result(raw)) is False


@pytest.mark.parametrize("raw", CONTRACT_VIOLATIONS)
def test_contract_rules_unchanged(raw):
    assert Scorer._parse_score_result(raw)[0] < 0


@pytest.mark.parametrize("raw", CROSS_GRAMMAR)
def test_cross_grammar_consistency_for_punctuation_only(raw):
    assert Scorer._parse_score_result(raw) == FIXED


# ---- B. real validation paths ----------------------------------------------
def test_initial_scoring_regression(side_effects):
    """The JSON parser is not repair-only: initial scoring must be untouched."""
    parsed, reason, detail = _validate(FIXED_OK, "initial", "")
    assert parsed == FIXED and reason == "" and detail == ""
    parsed, reason, _ = _validate(FIXED_PERIOD, "initial", "")
    assert parsed == FIXED and reason == ""
    parsed, reason, _ = _validate(POSITIVE, "initial", "")
    assert (
        parsed == (8.0, "宏观政策", "央行降准释放流动性利好银行股", "📈")
        and reason == ""
    )
    _, reason, _ = _validate(UNGROUNDED, "initial", "")
    assert reason.startswith("ungrounded_score_")
    _, reason, detail = _validate(BROKEN, "initial", "")
    assert reason == "invalid_score_contract" and detail
    assert side_effects["capture"] == 5 and side_effects["record"] == 5


def test_grounding_repair_path(side_effects):
    parsed, reason, _ = _validate(FIXED_PERIOD, "repair", "ungrounded_score_facts")
    assert parsed == FIXED and reason == "", (
        "period-only variant must be accepted after repair"
    )
    _, reason, _ = _validate(PAREN_FACT, "repair", "ungrounded_score_facts")
    assert reason == "unsafe_grounding_repair_result"
    _, reason, _ = _validate(POSITIVE, "repair", "ungrounded_score_facts")
    assert reason == "unsafe_grounding_repair_result"
    _, reason, _ = _validate(UNGROUNDED, "repair", "ungrounded_score_theme")
    assert reason.startswith("ungrounded_score_")
    assert side_effects["record"] == 4


def test_contract_repair_path(side_effects):
    parsed, reason, _ = _validate(FIXED_PERIOD, "repair", "invalid_score_contract")
    assert parsed == FIXED and reason == "", (
        "period-only variant must be accepted after contract repair"
    )
    _, reason, _ = _validate(PAREN_FACT, "repair", "invalid_score_contract")
    assert reason == "unsafe_contract_repair_result"
    _, reason, detail = _validate(BROKEN, "repair", "invalid_score_contract")
    assert reason == "invalid_score_contract" and detail
    assert side_effects["record"] == 3


# ---- C. re-encoding idempotence --------------------------------------------
_SENTIMENT_TEXT = {"📈": "看多", "📉": "看空", "⚖️": "中性"}


def _reencode(parsed):
    score, tag, logic, direction = parsed
    return json.dumps(
        {
            "score": score,
            "tag": tag,
            "logic": logic,
            "sentiment": _SENTIMENT_TEXT.get(direction, "中性"),
        },
        ensure_ascii=False,
    )


@pytest.mark.parametrize("raw", PUNCT_ACCEPT + CROSS_GRAMMAR)
def test_parse_reencode_parse_is_stable(raw):
    first = Scorer._parse_score_result(raw)
    assert first == FIXED
    second = Scorer._parse_score_result(_reencode(first))
    assert second == first, f"re-encoding changed the result: {first!r} -> {second!r}"


@pytest.mark.parametrize("raw", UNSAFE_REPAIR_RESULTS)
def test_reencode_keeps_rejected_content(raw):
    """A rejected-but-parseable reply must survive re-encoding unchanged."""
    first = Scorer._parse_score_result(raw)
    assert first[0] >= 0
    assert Scorer._parse_score_result(_reencode(first)) == first


@pytest.mark.parametrize("suffix", ["。", "."])
@pytest.mark.parametrize(
    "trigger",
    ["ungrounded_score_facts", "ungrounded_score_theme", "invalid_score_contract"],
)
def test_sentence_stop_is_accepted_in_each_repair_path(side_effects, suffix, trigger):
    raw = _reencode((FIXED[0], FIXED[1], FIXED[2] + suffix, FIXED[3]))
    parsed, reason, detail = _validate(raw, "repair", trigger)
    assert parsed == FIXED
    assert reason == detail == ""
    assert side_effects == {"capture": 1, "record": 1}


@pytest.mark.parametrize(
    "suffix",
    [
        "、",
        "：",
        ":",
        "，",
        ",",
        "。。",
        "..",
        "...",
        "。.",
        "！",
        "？",
        "…",
        "（但订单增长）",
        "。订单增长",
        "。123",
    ],
)
def test_other_suffixes_are_not_collapsed_to_fixed_tuple(side_effects, suffix):
    expected = (FIXED[0], FIXED[1], FIXED[2] + suffix, FIXED[3])
    raw = _reencode(expected)
    assert Scorer._parse_score_result(raw) == expected
    for trigger, rejection in [
        ("ungrounded_score_facts", "unsafe_grounding_repair_result"),
        ("invalid_score_contract", "unsafe_contract_repair_result"),
    ]:
        _, reason, _ = _validate(raw, "repair", trigger)
        assert reason in {rejection, "ungrounded_score_facts", "ungrounded_score_theme"}
    assert _grounding_repair_result_is_safe(expected) is False


@pytest.mark.parametrize(
    "score,tag,direction",
    [(5.0, "其他", "⚖️"), (4.0, "政策", "⚖️"), (4.0, "其他", "📈"), (4.0, "其他", "📉")],
)
def test_complete_neutral_tuple_is_required(score, tag, direction):
    expected = (score, tag, FIXED[2] + "。", direction)
    assert Scorer._parse_score_result(_reencode(expected)) == expected
    assert _grounding_repair_result_is_safe(expected) is False


@pytest.mark.parametrize(
    "logic",
    [
        "公司发布新品。",
        "公司发布新品,",
        "版本升级至GPT-5.",
        "收益可能增长...",
        "来源未显示明确投资催化。仍需核实",
        "预计增长2.5.",
    ],
)
def test_ordinary_json_logic_is_preserved_byte_for_byte(logic):
    expected = (7.0, "产业动态", logic, "⚖️")
    assert Scorer._parse_score_result(_reencode(expected)) == expected


@pytest.mark.parametrize(
    "raw",
    [
        FIXED_PERIOD + "。",
        FIXED_PERIOD + " extra explanation",
        FIXED_PERIOD + FIXED_OK,
        "```json\n" + FIXED_PERIOD + "\n```",
        FIXED_PERIOD.replace('"score": 4', '"score": 4, "score": 4'),
        FIXED_PERIOD.replace('"score": 4', '"score": true'),
        FIXED_PERIOD.replace('"score": 4', '"score": NaN'),
        FIXED_PERIOD.replace('"score": 4', '"score": Infinity'),
        FIXED_PERIOD.replace('"score": 4', '"score": 4, "extra": 0'),
        FIXED_PERIOD.replace('"tag": "其他"', '"tag": "其他 "'),
        FIXED_PERIOD.replace('"logic": "来源', '"logic": " 来源'),
        FIXED_PERIOD.replace('催化。"', '催化。 "'),
        FIXED_PERIOD.replace('催化。"', '催化。\\n"'),
        FIXED_PERIOD[:-1],
    ],
)
def test_strict_json_boundary_is_not_relaxed(side_effects, raw):
    parsed, detail = Scorer._parse_score_result_with_detail(raw)
    assert parsed[0] < 0
    assert detail
    for attempt, trigger in [("initial", ""), ("repair", "invalid_score_contract")]:
        _, reason, detail = _validate(raw, attempt, trigger)
        assert reason == "invalid_score_contract"
        assert detail
