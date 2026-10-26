from datetime import date, datetime, timedelta
import re
from zoneinfo import ZoneInfo

import pytest

from db import database
from db.event_core import merge_events, resolve_or_create_event
from core.processor import process_entry
from services import backtest
from utils.helpers import FeedEntry


TZ = ZoneInfo("Asia/Shanghai")


def _ts(day: date, hour: int = 10) -> int:
    return int(datetime(day.year, day.month, day.day, hour, tzinfo=TZ).timestamp())


def _signal(
    link: str,
    *,
    available_at: int,
    score: float = 9.1,
    selected: bool = True,
    event_key: str | None = None,
    score_path: str = "stage2",
    similarity: bool = False,
    aggregation_threshold: float = 8.0,
    revision_key: str = "base",
    signal_stage: str = "base",
    mapping_version: str = "asset-map:test",
    strategy_version: str = "strategy-v1",
) -> dict:
    return {
        "event_key": event_key or link,
        "independence_key": event_key or link,
        "link": link,
        "feed_url": "feed-a",
        "available_at": available_at,
        "scored_at": available_at,
        "published_at": available_at - 60,
        "score": score,
        "direction": "看多",
        "score_confidence": 0.7,
        "strategy_version": strategy_version,
        "mapping_version": mapping_version,
        "mapped_theme": "AI算力",
        "backtest_symbol": "sz399006",
        "score_path": score_path,
        "revision_key": revision_key,
        "signal_stage": signal_stage,
        "aggregation_threshold": aggregation_threshold,
        "selected": selected,
        "is_similarity_copy": similarity,
    }


@pytest.fixture(autouse=True)
def _pin_score_backtest_mapping_contract(monkeypatch):
    monkeypatch.setattr(backtest, "get_mapping_version", lambda: "asset-map:test")


def test_score_backtest_isolates_the_exact_mapping_contract(monkeypatch):
    database.init_db()
    now = int(datetime.now(TZ).timestamp())
    assert database.store_backtest_signal(
        _signal(
            "https://example.com/old-mapping",
            available_at=now,
            score=9.2,
            mapping_version="asset-map:old",
        )
    )
    assert database.store_backtest_signal(
        _signal(
            "https://example.com/current-mapping",
            available_at=now + 1,
            score=6.5,
        )
    )
    monkeypatch.setattr(
        backtest,
        "load_config",
        lambda: {"scoring_strategy_version": "strategy-v1"},
    )

    old_rows = database.get_backtest_signals(
        cutoff=0,
        strategy_version="strategy-v1",
        mapping_version="asset-map:old",
        min_score=0,
    )
    current_rows = database.get_backtest_signals(
        cutoff=0,
        strategy_version="strategy-v1",
        mapping_version="asset-map:test",
        min_score=0,
    )
    assert [row["link"] for row in old_rows] == [
        "https://example.com/old-mapping"
    ]
    assert [row["link"] for row in current_rows] == [
        "https://example.com/current-mapping"
    ]
    assert database.get_backtest_signal_stats(
        "strategy-v1", "asset-map:old"
    )["count"] == 1
    assert database.get_backtest_signal_stats(
        "strategy-v1", "asset-map:test"
    )["count"] == 1

    result = backtest.run_score_backtest(days=1, min_score=0)

    assert result["strategy_version"] == "strategy-v1"
    assert result["mapping_version"] == "asset-map:test"
    assert result["data_source"] == "signal_snapshots"
    assert result["scored_signal_samples"] == 1
    assert result["raw_samples"] == 1
    assert sum(row["samples"] for row in result["buckets"]) == 1
    assert result["cohort_history"]["current_mapping_version"] == "asset-map:test"
    assert result["cohort_history"]["current_scored_signals"] == 1
    assert result["cohort_history"]["family_scored_signals"] == 1


def test_signal_cohort_history_keeps_exact_fingerprint_separate_from_family():
    database.init_db()
    now = int(datetime.now(TZ).timestamp())
    exact = "2026-05-19-v2:current"
    archived = "2026-05-19-v2:archived"

    safe_neutral = _signal(
        "https://example.com/current-safe-neutral",
        available_at=now - 5,
        score=4.0,
        selected=False,
        score_path="llm_safe_neutral_contract",
        strategy_version=exact,
    )
    safe_neutral.update(
        event_type="其他",
        logic="来源未显示明确投资催化",
        direction="中性",
        score_confidence=0.0,
        mapped_theme="其他",
        backtest_symbol=None,
    )

    rows = [
        _signal(
            "https://example.com/current-selected",
            available_at=now - 70,
            strategy_version=exact,
        ),
        _signal(
            "https://example.com/current-unselected",
            available_at=now - 60,
            score=6.0,
            selected=False,
            strategy_version=exact,
        ),
        _signal(
            "https://example.com/archived-selected",
            available_at=now - 50,
            strategy_version=archived,
        ),
        _signal(
            "https://example.com/archived-unselected",
            available_at=now - 40,
            score=6.0,
            selected=False,
            strategy_version=archived,
        ),
        _signal(
            "https://example.com/shared-revision",
            available_at=now - 30,
            strategy_version=archived,
        ),
        _signal(
            "https://example.com/shared-revision",
            available_at=now - 20,
            score=6.0,
            selected=False,
            strategy_version=exact,
        ),
        _signal(
            "https://example.com/current-similarity",
            available_at=now - 10,
            score_path="similarity",
            similarity=True,
            strategy_version=exact,
        ),
        safe_neutral,
    ]
    for row in rows:
        assert database.store_backtest_signal(row) is True

    history = backtest._signal_cohort_history(
        exact,
        "asset-map:test",
        cutoff=now - 3600,
        evaluation_at=now,
    )

    assert history["version_count"] == 2
    assert history["current_scored_signals"] == 3
    assert history["current_selected_signals"] == 1
    assert history["current_unselected_signals"] == 2
    assert history["family_scored_signals"] == 5
    assert history["family_selected_signals"] == 2
    assert history["family_unselected_signals"] == 3
    assert history["archived_selected_signals"] == 1
    assert history["archived_version_count"] == 1


def test_latest_signal_score_path_isolated_by_exact_strategy():
    database.init_db()
    now = int(datetime.now(TZ).timestamp())
    link = "https://example.com/frozen-ledger-path"
    archived = "2026-05-19-v2:archived"
    current = "2026-05-19-v2:current"

    assert database.store_backtest_signal(
        _signal(
            link,
            available_at=now,
            strategy_version=archived,
        )
    ) is True

    assert database.get_latest_signal_score_path(link, archived) == "stage2"
    assert database.get_latest_signal_score_path(link, current) == ""


def test_existing_other_contract_does_not_enable_legacy_cache(monkeypatch):
    database.init_db()
    now = int(datetime.now(TZ).timestamp())
    assert database.store_backtest_signal(
        _signal(
            "https://example.com/old-contract-only",
            available_at=now,
            mapping_version="asset-map:old",
        )
    )
    monkeypatch.setattr(
        backtest,
        "load_config",
        lambda: {"scoring_strategy_version": "strategy-v1"},
    )
    monkeypatch.setattr(
        backtest,
        "_load_articles",
        lambda **_kwargs: pytest.fail("legacy cache must not cross contract boundaries"),
    )

    result = backtest.run_score_backtest(days=1)

    assert result["data_source"] == "signal_snapshots"
    assert result["mapping_version"] == "asset-map:test"
    assert result["scored_signal_samples"] == 0
    assert result["raw_samples"] == 0
    assert result["history_status"] == "accumulating"


def test_production_backtest_independently_settles_recorded_unselected_control(
    monkeypatch,
):
    signal_at = _ts(date(2026, 7, 10), 10)
    t1 = date(2026, 7, 13)
    clock = {"now": signal_at}
    monkeypatch.setattr(database.time, "time", lambda: clock["now"])
    monkeypatch.setattr(
        backtest,
        "load_config",
        lambda: {"scoring_strategy_version": "strategy-v1"},
    )
    database.init_db()
    assert database.store_backtest_signal(
        _signal("selected-control-pair", available_at=signal_at, selected=True)
    )
    clock["now"] = signal_at + 1
    assert database.store_backtest_signal(
        _signal(
            "unselected-control-pair",
            available_at=signal_at + 1,
            score=5.0,
            selected=False,
        )
    )
    clock["now"] = _ts(t1, 16)
    database.store_future_prices(
        [
            {
                "symbol": "sz399006",
                "trade_date": t1.isoformat(),
                "close_price": 2520,
                "change_amt": 20,
                "change_pct": 0.8,
            },
            {
                "symbol": "sh000001",
                "trade_date": t1.isoformat(),
                "close_price": 3210,
                "change_amt": 10,
                "change_pct": 0.3,
            },
        ]
    )

    result = backtest.run_score_backtest(days=30)
    control = result["control_cohort"]
    funnel = result["eligibility_funnel"]

    assert result["raw_samples"] == 1
    assert result["scored_signal_samples"] == 2
    assert control["strategy_version"] == "strategy-v1"
    assert control["mapping_version"] == "asset-map:test"
    assert control["cohort_overlap"] == 0
    assert control["selected"]["t1_settled_event_results"] == 1
    assert control["unselected"]["recorded_signals"] == 1
    assert control["unselected"]["t1_settled_event_results"] == 1
    assert control["ready_for_tuning"] is False
    assert result["optimization"]["control_cohort_ready"] is False
    assert funnel["ledger"]["current_contract_scored"] == 2
    assert funnel["ledger"]["current_contract_selected"] == 1
    assert funnel["ledger"]["current_contract_unselected"] == 1
    assert funnel["control_analysis"]["t1_settled_event_results"] == 1
    html = backtest.render_backtest_html(result)
    assert "样本资格漏斗" in html
    assert "未入选独立对照" in html
    assert "独立对照结算中" in html


def test_signal_and_cache_write_are_atomic_idempotent_and_signal_survives_cleanup():
    database.init_db()
    old = int((datetime.now(TZ) - timedelta(days=8)).timestamp())
    snapshot = _signal("https://example.com/a", available_at=int(datetime.now(TZ).timestamp()))

    database.set_cached_entry(
        snapshot["link"],
        "title",
        "raw",
        9.1,
        "AI算力",
        "logic",
        score_path=snapshot["score_path"],
        strategy_version="strategy-v1",
        created_at=old,
        backtest_signal=snapshot,
    )
    # A repeated refresh is idempotent and cannot rewrite the original signal.
    database.set_cached_entry(
        snapshot["link"],
        "title-2",
        "raw",
        9.9,
        "AI算力",
        "logic-2",
        score_path=snapshot["score_path"],
        strategy_version="strategy-v1",
        created_at=old,
        backtest_signal={**snapshot, "score": 9.9},
    )
    rows = database.get_backtest_signals(
        cutoff=0,
        strategy_version="strategy-v1",
        min_score=0,
    )
    assert len(rows) == 1
    assert rows[0]["score"] == 9.1

    with database.db_lock:
        with database.get_connection() as conn:
            database.cleanup_old_entries(conn, {"threshold": {"pass": 7}})
    with database.get_connection() as conn:
        assert conn.execute(
            "SELECT 1 FROM news_cache WHERE link = ?", (snapshot["link"],)
        ).fetchone() is None
        assert conn.execute(
            "SELECT COUNT(*) FROM backtest_signals"
        ).fetchone()[0] == 1


def test_backtest_reads_canonicalize_merge_without_rewriting_frozen_event_id():
    database.init_db()
    link = "https://example.com/stable-event-merge"
    available_at = 1_700_000_000
    assert database.store_backtest_signal(
        _signal(link, available_at=available_at, score=9.2)
    )

    with database.get_connection() as conn:
        stored_before = conn.execute(
            "SELECT event_id, event_version, independence_key "
            "FROM backtest_signals WHERE link = ?",
            (link,),
        ).fetchone()
        source_id = str(stored_before[0])
        target_id = resolve_or_create_event(
            conn,
            {"manual": "stable-event-merge-target"},
            title="合并后的权威事件",
            event_type="news",
            direction="看多",
            priority=9.2,
            occurred_at=available_at,
            available_at=available_at,
            strategy_version="strategy-v1",
            model_version="test",
            now=available_at,
        )
        merge_events(
            conn,
            source_id,
            target_id,
            reason="same stable event",
            actor="test",
            now=available_at + 1,
        )

    rows = database.get_backtest_signals(
        cutoff=0,
        strategy_version="strategy-v1",
        min_score=0,
    )
    assert len(rows) == 1
    assert rows[0]["event_id"] == target_id
    assert rows[0]["recorded_event_id"] == source_id
    assert rows[0]["independence_key"] == target_id

    states = database.get_backtest_signal_states([link], "strategy-v1")
    assert states[link]["latest"]["event_id"] == target_id
    assert states[link]["latest"]["recorded_event_id"] == source_id
    cluster = database.get_backtest_cluster_signal_states(
        [target_id], "strategy-v1"
    )
    assert cluster[target_id][link]["latest"]["event_id"] == target_id

    with database.get_connection() as conn:
        stored_after = conn.execute(
            "SELECT event_id, event_version, independence_key "
            "FROM backtest_signals WHERE link = ?",
            (link,),
        ).fetchone()
    assert tuple(stored_after) == tuple(stored_before)


def test_existing_first_generation_signal_table_adds_revision_columns():
    with database.get_connection() as conn:
        conn.execute("""
            CREATE TABLE backtest_signals (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                signal_key TEXT NOT NULL,
                event_key TEXT DEFAULT '',
                link TEXT NOT NULL,
                feed_url TEXT DEFAULT '',
                available_at INTEGER NOT NULL,
                scored_at INTEGER NOT NULL,
                published_at INTEGER,
                recorded_at INTEGER NOT NULL,
                score REAL NOT NULL,
                direction TEXT NOT NULL DEFAULT '中性',
                score_confidence REAL DEFAULT 0,
                strategy_version TEXT DEFAULT '',
                mapping_version TEXT DEFAULT '',
                mapped_theme TEXT DEFAULT '',
                backtest_symbol TEXT,
                score_path TEXT DEFAULT '',
                aggregation_threshold REAL NOT NULL,
                selected INTEGER NOT NULL DEFAULT 0,
                is_similarity_copy INTEGER NOT NULL DEFAULT 0
            )
        """)
        conn.commit()

    database.init_db()

    with database.get_connection() as conn:
        columns = {
            row[1]: row for row in conn.execute("PRAGMA table_info(backtest_signals)")
        }
    assert columns["revision_key"][4] == "'base'"
    assert columns["signal_stage"][4] == "'base'"
    assert columns["revision_seq"][4] == "0"
    assert columns["revision_state_key"][4] == "''"
    assert columns["independence_key"][4] == "''"


def test_invalid_snapshot_rolls_back_cache_write():
    database.init_db()
    snapshot = _signal("https://example.com/atomic", available_at=1_700_000_000)
    with pytest.raises(ValueError, match="finite"):
        database.set_cached_entry(
            snapshot["link"],
            "title",
            "raw",
            9.1,
            "AI算力",
            "logic",
            score_path=snapshot["score_path"],
            backtest_signal={**snapshot, "aggregation_threshold": float("nan")},
        )
    with database.get_connection() as conn:
        assert conn.execute(
            "SELECT 1 FROM news_cache WHERE link = ?", (snapshot["link"],)
        ).fetchone() is None
        assert conn.execute("SELECT COUNT(*) FROM backtest_signals").fetchone()[0] == 0


def test_stored_selection_is_used_and_similarity_copies_are_excluded():
    database.init_db()
    ts = 1_700_000_000
    database.store_backtest_signal(
        _signal("selected", available_at=ts, score=8.1, selected=True)
    )
    database.store_backtest_signal(
        _signal("rejected", available_at=ts + 1, score=7.9, selected=False)
    )
    database.store_backtest_signal(
        _signal(
            "copied",
            available_at=ts + 2,
            score=9.9,
            selected=True,
            score_path="similarity",
            similarity=True,
        )
    )

    default_rows = database.get_backtest_signals(
        cutoff=0,
        strategy_version="strategy-v1",
    )
    assert [row["link"] for row in default_rows] == ["selected"]
    explicit_rows = database.get_backtest_signals(
        cutoff=0,
        strategy_version="strategy-v1",
        min_score=7.0,
    )
    assert [row["link"] for row in explicit_rows] == ["selected", "rejected"]


def test_processor_keeps_rejected_scored_population_when_article_cache_is_disabled():
    database.init_db()
    published_at = int((datetime.now(TZ) - timedelta(hours=2)).timestamp())
    entry = FeedEntry(
        "普通公司营销稿",
        "https://example.com/rejected",
        published_parsed=datetime.fromtimestamp(published_at, TZ).utctimetuple(),
    )
    cfg = {
        "_blacklist_re": None,
        "blacklist": [],
        "threshold": {"pass": 7.0, "notify": 9.0, "similarity": 0.8},
        "fast_pass": {"enabled": False},
        "local_reject_rules": {
            "enabled": True,
            "rules": [{
                "pattern": "营销稿",
                "score": 2.0,
                "tag": "本地拒绝",
                "logic": "营销内容",
            }],
        },
        "time_weight": {"enabled": False},
        "quiet_mode": {"enabled": False},
        "cache_rejected_entries": False,
        "scoring_strategy_version": "strategy-v1",
        "feeds": [],
    }

    result, reason = process_entry(entry, [], "prompt", cfg)

    assert result is None
    assert reason == "filtered"
    with database.get_connection() as conn:
        assert conn.execute("SELECT COUNT(*) FROM news_cache").fetchone()[0] == 0
    rows = database.get_backtest_signals(
        cutoff=0,
        strategy_version="strategy-v1",
        min_score=0,
    )
    assert len(rows) == 1
    assert rows[0]["score"] == 2.0
    assert rows[0]["selected"] == 0
    assert rows[0]["aggregation_threshold"] == 7.0
    assert rows[0]["score_path"] == "local_reject"
    assert rows[0]["published_at"] == published_at
    assert rows[0]["available_at"] >= rows[0]["scored_at"] > published_at
    assert rows[0]["mapping_version"].startswith("asset-map:")


def test_backtest_prefers_atomic_snapshots_and_reports_actual_denominators(monkeypatch):
    database.init_db()
    signal_day = date(2026, 7, 13)
    t1 = date(2026, 7, 14)
    t2 = date(2026, 7, 15)
    signal_ts = _ts(signal_day)
    clock = {"now": signal_ts}
    monkeypatch.setattr(database.time, "time", lambda: clock["now"])
    monkeypatch.setattr(
        backtest,
        "load_config",
        lambda: {"scoring_strategy_version": "strategy-v1"},
    )

    database.store_backtest_signal(
        _signal("first", available_at=signal_ts, event_key="same-event")
    )
    first_identity = database.get_backtest_signals(
        cutoff=0, strategy_version="strategy-v1", min_score=0
    )[0]
    clock["now"] = signal_ts + 120
    # Same event/proxy is not an independent signal; the first observation wins.
    database.store_backtest_signal(
        {
            **_signal("duplicate", available_at=signal_ts + 60, event_key="same-event"),
            "event_id": first_identity["event_id"],
            "event_version": first_identity["event_version"],
        }
    )
    database.store_backtest_signal(
        _signal(
            "similarity",
            available_at=signal_ts + 120,
            event_key="another-event",
            score_path="similarity",
            similarity=True,
        )
    )
    database.store_future_prices([
        {
            "symbol": "sz399006", "trade_date": signal_day.isoformat(),
            "close_price": 2500, "change_amt": 10, "change_pct": 0.4,
            "ts": _ts(signal_day, 15),
        },
        {
            "symbol": "sz399006", "trade_date": t1.isoformat(),
            "close_price": 2520, "change_amt": 20, "change_pct": 0.8,
            "ts": _ts(t1, 15),
        },
        {
            "symbol": "sz399006", "trade_date": t2.isoformat(),
            "close_price": 2510, "change_amt": -10, "change_pct": -0.4,
            "ts": _ts(t2, 15),
        },
        {
            "symbol": "sh000001", "trade_date": signal_day.isoformat(),
            "close_price": 3200, "change_amt": 5, "change_pct": 0.2,
            "ts": _ts(signal_day, 15),
        },
        {
            "symbol": "sh000001", "trade_date": t1.isoformat(),
            "close_price": 3210, "change_amt": 10, "change_pct": 0.3,
            "ts": _ts(t1, 15),
        },
        {
            "symbol": "sh000001", "trade_date": t2.isoformat(),
            "close_price": 3190, "change_amt": -20, "change_pct": -0.6,
            "ts": _ts(t2, 15),
        },
    ])

    clock["now"] = _ts(t2, 16)
    result = backtest.run_score_backtest(days=30)
    bucket = result["buckets"][0]

    assert result["data_source"] == "signal_snapshots"
    assert result["mode"] == "realized_market_direction"
    assert result["validation_mode"] == "historical_association"
    assert result["history_status"] == "accumulating"
    assert result["actual_start_date"] == signal_day.isoformat()
    assert result["actual_end_date"] == signal_day.isoformat()
    assert result["actual_window_days"] == 1
    assert result["selection_policy"] == "stored_aggregation_threshold"
    assert result["scored_signal_samples"] == 2
    assert result["raw_samples"] == 2  # similarity copy excluded before loading
    assert result["deduped_samples"] == 1
    assert result["dedupe_removed"] == 1
    assert result["proxy_coverage"] == 100.0
    assert bucket["asset_t1_direction_samples"] == 1
    assert bucket["asset_t1_direction_unique_days"] == 1
    assert bucket["asset_t1_direction_win_rate"] == 100.0
    assert bucket["asset_t1_direction_win_rate_ci95"] == {
        "lower": 20.7,
        "upper": 100.0,
    }
    assert bucket["asset_t1_direction_sample_sufficient"] is False
    assert bucket["t1_signal_return_samples"] == 1


def test_final_radar_revision_supersedes_base_without_compounding_population():
    database.init_db()
    now = int(datetime.now(TZ).timestamp())
    link = "https://example.com/radar-final"
    database.store_backtest_signal(
        _signal(
            link,
            available_at=now,
            score=6.2,
            selected=False,
            aggregation_threshold=6.5,
        )
    )
    database.store_backtest_signal(
        _signal(
            link,
            available_at=now + 1,
            score=6.8,
            selected=True,
            aggregation_threshold=6.5,
            score_path="radar",
            revision_key="radar:6.8:two-source",
            signal_stage="radar",
        )
    )

    rows = database.get_backtest_signals(
        cutoff=0,
        strategy_version="strategy-v1",
    )
    assert len(rows) == 1
    assert rows[0]["score"] == 6.8
    assert rows[0]["signal_stage"] == "radar"
    stats = database.get_backtest_signal_stats("strategy-v1")
    assert stats["count"] == 1
    assert stats["revision_count"] == 2
    assert database.get_backtest_base_scores([link], "strategy-v1") == {link: 6.2}


def test_auto_revision_records_a_b_a_transition_but_dedupes_unchanged_state():
    database.init_db()
    now = int(datetime.now(TZ).timestamp())
    link = "https://example.com/radar-transition"
    database.store_backtest_signal(
        _signal(
            link,
            available_at=now,
            score=6.2,
            selected=False,
            aggregation_threshold=7.0,
        )
    )

    def radar(score: float, offset: int) -> dict:
        return {
            **_signal(
                link,
                available_at=now + offset,
                score=score,
                selected=score >= 7.0,
                aggregation_threshold=7.0,
                score_path="radar",
                revision_key="radar:auto",
                signal_stage="radar",
            ),
            "auto_revision": True,
        }

    assert database.store_backtest_signal(radar(6.7, 1)) is True
    assert database.store_backtest_signal(radar(7.2, 2)) is True
    assert database.store_backtest_signal(radar(6.7, 3)) is True
    assert database.store_backtest_signal(radar(6.7, 4)) is False

    rows = database.get_backtest_signals(
        cutoff=0,
        strategy_version="strategy-v1",
        min_score=0,
    )
    assert len(rows) == 1
    assert rows[0]["score"] == 6.7
    assert rows[0]["revision_seq"] == 3
    stats = database.get_backtest_signal_stats("strategy-v1")
    assert stats["count"] == 1
    assert stats["revision_count"] == 4


def test_backtest_covers_low_score_buckets_and_separates_parse_errors(monkeypatch):
    database.init_db()
    now = int(datetime.now(TZ).timestamp())
    monkeypatch.setattr(
        backtest,
        "load_config",
        lambda: {"scoring_strategy_version": "strategy-v1"},
    )
    database.store_backtest_signal(
        _signal(
            "https://example.com/selected-6x",
            available_at=now,
            score=6.5,
            selected=True,
            aggregation_threshold=6.0,
        )
    )
    database.store_backtest_signal(
        _signal(
            "https://example.com/rejected-low",
            available_at=now + 1,
            score=4.5,
            selected=False,
            aggregation_threshold=6.0,
        )
    )
    database.store_backtest_signal(
        _signal(
            "https://example.com/parse-error",
            available_at=now + 2,
            score=-1.0,
            selected=False,
            aggregation_threshold=6.0,
            score_path="parse_error",
        )
    )

    default_result = backtest.run_score_backtest(days=1)
    assert next(
        row for row in default_result["buckets"] if row["bucket"] == "6.0-6.9"
    )["samples"] == 1
    explicit_result = backtest.run_score_backtest(days=1, min_score=-1)
    assert next(
        row for row in explicit_result["buckets"] if row["bucket"] == "0.0-5.9"
    )["samples"] == 1
    assert explicit_result["invalid_score_samples"] == 1
    assert sum(row["samples"] for row in explicit_result["buckets"]) == 2


def test_wire_dedupe_is_bounded_and_preserves_next_day_recurrence():
    start = _ts(date(2026, 7, 13))
    rows = [
        {
            "event_key": "same-headline",
            "link": "wire-a",
            "backtest_symbol": "sh000001",
            "strategy_version": "v1",
            "available_at": start,
        },
        {
            "event_key": "same-headline",
            "link": "wire-b",
            "backtest_symbol": "sh000001",
            "strategy_version": "v1",
            "available_at": start + 60,
        },
        {
            "event_key": "same-headline",
            "link": "wire-next-day",
            "backtest_symbol": "sh000001",
            "strategy_version": "v1",
            "available_at": start + 24 * 3600,
        },
    ]

    deduped = backtest._dedupe_snapshot_signals(rows)

    assert [row["link"] for row in deduped] == ["wire-a", "wire-next-day"]


def test_result_day_counts_actual_price_dates_and_blocks_same_day_pseudoreplication(monkeypatch):
    signal_ts = _ts(date(2026, 7, 11))  # Saturday
    articles = [
        {
            "link": f"signal-{idx}",
            "event_key": f"event-{idx}",
            "score": 9.1,
            "tag": "AI算力",
            "direction": "看多",
            "created_at": signal_ts,
            "available_at": signal_ts,
            "mapped_theme": "AI算力",
            "backtest_symbol": "sz399006",
            "strategy_version": "strategy-v1",
            "_signal_snapshot": True,
        }
        for idx in range(30)
    ]
    history = {
        "count": 30,
        "first_available_at": signal_ts,
        "collection_started_at": signal_ts,
        "data_source": "signal_snapshots",
    }
    monkeypatch.setattr(
        backtest,
        "load_config",
        lambda: {"scoring_strategy_version": "strategy-v1"},
    )
    monkeypatch.setattr(
        backtest,
        "_load_backtest_samples",
        lambda *_args, **_kwargs: (articles, history),
    )
    monkeypatch.setattr(backtest, "_same_day_snapshot", lambda *_args: None)
    monkeypatch.setattr(
        backtest,
        "_future_snapshot",
        lambda *_args: {
            "trade_date": "2026-07-13",
            "close_price": 100.0,
            "ts": _ts(date(2026, 7, 13), 15),
            "change_amt": 1.0,
            "change_pct": 0.5,
        },
    )

    result = backtest.run_score_backtest(days=30)
    bucket = result["buckets"][0]

    assert bucket["asset_t1_direction_samples"] == 30
    assert bucket["asset_t1_direction_unique_days"] == 1
    assert bucket["asset_t1_direction_sample_sufficient"] is False
    assert result["ci_method"] == "article_level_wilson_unadjusted_for_intraday_clustering"


def test_write_time_lifts_available_at_after_lock_wait(monkeypatch):
    database.init_db()
    commit_path_time = _ts(date(2026, 7, 13), hour=15)
    monkeypatch.setattr(database.time, "time", lambda: commit_path_time)
    database.store_backtest_signal(
        _signal(
            "https://example.com/lock-wait",
            available_at=commit_path_time - 1,
        )
    )

    row = database.get_backtest_signals(
        cutoff=0,
        strategy_version="strategy-v1",
        min_score=0,
    )[0]
    assert row["available_at"] == commit_path_time


def test_cleanup_test_records_removes_long_retained_signals():
    database.init_db()
    database.store_backtest_signal(
        _signal(
            "https://test.com/backtest-fixture",
            available_at=int(datetime.now(TZ).timestamp()),
        )
    )

    removed = database.cleanup_test_records()

    assert removed["backtest_signals"] == 1
    assert database.get_backtest_signal_stats("strategy-v1")["count"] == 0


def test_signal_retention_is_bounded_by_complete_chain_and_runs_once_daily():
    database.init_db()
    now = int(datetime.now(TZ).timestamp())
    database.store_backtest_signal(_signal("expired", available_at=now))
    database.store_backtest_signal(_signal("fresh", available_at=now + 1))
    with database.get_connection() as conn:
        conn.execute(
            "UPDATE backtest_signals SET available_at = ? WHERE link = 'expired'",
            (now - 401 * 86400,),
        )
        conn.commit()
    with database.db_lock:
        with database.get_connection() as conn:
            database.cleanup_old_entries(
                conn,
                {
                    "threshold": {"pass": 7.0},
                    "backtest": {"signal_retention_days": 400},
                },
            )
    assert [
        row["link"]
        for row in database.get_backtest_signals(
            cutoff=0,
            strategy_version="strategy-v1",
            min_score=0,
        )
    ] == ["fresh"]

    database.store_backtest_signal(_signal("expired-after-daily-run", available_at=now + 2))
    with database.get_connection() as conn:
        conn.execute(
            "UPDATE backtest_signals SET available_at = ? "
            "WHERE link = 'expired-after-daily-run'",
            (now - 401 * 86400,),
        )
        conn.commit()
    with database.db_lock:
        with database.get_connection() as conn:
            database.cleanup_old_entries(
                conn,
                {"threshold": {"pass": 7.0}, "backtest": {"signal_retention_days": 400}},
            )
    assert database.get_backtest_signal_stats("strategy-v1")["count"] == 2


def test_signal_retention_preserves_complete_bridge_referenced_chain():
    database.init_db()
    now = int(datetime.now(TZ).timestamp())
    database.store_backtest_signal(
        _signal("bridged-expired", available_at=now, revision_key="base")
    )
    database.store_backtest_signal(
        _signal(
            "bridged-expired",
            available_at=now + 1,
            revision_key="revision-1",
            signal_stage="mapping_update",
        )
    )
    database.store_backtest_signal(_signal("unbridged-expired", available_at=now + 2))
    expired_at = now - 401 * 86400
    with database.get_connection() as conn:
        conn.execute(
            "UPDATE backtest_signals SET available_at = ? "
            "WHERE link IN ('bridged-expired', 'unbridged-expired')",
            (expired_at,),
        )
        referenced = conn.execute(
            "SELECT id, signal_key, selected FROM backtest_signals "
            "WHERE link = 'bridged-expired' ORDER BY id LIMIT 1"
        ).fetchone()
        conn.commit()

    run_id = "a" * 64
    database.store_score_bridge_preview(
        {
            "run_id": run_id,
            "source_strategy_version": "strategy-v1",
            "target_strategy_version": "strategy-v2",
            "source_mapping_version": "asset-map:test",
            "target_mapping_version": "asset-map:test",
            "policy_version": "test-policy-v1",
            "mode": "preview_only",
            "approval_status": "pending",
            "approved_at": None,
            "approved_by": "",
            "created_at": now,
            "artifact_name": "test-artifact.sqlite3",
            "artifact_sha256": "b" * 64,
            "artifact_schema_version": "test-v1",
            "source_row_count": 1,
            "eligible_count": 1,
            "ineligible_count": 0,
            "unknown_count": 0,
            "manifest_hash": "c" * 64,
        },
        [
            {
                "run_id": run_id,
                "ledger_signal_id": int(referenced["id"]),
                "source_signal_key": str(referenced["signal_key"]),
                "source_available_at": expired_at,
                "selected": int(referenced["selected"]),
                "eligibility": "eligible",
                "reason_code": "test_verified",
                "stage_metadata_json": "{}",
                "output_hash": "d" * 64,
                "evidence_hash": "e" * 64,
            }
        ],
    )

    with database.db_lock:
        with database.get_connection() as conn:
            database.cleanup_old_entries(
                conn,
                {
                    "threshold": {"pass": 7.0},
                    "backtest": {"signal_retention_days": 400},
                },
            )

    with database.get_connection() as conn:
        rows = conn.execute(
            "SELECT link, COUNT(*) AS revisions FROM backtest_signals "
            "GROUP BY link ORDER BY link"
        ).fetchall()
        assert [(row["link"], row["revisions"]) for row in rows] == [
            ("bridged-expired", 2)
        ]
        assert conn.execute("PRAGMA foreign_key_check").fetchall() == []


def test_legacy_cache_is_explicitly_labelled_and_never_claims_full_window(monkeypatch):
    database.init_db()
    rows = [{
        "link": "legacy",
        "title": "legacy",
        "score": 9.0,
        "tag": "AI算力",
        "direction": "看多",
        "created_at": _ts(date(2026, 7, 13)),
    }]
    monkeypatch.setattr(backtest, "load_config", lambda: {})
    monkeypatch.setattr(backtest, "_load_articles", lambda **_kwargs: rows)
    monkeypatch.setattr(backtest, "dedupe_articles", lambda values: values)

    result = backtest.run_score_backtest(days=90)

    assert result["validation_mode"] == "legacy_accumulating"
    assert result["history_status"] == "legacy"
    assert result["data_source"] == "legacy_news_cache"
    assert result["requested_days"] == 90
    assert result["actual_window_days"] == 1
    assert "不代表请求的完整历史窗口" in result["note"]


def test_safe_neutral_ledger_does_not_hide_substantive_legacy_population():
    database.init_db()
    now = int(datetime.now(TZ).timestamp())
    safe = _signal(
        "https://example.com/safe-only-ledger",
        available_at=now,
        score=4.0,
        selected=False,
        score_path="llm_safe_neutral_grounding",
    )
    safe.update(
        direction="中性",
        score_confidence=0.0,
        mapped_theme="其他",
        event_type="其他",
        logic="来源未显示明确投资催化",
        backtest_symbol=None,
        aggregation_threshold=7.0,
    )
    assert database.store_backtest_signal(safe) is True
    with database.get_connection() as conn:
        conn.execute(
            "INSERT INTO news_cache "
            "(link, title, raw_title, score, tag, logic, created_at, feed_url, "
            "score_path, strategy_version, direction) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                "https://example.com/legacy-substantive",
                "legacy substantive",
                "legacy substantive",
                9.0,
                "AI算力",
                "算力需求改善",
                now,
                "feed-a",
                "llm",
                "strategy-v1",
                "看多",
            ),
        )
        conn.commit()

    cfg = {
        "scoring_strategy_version": "strategy-v1",
        "threshold": {"pass": 7.0},
    }
    articles, control, stats = backtest._load_backtest_samples(
        1,
        None,
        cfg,
        "asset-map:test",
    )

    assert stats["ledger_revision_count"] == 0
    assert stats["data_source"] == "legacy_news_cache"
    assert [row["link"] for row in articles] == [
        "https://example.com/legacy-substantive"
    ]
    assert control == []
    assert stats["cohort_history"]["current_scored_signals"] == 0


def test_cross_feed_cache_hit_atomically_upgrades_first_actionable_selection():
    database.init_db()
    now = int(datetime.now(TZ).timestamp())
    link = "https://example.com/cross-feed"
    snapshot = _signal(
        link,
        available_at=now - 60,
        score=7.5,
        selected=False,
        aggregation_threshold=8.0,
    )
    database.set_cached_entry(
        link,
        "[⭐7.5 📈 AI算力] 算力订单增长 💡需求改善",
        "算力订单增长",
        7.5,
        "AI算力",
        "需求改善",
        feed_url="feed-a",
        score_path="stage2",
        strategy_version="strategy-v1",
        direction="看多",
        backtest_signal=snapshot,
    )
    cfg = {
        "_blacklist_re": re.compile(r"$^"),
        "threshold": {"pass": 7.0, "notify": 9.0, "similarity": 0.8},
        "scoring_strategy_version": "strategy-v1",
        "feeds": [
            {
                "url": "feed-a",
                "pass_threshold": 8.0,
                "aggregation_threshold": 8.0,
            },
            {
                "url": "feed-b",
                "pass_threshold": 7.0,
                "aggregation_threshold": 7.0,
            },
        ],
    }
    entry = FeedEntry("算力订单增长", link)

    result, reason = process_entry(entry, [], "prompt", cfg, feed_url="feed-b")

    assert result is entry
    assert reason == "cache"
    rows = database.get_backtest_signals(
        cutoff=0,
        strategy_version="strategy-v1",
    )
    assert len(rows) == 1
    assert rows[0]["selected"] == 1
    assert rows[0]["signal_stage"] == "cache_selection"
    assert rows[0]["feed_url"] == "feed-b"
    assert rows[0]["aggregation_threshold"] == 7.0
    assert rows[0]["available_at"] > snapshot["available_at"]
    assert database.get_backtest_signal_stats("strategy-v1")["revision_count"] == 2
    # Repeated cache hits are idempotent instead of alternating feed-policy rows.
    process_entry(FeedEntry("算力订单增长", link), [], "prompt", cfg, feed_url="feed-b")
    assert database.get_backtest_signal_stats("strategy-v1")["revision_count"] == 2


def test_cross_feed_cache_hit_fails_closed_when_selection_revision_cannot_commit(
    monkeypatch,
):
    database.init_db()
    now = int(datetime.now(TZ).timestamp())
    link = "https://example.com/cross-feed-write-failure"
    snapshot = _signal(
        link,
        available_at=now - 60,
        score=7.5,
        selected=False,
        aggregation_threshold=8.0,
    )
    database.set_cached_entry(
        link,
        "[⭐7.5 📈 AI算力] 算力订单增长 💡需求改善",
        "算力订单增长",
        7.5,
        "AI算力",
        "需求改善",
        feed_url="feed-a",
        score_path="stage2",
        strategy_version="strategy-v1",
        direction="看多",
        backtest_signal=snapshot,
    )
    cfg = {
        "_blacklist_re": re.compile(r"$^"),
        "threshold": {"pass": 7.0, "notify": 9.0, "similarity": 0.8},
        "scoring_strategy_version": "strategy-v1",
        "feeds": [
            {
                "url": "feed-a",
                "pass_threshold": 8.0,
                "aggregation_threshold": 8.0,
            },
            {
                "url": "feed-b",
                "pass_threshold": 7.0,
                "aggregation_threshold": 7.0,
            },
        ],
    }
    monkeypatch.setattr(
        database.DB,
        "update_cached_entry_origin",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(OSError("disk full")),
    )

    with pytest.raises(OSError, match="disk full"):
        process_entry(
            FeedEntry("算力订单增长", link),
            [],
            "prompt",
            cfg,
            feed_url="feed-b",
        )

    rows = database.get_backtest_signals(
        cutoff=0,
        strategy_version="strategy-v1",
        min_score=0,
    )
    assert len(rows) == 1
    assert rows[0]["selected"] == 0
    assert database.get_backtest_signal_stats("strategy-v1")["revision_count"] == 1


def test_exact_link_cache_hit_never_resurrects_similarity_copy():
    database.init_db()
    now = int(datetime.now(TZ).timestamp())
    link = "https://example.com/similarity-copy"
    snapshot = _signal(
        link,
        available_at=now,
        score=9.0,
        selected=True,
        score_path="similarity",
        similarity=True,
    )
    database.set_cached_entry(
        link,
        "[⭐9.0 📈 AI算力] 同一事件转述",
        "同一事件转述",
        9.0,
        "AI算力",
        "复制已有评分",
        feed_url="feed-a",
        score_path="similarity",
        strategy_version="strategy-v1",
        direction="看多",
        backtest_signal=snapshot,
    )
    cfg = {
        "_blacklist_re": re.compile(r"$^"),
        "threshold": {"pass": 7.0, "notify": 9.0, "similarity": 0.8},
        "scoring_strategy_version": "strategy-v1",
        "feeds": [{"url": "feed-a"}],
    }

    result, reason = process_entry(
        FeedEntry("同一事件转述", link),
        [],
        "prompt",
        cfg,
        feed_url="feed-a",
    )

    assert result is None
    assert reason == "similarity"
    assert database.get_backtest_signals(
        cutoff=0,
        strategy_version="strategy-v1",
    ) == []

def test_radar_cluster_batch_rolls_back_cache_and_ledger_on_mid_group_failure(
    monkeypatch,
):
    database.init_db()
    now = int(datetime.now(TZ).timestamp())
    updates = []
    for index, score in enumerate((7.0, 7.2), start=1):
        link = f"https://example.com/atomic-radar-{index}"
        base = _signal(
            link,
            available_at=now,
            score=score,
            selected=True,
            aggregation_threshold=6.0,
        )
        database.set_cached_entry(
            link,
            f"[⭐{score:.1f} 📈 科技主线] 共同事件进展",
            "共同事件进展",
            score,
            "科技主线",
            "订单改善",
            score_path="stage2",
            strategy_version="strategy-v1",
            direction="看多",
            backtest_signal=base,
        )
        revision = {
            **base,
            "available_at": now + 1,
            "score": score + 0.5,
            "score_path": "radar",
            "signal_stage": "radar",
            "revision_key": "radar:auto",
            "auto_revision": True,
            "independence_key": "radar:atomic-test",
            "is_similarity_copy": index != 2,
        }
        updates.append(
            {
                "link": link,
                "title": f"[⭐{score + 0.5:.1f} 📈 科技主线] 共同事件进展",
                "score": score + 0.5,
                "score_path": "radar",
                "direction": "看多",
                "score_confidence": 0.7,
                "backtest_signal": revision,
                "update_cache": True,
            }
        )

    original_insert = database._insert_backtest_signal
    calls = 0

    def fail_second_insert(conn, signal):
        nonlocal calls
        calls += 1
        if calls == 2:
            raise OSError("simulated ledger failure")
        return original_insert(conn, signal)

    monkeypatch.setattr(database, "_insert_backtest_signal", fail_second_insert)

    with pytest.raises(OSError, match="simulated ledger failure"):
        database.update_cached_entry_score_states(updates)

    with database.get_connection() as conn:
        cache_scores = [
            row[0]
            for row in conn.execute(
                "SELECT score FROM news_cache ORDER BY link"
            ).fetchall()
        ]
    assert cache_scores == [7.0, 7.2]
    assert database.get_backtest_signal_stats("strategy-v1")["revision_count"] == 2
