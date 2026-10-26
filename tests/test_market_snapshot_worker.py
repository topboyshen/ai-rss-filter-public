import inspect
import math
from contextlib import contextmanager
from datetime import datetime
from zoneinfo import ZoneInfo

import pytest

from db import database
from modules import a_stock_data
from routes import dashboard_routes
from services import refresh, shared_cache


def _valid_quotes():
    result = {}
    for index, symbol in enumerate(dashboard_routes._MARKET_QUOTE_SYMBOLS):
        result[str(index)] = {
            "symbol": symbol,
            "name": f"指数{index}",
            "price": 3000.0 + index,
            "last_close": 2990.0 + index,
            "change_amt": 10.0,
            "change_pct": 0.33,
        }
    return result


def _prepare_success_path(monkeypatch, quotes):
    order = []
    shared_payloads = []
    stored_batches = []

    monkeypatch.setattr(dashboard_routes, "_quote_refresh_role_allowed", lambda: True)
    monkeypatch.setattr(a_stock_data, "tencent_quote", lambda codes: quotes)
    monkeypatch.setattr(shared_cache, "redis_enabled", lambda: True)

    def set_shared(name, payload, ttl):
        order.append("shared")
        shared_payloads.append((name, payload, ttl))
        return True

    def store(rows):
        order.append("store")
        stored_batches.append(rows)

    monkeypatch.setattr(shared_cache, "set_shared_json", set_shared)
    monkeypatch.setattr(database, "store_market_prices", store)

    @contextmanager
    def acquired_refresh_execution_lock():
        yield True

    monkeypatch.setattr(
        refresh, "refresh_execution_lock", acquired_refresh_execution_lock
    )
    with dashboard_routes._quotes_cache_lock:
        dashboard_routes._quotes_cache["html"] = None
        dashboard_routes._quotes_cache["ts"] = 0.0
    return order, shared_payloads, stored_batches


def test_worker_quote_refresh_defers_replaceable_db_snapshot_when_writer_busy(
    monkeypatch, caplog
):
    caplog.set_level("INFO", logger=dashboard_routes.logger.name)
    quotes = _valid_quotes()
    order, shared_payloads, stored_batches = _prepare_success_path(monkeypatch, quotes)

    @contextmanager
    def busy_refresh_execution_lock():
        yield False

    monkeypatch.setattr(refresh, "refresh_execution_lock", busy_refresh_execution_lock)

    assert dashboard_routes._refresh_quotes_cache() is True
    assert order == ["shared"]
    assert stored_batches == []
    assert shared_payloads[0][0] == "quotes"
    with dashboard_routes._quotes_cache_lock:
        assert dashboard_routes._quotes_cache["ts"] == shared_payloads[0][1]["ts"]
        assert dashboard_routes._quotes_cache["html"] == shared_payloads[0][1]["html"]
    assert "SQLite persistence deferred: refresh writer busy" in caplog.text


def test_worker_quote_refresh_fetches_through_hardened_module_and_persists_complete_batch(monkeypatch):
    quotes = _valid_quotes()
    requested = []

    def fetch(codes):
        requested.append(codes)
        return quotes

    order, shared_payloads, stored_batches = _prepare_success_path(monkeypatch, quotes)
    monkeypatch.setattr(a_stock_data, "tencent_quote", fetch)
    captured_at = 1_700_000_000
    monkeypatch.setattr(dashboard_routes.time, "time", lambda: captured_at)

    assert dashboard_routes._refresh_quotes_cache() is True
    assert requested == [list(dashboard_routes._MARKET_QUOTE_SYMBOLS)]
    assert order == ["shared", "store"]
    assert len(stored_batches) == 1
    rows = stored_batches[0]
    assert [row["symbol"] for row in rows] == list(dashboard_routes._MARKET_QUOTE_SYMBOLS)
    assert all(row["ts"] == captured_at for row in rows)
    # The A-share trade date is the Beijing calendar day, whatever the host TZ.
    beijing_day = datetime.fromtimestamp(captured_at, ZoneInfo("Asia/Shanghai")).date().isoformat()
    assert all(row["trade_date"] == beijing_day for row in rows)
    assert rows[0]["price"] == 3000.0
    assert rows[0]["prev_close"] == 2990.0
    assert shared_payloads[0][0] == "quotes"
    assert shared_payloads[0][1]["ts"] == captured_at
    assert "指数0" in shared_payloads[0][1]["html"]
    assert "VIX" not in shared_payloads[0][1]["html"]
    assert "降低整体仓位" not in shared_payloads[0][1]["html"]


@pytest.mark.parametrize("failure", ["missing", "invalid", "fetch_error"])
def test_worker_quote_refresh_never_writes_partial_or_invalid_batch(monkeypatch, failure):
    quotes = _valid_quotes()
    if failure == "missing":
        quotes.pop("3")
    elif failure == "invalid":
        quotes["0"]["price"] = math.nan

    monkeypatch.setattr(dashboard_routes, "_quote_refresh_role_allowed", lambda: True)

    def fetch(_codes):
        if failure == "fetch_error":
            raise ValueError("腾讯行情响应超过大小限制")
        return quotes

    monkeypatch.setattr(a_stock_data, "tencent_quote", fetch)
    monkeypatch.setattr(
        shared_cache,
        "set_shared_json",
        lambda *args, **kwargs: pytest.fail("invalid batch reached shared cache"),
    )
    monkeypatch.setattr(shared_cache, "redis_enabled", lambda: True)
    monkeypatch.setattr(
        database,
        "store_market_prices",
        lambda rows: pytest.fail("invalid batch reached database"),
    )

    assert dashboard_routes._refresh_quotes_cache() is False


def test_worker_quote_refresh_does_not_write_when_required_redis_publish_fails(monkeypatch):
    quotes = _valid_quotes()
    monkeypatch.setattr(dashboard_routes, "_quote_refresh_role_allowed", lambda: True)
    monkeypatch.setattr(a_stock_data, "tencent_quote", lambda codes: quotes)
    monkeypatch.setattr(shared_cache, "redis_enabled", lambda: True)
    monkeypatch.setattr(shared_cache, "set_shared_json", lambda *args, **kwargs: False)
    monkeypatch.setattr(
        database,
        "store_market_prices",
        lambda rows: pytest.fail("failed refresh reached database"),
    )
    assert dashboard_routes._refresh_quotes_cache() is False


def test_web_role_cannot_fetch_or_start_quote_refresher(monkeypatch):
    monkeypatch.setattr(dashboard_routes, "_quote_refresh_role_allowed", lambda: False)
    monkeypatch.setattr(
        a_stock_data,
        "tencent_quote",
        lambda codes: pytest.fail("web role reached network"),
    )
    monkeypatch.setattr(
        database,
        "store_market_prices",
        lambda rows: pytest.fail("web role reached database"),
    )
    monkeypatch.setattr(
        dashboard_routes.threading,
        "Thread",
        lambda *args, **kwargs: pytest.fail("web role started a thread"),
    )

    assert dashboard_routes._refresh_quotes_cache() is False
    assert dashboard_routes._ensure_quotes_refresher() is None


def test_quote_refresh_source_reuses_bounded_fixed_endpoint_module():
    from pathlib import Path

    source = (Path(__file__).resolve().parents[1] / "routes" / "dashboard_routes.py").read_text()
    refresh_source = inspect.getsource(dashboard_routes._refresh_quotes_cache)
    assert "tencent_quote(list(_MARKET_QUOTE_SYMBOLS))" in source
    assert "get_risk_status" not in refresh_source
    assert "render_risk_badge" not in refresh_source
    assert "qt.gtimg.cn" not in source
    assert "resp.read()" not in source
    assert a_stock_data.TENCENT_QUOTE_URL == "https://qt.gtimg.cn/q="
    assert a_stock_data.MAX_RESPONSE_BYTES == 512 * 1024
