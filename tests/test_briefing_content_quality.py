"""Content-contract regression tests for 今日情报 Briefing.

These tests use already-scored in-memory rows only.  They do not call an LLM,
query a production database, or alter the scoring and Quant decision paths.
"""

from __future__ import annotations

import pytest

from core.briefing_assembly import assemble_briefing_candidates
from core.asset_mapping import map_article_to_assets
from core.digest_engine import (
    briefing_source_confirmation,
    render_themes_html,
    render_themes_markdown,
    render_themes_rss,
)
from core.theme_rules import (
    canonical_theme_name,
    resolve_configured_theme,
    theme_display_name,
)
from routes import dashboard_routes as dashboard


def _cfg():
    return {
        "briefing": {"default_hours": 24},
        "briefing_assembly": {
            "enabled": True,
            "max_themes": 4,
            "max_articles_per_theme": 4,
            "max_share_ratio": 1.0,
            "reserved_non_head_slots": 0,
            "grounding": {"enabled": True},
        },
        "briefing_themes": [
            {
                "name": "黄金与贵金属",
                "keywords": ["黄金", "金价", "贵金属", "央行购金"],
            },
            {
                "name": "国内货币政策与流动性",
                "keywords": [
                    "中国央行", "中国人民银行", "人民银行", "央行", "货币政策",
                    "逆回购", "MLF", "LPR", "降息", "降准", "社融", "M1", "M2",
                    "流动性",
                ],
            },
            {
                "name": "国内宏观政策",
                "keywords": ["国务院", "政治局", "稳增长", "宏观政策", "财政政策"],
            },
            {
                "name": "AI 算力链",
                "keywords": ["AI", "算力", "GPU", "服务器"],
            },
            {
                "name": "央行与黄金",
                "subscription_only": True,
                "keywords": ["央行", "人民银行", "黄金", "金价", "贵金属"],
            },
        ],
    }


def _article(
    title,
    *,
    tag="市场观察",
    score=8.5,
    created_at=100,
    index=1,
    summary="",
):
    return {
        "title": title,
        "raw_title": title,
        "tag": tag,
        "score": score,
        "logic": "",
        "summary": summary,
        "direction": "中性",
        "created_at": created_at,
        "link": f"https://source{index}.example/news/{index}",
        "feed_url": f"https://source{index}.example/rss",
    }


def test_default_window_is_bounded_and_configurable():
    assert dashboard._briefing_default_hours(_cfg()) == 24
    assert dashboard._briefing_default_hours({"briefing": {"default_hours": 0}}) == 1
    assert dashboard._briefing_default_hours({"briefing": {"default_hours": 999}}) == 168
    assert dashboard._briefing_default_hours({"briefing": {"default_hours": "bad"}}) == 24
    assert dashboard._briefing_default_hours({"briefing": "bad-section"}) == 24


def test_display_themes_split_monetary_policy_and_gold_but_keep_legacy_rss_union():
    cfg = _cfg()

    assert dashboard._briefing_theme_for_article(
        _article("中国人民银行开展5000亿元逆回购", tag="央行与黄金"), cfg=cfg
    ) == "国内货币政策与流动性"
    assert dashboard._briefing_theme_for_article(
        _article("央行连续第十个月增持黄金储备", tag="央行与黄金"), cfg=cfg
    ) == "黄金与贵金属"
    assert "央行与黄金" not in {
        name for name, _keywords in dashboard._briefing_theme_rules(cfg)
    }

    legacy = resolve_configured_theme(cfg, "央行与黄金")
    assert legacy is not None
    assert legacy.name == "央行与黄金"
    assert {"央行", "黄金"}.issubset(set(legacy.terms))

    selected, audit = assemble_briefing_candidates(
        [
            _article("中国人民银行开展5000亿元逆回购", tag="央行与黄金", index=1),
            _article("国际金价刷新历史高点", tag="央行与黄金", index=2),
        ],
        cfg,
        limit=2,
    )
    assert len(selected) == 2
    assert set(audit["selected_primary_themes"]) == {
        "国内货币政策与流动性",
        "黄金与贵金属",
    }


def test_supported_macro_tag_variants_collapse_to_one_factual_theme():
    articles = [
        _article(
            "中国人民银行开展5000亿元逆回购",
            tag="宏观放水",
            created_at=103,
            index=1,
        ),
        _article(
            "央行下调MLF利率20个基点",
            tag="宏观宽松+制度红利",
            created_at=102,
            index=2,
        ),
        _article(
            "M2同比增速回升，流动性保持合理充裕",
            tag="宏观政策/流动性",
            created_at=101,
            index=3,
        ),
    ]

    result = dashboard._local_briefing_from_articles(
        articles,
        hours=24,
        cfg=_cfg(),
    )

    assert [theme["theme"] for theme in result["themes"]] == [
        "国内货币政策与流动性"
    ]
    assert result["themes"][0]["signal_count"] == 3


def test_broad_macro_tag_without_source_evidence_is_excluded():
    article = _article(
        "中国广电发布新一代家庭机顶盒",
        tag="宏观放水",
    )

    result = dashboard._local_briefing_from_articles(
        [article],
        hours=24,
        cfg=_cfg(),
    )

    assert result["themes"] == []
    assert result["briefing_assembly"]["noise_excluded_count"] == 1


def test_broad_macro_tag_keeps_official_macro_and_reclassifies_source_topics():
    cfg = _cfg()
    assert dashboard._is_briefing_noise(
        _article("国常会部署稳增长政策组合", tag="宏观宽松+制度红利"),
        cfg=cfg,
    ) is False
    assert dashboard._briefing_theme_for_article(
        _article("国常会部署稳增长政策组合", tag="宏观宽松+制度红利"),
        cfg=cfg,
    ) == "国内宏观政策"
    assert dashboard._briefing_theme_for_article(
        _article("财政部出台新一轮财政政策", tag="宏观政策/流动性"),
        cfg=cfg,
    ) == "国内宏观政策"
    assert dashboard._briefing_theme_for_article(
        _article("AI服务器订单显著上修", tag="宏观放水"),
        cfg=cfg,
    ) == "AI 算力链"
    assert dashboard._briefing_theme_for_article(
        _article("国际金价创历史新高", tag="宏观放水"),
        cfg=cfg,
    ) == "黄金与贵金属"


def test_roundups_are_excluded_without_hiding_a_single_event_morning_update():
    roundup_articles = [
        _article(
            "早报（08.03）| 又TACO了，黄金与美股齐涨",
            summary="美联储政策、原油价格与人工智能公司消息汇总",
            index=1,
        ),
        _article("华尔街见闻早餐FM-Radio | 2026年8月3日", index=2),
        _article(
            "财联社8月3日早间新闻精选",
            summary="财政政策、央行操作、黄金和AI产业动态",
            index=3,
        ),
        _article("盘前必读：今日影响市场的十大消息", index=4),
    ]
    for article in roundup_articles:
        assert dashboard._is_briefing_noise(
            article,
            cfg=_cfg(),
        ) is True

    single_events = [
        _article("晨报：央行开展5000亿元逆回购", tag="国内货币政策"),
        _article("晨报：央行连续增持黄金储备", tag="黄金与贵金属"),
        _article("盘前必读：央行开展5000亿元逆回购", tag="国内货币政策"),
        _article("新闻精选：国务院发布单项稳增长政策", tag="宏观政策"),
    ]
    assert all(
        dashboard._is_briefing_noise(article, cfg=_cfg()) is False
        for article in single_events
    )


def test_local_why_now_is_title_anchored_and_does_not_repeat_score_rationale():
    title = "央行开展5000亿元逆回购维护月末流动性"
    result = dashboard._local_briefing_from_articles(
        [_article(title, tag="宏观放水")],
        hours=24,
        cfg=_cfg(),
    )
    theme = result["themes"][0]

    assert theme["background"] == ""
    assert title in theme["why_it_matters"]
    assert "近24小时" in theme["why_it_matters"]
    assert theme["why_it_matters"] != theme["insight"]

    html = render_themes_html(result, 24)
    assert "为什么现在重要：" in html
    assert "评分理由：" not in html
    assert "max-width:960px;margin:0 auto" in html


def test_theme_source_coverage_does_not_confirm_the_top_canonical_event():
    articles = [
        _article(
            f"国常会部署稳增长政策第{index}项",
            tag="宏观政策",
            score=9.5 - index / 10,
            created_at=200 - index,
            index=index,
        )
        for index in range(1, 4)
    ]

    result = dashboard._local_briefing_from_articles(
        articles,
        hours=24,
        cfg=_cfg(),
    )
    theme = result["themes"][0]

    assert theme["source_count"] == 3
    assert theme["top_event_source_count"] == 1
    assert theme["source_confirmation"] == (
        "头部事件仅覆盖 1 个入选发布方；转载谱系未核验；"
        "主题整体覆盖 3 个入选发布方"
    )
    assert "来源" not in theme["insight"]
    assert "来源" not in theme["why_it_matters"]


def test_zero_top_event_publishers_are_not_reported_as_one_publisher():
    top_without_source = {
        "title": "头部事件没有可识别发布方",
        "score": 9.8,
        "created_at": 200,
        "source_keys": [],
    }
    lower_with_source = {
        "title": "次级事件保留一个入选发布方",
        "score": 8.8,
        "created_at": 190,
        "link": "https://publisher.example/article",
    }

    wording = briefing_source_confirmation([top_without_source, lower_with_source])

    assert wording == (
        "头部事件未识别到入选发布方；转载谱系未核验；"
        "主题整体覆盖 1 个入选发布方"
    )
    assert "头部事件仅覆盖 1 个" not in wording


def test_only_top_event_source_keys_can_enable_confirmation_wording():
    top = _article(
        "国常会部署稳增长政策",
        tag="宏观政策",
        score=9.5,
        created_at=200,
        index=1,
    )
    top["source_keys"] = ["source1.example", "confirming-wire"]
    other = _article(
        "财政部发布预算执行报告",
        tag="宏观政策",
        score=8.8,
        created_at=190,
        index=2,
    )

    result = dashboard._local_briefing_from_articles(
        [top, other],
        hours=24,
        cfg=_cfg(),
    )
    theme = result["themes"][0]

    assert theme["source_count"] == 3
    assert theme["top_event_source_count"] == 2
    assert theme["source_confirmation"] == (
        "头部事件覆盖 2 个入选发布方；转载谱系未核验；"
        "主题整体覆盖 3 个入选发布方"
    )


def test_local_market_commentary_rejects_model_free_text_and_precomputed_fields():
    article = _article("普通公司公告", tag="FastPass")
    article.update({
        "logic": "模型声称央行降息并引发A股普跌、黄金和AI共振",
        "summary": "模型摘要建议立即买入黄金与算力股",
        "assets": "模型伪造资产",
        "sectors": "模型伪造板块",
        "watch": "立即买入模型推荐股票",
    })

    result = dashboard._local_briefing_from_articles(
        [article],
        hours=24,
        cfg=_cfg(),
    )
    theme = result["themes"][0]
    commentary = f"{theme['market_impact']} {theme['action']}"

    assert theme["theme"] == "其他高分情报"
    assert theme["market_impact"] == "等待资产价格确认"
    assert "模型伪造" not in commentary
    assert "立即买入" not in commentary
    assert "黄金" not in commentary
    assert "AI" not in commentary


def test_trusted_asset_mapping_ignores_model_tag_direction_and_sentiment():
    article = _article("普通公司发布例行公告", tag="AI算力链")
    article.update({
        "direction": "看多",
        "sentiment": "强烈利好",
        "logic": "模型声称GPU订单爆发",
        "summary": "模型建议买入算力股",
    })

    trusted = map_article_to_assets(article, trusted_only=True)
    untrusted = map_article_to_assets(article)

    assert trusted["mapped_theme"] == "主题"
    assert trusted["asset_classes"] == []
    assert trusted["sectors"] == []
    assert trusted["security_candidates"] == []
    assert untrusted["mapped_theme"] == "AI算力"


@pytest.mark.parametrize(
    "title",
    [
        "日本央行宣布加息",
        "英国央行下调政策利率",
        "欧洲央行维持利率不变",
        "美国财政部公布新一年度财政预算",
        "鲍威尔称政策利率仍需保持限制性",
        "fomc会议纪要显示利率路径仍有分歧",
        "美国就业数据降温，市场加大押注降息",
        "美国通胀回落强化市场降息押注",
    ],
)
def test_source_only_foreign_macro_events_never_enter_domestic_themes(title):
    article = _article(title, tag="宏观政策/流动性")
    article["logic"] = "模型声称这是中国国内货币政策并将直接利好A股"
    article["direction"] = "看多"
    article["sentiment"] = "国内政策利好"

    assert dashboard._briefing_theme_for_article(
        article, cfg=_cfg()
    ) == "海外宏观政策"
    assert dashboard._is_briefing_noise(article, cfg=_cfg()) is True
    mapping = map_article_to_assets(article, trusted_only=True)
    assert mapping["mapped_theme"] != "国内宽信用"
    article["briefing_theme"] = "国内货币政策与流动性"
    _selected, audit = assemble_briefing_candidates(
        [article], _cfg(), limit=1
    )
    assert audit["selected_primary_themes"] == ["海外宏观政策"]


def test_mixed_foreign_macro_and_a_share_title_keeps_explicit_global_identity():
    article = _article("美联储降息可能影响A股估值", tag="宏观政策")

    assert dashboard._briefing_theme_for_article(
        article, cfg=_cfg()
    ) == "海外宏观与市场联动"
    assert dashboard._is_foreign_macro_briefing_noise(article) is False
    assert dashboard._briefing_theme_for_article(
        _article("中国人民银行宣布降准", tag="宏观政策"), cfg=_cfg()
    ) == "国内货币政策与流动性"


def test_full_theme_identity_is_separate_from_the_16_character_display_label():
    prefix = "这是一个用于验证主题显示截断碰撞的共同前缀"
    first_name = f"{prefix}甲"
    second_name = f"{prefix}乙"
    cfg = {
        "briefing_assembly": {
            "enabled": True,
            "max_themes": 4,
            "max_articles_per_theme": 4,
            "max_share_ratio": 1.0,
            "reserved_non_head_slots": 0,
        },
        "briefing_themes": [
            {"name": first_name, "keywords": ["事件甲"]},
            {"name": second_name, "keywords": ["事件乙"]},
        ],
    }

    assert canonical_theme_name(first_name) == first_name
    assert canonical_theme_name(second_name) == second_name
    assert theme_display_name(first_name) == theme_display_name(second_name)
    assert len(theme_display_name(first_name)) == 16
    assert resolve_configured_theme(cfg, first_name).name == first_name
    assert resolve_configured_theme(cfg, second_name).name == second_name
    with pytest.raises(KeyError):
        resolve_configured_theme(cfg, f"{prefix}丙")

    result = dashboard._local_briefing_from_articles(
        [
            _article("事件甲落地", tag=first_name, index=1),
            _article("事件乙落地", tag=second_name, index=2),
        ],
        cfg=cfg,
    )
    assert {theme["theme"] for theme in result["themes"]} == {
        first_name,
        second_name,
    }
    assert len({theme["theme_display_name"] for theme in result["themes"]}) == 1
    html = render_themes_html(result, 24)
    markdown = render_themes_markdown(result, 24)
    rss = render_themes_rss(result, 24)
    assert theme_display_name(first_name) in html
    for full_name in (first_name, second_name):
        assert f"完整主题：{full_name}" in html
        assert f"完整主题：{full_name}" in markdown
        assert f"完整主题：{full_name}" in rss
