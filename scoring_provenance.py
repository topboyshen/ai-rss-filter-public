"""Bounded scoring-resolution labels shared by scoring and persistence."""

from decimal import Decimal, InvalidOperation

SEMANTIC_RESOLUTION_SAFE_NEUTRAL_CONTRACT = "safe_neutral_contract"
SEMANTIC_RESOLUTION_SAFE_NEUTRAL_GROUNDING = "safe_neutral_grounding"
SEMANTIC_RESOLUTION_SAFE_NEUTRAL_INITIAL = "safe_neutral_initial"
SEMANTIC_RESOLUTION_INSUFFICIENT_INPUT = "insufficient_input"
INSUFFICIENT_INPUT_TAG = "信息不足"
INSUFFICIENT_INPUT_EXPLANATIONS = (
    "缺少可识别主体",
    "缺少可识别事件",
    "缺少可识别主体和事件",
)
INSUFFICIENT_INPUT_PROMPT = (
    f'标题和摘要合起来仍无法识别主体或事件时，不要猜，也不要给 4 分。输出 score=0、'
    f'tag="{INSUFFICIENT_INPUT_TAG}"、sentiment="中性"。logic 只可原样选择'
    + "、".join(f"“{reason}”" for reason in INSUFFICIENT_INPUT_EXPLANATIONS)
    + "之一，不得附加其他内容。"
)
SAFE_NEUTRAL_CONTRACT_VALUES = (
    4.0,
    "其他",
    "来源未显示明确投资催化",
    "⚖️",
)


def is_fixed_safe_neutral_fields(score, tag, logic, direction) -> bool:
    """Exact content guard for legacy cache reads, without rewriting history."""
    return (score, tag, logic) == SAFE_NEUTRAL_CONTRACT_VALUES[:3] and direction in {
        "中性",
        "⚖️",
    }


def is_insufficient_input_fields(score, tag, logic, direction) -> bool:
    """Semantic 'not enough input' is a sentinel, never a substantive score."""
    try:
        zero_score = Decimal(str(score)).is_zero()
    except InvalidOperation:
        return False
    return (
        zero_score
        and tag == INSUFFICIENT_INPUT_TAG
        and logic in INSUFFICIENT_INPUT_EXPLANATIONS
        and str(direction or "").strip() in {"中性", "⚖️"}
    )


ALLOWED_SEMANTIC_RESOLUTIONS = frozenset(
    {
        "",
        SEMANTIC_RESOLUTION_SAFE_NEUTRAL_CONTRACT,
        SEMANTIC_RESOLUTION_SAFE_NEUTRAL_GROUNDING,
        SEMANTIC_RESOLUTION_SAFE_NEUTRAL_INITIAL,
        SEMANTIC_RESOLUTION_INSUFFICIENT_INPUT,
    }
)

SAFE_NEUTRAL_CONTRACT_SCORE_PATHS = {
    "stage1": "llm_safe_neutral_contract",
    "stage2": "stage2_safe_neutral_contract",
}

SAFE_NEUTRAL_GROUNDING_SCORE_PATHS = {
    "stage1": "llm_safe_neutral_grounding",
    "stage2": "stage2_safe_neutral_grounding",
}

SAFE_NEUTRAL_INITIAL_SCORE_PATHS = {
    "stage1": "llm_safe_neutral_initial",
    "stage2": "stage2_safe_neutral_initial",
}

INSUFFICIENT_INPUT_SCORE_PATHS = {
    "stage1": "insufficient_input",
    "stage2": "stage2_insufficient_input",
}

SAFE_NEUTRAL_SCORE_PATHS_BY_RESOLUTION = {
    SEMANTIC_RESOLUTION_SAFE_NEUTRAL_INITIAL: SAFE_NEUTRAL_INITIAL_SCORE_PATHS,
    SEMANTIC_RESOLUTION_SAFE_NEUTRAL_CONTRACT: (SAFE_NEUTRAL_CONTRACT_SCORE_PATHS),
    SEMANTIC_RESOLUTION_SAFE_NEUTRAL_GROUNDING: (SAFE_NEUTRAL_GROUNDING_SCORE_PATHS),
    SEMANTIC_RESOLUTION_INSUFFICIENT_INPUT: INSUFFICIENT_INPUT_SCORE_PATHS,
}

NON_SUBSTANTIVE_SCORE_PATHS = frozenset(
    path
    for paths in SAFE_NEUTRAL_SCORE_PATHS_BY_RESOLUTION.values()
    for path in paths.values()
)

# These paths are audit/read-model states, not independent score evidence.
# They must never seed similarity copies or cross the Quant integration
# boundary, even if a legacy row carries an otherwise usable numeric score.
NON_EXPORTABLE_SCORE_PATHS = NON_SUBSTANTIVE_SCORE_PATHS | frozenset({
    "parse_error",
    "quality_quarantine",
    "similarity",
})

RECOGNIZED_SCORE_PATHS = frozenset({
    "llm",
    "stage2",
    "fast_pass",
    "local_reject",
    "fallback_local",
    "parse_error",
    "similarity",
    "quality_quarantine",
    "radar",
}) | NON_SUBSTANTIVE_SCORE_PATHS

PUBLIC_SCORE_PATHS = RECOGNIZED_SCORE_PATHS - (
    NON_SUBSTANTIVE_SCORE_PATHS | {"quality_quarantine"}
)

FORMAL_SIGNAL_SCORE_PATHS = (
    RECOGNIZED_SCORE_PATHS - NON_EXPORTABLE_SCORE_PATHS
)


def normalize_score_path(value: object) -> str:
    """Canonicalize the bounded score-path enum without touching model IDs."""
    return str(value or "").strip().casefold()


def is_recognized_score_path(value: object) -> bool:
    return normalize_score_path(value) in RECOGNIZED_SCORE_PATHS


SAFE_NEUTRAL_SCORE_PATH_METADATA = {
    path: (stage, resolution)
    for resolution, paths in SAFE_NEUTRAL_SCORE_PATHS_BY_RESOLUTION.items()
    for stage, path in paths.items()
}

# Bounded, record-backed evidence notes for safe-neutral decisions.
# Written from the already-computed rejection reason, never generated text.
NEUTRAL_REASON_THEME = "theme"
NEUTRAL_REASON_FACTS = "facts"
NEUTRAL_REASON_CONTRACT = "contract"
NEUTRAL_REASON_INITIAL = "initial"
NEUTRAL_REASON_INSUFFICIENT = "insufficient"
NEUTRAL_EVIDENCE_REASONS = frozenset(
    {
        NEUTRAL_REASON_THEME,
        NEUTRAL_REASON_FACTS,
        NEUTRAL_REASON_CONTRACT,
        NEUTRAL_REASON_INITIAL,
        NEUTRAL_REASON_INSUFFICIENT,
    }
)
