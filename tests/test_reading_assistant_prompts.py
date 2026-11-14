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
    assert "研究阅读优先级" in default
    assert "四道闸门" in default
    assert "信息不足" in default
    assert "4 分只表示" in default
    assert "重复事件" in default
    industry = get_prompt("industry", cfg)
    assert default in industry
    assert "多行业频道" in industry
    assert "不把非科技新闻强行归入" in industry
    assert "上帝视角" not in BUILTIN_PROMPTS["default"]
    assert "顶级宏观策略分析师" not in BUILTIN_PROMPTS["default"]


def test_prompt_text_change_updates_scoring_contract_fingerprint():
    cfg = load_config()
    original = cfg["scoring_strategy_fingerprint"]
    assert original
    mutated = deepcopy(cfg)
    mutated["prompts"] = {"default": "changed prompt"}
    assert _strategy_fingerprint(mutated) != original


def test_36kr_feed_still_selects_industry_prompt_after_reading_assistant_rewrite():
    cfg = yaml.safe_load(
        (Path(__file__).resolve().parents[1] / "config.yaml").read_text(
            encoding="utf-8"
        )
    )
    selected = get_prompt_for_url("http://rsshub:1200/36kr/newsflashes", cfg)
    assert selected == BUILTIN_PROMPTS["industry"]
    assert "多行业频道" in selected


def test_insufficient_input_boundary_is_narrow_and_pairs_zero_score():
    rubric = BUILTIN_PROMPTS["default"]
    assert "只有“标题和摘要都无法识别主体或事件”时才用信息不足" in rubric
    assert "“摘要为空”不等于信息不足" in rubric
    assert "必须按研究价值正常给分" in rubric
    assert "缺少数字、来源、金额或后续细节时给 5-7 分" in rubric
    assert "标题已陈述的事实视为来源事实" in rubric
    assert "不要判断事件在现实中是否真实" in rubric
    assert 'tag="信息不足" 只能与 score=0 同时出现' in rubric
    assert "标题+摘要无法识别主体或事件时，不要猜" not in rubric
