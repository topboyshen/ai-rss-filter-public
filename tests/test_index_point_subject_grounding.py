"""An adjacent quoted index point retains its explicitly reported subject."""

import json
from types import SimpleNamespace

import pytest

from core.scorer import Scorer, _numeric_claims_are_grounded, _score_grounding_verdict


SOURCE = "沪指下跌0.12%，报3809.08点"
FROZEN_TITLE = "A股震荡整理，沪指半日微跌0.12%"
FROZEN_SOURCE = (
    "10月12日， A股 三大指数集体下跌，截至上午收盘，沪指下跌0.12%，报3809.08点；"
    "深成指下跌1.03%，报12512.1点；创业板指下跌1.27%，报3004.81点；"
    "科创50指数下跌0.78%，报1445.95点；北证50指数下跌1.24%，报1024.41点。"
)


@pytest.fixture
def validate(monkeypatch):
    monkeypatch.setattr("core.scorer.record_score_validation", lambda **kwargs: None)
    monkeypatch.setattr("core.scorer.capture_validation_attempt", lambda **kwargs: None)
    scorer = object.__new__(Scorer)

    def validate_logic(logic, *, source=SOURCE, stage="stage1", title="市场午间行情播报"):
        raw = json.dumps(
            dict(score=3, tag="市场行情", logic=logic, sentiment="中性"),
            ensure_ascii=False,
        )
        return scorer._validate_scoring_attempt(
            raw, title, source, "initial", "", stage,
            SimpleNamespace(name="fixture", model="no-transport"), {},
        )

    return validate_logic


@pytest.mark.parametrize("stage", ["stage1", "stage2"])
def test_recorded_index_point_quote_passes_complete_validator(validate, stage):
    parsed, reason, detail = validate(SOURCE, stage=stage)
    assert parsed == (3.0, "市场行情", SOURCE, "⚖️")
    assert reason == detail == ""


@pytest.mark.parametrize("stage", ["stage1", "stage2"])
def test_exact_quote_is_grounded_in_full_frozen_source(validate, stage):
    parsed, reason, detail = validate(
        SOURCE, source=FROZEN_SOURCE, title=FROZEN_TITLE, stage=stage
    )
    assert parsed == (3.0, "市场行情", SOURCE, "⚖️")
    assert reason == detail == ""


def test_original_rejected_paraphrase_does_not_gain_a_general_exemption(validate):
    logic = "10月12日半日沪指跌0.12%报3809.08点，深成指、创业板指均下跌，属常规盘面波动"
    _, reason, _ = validate(logic, source=FROZEN_SOURCE, title=FROZEN_TITLE)
    assert reason == "ungrounded_score_facts"


@pytest.mark.parametrize(
    "index",
    ["沪指", "上证指数", "上证综指", "上证综合指数", "深成指", "深证成指",
     "深证成份指数", "创业板指", "创业板指数", "恒指", "恒生指数", "纳指"],
)
@pytest.mark.parametrize("direction", ["涨", "跌", "上涨", "下跌"])
def test_only_explicit_index_direction_can_anchor_adjacent_point(validate, index, direction):
    source = f"{index}{direction}0.12%，报3809.08点"
    parsed, reason, detail = validate(source, source=source)
    assert parsed == (3.0, "市场行情", source, "⚖️")
    assert reason == detail == ""


@pytest.mark.parametrize(
    "point", ["3809.08", "-3809.08", "+3809.08", "约3809.08", "±3809.08",
              "≈3809.08", "≤3809.08", "≥3809.08", "不低于3809.08",
              "3809.08点以上", "3809.08点以下"],
)
def test_point_qualifiers_remain_part_of_verbatim_grounding(validate, point):
    point_text = point if "点" in point else f"{point}点"
    source = f"沪指下跌0.12%，报{point_text}"
    _, reason, detail = validate(source, source=source)
    assert reason == detail == ""


@pytest.mark.parametrize(
    "source,logic",
    [
        (SOURCE, SOURCE.replace("沪指", "深成指")),
        (SOURCE, SOURCE.replace("3809.08", "3809.09")),
        (SOURCE, SOURCE.replace("3809.08点", "3809.08元")),
        (SOURCE, SOURCE.replace("0.12%", "0.13%")),
        (SOURCE, SOURCE.replace("下跌", "上涨")),
        (SOURCE, SOURCE.replace("下跌", "未下跌")),
        (SOURCE, SOURCE.replace("下跌", "预计下跌")),
        (SOURCE, "报3809.08点"),
        (SOURCE, "下跌0.12%，报3809.08点"),
        (SOURCE, "沪指下跌0.12%，深成指报3809.08点"),
        (SOURCE, "沪指下跌0.12%，甲公司报3809.08点"),
        (SOURCE, SOURCE.replace("报3809.08", "报-3809.08")),
        (SOURCE.replace("报3809.08", "报-3809.08"), SOURCE),
        (SOURCE.replace("报3809.08", "报±3809.08"), SOURCE),
        (SOURCE.replace("报3809.08", "报≈3809.08"), SOURCE),
        (SOURCE.replace("报3809.08", "报≥3809.08"), SOURCE),
        (SOURCE.replace("报3809.08", "报不低于3809.08"), SOURCE),
        (SOURCE.replace("点", "点以上"), SOURCE),
        (SOURCE.replace("点", "点以下"), SOURCE),
        (SOURCE.replace("下跌", "未下跌"), SOURCE),
        (SOURCE.replace("下跌", "预计下跌"), SOURCE),
        (SOURCE.replace("下跌", "计划下跌"), SOURCE),
        (SOURCE.replace("沪指", "沪指和深成指"), SOURCE),
        (SOURCE.replace("沪指", "沪指、深成指"), SOURCE),
        (SOURCE.replace("，", "，成交活跃，"), SOURCE),
        (SOURCE.replace("，", "，深成指震荡，"), SOURCE),
        (SOURCE, SOURCE + "，报3809.08点"),
        (SOURCE, SOURCE + "；" + SOURCE),
        (SOURCE, SOURCE.replace("3809.08点", "3809.08至3810点")),
    ],
)
def test_inheritance_never_exempts_changed_or_missing_facts(validate, source, logic):
    parsed, reason, detail = validate(logic, source=source)
    assert parsed == (3.0, "市场行情", logic, "⚖️")
    assert reason == "ungrounded_score_facts"
    assert detail == ""
    assert _score_grounding_verdict(parsed, "市场午间行情播报", source) == (
        "ungrounded_score_facts", "numeric_fact_failure"
    )


@pytest.mark.parametrize("boundary", ["。", "；", ";", "\n", "\r\n", "！", "？", "、", "，，", ",,"])
def test_inheritance_does_not_cross_hard_or_ambiguous_boundaries(validate, boundary):
    source = SOURCE.replace("，", boundary)
    _, reason, _ = validate(SOURCE, source=source)
    assert reason == "ungrounded_score_facts"
    assert not _numeric_claims_are_grounded(SOURCE, source)
    assert not _numeric_claims_are_grounded(source, source)


@pytest.mark.parametrize("comma", [",", "，", ", ", "，　"])
def test_single_adjacent_comma_can_have_whitespace(validate, comma):
    source = SOURCE.replace("，", comma)
    _, reason, detail = validate(source, source=source)
    assert reason == detail == ""


@pytest.mark.parametrize(
    "source",
    ["沪指和深成指下跌0.12%，报3809.08点",
     "沪指未下跌0.12%，报3809.08点", "沪指预计下跌0.12%，报3809.08点",
     "沪指下跌0.12%，成交活跃，报3809.08点",
     "甲公司下跌0.12%，报3809.08点", "陌生指数下跌0.12%，报3809.08点",
     "沪指下跌约0.12%，报3809.08点", "沪指下跌±0.12%，报3809.08点",
     "沪指下跌0.12%，报∓3809.08点"],
)
def test_unhandled_or_ambiguous_wording_stays_conservative(validate, source):
    _, reason, _ = validate(source, source=source)
    assert reason == "ungrounded_score_facts"


@pytest.mark.parametrize("numeral", ["壹", "贰", "叁", "肆", "伍", "陆", "柒", "捌", "玖", "拾", "佰", "仟"])
def test_inheritance_does_not_relax_real_financial_upper_numerals(validate, numeral):
    source = SOURCE + f"，甲公司合同金额{numeral}万元"
    _, reason, _ = validate(source, source=source)
    assert reason == "ungrounded_score_facts"


def test_already_grounded_direction_only_quote_stays_supported(validate):
    logic = "沪指下跌0.12%"
    _, reason, detail = validate(logic)
    assert reason == detail == ""


@pytest.mark.parametrize(
    "source,logic",
    [
        (SOURCE + "；沪指下跌0.23%，报3810点", "沪指下跌0.12%，报3810点"),
        (SOURCE + "；沪指下跌0.23%，报3810点", "沪指下跌0.23%，报3809.08点"),
        (SOURCE + "；深成指下跌0.12%，报3810点", "深成指下跌0.12%，报3809.08点"),
        (SOURCE, "沪指下跌0.12%，沪指下跌0.12%，报3809.08点"),
        (SOURCE + "；沪指下跌-0.12%，报3810点", "沪指下跌-0.12%，报3809.08点"),
        (SOURCE + "；沪指上涨0.12%，报3810点", "沪指上涨0.12%，报3809.08点"),
        (
            SOURCE + "；沪指下跌0.23%，报3810点",
            "沪指下跌0.12%，报3810点；沪指下跌0.23%，报3809.08点",
        ),
    ],
)
def test_point_inheritance_does_not_splice_or_duplicate_numeric_evidence(validate, source, logic):
    _, reason, _ = validate(logic, source=source)
    assert reason == "ungrounded_score_facts"


def test_distinct_reported_index_change_point_pairs_stay_supported(validate):
    source = SOURCE + "；沪指下跌0.23%，报3810点"
    _, reason, detail = validate(source, source=source)
    assert reason == detail == ""
