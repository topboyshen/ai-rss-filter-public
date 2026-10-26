from copy import deepcopy

import yaml
from pathlib import Path

from core.config import _strategy_fingerprint, load_config
from core.prompts import BUILTIN_PROMPTS, get_prompt, get_prompt_for_url


def test_reading_assistant_prompts_drop_god_view_and_keep_industry_anchors():
    cfg = load_config()
    default = get_prompt("default", cfg)
    assert "上帝视角" not in default
    assert "顶级宏观策略分析师" not in default
    assert "没有新增信息的重复报道" in default
    assert "不要一律低分" in default
    assert "观点可以值得阅读" in default
    assert "点名也不等于受益" in default
    industry = get_prompt("industry", cfg)
    assert "来源频道不是行业证据" in industry
    assert "覆盖科技、创新药与医疗、消费、新能源" in industry
    assert "上帝视角" not in BUILTIN_PROMPTS["default"]
    assert "顶级宏观策略分析师" not in BUILTIN_PROMPTS["default"]


def test_prompt_text_change_updates_scoring_contract_fingerprint():
    cfg = load_config()
    original = cfg["scoring_strategy_fingerprint"]
    assert original
    mutated = deepcopy(cfg)
    mutated["prompts"] = dict(cfg["prompts"])
    mutated["prompts"]["default"] = cfg["prompts"]["default"] + "\n"
    assert _strategy_fingerprint(mutated) != original


def test_36kr_feed_still_selects_industry_prompt_after_reading_assistant_rewrite():
    cfg = yaml.safe_load(
        (Path(__file__).resolve().parents[1] / "config.yaml").read_text(
            encoding="utf-8"
        )
    )
    selected = get_prompt_for_url("http://rsshub:1200/36kr/newsflashes", cfg)
    assert selected == cfg["prompts"]["industry"]
    assert "来源频道不是行业证据" in selected
