import importlib
import os
import re
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from unittest.mock import MagicMock, patch

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from core.config import load_config
from core.processor import (
    _cap_irrelevant_high_score,
    _is_quiet_mode,
    _match_fast_pass,
    _match_local_reject,
    _should_rescore_similar,
    process_entry,
)
from core.scorer import Scorer, ScoringResult
from utils.helpers import FeedEntry, strategy_cache_key

config_module = importlib.import_module("core.config")


def test_fast_pass_require_and_exclude():
    cfg = {
        "fast_pass": {
            "enabled": True,
            "skip_llm": True,
            "tiers": [
                {
                    "score": 8.2,
                    "tag": "科技主线",
                    "logic": "科技主线上下文命中",
                    "keywords": ["AI"],
                    "require_any": ["算力"],
                    "exclude_keywords": ["培训"],
                    "skip_llm": False,
                }
            ],
        }
    }
    assert _match_fast_pass("AI 培训课程上线", cfg) is None
    assert _match_fast_pass("AI 应用更新", cfg) is None
    hit = _match_fast_pass("AI 算力需求扩张", cfg)
    assert hit[:3] == (8.2, "科技主线", "科技主线上下文命中")
    assert hit[4] is False


def test_local_reject_rules_are_independent_from_local_scoring():
    cfg = {
        "local_reject_rules": {
            "enabled": True,
            "rules": [{"pattern": "业绩预告", "score": 2, "tag": "财报噪音", "logic": "常规财报公告"}],
        },
        "local_scoring": {"enabled": False},
    }
    score, tag, logic, sentiment = _match_local_reject("某公司发布业绩预告", cfg)
    assert score == 2.0
    assert tag == "财报噪音"
    assert sentiment == "⚖️"


def test_local_reject_rules_skip_protected_industry_signals():
    cfg = {
        "local_reject_rules": {
            "enabled": True,
            "protect_keywords": ["订单", "资本开支", "AI"],
            "rules": [{"pattern": "业绩预告|半年报|中报披露|年报披露", "score": 2, "tag": "财报噪音", "logic": "常规财报公告"}],
        }
    }
    assert _match_local_reject("某公司发布业绩预告", cfg)[0] == 2.0
    assert _match_local_reject("某公司发布半年报", cfg)[0] == 2.0
    assert _match_local_reject("某公司中报披露净利润增长", cfg)[0] == 2.0
    assert _match_local_reject("半导体龙头年报披露资本开支高增", cfg) is None
    assert _match_local_reject("AI 公司业绩预告显示订单爆发", cfg) is None


def test_quiet_mode_supports_slot_level_threshold_raise():
    cfg = {
        "quiet_mode": {
            "enabled": True,
            "notify_threshold_raise": 1.0,
            "schedule": [
                {"start": "00:00", "end": "08:30", "notify_threshold_raise": 1.0},
                {"start": "08:30", "end": "09:15", "notify_threshold_raise": 0.3},
            ],
        }
    }
    with patch("core.processor.datetime") as mock_dt:
        mock_dt.now.return_value.hour = 8
        mock_dt.now.return_value.minute = 45
        assert _is_quiet_mode(cfg) == (True, 0.3)


def test_similarity_rescore_only_for_core_sources_and_keywords():
    cfg = {
        "feeds": [{"url": "u", "tier": "primary_news"}, {"url": "n", "tier": "noise_prone"}],
        "similarity_rescore_keywords": ["降准"],
    }
    assert _should_rescore_similar("央行宣布降准", cfg, "u") is True
    assert _should_rescore_similar("普通行业新闻", cfg, "u") is False
    assert _should_rescore_similar("央行宣布降准", cfg, "n") is False


def test_structured_prompt_parse():
    result = "分数: 8.5\n标签: 宏观放水\n逻辑: 流动性宽松利好A股\n情绪: 看多"
    score, tag, logic, sentiment = Scorer._parse_score_result(result)
    assert score == 8.5
    assert tag == "宏观放水"
    assert "流动性" in logic
    assert sentiment == "📈"


def test_strategy_fingerprint_changes_when_scoring_config_changes(tmp_path, monkeypatch):
    config_file = tmp_path / "config.yaml"
    config_file.write_text(
        "threshold:\n  pass: 7\nfast_pass:\n  enabled: true\n  tiers:\n    - keywords: ['降准']\n      score: 9\n",
        encoding="utf-8",
    )
    monkeypatch.setattr(config_module, "CONFIG_PATH", str(config_file))
    first = load_config()["scoring_strategy_version"]
    config_file.write_text(
        "threshold:\n  pass: 8\nfast_pass:\n  enabled: true\n  tiers:\n    - keywords: ['降准']\n      score: 9\n",
        encoding="utf-8",
    )
    second = load_config()["scoring_strategy_version"]
    assert first != second


def test_strategy_fingerprint_changes_when_llm_routing_changes(tmp_path, monkeypatch):
    config_file = tmp_path / "config.yaml"
    monkeypatch.setattr(config_module, "CONFIG_PATH", str(config_file))
    config_file.write_text(
        "llm_routing:\n  stage1: [siliconflow-qwen]\n  stage2: [kimi-code]\n",
        encoding="utf-8",
    )
    first = load_config()["scoring_strategy_version"]
    config_file.write_text(
        "llm_routing:\n  stage1: [siliconflow-qwen]\n  stage2: [siliconflow-qwen]\n",
        encoding="utf-8",
    )
    second = load_config()["scoring_strategy_version"]
    assert first != second


def test_strategy_fingerprint_changes_when_semantic_repair_changes(
    tmp_path, monkeypatch
):
    config_file = tmp_path / "config.yaml"
    monkeypatch.setattr(config_module, "CONFIG_PATH", str(config_file))
    config_file.write_text(
        "llm_semantic_repair:\n  enabled: false\n",
        encoding="utf-8",
    )
    disabled = load_config()["scoring_strategy_version"]
    config_file.write_text(
        "llm_semantic_repair:\n  enabled: true\n",
        encoding="utf-8",
    )
    enabled = load_config()["scoring_strategy_version"]

    assert disabled != enabled


def test_strategy_fingerprint_changes_when_few_shot_config_changes(tmp_path, monkeypatch):
    config_file = tmp_path / "config.yaml"
    monkeypatch.setattr(config_module, "CONFIG_PATH", str(config_file))
    config_file.write_text(
        "few_shot_feedback:\n  enabled: true\n  positive_examples: 3\n",
        encoding="utf-8",
    )
    first = load_config()["scoring_strategy_version"]
    config_file.write_text(
        "few_shot_feedback:\n  enabled: true\n  positive_examples: 4\n",
        encoding="utf-8",
    )
    second = load_config()["scoring_strategy_version"]
    assert first != second


def test_strategy_fingerprint_ignores_top_level_source_provenance(
    tmp_path, monkeypatch
):
    config_file = tmp_path / "config.yaml"
    monkeypatch.setattr(config_module, "CONFIG_PATH", str(config_file))
    config_file.write_text(
        "threshold:\n  pass: 7\n"
        "source_provenance:\n"
        "  rules: []\n",
        encoding="utf-8",
    )
    first = load_config()["scoring_strategy_version"]
    config_file.write_text(
        "threshold:\n  pass: 7\n"
        "source_provenance:\n"
        "  rules:\n"
        "    - publisher_group: issuer\n"
        "      domains: [issuer.example]\n"
        "      source_class: issuer_first_party\n"
        "      first_party_scopes: [issuer]\n",
        encoding="utf-8",
    )
    second = load_config()["scoring_strategy_version"]

    assert first == second


def test_strategy_cache_key_is_isolated_by_strategy_version():
    assert strategy_cache_key("feed-url", {"scoring_strategy_version": "v1"}) != strategy_cache_key(
        "feed-url", {"scoring_strategy_version": "v2"}
    )
    assert strategy_cache_key("feed-url", {"scoring_strategy_version": "v1"}).endswith("#strategy=v1")


def test_llm_score_cache_key_is_context_isolated_and_24_chars():
    scorer = Scorer()
    base = scorer._cache_key(
        "title", "summary", {"scoring_strategy_version": "v1"},
        stage="stage1", prompt_text="prompt-a", few_shot_context="few",
    )
    assert len(base) == 24
    assert base != scorer._cache_key(
        "title", "summary", {"scoring_strategy_version": "v1"},
        stage="stage2", prompt_text="prompt-a", few_shot_context="few",
    )
    assert base != scorer._cache_key(
        "title", "summary", {"scoring_strategy_version": "v2"},
        stage="stage1", prompt_text="prompt-a", few_shot_context="few",
    )
    assert base != scorer._cache_key(
        "title", "summary", {"scoring_strategy_version": "v1"},
        stage="stage1", prompt_text="prompt-b", few_shot_context="few",
    )
    assert base != scorer._cache_key(
        "title", "summary", {"scoring_strategy_version": "v1"},
        stage="stage1", prompt_text="prompt-a", few_shot_context="other",
    )


def test_clear_runtime_cache_empties_score_cache():
    scorer = Scorer()
    scorer._set_cached_score(
        "cache-key", ScoringResult((8.0, "宏观", "logic", "⚖️"), "cache-model")
    )
    assert scorer._get_cached_score("cache-key") is not None
    scorer.clear_runtime_cache()
    assert scorer._get_cached_score("cache-key") is None


def test_feed_threshold_gateway_prefers_feed_over_global():
    from db.database import get_feed_thresholds, pass_threshold_for_feed, notify_threshold_for_feed, aggregation_threshold_for_feed

    cfg = {
        "threshold": {"pass": 7, "notify": 9},
        "feeds": [{
            "url": "http://rsshub:1200/jin10",
            "pass_threshold": 6.0,
            "notify_threshold": 8.0,
            "aggregation_threshold": 6.5,
        }],
        "_runtime_feed_url_aliases": {
            "http://rsshub:1200/jin10": ["http://127.0.0.1:1200/jin10"]
        },
    }
    thresholds = get_feed_thresholds(cfg, "http://127.0.0.1:1200/jin10")
    assert thresholds == {"pass": 6.0, "notify": 8.0, "aggregation": 6.5}
    assert pass_threshold_for_feed(cfg, "http://rsshub:1200/jin10") == 6.0
    assert notify_threshold_for_feed(cfg, "http://rsshub:1200/jin10") == 8.0
    assert aggregation_threshold_for_feed(cfg, "http://rsshub:1200/jin10") == 6.5


def test_feed_threshold_gateway_aggregation_defaults_to_pass():
    from db.database import get_feed_thresholds

    cfg = {
        "threshold": {"pass": 7, "notify": 9},
        "feeds": [{"url": "http://feed.test/rss", "pass_threshold": 6.2}],
    }
    assert get_feed_thresholds(cfg, "http://feed.test/rss") == {
        "pass": 6.2,
        "notify": 9.0,
        "aggregation": 6.2,
    }


def test_irrelevance_floor_prefers_explicit_feed_setting_over_tier_policy():
    cfg = {
        "irrelevance_policy": {
            "default_floor": 4.0,
            "macro_floor": 6.5,
            "macro_tiers": ["primary_news"],
            "macro_prompts": ["macro"],
        },
        "feeds": [
            {
                "url": "http://rsshub:1200/jin10",
                "tier": "primary_news",
                "prompt": "macro",
                "irrelevance_floor": 6.5,
            },
            {
                "url": "http://rsshub:1200/official",
                "tier": "primary_news",
                "prompt": "macro",
            },
        ],
    }
    capped, _, _ = _cap_irrelevant_high_score(9.0, "无关", "无相关内容", cfg, "http://rsshub:1200/jin10")
    assert capped == 6.5

    fallback, _, _ = _cap_irrelevant_high_score(9.0, "无关", "无相关内容", cfg, "http://rsshub:1200/official")
    assert fallback == 4.0


def test_irrelevance_floor_defaults_to_4_without_policy_or_feed_setting():
    cfg = {"feeds": [{"url": "http://rsshub:1200/official", "tier": "primary_news", "prompt": "macro"}]}
    capped, _, _ = _cap_irrelevant_high_score(9.0, "无关", "无相关内容", cfg, "http://rsshub:1200/official")
    assert capped == 4.0


def test_equivalent_feed_urls_expand_only_controlled_runtime_aliases():
    from db.database import equivalent_feed_urls

    cfg = {
        "feeds": [{"url": "http://rsshub:1200/jin10"}],
        "_runtime_feed_url_aliases": {
            "http://rsshub:1200/jin10": ["http://127.0.0.1:1200/jin10"]
        },
    }
    assert equivalent_feed_urls(cfg, "http://127.0.0.1:1200/jin10") == [
        "http://127.0.0.1:1200/jin10",
        "http://rsshub:1200/jin10",
    ]


def test_cached_rss_query_matches_equivalent_feed_url(tmp_path, monkeypatch):
    import db.database as database

    db_path = tmp_path / "cache.db"
    monkeypatch.setattr(database, "DB_PATH", str(db_path))
    database.init_db()
    now = int(time.time())
    with database.get_connection() as conn:
        conn.execute(
            "INSERT INTO news_cache (link, title, raw_title, score, tag, logic, created_at, summary, feed_url, strategy_version, score_path) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'llm')",
            ("jin10", "title", "raw", 7.0, "tag", "logic", now, "summary", "http://rsshub:1200/jin10", "v"),
        )
        conn.commit()

    cfg = {
        "threshold": {"pass": 7, "notify": 9},
        "scoring_strategy_version": "v",
        "feeds": [{"url": "http://rsshub:1200/jin10", "aggregation_threshold": 6.0}],
        "_runtime_feed_url_aliases": {
            "http://rsshub:1200/jin10": ["http://127.0.0.1:1200/jin10"]
        },
    }
    assert [r[0] for r in database.get_entries_for_cached_rss(cfg, feed_url="http://127.0.0.1:1200/jin10")] == ["jin10"]


def test_runtime_feed_alias_query_is_bidirectional_and_exact(tmp_path, monkeypatch):
    import db.database as database

    db_path = tmp_path / "cache.db"
    monkeypatch.setattr(database, "DB_PATH", str(db_path))
    database.init_db()
    now = int(time.time())
    original = "http://127.0.0.1:1200/wallstreetcn/live"
    runtime = "http://rsshub:1200/wallstreetcn/live"
    evil = "https://evil.example/wallstreetcn/live"
    with database.get_connection() as conn:
        conn.executemany(
            "INSERT INTO news_cache (link, title, raw_title, score, tag, logic, created_at, summary, feed_url, strategy_version, score_path) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'llm')",
            [
                ("original", "title", "raw", 8.0, "tag", "logic", now - 3600, "summary", original, "v"),
                ("evil", "title", "raw", 9.0, "tag", "logic", now, "summary", evil, "v"),
            ] + [
                (
                    f"low-{index}", "low", "low", 2.0, "tag", "logic",
                    now - index, "summary", runtime, "v",
                )
                for index in range(24)
            ],
        )
        conn.commit()

    cfg = {
        "threshold": {"pass": 7, "notify": 9},
        "scoring_strategy_version": "v",
        "feeds": [{"url": runtime, "aggregation_threshold": 6.0}],
        "_runtime_feed_url_aliases": {runtime: [original]},
    }

    assert database.canonical_feed_url(cfg, original) == runtime
    assert database.equivalent_feed_urls(cfg, runtime) == [runtime, original]
    assert database.equivalent_feed_urls(cfg, evil) == [evil]
    assert [
        row[0]
        for row in database.get_entries_for_cached_rss(
            cfg, feed_url=runtime, limit=1
        )
    ] == ["original"]


def test_cache_key_and_feed_limit_use_only_controlled_aliases():
    from utils.helpers import max_entries_for_feed_url, resolve_feed_url_to_cache_key

    runtime = "http://rsshub:1200/wallstreetcn/live"
    original = "http://127.0.0.1:1200/wallstreetcn/live"
    evil = "https://evil.example/wallstreetcn/live"
    cfg = {
        "feeds": [{"url": runtime, "max_entries": 7}],
        "_runtime_feed_url_aliases": {runtime: [original]},
    }

    assert resolve_feed_url_to_cache_key(original, cfg) == runtime
    assert max_entries_for_feed_url(original, cfg) == 7
    assert resolve_feed_url_to_cache_key(evil, cfg) == evil
    assert max_entries_for_feed_url(evil, cfg) is None


def test_strategy_filtered_db_queries_only_return_current_strategy(tmp_path, monkeypatch):
    import db.database as database

    db_path = tmp_path / "cache.db"
    monkeypatch.setattr(database, "DB_PATH", str(db_path))
    database.init_db()
    now = int(time.time())
    with database.get_connection() as conn:
        conn.execute(
            "INSERT INTO news_cache (link, title, raw_title, score, tag, logic, created_at, summary, feed_url, strategy_version, score_path) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'llm')",
            ("old", "old title", "old raw", 9.0, "old", "old logic", now, "old summary", "feed", "old"),
        )
        conn.execute(
            "INSERT INTO news_cache (link, title, raw_title, score, tag, logic, created_at, summary, feed_url, strategy_version, score_path) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'llm')",
            ("new", "new title", "new raw", 8.0, "new", "new logic", now, "new summary", "feed", "new"),
        )
        conn.execute(
            "UPDATE news_cache SET score_path = 'llm' WHERE link IN ('old', 'new')"
        )
        conn.execute(
            "INSERT INTO news_cache "
            "(link, title, raw_title, score, tag, logic, created_at, summary, "
            "feed_url, strategy_version, score_path, direction, score_confidence) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                "safe-neutral",
                "safe-neutral title",
                "safe-neutral raw",
                4.0,
                "其他",
                "来源未显示明确投资催化",
                now,
                "",
                "feed",
                "new",
                "llm_safe_neutral_contract",
                "中性",
                0.0,
            ),
        )
        conn.commit()

    cfg = {"threshold": {"pass": 7}, "similarity_window_hours": 72, "scoring_strategy_version": "new"}
    assert [r[0] for r in database.get_recent_entries(cfg=cfg)] == ["new"]
    assert [r[0] for r in database.get_entries_for_cached_rss(cfg, feed_url="feed")] == ["new"]
    assert [r[0] for r in database.get_high_score_articles_with_summary(cfg=cfg)] == ["new"]


def test_safe_neutral_cache_cannot_be_laundered_into_similarity_export(
    tmp_path, monkeypatch,
):
    import db.database as database

    db_path = tmp_path / "cache.db"
    monkeypatch.setattr(database, "DB_PATH", str(db_path))
    database.init_db()
    now = int(time.time())
    cfg = _quality_gate_process_cfg()
    cfg["similarity_window_hours"] = 72
    with database.get_connection() as conn:
        conn.execute(
            "INSERT INTO news_cache "
            "(link, title, raw_title, score, tag, logic, created_at, summary, "
            "feed_url, strategy_version, score_path, direction, score_confidence) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                "https://example.com/safe-parent",
                "公司日常经营信息",
                "公司日常经营信息",
                4.0,
                "其他",
                "来源未显示明确投资催化",
                now,
                "",
                "u",
                cfg["scoring_strategy_version"],
                "llm_safe_neutral_grounding",
                "中性",
                0.0,
            ),
        )
        conn.commit()

    recent_entries = database.get_recent_entries(cfg=cfg)
    assert recent_entries == []
    scorer = _quality_gate_scorer(
        (8.0, "公司经营", "经营信息由模型独立核验", "⚖️"),
        backend_name="qwen",
    )
    entry = FeedEntry(
        "公司日常经营信息更新",
        "https://example.com/new-independent-score",
    )
    mapping = {
        "mapped_theme": "公司经营",
        "backtest_symbol": None,
        "mapping_confidence": 0.0,
        "mapping_relevance": 0.0,
        "expected_gap": None,
        "security_candidates": [],
        "market_references": [],
    }
    with patch("core.processor.map_article_to_assets", return_value=mapping):
        result, reason = process_entry(
            entry,
            recent_entries,
            "prompt",
            cfg,
            scorer=scorer,
            feed_url="u",
        )

    assert result is entry
    assert reason == "scored"
    scorer.score_stage1.assert_called_once()
    with database.get_connection() as conn:
        cache_path = conn.execute(
            "SELECT score_path FROM news_cache WHERE link = ?", (entry.link,)
        ).fetchone()[0]
        ledger_path = conn.execute(
            "SELECT score_path FROM backtest_signals WHERE link = ?", (entry.link,)
        ).fetchone()[0]
        exported_path = conn.execute(
            "SELECT score_path FROM integration_signal_outbox "
            "WHERE ledger_signal_id = ("
            "SELECT id FROM backtest_signals WHERE link = ?)",
            (entry.link,),
        ).fetchone()[0]
    assert (cache_path, ledger_path, exported_path) == ("llm", "llm", "llm")


@pytest.mark.parametrize(
    "score_path",
    ["parse_error", "quality_quarantine", "similarity", " future_unknown "],
)
def test_nonexportable_cache_cannot_seed_similarity(
    tmp_path, monkeypatch, score_path,
):
    import db.database as database

    db_path = tmp_path / "cache.db"
    monkeypatch.setattr(database, "DB_PATH", str(db_path))
    database.init_db()
    with database.get_connection() as conn:
        conn.execute(
            "INSERT INTO news_cache "
            "(link, title, raw_title, score, tag, logic, created_at, feed_url, "
            "strategy_version, score_path) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                f"https://example.com/source-{score_path.strip()}",
                "apparently usable source",
                "apparently usable source",
                9.0,
                "宏观",
                "apparently usable logic",
                int(time.time()),
                "feed",
                "strategy-v1",
                score_path,
            ),
        )
        conn.commit()

    assert database.get_recent_entries(
        cfg={
            "similarity_window_hours": 72,
            "scoring_strategy_version": "strategy-v1",
        }
    ) == []


def test_read_models_match_manual_prefix_but_similarity_requires_exact_strategy(
    tmp_path, monkeypatch
):
    import db.database as database

    db_path = tmp_path / "cache.db"
    monkeypatch.setattr(database, "DB_PATH", str(db_path))
    database.init_db()
    now = int(time.time())
    with database.get_connection() as conn:
        conn.execute(
            "INSERT INTO news_cache (link, title, raw_title, score, tag, logic, created_at, summary, feed_url, strategy_version, score_path) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'llm')",
            ("v1", "v1 title", "v1 raw", 9.0, "宏观", "logic", now, "sum", "feed", "2026-05-19-v2:oldhash"),
        )
        conn.execute(
            "INSERT INTO news_cache (link, title, raw_title, score, tag, logic, created_at, summary, feed_url, strategy_version, score_path) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'llm')",
            ("legacy", "legacy title", "legacy raw", 8.0, "宏观", "logic", now, "sum", "feed", "legacy-manual"),
        )
        conn.commit()

    cfg = {
        "threshold": {"pass": 7},
        "similarity_window_hours": 72,
        "scoring_strategy_version": "2026-05-19-v2:newhash",
    }
    assert [r[0] for r in database.get_entries_for_cached_rss(cfg, feed_url="feed")] == ["v1"]
    assert database.get_recent_entries(cfg=cfg, feed_url="feed") == []


@pytest.fixture
def no_same_strategy_ledger(monkeypatch):
    lookup = MagicMock(return_value="")
    monkeypatch.setattr(
        "core.processor.DB.get_latest_signal_score_path",
        lookup,
    )
    return lookup


def test_process_entry_ignores_cached_row_from_old_strategy(
    no_same_strategy_ledger,
):
    entry = MagicMock()
    entry.title = "央行宣布降准"
    entry.link = "http://example.com/strategy-cache"
    entry.summary = ""
    cfg = {
        "scoring_strategy_version": "new-strategy",
        "threshold": {"pass": 7, "similarity": 0.6, "notify": 99},
        "_blacklist_re": re.compile("$^"),
        "cache_rejected_entries": True,
        "generate_summary": False,
        "local_reject_rules": {"enabled": False, "rules": []},
        "fast_pass": {
            "enabled": True,
            "skip_llm": True,
            "tiers": [
                {"keywords": ["降准"], "score": 9, "tag": "宏观政策", "logic": "宏观政策命中", "skip_llm": True}
            ],
        },
        "feeds": [{"url": "u", "prompt": "macro", "tier": "primary_news"}],
    }
    old_cached = ("[⭐2 ⚖️ 旧策略] 央行宣布降准", "央行宣布降准", 2.0, "旧策略", "旧策略低分", int(time.time()), "", "old-strategy")
    with patch("core.processor.DB.get_cached_entry", return_value=old_cached), patch("core.processor.DB.record_score"), patch("core.processor.DB.set_cached_entry") as set_cached:
        result, reason = process_entry(entry, [], "prompt", cfg, scorer=MagicMock(backends=[]), feed_url="u")
    assert result is not None
    assert reason == "fast_pass"
    assert set_cached.call_args.kwargs["strategy_version"] == "new-strategy"
    no_same_strategy_ledger.assert_called_once_with(
        entry.link,
        cfg["scoring_strategy_version"],
    )


def test_process_entry_writes_metadata_for_fast_pass(no_same_strategy_ledger):
    entry = MagicMock()
    entry.title = "央行宣布降准"
    entry.link = "http://example.com/fast"
    entry.summary = ""
    cfg = {
        "scoring_strategy_version": "test-strategy",
        "threshold": {"pass": 7, "similarity": 0.6, "notify": 99},
        "_blacklist_re": None,
        "cache_rejected_entries": True,
        "generate_summary": False,
        "fast_pass": {"enabled": True, "skip_llm": True, "tiers": [{"keywords": ["降准"], "score": 9, "tag": "宏观政策", "logic": "宏观政策命中", "skip_llm": True}]},
        "prompts": {"macro": "macro prompt body"},
        "feeds": [{"url": "u", "prompt": "macro", "tier": "primary_news"}],
    }
    with patch("core.processor.DB.get_cached_entry", return_value=None), patch("core.processor.DB.record_score"), patch("core.processor.DB.set_cached_entry") as set_cached:
        result, reason = process_entry(entry, [], "macro prompt body", cfg, scorer=MagicMock(backends=[]), feed_url="u")
    assert result is not None
    assert reason == "fast_pass"
    assert entry.scoring_meta["score_path"] == "fast_pass"
    assert entry.scoring_meta["prompt_key"] == "macro"
    assert entry.scoring_meta["source_tier"] == "primary_news"
    assert set_cached.call_args.kwargs["score_path"] == "fast_pass"
    assert set_cached.call_args.kwargs["strategy_version"] == "test-strategy"
    assert "require_insert" not in set_cached.call_args.kwargs["backtest_signal"]


def _quality_gate_process_cfg():
    return {
        "scoring_strategy_version": "quality-gate-v1",
        "threshold": {"pass": 7, "similarity": 0.6, "notify": 99},
        "_blacklist_re": None,
        "cache_rejected_entries": True,
        "generate_summary": False,
        "local_reject_rules": {"enabled": False, "rules": []},
        "fast_pass": {"enabled": False},
        "two_stage_scoring": {"enabled": False},
        "feeds": [{"url": "u", "prompt": "macro", "tier": "primary_news"}],
    }


def _quality_gate_scorer(result, *, backend_name="qwen"):
    scorer = MagicMock()
    scorer.backends = [MagicMock(model=backend_name, name=backend_name)]
    scorer.last_backend_name = backend_name
    scorer.score_stage1.return_value = result
    return scorer


class _BoundResolutionScorer:
    def __init__(self, stage1, *, stage2=None, model="qwen"):
        self.backends = [MagicMock(model=model, name=model)]
        self.last_backend_name = model
        self._stage1 = stage1
        self._stage2 = stage2
        self.model = model

    def score_stage1_with_identity(self, *_args, **_kwargs):
        values, resolution = self._stage1
        return ScoringResult(values, self.model, resolution)

    def score_stage2_with_identity(self, *_args, **_kwargs):
        if self._stage2 is None:
            raise AssertionError("stage2 was not expected")
        values, resolution = self._stage2
        return ScoringResult(values, self.model, resolution)


def test_process_entry_rescores_instead_of_copying_polluted_similarity(
    no_same_strategy_ledger,
):
    entry = FeedEntry("央行宣布新的流动性安排", "https://example.com/new")
    scorer = _quality_gate_scorer(
        (8.0, "宏观政策", "流动性改善利好权益资产", "📈")
    )
    polluted = (
        "https://example.com/old",
        "old",
        "央行宣布流动性安排",
        9.0,
        "宏观�策",
        "流动性改善",
        "看多",
    )
    mapping = {
        "mapped_theme": "宏观政策",
        "backtest_symbol": None,
        "mapping_confidence": 0.0,
        "mapping_relevance": 0.0,
        "expected_gap": None,
        "security_candidates": [],
        "market_references": [],
    }

    with (
        patch("core.processor.DB.get_cached_entry", return_value=None),
        patch("core.processor.DB.record_score"),
        patch("core.processor.DB.set_cached_entry", return_value=("", 0)) as set_cached,
        patch("core.processor.DB.get_event_identity_for_link", return_value=("", 0)),
        patch("core.processor.find_similar_article", return_value=(polluted, 0.99)),
        patch("core.processor.map_article_to_assets", return_value=mapping),
    ):
        result, reason = process_entry(
            entry, [polluted], "prompt", _quality_gate_process_cfg(),
            scorer=scorer, feed_url="u",
        )

    assert result is entry
    assert reason == "scored"
    scorer.score_stage1.assert_called_once()
    assert set_cached.call_args.args[4:6] == ("宏观政策", "流动性改善利好权益资产")
    assert set_cached.call_args.kwargs["score_path"] == "llm"


def test_process_entry_labels_local_fallback_instead_of_llm_success(
    no_same_strategy_ledger,
):
    entry = FeedEntry("普通市场消息", "https://example.com/fallback")
    scorer = _quality_gate_scorer(
        (4.2, "本地规则", "本地规则评分", "⚖️"),
        backend_name="fallback_local",
    )
    mapping = {
        "mapped_theme": "本地规则",
        "backtest_symbol": None,
        "mapping_confidence": 0.0,
        "mapping_relevance": 0.0,
        "expected_gap": None,
        "security_candidates": [],
        "market_references": [],
    }

    with (
        patch("core.processor.DB.get_cached_entry", return_value=None),
        patch("core.processor.DB.record_score"),
        patch("core.processor.DB.set_cached_entry", return_value=("", 0)) as set_cached,
        patch("core.processor.DB.get_event_identity_for_link", return_value=("", 0)),
        patch("core.processor.find_similar_article", return_value=(None, 0.0)),
        patch("core.processor.map_article_to_assets", return_value=mapping),
    ):
        result, reason = process_entry(
            entry, [], "prompt", _quality_gate_process_cfg(),
            scorer=scorer, feed_url="u",
        )

    assert result is None
    assert reason == "filtered"
    assert entry.scoring_meta["score_path"] == "fallback_local"
    assert entry.scoring_meta["error_type"] == "llm_backends_exhausted"
    assert set_cached.call_args.kwargs["score_path"] == "fallback_local"
    assert set_cached.call_args.kwargs["error_type"] == "llm_backends_exhausted"


@pytest.mark.parametrize(
    ("stage", "resolution", "expected_path"),
    [
        ("stage1", "safe_neutral_initial", "llm_safe_neutral_initial"),
        ("stage2", "safe_neutral_initial", "stage2_safe_neutral_initial"),
        ("stage1", "safe_neutral_contract", "llm_safe_neutral_contract"),
        ("stage2", "safe_neutral_contract", "stage2_safe_neutral_contract"),
        ("stage1", "safe_neutral_grounding", "llm_safe_neutral_grounding"),
        ("stage2", "safe_neutral_grounding", "stage2_safe_neutral_grounding"),
    ],
)
def test_safe_neutral_resolution_freezes_same_cache_and_ledger_path(
    stage,
    resolution,
    expected_path,
    no_same_strategy_ledger,
):
    neutral = (
        (4.0, "其他", "来源未显示明确投资催化", "⚖️"),
        resolution,
    )
    stage1 = (
        neutral
        if stage == "stage1"
        else (
            (7.0, "宏观政策", "流动性信息需要复核", "⚖️"),
            "",
        )
    )
    scorer = _BoundResolutionScorer(
        stage1,
        stage2=neutral if stage == "stage2" else None,
    )
    cfg = _quality_gate_process_cfg()
    if stage == "stage2":
        cfg["two_stage_scoring"] = {
            "enabled": True,
            "stage2_min_score": 6.5,
            "stage2_max_score": 8.5,
        }
    entry = FeedEntry(
        "央行发布日常市场信息",
        f"https://example.com/{expected_path}",
    )
    mapping = {
        "mapped_theme": "其他",
        "backtest_symbol": None,
        "mapping_confidence": 0.0,
        "mapping_relevance": 0.0,
        "expected_gap": None,
        "security_candidates": [],
        "market_references": [],
    }

    with (
        patch("core.processor.DB.get_cached_entry", return_value=None),
        patch("core.processor.DB.record_score"),
        patch("core.processor.DB.set_cached_entry", return_value=("", 0)) as set_cached,
        patch("core.processor.DB.get_event_identity_for_link", return_value=("", 0)),
        patch("core.processor.find_similar_article", return_value=(None, 0.0)),
        patch("core.processor.map_article_to_assets", return_value=mapping),
    ):
        result, reason = process_entry(
            entry, [], "prompt", cfg, scorer=scorer, feed_url="u"
        )

    assert result is None
    assert reason == "filtered"
    assert entry.scoring_meta["score_path"] == expected_path
    assert set_cached.call_args.kwargs["score_path"] == expected_path
    assert (
        set_cached.call_args.kwargs["backtest_signal"]["score_path"]
        == expected_path
    )
    assert set_cached.call_args.kwargs["model_name"] == "qwen"


@pytest.mark.parametrize(
    ("stage", "resolution", "expected_path"),
    [
        ("stage1", "safe_neutral_initial", "llm_safe_neutral_initial"),
        ("stage2", "safe_neutral_initial", "stage2_safe_neutral_initial"),
        ("stage1", "safe_neutral_contract", "llm_safe_neutral_contract"),
        ("stage2", "safe_neutral_grounding", "stage2_safe_neutral_grounding"),
    ],
)
def test_safe_neutral_skips_stage_laundering_hints_and_time_weight(
    monkeypatch,
    stage,
    resolution,
    expected_path,
    no_same_strategy_ledger,
):
    neutral = (
        (4.0, "其他", "来源未显示明确投资催化", "⚖️"),
        resolution,
    )
    stage1 = (
        neutral
        if stage == "stage1"
        else (
            (7.0, "宏观政策", "流动性信息需要复核", "⚖️"),
            "",
        )
    )
    scorer = _BoundResolutionScorer(
        stage1,
        stage2=neutral if stage == "stage2" else None,
    )
    cfg = _quality_gate_process_cfg()
    cfg["two_stage_scoring"] = {
        "enabled": True,
        "stage2_min_score": 6.5,
        "stage2_max_score": 8.5,
        "stage2_on_fast_pass_hint": True,
        "stage2_on_similar_higher_score": True,
    }
    cfg["feeds"][0]["aggregation_threshold"] = 3.0
    entry = FeedEntry(
        "央行发布日常市场信息",
        f"https://example.com/postprocess-{expected_path}",
    )
    similar = (
        "https://example.com/substantive-parent",
        "央行发布日常市场信息进展",
        "央行发布日常市场信息进展",
        9.0,
        "宏观政策",
        "流动性改善利好权益资产",
        "看多",
    )
    mapping = {
        "mapped_theme": "其他",
        "backtest_symbol": None,
        "mapping_confidence": 0.0,
        "mapping_relevance": 0.0,
        "expected_gap": None,
        "security_candidates": [],
        "market_references": [],
    }
    monkeypatch.setattr(
        "core.processor._match_fast_pass",
        lambda *_args, **_kwargs: (
            9.0,
            "FastPass",
            "本地提示",
            "📈",
            False,
        ),
    )
    monkeypatch.setattr(
        "core.processor._should_rescore_similar",
        lambda *_args, **_kwargs: True,
    )
    monkeypatch.setattr(
        "core.processor._get_time_bonus",
        lambda *_args, **_kwargs: (2.0, "adversarial"),
    )

    with (
        patch("core.processor.DB.get_cached_entry", return_value=None),
        patch("core.processor.DB.record_score"),
        patch("core.processor.DB.set_cached_entry", return_value=("", 0)) as set_cached,
        patch("core.processor.DB.get_event_identity_for_link", return_value=("", 0)),
        patch("core.processor.find_similar_article", return_value=(similar, 0.99)),
        patch("core.processor.map_article_to_assets", return_value=mapping),
    ):
        result, reason = process_entry(
            entry, [similar], "prompt", cfg, scorer=scorer, feed_url="u"
        )

    assert result is None
    assert reason == "filtered"
    assert set_cached.call_args.args[3:6] == (
        4.0,
        "其他",
        "来源未显示明确投资催化",
    )
    assert set_cached.call_args.kwargs["direction"] == "中性"
    assert set_cached.call_args.kwargs["score_confidence"] == 0.0
    snapshot = set_cached.call_args.kwargs["backtest_signal"]
    assert snapshot["score_path"] == expected_path
    assert snapshot["score"] == 4.0
    assert snapshot["logic"] == "来源未显示明确投资催化"
    assert snapshot["score_confidence"] == 0.0
    assert snapshot["aggregation_threshold"] == 3.0
    assert snapshot["selected"] is False


def test_safe_neutral_is_never_emitted_or_counted_at_low_threshold(
    no_same_strategy_ledger,
):
    scorer = _BoundResolutionScorer((
        (4.0, "其他", "来源未显示明确投资催化", "⚖️"),
        "safe_neutral_contract",
    ))
    scorer.summarize = MagicMock(return_value="must not run")
    cfg = _quality_gate_process_cfg()
    cfg["threshold"]["pass"] = 3.0
    cfg["threshold"]["notify"] = 3.0
    cfg["generate_summary"] = True
    entry = FeedEntry(
        "公司发布日常经营信息",
        "https://example.com/low-threshold-safe-neutral",
    )
    mapping = {
        "mapped_theme": "其他",
        "backtest_symbol": None,
        "mapping_confidence": 0.0,
        "mapping_relevance": 0.0,
        "expected_gap": None,
        "security_candidates": [],
        "market_references": [],
    }

    with (
        patch("core.processor.DB.get_cached_entry", return_value=None),
        patch("core.processor.DB.record_score") as record_score,
        patch("core.processor.DB.set_cached_entry", return_value=("", 0)) as set_cached,
        patch("core.processor.DB.get_event_identity_for_link", return_value=("", 0)),
        patch("core.processor.find_similar_article", return_value=(None, 0.0)),
        patch("core.processor.map_article_to_assets", return_value=mapping),
        patch("core.processor.send_telegram_notification") as notify,
    ):
        result, reason = process_entry(
            entry, [], "prompt", cfg, scorer=scorer, feed_url="u"
        )

    assert result is None
    assert reason == "filtered"
    assert set_cached.call_args.kwargs["score_path"] == (
        "llm_safe_neutral_contract"
    )
    assert set_cached.call_args.kwargs["backtest_signal"]["selected"] is False
    record_score.assert_not_called()
    scorer.summarize.assert_not_called()
    notify.assert_not_called()


def test_cached_safe_neutral_is_never_reactivated_at_low_threshold():
    cfg = _quality_gate_process_cfg()
    cfg["threshold"]["pass"] = 3.0
    cfg["threshold"]["notify"] = 3.0
    cached = (
        "[⭐4 ⚖️ 其他] 日常经营信息",
        "日常经营信息",
        4.0,
        "其他",
        "来源未显示明确投资催化",
        int(time.time()),
        "",
        cfg["scoring_strategy_version"],
        " LLM_SAFE_NEUTRAL_GROUNDING ",
        0.0,
        " 中性 ",
        "",
        0,
    )
    entry = FeedEntry(
        "日常经营信息",
        "https://example.com/cached-low-threshold-safe-neutral",
    )

    with (
        patch("core.processor.DB.get_cached_entry", return_value=cached),
        patch("core.processor.DB.update_cached_entry_origin") as update_origin,
        patch("core.processor.DB.record_score") as record_score,
        patch("core.processor.send_telegram_notification") as notify,
    ):
        result, reason = process_entry(entry, [], "prompt", cfg, feed_url="u")

    assert result is None
    assert reason == "filtered"
    assert update_origin.call_args.kwargs["backtest_selection"] is None
    record_score.assert_not_called()
    notify.assert_not_called()


def test_empty_semantic_resolution_keeps_existing_stage2_path(
    no_same_strategy_ledger,
):
    scorer = _BoundResolutionScorer(
        ((7.0, "宏观政策", "流动性信息需要复核", "⚖️"), ""),
        stage2=((8.0, "宏观政策", "流动性改善利好权益资产", "📈"), ""),
    )
    cfg = _quality_gate_process_cfg()
    cfg["two_stage_scoring"] = {
        "enabled": True,
        "stage2_min_score": 6.5,
        "stage2_max_score": 8.5,
    }
    entry = FeedEntry(
        "央行发布流动性安排",
        "https://example.com/unchanged-stage2-path",
    )
    mapping = {
        "mapped_theme": "宏观政策",
        "backtest_symbol": None,
        "mapping_confidence": 0.0,
        "mapping_relevance": 0.0,
        "expected_gap": None,
        "security_candidates": [],
        "market_references": [],
    }

    with (
        patch("core.processor.DB.get_cached_entry", return_value=None),
        patch("core.processor.DB.record_score"),
        patch("core.processor.DB.set_cached_entry", return_value=("", 0)) as set_cached,
        patch("core.processor.DB.get_event_identity_for_link", return_value=("", 0)),
        patch("core.processor.find_similar_article", return_value=(None, 0.0)),
        patch("core.processor.map_article_to_assets", return_value=mapping),
    ):
        result, reason = process_entry(
            entry, [], "prompt", cfg, scorer=scorer, feed_url="u"
        )

    assert result is entry
    assert reason == "scored"
    assert entry.scoring_meta["score_path"] == "stage2"
    assert set_cached.call_args.kwargs["score_path"] == "stage2"
    assert set_cached.call_args.kwargs["backtest_signal"]["score_path"] == "stage2"


@pytest.mark.parametrize(
    ("title", "summary"),
    [
        ("央行流动性安�排", "clean source summary"),
        ("央行流动性安排", "source summary contains � damage"),
    ],
)
def test_process_entry_rejects_replacement_character_before_scoring_or_writes(
    title, summary
):
    class SourceEntry:
        def __init__(self):
            self.title = title
            self.link = "https://example.com/invalid-source-text"

        def get(self, key, default=""):
            return summary if key == "summary" else default

    entry = SourceEntry()
    scorer = _quality_gate_scorer(
        (8.0, "宏观政策", "流动性改善利好权益资产", "📈")
    )
    with (
        patch("core.processor.DB.get_cached_entry") as get_cached,
        patch("core.processor.DB.record_score") as record_score,
        patch("core.processor.DB.set_cached_entry") as set_cached,
        patch("core.processor.DB.store_backtest_signal") as store_signal,
    ):
        result, reason = process_entry(
            entry,
            [],
            "prompt",
            _quality_gate_process_cfg(),
            scorer=scorer,
            feed_url="u",
        )

    assert result is None
    assert reason == "invalid_source_text"
    scorer.score_stage1.assert_not_called()
    get_cached.assert_not_called()
    record_score.assert_not_called()
    set_cached.assert_not_called()
    store_signal.assert_not_called()


_QUARANTINE_EVENT_ID = "11111111-1111-4111-8111-111111111111"


def _quarantined_cache_row(*, summary="clean frozen summary"):
    return (
        "央行流动性安排",
        "央行流动性安排",
        -1.0,
        "质量隔离",
        "模型输出含无效字符，等待显式重评分",
        int(time.time()),
        summary,
        "quality-quarantine:ufffd-v1",
        "quality_quarantine",
        0.0,
        "中性",
        _QUARANTINE_EVENT_ID,
        3,
    )


def _explicit_quality_repair_cfg(*, cache_rejected=True):
    cfg = _quality_gate_process_cfg()
    cfg["_allow_quality_quarantine_repair"] = True
    cfg["cache_rejected_entries"] = cache_rejected
    return cfg


def test_normal_refresh_keeps_quality_quarantine_closed():
    entry = FeedEntry("央行流动性安排", "https://example.com/quarantined-default")
    scorer = _quality_gate_scorer(
        (8.0, "宏观政策", "流动性改善利好权益资产", "📈")
    )
    with (
        patch("core.processor.DB.get_cached_entry", return_value=_quarantined_cache_row()),
        patch("core.processor.DB.set_cached_entry") as set_cached,
    ):
        result, reason = process_entry(
            entry,
            [],
            "prompt",
            _quality_gate_process_cfg(),
            scorer=scorer,
            feed_url="u",
        )

    assert result is None
    assert reason == "quality_quarantine"
    scorer.score_stage1.assert_not_called()
    set_cached.assert_not_called()


def test_explicit_quality_repair_passed_path_preserves_identity_and_summary():
    entry = FeedEntry("央行流动性安排", "https://example.com/quarantined-pass")
    scorer = _quality_gate_scorer(
        (8.0, "宏观政策", "流动性改善利好权益资产", "📈")
    )
    mapping = {
        "mapped_theme": "宏观政策",
        "backtest_symbol": None,
        "mapping_confidence": 0.0,
        "mapping_relevance": 0.0,
        "expected_gap": None,
        "security_candidates": [],
        "market_references": [],
    }
    with (
        patch("core.processor.DB.get_cached_entry", return_value=_quarantined_cache_row()),
        patch("core.processor.DB.record_score"),
        patch(
            "core.processor.DB.set_cached_entry",
            return_value=(_QUARANTINE_EVENT_ID, 3),
        ) as set_cached,
        patch("core.processor.find_similar_article", return_value=(None, 0.0)),
        patch("core.processor.map_article_to_assets", return_value=mapping),
    ):
        result, reason = process_entry(
            entry,
            [],
            "prompt",
            _explicit_quality_repair_cfg(),
            scorer=scorer,
            feed_url="u",
        )

    assert result is entry
    assert reason == "scored"
    assert set_cached.call_args.args[6] == "clean frozen summary"
    snapshot = set_cached.call_args.kwargs["backtest_signal"]
    assert snapshot["event_id"] == _QUARANTINE_EVENT_ID
    assert snapshot["event_version"] == 3
    assert snapshot["strategy_version"] == "quality-gate-v1"
    assert snapshot["summary"] == "clean frozen summary"
    assert snapshot["signal_stage"] == "quality_repair"
    assert snapshot["auto_revision"] is True
    assert snapshot["require_insert"] is True


def test_explicit_quality_repair_skips_summary_model_under_production_config():
    entry = FeedEntry(
        "央行流动性安排", "https://example.com/quarantined-production-summary"
    )
    scorer = _quality_gate_scorer(
        (8.0, "宏观政策", "流动性改善利好权益资产", "📈")
    )
    scorer.summarize.side_effect = AssertionError(
        "quality repair must not regenerate the frozen summary"
    )
    mapping = {
        "mapped_theme": "宏观政策",
        "backtest_symbol": None,
        "mapping_confidence": 0.0,
        "mapping_relevance": 0.0,
        "expected_gap": None,
        "security_candidates": [],
        "market_references": [],
    }
    cfg = _explicit_quality_repair_cfg()
    cfg.update({"generate_summary": True, "summary_min_score": 7.0})

    with (
        patch("core.processor.DB.get_cached_entry", return_value=_quarantined_cache_row()),
        patch("core.processor.DB.record_score"),
        patch(
            "core.processor.DB.set_cached_entry",
            return_value=(_QUARANTINE_EVENT_ID, 3),
        ) as set_cached,
        patch("core.processor.find_similar_article", return_value=(None, 0.0)),
        patch("core.processor.map_article_to_assets", return_value=mapping),
    ):
        result, reason = process_entry(
            entry,
            [],
            "prompt",
            cfg,
            scorer=scorer,
            feed_url="u",
        )

    assert result is entry
    assert reason == "scored"
    scorer.summarize.assert_not_called()
    assert set_cached.call_args.args[6] == "clean frozen summary"
    snapshot = set_cached.call_args.kwargs["backtest_signal"]
    assert snapshot["summary"] == "clean frozen summary"
    assert snapshot["signal_stage"] == "quality_repair"
    assert snapshot["require_insert"] is True


def test_explicit_quality_repair_filtered_path_uses_atomic_cache_when_reject_cache_disabled():
    entry = FeedEntry("央行流动性安排", "https://example.com/quarantined-filter")
    scorer = _quality_gate_scorer(
        (4.0, "宏观政策", "影响有限但输出已修复", "⚖️")
    )
    mapping = {
        "mapped_theme": "宏观政策",
        "backtest_symbol": None,
        "mapping_confidence": 0.0,
        "mapping_relevance": 0.0,
        "expected_gap": None,
        "security_candidates": [],
        "market_references": [],
    }
    with (
        patch("core.processor.DB.get_cached_entry", return_value=_quarantined_cache_row()),
        patch("core.processor.DB.record_score"),
        patch(
            "core.processor.DB.set_cached_entry",
            return_value=(_QUARANTINE_EVENT_ID, 3),
        ) as set_cached,
        patch("core.processor.DB.store_backtest_signal") as store_signal,
        patch("core.processor.find_similar_article", return_value=(None, 0.0)),
        patch("core.processor.map_article_to_assets", return_value=mapping),
    ):
        result, reason = process_entry(
            entry,
            [],
            "prompt",
            _explicit_quality_repair_cfg(cache_rejected=False),
            scorer=scorer,
            feed_url="u",
        )

    assert result is None
    assert reason == "filtered"
    set_cached.assert_called_once()
    store_signal.assert_not_called()
    assert set_cached.call_args.args[6] == "clean frozen summary"
    snapshot = set_cached.call_args.kwargs["backtest_signal"]
    assert snapshot["signal_stage"] == "quality_repair"
    assert snapshot["selected"] is False
    assert snapshot["require_insert"] is True


def test_explicit_quality_repair_similarity_path_keeps_copy_semantics():
    entry = FeedEntry("央行流动性安排", "https://example.com/quarantined-copy")
    scorer = _quality_gate_scorer(
        (5.0, "unused", "unused score", "⚖️")
    )
    # Exercise copy provenance using a proven same-title reprint, not an
    # unknown macro rewrite whose identity now requires independent scoring.
    similar = (
        "https://example.com/clean-parent",
        "央行流动性安排",
        "央行流动性安排",
        8.2,
        "宏观政策",
        "流动性改善利好权益资产",
        "看多",
    )
    mapping = {
        "mapped_theme": "宏观政策",
        "backtest_symbol": None,
        "mapping_confidence": 0.0,
        "mapping_relevance": 0.0,
        "expected_gap": None,
        "security_candidates": [],
        "market_references": [],
    }
    with (
        patch("core.processor.DB.get_cached_entry", return_value=_quarantined_cache_row()),
        patch(
            "core.processor.DB.set_cached_entry",
            return_value=(_QUARANTINE_EVENT_ID, 3),
        ) as set_cached,
        patch("core.processor.find_similar_article", return_value=(similar, 0.99)),
        patch("core.processor.map_article_to_assets", return_value=mapping),
    ):
        result, reason = process_entry(
            entry,
            [similar],
            "prompt",
            _explicit_quality_repair_cfg(),
            scorer=scorer,
            feed_url="u",
        )

    assert result is None
    assert reason == "similarity"
    scorer.score_stage1.assert_not_called()
    assert set_cached.call_args.args[6] == "clean frozen summary"
    snapshot = set_cached.call_args.kwargs["backtest_signal"]
    assert snapshot["score_path"] == "similarity"
    assert snapshot["is_similarity_copy"] is True
    assert snapshot["signal_stage"] == "quality_repair"
    assert snapshot["require_insert"] is True


@pytest.mark.parametrize(
    "case",
    ["parse_error", "fallback_local", "fast_pass", "local_reject", "missing_model"],
)
def test_explicit_quality_repair_keeps_invalid_or_non_model_results_quarantined(case):
    entry = FeedEntry(
        "央行流动性安排",
        f"https://example.com/quarantined-{case}",
    )
    valid = (8.0, "宏观政策", "流动性改善利好权益资产", "📈")
    scorer = _quality_gate_scorer(valid)
    fast_pass = None
    local_reject = None
    if case == "parse_error":
        scorer.score_stage1.return_value = (
            -1.0,
            "解析异常",
            "LLM输出格式无法解析",
            "⚖️",
        )
    elif case == "fallback_local":
        scorer = _quality_gate_scorer(
            (4.2, "本地规则", "本地规则评分", "⚖️"),
            backend_name="fallback_local",
        )
    elif case == "fast_pass":
        fast_pass = (*valid, True)
    elif case == "local_reject":
        local_reject = (2.0, "本地拒绝", "本地低价值规则命中", "⚖️")
    elif case == "missing_model":
        scorer = _quality_gate_scorer(valid, backend_name="")
        scorer.backends[0].model = ""
        scorer.backends[0].name = ""

    with (
        patch("core.processor.DB.get_cached_entry", return_value=_quarantined_cache_row()),
        patch(
            "core.processor.DB.get_latest_signal_score_path",
            side_effect=AssertionError(
                "quality repair must not use the ordinary ledger freeze preflight"
            ),
        ),
        patch("core.processor.DB.record_score") as record_score,
        patch("core.processor.DB.set_cached_entry") as set_cached,
        patch("core.processor.find_similar_article", return_value=(None, 0.0)),
        patch("core.processor._match_fast_pass", return_value=fast_pass),
        patch("core.processor._match_local_reject", return_value=local_reject),
    ):
        result, reason = process_entry(
            entry,
            [],
            "prompt",
            _explicit_quality_repair_cfg(),
            scorer=scorer,
            feed_url="u",
        )

    assert result is None
    assert reason == "quality_quarantine"
    set_cached.assert_not_called()
    record_score.assert_not_called()
    if case in {"fast_pass", "local_reject"}:
        scorer.score_stage1.assert_not_called()
    else:
        scorer.score_stage1.assert_called_once()


@pytest.mark.parametrize(
    "resolution",
    ["safe_neutral_initial", "safe_neutral_contract", "safe_neutral_grounding"],
)
def test_explicit_quality_repair_rejects_safe_neutral_resolution(resolution):
    neutral = (
        (4.0, "其他", "来源未显示明确投资催化", "⚖️"),
        resolution,
    )
    scorer = _BoundResolutionScorer(neutral)
    entry = FeedEntry(
        "央行流动性安排",
        "https://example.com/quarantined-safe-neutral-contract",
    )

    with (
        patch("core.processor.DB.get_cached_entry", return_value=_quarantined_cache_row()),
        patch("core.processor.DB.record_score") as record_score,
        patch("core.processor.DB.set_cached_entry") as set_cached,
        patch("core.processor.find_similar_article", return_value=(None, 0.0)),
    ):
        result, reason = process_entry(
            entry,
            [],
            "prompt",
            _explicit_quality_repair_cfg(),
            scorer=scorer,
            feed_url="u",
        )

    assert result is None
    assert reason == "quality_quarantine"
    set_cached.assert_not_called()
    record_score.assert_not_called()


def test_concurrent_process_entries_keep_per_call_model_provenance(
    no_same_strategy_ledger,
):
    barrier = threading.Barrier(2)

    class BoundConcurrentScorer:
        def __init__(self):
            self.backends = [
                MagicMock(model="model-a", name="backend-a"),
                MagicMock(model="model-b", name="backend-b"),
            ]
            self.last_backend_name = ""

        def _bound(self, title):
            model = "model-a" if " A " in title else "model-b"
            self.last_backend_name = model
            barrier.wait(timeout=5)
            if model == "model-a":
                return ScoringResult(
                    (4.0, "其他", "来源未显示明确投资催化", "⚖️"),
                    model,
                    "safe_neutral_contract",
                )
            return ScoringResult(
                (8.0, "macro", "marketimpact", "neutral"), model
            )

        def score_stage1_with_identity(self, title, *_args, **_kwargs):
            return self._bound(title)

        def score_stage1(self, title, *_args, **_kwargs):
            return self._bound(title).values

    scorer = BoundConcurrentScorer()
    entries = [
        FeedEntry("concurrent A macro signal", "https://example.com/concurrent-a"),
        FeedEntry("concurrent B macro signal", "https://example.com/concurrent-b"),
    ]
    mapping = {
        "mapped_theme": "macro",
        "backtest_symbol": None,
        "mapping_confidence": 0.0,
        "mapping_relevance": 0.0,
        "expected_gap": None,
        "security_candidates": [],
        "market_references": [],
    }

    def run(entry):
        return process_entry(
            entry,
            [],
            "prompt",
            _quality_gate_process_cfg(),
            scorer=scorer,
            feed_url="u",
        )

    with (
        patch("core.processor.DB.get_cached_entry", return_value=None),
        patch("core.processor.DB.record_score"),
        patch("core.processor.DB.set_cached_entry", return_value=("", 0)),
        patch("core.processor.DB.get_event_identity_for_link", return_value=("", 0)),
        patch("core.processor.find_similar_article", return_value=(None, 0.0)),
        patch("core.processor.map_article_to_assets", return_value=mapping),
    ):
        with ThreadPoolExecutor(max_workers=2) as pool:
            outcomes = list(pool.map(run, entries))

    assert [reason for _entry, reason in outcomes] == ["filtered", "scored"]
    assert entries[0].scoring_meta["model_name"] == "model-a"
    assert entries[0].scoring_meta["stage1_model"] == "model-a"
    assert (
        entries[0].scoring_meta["score_path"]
        == "llm_safe_neutral_contract"
    )
    assert entries[1].scoring_meta["model_name"] == "model-b"
    assert entries[1].scoring_meta["stage1_model"] == "model-b"
    assert entries[1].scoring_meta["score_path"] == "llm"


def test_fast_pass_hint_records_bounded_tag_without_undefined_tier(
    no_same_strategy_ledger,
):
    entry = FeedEntry("央行宣布降准", "https://example.com/fast-pass-hint")
    cfg = _quality_gate_process_cfg()
    cfg["fast_pass"] = {
        "enabled": True,
        "skip_llm": False,
        "tiers": [
            {
                "keywords": ["降准"],
                "score": 9,
                "tag": "宏观政策",
                "logic": "宏观政策命中",
                "skip_llm": False,
            }
        ],
    }
    scorer = _quality_gate_scorer(
        (8.0, "宏观政策", "流动性改善利好权益资产", "📈"),
        backend_name="model-a",
    )
    mapping = {
        "mapped_theme": "宏观政策",
        "backtest_symbol": None,
        "mapping_confidence": 0.0,
        "mapping_relevance": 0.0,
        "expected_gap": None,
        "security_candidates": [],
        "market_references": [],
    }
    metrics = MagicMock()

    with (
        patch("core.processor.get_metrics", return_value=metrics),
        patch("core.processor.DB.get_cached_entry", return_value=None),
        patch("core.processor.DB.record_score"),
        patch("core.processor.DB.set_cached_entry", return_value=("", 0)),
        patch("core.processor.DB.get_event_identity_for_link", return_value=("", 0)),
        patch("core.processor.find_similar_article", return_value=(None, 0.0)),
        patch("core.processor.map_article_to_assets", return_value=mapping),
    ):
        result, reason = process_entry(
            entry, [], "prompt", cfg, scorer=scorer, feed_url="u"
        )

    assert result is entry
    assert reason == "scored"
    assert entry.scoring_meta["score_path"] == "llm"
    metrics.record_fast_pass.assert_called_once_with("宏观政策")
