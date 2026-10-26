from pathlib import Path

import yaml

from core.prompts import BUILTIN_PROMPTS


def _production_config():
    return yaml.safe_load(
        (Path(__file__).resolve().parents[1] / "config.yaml").read_text(
            encoding="utf-8"
        )
    )


def test_production_scoring_routes_have_ordered_cold_backup():
    cfg = _production_config()

    assert cfg["llm_routing"]["stage1"] == [
        "kimi-code",
        "siliconflow-qwen",
    ]
    assert cfg["llm_routing"]["stage2"] == [
        "kimi-code",
        "siliconflow-qwen",
    ]


def test_production_qwen_has_enough_output_budget_for_score_json():
    cfg = _production_config()
    qwen = next(item for item in cfg["llm"] if item["name"] == "siliconflow-qwen")

    assert qwen["min_output_tokens"] == 160
    assert cfg["llm_semantic_repair"] == {"enabled": True}


def test_production_scoring_layers_and_expected_feeds_are_enabled():
    cfg = _production_config()

    assert cfg["briefing_assembly"]["enabled"] is True
    assert cfg["briefing_assembly"]["grounding"]["enabled"] is True
    assert cfg["fast_pass"]["enabled"] is True
    assert cfg["llm_score_cache"]["enabled"] is True
    assert cfg["local_reject_rules"]["enabled"] is True
    assert cfg["quiet_mode"]["enabled"] is True
    assert cfg["two_stage_scoring"]["enabled"] is True
    assert cfg["few_shot_feedback"]["enabled"] is False

    enabled_feeds = {
        feed["name"] for feed in cfg["feeds"] if feed.get("enabled") is True
    }
    assert enabled_feeds == {
        "财联社电报",
        "同花顺7x24",
        "36氪快讯",
        "华尔街见闻全球",
        "证监会新闻",
        "国家发改委·新闻发布",
        "国家统计局·最新数据",
        "华尔街见闻·实时",
        "财新网",
        "第一财经·最新",
        "格隆汇",
        "华尔街见闻·A股实时",
        "上交所公告",
        "上交所监管问询",
        "上交所科创板项目动态",
        "深交所主板问询",
        "深交所创业板问询",
        "深交所上市公司公告",
        "东方财富A股搜索",
        "金十数据",
    }


def test_production_kimi_transport_circuit_recovers_quickly():
    cfg = _production_config()
    kimi = next(item for item in cfg["llm"] if item["name"] == "kimi-code")

    assert kimi["max_fail_count"] == 8
    assert kimi["fail_decay_seconds"] == 300


def test_mixed_and_company_feeds_use_cross_industry_prompt():
    cfg = _production_config()
    expected = {
        "财联社电报",
        "同花顺7x24",
        "华尔街见闻全球",
        "华尔街见闻·实时",
        "财新网",
        "第一财经·最新",
        "格隆汇",
        "华尔街见闻·A股实时",
        "上交所公告",
        "上交所监管问询",
        "上交所科创板项目动态",
        "深交所主板问询",
        "深交所创业板问询",
        "深交所上市公司公告",
        "东方财富A股搜索",
    }
    configured = {
        feed["name"]: feed["prompt"]
        for feed in cfg["feeds"]
        if feed["name"] in expected
    }

    assert configured == {name: "industry" for name in expected}


def test_all_scoring_prompts_defer_to_final_json_contract_without_examples():
    cfg = _production_config()
    prompts = {
        **{f"config:{key}": value for key, value in cfg["prompts"].items()},
        **{f"builtin:{key}": value for key, value in BUILTIN_PROMPTS.items()},
    }
    forbidden_fragments = (
        "\u4e25\u683c\u56db\u884c",
        "\u793a\u4f8b",
        "\n\u5206\u6570:",
        "\n\u6807\u7b7e:",
        "\n\u903b\u8f91:",
        "\n\u60c5\u7eea:",
    )

    for name, prompt in prompts.items():
        expected_contract = (
            "\u6309\u8bc4\u5206\u5668\u8ffd\u52a0\u7684\u6700\u7ec8 JSON "
            "\u5951\u7ea6\u8fd4\u56de\u7ed3\u679c"
        )
        assert expected_contract in prompt, name
        assert not any(fragment in prompt for fragment in forbidden_fragments), name
