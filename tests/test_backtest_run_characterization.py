"""Whole-result checks, including the fixed cutoff and exact-close admission.

The close-admission update changes only wrong-day T+2 article observations and
the evaluation_at argument passed to control/theme consumers in these fixtures.
"""

from copy import deepcopy
from datetime import date, datetime, timedelta
import hashlib
import json
from uuid import UUID
from zoneinfo import ZoneInfo

import pytest

from services import backtest


_TZ = ZoneInfo("Asia/Shanghai")
_NOW = int(datetime(2026, 9, 10, 8, tzinfo=_TZ).timestamp())
_GOLDEN = {
    "snapshot": "59395c59fe6dfac8dc2ca657a3455fa6c187645a815cbe6c3ecc079fb763530a",
    "two_tuple": "59c3997ea3be5be64d17372dc9f323d7f1bf628d74ca7cb90ecc91b99bdbc472",
    "exploratory_zero": "d046d859e93cc9723086056efd40b163235b60bb4752ac268c1d27dc968fb385",
    "exploratory_threshold": "c2ded1476b096d58eae773486e91fdd528ca31d64ec4f31d112cba7b4a30a505",
    "legacy": "e2c1c1e7133628dd3b25b07459aba6ab32197e013ceafbb51e2763aa0dd424f4",
    "empty": "aabee55191b25a3d6e6e0c9182f553cd494db79d300cb39723e16e318c7454a7",
}


def _article(number, score, day, *, symbol="sz399006", direction="看多"):
    timestamp = int(datetime(2026, 9, day, 10, tzinfo=_TZ).timestamp())
    return {
        "link": f"fixture:{number}", "title": f"frozen news {number}",
        "event_id": str(UUID(int=number)), "score": score,
        "created_at": timestamp, "available_at": timestamp,
        "direction": direction, "tag": "主题", "mapped_theme": "主题",
        "backtest_symbol": symbol, "strategy_version": "strategy:exact",
        "mapping_version": "mapping:exact", "score_confidence": 0.6,
        "_signal_snapshot": True, "selected": True,
    }


def _fixture_inputs(case):
    articles = [
        _article(1, 9.2, 1),
        _article(2, 8.2, 2, symbol=None),
        _article(3, 7.2, 3, direction="中性"),
        _article(4, 11.0, 3),
        _article(5, -1.0, 3),
    ]
    articles.append({**articles[0], "link": "fixture:duplicate",
                     "available_at": articles[0]["available_at"] + 30})
    if case == "empty":
        articles = []
    history = {
        "data_source": "legacy_news_cache" if case == "legacy" else "signal_snapshots",
        "count": len(articles), "ledger_revision_count": 23, "ledger_count": 20,
        "collection_started_at": _NOW - 120 * 86400,
        "first_available_at": _NOW - 10 * 86400,
        "cohort_history": {
            "current_scored_signals": 8, "current_selected_signals": 6,
            "current_unselected_signals": 2, "current_selected_signal_days": 3,
            "archived_selected_signals": 12, "version_count": 2,
            "current_strategy_version": "strategy:exact",
            "current_mapping_version": "mapping:exact",
        },
    }
    return articles, [_article(6, 4.2, 4)], history


def _install_fixture(monkeypatch, case, inputs, calls):
    def record(name, result):
        def callback(*args, **kwargs):
            calls.append([name, deepcopy(args), deepcopy(kwargs)])
            return result
        return callback

    def clock():
        calls.append(["clock"])
        return _NOW

    def price(symbol, from_date, horizon=0):
        calls.append(["price", symbol, from_date, horizon])
        if from_date == "2026-09-02" and horizon == 1:
            return None
        target = date.fromisoformat(from_date) + timedelta(days=horizon)
        if horizon == 2 and symbol == "sz399006":
            target += timedelta(days=1)
        change = 0.5 if symbol == "sh000001" else -2.0 - horizon
        return {"trade_date": target.isoformat(), "change_amt": change,
                "close_price": 100.0,
                "ts": int(datetime.combine(target, datetime.min.time(), _TZ)
                          .replace(hour=15).timestamp()),
                "change_pct": change, "revision_id": "frozen-price"}

    def fail_database(*_args, **_kwargs):
        raise AssertionError("orchestration must not open a new database connection")

    cfg = {"scoring_strategy_version": "strategy:exact"}
    loaded = (inputs[0], inputs[2]) if case == "two_tuple" else inputs
    monkeypatch.setattr(backtest.time, "time", clock)
    monkeypatch.setattr(backtest, "load_config", record("config", cfg))
    monkeypatch.setattr(backtest, "resolve_scoring_contract", record("contract", cfg))
    monkeypatch.setattr(backtest, "get_mapping_version", record("mapping", "mapping:exact"))
    monkeypatch.setattr(backtest, "_load_backtest_samples", record("samples", loaded))
    monkeypatch.setattr(backtest, "dedupe_articles", lambda rows: list(rows))
    monkeypatch.setattr(backtest, "_same_day_snapshot", price)
    monkeypatch.setattr(backtest, "_future_snapshot", price)
    price.cache_clear = lambda: calls.append(["cache_clear"])
    monkeypatch.setattr(backtest, "_trading_day_after", lambda day, n: (
        date.fromisoformat(day) + timedelta(days=n)).isoformat())
    monkeypatch.setattr(backtest, "_is_t0_evaluable", lambda _timestamp: True)
    control = {"ready_for_tuning": False, "status": "settling",
               "unselected": {"recorded_signals": 2, "deduped_signals": 1,
                              "t1_settled_event_results": 0}}
    monkeypatch.setattr(backtest, "_settle_score_control_cohort", record("control", control))
    monkeypatch.setattr(backtest, "_theme_future_win_rate", record(
        "themes", {"主题": {"gross_return": -3.0, "missing": None}}))
    preview = None if case == "empty" else {"status": "preview", "count": 3}
    monkeypatch.setattr(backtest, "load_bridge_preview", record("bridge", preview))
    monkeypatch.setattr(backtest, "get_connection", fail_database)


@pytest.mark.parametrize("case", tuple(_GOLDEN))
def test_run_score_backtest_complete_result_and_side_effect_order(monkeypatch, case):
    inputs = _fixture_inputs(case)
    before = deepcopy(inputs)
    calls = []
    _install_fixture(monkeypatch, case, inputs, calls)
    min_score = {"exploratory_zero": 0.0, "exploratory_threshold": 8.0}.get(case)

    result = backtest.run_score_backtest(days=90, min_score=min_score)

    payload = json.dumps({"result": result, "calls": calls}, sort_keys=True,
                         ensure_ascii=False, allow_nan=False, separators=(",", ":"))
    assert hashlib.sha256(payload.encode()).hexdigest() == _GOLDEN[case]
    assert inputs == before
    assert result["strategy_version"] == "strategy:exact"
    assert result["mapping_version"] == "mapping:exact"
    assert result["cohort_history"]["archived_selected_signals"] == 12
    assert result["optimization"]["auto_apply"] is False
    for call in calls:
        if call[0] in {"control", "themes"}:
            assert call[2]["evaluation_at"] == _NOW
    if case != "empty":
        loss_bucket = result["buckets"][0]
        assert loss_bucket["t1_avg_signal_return_pct"] == -3.0
        assert loss_bucket["clustered_t2_signal_return_samples"] == 0
        assert loss_bucket["clustered_t2_avg_signal_return_pct"] is None
        assert loss_bucket["asset_t2_direction_samples"] == 0
        assert loss_bucket["t2_signal_return_samples"] == 0
        assert loss_bucket["t2_excess_return_samples"] == 0
        assert loss_bucket["t2_avg_signal_return_pct"] is None
        assert result["buckets"][1]["asset_t1_direction_samples"] == 0
        assert result["invalid_score_samples"] == 2


def test_missing_mapping_contract_stops_before_loading_samples(monkeypatch):
    calls = []
    _install_fixture(monkeypatch, "empty", _fixture_inputs("empty"), calls)
    monkeypatch.setattr(backtest, "get_mapping_version", lambda: "  ")
    with pytest.raises(RuntimeError, match="mapping contract unavailable"):
        backtest.run_score_backtest()
    assert not any(call[0] == "samples" for call in calls)
    assert [call[0] for call in calls] == [
        "clock", "cache_clear", "cache_clear", "config", "contract",
    ]
