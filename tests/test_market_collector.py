import math
import json
from datetime import datetime
from zoneinfo import ZoneInfo

import pytest

from modules import a_stock_data


@pytest.fixture(autouse=True)
def isolate_secondary_close_source(monkeypatch):
    from workers import market_close_collector
    monkeypatch.setattr(market_close_collector, 'fetch_sina_close_prices', lambda *a, **kw: [])


class _Response:
    def __init__(self, url, data):
        self._url = url
        self._data = data
        self.read_limit = None

    def __enter__(self):
        return self

    def __exit__(self, *args):
        return False

    def geturl(self):
        return self._url

    def read(self, limit):
        self.read_limit = limit
        return self._data


def _quote_payload():
    values = [""] * 53
    values[1] = "上证指数"
    values[3] = "3001.5"
    values[4] = "2990.0"
    values[5] = "2995.0"
    values[30] = "20260710150000"
    values[31] = "11.5"
    values[32] = "0.38"
    return "~".join(values)


def test_tencent_quote_uses_fixed_https_endpoint_and_bounded_read():
    requested = {}

    def fake_urlopen(request, timeout):
        requested["url"] = request.full_url
        response = _Response(
            request.full_url,
            f'v_sh000001="{_quote_payload()}";'.encode("gbk"),
        )
        requested["response"] = response
        return response

    quotes = a_stock_data.tencent_quote(["sh000001"], opener=fake_urlopen)

    assert requested["url"] == "https://qt.gtimg.cn/q=sh000001"
    assert requested["response"].read_limit == a_stock_data.MAX_RESPONSE_BYTES + 1
    assert quotes["000001"]["price"] == 3001.5
    assert quotes["000001"]["symbol"] == "sh000001"
    assert quotes["000001"]["quote_date"] == "2026-07-10"
    assert quotes["000001"]["quote_ts"] == int(
        datetime(2026, 7, 10, 15, 0, tzinfo=ZoneInfo("Asia/Shanghai")).timestamp()
    )


def test_tencent_daily_close_fetch_is_exact_date_and_no_lookahead():
    requested = {}
    document = {
        "code": 0,
        "data": {
            "sh000001": {
                "day": [
                    ["2026-07-09", "2990", "3000", "3010", "2980", "100"],
                    ["2026-07-10", "3005", "3030", "3040", "3001", "120"],
                ],
                "qt": {"sh000001": ["1", "上证指数"]},
            }
        },
    }

    def opener(request, timeout):
        requested["url"] = request.full_url
        requested["timeout"] = timeout
        return _Response(request.full_url, json.dumps(document).encode("utf-8"))

    records = a_stock_data.fetch_tencent_daily_close_prices(
        ["sh000001"], trade_date="2026-07-10", opener=opener
    )

    assert requested["url"].startswith(a_stock_data.TENCENT_KLINE_URL + "?")
    assert "2026-07-10" in requested["url"]
    assert requested["timeout"] == 15
    assert len(records) == 1
    assert records[0]["symbol"] == "sh000001"
    assert records[0]["name"] == "上证指数"
    assert records[0]["close_price"] == 3030.0
    assert records[0]["prev_close"] == 3000.0
    assert records[0]["change_pct"] == pytest.approx(1.0)
    assert records[0]["source_trade_date"] == "2026-07-10"
    assert records[0]["ts"] == int(
        datetime(2026, 7, 10, 15, 0, tzinfo=ZoneInfo("Asia/Shanghai")).timestamp()
    )


def test_tencent_daily_close_rejects_provider_lookahead():
    document = {
        "code": 0,
        "data": {
            "sh000001": {
                "day": [
                    ["2026-07-09", "2990", "3000", "3010", "2980", "100"],
                    ["2026-07-10", "3005", "3030", "3040", "3001", "120"],
                    ["2026-07-11", "3030", "3050", "3060", "3020", "130"],
                ]
            }
        },
    }

    def opener(request, timeout):
        del timeout
        return _Response(request.full_url, json.dumps(document).encode("utf-8"))

    with pytest.raises(ValueError, match="目标日期之后"):
        a_stock_data.fetch_tencent_daily_close_prices(
            ["sh000001"], trade_date="2026-07-10", opener=opener
        )


def test_quote_time_uses_exchange_timezone():
    china = a_stock_data._parse_quote_time("sh000001", "20260710150000")
    us = a_stock_data._parse_quote_time("usQQQ", "2026-07-09 16:00:00")

    assert china == (
        int(datetime(2026, 7, 10, 15, 0, tzinfo=ZoneInfo("Asia/Shanghai")).timestamp()),
        "2026-07-10",
    )
    assert us == (
        int(
            datetime(2026, 7, 9, 16, 0, tzinfo=ZoneInfo("America/New_York")).timestamp()
        ),
        "2026-07-09",
    )


def test_bare_920_ticker_maps_to_beijing_exchange():
    assert a_stock_data._tencent_prefix("920000") == "bj920000"


@pytest.mark.parametrize("code", ["", "sh000001&evil=1", "http://127.0.0.1", "123"])
def test_tencent_quote_rejects_invalid_codes_before_network(code):
    with pytest.raises(ValueError):
        a_stock_data.tencent_quote(
            [code],
            opener=lambda *args, **kwargs: pytest.fail("invalid code reached network"),
        )


def test_tencent_quote_rejects_redirect_and_oversized_body():
    url = "https://qt.gtimg.cn/q=sh000001"
    with pytest.raises(ValueError, match="重定向"):
        a_stock_data.tencent_quote(
            ["sh000001"],
            opener=lambda *args, **kwargs: _Response("https://other.invalid/", b""),
        )

    with pytest.raises(ValueError, match="大小限制"):
        a_stock_data.tencent_quote(
            ["sh000001"],
            opener=lambda *args, **kwargs: _Response(
                url, b"x" * (a_stock_data.MAX_RESPONSE_BYTES + 1)
            ),
        )


def test_tencent_quote_drops_unsolicited_or_malformed_records():
    url = "https://qt.gtimg.cn/q=sh000001"
    body = (
        f'v_sz399001="{_quote_payload()}";' "not-a-record;" 'v_sh000001="too~short";'
    ).encode("gbk")
    assert (
        a_stock_data.tencent_quote(
            ["sh000001"],
            opener=lambda *args, **kwargs: _Response(url, body),
        )
        == {}
    )


def test_tencent_default_fetcher_disables_ambient_proxies_and_redirects(monkeypatch):
    url = "https://qt.gtimg.cn/q=sh000001"
    response = _Response(url, f'v_sh000001="{_quote_payload()}";'.encode("gbk"))
    seen_handlers = []

    class BuiltOpener:
        def open(self, request, timeout):
            assert request.get_header("Accept-encoding") == "identity"
            assert timeout == 15
            return response

    def build_opener(*handlers):
        seen_handlers.extend(handlers)
        return BuiltOpener()

    monkeypatch.setattr(a_stock_data.urllib.request, "build_opener", build_opener)
    assert "000001" in a_stock_data.tencent_quote(["sh000001"])
    assert any(
        isinstance(handler, a_stock_data.urllib.request.ProxyHandler)
        and handler.proxies == {}
        for handler in seen_handlers
    )
    assert any(
        isinstance(handler, a_stock_data._RejectRedirects) for handler in seen_handlers
    )


def test_historical_daily_prices_preserve_ohlc_and_tradability_fields():
    requested = {}
    document = {
        "ResultCode": 0,
        "Result": {
            "newMarketData": {
                "keys": [
                    "time",
                    "open",
                    "close",
                    "volume",
                    "high",
                    "low",
                    "amount",
                    "preClose",
                ],
                "marketData": (
                    "2026-07-13,99,100,12345,102,98,987654,97;"
                    "2026-07-14,100,105,23456,106,99,1234567,100"
                ),
            }
        },
    }

    def opener(request, timeout):
        requested["url"] = request.full_url
        requested["timeout"] = timeout
        return _Response(request.full_url, json.dumps(document).encode("utf-8"))

    rows = a_stock_data.fetch_historical_daily_prices(
        "sh688256",
        start_date="2026-07-13",
        opener=opener,
    )

    assert requested["url"].startswith(a_stock_data.BAIDU_KLINE_URL + "?")
    assert requested["timeout"] == 15
    assert len(rows) == 2
    assert rows[0]["symbol"] == "sh688256"
    assert rows[0]["open_price"] == 99
    assert rows[0]["high_price"] == 102
    assert rows[0]["low_price"] == 98
    assert rows[0]["prev_close"] == 97
    assert rows[0]["change_pct"] == pytest.approx(100 / 97 * 100 - 100)
    assert rows[0]["suspension_state"] == "trading"
    assert rows[0]["limit_state"] == "unknown_historical"
    assert rows[0]["tradability_state"] == "trading_ohlc"


def test_collector_validates_date_and_records_before_store(monkeypatch):
    from workers import market_close_collector as collector

    monkeypatch.setattr(collector, "_today_local", lambda: "2026-07-10")
    monkeypatch.setattr(
        collector.database, "_is_trading_day_local", lambda value: True, raising=False
    )
    close_ts = int(
        datetime(2026, 7, 10, 15, 0, tzinfo=ZoneInfo("Asia/Shanghai")).timestamp()
    )
    monkeypatch.setattr(collector.time, "time", lambda: close_ts + 600)
    monkeypatch.setattr(collector.time, "sleep", lambda _delay: None)

    def good(symbol, name):
        return {
            "symbol": symbol,
            "name": name,
            "close_price": 3001.5,
            "change_amt": 11.5,
            "change_pct": 0.38,
            "ts": close_ts,
            "source_trade_date": "2026-07-10",
        }

    benchmark = good("sh000001", " 上证指数 ")
    csi300 = good("sh000300", "沪深300")
    deep = good("sz399001", "深证成指")
    growth = good("sz399006", "创业板指")
    bad = {**benchmark, "symbol": "sh000002", "close_price": math.nan}
    duplicate = dict(benchmark)
    monkeypatch.setattr(
        collector,
        "fetch_index_close_prices",
        lambda symbols: [benchmark, csi300, bad, duplicate, deep, growth],
    )
    captured = []
    monkeypatch.setattr(
        collector.database, "store_future_prices", captured.append, raising=False
    )
    import services.event_outcomes as event_outcomes

    monkeypatch.setattr(
        event_outcomes,
        "materialize_event_outcomes",
        lambda _target: {"eligible": 0, "materialized": 0},
    )

    assert collector.run("2026-07-10") == 4
    assert len(captured) == 1
    assert [row["symbol"] for row in captured[0]] == collector.SYMBOLS
    assert captured[0][0]["symbol"] == "sh000001"
    assert captured[0][0]["trade_date"] == "2026-07-10"
    assert captured[0][0]["name"] == "上证指数"
    assert captured[0][0]["close_price"] == 3001.5
    assert captured[0][0]["tradability_state"] == "unknown"
    assert captured[0][0]["ts"] == close_ts


def test_collector_rejects_invalid_or_historical_trade_date(monkeypatch):
    from workers import market_close_collector as collector

    monkeypatch.setattr(collector, "_today_local", lambda: "2026-07-10")
    with pytest.raises(ValueError, match="YYYY-MM-DD"):
        collector.run("2026-7-10")
    with pytest.raises(ValueError, match="不支持历史"):
        collector.run("2026-07-09")


def test_trading_calendar_fails_closed_for_unsupported_year():
    from db import database

    with pytest.raises(ValueError, match="交易日历尚未配置 2027"):
        database._is_trading_day_local("2027-01-04")


@pytest.mark.parametrize(
    ("date_str", "expected"),
    [
        ("2026-02-23", False),  # Spring Festival closure through Monday.
        ("2026-06-22", True),  # Dragon Boat market reopens Monday.
        ("2026-09-25", False),  # Mid-Autumn Festival closure.
        ("2026-10-08", True),  # National Day market reopens Thursday.
    ],
)
def test_2026_exchange_calendar_boundary_dates(date_str, expected):
    from db import database

    assert database._is_trading_day_local(date_str) is expected


def test_collector_skips_current_non_trading_day_without_fetch(monkeypatch):
    from workers import market_close_collector as collector

    monkeypatch.setattr(collector, "_today_local", lambda: "2026-07-12")
    monkeypatch.setattr(
        collector.database,
        "_is_trading_day_local",
        lambda value: False,
        raising=False,
    )
    monkeypatch.setattr(
        collector,
        "fetch_index_close_prices",
        lambda symbols: pytest.fail("non-trading day reached market feed"),
    )

    assert collector.run() == 0


@pytest.mark.parametrize("prices", [[], [{"malformed": True}]])
def test_collector_fails_when_trading_day_writes_no_prices(monkeypatch, prices):
    from workers import market_close_collector as collector

    monkeypatch.setattr(collector, "_today_local", lambda: "2026-07-10")
    monkeypatch.setattr(
        collector.database,
        "_is_trading_day_local",
        lambda value: True,
        raising=False,
    )
    monkeypatch.setattr(collector, "fetch_index_close_prices", lambda symbols: prices)
    monkeypatch.setattr(
        collector, "fetch_tencent_daily_close_prices", lambda *args, **kwargs: []
    )
    monkeypatch.setattr(collector.time, "sleep", lambda _delay: None)
    monkeypatch.setattr(
        collector.database,
        "store_future_prices",
        lambda records: pytest.fail("invalid records reached database"),
        raising=False,
    )

    with pytest.raises(RuntimeError, match="完整性校验"):
        collector.run()


def test_collector_retries_partial_batch_but_never_writes_it(monkeypatch):
    from workers import market_close_collector as collector

    target = "2026-07-10"
    close_ts = int(
        datetime(2026, 7, 10, 15, 0, tzinfo=ZoneInfo("Asia/Shanghai")).timestamp()
    )
    benchmark = {
        "symbol": "sh000001",
        "name": "上证指数",
        "close_price": 3001.5,
        "change_amt": 11.5,
        "change_pct": 0.38,
        "ts": close_ts,
        "source_trade_date": target,
    }
    monkeypatch.setattr(collector, "_today_local", lambda: target)
    monkeypatch.setattr(
        collector.database, "_is_trading_day_local", lambda _value: True
    )
    monkeypatch.setattr(collector.time, "time", lambda: close_ts + 600)
    sleeps = []
    monkeypatch.setattr(collector.time, "sleep", sleeps.append)
    monkeypatch.setattr(
        collector, "fetch_index_close_prices", lambda _symbols: [benchmark]
    )
    monkeypatch.setattr(
        collector, "fetch_tencent_daily_close_prices", lambda *args, **kwargs: []
    )
    monkeypatch.setattr(
        collector.database,
        "store_future_prices",
        lambda _records: pytest.fail("partial batch reached database"),
    )

    with pytest.raises(RuntimeError, match="sh000300"):
        collector.run(target)

    assert sleeps == [5, 15]


def test_collector_recovers_missing_required_index_from_exact_date_kline(
    monkeypatch,
):
    from workers import market_close_collector as collector

    target = "2026-07-10"
    close_ts = int(
        datetime(2026, 7, 10, 15, 0, tzinfo=ZoneInfo("Asia/Shanghai")).timestamp()
    )

    def record(symbol):
        return {
            "symbol": symbol,
            "name": symbol,
            "close_price": 3001.5,
            "change_amt": 11.5,
            "change_pct": 0.38,
            "ts": close_ts,
            "source_trade_date": target,
        }

    primary = [record(symbol) for symbol in collector.SYMBOLS if symbol != "sh000001"]
    fallback_calls = []
    monkeypatch.setattr(collector, "_today_local", lambda: target)
    monkeypatch.setattr(
        collector.database, "_is_trading_day_local", lambda _value: True
    )
    monkeypatch.setattr(collector.time, "time", lambda: close_ts + 600)
    monkeypatch.setattr(collector.time, "sleep", lambda _delay: None)
    monkeypatch.setattr(collector, "fetch_index_close_prices", lambda _symbols: primary)

    def fallback(symbols, *, trade_date):
        fallback_calls.append((symbols, trade_date))
        return [record("sh000001")]

    monkeypatch.setattr(collector, "fetch_tencent_daily_close_prices", fallback)
    captured = []
    monkeypatch.setattr(collector.database, "store_future_prices", captured.append)
    import services.event_outcomes as event_outcomes

    monkeypatch.setattr(
        event_outcomes,
        "materialize_event_outcomes",
        lambda _target: {"eligible": 0, "materialized": 0},
    )

    assert collector.run(target) == 4
    assert fallback_calls == [(["sh000001"], target)]
    assert [row["symbol"] for row in captured[0]] == collector.SYMBOLS


def test_collector_rejects_same_day_pre_close_quote(monkeypatch):
    from workers import market_close_collector as collector

    target = "2026-07-10"
    stale_ts = int(
        datetime(2026, 7, 10, 10, 0, tzinfo=ZoneInfo("Asia/Shanghai")).timestamp()
    )
    now = int(
        datetime(2026, 7, 10, 15, 10, tzinfo=ZoneInfo("Asia/Shanghai")).timestamp()
    )
    record = {
        "symbol": "sh000001",
        "name": "上证指数",
        "close_price": 3001.5,
        "change_amt": 11.5,
        "change_pct": 0.38,
        "ts": stale_ts,
        "source_trade_date": target,
    }

    assert collector._validated_record(record, target, now) is None


def test_collector_rejects_1459_continuous_or_auction_quote():
    from workers import market_close_collector as collector

    target = "2026-07-10"
    quote_ts = int(
        datetime(2026, 7, 10, 14, 59, tzinfo=ZoneInfo("Asia/Shanghai")).timestamp()
    )
    now = int(
        datetime(2026, 7, 10, 15, 10, tzinfo=ZoneInfo("Asia/Shanghai")).timestamp()
    )
    record = {
        "symbol": "sh000001",
        "name": "上证指数",
        "close_price": 3001.5,
        "change_amt": 11.5,
        "change_pct": 0.38,
        "ts": quote_ts,
        "source_trade_date": target,
    }

    assert collector._validated_record(record, target, now) is None


def test_collector_keeps_latest_valid_sample_across_retries(monkeypatch):
    from workers import market_close_collector as collector

    target = "2026-07-10"
    first_ts = int(
        datetime(2026, 7, 10, 15, 0, tzinfo=ZoneInfo("Asia/Shanghai")).timestamp()
    )
    latest_ts = first_ts + 60

    def record(symbol, ts, price):
        return {
            "symbol": symbol,
            "name": symbol,
            "close_price": price,
            "change_amt": 1.0,
            "change_pct": 0.1,
            "ts": ts,
            "source_trade_date": target,
        }

    batches = iter(
        [
            [record("sh000001", first_ts, 3000.0)],
            [
                record("sh000001", latest_ts, 3001.0),
                record("sh000300", latest_ts, 4001.0),
                record("sz399001", latest_ts, 10001.0),
                record("sz399006", latest_ts, 2001.0),
            ],
        ]
    )
    monkeypatch.setattr(collector, "_today_local", lambda: target)
    monkeypatch.setattr(
        collector.database, "_is_trading_day_local", lambda _value: True
    )
    monkeypatch.setattr(collector.time, "time", lambda: latest_ts + 600)
    monkeypatch.setattr(collector.time, "sleep", lambda _delay: None)
    monkeypatch.setattr(
        collector, "fetch_index_close_prices", lambda _symbols: next(batches)
    )
    captured = []
    monkeypatch.setattr(collector.database, "store_future_prices", captured.append)
    import services.event_outcomes as event_outcomes

    monkeypatch.setattr(
        event_outcomes,
        "materialize_event_outcomes",
        lambda _target: {"eligible": 0, "materialized": 0},
    )

    assert collector.run(target) == 4
    assert captured[0][0]["ts"] == latest_ts
    assert captured[0][0]["close_price"] == 3001.0
