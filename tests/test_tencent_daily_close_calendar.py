import pytest

from modules.a_stock_data import _tencent_daily_record


def _bar(day, close="100"):
    return [day, close, close, close, close, "1000"]


@pytest.mark.parametrize("previous,target", [
    ("2026-07-13", "2026-07-14"),
    ("2026-07-10", "2026-07-13"),
    ("2026-09-24", "2026-09-28"),
    ("2026-09-30", "2026-10-08"),
])
def test_exact_previous_session_accepts_weekends_and_holidays(previous, target):
    # Unsorted provider rows retain their supported behavior.
    record = _tencent_daily_record("sz399006", {
        "day": [_bar(target, "110"), _bar(previous)],
    }, target)
    assert record["prev_close"] == 100.0
    assert record["change_pct"] == 10.0


@pytest.mark.parametrize("previous,target", [
    ("2026-07-10", "2026-07-14"),
    ("2026-07-12", "2026-07-13"),
    ("2026-09-23", "2026-09-28"),
    ("2026-07-10", "2026-07-11"),
    ("2027-07-12", "2027-07-13"),
])
def test_gap_or_unsupported_calendar_remains_unavailable(previous, target):
    assert _tencent_daily_record("sz399006", {
        "day": [_bar(previous), _bar(target, "120")],
    }, target) is None


def test_duplicate_date_is_still_rejected():
    with pytest.raises(ValueError, match="重复交易日"):
        _tencent_daily_record("sz399006", {"day": [
            _bar("2026-07-13"), _bar("2026-07-13"), _bar("2026-07-14"),
        ]}, "2026-07-14")


@pytest.mark.parametrize("close", ["NaN", "Infinity", "0", "-1"])
def test_invalid_previous_close_is_still_rejected(close):
    with pytest.raises(ValueError, match="无效价格"):
        _tencent_daily_record("sz399006", {"day": [
            _bar("2026-07-13", close), _bar("2026-07-14", "120"),
        ]}, "2026-07-14")


def test_missing_primary_session_uses_existing_secondary_fallback(monkeypatch):
    from datetime import datetime
    from zoneinfo import ZoneInfo
    from modules.verified_close import _close_record
    from workers import market_close_collector as collector

    now = int(datetime(2026, 7, 14, 16, tzinfo=ZoneInfo("Asia/Shanghai")).timestamp())
    calls = []

    def primary(codes, *, trade_date):
        calls.append("primary")
        record = _tencent_daily_record(codes[0], {"day": [
            _bar("2026-07-10", "90"), _bar(trade_date, "120"),
        ]}, trade_date)
        return [record] if record else []

    def secondary(codes, *, trade_date):
        calls.append("secondary")
        return [_close_record(codes[0], [
            {"day": "2026-07-13 15:00:00", "close": "100"},
            {"day": "2026-07-14 15:00:00", "close": "120"},
        ], trade_date, now)]

    monkeypatch.setattr(collector.time, "time", lambda: now)
    monkeypatch.setattr(collector, "fetch_tencent_daily_close_prices", primary)
    monkeypatch.setattr(collector, "fetch_sina_close_prices", secondary)
    record = collector._fallback_close("sz399006", "2026-07-14", collector.time.monotonic() + 10)
    assert calls == ["primary", "secondary"]
    assert record["prev_close"] == 100.0
    assert record["change_pct"] == 20.0
