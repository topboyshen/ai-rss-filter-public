import logging

import pytest


def _stock_row(**overrides):
    row = {
        "security_id": "SSE:600000",
        "exchange": "SSE",
        "ticker": "600000",
        "name": "浦发银行",
        "asset_type": "equity",
        "currency": "CNY",
        "aliases": '["浦发"]',
        "industry": "银行",
        "concepts": '["金融"]',
        "is_active": 1,
        "security_version": 2,
        "mapping_id": "mapping-1",
        "mapping_event_id": "event-1",
        "relation_type": "direct",
        "direction": "bullish",
        "expected_gap": 0.03,
        "confidence": 0.8,
        "relevance": 9.0,
        "rationale": "直接影响",
        "mapping_version": "mapping-v1",
        "revision": 1,
        "created_at": 100,
        "candidate_count": 2,
        "candidate_event_count": 1,
        "direction_variant_count": 1,
    }
    row.update(overrides)
    return row


def _outcome_row(**overrides):
    row = {
        "outcome_id": "outcome-1",
        "outcome_revision_id": "revision-1",
        "event_id": "event-1",
        "mapping_id": "mapping-1",
        "mapping_revision": 2,
        "horizon": "T+1",
        "signal_ledger_id": 7,
        "ledger_strategy_version": "strategy-v1",
        "ledger_mapping_version": "mapping-v1",
        "signal_available_at": 100,
        "baseline_at": 200,
        "measured_at": 300,
        "baseline_price": 10.0,
        "gross_return": 0.05,
        "benchmark_return": 0.02,
        "excess_return": 0.03,
        "mfe": 0.08,
        "mae": -0.01,
        "direction_hit": 1,
        "method_version": "close-to-close-v1",
        "data_version": "prices-v1",
        "data_quality": 0.99,
        "observation_state": "complete",
        "revision": 3,
        "created_at": 400,
        "merged_series_count": 2,
        "security_id": "SSE:600000",
        "exchange": "SSE",
        "ticker": "600000",
        "name": "浦发银行",
        "asset_type": "equity",
        "relation_type": "direct",
        "mapping_direction": "bullish",
        "rationale": "直接影响",
        "benchmark_security_id": "sh000300",
        "benchmark_exchange": "SSE",
        "benchmark_ticker": "000300",
        "benchmark_name": "沪深300",
    }
    row.update(overrides)
    return row


def _pending_mapping(**overrides):
    mapping = {
        "ledger_signal_id": 8,
        "frozen_mapping_id": "mapping-2",
        "mapping_event_id": "event-2",
        "revision": 1,
        "strategy_version": "strategy-v1",
        "signal_mapping_version": "mapping-v1",
        "available_at": 100,
        "mapping_is_current": 1,
        "exchange": "SSE",
        "name": "测试股票",
        "asset_type": "equity",
        "relation_type": "direct",
        "direction": "bullish",
        "rationale": "直接影响",
    }
    mapping.update(overrides)
    return mapping


def test_merge_stock_mapping_aggregates_conflicts_and_evidence():
    from services.event_radar_query import _merge_stock_mapping

    stocks = {}
    ranks = {"SSE:600000": (9.0, 0.8, 0.03)}
    evidence = {"mapping-1": [{"source_uri": "https://example.com/a"}]}

    _merge_stock_mapping(stocks, _stock_row(), ranks, evidence)
    _merge_stock_mapping(
        stocks,
        _stock_row(
            mapping_id="mapping-2",
            relation_type="upstream",
            candidate_count=3,
            direction_variant_count=2,
        ),
        ranks,
        evidence,
    )

    stock = stocks["SSE:600000"]
    assert stock["aliases"] == ["浦发"]
    assert stock["concepts"] == ["金融"]
    assert stock["mapping_candidate_count"] == 5
    assert stock["has_direction_conflict"] is True
    assert len(stock["relations"]) == 2
    assert stock["relations"][0]["evidence"][0]["source_uri"].endswith("/a")
    assert stock["relations"][1]["relation_label"] == "上游"


@pytest.mark.parametrize(
    ("benchmark_id", "samples", "days", "expected_status", "expected_benchmark"),
    [
        ("sh000300", 30, 30, "complete_close_only", True),
        (None, 0, 0, "missing_asset_close", False),
    ],
)
def test_materialized_outcome_view_contract(
    benchmark_id, samples, days, expected_status, expected_benchmark
):
    from services.event_radar_query import _materialized_outcome_view

    row = _outcome_row(benchmark_security_id=benchmark_id)
    if expected_status != "complete_close_only":
        row.update(gross_return=None, observation_state=expected_status)
    result = _materialized_outcome_view(
        row,
        tradability={
            "tradable_baseline_at": 210,
            "tradable_baseline_price": 10.1,
            "tradability_status": "tradable",
            "suspension_status": "not_suspended",
            "limit_status": "normal",
        },
        method_supported=True,
        mapping_current=True,
        samples=samples,
        independent_days=days,
        successes=24 if samples else 0,
        ci_low=0.6 if samples else None,
        ci_high=0.9 if samples else None,
    )

    assert result["materialized"] is True
    assert result["data_status"] == expected_status
    assert result["sample_sufficient"] is (samples >= 30 and days >= 30)
    assert (result["benchmark"] is not None) is expected_benchmark
    assert result["security"]["ticker"] == "600000"
    assert result["mapping"]["direction"] == "bullish"


@pytest.mark.parametrize(
    ("state", "data_status"),
    [("not_due", "not_due"), ("complete", "complete_pending_materialization")],
)
def test_pending_outcome_view_contract(state, data_status):
    from services.event_radar_query import (
        MARKET_BENCHMARK_SYMBOL,
        OUTCOME_METHOD_VERSION,
        _pending_outcome_view,
    )

    result = _pending_outcome_view(
        _pending_mapping(),
        horizon="T+1",
        baseline_at=200,
        measured_at=300,
        tradability={"tradability_status": "tradable"},
        state=state,
        data_status=data_status,
        ticker="600000",
        security_id="SSE:600000",
    )

    assert result["outcome_id"] == "pending:8:mapping-2:T+1"
    assert result["materialized"] is False
    assert result["observation_state"] == state
    assert result["data_status"] == data_status
    assert result["benchmark"]["security_id"] == MARKET_BENCHMARK_SYMBOL
    assert result["method_version"] == OUTCOME_METHOD_VERSION


def test_operation_duration_emits_slow_warning(monkeypatch, caplog):
    import services.event_radar_query as radar

    monkeypatch.setattr(radar, "perf_counter", lambda: 1.5)
    with caplog.at_level(logging.WARNING, logger=radar.__name__):
        elapsed = radar._log_operation_duration("unit_test", 1.0, item_count=3)

    assert elapsed == 500.0
    assert "operation=unit_test" in caplog.text
    assert "slow=True" in caplog.text
    assert "item_count=3" in caplog.text


def test_operation_duration_emits_debug_for_fast_path(monkeypatch, caplog):
    import services.event_radar_query as radar

    monkeypatch.setattr(radar, "perf_counter", lambda: 1.01)
    with caplog.at_level(logging.DEBUG, logger=radar.__name__):
        elapsed = radar._log_operation_duration("unit_test_fast", 1.0)

    assert elapsed == pytest.approx(10.0)
    assert "slow=False" in caplog.text
    assert "item_count=unknown" in caplog.text
