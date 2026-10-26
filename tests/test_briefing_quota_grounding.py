"""Integration contract for Briefing quotas, grounding, and Quant direction.

The tests intentionally use only already-scored in-memory articles.  They do
not call a model, read a production database, or make network requests.
"""

from __future__ import annotations

import copy
import json
from contextlib import contextmanager

from core.briefing_assembly import briefing_policy_fingerprint
from core.digest_engine import (
    _event_evidence_by_id,
    aggregate_themes,
    render_themes_html,
    render_themes_markdown,
    render_themes_rss,
)
from routes import dashboard_routes as dashboard
from services.quant_market_context import _briefing_bias


def _policy_cfg(*, grounding: bool = True, **assembly_overrides):
    assembly = {
        "enabled": True,
        "candidate_multiplier": 4,
        "max_themes": 4,
        "max_articles_per_theme": 3,
        "max_share_ratio": 0.4,
        "reserved_non_head_slots": 1,
        "grounding": {
            "enabled": grounding,
            "min_distinct_hits": 2,
            "require_strong_hit": True,
            "validate_direction": True,
            "on_fail": "deterministic_fallback",
        },
    }
    assembly.update(assembly_overrides)
    return {
        "threshold": {"pass": 7},
        "briefing_assembly": assembly,
        "briefing_themes": [
            {"name": "AI 算力链", "keywords": ["HBM", "GPU"]},
            {"name": "债券与流动性", "keywords": ["国债", "逆回购"]},
            {"name": "黄金与贵金属", "keywords": ["黄金", "贵金属"]},
        ],
    }


def _article(
    title,
    *,
    theme,
    score,
    created_at,
    direction="中性",
    index=0,
):
    return {
        "title": title,
        "raw_title": title,
        "tag": theme,
        "briefing_theme": theme,
        "score": score,
        "created_at": created_at,
        "direction": direction,
        "logic": "",
        "summary": "",
        "link": f"https://source{index}.example/news/{index}",
        "feed_url": f"https://source{index}.example/rss",
    }


class _Backend:
    name = "briefing-test"
    timeout = 1

    def is_healthy(self):
        return True

    def record_fail(self):
        raise AssertionError("the deterministic fake backend must not fail")


class _Scorer:
    backends = [_Backend()]

    def __init__(self, payload):
        self.payload = payload
        self.messages = None

    def _call_llm(
        self,
        backend,
        messages,
        max_tokens,
        timeout=None,
        purpose="score",
        cfg=None,
    ):
        assert backend.name == "briefing-test"
        assert purpose == "digest"
        self.messages = messages
        return json.dumps(self.payload, ensure_ascii=False)


def _llm_payload(themes):
    return {
        "themes": themes,
        "overall_assessment": "模型整体判断",
        "market_impact": "模型市场影响",
        "background": "模型背景",
        "why_it_matters": "模型重要性",
        "risk_alert": "",
        "action_hint": "模型操作建议",
        "confidence": 0.91,
    }


def test_local_briefing_quota_reaches_candidates_behind_dominant_theme():
    cfg = _policy_cfg()
    articles = [
        _article(
            f"HBM存储产业第{i}批扩产项目落地",
            theme="AI 算力链",
            score=9.9 - i * 0.05,
            created_at=500 - i,
            direction="看多",
            index=i,
        )
        for i in range(10)
    ]
    # This lower-scored non-head candidate deliberately sits behind all ten
    # dominant-theme rows in rank order.
    articles.append(_article(
        "国债流动性观察",
        theme="债券与流动性",
        score=8.4,
        created_at=300,
        index=20,
    ))

    result = dashboard._local_briefing_from_articles(
        articles,
        hours=48,
        limit=12,
        cfg=cfg,
    )

    audit = result["briefing_assembly"]
    assert result["article_count"] == 4
    assert audit["selected_count"] == 4
    assert audit["theme_counts"]["AI 算力链"] == 3
    assert audit["theme_counts"]["债券与流动性"] == 1
    assert audit["share_basis"] == 11
    assert audit["share_cap"] == 4
    assert audit["effective_reserved_non_head_slots"] == 1
    assert {theme["theme"] for theme in result["themes"]} == {
        "AI 算力链",
        "债券与流动性",
    }
    # Quota exclusion is presentation audit data, not a scoring rejection.
    assert result["quota_suppressed_count"] == len(articles) - 4
    assert audit["scope"] == "briefing_presentation_only"
    audit_html = render_themes_html(result, 48)
    assert "候选 11 · 入选 4 · 配额剔除 7" in audit_html


def test_llm_unsupported_facts_fall_back_without_losing_theme_contract():
    cfg = _policy_cfg(max_share_ratio=1.0)
    article = _article(
        "NVIDIA签署500亿美元数据中心租赁协议",
        theme="AI 算力链",
        score=9.2,
        created_at=100,
        direction="看多",
        index=1,
    )
    hallucinated = {
        "insight": "AMD宣布999亿美元收购英特尔",
        "why_it_matters": "AMD的999亿美元交易将改变芯片格局",
        "market_impact": "英特尔将因999亿美元交易上涨20%",
        "action": "立即买入AMD等待999亿美元交易完成",
    }
    model_theme = {
        "theme_id": "t1",
        "theme": "AI 算力链",
        "evidence_article_ids": ["a1"],
        "direction": "看空",
        "importance": 10,
        "background": "模型背景",
        **hallucinated,
    }

    result = aggregate_themes(
        [article],
        scorer=_Scorer(_llm_payload([model_theme])),
        period_hours=48,
        cfg=cfg,
        limit=1,
    )

    theme = result["themes"][0]
    assert result["source"] == "llm"
    assert theme["theme"] == "AI 算力链"
    assert theme["direction"] == "看多"
    assert theme["evidence_article_ids"] == ["a1"]
    assert [item["link"] for item in theme["articles"]] == [article["link"]]
    assert theme["articles"][0]["briefing_article_id"] == "a1"
    assert result["articles"][0]["briefing_article_id"] == "a1"
    assert result["articles"][0]["link"] == article["link"]
    for field, unsupported in hallucinated.items():
        assert theme[field]
        assert theme[field] != unsupported
        expected_status = "local_derived" if field == "action" else "fallback"
        assert theme["grounding"]["fields"][field]["status"] == expected_status
    assert theme["grounding"]["status"] == "fallback"
    public_json = json.dumps(result, ensure_ascii=False)
    assert "999亿美元" not in public_json
    assert "AMD" not in public_json
    assert "收购英特尔" not in public_json


def test_evidence_ids_cannot_cross_themes_or_inject_model_links():
    cfg = _policy_cfg(max_share_ratio=1.0)
    liquidity = _article(
        "央行开展100亿元逆回购操作",
        theme="债券与流动性",
        score=9.4,
        created_at=200,
        direction="中性",
        index=1,
    )
    ai = _article(
        "NVIDIA签署500亿美元数据中心租赁协议",
        theme="AI 算力链",
        score=9.2,
        created_at=190,
        direction="看多",
        index=2,
    )
    crossed_claim = "NVIDIA签署500亿美元数据中心租赁协议"
    payload = _llm_payload([
        {
            "theme_id": "t1",
            "theme": "债券与流动性",
            "evidence_article_ids": ["a2", "forged"],
            "direction": "看多",
            "importance": 9.9,
            "insight": crossed_claim,
            "why_it_matters": crossed_claim,
            "market_impact": crossed_claim,
            "action": crossed_claim,
            "articles": [{"link": "https://evil.example/forged"}],
        },
        {
            "theme_id": "t2",
            "theme": "AI 算力链",
            "evidence_article_ids": ["a2"],
            "direction": "看空",
            "importance": 9.1,
            "insight": crossed_claim,
            "why_it_matters": crossed_claim,
            "market_impact": crossed_claim,
            "action": crossed_claim,
        },
    ])

    result = aggregate_themes(
        [liquidity, ai],
        scorer=_Scorer(payload),
        cfg=cfg,
        limit=2,
    )
    themes = {theme["theme"]: theme for theme in result["themes"]}
    liquidity_theme = themes["债券与流动性"]
    ai_theme = themes["AI 算力链"]

    assert liquidity_theme["evidence_article_ids"] == []
    assert liquidity_theme["grounding"]["status"] == "fallback"
    assert liquidity_theme["insight"] != crossed_claim
    assert [item["link"] for item in liquidity_theme["articles"]] == [liquidity["link"]]
    assert not any(
        item["link"] == "https://evil.example/forged"
        for theme in result["themes"]
        for item in theme["articles"]
    )
    assert ai_theme["evidence_article_ids"] == ["a2"]
    assert ai_theme["grounding"]["status"] == "fallback"
    assert ai_theme["insight"] != crossed_claim
    assert ai_theme["grounding"]["fields"]["action"]["status"] == "local_derived"
    assert ai_theme["action"] != crossed_claim
    assert [item["link"] for item in ai_theme["articles"]] == [ai["link"]]


def test_event_grounding_reads_source_headline_not_ai_summary(monkeypatch):
    queries = []

    class Cursor:
        def __init__(self, rows):
            self.rows = rows

        def fetchone(self):
            return self.rows[0] if self.rows else None

        def fetchall(self):
            return list(self.rows)

    class Connection:
        def execute(self, sql, params=()):
            queries.append(sql)
            if "sqlite_master" in sql:
                return Cursor([(1,)])
            assert "SELECT event_id, snapshot_text " in sql
            assert "source_type = 'article'" in sql
            assert "claim_key GLOB 'article_snapshot:*'" in sql
            assert "stance = 'supports'" in sql
            assert "excerpt" not in sql
            assert "claim_text" not in sql
            return Cursor([(
                "event-1",
                "来源原标题包含100亿元逆回购\n\nAI摘要声称999亿元",
            )])

    @contextmanager
    def fake_connection():
        yield Connection()

    import db.database as database

    monkeypatch.setattr(database, "get_connection", fake_connection)
    evidence = _event_evidence_by_id([{"event_id": "event-1"}])

    assert evidence == {"event-1": ["来源原标题包含100亿元逆回购"]}
    assert len(queries) == 2


def test_article_consensus_overrides_llm_direction_for_quant_bias():
    cfg = _policy_cfg(max_share_ratio=1.0)
    articles = [
        _article(
            "GPU服务器订单上修",
            theme="AI 算力链",
            score=9.3,
            created_at=300,
            direction="看多",
            index=1,
        ),
        _article(
            "HBM产能利用率提升",
            theme="AI 算力链",
            score=9.1,
            created_at=290,
            direction="看多",
            index=2,
        ),
        _article(
            "算力链短期库存承压",
            theme="AI 算力链",
            score=8.8,
            created_at=280,
            direction="看空",
            index=3,
        ),
    ]
    payload = _llm_payload([{
        "theme_id": "t1",
        "theme": "AI 算力链",
        "evidence_article_ids": ["a1"],
        "direction": "看空",
        "importance": 9.9,
        "insight": "模型无锚点判断",
        "why_it_matters": "模型无锚点说明",
        "market_impact": "模型无锚点影响",
        "action": "模型无锚点建议",
    }])

    result = aggregate_themes(
        articles,
        scorer=_Scorer(payload),
        cfg=cfg,
        limit=3,
    )

    theme = result["themes"][0]
    assert theme["direction"] == "看多"
    assert theme["direction_validation"] == {
        "status": "validated",
        "model_direction": "看空",
        "evidence_direction": "看多",
    }
    assert _briefing_bias(result)["value"] == "bullish"


def test_disabled_grounding_keeps_constrained_direction_but_not_model_prose():
    cfg = _policy_cfg(grounding=False, max_share_ratio=1.0)
    article = _article(
        "GPU服务器订单上修",
        theme="AI 算力链",
        score=9.0,
        created_at=100,
        direction="看多",
        index=1,
    )
    commentary = {
        "insight": "原有模型洞察",
        "why_it_matters": "原有重要性说明",
        "market_impact": "原有市场影响",
        "action": "原有操作关注点",
    }
    payload = _llm_payload([{
        "theme_id": "t1",
        "theme": "AI 算力链",
        "evidence_article_ids": ["a1"],
        "direction": "看空",
        "importance": 8.6,
        "background": "原有背景",
        **commentary,
    }])
    payload["data_as_of"] = "2099-01-01T00:00:00"
    payload["generated_at"] = "2099-01-01T00:00:00"

    result = aggregate_themes(
        [article],
        scorer=_Scorer(payload),
        cfg=cfg,
        limit=1,
    )

    theme = result["themes"][0]
    assert all(theme[field] for field in commentary)
    assert all(theme[field] != value for field, value in commentary.items())
    assert theme["background"] == ""
    assert theme["source_confirmation"] == (
        "头部事件仅覆盖 1 个入选发布方；转载谱系未核验"
    )
    assert theme["direction"] == "看空"
    assert theme["direction_validation"] == {
        "status": "disabled",
        "model_direction": "看空",
        "evidence_direction": "看多",
    }
    assert "grounding" not in theme
    assert not result["data_as_of"].startswith("2099")
    assert not result["generated_at"].startswith("2099")
    assert "事实锚定已关闭" in render_themes_html(result, 48)


def test_assembly_kill_switch_restores_legacy_prompt_limit_and_shape():
    cfg = {"briefing_assembly": {"enabled": False}}
    themes = [
        {
            "theme": f"主题{i}",
            "direction": "中性",
            "importance": 8,
            "insight": "旧路径洞察",
            "why_it_matters": "旧路径说明",
            "market_impact": "旧路径影响",
            "action": "旧路径关注",
            "grounding": {"status": "model-injected"},
            "direction_validation": {"status": "model-injected"},
        }
        for i in range(6)
    ]
    scorer = _Scorer(_llm_payload(themes))
    articles = [
        _article(
            f"普通标题{i}",
            theme=f"主题{i}",
            score=9 - i / 10,
            created_at=100 - i,
            index=i,
        )
        for i in range(6)
    ]

    result = aggregate_themes(articles, scorer=scorer, cfg=cfg, limit=1)

    assert len(result["themes"]) == 4
    assert "顶级 A 股宏观策略师" in scorer.messages[0]["content"]
    assert "theme_id" not in scorer.messages[0]["content"]
    assert "文章总数：6篇" in scorer.messages[1]["content"]
    assert "briefing_assembly" not in result
    assert "briefing_policy_fingerprint" not in result
    assert all("theme_id" not in theme for theme in result["themes"])
    assert all("evidence_article_ids" not in theme for theme in result["themes"])
    assert all("grounding" not in theme for theme in result["themes"])
    assert all("direction_validation" not in theme for theme in result["themes"])


def test_assembly_kill_switch_restores_legacy_fallback_and_local_shape():
    cfg = {"briefing_assembly": {"enabled": False, "max_themes": 1}}
    articles = [
        _article(
            f"普通标题{i}",
            theme="普通主题",
            score=8 + i,
            created_at=100 + i,
            index=i,
        )
        for i in range(2)
    ]

    local = dashboard._local_briefing_from_articles(
        articles,
        hours=48,
        limit=2,
        cfg=cfg,
    )
    fallback = aggregate_themes(
        articles,
        scorer=type("NoBackendScorer", (), {"backends": []})(),
        cfg=cfg,
        limit=1,
    )

    for key in (
        "briefing_assembly",
        "briefing_policy_fingerprint",
        "briefing_input_count",
        "candidate_count",
        "quota_suppressed_count",
        "source",
        "articles",
    ):
        assert key not in local
    assert "grounding" not in local["themes"][0]
    assert "direction_validation" not in local["themes"][0]
    assert "briefing_theme" not in local["themes"][0]["articles"][0]
    assert "briefing_article_id" not in local["themes"][0]["articles"][0]
    assert len(dashboard._local_briefing_from_articles(
        [
            _article(
                f"主题{i}标题",
                theme=f"主题{i}",
                score=9,
                created_at=100 - i,
                index=20 + i,
            )
            for i in range(5)
        ],
        hours=48,
        limit=5,
        cfg=cfg,
    )["themes"]) == 4
    assert fallback["overall_assessment"] == "近期共 2 篇高分情报，降级聚合展示"
    assert fallback["themes"][0]["importance"] == 8.5
    assert "briefing_assembly" not in fallback
    assert "briefing_policy_fingerprint" not in fallback


def test_structural_theme_resolution_ignores_model_only_text():
    article = _article(
        "普通公司公告",
        theme="FastPass",
        score=9,
        created_at=100,
        index=1,
    )
    article.update({
        "logic": "A股三大指数集体收跌，个股普跌",
        "summary": "国务院印发扩大消费规划",
    })

    assert dashboard._briefing_theme_for_article(
        article,
        cfg=_policy_cfg(),
    ) == "其他高分情报"


def test_local_and_llm_paths_share_theme_order_and_representative_links():
    cfg = _policy_cfg(
        grounding=False,
        max_articles_per_theme=3,
        max_share_ratio=1.0,
    )
    articles = []
    for index, score in enumerate((10.0, 7.0, 7.0), 1):
        article = _article(
            f"A主题事件{index}",
            theme="A主题",
            score=score,
            created_at=200 - index,
            index=index,
        )
        article["link"] = f"https://same-source.example/news/{index}"
        article["feed_url"] = "https://same-source.example/rss"
        articles.append(article)
    for index in range(3):
        article = _article(
            f"B主题事件{index}",
            theme="B主题",
            score=9.8,
            created_at=190 - index,
            index=10 + index,
        )
        articles.append(article)

    local = dashboard._local_briefing_from_articles(
        articles,
        hours=48,
        limit=6,
        cfg=cfg,
    )
    model = _llm_payload([
        {
            "theme_id": "t1",
            "theme": "A主题",
            "direction": "中性",
            "importance": 1,
            "insight": "A",
            "why_it_matters": "A",
            "market_impact": "A",
            "action": "A",
        },
        {
            "theme_id": "t2",
            "theme": "B主题",
            "direction": "中性",
            "importance": 1,
            "insight": "B",
            "why_it_matters": "B",
            "market_impact": "B",
            "action": "B",
        },
    ])
    llm = aggregate_themes(
        local["articles"],
        scorer=_Scorer(model),
        cfg=cfg,
        limit=6,
        assembly_audit=local["briefing_assembly"],
    )

    assert [theme["theme"] for theme in llm["themes"]] == [
        theme["theme"] for theme in local["themes"]
    ]
    assert [
        [article["link"] for article in theme["articles"]]
        for theme in llm["themes"]
    ] == [
        [article["link"] for article in theme["articles"]]
        for theme in local["themes"]
    ]
    article_ids = {
        article["briefing_article_id"] for article in llm["articles"]
    }
    assert all(
        set(theme["evidence_article_ids"]).issubset(article_ids)
        for theme in llm["themes"]
    )


def test_grounding_fallback_uses_full_theme_not_source_deduped_display_subset():
    cfg = _policy_cfg(max_articles_per_theme=3, max_share_ratio=1.0)
    articles = []
    for index in range(3):
        article = _article(
            f"同源独立事件{index}",
            theme="同源主题",
            score=9 - index / 10,
            created_at=100 - index,
            index=index,
        )
        article["link"] = f"https://same-source.example/news/{index}"
        article["feed_url"] = "https://same-source.example/rss"
        articles.append(article)
    local = dashboard._local_briefing_from_articles(
        articles,
        hours=48,
        limit=3,
        cfg=cfg,
    )
    payload = _llm_payload([{
        "theme_id": "t1",
        "theme": "同源主题",
        "evidence_article_ids": ["a1"],
        "direction": "中性",
        "importance": 9,
        "insight": "无来源支持的模型结论999%",
        "why_it_matters": "无来源支持的重要性999%",
        "market_impact": "无来源支持的影响999%",
        "action": "立即买入",
    }])

    llm = aggregate_themes(
        local["articles"],
        scorer=_Scorer(payload),
        cfg=cfg,
        limit=3,
        assembly_audit=local["briefing_assembly"],
    )

    assert len(local["themes"][0]["articles"]) == 1
    assert len(llm["themes"][0]["articles"]) == 1
    assert llm["themes"][0]["insight"] == local["themes"][0]["insight"]
    assert "3 个去重事件、3 条更新" in llm["themes"][0]["insight"]
    assert llm["themes"][0]["source_confirmation"] == (
        "累计 3 条同发布方入选更新；转载谱系未核验"
    )
    assert "_theme_articles" not in json.dumps(llm, ensure_ascii=False)


def test_model_cannot_promote_theme_source_coverage_to_event_confirmation():
    cfg = _policy_cfg(
        grounding=False,
        max_articles_per_theme=3,
        max_share_ratio=1.0,
    )
    articles = [
        _article(
            f"同主题独立事件{index}",
            theme="同主题",
            score=9.5 - index / 10,
            created_at=200 - index,
            index=index,
        )
        for index in (1, 2)
    ]
    forged_marker = "XQZ-777：各家发布方已经相互印证"
    payload = _llm_payload([{
        "theme_id": "t1",
        "theme": "同主题",
        "evidence_article_ids": ["a1", "a2"],
        "direction": "中性",
        "importance": 9,
        "insight": forged_marker,
        "why_it_matters": forged_marker,
        "market_impact": forged_marker,
        "action": forged_marker,
        "background": forged_marker,
        "source_confirmation": forged_marker,
    }])
    for field in (
        "overall_assessment",
        "market_impact",
        "background",
        "why_it_matters",
        "risk_alert",
        "action_hint",
    ):
        payload[field] = forged_marker

    result = aggregate_themes(
        articles,
        scorer=_Scorer(payload),
        cfg=cfg,
        limit=2,
    )
    theme = result["themes"][0]

    assert theme["source_count"] == 2
    assert theme["top_event_source_count"] == 1
    assert theme["source_confirmation"] == (
        "头部事件仅覆盖 1 个入选发布方；转载谱系未核验；"
        "主题整体覆盖 2 个入选发布方"
    )
    rendered = render_themes_html(result, 48)
    assert "入选发布方覆盖：" in rendered
    assert "转载谱系未核验" in rendered
    assert "独立来源确认" not in rendered
    assert "来源核验：" not in rendered
    assert theme["background"] == ""
    assert result["background"] == ""
    assert forged_marker not in json.dumps(result, ensure_ascii=False)


def test_briefing_policy_fingerprint_tracks_presentation_policy_only():
    baseline = _policy_cfg()
    score_change = copy.deepcopy(baseline)
    score_change["threshold"]["pass"] = 9
    quota_change = copy.deepcopy(baseline)
    quota_change["briefing_assembly"]["max_articles_per_theme"] = 2
    grounding_change = copy.deepcopy(baseline)
    grounding_change["briefing_assembly"]["grounding"]["min_distinct_hits"] = 3
    overfetch_change = copy.deepcopy(baseline)
    overfetch_change["briefing_assembly"]["candidate_multiplier"] = 5

    baseline_fingerprint = briefing_policy_fingerprint(baseline)
    assert briefing_policy_fingerprint(score_change) == baseline_fingerprint
    assert briefing_policy_fingerprint(quota_change) != baseline_fingerprint
    assert briefing_policy_fingerprint(grounding_change) != baseline_fingerprint
    assert briefing_policy_fingerprint(overfetch_change) != baseline_fingerprint


def test_overfetch_expands_result_pool_without_multiplying_query_scan(monkeypatch):
    cfg = _policy_cfg(candidate_multiplier=4)
    calls = []

    def fake_query(_cfg, **kwargs):
        calls.append(kwargs)
        return []

    import services.intelligence_query as intelligence_query

    monkeypatch.setattr(dashboard, "_cfg", lambda: cfg)
    monkeypatch.setattr(intelligence_query, "query_briefing_tuples", fake_query)

    dashboard._get_briefing_articles(limit=12, hours=48, overfetch=True)
    dashboard._get_briefing_articles(limit=12, hours=48, overfetch=False)

    enlarged, legacy = calls
    assert enlarged["limit"] == 48
    assert enlarged["candidate_multiplier"] == 20
    assert enlarged["limit"] * enlarged["candidate_multiplier"] == 12 * 80
    assert legacy["limit"] == 12
    assert legacy["candidate_multiplier"] == 80


def test_llm_cache_uses_stable_ttl_slot_but_rotates_with_policy(monkeypatch):
    baseline = _policy_cfg()
    monkeypatch.setattr(dashboard, "_cfg", lambda: baseline)
    first = dashboard._briefing_llm_cache_name(
        48, 7, 12, "", [("row-a",)]
    )
    rolling_update = dashboard._briefing_llm_cache_name(
        48, 7, 12, "", [("row-b",)]
    )
    assert rolling_update == first

    changed = copy.deepcopy(baseline)
    changed["briefing_assembly"]["max_articles_per_theme"] = 2
    monkeypatch.setattr(dashboard, "_cfg", lambda: changed)
    assert dashboard._briefing_llm_cache_name(
        48, 7, 12, "", [("row-b",)]
    ) != first


def test_why_now_is_visible_in_public_briefing_renderers():
    result = {
        "briefing_assembly": {"enabled": True},
        "themes": [{
            "theme": "AI 算力链",
            "direction": "看多",
            "importance": 8.8,
            "insight": "来源事实",
            "why_it_matters": "订单变化发生在当前扩产窗口",
            "market_impact": "关注算力链",
            "action": "等待价格确认",
            "articles": [],
        }],
        "overall_assessment": "测试",
        "market_impact": "测试影响",
        "confidence": 0.5,
        "generated_at": "2026-08-03T12:00:00",
    }

    assert "为什么现在重要：订单变化发生在当前扩产窗口" in render_themes_html(result, 48)
    assert "为什么现在重要：订单变化发生在当前扩产窗口" in render_themes_markdown(result, 48)
    assert "为什么现在重要：订单变化发生在当前扩产窗口" in render_themes_rss(result, 48)
