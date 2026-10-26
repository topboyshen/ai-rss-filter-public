from core.digest_engine import dedupe_articles, infer_theme_direction, _fallback_aggregation
from core.processor import _similar_article_direction
from utils.helpers import find_similar_article, normalize_direction


def test_infer_theme_direction_uses_sentiment_not_score():
    bearish = [{"title": "[⭐9.0 📉 宏观] 利空冲击", "score": 9.0}]
    bullish = [{"title": "[⭐6.0 📈 政策] 宽松预期", "score": 6.0}]
    mixed = bearish + bullish + [{"title": "中性观察", "score": 8.5}]

    assert infer_theme_direction(bearish) == "看空"
    assert infer_theme_direction(bullish) == "看多"
    assert infer_theme_direction(mixed) == "中性"
    assert infer_theme_direction([{"title": "高分但无方向", "score": 9.5}]) == "中性"


def test_fallback_aggregation_can_be_bearish():
    result = _fallback_aggregation([
        {"title": "[⭐9.0 📉 风险] 外部冲击", "score": 9.0, "tag": "风险", "link": "https://ex/1"},
        {"title": "[⭐8.5 📉 风险] 继续恶化", "score": 8.5, "tag": "风险", "link": "https://ex/2"},
    ])
    assert result["themes"][0]["direction"] == "看空"


def test_direction_normalization_handles_negation_and_transitions():
    assert normalize_direction("📈", text="此前市场看空") == "看多"
    assert normalize_direction(text="市场由看空转为看多") == "看多"
    assert normalize_direction(text="市场由看多转向看空") == "看空"
    assert normalize_direction(text="市场由看多转为不再看空") == "中性"
    assert normalize_direction(text="市场由看空转为不再看多") == "中性"
    assert normalize_direction(text="不看多，等待确认") == "中性"
    assert normalize_direction(text="不看空，等待确认") == "中性"
    assert normalize_direction(text="并不看空，等待确认") == "中性"
    assert normalize_direction(text="市场由看多转为不看空") == "中性"
    assert normalize_direction(text="市场由看空转向并不看多") == "中性"
    assert normalize_direction(text="不再看空，等待确认") == "中性"
    assert normalize_direction(text="看多与看空观点并存") == "中性"


def test_dedupe_keeps_direction_and_scoring_semantics_atomic():
    high_score_bearish = {
        "title": "格隆汇：公司下调全年指引",
        "raw_title": "公司下调全年指引",
        "link": "https://example.com/high",
        "score": 9.0,
        "tag": "风险",
        "logic": "盈利预期下修",
        "summary": "高分看空摘要",
        "direction": "看空",
        "sentiment": "📉",
        "created_at": 100,
    }
    newer_low_score_bullish = {
        "title": "财联社：公司下调全年指引",
        "raw_title": "公司下调全年指引（更新）",
        "link": "https://example.com/new",
        "score": 7.0,
        "tag": "机会",
        "logic": "误判为利好",
        "summary": "低分看多摘要",
        "direction": "看多",
        "sentiment": "📈",
        "created_at": 200,
    }

    for articles in (
        [high_score_bearish, newer_low_score_bullish],
        [newer_low_score_bullish, high_score_bearish],
    ):
        result = dedupe_articles(articles)
        assert len(result) == 1
        merged = result[0]
        assert merged["duplicate_count"] == 2
        assert merged["score"] == 9.0
        assert merged["tag"] == "风险"
        assert merged["logic"] == "盈利预期下修"
        assert merged["summary"] == "高分看空摘要"
        assert merged["direction"] == "看空"
        assert merged["sentiment"] == "📉"
        assert merged["title"] == "格隆汇：公司下调全年指引"
        assert merged["link"] == "https://example.com/new"
        assert merged["created_at"] == 200


def test_dedupe_equal_score_newer_item_replaces_complete_semantics():
    older = {
        "title": "格隆汇：政策预期变化",
        "link": "https://example.com/old",
        "score": 8.0,
        "tag": "机会",
        "logic": "旧看多逻辑",
        "summary": "旧摘要",
        "direction": "看多",
        "sentiment": "📈",
        "created_at": 100,
    }
    newer = {
        "title": "财联社：政策预期变化",
        "link": "https://example.com/new",
        "score": 8.0,
        "tag": "风险",
        "logic": "新看空逻辑",
        "summary": "",
        "direction": "看空",
        "sentiment": "📉",
        "created_at": 200,
    }

    merged = dedupe_articles([older, newer])[0]

    assert merged["title"] == "财联社：政策预期变化"
    assert merged["tag"] == "风险"
    assert merged["logic"] == "新看空逻辑"
    assert merged["summary"] == ""
    assert merged["direction"] == "看空"
    assert merged["sentiment"] == "📉"


def test_direction_is_persisted_as_structured_cache_field():
    from db import database

    database.init_db()
    database.set_cached_entry(
        "https://example.com/a",
        "[⭐9.0 📉 风险] 原始标题",
        "原始标题",
        9.0,
        "风险",
        "风险上升",
        score_path="llm",
        direction="看空",
    )
    with database.get_connection() as conn:
        row = conn.execute(
            "SELECT direction FROM news_cache WHERE link = ?",
            ("https://example.com/a",),
        ).fetchone()
    assert row[0] == "看空"
    recent = next(
        item for item in database.get_recent_entries(hours=1)
        if item[0] == "https://example.com/a"
    )
    assert len(recent) == 7
    assert recent[6] == "看空"
    similar, _ = find_similar_article("原始标题", [recent], threshold=0.5)
    assert similar[6] == "看空"


def test_similar_direction_falls_back_to_title_only_for_legacy_rows():
    bullish_title = "[⭐9.0 📈 机会] 原始标题"
    common = ("https://example.com/a", bullish_title, "原始标题", 9.0, "机会", "逻辑")

    assert _similar_article_direction((*common, "看空")) == "看空"
    assert _similar_article_direction((*common, "")) == "中性"
    assert _similar_article_direction(common) == "看多"


def test_briefing_route_keeps_direction_after_display_title_cleanup(monkeypatch):
    import app as app_module
    import routes.dashboard_routes as dashboard

    row = (
        "https://example.com/a",
        "[⭐9.0 📉 风险] 原始标题",
        "原始标题",
        9.0,
        "风险",
        "风险上升",
        "摘要",
        100,
        "https://feed.example/rss",
        "看空",
    )
    monkeypatch.setattr(dashboard, "_get_briefing_articles", lambda **_kwargs: [row])

    with app_module.app.test_client() as client:
        response = client.get("/briefing-card")

    assert response.status_code == 200
    html = response.get_data(as_text=True)
    assert "看空" in html
    assert "看多" not in html
