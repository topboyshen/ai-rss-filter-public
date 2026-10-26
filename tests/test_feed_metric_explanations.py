import time

import pytest


@pytest.fixture
def feed_metrics_db(tmp_path, monkeypatch):
    import db.database as database
    import routes.dashboard_routes as dashboard_routes
    from core.config import config as core_config

    monkeypatch.setattr(database, "DB_PATH", str(tmp_path / "feed-metrics.db"))
    monkeypatch.setattr(dashboard_routes, "get_connection", database.get_connection)
    monkeypatch.setattr(dashboard_routes, "db_lock", database.db_lock)
    database.init_db()
    cfg = {"threshold": {"pass": 6}, "feeds": []}
    monkeypatch.setattr(dashboard_routes, "_cfg", lambda: cfg)
    monkeypatch.setattr(core_config, "_cfg", cfg)
    return database, dashboard_routes, cfg


def _add_feed(cfg, name="测试源", url="https://example.com/feed", **overrides):
    cfg["feeds"].append({"name": name, "url": url, "enabled": True, **overrides})
    return url


def _insert_news(database, rows):
    now = int(time.time())
    with database.get_connection() as conn:
        offset = conn.execute("SELECT COUNT(*) FROM news_cache").fetchone()[0]
        conn.executemany(
            "INSERT INTO news_cache "
            "(link, title, raw_title, score, tag, logic, created_at, summary, "
            "feed_url, strategy_version, score_path) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            [
                (
                    f"https://example.com/news/{offset + index}",
                    "固定新闻样本",
                    "固定新闻样本",
                    score,
                    "其他",
                    "",
                    now - age_seconds,
                    "",
                    feed_url,
                    "test-strategy",
                    score_path,
                )
                for index, (feed_url, score, score_path, age_seconds) in enumerate(rows)
            ],
        )
        conn.commit()


def test_mixed_paths_are_exclusive_and_keep_existing_formal_eligibility(
    feed_metrics_db,
):
    database, dashboard, cfg = feed_metrics_db
    url = _add_feed(cfg)
    cases = [
        (8, "llm"),
        (6, "stage2"),
        (9, "fast_pass"),
        (7, "radar"),
        (8, "fallback_local"),
        (7, "local_reject"),
        (5, "llm"),
        (4, "stage2"),
        (9, "similarity"),
        (4, "llm_safe_neutral_contract"),
        (9, "stage2_safe_neutral_contract"),
        (4, "llm_safe_neutral_grounding"),
        (9, "stage2_safe_neutral_grounding"),
        (8, "quality_quarantine"),
        (8, "parse_error"),
    ]
    _insert_news(database, [(url, score, path, 60) for score, path in cases])

    rows, details = dashboard._get_feed_health_snapshot()

    # fallback_local is a model-link failure, not 正式达标.
    assert rows[0][6:9] == (15, 5, 6.75)
    assert details[url]["counts"] == {
        "passed": 5,
        "below_threshold": 2,
        "similarity": 1,
        "safe_contract": 2,
        "safe_grounding": 2,
        "model_failure": 1,
        "processing_error": 2,
        "other_excluded": 0,
    }
    assert details[url]["breakdown_text"].count("模型链路失败 1") == 1
    assert details[url]["breakdown_text"].count("条目处理异常 2") == 1
    assert sum(details[url]["counts"].values()) == rows[0][6]
    assert details[url]["rate_text"] == "33.3%"
    assert details[url]["threshold"] == 6
    for label in (
        "正式达标",
        "低于阈值",
        "重复稿",
        "格式/字段中性",
        "来源依据中性",
        "模型链路失败",
        "条目处理异常",
        "其他未计入",
    ):
        assert label in details[url]["breakdown_text"]


def test_no_samples_is_distinct_from_zero_passed_despite_fetch_history(feed_metrics_db):
    database, dashboard, cfg = feed_metrics_db
    empty_url = _add_feed(cfg, "无新样本", "https://example.com/empty")
    low_url = _add_feed(cfg, "低分样本", "https://example.com/low")
    with database.get_connection() as conn:
        conn.execute(
            "INSERT INTO feed_health "
            "(url, name, last_check, success, fail, articles_total, articles_passed) "
            "VALUES (?, ?, ?, ?, ?, ?, ?)",
            (empty_url, "无新样本", int(time.time()), 500, 0, 1000, 300),
        )
        conn.commit()
    _insert_news(database, [(low_url, 5, "llm", 60)])

    rows, details = dashboard._get_feed_health_snapshot()

    assert rows[0][4:8] == (500, 0, 0, 0)
    assert rows[1][6:8] == (1, 0)
    assert details[empty_url]["rate_text"] == "暂无24h样本"
    assert sum(details[empty_url]["counts"].values()) == 0
    assert details[low_url]["rate_text"] == "0.0%"
    assert details[low_url]["counts"]["below_threshold"] == 1


def test_each_feed_uses_aggregation_threshold_not_pass_or_global_threshold(
    feed_metrics_db,
):
    database, dashboard, cfg = feed_metrics_db
    strict_url = _add_feed(
        cfg,
        "独立阈值",
        "https://example.com/strict",
        pass_threshold=4,
        aggregation_threshold=8,
    )
    default_url = _add_feed(cfg, "默认阈值", "https://example.com/default")
    _insert_news(database, [(strict_url, 7, "llm", 60), (default_url, 7, "llm", 60)])

    rows, details = dashboard._get_feed_health_snapshot()

    assert [row[7] for row in rows] == [0, 1]
    assert details[strict_url]["threshold"] == 8
    assert details[strict_url]["counts"]["below_threshold"] == 1
    assert details[default_url]["threshold"] == 6
    assert details[default_url]["counts"]["passed"] == 1


def test_only_controlled_aliases_and_recent_enabled_primary_sources_count(
    feed_metrics_db,
):
    database, dashboard, cfg = feed_metrics_db
    canonical_url = _add_feed(cfg, "当前源", "http://rsshub:1200/jin10")
    other_url = _add_feed(cfg, "另一源", "http://rsshub:1200/other")
    disabled_url = _add_feed(
        cfg, "禁用源", "http://rsshub:1200/disabled", enabled=False
    )
    alias_url = "http://127.0.0.1:1200/jin10"
    cfg["_runtime_feed_url_aliases"] = {canonical_url: [alias_url]}
    _insert_news(
        database,
        [
            (canonical_url, 7, "llm", 60),
            (alias_url, 8, "stage2", 60),
            (canonical_url, 9, "llm", 25 * 3600),
            ("https://unknown-host.example/jin10", 9, "llm", 60),
            ("http://rsshub:1200/jin10/old", 9, "llm", 60),
            (disabled_url, 9, "llm", 60),
            (other_url, 9, "similarity", 60),
        ],
    )

    rows, details = dashboard._get_feed_health_snapshot()

    assert [row[0] for row in rows] == [canonical_url, other_url]
    assert set(details) == {canonical_url, other_url}
    assert rows[0][6:9] == (2, 2, 7.5)
    assert details[canonical_url]["counts"]["passed"] == 2
    assert rows[1][6:9] == (1, 0, None)
    assert details[other_url]["counts"]["similarity"] == 1


@pytest.mark.parametrize(
    ("score_path", "expected_category"),
    [
        (" LLM ", "passed"),
        ("\tStage2\n", "passed"),
        (" Similarity ", "similarity"),
        (" LLM_SAFE_NEUTRAL_CONTRACT ", "safe_contract"),
        (" Stage2_Safe_Neutral_Contract ", "safe_contract"),
        (" LLM_SAFE_NEUTRAL_GROUNDING ", "safe_grounding"),
        (" Stage2_Safe_Neutral_Grounding ", "safe_grounding"),
        ("custom_llm_path", "other_excluded"),
        ("llm_safe_neutral_contract_unknown", "other_excluded"),
        ("", "other_excluded"),
        (None, "other_excluded"),
    ],
)
def test_score_paths_normalize_without_widening_allowed_sets(
    feed_metrics_db, score_path, expected_category
):
    database, dashboard, cfg = feed_metrics_db
    url = _add_feed(cfg)
    _insert_news(database, [(url, 8, score_path, 60)])

    rows, details = dashboard._get_feed_health_snapshot()

    counts = details[url]["counts"]
    assert counts[expected_category] == 1
    assert sum(counts.values()) == 1
    assert rows[0][7] == int(expected_category == "passed")
    with database.get_connection() as conn:
        assert (
            conn.execute("SELECT score_path FROM news_cache").fetchone()[0]
            == score_path
        )


def test_missing_score_is_excluded_and_legacy_twelve_field_rows_stay_compatible(
    feed_metrics_db,
):
    database, dashboard, cfg = feed_metrics_db
    url = _add_feed(cfg)
    _insert_news(database, [(url, None, "llm", 60), (url, 8, "llm", 60)])

    rows, details = dashboard._get_feed_health_snapshot()
    legacy_rows = dashboard._get_health_from_news_cache()

    assert legacy_rows == rows
    assert len(rows[0]) == 12
    assert rows[0][0:2] == (url, "测试源")
    assert rows[0][6:9] == (2, 1, 8.0)
    assert rows[0][9:12] == (None, 0, 0)
    assert details[url]["counts"]["other_excluded"] == 1
    assert details[url]["rate_text"] == "50.0%"


def _insert_news_with_version(database, rows):
    now = int(time.time())
    with database.get_connection() as conn:
        offset = conn.execute("SELECT COUNT(*) FROM news_cache").fetchone()[0]
        conn.executemany(
            "INSERT INTO news_cache "
            "(link, title, raw_title, score, tag, logic, created_at, summary, "
            "feed_url, strategy_version, score_path) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            [
                (
                    f"https://example.com/versioned/{offset + index}",
                    "固定新闻样本",
                    "固定新闻样本",
                    score,
                    "其他",
                    "",
                    now - age_seconds,
                    "",
                    feed_url,
                    version,
                    score_path,
                )
                for index, (feed_url, score, score_path, version, age_seconds) in enumerate(rows)
            ],
        )
        conn.commit()


def _insert_health(database, url, *, empty_checks=0, last_nonempty_at=0, success=1):
    with database.get_connection() as conn:
        conn.execute(
            "INSERT INTO feed_health "
            "(url, name, last_check, success, fail, articles_total, articles_passed, "
            "consecutive_empty_checks, last_nonempty_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (url, "测试源", int(time.time()), success, 0, 0, 0, empty_checks, last_nonempty_at),
        )
        conn.commit()


def test_zero_output_reason_names_empty_source_with_evidence(feed_metrics_db):
    database, dashboard, cfg = feed_metrics_db
    url = _add_feed(cfg)
    _insert_health(database, url, empty_checks=4, last_nonempty_at=int(time.time()) - 7200)

    _rows, details = dashboard._get_feed_health_snapshot()

    assert details[url]["zero_output_reason"] == "近24h无新文章（连续空检查 4 次）"
    assert details[url]["rate_text"] == "暂无24h样本"


def test_zero_output_reason_lists_counts_and_never_guesses(feed_metrics_db):
    database, dashboard, cfg = feed_metrics_db
    url = _add_feed(cfg)
    _insert_news(
        database,
        [
            (url, 5, "llm", 60),
            (url, 4, "stage2", 60),
            (url, 9, "similarity", 60),
            (url, 8, "fallback_local", 60),
            (url, 8, "parse_error", 60),
        ],
    )

    _rows, details = dashboard._get_feed_health_snapshot()

    reason = details[url]["zero_output_reason"]
    assert details[url]["counts"]["passed"] == 0
    for expected in ("未达阈值 2", "重复稿 1", "模型链路失败 1", "条目处理异常 1"):
        assert expected in reason
    assert "原因未完整记录" not in reason


def test_zero_output_reason_is_unknown_without_recorded_counts(feed_metrics_db):
    database, dashboard, cfg = feed_metrics_db
    url = _add_feed(cfg)

    _rows, details = dashboard._get_feed_health_snapshot()

    assert "近24h无新文章" in details[url]["zero_output_reason"]
    assert "原因未完整记录" in details[url]["zero_output_reason"]


def test_zero_output_reason_is_silent_when_something_passed(feed_metrics_db):
    database, dashboard, cfg = feed_metrics_db
    url = _add_feed(cfg)
    _insert_news(database, [(url, 8, "llm", 60)])

    _rows, details = dashboard._get_feed_health_snapshot()

    assert details[url]["zero_output_reason"] == ""


def test_other_scoring_contract_samples_are_excluded_and_reported(feed_metrics_db):
    database, dashboard, cfg = feed_metrics_db
    cfg["scoring_strategy_version"] = "current-contract"
    url = _add_feed(cfg)
    _insert_news_with_version(
        database,
        [
            (url, 5, "llm", "current-contract", 60),
            (url, 9, "llm", "old-contract", 60),
            (url, 4, "llm_safe_neutral_grounding", "old-contract", 60),
        ],
    )

    rows, details = dashboard._get_feed_health_snapshot()

    assert rows[0][6:8] == (1, 0)
    assert details[url]["counts"]["below_threshold"] == 1
    assert details[url]["counts"]["safe_grounding"] == 0
    assert details[url]["other_contract_total"] == 2
    assert "其他评分契约 2 条未计入" in details[url]["zero_output_reason"]
    assert "来源依据中性 1" not in details[url]["breakdown_text"]


def test_old_contract_only_samples_are_not_reported_as_no_new_articles(
    feed_metrics_db,
):
    database, dashboard, cfg = feed_metrics_db
    cfg["scoring_strategy_version"] = "current-contract"
    url = _add_feed(cfg)
    _insert_news_with_version(
        database,
        [(url, 9, "llm", "old-contract", 60)],
    )

    rows, details = dashboard._get_feed_health_snapshot()

    assert rows[0][6:8] == (0, 0)
    reason = details[url]["zero_output_reason"]
    assert "近24h暂无当前契约样本（其他评分契约 1 条未计入）" in reason
    assert "近24h无新文章" not in reason


def test_old_contract_only_keeps_empty_source_streak_separate(feed_metrics_db):
    database, dashboard, cfg = feed_metrics_db
    cfg["scoring_strategy_version"] = "current-contract"
    url = _add_feed(cfg)
    _insert_news_with_version(
        database,
        [(url, 9, "llm", "old-contract", 60)],
    )
    _insert_health(database, url, empty_checks=5, success=0)

    _rows, details = dashboard._get_feed_health_snapshot()

    reason = details[url]["zero_output_reason"]
    assert "近24h暂无当前契约样本（其他评分契约 1 条未计入）" in reason
    assert "空源连击 5 次" in reason
    assert "近24h无新文章" not in reason


def test_zero_passed_does_not_mark_data_degraded_by_itself(feed_metrics_db):
    from utils.helpers import feed_data_health

    database, dashboard, cfg = feed_metrics_db
    url = _add_feed(cfg)
    _insert_news(database, [(url, 5, "llm", 60)])
    _insert_health(database, url, empty_checks=0, success=50)

    rows, _details = dashboard._get_feed_health_snapshot()

    assert rows[0][7] == 0
    assert feed_data_health(rows[0], cfg)["data_degraded"] is False
    assert feed_data_health(rows[0], cfg)["data_state"] == "healthy"
