from datetime import datetime
from zoneinfo import ZoneInfo

import pytest


BASE_URL = "https://ai.example.invalid"
_TZ = ZoneInfo("Asia/Shanghai")


def _ts(value: str) -> int:
    return int(datetime.strptime(value, "%Y-%m-%d %H:%M").replace(tzinfo=_TZ).timestamp())


@pytest.fixture
def database():
    import db.database as database_module

    database_module.init_db()
    return database_module


@pytest.fixture
def history_client(database, monkeypatch):
    import app as app_module
    import routes.security as security
    import routes.security_history_routes as history_routes
    from routes.browser_auth import DashboardAuth

    monkeypatch.setattr(
        history_routes,
        "get_dashboard_auth",
        lambda: DashboardAuth(kind="header"),
    )
    with security._rate_limit_lock:
        security._rate_limit_store.clear()
    app_module.app.config["TESTING"] = True
    with app_module.app.test_client() as client:
        yield client


def _security(database, *, ticker="002202", name="金风科技"):
    from db.event_core import upsert_security

    with database.get_connection() as conn:
        return upsert_security(
            conn,
            exchange="SZSE",
            ticker=ticker,
            name=name,
            asset_type="equity",
            aliases=[ticker, f"{ticker}.SZ"],
            industry="风电设备",
            concepts=["风电", "新能源"],
            now=_ts("2026-07-01 10:00"),
        )


def _event(database, security_id, key, *, title, available_at, relevance=9.0):
    from db.event_core import (
        resolve_or_create_event,
        upsert_event_evidence,
        upsert_event_security,
    )

    observed = _ts(available_at)
    with database.get_connection() as conn:
        event_id = resolve_or_create_event(
            conn,
            {"test": key},
            title=title,
            event_type="能源政策",
            direction="看多",
            priority=8.0,
            occurred_at=observed - 300,
            available_at=observed,
            strategy_version="history-test-v1",
            model_version="history-model-v1",
            now=observed,
        )
        mapping_id = upsert_event_security(
            conn,
            event_id,
            security_id,
            relation_type="direct",
            direction="看多",
            expected_gap=12,
            confidence=0.82,
            relevance=relevance,
            rationale="风电装机增长传导至整机需求",
            mapping_version="mapping-history-v1",
            now=observed + 1,
        )
        upsert_event_evidence(
            conn,
            event_id,
            source_uri=f"https://example.test/{key}",
            source_name="测试来源",
            claim_key=f"claim:{key}",
            claim_text=title,
            security_id=security_id,
            mapping_id=mapping_id,
            stance="supports",
            reliability=0.91,
            announcement_at=observed - 60,
            observed_at=observed,
            now=observed,
        )
    return event_id


def _prices(database):
    rows = []
    prior = 10.0
    for index, day in enumerate(
        ["2026-07-08", "2026-07-09", "2026-07-10", "2026-07-13", "2026-07-14"]
    ):
        close = 10.1 + index * 0.15
        rows.append(
            {
                "symbol": "sz002202",
                "trade_date": day,
                "name": "金风科技",
                "open_price": prior,
                "high_price": close + 0.2,
                "low_price": prior - 0.1,
                "close_price": close,
                "prev_close": prior,
                "change_amt": close - prior,
                "change_pct": (close / prior - 1) * 100,
                "volume": 1_000_000 + index,
                "amount": 10_000_000 + index,
                "suspension_state": "trading",
                "limit_state": "normal",
                "tradability_state": "trading_ohlc",
                "ts": _ts(f"{day} 15:00"),
            }
        )
        prior = close
    database.store_future_prices(rows)


def test_reverse_history_joins_canonical_events_prices_and_tradable_marker(database):
    from db.event_core import merge_events
    from services.security_history_query import load_security_history

    security_id = _security(database)
    canonical = _event(
        database,
        security_id,
        "canonical",
        title="海上风电新增开工",
        available_at="2026-07-09 16:00",
        relevance=9.1,
    )
    duplicate = _event(
        database,
        security_id,
        "duplicate",
        title="海上风电新增开工（重复稿）",
        available_at="2026-07-09 16:05",
        relevance=8.7,
    )
    _prices(database)
    with database.get_connection() as conn:
        merge_events(
            conn,
            duplicate,
            canonical,
            reason="same announcement",
            now=_ts("2026-07-09 17:00"),
        )

    model = load_security_history("szse", "002202", window=60)

    assert model["security"]["name"] == "金风科技"
    assert [row["trade_date"] for row in model["prices"]] == [
        "2026-07-08",
        "2026-07-09",
        "2026-07-10",
        "2026-07-13",
        "2026-07-14",
    ]
    assert len(model["events"]) == 1
    event = model["events"][0]
    assert event["event_id"] == canonical
    assert event["tradable_trade_date"] == "2026-07-10"
    assert event["baseline_trade_date"] == "2026-07-10"
    assert event["marker_state"] == "on_chart"
    assert event["evidence"][0]["source_uri"].startswith("https://example.test/")


def test_history_route_renders_server_side_kline_and_bidirectional_links(
    database, history_client
):
    security_id = _security(database)
    event_id = _event(
        database,
        security_id,
        "render",
        title="风电设备需求增长",
        available_at="2026-07-09 16:00",
    )
    _prices(database)

    response = history_client.get(
        "/dashboard/stocks/SZSE/002202", base_url=BASE_URL
    )
    body = response.get_data(as_text=True)

    assert response.status_code == 200
    assert "金风科技" in body
    assert "+1.42%" in body
    assert "日 K 与事件基准联动" in body
    assert "风电设备需求增长" in body
    assert f'href="#event-{event_id}"' in body
    assert f"/dashboard/events?event_id={event_id}&amp;tab=overview#event-detail" in body
    assert "历史关联验证，非交易回测" in body
    assert "<svg" in body
    assert "<script" not in body.lower()
    assert "script-src 'none'" in response.headers["Content-Security-Policy"]


def test_history_route_validates_auth_path_and_query(database, monkeypatch):
    import app as app_module
    import routes.security_history_routes as history_routes

    _security(database)
    monkeypatch.setattr(history_routes, "get_dashboard_auth", lambda: None)
    app_module.app.config["TESTING"] = True
    with app_module.app.test_client() as client:
        login = client.get(
            "/dashboard/stocks/SZSE/002202",
            base_url=BASE_URL,
            headers={"Accept": "text/html"},
        )
    assert login.status_code == 302
    assert login.headers["Location"].endswith(
        "/dashboard/login?next=/dashboard/stocks/SZSE/002202"
    )


@pytest.mark.parametrize(
    "path",
    [
        "/dashboard/stocks/NASDAQ/002202",
        "/dashboard/stocks/SZSE/not-six",
        "/dashboard/stocks/SZSE/002202?window=30",
        "/dashboard/stocks/SZSE/002202?window=60&window=120",
        "/dashboard/stocks/SZSE/002202?cursor=not-valid!",
        "/dashboard/stocks/SZSE/002202?cursor=a&cursor=b",
        "/dashboard/stocks/SZSE/002202?unexpected=1",
    ],
)
def test_history_route_rejects_malformed_requests(database, history_client, path):
    _security(database)
    assert history_client.get(path, base_url=BASE_URL).status_code == 400


def test_event_radar_links_real_equities_to_reverse_history(database, history_client, monkeypatch):
    import routes.event_radar_routes as radar_routes
    from routes.browser_auth import DashboardAuth

    security_id = _security(database)
    event_id = _event(
        database,
        security_id,
        "radar-link",
        title="风电事件",
        available_at="2026-07-09 16:00",
    )
    monkeypatch.setattr(
        radar_routes, "get_dashboard_auth", lambda: DashboardAuth(kind="header")
    )
    body = history_client.get(
        "/dashboard/events",
        query_string={"event_id": event_id},
        base_url=BASE_URL,
    ).get_data(as_text=True)
    assert 'href="/dashboard/stocks/SZSE/002202"' in body
    assert "查看该股事件历史与 K 线" in body


def test_stock_login_allowlist_accepts_only_strict_security_paths():
    from routes.browser_auth import _safe_next

    assert _safe_next("/dashboard/stocks/SZSE/002202") == "/dashboard/stocks/SZSE/002202"
    assert _safe_next("/dashboard/stocks/SZSE/002202?window=60") == "/dashboard"
    assert _safe_next("/dashboard/stocks/NASDAQ/002202") == "/dashboard"
    assert _safe_next("//dashboard/stocks/SZSE/002202") == "/dashboard"


def test_market_backfill_universe_uses_event_availability_not_replay_time(database):
    from db.event_core import resolve_or_create_event, upsert_event_security, upsert_security

    with database.get_connection() as conn:
        old_security = upsert_security(
            conn,
            exchange="SSE",
            ticker="600001",
            name="旧事件高相关股",
            asset_type="equity",
            now=100,
        )
        recent_security = upsert_security(
            conn,
            exchange="SSE",
            ticker="600002",
            name="新事件相关股",
            asset_type="equity",
            now=9000,
        )
        old_event = resolve_or_create_event(
            conn,
            {"test": "old-backfill-event"},
            title="很早以前的事件",
            occurred_at=100,
            available_at=100,
            now=100,
        )
        recent_event = resolve_or_create_event(
            conn,
            {"test": "recent-backfill-event"},
            title="最近发生的事件",
            occurred_at=9000,
            available_at=9000,
            now=9000,
        )
        # Simulate a full historical replay today.  The old mapping has the
        # newest write timestamp and the highest relevance, but must not crowd
        # the recent event out of a bounded market-data universe.
        upsert_event_security(
            conn,
            old_event,
            old_security,
            relation_type="direct",
            relevance=10,
            confidence=1,
            mapping_version="replay-v1",
            now=10_000,
        )
        upsert_event_security(
            conn,
            recent_event,
            recent_security,
            relation_type="direct",
            relevance=7,
            confidence=0.8,
            mapping_version="recent-v1",
            now=9_001,
        )

    universe = database.get_event_quote_universe(since=5_000, limit=1)
    assert [item["symbol"] for item in universe] == ["sh600002"]
