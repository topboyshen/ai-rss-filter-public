"""
核心链路测试：processor, digest_engine, webhook 路径
"""
import pytest
import time
import json
import sqlite3
from types import SimpleNamespace
from unittest.mock import MagicMock

# 确保项目根目录在路径中
import sys
import os
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from core.processor import (
    _cap_irrelevant_high_score,
    _match_fast_pass,
    _validated_feed_entries,
    build_rss_xml,
    process_entry,
)
from utils.helpers import FeedEntry
from core.digest_engine import aggregate_themes, _fallback_aggregation
from sse_engine import (
    _is_realtime_card_noise,
    _load_from_db,
    _preferred_card_title,
    _render_cards,
    _sanitize_card_logic,
    _sanitize_card_title,
)


class TestProcessor:
    """测试 RSS 处理核心链路"""

    def test_build_rss_xml_basic(self):
        """RSS XML 生成：基础结构正确"""
        entries = [
            FeedEntry("Test Title", "http://example.com/1", "Summary 1"),
            FeedEntry("Title 2", "http://example.com/2", "Summary 2"),
        ]
        cfg = {"output": {"max_items": 20}}
        xml = build_rss_xml(entries, "Test Channel", "http://example.com", "desc", cfg)
        assert "<channel>" in xml
        assert "<item>" in xml
        assert "Test Title" in xml
        assert "http://example.com/1" in xml
        assert xml.startswith('<?xml version="1.0" encoding="UTF-8"?>')

    def test_build_rss_xml_empty(self):
        """RSS XML 生成：空列表应返回有效但无 item 的 RSS"""
        cfg = {"output": {"max_items": 20}}
        xml = build_rss_xml([], "Empty", "http://e.com", "desc", cfg)
        assert "<channel>" in xml
        assert "<item>" not in xml

    def test_malformed_empty_feed_is_not_treated_as_success(self):
        feed = SimpleNamespace(
            entries=[],
            feed={},
            version="",
            bozo=True,
            bozo_exception=ValueError("HTML is not RSS"),
        )

        with pytest.raises(ValueError, match="HTML is not RSS"):
            _validated_feed_entries(feed)

    def test_recognizable_empty_feed_remains_valid(self):
        feed = SimpleNamespace(
            entries=[],
            feed={"title": "Quiet source"},
            version="rss20",
            bozo=False,
        )

        assert _validated_feed_entries(feed) == []

    def test_recognizable_empty_feed_records_transport_success(self, monkeypatch):
        import core.processor as processor

        feed = SimpleNamespace(
            entries=[],
            feed={"title": "Quiet source"},
            version="rss20",
            bozo=False,
        )

        class FakeScorer:
            backends = [object()]

        health = []
        monkeypatch.setattr(processor, "_load_feed_bytes", lambda *_args: b"rss")
        monkeypatch.setattr(processor.feedparser, "parse", lambda _raw: feed)
        monkeypatch.setattr(processor, "max_entries_for_feed_url", lambda *_args: None)
        monkeypatch.setattr(processor, "_repair_cached_feed_origins", lambda *_args: {})
        monkeypatch.setattr(processor, "Scorer", FakeScorer)
        monkeypatch.setattr(processor.DB, "get_recent_entries", lambda **_kwargs: [])
        monkeypatch.setattr(
            processor.DB,
            "get_feed_thresholds",
            lambda *_args: {"notify": 9.0},
        )
        monkeypatch.setattr(
            processor.DB,
            "update_feed_health",
            lambda *args: health.append(args),
        )
        monkeypatch.setattr(processor, "bump_stat", lambda *_args, **_kwargs: None)

        _passed, stats, error, title = processor.process_feed(
            "https://example.com/feed",
            "prompt",
            "Quiet source",
            {
                "_blacklist_re": object(),
                "rss_max_fetch_attempts": 1,
                "rss_timeout": 1,
                "max_entries": 15,
                "few_shot_feedback": {"enabled": False},
            },
            check_output_cache=False,
        )

        assert stats["total"] == 0
        assert error is None
        assert title == "Quiet source"
        assert health[-1][3] is True
        assert health[-1][4] == 0

    def test_filtered_llm_attempt_is_included_in_feed_stats(self, monkeypatch):
        import core.processor as processor
        from core.scorer import _record_llm_attempt

        entry = {"title": "Low signal", "link": "https://example.com/item"}
        feed = SimpleNamespace(
            entries=[entry],
            feed={"title": "Source"},
            version="rss20",
            bozo=False,
        )

        class FakeScorer:
            backends = [object()]

        def filtered_after_llm(*_args, **_kwargs):
            _record_llm_attempt("stage1")
            return None, "filtered"

        bumps = []
        monkeypatch.setattr(processor, "_load_feed_bytes", lambda *_args: b"rss")
        monkeypatch.setattr(processor.feedparser, "parse", lambda _raw: feed)
        monkeypatch.setattr(processor, "max_entries_for_feed_url", lambda *_args: None)
        monkeypatch.setattr(processor, "_repair_cached_feed_origins", lambda *_args: {})
        monkeypatch.setattr(processor, "Scorer", FakeScorer)
        monkeypatch.setattr(processor, "process_entry", filtered_after_llm)
        monkeypatch.setattr(processor.DB, "get_recent_entries", lambda **_kwargs: [])
        monkeypatch.setattr(
            processor.DB,
            "get_feed_thresholds",
            lambda *_args: {"notify": 9.0},
        )
        monkeypatch.setattr(processor.DB, "update_feed_health", lambda *_args: None)
        monkeypatch.setattr(
            processor,
            "bump_stat",
            lambda _callback, **kwargs: bumps.append(kwargs),
        )

        _passed, stats, error, _title = processor.process_feed(
            "https://example.com/feed",
            "prompt",
            "Source",
            {
                "_blacklist_re": object(),
                "rss_max_fetch_attempts": 1,
                "rss_timeout": 1,
                "max_entries": 15,
                "few_shot_feedback": {"enabled": False},
            },
            check_output_cache=False,
        )

        assert error is None
        assert stats["api_calls"] == 1
        assert "api_calls_made" not in bumps[-1]

    def test_tolerant_parser_keeps_usable_entries_despite_bozo(self):
        entry = {"title": "Valid item", "link": "https://example.com/item"}
        feed = SimpleNamespace(
            entries=[entry],
            feed={},
            version="rss20",
            bozo=True,
            bozo_exception=ValueError("trailing garbage"),
        )

        assert _validated_feed_entries(feed) == [entry]

    def test_all_entry_exceptions_fail_the_source_refresh(self, monkeypatch):
        import core.processor as processor
        from core.scorer import _record_llm_attempt

        entry = {"title": "Item", "link": "https://example.com/item"}
        feed = SimpleNamespace(
            entries=[entry],
            feed={"title": "Source"},
            version="rss20",
            bozo=False,
        )

        class FakeScorer:
            backends = [object()]

        health = []
        bumps = []

        def fail_after_llm(*_args, **_kwargs):
            _record_llm_attempt("stage1")
            raise RuntimeError("database unavailable")

        monkeypatch.setattr(processor, "_load_feed_bytes", lambda *_args: b"rss")
        monkeypatch.setattr(processor.feedparser, "parse", lambda _raw: feed)
        monkeypatch.setattr(processor, "max_entries_for_feed_url", lambda *_args: None)
        monkeypatch.setattr(processor, "_repair_cached_feed_origins", lambda *_args: {})
        monkeypatch.setattr(processor, "Scorer", FakeScorer)
        monkeypatch.setattr(
            processor,
            "process_entry",
            fail_after_llm,
        )
        monkeypatch.setattr(processor.DB, "get_recent_entries", lambda **_kwargs: [])
        monkeypatch.setattr(
            processor.DB,
            "get_feed_thresholds",
            lambda *_args: {"notify": 9.0},
        )
        monkeypatch.setattr(
            processor.DB,
            "update_feed_health",
            lambda *args: health.append(args),
        )
        monkeypatch.setattr(
            processor,
            "bump_stat",
            lambda _callback, **kwargs: bumps.append(kwargs),
        )

        _passed, stats, error, _title = processor.process_feed(
            "https://example.com/feed",
            "prompt",
            "Source",
            {
                "_blacklist_re": object(),
                "rss_max_fetch_attempts": 1,
                "rss_timeout": 1,
                "max_entries": 15,
                "few_shot_feedback": {"enabled": False},
            },
            check_output_cache=False,
        )

        assert stats["entry_errors"] == 1
        assert stats["api_calls"] == 1
        assert "条目处理全部失败" in error
        assert health[-1][3] is True
        assert "api_calls_made" not in bumps[-1]

    def test_partial_entry_error_does_not_poison_source_refresh(self, monkeypatch):
        import core.processor as processor

        feed = SimpleNamespace(
            entries=[
                {"title": "Broken", "link": "https://example.com/broken"},
                {"title": "Good", "link": "https://example.com/good"},
            ],
            feed={"title": "Source"},
            version="rss20",
            bozo=False,
        )

        class FakeScorer:
            backends = [object()]

        calls = iter([RuntimeError("bad entry"), None])

        def process_one(*_args, **_kwargs):
            outcome = next(calls)
            if outcome is not None:
                raise outcome
            return SimpleNamespace(title="[⭐8.0 ⚖️ 宏观] Good", link="good"), "scored"

        health = []
        monkeypatch.setattr(processor, "_load_feed_bytes", lambda *_args: b"rss")
        monkeypatch.setattr(processor.feedparser, "parse", lambda _raw: feed)
        monkeypatch.setattr(processor, "max_entries_for_feed_url", lambda *_args: None)
        monkeypatch.setattr(processor, "_repair_cached_feed_origins", lambda *_args: {})
        monkeypatch.setattr(processor, "Scorer", FakeScorer)
        monkeypatch.setattr(processor, "process_entry", process_one)
        monkeypatch.setattr(processor.DB, "get_recent_entries", lambda **_kwargs: [])
        monkeypatch.setattr(
            processor.DB,
            "get_feed_thresholds",
            lambda *_args: {"notify": 9.0},
        )
        monkeypatch.setattr(
            processor.DB,
            "update_feed_health",
            lambda *args: health.append(args),
        )
        monkeypatch.setattr(processor, "bump_stat", lambda *_args, **_kwargs: None)

        passed, stats, error, _title = processor.process_feed(
            "https://example.com/feed",
            "prompt",
            "Source",
            {
                "_blacklist_re": object(),
                "rss_max_fetch_attempts": 1,
                "rss_timeout": 1,
                "max_entries": 15,
                "few_shot_feedback": {"enabled": False},
            },
            check_output_cache=False,
        )

        assert len(passed) == 1
        assert stats["entry_errors"] == 1
        assert error is None
        assert health[-1][3] is True
        assert str(health[-1][7]).startswith("部分条目处理失败: 1/2")

    def test_process_entry_blacklist(self):
        """黑名单过滤：含黑名单关键词的 entry 应被过滤"""
        entry = MagicMock()
        entry.title = "新股申购公告：某公司"
        entry.link = "http://example.com"
        entry.summary = ""
        entry.published_parsed = time.gmtime()
        cfg = {
            "blacklist": ["新股申购"],
            "threshold": {"pass": 7, "similarity": 0.6, "notify": 9},
            "_blacklist_re": None,
        }
        result, reason = process_entry(entry, [], "prompt", cfg)
        assert result is None
        assert reason in ("blacklist", "filtered")  # 黑名单命中返回 None

    @pytest.mark.parametrize("prefix", ["[⭐", "[★"])
    def test_process_entry_skips_already_scored_titles(
        self, monkeypatch, prefix
    ):
        entry = FeedEntry(
            f"{prefix}8.5 ⚖️ 政策] 央行发布流动性安排",
            "http://example.com/already-scored",
            "来源摘要",
        )
        scorer = MagicMock()
        monkeypatch.setattr(
            "core.processor.DB.canonical_feed_url",
            lambda _cfg, value: value,
        )

        result, reason = process_entry(
            entry,
            [],
            "prompt",
            {"few_shot_feedback": {"enabled": False}},
            scorer=scorer,
        )

        assert result is None
        assert reason == "already_scored_title"
        assert scorer.mock_calls == []

    def test_process_entry_cache_hit(self):
        """URL 缓存命中：已有高分缓存直接返回"""
        from db import database

        # process_entry re-reads this link's live cache row; give it a schema.
        database.init_db()
        entry = MagicMock()
        entry.title = "原标题"
        entry.link = "http://example.com/1"
        entry.summary = "内容"
        entry.published_parsed = time.gmtime()
        cfg = {
            "blacklist": [],
            "threshold": {"pass": 7, "similarity": 0.6, "notify": 9},
            "_blacklist_re": None,
        }
        # 模拟缓存命中
        link_cache = {
            "http://example.com/1": (
                "新标题",
                "原标题",
                8.5,
                "宏观",
                "逻辑",
                time.time(),
                "摘要",
                "",
                "llm",
            )
        }
        result, reason = process_entry(entry, [], "prompt", cfg, link_cache=link_cache)
        assert result is not None
        assert reason == "cache"
        assert entry.title == "新标题"  # 标题被替换

    def test_process_entry_cache_hit_normalizes_dirty_cached_and_source_titles(self):
        from db import database

        # process_entry re-reads this link's live cache row; give it a schema.
        database.init_db()
        entry = MagicMock()
        entry.title = '&lt;span class="section-news"&gt;1. 央企AI+行动&lt;/span&gt;'
        entry.link = "http://example.com/dirty-title"
        entry.summary = "内容"
        entry.published_parsed = time.gmtime()
        cfg = {
            "blacklist": [],
            "threshold": {"pass": 7, "similarity": 0.6, "notify": 9},
            "_blacklist_re": None,
        }
        link_cache = {
            entry.link: (
                '[⭐8.5 ⚖️ 政策] <span class="section-news">1. 央企AI+行动</span>',
                '<span class="section-news">1. 央企AI+行动</span>',
                8.5,
                "政策",
                "逻辑",
                time.time(),
                "摘要",
                "",
                "llm",
            )
        }

        result, reason = process_entry(
            entry, [], "prompt", cfg, link_cache=link_cache
        )

        assert result is entry
        assert reason == "cache"
        assert entry.title == "[⭐8.5 ⚖️ 政策] 1. 央企AI+行动"
        assert entry._raw_title == "1. 央企AI+行动"

    @pytest.mark.parametrize(
        ("ledger_path", "expected_reason"),
        [
            ("llm", "filtered"),
            ("fast_pass", "filtered"),
            ("fallback_local", "filtered"),
            ("llm_safe_neutral_contract", "filtered"),
            ("stage2_safe_neutral_grounding", "filtered"),
            ("similarity", "similarity"),
        ],
    )
    def test_process_entry_skips_rescoring_when_ledger_already_has_path(
        self, monkeypatch, ledger_path, expected_reason
    ):
        from db import database

        # process_entry re-reads this link's live cache row; give it a schema.
        database.init_db()
        entry = FeedEntry(
            "7月份国民经济保持总体平稳向新向优发展态势",
            "http://www.stats.gov.cn/sj/zxfb/202608/t20260817_1965056.html",
            "",
        )
        scorer = MagicMock()
        monkeypatch.setattr(
            "core.processor.DB.canonical_feed_url",
            lambda _cfg, value: value,
        )
        ledger_lookup = MagicMock(return_value=ledger_path)
        set_cached = MagicMock()
        store_signal = MagicMock()
        record_score = MagicMock()
        find_similar = MagicMock()
        monkeypatch.setattr(
            "core.processor.DB.get_latest_signal_score_path",
            ledger_lookup,
        )
        monkeypatch.setattr("core.processor.DB.set_cached_entry", set_cached)
        monkeypatch.setattr("core.processor.DB.store_backtest_signal", store_signal)
        monkeypatch.setattr("core.processor.DB.record_score", record_score)
        monkeypatch.setattr("core.processor.find_similar_article", find_similar)
        cfg = {
            "blacklist": [],
            "threshold": {"pass": 7, "similarity": 0.6, "notify": 9},
            "_blacklist_re": None,
            "scoring_strategy_version": "2026-05-19-v2:test",
            "few_shot_feedback": {"enabled": False},
        }

        result, reason = process_entry(
            entry,
            [],
            "prompt",
            cfg,
            scorer=scorer,
            link_cache={},
        )

        assert result is None
        assert reason == expected_reason
        ledger_lookup.assert_called_once_with(
            entry.link,
            cfg["scoring_strategy_version"],
        )
        assert scorer.mock_calls == []
        set_cached.assert_not_called()
        store_signal.assert_not_called()
        record_score.assert_not_called()
        find_similar.assert_not_called()

    def test_fast_pass_tiered_scores(self):
        cfg = {
            "fast_pass": {
                "enabled": True,
                "tiers": [
                    {"score": 9.0, "tag": "宏观政策", "logic": "宏观政策命中", "keywords": ["降准"]},
                    {"score": 7.2, "tag": "市场异动", "logic": "市场异动命中", "keywords": ["涨停"]},
                ],
                "watchlist": ["990002"],
                "watchlist_score": 8.5,
                "watchlist_tag": "持仓命中",
            }
        }
        assert _match_fast_pass("央行宣布降准", cfg)[0:3] == (9.0, "宏观政策", "宏观政策命中")
        assert _match_fast_pass("某板块涨停", cfg)[0:3] == (7.2, "市场异动", "市场异动命中")
        assert _match_fast_pass("990002 出现异动", cfg)[0:2] == (8.5, "持仓命中")

    def test_cap_irrelevant_high_score(self):
        score, tag, logic = _cap_irrelevant_high_score(10.0, "无相关标签", "无相关性标签")
        assert score == 4.0
        assert tag == "无相关标签"
        assert logic == "无相关性标签"
        assert _cap_irrelevant_high_score(5.5, "无相关标签", "无相关性标签")[0] == 5.5
        assert _cap_irrelevant_high_score(8.5, "无线通信", "6G 技术突破")[0] == 8.5
        assert _cap_irrelevant_high_score(8.5, "未知", "模型标签未知但逻辑有效")[0] == 8.5
        assert _cap_irrelevant_high_score(8.5, "宏观政策", "降准利好A股")[0] == 8.5


class TestRealtimeCards:
    @staticmethod
    def _create_realtime_cache_db(path, *, with_score_path=True):
        score_path_column = ", score_path TEXT DEFAULT ''" if with_score_path else ""
        conn = sqlite3.connect(path)
        conn.execute(
            "CREATE TABLE news_cache ("
            "link TEXT PRIMARY KEY, title TEXT, score REAL, tag TEXT, logic TEXT, "
            "created_at INTEGER, feed_url TEXT, direction TEXT"
            f"{score_path_column})"
        )
        conn.commit()
        return conn

    @staticmethod
    def _patch_realtime_db(monkeypatch, path):
        import db.database as database

        monkeypatch.setattr(database, "DB_PATH", str(path))
        monkeypatch.setattr(
            database,
            "get_feed_thresholds",
            lambda *_args, **_kwargs: {"aggregation": 0.0},
        )
        monkeypatch.setattr(
            "sse_engine._enabled_feed_config",
            lambda _cfg, url: {"name": "测试源", "url": url, "enabled": True} if url else {},
        )

    def test_db_fallback_excludes_reported_similarity_copies(self, tmp_path, monkeypatch):
        path = tmp_path / "realtime-with-score-path.sqlite"
        conn = self._create_realtime_cache_db(path)
        now = int(time.time())
        rows = [
            ("https://wallstreetcn.com/livenews/3140749", "早盘多只宽基ETF成交放量", 6.5, "similarity"),
            ("https://api3.cls.cn/share/article/2438732", "早盘多只宽基ETF成交显著放量", 6.5, "similarity"),
            ("https://flash.jin10.com/detail/20260728130045887800", "多只宽基ETF早盘放量 资金正持续入场", 6.5, "stage2"),
            ("https://www.36kr.com/newsflashes/3914796783801477", "英伟达据悉签署价值高达500亿美元的得州数据中心租赁协议", 8.5, "llm"),
            ("https://wallstreetcn.com/livenews/3140738", "英伟达签署价值高达500亿美元的德州数据中心租赁协议", 8.5, "similarity"),
            ("https://safe-neutral.test/dirty", "安全中性结果不得公开", 9.9, " LLM_SAFE_NEUTRAL_CONTRACT "),
            ("https://unknown.test/dirty", "未知评分来源不得公开", 9.9, " future_unknown "),
        ]
        conn.executemany(
            "INSERT INTO news_cache "
            "(link, title, score, tag, logic, created_at, feed_url, direction, score_path) "
            "VALUES (?, ?, ?, '', '', ?, 'http://feed.test/live', '中性', ?)",
            [(link, title, score, now - index, score_path) for index, (link, title, score, score_path) in enumerate(rows)],
        )
        conn.commit()
        conn.close()
        self._patch_realtime_db(monkeypatch, path)

        loaded = _load_from_db(limit=30, hours=48, cfg={})

        assert {entry["link"] for entry in loaded} == {
            "https://flash.jin10.com/detail/20260728130045887800",
            "https://www.36kr.com/newsflashes/3914796783801477",
        }

    def test_similarity_filter_is_applied_before_sql_limit(self, tmp_path, monkeypatch):
        path = tmp_path / "realtime-filter-before-limit.sqlite"
        conn = self._create_realtime_cache_db(path)
        now = int(time.time())
        conn.executemany(
            "INSERT INTO news_cache "
            "(link, title, score, tag, logic, created_at, feed_url, direction, score_path) "
            "VALUES (?, ?, 9.9, '', '', ?, 'http://feed.test/live', '中性', 'similarity')",
            [(f"https://duplicate.test/{index}", f"重复副本{index}", now - index) for index in range(20)],
        )
        conn.execute(
            "INSERT INTO news_cache "
            "(link, title, score, tag, logic, created_at, feed_url, direction, score_path) "
            "VALUES ('https://original.test/1', '独立评分原件', 6.5, '', '', ?, "
            "'http://feed.test/live', '中性', 'stage2')",
            (now - 30,),
        )
        conn.commit()
        conn.close()
        self._patch_realtime_db(monkeypatch, path)

        loaded = _load_from_db(limit=1, hours=48, cfg={})

        assert [entry["link"] for entry in loaded] == ["https://original.test/1"]

    def test_db_fallback_applies_public_contract_before_sql_limit(self, tmp_path, monkeypatch):
        path = tmp_path / "realtime-public-contract.sqlite"
        conn = sqlite3.connect(path)
        conn.execute(
            "CREATE TABLE news_cache ("
            "link TEXT PRIMARY KEY, title TEXT, raw_title TEXT, score REAL, "
            "tag TEXT, logic TEXT, created_at INTEGER, feed_url TEXT, "
            "direction TEXT, score_path TEXT, strategy_version TEXT)"
        )
        now = int(time.time())
        excluded_rows = []
        for index in range(12):
            excluded_rows.extend([
                (
                    f"https://quarantine.test/{index}", "隔离行", "隔离行", 9.9,
                    "宏观", "", now - index, "http://feed.test/live", "中性",
                    "quality_quarantine", "current:previous",
                ),
                (
                    f"https://irrelevant.test/{index}", "无关行", "无关行", 9.8,
                    "未知", "", now - index, "http://feed.test/live", "中性",
                    "llm", "current:previous",
                ),
                (
                    f"https://old-strategy.test/{index}", "旧策略行", "旧策略行", 9.7,
                    "宏观", "", now - index, "http://feed.test/live", "中性",
                    "llm", "legacy:old",
                ),
            ])
        conn.executemany(
            "INSERT INTO news_cache VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            excluded_rows,
        )
        conn.execute(
            "INSERT INTO news_cache VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                "https://valid.test/1", "独立有效行", "独立有效行", 6.5,
                "宏观", "有效逻辑", now - 100, "http://feed.test/live", "中性",
                "stage2", "current:previous",
            ),
        )
        conn.commit()
        conn.close()
        self._patch_realtime_db(monkeypatch, path)

        loaded = _load_from_db(
            limit=1,
            hours=48,
            cfg={"scoring_strategy_version": "current:runtime"},
        )

        assert [entry["link"] for entry in loaded] == ["https://valid.test/1"]
        assert loaded[0]["strategy_version"] == "current:previous"

    def test_db_fallback_prefers_raw_title_and_bounds_visible_title(self, tmp_path, monkeypatch):
        path = tmp_path / "realtime-raw-title.sqlite"
        conn = sqlite3.connect(path)
        conn.execute(
            "CREATE TABLE news_cache ("
            "link TEXT PRIMARY KEY, title TEXT, raw_title TEXT, score REAL, "
            "tag TEXT, logic TEXT, created_at INTEGER, feed_url TEXT, "
            "direction TEXT, score_path TEXT, strategy_version TEXT)"
        )
        raw_title = "央行发布结构性流动性工具操作安排" + "并明确后续实施细节" * 12
        conn.execute(
            "INSERT INTO news_cache VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                "https://valid.test/raw-title",
                "DeepSeek 类输出截断到这里……",
                raw_title,
                8.8,
                "宏观",
                "政策工具影响资金面",
                int(time.time()),
                "http://feed.test/live",
                "中性",
                "stage2",
                "current:previous",
            ),
        )
        conn.commit()
        conn.close()
        self._patch_realtime_db(monkeypatch, path)

        loaded = _load_from_db(
            limit=1,
            cfg={"scoring_strategy_version": "current:runtime"},
        )

        assert len(loaded) == 1
        assert loaded[0]["title"] == _preferred_card_title(raw_title, "模型截断标题")
        assert loaded[0]["title"].startswith("央行发布结构性流动性工具操作安排")
        assert loaded[0]["title"].endswith("…")
        assert len(loaded[0]["title"]) <= 72
        assert _preferred_card_title("<span></span>", "可信回退标题") == "可信回退标题"
        truncated = "DeepSeek发布新模型，"
        wrapped_same = (
            f"[⭐8.8 ⚖️ 科技] {truncated} 💡" + "评分逻辑很长" * 12
        )
        assert _preferred_card_title(truncated, wrapped_same) == truncated
        wrapped_extension = (
            f"[⭐8.8 ⚖️ 科技] {truncated}并开放完整技术报告 💡评分逻辑"
        )
        assert _preferred_card_title(truncated, wrapped_extension) == (
            truncated + "并开放完整技术报告"
        )

    def test_db_fallback_remains_compatible_without_score_path_column(self, tmp_path, monkeypatch):
        path = tmp_path / "realtime-legacy-schema.sqlite"
        conn = self._create_realtime_cache_db(path, with_score_path=False)
        now = int(time.time())
        conn.execute(
            "INSERT INTO news_cache "
            "(link, title, score, tag, logic, created_at, feed_url, direction) "
            "VALUES ('https://legacy.test/1', '旧库正常条目', 8.0, '', '', ?, "
            "'http://feed.test/live', '中性')",
            (now,),
        )
        conn.commit()
        conn.close()
        self._patch_realtime_db(monkeypatch, path)

        loaded = _load_from_db(limit=1, hours=48, cfg={})

        assert [entry["link"] for entry in loaded] == ["https://legacy.test/1"]

    def test_realtime_card_title_hides_legacy_markup_without_executing_it(self):
        dirty = (
            '[⭐6.5 ⚖️ 国家级AI政策] '
            '&lt;span class=&quot;section-news&quot;&gt;1. 国资委推进央企AI+行动'
            '&lt;/span&gt;&lt;br /&gt;&lt;img src=x onerror=&quot;alert(1)&quot;&gt;'
        )

        cleaned = _sanitize_card_title(dirty)
        rendered = _render_cards([{
            "title": dirty,
            "score": 6.5,
            "tag": "国家级AI政策",
            "logic": "政策利好算力基建",
            "link": "https://flash.jin10.com/detail/20260728115559661800",
            "time": int(time.time()),
        }])

        assert cleaned == "1. 国资委推进央企AI+行动"
        assert "span" not in cleaned
        assert "onerror" not in cleaned
        assert "section-news" not in rendered
        assert "onerror" not in rendered
        assert "国资委推进央企AI+行动" in rendered

    def test_sanitize_card_logic_clips_long_briefing_body(self):
        long_logic = "债市要闻 【李强主持召开国务院常务会议 讨论草案】" + "长正文" * 300
        cleaned = _sanitize_card_logic(long_logic)
        assert len(cleaned) <= 97
        assert "长正文" not in cleaned or cleaned.endswith("…")
        assert _sanitize_card_logic("逻辑：资金面改善; FastPass候选:宏观") == "资金面改善"
        assert _sanitize_card_logic("FastPass候选:宏观") == ""

    def test_realtime_card_noise_filters_omnibus_reports(self):
        assert _is_realtime_card_noise({
            "title": "债市早参5月22日|今年首期注资特别国债发行",
            "logic": "债市要闻 【新闻一】正文 【新闻二】正文 【新闻三】正文",
        })

    def test_render_cards_does_not_emit_full_logic_body(self):
        entries = [{
            "title": "央行昨日净投放近千亿",
            "score": 8.8,
            "tag": "宏观政策",
            "logic": "债市要闻 【李强主持召开国务院常务会议】" + "长正文" * 300,
            "link": "http://example.com",
            "time": int(time.time()),
        }]
        html = _render_cards(entries)
        assert len(html) < 3500
        assert "长正文" * 20 not in html

    def test_glance_sse_widget_passes_feed_url_to_db_loader(self, monkeypatch):
        from app import app

        calls = []

        def _fake_load(limit=30, min_score=None, hours=48, feed_url=None, cfg=None):
            calls.append({"min_score": min_score, "feed_url": feed_url})
            if feed_url == "http://feed.test/a":
                return [{
                    "title": "测试", "link": "http://x", "score": 9.0,
                    "tag": "", "logic": "", "time": int(time.time()),
                    "feed_url": "http://feed.test/a", "source": "测试源",
                }]
            return []

        monkeypatch.setattr("sse_engine.load_config", lambda: {
            "threshold": {"pass": 8},
            "feeds": [{"name": "测试源", "url": "http://feed.test/a", "enabled": True}],
        })
        monkeypatch.setattr("sse_engine._load_from_db", _fake_load)

        with app.test_client() as client:
            r = client.get("/glance-sse?feed_url=http://feed.test/a")
            assert r.status_code == 200
            assert "<article" in r.get_data(as_text=True)
        assert calls[-1]["feed_url"] == "http://feed.test/a"
        assert calls[-1]["min_score"] is None

    def test_glance_sse_widget_default_path_uses_db_and_no_feed_filter(self, monkeypatch):
        from app import app

        calls = []

        def _fake_load(limit=30, min_score=None, hours=48, feed_url=None, cfg=None):
            calls.append({"min_score": min_score, "feed_url": feed_url})
            return [{
                "title": "缓存条目", "link": "http://cache", "score": 8.0,
                "tag": "", "logic": "", "time": int(time.time()),
                "feed_url": "http://rsshub:1200/cache", "source": "缓存源",
            }]

        monkeypatch.setattr("sse_engine.load_config", lambda: {
            "threshold": {"pass": 7},
            "feeds": [{"name": "缓存源", "url": "http://rsshub:1200/cache", "enabled": True}],
            "_runtime_feed_url_aliases": {
                "http://rsshub:1200/cache": ["http://127.0.0.1:1200/cache"]
            },
        })
        monkeypatch.setattr("sse_engine._load_from_db", _fake_load)

        with app.test_client() as client:
            r = client.get("/glance-sse")
            body = r.get_data(as_text=True)
            assert r.status_code == 200
            assert "<article" in body
            assert "缓存条目" in body
            assert "阅读优先级，不代表买入强度" in body
            assert "实时高分情报" not in body
            assert "源门槛 ≥7.0" in body
            assert "1个订阅源" in body
            assert " · 1源" not in body
        assert len(calls) == 1
        assert calls[0]["feed_url"] is None
        assert calls[0]["min_score"] is None

    def test_glance_sse_widget_explicit_min_score_filters_db_results(self, monkeypatch):
        from app import app

        monkeypatch.setattr("sse_engine.load_config", lambda: {
            "threshold": {"pass": 7},
            "feeds": [{"name": "缓存源", "url": "http://rsshub:1200/cache", "enabled": True}],
        })
        monkeypatch.setattr(
            "sse_engine._load_from_db",
            lambda **_kwargs: [{
                "title": "低于指定门槛的缓存条目",
                "link": "https://example.com/cache",
                "score": 8.0,
                "tag": "测试",
                "logic": "",
                "time": int(time.time()),
                "feed_url": "http://rsshub:1200/cache",
                "source": "缓存源",
            }],
        )

        with app.test_client() as client:
            body = client.get("/glance-sse?min_score=9").get_data(as_text=True)

        assert "低于指定门槛的缓存条目" not in body
        assert "<article" not in body
        assert "≥9.0分" in body

    def test_glance_sse_widget_applies_public_contract_to_db_results(self, monkeypatch):
        from app import app

        feed_url = "http://feed.test/enabled"
        cfg = {
            "scoring_strategy_version": "current:runtime",
            "threshold": {"pass": 7},
            "feeds": [{
                "name": "有效源",
                "url": feed_url,
                "enabled": True,
                "aggregation_threshold": 6.0,
            }],
        }
        now = int(time.time())
        base = {
            "score": 8.8,
            "time": now,
            "feed_url": feed_url,
            "source": "有效源",
            "score_path": "llm",
            "strategy_version": "current:previous",
            "tag": "宏观",
        }
        monkeypatch.setattr("sse_engine.load_config", lambda: cfg)
        monkeypatch.setattr(
            "sse_engine._load_from_db",
            lambda **_kwargs: [
                {
                    **base,
                    "title": "保留的当前策略情报",
                    "link": "https://example.com/current",
                    "logic": "资金面改善; FastPass候选:宏观",
                },
                {
                    **base,
                    "title": "隔离情报",
                    "link": "https://example.com/quarantine",
                    "score_path": "quality_quarantine",
                },
                {
                    **base,
                    "title": "无关情报",
                    "link": "https://example.com/irrelevant",
                    "tag": "未知",
                },
                {
                    **base,
                    "title": "旧策略情报",
                    "link": "https://example.com/old",
                    "strategy_version": "legacy:old",
                },
            ],
        )

        with app.test_client() as client:
            body = client.get("/glance-sse").get_data(as_text=True)

        assert body.count("<article") == 1
        assert "保留的当前策略情报" in body
        # This legacy loader fixture has no publisher raw_title. Missing input
        # is not evidence of a completed company lookup with no match.
        assert "标题提及：原始标题未记录，暂无法识别公司" in body
        assert "标题未匹配到现有主题规则，暂不展示资产关联" not in body
        assert "方向与入选依据" in body
        assert "主题背景／规则池" not in body
        assert "等待更多来源证据" not in body
        assert "资金面改善" not in body
        assert "FastPass候选" not in body
        assert "隔离情报" not in body
        assert "无关情报" not in body
        assert "旧策略情报" not in body

    def test_glance_sse_widget_rebinds_threshold_after_cross_source_dedupe(self, monkeypatch):
        from app import app

        now = int(time.time())
        cfg = {
            "threshold": {"pass": 7},
            "feeds": [
                {
                    "name": "来源甲",
                    "url": "http://feed.test/a",
                    "enabled": True,
                    "aggregation_threshold": 6.0,
                },
                {
                    "name": "来源乙",
                    "url": "http://feed.test/b",
                    "enabled": True,
                    "aggregation_threshold": 7.0,
                },
            ],
        }
        monkeypatch.setattr("sse_engine.load_config", lambda: cfg)
        monkeypatch.setattr(
            "sse_engine._load_from_db",
            lambda **_kwargs: [
                {
                    "title": "同一事件",
                    "link": "https://example.com/a",
                    "score": 8.0,
                    "time": now - 60,
                    "feed_url": "http://feed.test/a",
                    "source": "来源甲",
                },
                {
                    "title": "同一事件",
                    "link": "https://example.com/b",
                    "score": 8.0,
                    "time": now - 10,
                    "feed_url": "http://feed.test/b",
                    "source": "来源乙",
                },
            ],
        )

        with app.test_client() as client:
            body = client.get("/glance-sse").get_data(as_text=True)

        assert body.count("<article") == 1
        assert "来源乙" in body
        assert "源门槛 ≥7.0" in body
        assert "源门槛 ≥6.0" not in body

    def test_glance_sse_widget_filters_empty_title_before_counts(self, monkeypatch):
        from app import app

        monkeypatch.setattr("sse_engine.load_config", lambda: {
            "threshold": {"pass": 7},
            "feeds": [{"name": "缓存源", "url": "http://feed.test/a", "enabled": True}],
        })
        monkeypatch.setattr(
            "sse_engine._load_from_db",
            lambda **_kwargs: [{
                "title": "&lt;span&gt;&lt;/span&gt;",
                "link": "https://example.com/empty",
                "score": 9.0,
                "time": int(time.time()),
                "feed_url": "http://feed.test/a",
                "source": "缓存源",
            }],
        )

        with app.test_client() as client:
            body = client.get("/glance-sse").get_data(as_text=True)

        assert "<article" not in body
        assert "近12小时 · 强信号" not in body
        assert "近48小时暂无达到当前来源门槛的重点情报" in body

    @pytest.mark.parametrize("value", ["nan", "inf", "-1", "10.1", "not-a-number"])
    def test_glance_sse_widget_rejects_invalid_min_score(self, monkeypatch, value):
        from app import app

        monkeypatch.setattr("sse_engine.load_config", lambda: {})

        with app.test_client() as client:
            response = client.get(f"/glance-sse?min_score={value}")

        assert response.status_code == 400
        assert "min_score" in response.get_data(as_text=True)

    def test_glance_sse_widget_rejects_unknown_cache_and_explicit_feed(self, monkeypatch):
        from app import app

        load_calls = []
        monkeypatch.setattr("sse_engine.load_config", lambda: {
            "threshold": {"pass": 7},
            "feeds": [{"name": "有效源", "url": "http://feed.test/enabled", "enabled": True}],
        })
        monkeypatch.setattr(
            "sse_engine._load_from_db",
            lambda **kwargs: load_calls.append(kwargs) or [{
                "title": "积压测试", "link": "https://example.com/news/1", "score": 9.5,
                "tag": "测试", "logic": "", "time": int(time.time()),
                "feed_url": "unknown", "source": "unknown",
            }],
        )

        with app.test_client() as client:
            default_body = client.get("/glance-sse").get_data(as_text=True)
            explicit_body = client.get("/glance-sse?feed_url=unknown").get_data(as_text=True)

        assert "积压测试" not in default_body
        assert "<article" not in explicit_body
        assert len(load_calls) == 1


class TestDigestEngine:
    """测试主题聚合与日报生成"""

    def test_fallback_aggregation_empty(self):
        """fallback 聚合：空列表返回空 themes"""
        result = _fallback_aggregation([])
        assert result["themes"] == []
        assert "暂无" in result["overall_assessment"] or "降级" in result["overall_assessment"]

    def test_event_key_ignores_search_highlight_markup(self):
        from core.digest_engine import normalize_event_key

        assert normalize_event_key("<em>A股</em>超4600只个股飘绿") == normalize_event_key(
            "A股超4600只个股飘绿"
        )
        assert normalize_event_key("&lt;em&gt;A股&lt;/em&gt;超4600只个股飘绿") == normalize_event_key(
            "A股超4600只个股飘绿"
        )

    def test_dedupe_preserves_cross_source_identity_and_normalizes_rss_aliases(self):
        from core.digest_engine import article_source_key, dedupe_articles

        deduped = dedupe_articles([
            {
                "title": "国务院发布扩大消费十五五规划", "score": 9.0,
                "created_at": 100, "link": "https://www.gelonghui.com/p/1",
                "feed_url": "http://rsshub:1200/gelonghui/news",
            },
            {
                "title": "国务院发布扩大消费十五五规划", "score": 8.8,
                "created_at": 101, "link": "https://wallstreetcn.com/livenews/2",
                "feed_url": "http://127.0.0.1:1200/wallstreetcn/live",
            },
        ])
        assert len(deduped) == 1
        assert deduped[0]["duplicate_count"] == 2
        assert set(deduped[0]["source_keys"]) == {"gelonghui", "wallstreetcn"}
        assert article_source_key({
            "feed_url": "http://127.0.0.1:1200/caixin/latest",
            "link": "",
        }) == article_source_key({
            "feed_url": "http://rsshub:1200/caixin/latest",
            "link": "",
        }) == "caixin"

    def test_source_label_cannot_be_spoofed_by_an_untrusted_path(self):
        from core.digest_engine import _briefing_source_label

        assert _briefing_source_label({"link": "https://evil.example/gelonghui"}) == "evil.example"

    def test_fallback_aggregation_grouping(self):
        """fallback 聚合：相同 tag 的 articles 应被分组"""
        articles = [
            {"title": "A", "tag": "宏观放水", "score": 8.0, "link": "http://a", "logic": "L1"},
            {"title": "B", "tag": "宏观放水", "score": 7.5, "link": "http://b", "logic": "L2"},
            {"title": "C", "tag": "政策", "score": 6.0, "link": "http://c", "logic": "L3"},
        ]
        result = _fallback_aggregation(articles)
        themes = result["themes"]
        assert len(themes) == 2
        # 按分数排序，宏观放水组应在前面
        assert themes[0]["theme"] == "宏观放水"
        assert len(themes[0]["articles"]) == 2

    def test_fallback_aggregation_merges_national_policy_aliases(self):
        result = _fallback_aggregation([
            {"title": "A", "tag": "国家级政策", "score": 9.0},
            {"title": "B", "tag": "国家级产业政策", "score": 8.5},
            {"title": "C", "tag": "国家产业政策", "score": 8.0},
        ])
        assert [theme["theme"] for theme in result["themes"]] == ["国家产业政策"]

    def test_theme_alias_direction_merge_is_order_independent(self):
        from core.digest_engine import _normalize_briefing

        bullish = {"theme": "AI算力", "direction": "看多", "importance": 8, "articles": [{"link": "a"}]}
        neutral = {"theme": "算力基建", "direction": "中性", "importance": 9, "articles": [{"link": "a"}]}
        bearish = {"theme": "数字中国", "direction": "看空", "importance": 8, "articles": [{"link": "b"}]}
        for themes in ([bullish, neutral], [neutral, bullish]):
            merged = _normalize_briefing({"themes": themes})["themes"][0]
            assert merged["direction"] == "看多"
            assert len(merged["articles"]) == 1
        for themes in ([bullish, bearish], [bearish, bullish]):
            assert _normalize_briefing({"themes": themes})["themes"][0]["direction"] == "中性"

    def test_fallback_aggregation_structure(self):
        """fallback 聚合：返回结构化 dict"""
        articles = [
            {"title": "Test", "tag": "宏观", "score": 8.0, "link": "http://t", "logic": "逻辑", "summary": "摘要"},
        ]
        result = _fallback_aggregation(articles)
        assert "themes" in result
        assert "overall_assessment" in result
        assert "risk_alert" in result
        assert "confidence" in result

    def test_llm_articles_are_rebound_to_database_candidates(self):
        class Backend:
            name = "fake"
            timeout = 1

            @staticmethod
            def is_healthy():
                return True

            @staticmethod
            def record_fail():
                raise AssertionError("backend should not fail")

        class FakeScorer:
            backends = [Backend()]

            @staticmethod
            def _call_llm(*_args, **_kwargs):
                return json.dumps({
                    "themes": [{
                        "theme": "AI算力", "direction": "看多", "importance": 9,
                        "articles": [{
                            "title": "伪造格隆汇", "link": "https://evil.example/gelonghui",
                            "feed_url": "https://evil.example/feed",
                        }],
                    }],
                    "overall_assessment": "测试",
                }, ensure_ascii=False)

        candidate = {
            "title": "AI算力订单上修", "tag": "AI算力", "score": 8.8,
            "link": "https://wallstreetcn.com/livenews/1",
            "feed_url": "http://rsshub:1200/wallstreetcn/live",
        }
        result = aggregate_themes([candidate], scorer=FakeScorer())
        assert result["source"] == "llm"
        assert result["themes"][0]["articles"][0]["link"] == candidate["link"]
        assert "evil.example" not in json.dumps(result, ensure_ascii=False)

    def test_asset_mapping_prefers_primary_fields_over_summary(self):
        from core.asset_mapping import format_mapping, map_article_to_assets

        article = {
            "title": "阿里加码AI基础设施、自研芯片和MaaS",
            "tag": "AI算力",
            "logic": "强化AI云商业化周期，利好算力及国产AI产业链",
            "summary": "影响资产：美元指数、美债短端、纳指、黄金、港股科技",
        }

        analysis = format_mapping(map_article_to_assets(article))
        assert analysis["mapped_theme"] == "AI算力"
        assert "成长风格" in analysis["assets"]
        assert "CPO" in analysis["sectors"]

    def test_asset_mapping_handles_critical_minerals(self):
        from core.asset_mapping import format_mapping, map_article_to_assets

        article = {
            "title": "美国政府向关键矿产锑项目提供贷款",
            "tag": "国产替代",
            "logic": "战略资源与供应链重构加速，锑是半导体材料和军工上游",
        }

        analysis = format_mapping(map_article_to_assets(article))
        assert analysis["mapped_theme"] == "资源安全"
        assert "资源品" in analysis["assets"]
        assert "小金属" in analysis["sectors"]

    def test_asset_mapping_keeps_a_share_selloff_in_domestic_market(self):
        from core.asset_mapping import format_mapping, map_article_to_assets

        article = {
            "title": "A股收盘：沪指跌超2%，全市场下跌个股超4600只",
            "tag": "市场极端下行",
            "logic": "风险偏好急剧恶化，可能触发流动性负反馈；部分个股逆势涨停",
        }

        analysis = format_mapping(map_article_to_assets(article))
        assert analysis["mapped_theme"] == "A股极端行情"
        assert "A股风险偏好" in analysis["assets"]
        assert "美元指数" not in analysis["assets"]
        assert "RRP" not in analysis["watch"]

    def test_asset_mapping_does_not_treat_one_falling_sector_as_market_stress(self):
        from core.asset_mapping import format_mapping, map_article_to_assets

        analysis = format_mapping(map_article_to_assets({
            "title": "沪指收涨，AI芯片板块午后下跌",
            "tag": "AI算力",
            "logic": "指数企稳但行业分化",
        }))

        assert analysis["mapped_theme"] == "AI算力"

    @pytest.mark.parametrize("title", [
        "沪指止跌回升，深成指翻红，三大指数集体收涨",
        "美股三大指数集体下挫，美国股指期货下跌",
        "沪指跌幅收窄后翻红，深成指低开高走",
        "沪指涨2%，深成指收涨，芯片板块下跌",
        "市场未出现普跌，沪指止跌企稳，深成指翻红",
        "沪指一度跌超2%但尾盘收涨，深成指翻红",
        "沪指微跌0.1%，深成指窄幅震荡",
        "沪指下跌0.1%，深成指上涨0.2%",
        "创业板指小幅走弱",
        "沪指不跌反涨",
    ])
    def test_asset_mapping_does_not_treat_recovery_or_overseas_moves_as_a_share_stress(self, title):
        from core.asset_mapping import map_article_to_assets

        assert map_article_to_assets({
            "title": title,
            "tag": "市场观察",
            "logic": "指数行情更新",
        })["mapped_theme"] != "A股极端行情"

    def test_asset_mapping_distinguishes_china_reverse_repo_from_usd_liquidity(self):
        from core.asset_mapping import format_mapping, map_article_to_assets

        domestic = format_mapping(map_article_to_assets({
            "title": "中国人民银行开展逆回购操作",
            "tag": "流动性",
            "logic": "维护银行体系流动性合理充裕",
        }))
        overseas = format_mapping(map_article_to_assets({
            "title": "美联储隔夜逆回购余额回落",
            "tag": "美元流动性",
            "logic": "关注RRP与美债收益率",
        }))

        assert domestic["mapped_theme"] == "国内宽信用"
        assert overseas["mapped_theme"] == "美元流动性"

    def test_asset_mapping_keeps_central_bank_gold_buying_in_precious_metals(self):
        from core.asset_mapping import map_article_to_assets

        assert map_article_to_assets({
            "title": "央行购金持续，黄金储备连续增加",
            "tag": "宏观",
            "logic": "官方储备配置需求上升",
        })["mapped_theme"] == "贵金属"

    def test_asset_mapping_disambiguates_clean_energy_from_fossil_inflation(self):
        from core.asset_mapping import map_article_to_assets

        mapping = map_article_to_assets({
            "title": "《2026能源产业生态报告》发布，风电光伏装机超越火电",
            "tag": "能源政策",
            "logic": "核能和生物质能领域同步发展",
            "direction": "中性",
        })

        assert mapping["mapped_theme"] == "新能源建设"
        assert [item["name"] for item in mapping["security_candidates"][:3]] == [
            "金风科技", "运达股份", "明阳智能",
        ]
        assert all(
            item["name"] not in {"中国海油", "陕西煤业", "荣盛石化"}
            for item in mapping["security_candidates"]
        )
        assert all(item["direction"] == "看多" for item in mapping["security_candidates"])

    def test_generic_energy_word_does_not_trigger_fossil_stock_pool(self):
        from core.asset_mapping import map_article_to_assets

        generic = map_article_to_assets({
            "title": "能源产业生态报告正式发布",
            "tag": "能源政策",
            "logic": "报告介绍行业总体情况",
        })
        oil = map_article_to_assets({
            "title": "国际原油价格上涨，OPEC维持减产",
            "tag": "能源通胀",
            "logic": "油价上行",
        })

        assert generic["mapped_theme"] != "能源通胀"
        assert generic["security_candidates"] == []
        assert oil["mapped_theme"] == "能源通胀"

    def test_asset_mapping_records_explicit_indices_without_inventing_an_etf(self):
        from core.asset_mapping import map_article_to_assets

        mapping = map_article_to_assets({
            "title": "精选：可考虑上证50、沪深300等ETF",
            "tag": "股评",
        })

        assert mapping["security_candidates"] == []
        assert [item["ticker"] for item in mapping["market_references"][:2]] == [
            "000016", "000300",
        ]
        assert mapping["market_references"][-1]["asset_type"] == "fund_category"

    @pytest.mark.parametrize("article", [
        {"title": "某平台启动以旧换新活动提振消费", "tag": "公司动态", "logic": "促销活动"},
        {"title": "旅游旺季释放内需潜力", "tag": "行业观察", "logic": "客流回暖"},
    ])
    def test_asset_mapping_requires_policy_anchor_for_consumption_theme(self, article):
        from core.asset_mapping import map_article_to_assets

        assert map_article_to_assets(article)["mapped_theme"] != "扩内需政策"

    def test_asset_mapping_ignores_generic_or_negated_liquidity_mentions(self):
        from core.asset_mapping import format_mapping, map_article_to_assets

        analysis = format_mapping(map_article_to_assets({
            "title": "四部门推进人工智能基础资源技术攻关",
            "tag": "产业政策",
            "logic": "属于AI产业政策，非宏观流动性事件",
        }))

        assert analysis["mapped_theme"] == "AI算力"
        assert "美元指数" not in analysis["assets"]

    def test_local_briefing_filters_omnibus_theme_contamination(self):
        from routes.dashboard_routes import _local_briefing_from_articles

        articles = [
            {
                "title": "财经早知道｜中俄发表联合声明：致力维护和加强全球战略平衡与稳定",
                "raw_title": "财经早知道｜中俄发表联合声明：致力维护和加强全球战略平衡与稳定",
                "tag": "AI算力",
                "score": 7.5,
                "logic": "英伟达新增800亿美元回购，AI算力需求强劲",
                "summary": "早上好，今天是5月21日星期四。24小时热点：中俄发表联合声明。商务部解读中美经贸磋商。英伟达Q1营收同比增长85%。个税、印花税大涨。",
                "created_at": 100,
                "link": "http://example.com/omnibus",
            },
            {
                "title": "英伟达营收与利润高速增长，验证全球AI算力需求持续爆发",
                "tag": "AI算力",
                "score": 9.0,
                "logic": "强化GPU、服务器、HBM、光模块等产业链高景气预期",
                "summary": "AI算力需求持续增长",
                "created_at": 90,
                "link": "http://example.com/nvidia",
            },
        ]

        result = _local_briefing_from_articles(articles)
        all_titles = [a["title"] for theme in result["themes"] for a in theme["articles"]]
        assert not any("中俄发表联合声明" in title for title in all_titles)
        assert any("AI" in theme["theme"] for theme in result["themes"])

    def test_local_briefing_canonicalizes_themes_and_prioritizes_strong_signals(self, monkeypatch):
        import routes.dashboard_routes as dashboard

        monkeypatch.setattr(dashboard, "_cfg", lambda: {
            "briefing_themes": [
                {"name": "AI 算力链", "keywords": ["AI", "算力", "芯片"]},
                {"name": "市场情绪与龙虎榜", "keywords": ["龙虎榜", "涨停"]},
            ]
        })
        articles = [
            {
                "title": "扩大消费“十五五”规划，国务院重磅！",
                "tag": "国家级政策", "score": 9.2, "logic": "扩大消费",
                "direction": "看多",
                "created_at": 100, "link": "https://www.gelonghui.com/p/1",
                "feed_url": "http://rsshub:1200/gelonghui/news",
            },
            {
                "title": "扩大消费规划：推动全国一体化算力网建设",
                "tag": "国家级政策", "score": 8.0, "logic": "消费规划细则",
                "direction": "中性",
                "created_at": 110, "link": "https://wallstreetcn.com/livenews/1",
                "feed_url": "http://rsshub:1200/wallstreetcn/live",
            },
            {
                "title": "科创50、深成指跌逾4%，近630股跌超9%",
                "tag": "极端行情", "score": 9.0, "logic": "流动性负反馈",
                "created_at": 120, "link": "https://api3.cls.cn/share/article/1",
                "feed_url": "http://rsshub:1200/cls/telegraph",
            },
            {
                "title": "A股收盘跌超2%，九丰能源逆势涨停",
                "tag": "市场极端下行", "score": 8.5, "logic": "全市场普跌",
                "created_at": 130, "link": "https://wallstreetcn.com/livenews/2",
                "feed_url": "http://rsshub:1200/wallstreetcn/live",
            },
            {
                "title": "AI服务器订单上修",
                "tag": "AI算力", "score": 8.0, "logic": "算力景气改善",
                "created_at": 140, "link": "https://example.com/ai",
                "feed_url": "https://example.com/feed",
            },
        ]

        result = dashboard._local_briefing_from_articles(articles)
        themes = {theme["theme"]: theme for theme in result["themes"]}

        assert list(themes)[:2] == ["扩内需政策", "A股极端行情"]
        assert themes["A股极端行情"]["direction"] == "看空"
        assert themes["A股极端行情"]["source_count"] == 2
        assert "美元指数" not in themes["A股极端行情"]["market_impact"]
        assert "市场情绪与龙虎榜" not in themes
        assert result["overall_assessment"].count("扩内需政策") == 1
        assert "市场风险" in result["market_impact"]

    def test_local_briefing_counts_sources_surviving_exact_event_dedupe(self, monkeypatch):
        import routes.dashboard_routes as dashboard

        monkeypatch.setattr(dashboard, "_cfg", lambda: {"briefing_themes": []})
        base = {
            "title": "国务院发布扩大消费十五五规划",
            "tag": "国家级政策", "logic": "提振消费政策", "direction": "看多",
            "score": 9.0, "created_at": 100,
        }
        result = dashboard._local_briefing_from_articles([
            dict(base, link="https://www.gelonghui.com/p/1", feed_url="http://rsshub:1200/gelonghui/news"),
            dict(base, score=8.8, created_at=101, link="https://wallstreetcn.com/livenews/1", feed_url="http://127.0.0.1:1200/wallstreetcn/live"),
        ])
        theme = result["themes"][0]
        assert theme["theme"] == "扩内需政策"
        assert theme["source_count"] == 2
        assert theme["signal_count"] == 2
        assert theme["source_confirmation"] == (
            "头部事件覆盖 2 个入选发布方；转载谱系未核验；"
            "主题整体覆盖 2 个入选发布方"
        )
        assert "交叉验证" not in theme["insight"]


class TestScorer:
    """测试评分器（不调用真实 LLM）"""

    def test_scorer_singleton(self):
        """Scorer 是单例"""
        from core.scorer import Scorer
        s1 = Scorer()
        s2 = Scorer()
        assert s1 is s2


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
