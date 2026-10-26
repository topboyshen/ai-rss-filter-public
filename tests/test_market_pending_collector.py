"""Bounded close collection for frozen observations, without market I/O."""

from datetime import datetime
from zoneinfo import ZoneInfo

import pytest

from services import event_outcomes
from workers import market_close_collector as collector


TARGET = "2026-09-04"
CLOSE_TS = int(datetime(2026, 9, 4, 15, tzinfo=ZoneInfo("Asia/Shanghai")).timestamp())


def _quote(symbol):
    return {
        "symbol": symbol, "name": symbol, "close_price": 8.56,
        "change_amt": 0.1, "change_pct": 1.0, "ts": CLOSE_TS,
        "source_trade_date": TARGET,
    }


@pytest.fixture
def run_env(monkeypatch):
    state = {"pending": ["sh688306"], "dynamic": [], "stored": [], "outcomes": []}

    def requirements(target_date, *, start_date, limit):
        assert target_date == start_date == TARGET
        assert limit == collector.PENDING_SYMBOL_LIMIT
        return {
            "pairs": [{"symbol": value, "trade_date": TARGET} for value in state["pending"]],
            "total": len(state["pending"]) + int(state.get("truncated", False)),
            "truncated": state.get("truncated", False),
        }

    def materialize(target_date):
        state["outcomes"].append(target_date)
        return {"eligible": 0, "materialized": 0}

    monkeypatch.setattr(collector, "_today_local", lambda: TARGET)
    monkeypatch.setattr(collector.time, "time", lambda: CLOSE_TS + 600)
    monkeypatch.setattr(collector.time, "sleep", lambda delay: None)
    monkeypatch.setattr(event_outcomes, "get_pending_quote_requirements", requirements)
    monkeypatch.setattr(event_outcomes, "materialize_event_outcomes", materialize)
    monkeypatch.setattr(
        collector.database, "get_event_quote_universe",
        lambda **kwargs: [{"symbol": symbol} for symbol in state["dynamic"]],
    )
    monkeypatch.setattr(collector.database, "store_future_prices", state["stored"].append)
    monkeypatch.setattr(
        collector, "fetch_index_close_prices",
        lambda symbols: [_quote(symbol) for symbol in symbols],
    )
    monkeypatch.setattr(collector, "fetch_tencent_daily_close_prices", lambda *a, **kw: [])
    monkeypatch.setattr(collector, "fetch_sina_close_prices", lambda *a, **kw: [])
    return state


def test_pending_stock_is_additional_to_full_latest_500_universe(run_env, monkeypatch):
    run_env["dynamic"] = [f"sh{600000 + index}" for index in range(500)]
    calls = []

    def fetch(symbols):
        calls.append(symbols)
        return [_quote(symbol) for symbol in symbols]

    monkeypatch.setattr(collector, "fetch_index_close_prices", fetch)
    assert collector.run(TARGET) == 505
    assert len(calls) == 3 and max(map(len, calls)) <= 200
    assert "sh688306" in calls[0]
    assert len({row["symbol"] for row in run_env["stored"][0]}) == 505


def test_complete_indices_do_not_end_retry_while_pending_stock_is_missing(run_env, monkeypatch):
    calls = []

    def fetch(symbols):
        calls.append(symbols)
        return [_quote(symbol) for symbol in (collector.SYMBOLS if len(calls) == 1 else symbols)]

    monkeypatch.setattr(collector, "fetch_index_close_prices", fetch)
    assert collector.run(TARGET) == 5
    assert len(calls) == 2


def test_later_live_chunk_failure_preserves_earlier_success(run_env, monkeypatch):
    run_env["dynamic"] = [f"sh{600000 + index}" for index in range(500)]

    def fetch(symbols):
        if "sh000001" not in symbols:
            raise TimeoutError("later chunk")
        return [_quote(symbol) for symbol in symbols]

    monkeypatch.setattr(collector, "fetch_index_close_prices", fetch)
    assert collector.run(TARGET) == 200
    assert "sh688306" in {row["symbol"] for row in run_env["stored"][0]}


def _only_indices(monkeypatch):
    monkeypatch.setattr(
        collector, "fetch_index_close_prices",
        lambda symbols: [_quote(symbol) for symbol in collector.SYMBOLS],
    )


def test_pending_stock_uses_exact_date_daily_fallback(run_env, monkeypatch):
    _only_indices(monkeypatch)
    calls = []

    def fallback(symbols, *, trade_date):
        calls.append((symbols, trade_date))
        return [_quote(symbols[0])]

    monkeypatch.setattr(collector, "fetch_tencent_daily_close_prices", fallback)
    assert collector.run(TARGET) == 5
    assert calls == [(["sh688306"], TARGET)]


def test_live_before_close_does_not_prove_nontrading(run_env, monkeypatch, caplog):
    def fetch(symbols):
        rows = []
        for symbol in symbols:
            quote = _quote(symbol)
            if symbol == "sh688306":
                quote = {**quote, "ts": CLOSE_TS - 6 * 3600}
            rows.append(quote)
        return rows

    monkeypatch.setattr(collector, "fetch_index_close_prices", fetch)
    with pytest.raises(RuntimeError, match="missing=1"):
        collector.run(TARGET)
    assert len(run_env["stored"][0]) == 4
    assert "excluded_nontrading" not in caplog.text
    assert "sh688306" in caplog.text


def test_unresolved_stock_saves_valid_prices_and_reports_incomplete(run_env, monkeypatch):
    _only_indices(monkeypatch)
    with pytest.raises(RuntimeError, match="待结算覆盖未完成.*missing=1"):
        collector.run(TARGET)
    assert len(run_env["stored"][0]) == 4
    assert run_env["outcomes"] == [TARGET]


def test_one_daily_exception_does_not_discard_another_recovery(run_env, monkeypatch):
    run_env["pending"] = ["sh600001", "sh600002"]
    _only_indices(monkeypatch)

    def fallback(symbols, *, trade_date):
        if symbols == ["sh600002"]:
            raise TimeoutError("single symbol")
        return [_quote(symbols[0])]

    monkeypatch.setattr(collector, "fetch_tencent_daily_close_prices", fallback)
    with pytest.raises(RuntimeError, match="missing=1.*sh600002"):
        collector.run(TARGET)
    assert len(run_env["stored"][0]) == 5
    assert "sh600001" in {row["symbol"] for row in run_env["stored"][0]}


def test_daily_pending_count_is_bounded(run_env, monkeypatch):
    run_env["pending"] = [f"sh{600000 + index}" for index in range(3)]
    _only_indices(monkeypatch)
    monkeypatch.setattr(collector, "DAILY_PENDING_SYMBOL_LIMIT", 2)
    calls = []

    def fallback(symbols, *, trade_date):
        calls.append(symbols)
        return [_quote(symbols[0])]

    monkeypatch.setattr(collector, "fetch_tencent_daily_close_prices", fallback)
    with pytest.raises(RuntimeError, match="missing=1"):
        collector.run(TARGET)
    assert len(calls) == 2


def test_daily_deadline_stops_new_calls_and_keeps_progress(run_env, monkeypatch):
    run_env["pending"] = ["sh600001", "sh600002"]
    _only_indices(monkeypatch)
    clock = [0]
    monkeypatch.setattr(collector.time, "monotonic", lambda: clock[0])
    calls = []

    def fallback(symbols, *, trade_date):
        calls.append(symbols)
        clock[0] = collector.DAILY_FALLBACK_BUDGET_SECONDS + 1
        return [_quote(symbols[0])]

    monkeypatch.setattr(collector, "fetch_tencent_daily_close_prices", fallback)
    with pytest.raises(RuntimeError, match="missing=1"):
        collector.run(TARGET)
    assert calls == [["sh600001"]]
    assert len(run_env["stored"][0]) == 5


@pytest.mark.parametrize(
    "patch, reason",
    [
        ({"source_trade_date": "2026-09-03"}, "wrong_source_date"),
        ({"close_price": float("nan")}, "nonfinite_close_price"),
        ({"symbol": "sh600000"}, "unexpected_symbol"),
    ],
)
def test_bad_daily_quote_cannot_fill_pending_endpoint(run_env, monkeypatch, caplog, patch, reason):
    _only_indices(monkeypatch)
    monkeypatch.setattr(
        collector, "fetch_tencent_daily_close_prices",
        lambda *args, **kwargs: [{**_quote("sh688306"), **patch}],
    )
    with pytest.raises(RuntimeError, match="missing=1"):
        collector.run(TARGET)
    assert len(run_env["stored"][0]) == 4
    assert "reason=" + reason in caplog.text


def test_before_close_daily_quote_remains_missing_not_written(run_env, monkeypatch, caplog):
    _only_indices(monkeypatch)
    monkeypatch.setattr(
        collector, "fetch_tencent_daily_close_prices",
        lambda *args, **kwargs: [{**_quote("sh688306"), "ts": CLOSE_TS - 60}],
    )
    with pytest.raises(RuntimeError, match="missing=1"):
        collector.run(TARGET)
    assert len(run_env["stored"][0]) == 4
    assert "reason=before_close" in caplog.text
    assert "excluded_nontrading" not in caplog.text


def test_beijing_secondary_recovers_but_empty_responses_remain_missing(run_env, monkeypatch):
    run_env["pending"] = ["bj430047"]
    _only_indices(monkeypatch)
    with pytest.raises(RuntimeError, match="missing=1.*bj430047"):
        collector.run(TARGET)
    monkeypatch.setattr(
        collector, "fetch_sina_close_prices",
        lambda codes, **kw: [_quote(codes[0])],
    )
    assert collector.run(TARGET) == 5
    assert any(row['symbol'] == 'bj430047' for row in run_env['stored'][-1])


def test_demand_truncation_is_not_reported_as_complete(run_env):
    run_env["truncated"] = True
    with pytest.raises(RuntimeError, match="truncated=True"):
        collector.run(TARGET)
    assert len(run_env["stored"][0]) == 5
