from datetime import datetime
from zoneinfo import ZoneInfo

import pytest

from db import database
from services import backtest


TZ = ZoneInfo("Asia/Shanghai")
ASSET = "sz399006"
BENCHMARK = "sh000001"


def _ts(value):
    return int(datetime.strptime(value, "%Y-%m-%d %H:%M:%S").replace(tzinfo=TZ).timestamp())


@pytest.fixture
def cohort(monkeypatch):
    clock = {"now": _ts("2026-07-13 10:00:00")}
    monkeypatch.setattr(database.time, "time", lambda: clock["now"])
    monkeypatch.setattr(backtest, "load_config", lambda: {"scoring_strategy_version": "close-audit"})
    monkeypatch.setattr(backtest, "get_mapping_version", lambda: "close-audit")
    database.init_db()
    assert database.store_backtest_signal({
        "link": "https://example.test/verified-close",
        "event_key": "verified-close",
        "available_at": clock["now"], "scored_at": clock["now"],
        "score": 9.0, "direction": "看多", "selected": True,
        "score_path": "stage2", "score_confidence": 0.8,
        "strategy_version": "close-audit", "mapping_version": "close-audit",
        "mapped_theme": "AI算力", "backtest_symbol": ASSET,
        "aggregation_threshold": 8.0,
    })
    clock["now"] = _ts("2026-07-14 16:00:00")
    return clock


def _price(symbol, **changes):
    return {
        "symbol": symbol, "trade_date": "2026-07-14",
        "close_price": 110.0, "change_amt": 10.0,
        "change_pct": 10.0 if symbol == ASSET else 1.0,
        "ts": _ts("2026-07-14 15:00:00"), **changes,
    }


def _run():
    result = backtest.run_score_backtest(days=30)
    return result, result["buckets"][0], result["theme_win_rates"]["AI算力"]


@pytest.mark.parametrize("source_hour", [10, 15])
def test_live_table_cannot_supply_posterior_even_after_close(cohort, source_hour):
    database.store_market_prices([
        {**_price(symbol), "price": 110.0,
         "ts": _ts(f"2026-07-14 {source_hour:02d}:00:00")}
        for symbol in (ASSET, BENCHMARK)
    ])
    result, bucket, theme = _run()
    assert bucket["asset_t1_direction_samples"] == 0
    assert bucket["clustered_asset_t1_direction_samples"] == 0
    assert theme["t1_samples"] == theme["clustered_t1_samples"] == 0
    assert result["control_cohort"]["selected"]["t1_settled_event_results"] == 0


@pytest.mark.parametrize("bad_side", [ASSET, BENCHMARK])
@pytest.mark.parametrize("invalid", [
    {"ts": _ts("2026-07-14 10:00:00")},
    {"ts": _ts("2026-07-14 17:00:00")},
    {"ts": _ts("2026-07-13 15:00:00")},
    {"ts": None},
    {"close_price": 0.0},
    {"close_price": float("inf")},
    {"trade_date": "2026-07-15", "ts": _ts("2026-07-15 15:00:00")},
    None,
], ids=["intraday", "future", "wrong-source-day", "missing-time", "zero-close", "infinite-close", "missing-exact-day", "missing-side"])
def test_invalid_or_missing_side_cannot_settle_control_or_excess(cohort, bad_side, invalid):
    database.store_future_prices([
        _price(symbol, **(invalid or {})) if symbol == bad_side else _price(symbol)
        for symbol in (ASSET, BENCHMARK)
        if symbol != bad_side or invalid is not None
    ])
    result, bucket, theme = _run()
    # Preserve independent asset/benchmark denominators; paired metrics need both.
    asset_samples = int(bad_side != ASSET)
    benchmark_samples = int(bad_side != BENCHMARK)
    assert bucket["asset_t1_direction_samples"] == asset_samples
    assert bucket["clustered_asset_t1_direction_samples"] == asset_samples
    assert bucket["benchmark_t1_direction_samples"] == benchmark_samples
    assert bucket["clustered_benchmark_t1_direction_samples"] == benchmark_samples
    assert bucket["t1_excess_return_samples"] == 0
    assert bucket["clustered_t1_excess_return_samples"] == 0
    assert theme["t1_samples"] == theme["clustered_t1_samples"] == asset_samples
    assert theme["t1_avg_excess_return_pct"] is None
    assert theme["clustered_t1_avg_excess_return_pct"] is None
    assert result["control_cohort"]["selected"]["t1_settled_event_results"] == 0
    assert result["optimization"]["can_tune_thresholds"] is False


def test_verified_closes_keep_original_single_day_return_method(cohort):
    database.store_future_prices([_price(ASSET), _price(BENCHMARK)])
    result, bucket, theme = _run()
    assert bucket["asset_t1_direction_samples"] == 1
    assert bucket["clustered_asset_t1_direction_samples"] == 1
    assert bucket["t1_avg_signal_return_pct"] == 10.0
    assert bucket["t1_avg_excess_return_pct"] == 9.0
    assert theme["t1_avg_signal_return_pct"] == 10.0
    assert theme["clustered_t1_avg_excess_return_pct"] == 9.0
    assert result["control_cohort"]["selected"]["t1_settled_event_results"] == 1


def test_all_consumers_share_cutoff_when_run_crosses_close(cohort, monkeypatch):
    cohort["now"] = _ts("2026-07-14 14:59:59")
    database.store_future_prices([_price(ASSET), _price(BENCHMARK)])
    original = backtest._future_snapshot

    def after_close_lookup(*args):
        cohort["now"] = _ts("2026-07-14 15:01:00")
        return original(*args)

    monkeypatch.setattr(backtest, "_future_snapshot", after_close_lookup)
    result, bucket, theme = _run()
    assert bucket["asset_t1_direction_samples"] == 0
    assert bucket["clustered_asset_t1_direction_samples"] == 0
    assert theme["t1_samples"] == theme["clustered_t1_samples"] == 0
    assert result["control_cohort"]["selected"]["t1_settled_event_results"] == 0
    # A new run may admit the same verified closes using its own later cutoff.
    result, bucket, theme = _run()
    assert bucket["asset_t1_direction_samples"] == 1
    assert theme["t1_samples"] == 1
    assert result["control_cohort"]["selected"]["t1_settled_event_results"] == 1


def test_same_day_consumer_rejects_intraday_close(cohort):
    cohort["now"] = _ts("2026-07-13 11:00:00")
    database.store_future_prices([
        _price(symbol, trade_date="2026-07-13", ts=_ts("2026-07-13 10:30:00"))
        for symbol in (ASSET, BENCHMARK)
    ])
    _, bucket, _ = _run()
    assert bucket["asset_direction_samples"] == 0
    assert bucket["clustered_asset_direction_samples"] == 0
    assert backtest._market_direction("2026-07-13") is None
