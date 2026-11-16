"""Bounded scoring-resolution labels shared by scoring and persistence."""

from dataclasses import dataclass
from decimal import Decimal, InvalidOperation

SEMANTIC_RESOLUTION_SAFE_NEUTRAL_CONTRACT = "safe_neutral_contract"
SEMANTIC_RESOLUTION_SAFE_NEUTRAL_GROUNDING = "safe_neutral_grounding"
SEMANTIC_RESOLUTION_SAFE_NEUTRAL_INITIAL = "safe_neutral_initial"
SEMANTIC_RESOLUTION_INSUFFICIENT_INPUT = "insufficient_input"
SEMANTIC_RESOLUTION_MARKET_FLASH = "market_flash_rule"
INSUFFICIENT_INPUT_TAG = "信息不足"
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
    # The explanation stays free text: requiring a fixed menu pushed the model
    # to choose the sentinel far more often and cost relevant items.  What the
    # contract needs is an exact zero, the reserved tag, a recorded reason and
    # a neutral direction; the reserved tag is rejected otherwise.
    return (
        zero_score
        and tag == INSUFFICIENT_INPUT_TAG
        and bool(str(logic or "").strip())
        and str(direction or "").strip() in {"中性", "⚖️"}
    )


ALLOWED_SEMANTIC_RESOLUTIONS = frozenset(
    {
        "",
        SEMANTIC_RESOLUTION_SAFE_NEUTRAL_CONTRACT,
        SEMANTIC_RESOLUTION_SAFE_NEUTRAL_GROUNDING,
        SEMANTIC_RESOLUTION_SAFE_NEUTRAL_INITIAL,
        SEMANTIC_RESOLUTION_INSUFFICIENT_INPUT,
        SEMANTIC_RESOLUTION_MARKET_FLASH,
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

# A deterministic guard, not a model verdict: a terse index/asset move is a
# real subject and event, so it gets a low substantive score instead of the
# insufficient-input sentinel.  Kept out of NON_SUBSTANTIVE so it is never
# treated as a conservative neutral, and out of the exportable set because it
# is not model evidence.
MARKET_FLASH_TAG = "行情"
MARKET_FLASH_SCORE = 3.0
MARKET_FLASH_LOGIC = "标题含明确指数或资产价格变动，按行情快讯处理"
MARKET_FLASH_VALUES = (MARKET_FLASH_SCORE, MARKET_FLASH_TAG, MARKET_FLASH_LOGIC, "⚖️")
MARKET_FLASH_SCORE_PATHS = {
    "stage1": "market_flash_rule",
    "stage2": "stage2_market_flash_rule",
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

# Resolution-to-path lookup for the processor.  Safe-neutral resolutions stay
# non-substantive; the market-flash resolution is a substantive rule score.
RESOLUTION_SCORE_PATHS = {
    **SAFE_NEUTRAL_SCORE_PATHS_BY_RESOLUTION,
    SEMANTIC_RESOLUTION_MARKET_FLASH: MARKET_FLASH_SCORE_PATHS,
}

# These paths are audit/read-model states, not independent score evidence.
# They must never seed similarity copies or cross the Quant integration
# boundary, even if a legacy row carries an otherwise usable numeric score.
NON_EXPORTABLE_SCORE_PATHS = NON_SUBSTANTIVE_SCORE_PATHS | frozenset({
    "parse_error",
    "quality_quarantine",
    "similarity",
    *MARKET_FLASH_SCORE_PATHS.values(),
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
    *MARKET_FLASH_SCORE_PATHS.values(),
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

# One read-surface registry for every persisted score path.  Adding a path here
# is enough for dashboard count defaults, breakdown labels and reading labels;
# those surfaces must not keep parallel copies that drift.
FEED_SCORE_CATEGORY_LABELS = (
    ("passed", "正式达标"),
    ("below_threshold", "低于阈值"),
    ("similarity", "重复稿"),
    ("safe_contract", "格式/字段中性"),
    ("safe_grounding", "来源依据中性"),
    ("insufficient_input", "信息不足（0分）"),
    ("market_flash", "行情规则（3分）"),
    ("model_failure", "模型链路失败"),
    ("processing_error", "条目处理异常"),
    ("other_excluded", "其他未计入"),
)
FEED_SCORE_CATEGORY_LABEL_BY_KEY = dict(FEED_SCORE_CATEGORY_LABELS)
FEED_SCORE_CATEGORY_KEYS = tuple(FEED_SCORE_CATEGORY_LABEL_BY_KEY)
DEFAULT_FEED_SCORE_COUNTS = dict.fromkeys(FEED_SCORE_CATEGORY_KEYS, 0)


@dataclass(frozen=True)
class ScorePathSpec:
    """Category and human label for one persisted score path."""

    path_id: str
    category: str
    reading_label: str


SCORE_PATH_SPECS = {
    spec.path_id: spec
    for spec in (
        ScorePathSpec("llm", "", "模型评分"),
        ScorePathSpec("stage2", "", "模型二阶段评分"),
        ScorePathSpec("fast_pass", "", "本地快速规则"),
        ScorePathSpec("local_reject", "", "本地拒绝规则"),
        ScorePathSpec("fallback_local", "model_failure", "系统兜底"),
        ScorePathSpec("parse_error", "processing_error", "处理异常（解析失败）"),
        ScorePathSpec("quality_quarantine", "processing_error", "质量隔离"),
        ScorePathSpec("similarity", "similarity", "相似结果复用"),
        ScorePathSpec("radar", "", "雷达路径"),
        *(
            ScorePathSpec(path, "safe_contract", "保守中性")
            for path in SAFE_NEUTRAL_CONTRACT_SCORE_PATHS.values()
        ),
        *(
            ScorePathSpec(path, "safe_grounding", "保守中性")
            for path in SAFE_NEUTRAL_GROUNDING_SCORE_PATHS.values()
        ),
        *(
            ScorePathSpec(path, "other_excluded", "保守中性")
            for path in SAFE_NEUTRAL_INITIAL_SCORE_PATHS.values()
        ),
        *(
            ScorePathSpec(path, "insufficient_input", "信息不足（0分）")
            for path in INSUFFICIENT_INPUT_SCORE_PATHS.values()
        ),
        *(
            ScorePathSpec(path, "market_flash", "行情规则（3分）")
            for path in MARKET_FLASH_SCORE_PATHS.values()
        ),
    )
}

if set(SCORE_PATH_SPECS) != RECOGNIZED_SCORE_PATHS:
    raise RuntimeError(
        "score path registry is out of sync with RECOGNIZED_SCORE_PATHS"
    )
if not set(FEED_SCORE_CATEGORY_KEYS) >= {
    spec.category for spec in SCORE_PATH_SPECS.values() if spec.category
}:
    raise RuntimeError(
        "score path category is not declared in FEED_SCORE_CATEGORY_LABELS"
    )


def score_path_reading_label(value: object) -> str:
    """Reading-surface explanation for a persisted path; never guess."""
    spec = SCORE_PATH_SPECS.get(normalize_score_path(value))
    return spec.reading_label if spec else "未知路径"
