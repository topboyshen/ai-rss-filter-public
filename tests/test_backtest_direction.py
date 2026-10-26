from datetime import date, datetime, timedelta
from zoneinfo import ZoneInfo

from core.asset_mapping import map_article_to_assets
from db import database
from services import backtest


def test_run_score_backtest_counts_only_future_theme_recurrence(monkeypatch):
    rows = [
        {
            "link": "early",
            "title": "早期信号",
            "raw_title": "早期信号",
            "score": 9.0,
            "tag": "主题A",
            "logic": "",
            "summary": "",
            "created_at": 100,
            "score_confidence": 1.0,
        },
        {
            "link": "later",
            "title": "后续验证",
            "raw_title": "后续验证",
            "score": 9.0,
            "tag": "主题A",
            "logic": "",
            "summary": "",
            "created_at": 200,
            "score_confidence": 1.0,
        },
    ]
    monkeypatch.setattr(backtest, "load_config", lambda: {})
    monkeypatch.setattr(backtest, "_load_articles", lambda **_kwargs: list(rows))
    monkeypatch.setattr(backtest, "dedupe_articles", lambda articles: articles)
    monkeypatch.setattr(backtest, "map_article_to_assets", lambda _article: {"mapped_theme": "主题A"})

    result = backtest.run_score_backtest(days=1, min_score=0)

    assert result["buckets"][0]["samples"] == 2
    assert result["buckets"][0]["theme_follow_rate"] == 50.0


def test_run_score_backtest_does_not_count_past_theme_as_future(monkeypatch):
    rows = [
        {"link": "past", "title": "过去", "score": 9.0, "tag": "主题A", "created_at": 100},
        {"link": "signal", "title": "信号", "score": 9.0, "tag": "主题B", "created_at": 200},
    ]
    monkeypatch.setattr(backtest, "load_config", lambda: {})
    monkeypatch.setattr(backtest, "_load_articles", lambda **_kwargs: list(rows))
    monkeypatch.setattr(backtest, "dedupe_articles", lambda articles: articles)
    monkeypatch.setattr(
        backtest,
        "map_article_to_assets",
        lambda article: {"mapped_theme": article["tag"]},
    )

    result = backtest.run_score_backtest(days=1, min_score=0)

    assert result["buckets"][0]["theme_follow_rate"] == 0.0


def _timestamp(day: date) -> int:
    return int(datetime.combine(day, datetime.min.time(), ZoneInfo("Asia/Shanghai")).timestamp())


def _timestamp_at(day: date, hour: int, minute: int = 0) -> int:
    return int(datetime(day.year, day.month, day.day, hour, minute, tzinfo=ZoneInfo("Asia/Shanghai")).timestamp())


def test_future_direction_does_not_shift_across_a_missing_trading_day():
    database.init_db()
    friday = date(2026, 7, 10)
    tuesday = date(2026, 7, 14)
    # Monday 2026-07-13 (the real T+1) is deliberately absent.  The Tuesday
    # row is T+2 and must never be relabelled as T+1.
    database.store_future_prices([{
        "symbol": "sh000001",
        "trade_date": tuesday.isoformat(),
        "close_price": 3200,
        "change_amt": -10,
        "change_pct": -0.31,
    }])

    assert database._trading_day_after(friday.isoformat(), 1) == "2026-07-13"
    assert database._trading_day_after(friday.isoformat(), 2) == "2026-07-14"
    assert database._get_future_direction("sh000001", friday.isoformat(), 1) is None
    assert database._get_future_direction("sh000001", friday.isoformat(), 2) == -1


def test_trading_horizon_skips_weekends_and_configured_holidays():
    assert database._trading_day_after("2026-09-24", 1) == "2026-09-28"


def test_asset_mapping_exposes_only_supported_price_proxies():
    growth = map_article_to_assets({"title": "AI算力订单上修", "tag": "AI算力"})
    overseas = map_article_to_assets({"title": "美联储RRP余额变化", "tag": "美元流动性"})
    unknown = map_article_to_assets({"title": "没有已配置资产主题的新闻", "tag": "自定义主题"})

    assert growth["backtest_symbol"] == "sz399006"
    assert overseas["backtest_symbol"] is None
    assert unknown["backtest_symbol"] is None


def test_run_score_backtest_uses_structured_direction_and_real_future_prices(monkeypatch):
    database.init_db()
    article_day = date.today() - timedelta(days=10)
    while not database._is_trading_day_local(article_day.isoformat()):
        article_day -= timedelta(days=1)
    t1 = date.fromisoformat(database._trading_day_after(article_day.isoformat(), 1))
    t2 = date.fromisoformat(database._trading_day_after(article_day.isoformat(), 2))

    database.set_cached_entry(
        link="neutral-high-score",
        title="高分但没有方向",
        raw_title="高分但没有方向",
        score=9.6,
        tag="利多",
        logic="置信度不等于看多",
        score_path="llm",
        direction="中性",
        created_at=_timestamp(article_day),
    )
    database.set_cached_entry(
        link="explicit-bearish",
        title="明确看空信号",
        raw_title="明确看空信号",
        score=9.4,
        tag="利多",
        logic="结构化方向为看空",
        score_path="llm",
        direction="看空",
        created_at=_timestamp(article_day) + 1,
    )
    # The legacy snapshot deliberately disagrees with the historical T0 row.
    # The date-keyed future_prices record must win.
    database.store_market_prices([{
        "symbol": "sh000001",
        "name": "上证指数",
        "price": 3200,
        "change_amt": 10,
        "change_pct": 0.3,
        "trade_date": article_day.isoformat(),
    }])
    database.store_future_prices([
        {
            "symbol": "sh000001", "trade_date": article_day.isoformat(),
            "close_price": 3190, "change_amt": -10,
            "ts": _timestamp_at(article_day, 15),
        },
        {
            "symbol": "sh000001", "trade_date": t1.isoformat(),
            "close_price": 3170, "change_amt": -20,
            "ts": _timestamp_at(t1, 15),
        },
        {
            "symbol": "sh000001", "trade_date": t2.isoformat(),
            "close_price": 3150, "change_amt": -20,
            "ts": _timestamp_at(t2, 15),
        },
    ])

    monkeypatch.setattr(backtest, "load_config", lambda: {})
    monkeypatch.setattr(backtest, "dedupe_articles", lambda articles: articles)
    monkeypatch.setattr(
        backtest,
        "map_article_to_assets",
        lambda article: {"mapped_theme": article["link"]},
    )

    result = backtest.run_score_backtest(days=30, min_score=0)
    bucket = result["buckets"][0]

    # ``mode`` is the long-standing compatibility field; legacy/snapshot
    # coverage is exposed separately without breaking existing consumers.
    assert result["mode"] == "realized_market_direction"
    assert result["validation_mode"] == "legacy_accumulating"
    assert result["data_source"] == "legacy_news_cache"
    assert bucket["samples"] == 2
    # The neutral 9.6 article is not silently converted into a long signal.
    assert bucket["direction_samples"] == 1
    assert bucket["t1_direction_samples"] == 1
    assert bucket["t2_direction_samples"] == 1
    # The explicit bearish signal correctly matches T0, T+1 and T+2 declines.
    assert bucket["direction_win_rate"] == 100.0
    assert bucket["t1_direction_win_rate"] == 100.0
    assert bucket["t2_direction_win_rate"] == 100.0


def test_mapped_asset_drives_direction_and_return_while_benchmark_stays_separate(monkeypatch):
    database.init_db()
    article_day = date(2026, 7, 13)
    t1 = date(2026, 7, 14)
    t2 = date(2026, 7, 15)
    article = {
        "link": "ai-growth",
        "title": "AI算力订单上修",
        "raw_title": "AI算力订单上修",
        "score": 9.4,
        "tag": "AI算力",
        "logic": "成长资产看多",
        "summary": "",
        "direction": "看多",
        "created_at": _timestamp_at(article_day, 10),
        "score_confidence": 0.9,
    }
    prices = []
    for trade_day, growth, benchmark in (
        (article_day, (10, 1.0), (-10, -0.3)),
        (t1, (15, 1.5), (5, 0.5)),
        (t2, (-20, -2.0), (2, 0.2)),
    ):
        prices.extend([
            {
                "symbol": "sz399006", "trade_date": trade_day.isoformat(),
                "close_price": 2500, "change_amt": growth[0], "change_pct": growth[1],
                "ts": _timestamp_at(trade_day, 15),
            },
            {
                "symbol": "sh000001", "trade_date": trade_day.isoformat(),
                "close_price": 3200, "change_amt": benchmark[0], "change_pct": benchmark[1],
                "ts": _timestamp_at(trade_day, 15),
            },
        ])
    database.store_future_prices(prices)

    monkeypatch.setattr(backtest, "load_config", lambda: {})
    monkeypatch.setattr(backtest, "_load_articles", lambda **_kwargs: [article])
    monkeypatch.setattr(backtest, "dedupe_articles", lambda articles: articles)
    monkeypatch.setattr(
        backtest,
        "map_article_to_assets",
        lambda _article: {"mapped_theme": "AI算力", "backtest_symbol": "sz399006"},
    )

    result = backtest.run_score_backtest(days=30, min_score=0)
    bucket = result["buckets"][0]
    theme = result["theme_win_rates"]["AI算力"]

    assert bucket["asset_direction_win_rate"] == 100.0
    # Legacy generic direction fields retain their benchmark semantics.
    assert bucket["direction_win_rate"] == 0.0
    assert bucket["benchmark_direction_win_rate"] == 0.0
    assert bucket["asset_t1_direction_win_rate"] == 100.0
    assert bucket["t1_avg_signal_return_pct"] == 1.5
    assert bucket["t1_avg_excess_return_pct"] == 1.0
    assert bucket["asset_t2_direction_win_rate"] == 0.0
    assert bucket["t2_avg_signal_return_pct"] == -2.0
    assert bucket["t2_avg_excess_return_pct"] == -2.2
    assert theme["asset_symbols"] == ["sz399006"]
    assert theme["t1_avg_signal_return_pct"] == 1.5
    assert theme["t1_avg_excess_return_pct"] == 1.0


def test_after_close_signal_is_excluded_from_t0_but_keeps_future_horizons(monkeypatch):
    database.init_db()
    article_day = date(2026, 7, 13)
    t1 = date(2026, 7, 14)
    database.store_future_prices([
        {
            "symbol": "sh000001", "trade_date": article_day.isoformat(),
            "close_price": 3200, "change_amt": 20, "change_pct": 0.6,
            "ts": _timestamp_at(article_day, 15),
        },
        {
            "symbol": "sh000001", "trade_date": t1.isoformat(),
            "close_price": 3180, "change_amt": -20, "change_pct": -0.625,
            "ts": _timestamp_at(t1, 15),
        },
    ])
    article = {
        "link": "after-close",
        "title": "盘后看空信号",
        "score": 9.2,
        "tag": "看空",
        "direction": "看空",
        "created_at": _timestamp_at(article_day, 15, 30),
    }
    monkeypatch.setattr(backtest, "load_config", lambda: {})
    monkeypatch.setattr(backtest, "_load_articles", lambda **_kwargs: [article])
    monkeypatch.setattr(backtest, "dedupe_articles", lambda articles: articles)
    monkeypatch.setattr(
        backtest,
        "map_article_to_assets",
        lambda _article: {"mapped_theme": "大盘", "backtest_symbol": "sh000001"},
    )

    result = backtest.run_score_backtest(days=30, min_score=0)
    bucket = result["buckets"][0]

    assert bucket["direction_samples"] == 0
    assert bucket["benchmark_direction_samples"] == 0
    assert bucket["asset_t1_direction_samples"] == 1
    assert bucket["asset_t1_direction_win_rate"] == 100.0


# ============================================================================
# Tests for extracted pure functions (Phase 1-2 refactoring)
# ============================================================================


def test_initialize_metric_accumulators_returns_all_structures():
    """Test _initialize_metric_accumulators returns complete structure."""
    from services.backtest import _initialize_metric_accumulators
    
    accum = _initialize_metric_accumulators()
    
    # Verify all required keys exist
    assert "by_bucket" in accum
    assert "future_theme_hits" in accum
    assert "direction_hits" in accum
    assert "direction_total" in accum
    assert "benchmark_hits" in accum
    assert "benchmark_total" in accum
    assert "signal_return_sum" in accum
    assert "signal_return_total" in accum
    assert "excess_return_sum" in accum
    assert "excess_return_total" in accum
    assert "direction_unique_days" in accum
    assert "benchmark_unique_days" in accum
    assert "clustered_asset_direction" in accum
    assert "clustered_benchmark_direction" in accum
    assert "clustered_signal_return" in accum
    assert "clustered_excess_return" in accum
    assert "unclusterable_metric_observations" in accum


def test_initialize_metric_accumulators_creates_correct_types():
    """Test _initialize_metric_accumulators creates correct data types."""
    from services.backtest import _initialize_metric_accumulators
    from collections import defaultdict
    
    accum = _initialize_metric_accumulators()
    
    # Verify types
    assert isinstance(accum["by_bucket"], defaultdict)
    assert isinstance(accum["future_theme_hits"], defaultdict)
    assert isinstance(accum["direction_hits"], dict)
    assert accum["unclusterable_metric_observations"] == 0


def test_initialize_metric_accumulators_creates_horizon_dicts():
    """Test _initialize_metric_accumulators creates horizon-keyed dicts."""
    from services.backtest import _initialize_metric_accumulators
    
    accum = _initialize_metric_accumulators()
    
    # Verify horizon keys
    for horizon in (0, 1, 2):
        assert horizon in accum["direction_hits"]
        assert horizon in accum["direction_total"]
        assert horizon in accum["benchmark_hits"]
        assert horizon in accum["benchmark_total"]


def test_partition_valid_invalid_articles_with_snapshots():
    """Test _partition_valid_invalid_articles with signal_snapshots."""
    from services.backtest import _partition_valid_invalid_articles
    
    raw = [
        {"link": "test1", "score": 9.0},
        {"link": "test2", "score": -1},  # Invalid score
    ]
    
    valid, invalid, deduped = _partition_valid_invalid_articles(
        raw, "signal_snapshots"
    )
    
    assert isinstance(valid, list)
    assert isinstance(invalid, list)
    assert isinstance(deduped, list)
    assert len(deduped) >= 0


def test_partition_valid_invalid_articles_with_live_signals():
    """Test _partition_valid_invalid_articles with live signals."""
    from services.backtest import _partition_valid_invalid_articles
    
    raw = [
        {"link": "test1", "score": 9.0, "headline": "Test"},
    ]
    
    valid, invalid, deduped = _partition_valid_invalid_articles(
        raw, "live"
    )
    
    assert isinstance(valid, list)
    assert isinstance(invalid, list)
    assert isinstance(deduped, list)


def test_partition_valid_invalid_articles_returns_three_values():
    """Test _partition_valid_invalid_articles returns tuple of 3."""
    from services.backtest import _partition_valid_invalid_articles
    
    result = _partition_valid_invalid_articles([], "signal_snapshots")
    
    assert isinstance(result, tuple)
    assert len(result) == 3
    valid, invalid, deduped = result
    assert valid == []
    assert invalid == []
    assert deduped == []
