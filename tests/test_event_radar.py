import hashlib
import re
import uuid

import pytest


BASE_URL = "https://ai.example.invalid"
API_KEY = "event-radar-test-key"
SESSION_SECRET = "event-radar-session-secret-for-tests-0123456789"
XSS = '<svg/onload=alert("event-radar-xss")>'


def test_signed_values_never_hide_negative_direction():
    from routes.event_radar_routes import _signed_number

    assert _signed_number(55) == "+55.00"
    assert _signed_number(-55) == "−55.00"
    assert _signed_number(0) == "+0.00"


def test_event_type_view_hides_poisoned_legacy_labels():
    from routes.event_radar_routes import _event_view

    assert _event_view({"event_type": "�pérdidade"})["event_type"] == "其他事件"
    assert _event_view({"event_type": "-1"})["event_type"] == "其他事件"
    assert _event_view({"event_type": "囸级政策、国家级政策"})["event_type"] == "其他事件"
    assert _event_view({"event_type": "能源政策"})["event_type"] == "能源政策"


def test_retired_mapping_outcome_is_audit_only():
    from routes.event_radar_routes import _outcome_views

    outcome = _outcome_views(
        [
            {
                "mapping_current": False,
                "method_supported": True,
                "materialized": True,
                "direction_hit": True,
                "sample_size": 30,
                "independent_trading_days": 30,
                "security": {"name": "旧映射股票"},
            }
        ]
    )[0]

    assert outcome["mapping_current"] is False
    assert outcome["statistics_available"] is False
    assert outcome["method_label"] == "冻结信号结果 · 映射已退役，仅供审计"


@pytest.fixture
def database():
    import db.database as database_module

    database_module.init_db()
    return database_module


@pytest.fixture
def radar_client(database, monkeypatch):
    import app as app_module
    import routes.event_radar_routes as radar_routes
    import routes.security as security
    from routes.browser_auth import DashboardAuth

    monkeypatch.setattr(
        radar_routes,
        "get_dashboard_auth",
        lambda: DashboardAuth(kind="header"),
    )
    with security._rate_limit_lock:
        security._rate_limit_store.clear()
    app_module.app.config["TESTING"] = True
    with app_module.app.test_client() as client:
        yield client


def _event(
    database,
    key,
    *,
    title=None,
    event_type="policy",
    direction="看多",
    priority=8.0,
    occurred_at=100,
    now=200,
    available_at=None,
):
    from db.event_core import resolve_or_create_event

    with database.get_connection() as conn:
        return resolve_or_create_event(
            conn,
            {"test": str(key)},
            title=title or f"事件 {key}",
            event_type=event_type,
            direction=direction,
            priority=priority,
            occurred_at=occurred_at,
            available_at=available_at,
            strategy_version="radar-test-v1",
            model_version="model-test-v1",
            now=now,
        )


def _security(
    database,
    event_id,
    ticker,
    *,
    name=None,
    asset_type="equity",
    relation_type="direct",
    direction="看多",
    relevance=8.0,
    confidence=0.8,
    expected_gap=None,
    rationale=None,
    now=300,
):
    from db.event_core import upsert_event_security, upsert_security

    exchange = "SSE" if str(ticker).startswith("6") else "SZSE"
    with database.get_connection() as conn:
        security_id = upsert_security(
            conn,
            exchange=exchange,
            ticker=str(ticker),
            name=name or f"股票 {ticker}",
            asset_type=asset_type,
            aliases=[str(ticker)],
            industry="测试行业",
            concepts=["测试概念"],
            now=now,
        )
        mapping_id = upsert_event_security(
            conn,
            event_id,
            security_id,
            relation_type=relation_type,
            direction=direction,
            confidence=confidence,
            relevance=relevance,
            expected_gap=expected_gap,
            rationale=rationale or f"映射理由 {ticker}",
            mapping_version="mapping-test-v1",
            now=now + 1,
        )
    return security_id, mapping_id


def _evidence(
    database,
    event_id,
    url,
    claim,
    *,
    excerpt="",
    security_id=None,
    mapping_id=None,
    claim_key=None,
    reliability=0.9,
    observed_at=400,
    stance="supports",
):
    from db.event_core import upsert_event_evidence

    with database.get_connection() as conn:
        return upsert_event_evidence(
            conn,
            event_id,
            source_uri=url,
            source_name="https://feed.example/rss",
            claim_key=claim_key or f"claim:{uuid.uuid4()}",
            claim_text=claim,
            excerpt=excerpt,
            security_id=security_id,
            mapping_id=mapping_id,
            stance=stance,
            reliability=reliability,
            announcement_at=observed_at - 10,
            observed_at=observed_at,
            now=observed_at,
        )


def _cache_evidence_source(database, url, score_path):
    with database.get_connection() as conn:
        conn.execute(
            "INSERT INTO news_cache (link, title, created_at, score_path) "
            "VALUES (?, ?, ?, ?)",
            (url, f"缓存来源 {url}", 500, score_path),
        )
        conn.commit()


def test_source_revision_uses_latest_observation_not_support_or_prior(database):
    from services.event_radar_query import _load_sources

    event_id = _event(database, "stance-revision", now=50)
    url = "https://news.example/stance-revision"
    _evidence(database, event_id, url, "获得订单", observed_at=100, reliability=0.99)
    _evidence(
        database, event_id, url, "否认获得订单", stance="contradicts",
        observed_at=200, reliability=0.2,
    )
    with database.get_connection() as conn:
        before = conn.execute("SELECT * FROM event_evidence ORDER BY evidence_id").fetchall()
        sources, total, truncated, summary = _load_sources(conn, event_id, 250)
        after = conn.execute("SELECT * FROM event_evidence ORDER BY evidence_id").fetchall()
    assert total == 1 and not truncated
    assert summary == "否认获得订单"
    assert sources[0]["stance"] == "contradicts"
    assert sources[0]["has_stance_conflict"] is True
    assert {item["claim_text"] for item in sources[0]["observations"]} == {
        "获得订单", "否认获得订单",
    }
    assert list(map(tuple, before)) == list(map(tuple, after))


def test_source_revision_read_at_does_not_borrow_future_correction(database):
    from services.event_radar_query import _load_sources

    event_id = _event(database, "stance-revision-asof", now=50)
    url = "https://news.example/asof"
    _evidence(database, event_id, url, "获得订单", observed_at=100)
    _evidence(database, event_id, url, "否认获得订单", stance="contradicts", observed_at=200)
    with database.get_connection() as conn:
        sources, total, _truncated, summary = _load_sources(conn, event_id, 150)
    assert total == 1 and summary == "获得订单"
    assert sources[0]["observation_count"] == 1
    assert sources[0]["has_stance_conflict"] is False
    assert sources[0]["observations"] == []


def test_source_revision_history_is_bounded_but_keeps_each_stance(database):
    from services.event_radar_query import MAX_SOURCE_OBSERVATIONS, _load_sources

    event_id = _event(database, "stance-bounded", now=50)
    url = "https://news.example/bounded"
    _evidence(database, event_id, url, "旧否认记录", stance="contradicts", observed_at=100)
    for offset in range(12):
        _evidence(database, event_id, url, f"支持记录 {offset}", observed_at=200 + offset)
    with database.get_connection() as conn:
        sources, total, _truncated, summary = _load_sources(conn, event_id, 300)
    source = sources[0]
    assert total == 1 and summary == "支持记录 11"
    assert source["observation_count"] == 13
    assert len(source["observations"]) == MAX_SOURCE_OBSERVATIONS
    assert source["observations_truncated"] is True
    assert source["has_stance_conflict"] is True
    assert {item["stance"] for item in source["observations"]} == {"supports", "contradicts"}


def test_source_revision_distinct_claims_remain_observations_not_fact_verdict(database):
    from services.event_radar_query import _load_sources

    event_id = _event(database, "distinct-claims", now=50)
    url = "https://news.example/distinct-claims"
    _evidence(database, event_id, url, "甲公司获得订单", observed_at=100)
    _evidence(database, event_id, url, "乙公司发布会议通知", stance="context", observed_at=200)
    with database.get_connection() as conn:
        sources, _total, _truncated, summary = _load_sources(conn, event_id, 300)
    assert summary == "乙公司发布会议通知"
    assert sources[0]["has_stance_conflict"] is False
    assert len(sources[0]["observations"]) == 2


def test_source_revision_page_discloses_conflict_and_old_record(database, radar_client):
    event_id = _event(database, "revision-html", now=50)
    url = "https://news.example/revision-html"
    _evidence(database, event_id, url, "获得项目订单", observed_at=100)
    _evidence(database, event_id, url, "否认获得项目订单", stance="contradicts", observed_at=200)
    response = radar_client.get(
        "/dashboard/events", query_string={"event_id": event_id}, base_url=BASE_URL,
    )
    body = response.get_data(as_text=True)
    assert response.status_code == 200
    assert "同一链接存在不同立场记录" in body
    assert "不等于同一声明已被证伪" in body
    assert "查看留存观察" in body
    assert "收录" in body
    assert "否认获得项目订单" in body and "获得项目订单" in body


def test_source_revision_history_is_batched_and_keeps_public_visibility(database):
    from services.event_radar_query import _load_sources

    event_id = _event(database, "revision-batch", now=50)
    for index in range(7):
        url = f"https://news.example/revision-batch/{index}"
        _evidence(database, event_id, url, "支持记录", observed_at=100)
        _evidence(database, event_id, url, "背景记录", stance="context", observed_at=150)
        _evidence(database, event_id, url, "否认记录", stance="contradicts", observed_at=200)
    private_url = "https://news.example/revision-batch/private"
    _evidence(database, event_id, private_url, "不可公开记录", observed_at=200)
    _cache_evidence_source(database, private_url, "quality_quarantine")
    with database.get_connection() as conn:
        queries = []
        conn.set_trace_callback(queries.append)
        sources, total, _truncated, _summary = _load_sources(conn, event_id, 250)
        conn.set_trace_callback(None)
    assert total == len(sources) == 7
    assert len(queries) == 2
    assert all(len(source["observations"]) == 3 for source in sources)
    assert all(source["url"] != private_url for source in sources)


def test_source_revision_old_markup_is_not_rendered_as_html(database, radar_client):
    event_id = _event(database, "revision-html-escape", now=50)
    url = "https://news.example/revision-html-escape"
    _evidence(database, event_id, url, XSS + "旧观察", observed_at=100)
    _evidence(database, event_id, url, "新观察", stance="context", observed_at=200)
    body = radar_client.get(
        "/dashboard/events", query_string={"event_id": event_id}, base_url=BASE_URL,
    ).get_data(as_text=True)
    assert "旧观察" in body and "新观察" in body
    assert XSS not in body
    assert '<svg/onload=' not in body


def _contract_observation(database, event_id, url, claim, *, current, observed_at, stance):
    from core.source_contract import ARTICLE_CLAIM_PREFIX
    from db.event_core import upsert_event_evidence

    excerpt = "" if current else "旧生成摘要，不能作为原文证据"
    snapshot = claim if current else claim + "\n" + excerpt
    digest = hashlib.sha256(snapshot.encode()).hexdigest()
    prefix = ARTICLE_CLAIM_PREFIX if current else "article_snapshot:"
    with database.get_connection() as conn:
        return upsert_event_evidence(
            conn, event_id, source_uri=url, source_name="测试发布方", source_type="article",
            claim_key=prefix + digest, claim_text=claim, excerpt=excerpt,
            snapshot_text=snapshot, snapshot_hash=digest, stance=stance,
            reliability=0.9, observed_at=observed_at, now=observed_at,
        )


@pytest.mark.parametrize("current_first", [False, True])
def test_source_revision_mixed_contracts_keep_each_observation_identity_and_cutoff(database, current_first):
    from services.event_radar_query import _load_sources

    event_id = _event(database, "mixed-source-contracts", now=50)
    url = "https://news.example/mixed-contracts"
    first = _contract_observation(
        database, event_id, url, "第一次留存的标题", current=current_first,
        observed_at=100, stance="supports",
    )
    last = _contract_observation(
        database, event_id, url, "后续否认记录的标题", current=not current_first,
        observed_at=200, stance="contradicts",
    )
    with database.get_read_only_connection() as conn:
        before = [tuple(row) for row in conn.execute("SELECT * FROM event_evidence ORDER BY evidence_id")]
        earlier, _, _, early_summary = _load_sources(conn, event_id, 150)
        sources, total, _, summary = _load_sources(conn, event_id, 250)
        after = [tuple(row) for row in conn.execute("SELECT * FROM event_evidence ORDER BY evidence_id")]
    assert before == after
    assert early_summary == "第一次留存的标题"
    assert earlier[0]["evidence_id"] == first
    assert earlier[0]["source_contract_state"] == ("current" if current_first else "legacy_unknown")
    assert earlier[0]["observation_count"] == 1 and not earlier[0]["observations"]
    assert total == 1 and summary == "后续否认记录的标题"
    source = sources[0]
    assert source["evidence_id"] == last
    assert source["source_contract_state"] == ("legacy_unknown" if current_first else "current")
    assert source["has_stance_conflict"] is True
    states = {item["evidence_id"]: item["source_contract_state"] for item in source["observations"]}
    assert states == {
        first: "current" if current_first else "legacy_unknown",
        last: "legacy_unknown" if current_first else "current",
    }
    assert all("不等于事实核验" in item["stance_record_label"] for item in source["observations"])


def test_source_revision_page_labels_current_and_legacy_history_independently(database, radar_client):
    event_id = _event(database, "mixed-contract-history-html", now=50)
    url = "https://news.example/mixed-contract-history-html"
    _contract_observation(
        database, event_id, url, "旧契约独有标题", current=False,
        observed_at=100, stance="supports",
    )
    _contract_observation(
        database, event_id, url, "新契约独有标题", current=True,
        observed_at=200, stance="contradicts",
    )
    response = radar_client.get(
        "/dashboard/events", query_string={"event_id": event_id}, base_url=BASE_URL,
    )
    assert response.status_code == 200
    body = response.get_data(as_text=True)
    history = body.split("查看留存观察", 1)[1].split("</details>", 1)[0]
    entries = re.findall(r"<li>\s*(.*?)</li>", history, flags=re.DOTALL)
    assert len(entries) == 2
    old = next(entry for entry in entries if "旧契约独有标题" in entry)
    new = next(entry for entry in entries if "新契约独有标题" in entry)
    assert "历史来源记录 · 来源契约未知" in old
    assert "历史立场字段：支持（不等于事实核验）" in old
    assert "存档标题（来源契约未核验）：旧契约独有标题" in old
    assert "存档摘录（不作为原文证据）" in old
    assert "当前标题留存契约 · 不等于事实核验" in new
    assert "记录立场：否认（不等于事实核验）" in new
    assert "发布方标题：新契约独有标题" in new
    assert "历史来源记录" not in new


def _event_page(database, *, read_at=1_000):
    from services.event_radar_query import _load_event_page

    with database.get_connection() as conn:
        return _load_event_page(
            conn,
            cursor=None,
            limit=20,
            has_stocks=False,
            read_at=read_at,
        )


def _ready_provenance_shadow(**overrides):
    payload = {
        "status": "ready",
        "reason": "complete",
        "projection_version": "shadow-test-v1",
        "root_event_version": 1,
        "raw_evidence_count": 4,
        "projected_evidence_count": 4,
        "publisher_group_count": 3,
        "lineage_unit_count": 2,
        "first_party_evidence_count": 0,
        "official_record_evidence_count": 1,
        "claim_count": 5,
        "claim_state_counts": {
            "single_support": 1,
            "multi_independent_support": 1,
            "contested": 1,
            "contradicted_only": 1,
            "context_only": 1,
        },
        "latest_evidence_at": 400,
        "computed_at": 500,
    }
    payload.update(overrides)
    return payload


def test_authentication_precedes_query_and_preserves_radar_login_target(
    database, monkeypatch
):
    import app as app_module
    import routes.event_radar_routes as radar_routes

    monkeypatch.setattr(radar_routes, "get_dashboard_auth", lambda: None)
    monkeypatch.setattr(
        radar_routes,
        "load_event_radar",
        lambda **_kwargs: pytest.fail("event query ran before authentication"),
    )

    with app_module.app.test_client() as client:
        html = client.get(
            "/dashboard/events?key=must-never-authorize",
            base_url=BASE_URL,
            headers={"Accept": "text/html"},
        )
        explicit_bad_header = client.get(
            "/dashboard/events",
            base_url=BASE_URL,
            headers={"Accept": "text/html", "X-API-Key": "wrong"},
        )

    assert html.status_code == 302
    assert html.headers["Location"] == "/dashboard/login?next=/dashboard/events"
    assert explicit_bad_header.status_code == 401
    assert explicit_bad_header.get_json() == {"error": "Unauthorized"}


@pytest.mark.parametrize(
    ("next_path", "expected"),
    [
        ("/dashboard/events", "/dashboard/events"),
        ("https://evil.example/steal", "/dashboard"),
        ("//evil.example/steal", "/dashboard"),
        ("/dashboard/events?event_id=poison", "/dashboard"),
    ],
)
def test_login_next_allowlist_is_exact(database, monkeypatch, next_path, expected):
    import app as app_module
    import routes.browser_auth as browser_auth

    monkeypatch.setenv("AI_FILTER_SESSION_SECRET", SESSION_SECRET)
    monkeypatch.delenv("AI_FILTER_SESSION_SECRET_FILE", raising=False)
    monkeypatch.setattr(browser_auth, "_security_cfg", lambda: {"api_key": API_KEY})
    monkeypatch.setattr(
        browser_auth, "_consume_login_attempt", lambda _secret: (True, 1)
    )
    monkeypatch.setattr(browser_auth, "_clear_login_attempts", lambda _secret: None)
    monkeypatch.setattr(
        browser_auth, "_store_session", lambda _secret, _session_id: None
    )

    app_module.app.config["TESTING"] = True
    with app_module.app.test_client() as client:
        page = client.get(
            "/dashboard/login",
            query_string={"next": next_path},
            base_url=BASE_URL,
        )
        match = re.search(
            r'name="csrf_token"\s+value="([^"]+)"',
            page.get_data(as_text=True),
        )
        assert page.status_code == 200
        assert match
        response = client.post(
            "/dashboard/login",
            base_url=BASE_URL,
            data={
                "csrf_token": match.group(1),
                "api_key": API_KEY,
                "next": next_path,
            },
        )

    assert response.status_code == 303
    assert response.headers["Location"] == expected


def test_dual_cycle_overview_does_not_query_outcomes(database, radar_client, monkeypatch):
    from services import event_radar_query

    event_id = _event(database, "dual-cycle-overview")
    source_url = "https://news.example/dual-cycle"
    _evidence(database, event_id, source_url, "可公开原文证据")
    _cache_evidence_source(database, source_url, "llm")

    def unexpected_outcome_read(*_args, **_kwargs):
        pytest.fail("overview must not query or materialize market outcomes")

    monkeypatch.setattr(event_radar_query, "_load_outcomes", unexpected_outcome_read)
    response = radar_client.get(
        f"/dashboard/events?event_id={event_id}", base_url=BASE_URL
    )
    assert response.status_code == 200
    body = response.get_data(as_text=True)
    assert "短线关注" in body and "波段关注" in body
    assert "概览未读取市场结果，不判断是否到期、上涨或下跌" in body
    assert "尚未到期" not in body


def test_empty_page_is_safe_server_rendered_and_has_no_script(radar_client):
    response = radar_client.get(
        "/dashboard/events", base_url=BASE_URL, headers={"Accept": "text/html"}
    )
    body = response.get_data(as_text=True)

    assert response.status_code == 200
    assert "当前窗口暂无事件" in body
    assert "请选择一个事件" in body
    assert response.headers["Cache-Control"].startswith("no-store")
    assert "script-src 'none'" in response.headers["Content-Security-Policy"]
    assert "frame-ancestors 'none'" in response.headers["Content-Security-Policy"]
    assert "<script" not in body.lower()


def test_event_page_keeps_event_without_evidence(database):
    event_id = _event(database, "public-page-no-evidence", occurred_at=700)

    items, next_cursor, has_more = _event_page(database)

    assert [item["event_id"] for item in items] == [event_id]
    assert items[0]["source_count"] == 0
    assert next_cursor is None
    assert has_more is False


def test_event_page_keeps_public_cached_evidence(database):
    event_id = _event(database, "public-page-public", occurred_at=700)
    source_url = "https://news.example/public-page"
    _evidence(database, event_id, source_url, "可公开证据")
    _cache_evidence_source(database, source_url, "llm")

    items, _next_cursor, _has_more = _event_page(database)

    assert [item["event_id"] for item in items] == [event_id]
    assert items[0]["source_count"] == 1


def test_event_page_excludes_event_with_only_nonpublic_evidence(database):
    event_id = _event(database, "public-page-nonpublic", occurred_at=700)
    source_url = "https://news.example/nonpublic-page"
    _evidence(database, event_id, source_url, "不可公开证据")
    _cache_evidence_source(
        database,
        source_url,
        " LLM_SAFE_NEUTRAL_CONTRACT ",
    )

    items, next_cursor, has_more = _event_page(database)

    assert event_id not in {item["event_id"] for item in items}
    assert items == []
    assert next_cursor is None
    assert has_more is False


def test_event_page_uses_public_evidence_from_merged_group(database):
    from db.event_core import merge_events

    root = _event(database, "public-page-merged-root", occurred_at=700)
    child = _event(database, "public-page-merged-child", occurred_at=690)
    private_url = "https://news.example/merged-private"
    public_url = "https://news.example/merged-public"
    _evidence(database, root, private_url, "根事件不可公开证据")
    _evidence(database, child, public_url, "合并成员公开证据")
    _cache_evidence_source(database, private_url, "quality_quarantine")
    _cache_evidence_source(database, public_url, "stage2")
    with database.get_connection() as conn:
        assert merge_events(conn, child, root, reason="same public event", now=600) == root

    items, _next_cursor, _has_more = _event_page(database)
    by_id = {item["event_id"]: item for item in items}

    assert set(by_id) == {root}
    assert child not in by_id
    assert by_id[root]["source_count"] == 1


def test_stable_uuid_and_merged_group_sources_are_preserved(database, radar_client):
    from db.event_core import merge_events, resolve_or_create_event
    from services.event_radar_query import load_event_radar

    root = _event(database, "canonical", title="稳定事件原题", occurred_at=500)
    source = _event(database, "merged-source", title="合并前事件", occurred_at=490)
    first_url = "https://news.example/one"
    second_url = "https://news.example/two"
    _evidence(
        database,
        source,
        first_url,
        "合并来源证据",
        excerpt="来自合并成员的摘要",
        claim_key="snapshot-one",
        reliability=0.99,
    )
    # A second claim for the same URL is one independent source, not two.
    _evidence(
        database,
        source,
        first_url,
        "同一来源更新",
        claim_key="snapshot-two",
        reliability=0.8,
        observed_at=401,
    )
    _evidence(database, root, second_url, "根事件证据", claim_key="root-source")

    with database.get_connection() as conn:
        assert merge_events(conn, source, root, reason="same stable event") == root
        assert resolve_or_create_event(
            conn,
            {"test": "canonical"},
            title="稳定事件更新后的标题",
            event_type="policy",
            direction="看多",
            priority=8,
            occurred_at=500,
            strategy_version="radar-test-v1",
            model_version="model-test-v1",
            now=600,
        ) == root

    model = load_event_radar(event_id=root, limit=10)
    detail = model["detail"]

    assert str(uuid.UUID(root)) == root
    assert model["selected_event_id"] == root
    assert detail["event"]["event_id"] == root
    assert detail["event"]["title"] == "稳定事件更新后的标题"
    assert detail["group_size"] == 2
    assert detail["source_count"] == 2
    assert {item["url"] for item in detail["sources"]} == {first_url, second_url}
    assert sum(item["url"] == first_url for item in detail["sources"]) == 1
    # Across distinct URLs the existing source-prior ordering is unchanged;
    # within the member URL its latest observation is now selected.
    assert detail["summary"] == "根事件证据"
    member = next(item for item in detail["sources"] if item["url"] == first_url)
    assert member["original_claim"] == "同一来源更新"
    assert member["source_excerpt"] == ""
    assert any(item["source_excerpt"] == "来自合并成员的摘要" for item in member["observations"])
    assert member["claim_text"] == member["original_claim"]
    assert {item["event_id"] for item in model["events"]} == {root}
    body = radar_client.get(
        "/dashboard/events",
        query_string={"event_id": root},
        base_url=BASE_URL,
    ).get_data(as_text=True)
    assert "合并来源证据" in body
    assert "存档摘录（不作为原文证据）" in body
    assert "来自合并成员的摘要" in body

    merged_request = load_event_radar(event_id=source, limit=10)
    assert merged_request["selected_event_id"] == root
    redirect_response = radar_client.get(
        "/dashboard/events",
        query_string={"event_id": source},
        base_url=BASE_URL,
    )
    assert redirect_response.status_code == 308
    assert f"event_id={root}" in redirect_response.headers["Location"]


def test_provenance_shadow_ready_projection_is_aggregate_only(
    database, radar_client, monkeypatch
):
    import services.event_radar_query as radar_query

    event_id = _event(
        database,
        "provenance-shadow-ready",
        title="影子证据拓扑展示",
        occurred_at=600,
    )
    secret_marker = "shadow-secret.example/private-claim"
    payload = _ready_provenance_shadow(
        reason=secret_marker,
        projection_version=secret_marker,
        evidence_id=secret_marker,
        publisher_domains=[secret_marker],
        source_uri=f"https://{secret_marker}",
        claim_text=secret_marker,
    )
    provenance_config = {"rules": []}
    observed_config = {}
    monkeypatch.setitem(
        radar_query.runtime_config.cfg,
        "source_provenance",
        provenance_config,
    )

    def ready_loader(_conn, _root_event_id, *, config=None):
        observed_config["value"] = config
        return payload

    monkeypatch.setattr(
        radar_query,
        "_load_event_provenance_shadow",
        ready_loader,
    )

    model = radar_query.load_event_radar(event_id=event_id)
    assert observed_config["value"] is radar_query.runtime_config.cfg
    assert observed_config["value"]["source_provenance"] is provenance_config
    shadow = model["detail"]["provenance_shadow"]
    assert shadow == {
        "status": "ready",
        "raw_evidence_count": 4,
        "projected_evidence_count": 4,
        "publisher_group_count": 3,
        "lineage_unit_count": 2,
        "first_party_evidence_count": 0,
        "official_record_evidence_count": 1,
        "claim_count": 5,
        "claim_state_counts": {
            "single_support": 1,
            "multi_independent_support": 1,
            "contested": 1,
            "contradicted_only": 1,
            "context_only": 1,
        },
        "latest_evidence_at": 400,
        "computed_at": 500,
    }

    response = radar_client.get(
        "/dashboard/events",
        query_string={"event_id": event_id},
        base_url=BASE_URL,
    )
    body = response.get_data(as_text=True)
    assert response.status_code == 200
    assert "影子证据拓扑，不参与评分/归并/映射/后验。" in body
    assert "原始证据</small><strong>4</strong>" in body
    assert "发布方组</small><strong>3</strong>" in body
    assert "转载谱系单元</small><strong>2</strong>" in body
    assert "主体一手源</small><strong>未标注</strong>" in body
    assert "主体一手证据</small><strong>1</strong>" not in body
    assert "规则判定多发布方支持 1" in body
    assert "转载谱系只折叠完全相同的冻结快照" in body
    assert "轻微改写的转载仍可能分开" in body
    assert "多方独立支持" not in body
    assert secret_marker not in body
    assert "shadow-test-v1" not in body
    assert "multi_independent_support" not in body


def test_provenance_shadow_nonready_incomplete_or_failed_is_pending(
    database, radar_client, monkeypatch
):
    import services.event_radar_query as radar_query

    event_id = _event(
        database,
        "provenance-shadow-pending",
        title="影子投影待计算",
        occurred_at=610,
    )

    for payload in (
        {"status": "pending", "raw_evidence_count": 999},
        {"status": "degraded", "raw_evidence_count": 999},
        _ready_provenance_shadow(claim_state_counts={"single_support": 5}),
        _ready_provenance_shadow(projected_evidence_count=3),
        _ready_provenance_shadow(first_party_evidence_count=1),
    ):
        monkeypatch.setattr(
            radar_query,
            "_load_event_provenance_shadow",
            lambda _conn, _root_event_id, value=payload, **_kwargs: value,
        )
        model = radar_query.load_event_radar(event_id=event_id)
        assert model["detail"]["provenance_shadow"]["status"] in {
            "pending",
            "degraded",
        }
        assert set(model["detail"]["provenance_shadow"]) == {"status"}
        body = radar_client.get(
            "/dashboard/events",
            query_string={"event_id": event_id},
            base_url=BASE_URL,
        ).get_data(as_text=True)
        assert "待计算：影子账本不存在、投影不完整" in body
        assert "原始证据</small><strong>999</strong>" not in body

    def failed_loader(_conn, _root_event_id, **_kwargs):
        raise RuntimeError("must not escape into the Event Radar response")

    monkeypatch.setattr(
        radar_query, "_load_event_provenance_shadow", failed_loader
    )
    response = radar_client.get(
        "/dashboard/events",
        query_string={"event_id": event_id},
        base_url=BASE_URL,
    )
    assert response.status_code == 200
    assert "待计算：影子账本不存在、投影不完整" in response.get_data(
        as_text=True
    )


def test_provenance_shadow_registry_version_mismatch_stays_pending(
    database, radar_client, monkeypatch
):
    import services.event_radar_query as radar_query

    event_id = _event(
        database,
        "provenance-shadow-registry-mismatch",
        title="影子规则版本变化",
        occurred_at=620,
    )
    config_section = {
        "rules": [
            {
                "publisher_group": "wire-example",
                "domains": ["wire.example"],
                "source_class": "wire",
            }
        ]
    }
    monkeypatch.setitem(
        radar_query.runtime_config.cfg,
        "source_provenance",
        config_section,
    )

    def mismatch_loader(_conn, _root_event_id, *, config=None):
        assert config is radar_query.runtime_config.cfg
        assert config["source_provenance"] is config_section
        return {
            "status": "pending",
            "reason": "input_incomplete",
            "projection_version": "must-not-be-rendered",
            "raw_evidence_count": 999,
        }

    monkeypatch.setattr(
        radar_query, "_load_event_provenance_shadow", mismatch_loader
    )
    model = radar_query.load_event_radar(event_id=event_id)
    assert model["detail"]["provenance_shadow"] == {"status": "pending"}
    body = radar_client.get(
        "/dashboard/events",
        query_string={"event_id": event_id},
        base_url=BASE_URL,
    ).get_data(as_text=True)
    assert "待计算：影子账本不存在、投影不完整" in body
    assert "must-not-be-rendered" not in body
    assert "原始证据</small><strong>999</strong>" not in body


def test_invalid_explicit_registry_cannot_reuse_an_old_empty_rules_projection(
    database, radar_client, monkeypatch
):
    import services.event_radar_query as radar_query
    from services.provenance_shadow import (
        load_event_provenance_shadow,
        project_provenance_shadow,
    )

    event_id = _event(
        database,
        "provenance-shadow-invalid-registry",
        title="非法影子规则不得回读旧结果",
        occurred_at=630,
    )
    _evidence(
        database,
        event_id,
        "https://source.example/old-empty-rules",
        "旧空规则版本曾经完成投影",
    )
    with database.get_connection() as conn:
        projected = project_provenance_shadow(conn, config={}, now=700)
        assert projected["complete"] is True
        assert load_event_provenance_shadow(conn, event_id, config={})["status"] == "ready"

    monkeypatch.setitem(
        radar_query.runtime_config.cfg,
        "source_provenance",
        ["invalid-explicit-registry"],
    )
    model = radar_query.load_event_radar(event_id=event_id)
    assert model["detail"]["provenance_shadow"] == {"status": "pending"}
    body = radar_client.get(
        "/dashboard/events",
        query_string={"event_id": event_id},
        base_url=BASE_URL,
    ).get_data(as_text=True)
    assert "待计算：影子账本不存在、投影不完整" in body
    assert "原始证据</small><strong>1</strong>" not in body


def test_optional_shadow_initialization_error_does_not_break_query_module_import(
    monkeypatch,
):
    import builtins
    import importlib.util
    import sys
    from pathlib import Path

    import services.event_radar_query as radar_query

    original_import = builtins.__import__

    def isolated_import(name, *args, **kwargs):
        if name == "services.provenance_shadow":
            raise SyntaxError("isolated optional sidecar initialization failure")
        return original_import(name, *args, **kwargs)

    module_name = "_event_radar_query_optional_sidecar_import_test"
    spec = importlib.util.spec_from_file_location(module_name, Path(radar_query.__file__))
    assert spec is not None and spec.loader is not None
    isolated = importlib.util.module_from_spec(spec)
    monkeypatch.setattr(builtins, "__import__", isolated_import)
    sys.modules[module_name] = isolated
    try:
        spec.loader.exec_module(isolated)
    finally:
        sys.modules.pop(module_name, None)

    assert isolated._load_event_provenance_shadow is None


def test_equity_top_three_excludes_index_and_keeps_mapping_evidence_exact(database):
    from services.event_radar_query import load_event_radar

    event_id = _event(database, "stocks", occurred_at=700)
    mappings = []
    for number, (ticker, relevance, relation) in enumerate(
        [
            ("600001", 9.8, "direct"),
            ("000002", 9.2, "upstream"),
            ("600003", 8.6, "downstream"),
            ("000004", 7.5, "ecosystem"),
        ]
    ):
        mappings.append(
            _security(
                database,
                event_id,
                ticker,
                name=f"个股{number + 1}",
                relation_type=relation,
                relevance=relevance,
                confidence=0.9 - number * 0.1,
                now=710 + number * 10,
            )
        )
    index_security, index_mapping = _security(
        database,
        event_id,
        "000001",
        name="上证综指",
        asset_type="index",
        relation_type="benchmark_proxy",
        relevance=10,
        confidence=1,
        now=800,
    )
    first_security, first_mapping = mappings[0]
    second_security, second_mapping = mappings[1]
    _evidence(
        database,
        event_id,
        "https://evidence.example/first",
        "第一只股票专属证据",
        security_id=first_security,
        mapping_id=first_mapping,
        claim_key="mapping-first",
    )
    _evidence(
        database,
        event_id,
        "https://evidence.example/second",
        "第二只股票专属证据",
        security_id=second_security,
        mapping_id=second_mapping,
        claim_key="mapping-second",
    )
    _evidence(
        database,
        event_id,
        "https://evidence.example/index",
        "指数代理证据",
        security_id=index_security,
        mapping_id=index_mapping,
        claim_key="mapping-index",
    )

    model = load_event_radar(event_id=event_id)
    detail = model["detail"]
    stocks = detail["stocks"]

    assert [stock["name"] for stock in stocks] == ["个股1", "个股2", "个股3"]
    page_event = next(item for item in model["events"] if item["event_id"] == event_id)
    assert [stock["name"] for stock in page_event["stocks"]] == [
        "个股1", "个股2", "个股3",
    ]
    assert all(stock["asset_type"].lower() == "equity" for stock in stocks)
    assert "上证综指" not in {stock["name"] for stock in stocks}
    assert {stock["relations"][0]["relation_label"] for stock in stocks} == {
        "直接影响",
        "上游",
        "下游",
    }
    first = stocks[0]["relations"][0]
    second = stocks[1]["relations"][0]
    assert first["mapping_id"] == first_mapping
    assert [item["claim_text"] for item in first["evidence"]] == [
        "第一只股票专属证据"
    ]
    assert [item["claim_text"] for item in second["evidence"]] == [
        "第二只股票专属证据"
    ]
    assert all(
        item["claim_text"] != "指数代理证据"
        for stock in stocks
        for relation in stock["relations"]
        for item in relation["evidence"]
    )


def test_has_stocks_filter_marks_only_canonical_events_with_real_equities(
    database, radar_client
):
    from db.event_core import merge_events
    from services.event_radar_query import load_event_radar

    root = _event(
        database,
        "stock-filter-root",
        title="根事件有真实相关股票",
        occurred_at=880,
    )
    child = _event(
        database,
        "stock-filter-child",
        title="合并成员承载个股映射",
        occurred_at=870,
    )
    _security(
        database,
        child,
        "600519",
        name="贵州茅台",
        relation_type="direct",
        now=881,
    )
    with database.get_connection() as conn:
        merge_events(conn, child, root, reason="same stable event", now=890)

    no_stock = _event(
        database,
        "stock-filter-empty",
        title="没有股票关系",
        occurred_at=860,
    )
    index_only = _event(
        database,
        "stock-filter-index",
        title="只有指数代理",
        occurred_at=850,
    )
    _security(
        database,
        index_only,
        "000001",
        name="上证综指",
        asset_type="index",
        relation_type="benchmark_proxy",
        now=891,
    )

    unfiltered = load_event_radar(limit=10)
    by_id = {item["event_id"]: item for item in unfiltered["events"]}
    assert by_id[root]["has_stocks"] is True
    assert by_id[no_stock]["has_stocks"] is False
    assert by_id[index_only]["has_stocks"] is False

    filtered = load_event_radar(limit=10, has_stocks="1")
    assert filtered["filters"] == {"has_stocks": True}
    assert [item["event_id"] for item in filtered["events"]] == [root]
    assert filtered["events"][0]["stocks"][0]["name"] == "贵州茅台"

    response = radar_client.get(
        "/dashboard/events",
        query_string={"has_stocks": 1},
        base_url=BASE_URL,
    )
    body = response.get_data(as_text=True)
    assert response.status_code == 200
    assert 'class="stream-filter" href="/dashboard/events?has_stocks=1' in body
    assert 'aria-current="true">有相关股票</a>' in body
    assert '<span class="stock-flag">有相关股票</span>' in body
    assert "贵州茅台" in body
    assert "没有股票关系" not in body
    assert "只有指数代理" not in body
    assert "has_stocks=1#event-detail" in body


def test_public_event_radar_card_is_summary_only_bounded_and_escaped(
    database, radar_client
):
    from services.event_radar_query import load_event_stream

    event_id = _event(
        database,
        "public-event-card",
        title=XSS,
        occurred_at=920,
    )
    security_id, mapping_id = _security(
        database,
        event_id,
        "600036",
        name="招商银行",
        relation_type="direct",
        now=921,
    )
    _evidence(
        database,
        event_id,
        "https://feed.example/public-card",
        "招商银行披露经营信息",
        security_id=security_id,
        mapping_id=mapping_id,
        observed_at=923,
    )

    stream = load_event_stream(
        limit=1,
        has_stocks=True,
        require_non_concept_stock=True,
    )
    assert set(stream) == {"events", "pagination", "filters", "read_at"}
    assert stream["filters"] == {"has_stocks": True}
    assert isinstance(stream["read_at"], int)
    assert stream["events"][0]["event_id"] == event_id
    assert "detail" not in stream

    response = radar_client.get(
        "/event-radar-card", query_string={"limit": 1}, base_url=BASE_URL
    )
    body = response.get_data(as_text=True)

    assert response.status_code == 200
    assert response.headers["Widget-Title"] == "Event Radar"
    assert response.headers["Widget-Content-Type"] == "html"
    assert response.headers["Cache-Control"] == "public, max-age=10"
    assert "X-Event-Radar-State" not in response.headers
    assert XSS not in body
    assert "&lt;svg/onload=alert" in body
    assert "招商银行" in body
    assert "有原文映射" in body
    assert "·直接" in body
    assert "事件重要度</span>8.0/10" in body
    assert "条原文链接 · 转载关系未核验" in body
    assert "个入选发布方" not in body
    assert "个来源</span>" not in body
    assert "不是个股评分" in body
    assert "处理审计" not in body
    assert "契约结清" not in body
    assert f"event_id={event_id}" in body
    assert "https://ai.example.invalid/dashboard/events?has_stocks=1" in body
    assert "<script" not in body.lower()

    radar = radar_client.get(
        "/dashboard/events",
        query_string={"event_id": event_id, "has_stocks": 1},
        base_url=BASE_URL,
    )
    radar_body = radar.get_data(as_text=True)
    assert radar.status_code == 200
    assert "条原文链接 · 转载关系未核验" in radar_body
    assert "个原文来源" not in radar_body
    assert "个入选发布方" not in radar_body
    assert "<small>事件重要度</small>8.0<small>/10</small>" in radar_body
    assert "Stable event core" in radar_body
    assert "读取快照" in radar_body
    assert "处理审计" not in radar_body


def test_public_radar_copy_counts_distinct_article_uris_not_publishers(
    database, radar_client
):
    event_id = _event(
        database,
        "two-uri-same-publisher",
        title="同一发布方两篇原文",
        occurred_at=920,
    )
    security_id, mapping_id = _security(
        database,
        event_id,
        "600036",
        name="招商银行",
        relation_type="direct",
        now=921,
    )
    _evidence(
        database,
        event_id,
        "https://feed.example/mapped",
        "映射证据",
        security_id=security_id,
        mapping_id=mapping_id,
        observed_at=922,
    )
    _evidence(
        database,
        event_id,
        "https://feed.example/first",
        "第一篇",
        observed_at=923,
    )
    _evidence(
        database,
        event_id,
        "https://feed.example/second",
        "第二篇",
        observed_at=924,
    )
    body = radar_client.get(
        "/event-radar-card",
        query_string={"limit": 1},
        base_url=BASE_URL,
    ).get_data(as_text=True)
    assert "2 条原文链接 · 转载关系未核验" in body
    assert "2 个入选发布方" not in body


def test_public_stream_requires_evidence_bound_to_the_displayed_mapping(database):
    from services.event_radar_query import load_event_stream

    supported = _event(
        database,
        "public-evidence-supported",
        title="具备冻结原文证据的个股事件",
        occurred_at=920,
    )
    unsupported = _event(
        database,
        "public-evidence-unsupported",
        title="只有映射行但没有绑定证据的事件",
        occurred_at=930,
    )
    security_id, mapping_id = _security(
        database,
        supported,
        "600036",
        name="招商银行",
        now=931,
    )
    _security(
        database,
        unsupported,
        "600000",
        name="浦发银行",
        now=933,
    )
    _evidence(
        database,
        supported,
        "https://feed.example/evidence-backed",
        "招商银行披露经营信息",
        security_id=security_id,
        mapping_id=mapping_id,
        observed_at=935,
    )

    stream = load_event_stream(
        limit=10,
        has_stocks=True,
        require_non_concept_stock=True,
    )

    assert [item["event_id"] for item in stream["events"]] == [supported]
    assert [item["name"] for item in stream["events"][0]["stocks"]] == [
        "招商银行"
    ]


@pytest.mark.parametrize(
    "query",
    [
        "limit=0",
        "limit=31",
        "limit=1&limit=2",
        "unknown=1",
    ],
)
def test_public_event_radar_card_rejects_malformed_queries(
    database, radar_client, query
):
    response = radar_client.get(
        f"/event-radar-card?{query}", base_url=BASE_URL
    )
    assert response.status_code == 400


def test_public_event_radar_card_degrades_without_leaking_errors(
    database, radar_client, monkeypatch
):
    import routes.event_radar_routes as radar_routes

    monkeypatch.setattr(
        radar_routes,
        "load_event_stream",
        lambda **_kwargs: (_ for _ in ()).throw(
            radar_routes.EventRadarUnavailableError("private database detail")
        ),
    )
    response = radar_client.get("/event-radar-card", base_url=BASE_URL)
    body = response.get_data(as_text=True)

    assert response.status_code == 503
    assert response.headers["X-Event-Radar-State"] == "unavailable"
    assert response.headers["Cache-Control"] == "no-store"
    assert response.headers["Retry-After"] == "30"
    assert "事件摘要暂不可用" in body
    assert "完整事件核心与管理页面不受影响" not in body
    assert "private database detail" not in body


def test_explicit_index_reference_is_separate_from_equities_and_etf_copy(
    database, radar_client
):
    from services.event_radar_query import load_event_radar

    event_id = _event(
        database,
        "etf-reference",
        title="精选：可考虑上证50、沪深300等ETF",
        event_type="股评",
        direction="中性",
        occurred_at=900,
        now=950,
    )
    source = "https://evidence.example/etf-reference"
    _evidence(
        database,
        event_id,
        source,
        "可考虑上证50、沪深300等ETF",
        observed_at=960,
    )
    with database.get_connection() as conn:
        ids = database.upsert_event_market_references(
            conn,
            event_id=event_id,
            source_uri=source,
            direction="中性",
            mapping_version="asset-map:test",
            references=[
                {
                    "name": "上证50",
                    "exchange": "SSE",
                    "ticker": "000016",
                    "asset_type": "index",
                    "reference_type": "explicit_mention",
                },
                {
                    "name": "沪深300",
                    "exchange": "SSE",
                    "ticker": "000300",
                    "asset_type": "index",
                    "reference_type": "explicit_mention",
                },
                {
                    "name": "ETF（原文未指定基金代码）",
                    "asset_type": "fund_category",
                    "reference_type": "product_class",
                },
            ],
            now=950,
        )
        conn.commit()

    detail = load_event_radar(event_id=event_id)["detail"]
    assert len(ids) == 2
    assert detail["stocks"] == []
    assert [item["ticker"] for item in detail["market_references"]] == [
        "000016", "000300",
    ]
    assert detail["mentions_unspecified_etf"] is True

    body = radar_client.get(
        "/dashboard/events",
        query_string={"event_id": event_id},
        base_url=BASE_URL,
    ).get_data(as_text=True)
    assert "原文明确提及的市场标的" in body
    assert "ETF · 未指定基金代码" in body
    assert "不从成分股倒推相关股票" in body


def test_equity_ranking_uses_one_real_mapping_row(database):
    from db.event_core import merge_events
    from services.event_radar_query import load_event_radar

    root = _event(database, "ranking-root", occurred_at=850)
    child = _event(database, "ranking-child", occurred_at=840)
    # Candidate A must rank as 9.0/0.1, not the impossible composite 9.0/1.0.
    _security(
        database,
        root,
        "600010",
        name="候选A",
        relevance=9.0,
        confidence=0.1,
        now=851,
    )
    _security(
        database,
        child,
        "600010",
        name="候选A",
        relation_type="affected",
        relevance=1.0,
        confidence=1.0,
        now=852,
    )
    for offset, (ticker, name, confidence) in enumerate(
        [
            ("600011", "候选B", 0.9),
            ("600012", "候选C", 0.8),
            ("600013", "候选D", 0.7),
        ]
    ):
        _security(
            database,
            root,
            ticker,
            name=name,
            relevance=9.0,
            confidence=confidence,
            now=860 + offset,
        )
    with database.get_connection() as conn:
        merge_events(conn, child, root, reason="same ranked event", now=900)

    stocks = load_event_radar(event_id=root)["detail"]["stocks"]

    assert [stock["name"] for stock in stocks] == ["候选B", "候选C", "候选D"]


def test_merged_mapping_conflict_and_outcome_series_are_disclosed(
    database, radar_client
):
    from db.event_core import merge_events, upsert_event_outcome
    from services.event_radar_query import load_event_radar

    root = _event(database, "conflict-root", occurred_at=950)
    child = _event(database, "conflict-child", occurred_at=940)
    security_id, root_mapping = _security(
        database,
        root,
        "600020",
        name="冲突股票",
        relation_type="direct",
        direction="看多",
        relevance=9.0,
        confidence=0.9,
        now=951,
    )
    child_security, child_mapping = _security(
        database,
        child,
        "600020",
        name="冲突股票",
        relation_type="affected",
        direction="看空",
        relevance=8.0,
        confidence=0.8,
        now=952,
    )
    assert child_security == security_id
    with database.get_connection() as conn:
        upsert_event_outcome(
            conn,
            root,
            security_id,
            mapping_id=root_mapping,
            horizon="T+1",
            baseline_at=1000,
            measured_at=1100,
            baseline_price=100,
            gross_return=0.01,
            benchmark_return=0.0,
            excess_return=0.01,
            mfe=None,
            mae=None,
            direction_hit=True,
            method_version="close-v1",
            data_version="root-price",
            now=1200,
        )
        child_outcome = upsert_event_outcome(
            conn,
            child,
            security_id,
            mapping_id=child_mapping,
            horizon="T+1",
            baseline_at=1000,
            measured_at=1150,
            baseline_price=100,
            gross_return=-0.02,
            benchmark_return=0.0,
            excess_return=-0.02,
            mfe=None,
            mae=None,
            direction_hit=True,
            method_version="close-v1",
            data_version="child-price",
            now=1201,
        )
        merge_events(conn, child, root, reason="same conflict event", now=1202)

    overview = load_event_radar(event_id=root)["detail"]
    relation = overview["stocks"][0]["relations"][0]
    market = load_event_radar(event_id=root, tab="market")["detail"]["outcomes"]

    assert relation["candidate_count"] == 2
    assert relation["candidate_event_count"] == 2
    assert relation["direction_variant_count"] == 2
    assert relation["direction_conflict"] is True
    assert len(market) == 1
    assert market[0]["outcome_id"] == child_outcome
    assert market[0]["merged_series_count"] == 2
    assert market[0]["merged_series"] is True

    overview_body = radar_client.get(
        "/dashboard/events",
        query_string={"event_id": root},
        base_url=BASE_URL,
    ).get_data(as_text=True)
    market_body = radar_client.get(
        "/dashboard/events",
        query_string={"event_id": root, "tab": "reaction"},
        base_url=BASE_URL,
    ).get_data(as_text=True)
    assert "包含 2 种方向" in overview_body
    assert "合并事件组内有 2 条同映射层级、同证券、同期限、同方法的冻结结果" in market_body


def test_market_tab_returns_only_current_outcome_revision(database):
    from db.event_core import upsert_event_outcome
    from services.event_radar_query import load_event_radar

    event_id = _event(database, "market", occurred_at=900)
    security_id, mapping_id = _security(
        database,
        event_id,
        "399006",
        name="创业板指",
        asset_type="index",
        relation_type="benchmark_proxy",
        relevance=7,
        now=910,
    )
    with database.get_connection() as conn:
        outcome_id = upsert_event_outcome(
            conn,
            event_id,
            security_id,
            mapping_id=mapping_id,
            horizon="T+1",
            baseline_at=1000,
            measured_at=1100,
            baseline_price=100,
            gross_return=0.10,
            benchmark_return=0.02,
            excess_return=0.08,
            mfe=None,
            mae=None,
            direction_hit=True,
            method_version="close-v1",
            data_version="prices-v1",
            now=1200,
        )
        assert upsert_event_outcome(
            conn,
            event_id,
            security_id,
            mapping_id=mapping_id,
            horizon="T+1",
            baseline_at=1000,
            measured_at=1100,
            baseline_price=100,
            gross_return=0.12,
            benchmark_return=0.02,
            excess_return=0.10,
            mfe=None,
            mae=None,
            direction_hit=True,
            method_version="close-v1",
            data_version="prices-v2",
            now=1201,
        ) == outcome_id

    detail = load_event_radar(event_id=event_id, tab="market")["detail"]

    assert len(detail["outcomes"]) == 1
    outcome = detail["outcomes"][0]
    assert outcome["outcome_id"] == outcome_id
    assert outcome["revision"] == 2
    assert outcome["gross_return"] == pytest.approx(0.12)
    assert outcome["excess_return"] == pytest.approx(0.10)
    assert outcome["mfe"] is None and outcome["mae"] is None
    assert outcome["security"]["asset_type"] == "index"


def test_similar_tab_reads_both_pair_directions_and_excludes_lookahead(database):
    from db.event_core import merge_events, upsert_event_similarity
    from services.event_radar_query import load_event_radar

    root = _event(database, "similar-root", occurred_at=1500)
    earlier_one = _event(database, "similar-earlier-one", occurred_at=1300)
    earlier_two = _event(database, "similar-earlier-two", occurred_at=1400)
    future = _event(database, "similar-future", occurred_at=1600)
    internal = _event(database, "similar-internal", occurred_at=1290)
    with database.get_connection() as conn:
        upsert_event_similarity(
            conn,
            earlier_one,
            root,
            similarity=0.81,
            method="features",
            rationale="root appears on the right",
            common_securities=["600001"],
            shared_paths=["政策→银行"],
            now=1400,
        )
        upsert_event_similarity(
            conn,
            root,
            earlier_two,
            similarity=0.88,
            method="features",
            rationale="root appears on the left",
            common_securities=["000002"],
            shared_paths=["供给→价格"],
            now=1401,
        )
        upsert_event_similarity(
            conn,
            root,
            future,
            similarity=0.97,
            method="features",
            rationale="future look-ahead must not render",
            now=1402,
        )
        upsert_event_similarity(
            conn,
            root,
            internal,
            similarity=0.99,
            method="title_candidate",
            rationale="later becomes internal",
            now=1403,
        )
        merge_events(conn, internal, root, reason="confirmed duplicate", now=1404)

    detail = load_event_radar(event_id=root, tab="similar")["detail"]
    similar = detail["similar_events"]

    assert {item["event_id"] for item in similar} == {earlier_one, earlier_two}
    assert [item["similarity"] for item in similar] == [0.88, 0.81]
    assert internal not in {item["event_id"] for item in similar}
    assert future not in {item["event_id"] for item in similar}


def test_future_available_event_is_hidden_from_list_detail_and_public_stream(
    database, monkeypatch
):
    import services.event_radar_query as radar_query

    read_calls = []

    def frozen_read_at():
        read_calls.append(1)
        return 1_000

    monkeypatch.setattr(radar_query, "_freeze_read_at", frozen_read_at)
    visible = _event(
        database,
        "available-now",
        occurred_at=900,
        now=900,
        available_at=1_000,
    )
    future = _event(
        database,
        "available-later",
        occurred_at=950,
        now=900,
        available_at=1_001,
    )
    visible_security, visible_mapping = _security(
        database, visible, "600001", now=901
    )
    _security(database, future, "600002", now=902)
    _evidence(
        database,
        visible,
        "https://feed.example/available-now",
        "当前已可用事件的个股证据",
        security_id=visible_security,
        mapping_id=visible_mapping,
        observed_at=950,
    )

    radar = radar_query.load_event_radar(limit=10)
    assert [item["event_id"] for item in radar["events"]] == [visible]
    assert radar["selected_event_id"] == visible
    assert len(read_calls) == 1

    with pytest.raises(radar_query.EventRadarNotFoundError, match="not found"):
        radar_query.load_event_radar(event_id=future, limit=10)
    assert len(read_calls) == 2

    stream = radar_query.load_event_stream(
        limit=10,
        has_stocks=True,
        require_non_concept_stock=True,
    )
    assert [item["event_id"] for item in stream["events"]] == [visible]
    assert len(read_calls) == 3


def test_mapping_and_evidence_use_the_same_frozen_read_boundary(
    database, monkeypatch
):
    import services.event_radar_query as radar_query

    read_boundary = {"value": 1_000}
    monkeypatch.setattr(
        radar_query, "_freeze_read_at", lambda: read_boundary["value"]
    )
    evidence_later = _event(
        database,
        "mapping-visible-evidence-later",
        occurred_at=900,
        now=900,
        available_at=900,
    )
    evidence_security, evidence_mapping = _security(
        database,
        evidence_later,
        "600011",
        name="证据稍后可用股票",
        now=949,
    )
    _evidence(
        database,
        evidence_later,
        "https://feed.example/evidence-available-later",
        "映射证据在下一时点才可用",
        security_id=evidence_security,
        mapping_id=evidence_mapping,
        observed_at=1_001,
    )

    mapping_later = _event(
        database,
        "mapping-created-later",
        occurred_at=890,
        now=890,
        available_at=900,
    )
    mapping_security, mapping_id = _security(
        database,
        mapping_later,
        "600012",
        name="映射稍后创建股票",
        now=1_000,
    )
    _evidence(
        database,
        mapping_later,
        "https://feed.example/mapping-created-later",
        "映射及证据在下一时点才可用",
        security_id=mapping_security,
        mapping_id=mapping_id,
        observed_at=1_001,
    )

    public_before = radar_query.load_event_stream(
        limit=10,
        has_stocks=True,
        require_non_concept_stock=True,
    )
    assert public_before["events"] == []

    evidence_detail = radar_query.load_event_radar(
        event_id=evidence_later, limit=10
    )["detail"]
    assert [stock["name"] for stock in evidence_detail["stocks"]] == [
        "证据稍后可用股票"
    ]
    assert evidence_detail["stocks"][0]["relations"][0]["evidence"] == []
    assert radar_query.load_event_radar(
        event_id=mapping_later, limit=10
    )["detail"]["stocks"] == []

    read_boundary["value"] = 1_001
    public_after = radar_query.load_event_stream(
        limit=10,
        has_stocks=True,
        require_non_concept_stock=True,
    )
    assert {item["event_id"] for item in public_after["events"]} == {
        evidence_later,
        mapping_later,
    }
    assert all(item["stocks"] for item in public_after["events"])


def test_similar_tab_excludes_earlier_event_available_after_root_boundary(
    database, monkeypatch
):
    from db.event_core import upsert_event_similarity
    import services.event_radar_query as radar_query

    monkeypatch.setattr(radar_query, "_freeze_read_at", lambda: 5_000)
    root = _event(
        database,
        "similar-availability-root",
        occurred_at=1_500,
        now=1_000,
        available_at=1_200,
    )
    available_history = _event(
        database,
        "similar-availability-valid",
        occurred_at=1_300,
        now=900,
        available_at=1_100,
    )
    late_backfill = _event(
        database,
        "similar-availability-late",
        occurred_at=1_200,
        now=900,
        available_at=1_300,
    )
    with database.get_connection() as conn:
        upsert_event_similarity(
            conn,
            root,
            available_history,
            similarity=0.80,
            method="features",
            rationale="known by the root availability boundary",
            now=1_400,
        )
        upsert_event_similarity(
            conn,
            root,
            late_backfill,
            similarity=0.99,
            method="features",
            rationale="occurred earlier but arrived too late",
            now=1_401,
        )

    similar = radar_query.load_event_radar(
        event_id=root, tab="similar"
    )["detail"]["similar_events"]

    assert [item["event_id"] for item in similar] == [available_history]
    assert late_backfill not in {item["event_id"] for item in similar}


def test_similar_tab_skips_candidate_whose_new_canonical_root_is_not_visible(
    database, monkeypatch
):
    from db.event_core import merge_events, upsert_event_similarity
    import services.event_radar_query as radar_query

    monkeypatch.setattr(radar_query, "_freeze_read_at", lambda: 1_000)
    selected = _event(
        database,
        "similar-selected-before-canonical-merge",
        occurred_at=1_500,
        now=800,
        available_at=900,
    )
    visible_child = _event(
        database,
        "similar-visible-child",
        occurred_at=1_300,
        now=800,
        available_at=900,
    )
    future_canonical = _event(
        database,
        "similar-future-canonical",
        occurred_at=1_200,
        now=800,
        available_at=1_100,
    )
    with database.get_connection() as conn:
        upsert_event_similarity(
            conn,
            selected,
            visible_child,
            similarity=0.9,
            method="features",
            rationale="edge was visible before the later canonical merge",
            now=950,
        )
        merge_events(
            conn,
            visible_child,
            future_canonical,
            reason="later canonicalization",
            now=960,
        )

    detail = radar_query.load_event_radar(
        event_id=selected, tab="similar"
    )["detail"]
    assert detail["similar_events"] == []
    assert detail["similarities_truncated"] is False


def test_xss_and_poison_evidence_urls_remain_inert(database, radar_client):
    event_id = _event(
        database,
        "xss",
        title=f"恶意事件 {XSS}",
        event_type=XSS,
        direction=XSS,
        occurred_at=1500,
    )
    security_id, mapping_id = _security(
        database,
        event_id,
        "600100",
        name=f"恶意股票 {XSS}",
        relation_type="concept",
        relevance=9,
        rationale=f"恶意映射理由 {XSS}",
        now=1510,
    )
    _evidence(
        database,
        event_id,
        "https://safe.example/evidence",
        f"恶意证据 {XSS}",
        excerpt=f"恶意摘要 {XSS}",
        security_id=security_id,
        mapping_id=mapping_id,
        claim_key="safe-mapping-xss",
    )
    with database.get_connection() as conn:
        conn.execute(
            "INSERT INTO event_evidence "
            "(evidence_id, event_id, source_uri, source_type, source_name, claim_key, "
            "claim_text, excerpt, stance, reliability, observed_at, version, created_at, updated_at) "
            "VALUES (?, ?, ?, 'article', ?, ?, ?, ?, 'supports', 1, ?, 1, ?, ?)",
            (
                str(uuid.uuid4()),
                event_id,
                "javascript:alert(1)",
                XSS,
                "poison-js",
                XSS,
                XSS,
                1520,
                1520,
                1520,
            ),
        )
        conn.execute(
            "INSERT INTO event_evidence "
            "(evidence_id, event_id, source_uri, source_type, source_name, claim_key, "
            "claim_text, excerpt, stance, reliability, observed_at, version, created_at, updated_at) "
            "VALUES (?, ?, ?, 'article', 'poison', 'poison-userinfo', 'credential URL', '', "
            "'context', 0.5, ?, 1, ?, ?)",
            (
                str(uuid.uuid4()),
                event_id,
                "https://user:secret@evil.example/private",
                1521,
                1521,
                1521,
            ),
        )
        conn.commit()

    response = radar_client.get(
        "/dashboard/events",
        query_string={"event_id": event_id},
        base_url=BASE_URL,
    )
    body = response.get_data(as_text=True)

    assert response.status_code == 200
    assert XSS not in body
    assert "&lt;svg/onload=alert" in body
    assert "javascript:" not in body
    assert "user:secret@evil.example" not in body
    assert 'href="https://safe.example/evidence"' in body
    assert 'rel="noopener noreferrer"' in body
    assert "<script" not in body.lower()
    assert "script-src 'none'" in response.headers["Content-Security-Policy"]


@pytest.mark.parametrize(
    ("kwargs", "message"),
    [
        ({"limit": 0}, "limit"),
        ({"limit": 51}, "limit"),
        ({"limit": "1.5"}, "limit"),
        ({"cursor": "not-a-valid-cursor"}, "cursor"),
        ({"tab": "<script>"}, "tab"),
        ({"has_stocks": "yes"}, "has_stocks"),
        ({"event_id": "headline-hash-is-not-an-id"}, "event_id"),
    ],
)
def test_query_service_rejects_malformed_parameters(database, kwargs, message):
    from services.event_radar_query import EventRadarQueryError, load_event_radar

    with pytest.raises(EventRadarQueryError, match=message):
        load_event_radar(**kwargs)


def test_route_rejects_unknown_duplicate_and_non_get_queries(radar_client):
    unknown = radar_client.get(
        "/dashboard/events?unknown=1", base_url=BASE_URL
    )
    duplicate = radar_client.get(
        "/dashboard/events?limit=2&limit=3", base_url=BASE_URL
    )
    malformed = radar_client.get(
        "/dashboard/events?event_id=not-a-uuid", base_url=BASE_URL
    )
    missing = radar_client.get(
        f"/dashboard/events?event_id={uuid.uuid4()}", base_url=BASE_URL
    )
    post = radar_client.post("/dashboard/events", base_url=BASE_URL)

    assert unknown.status_code == duplicate.status_code == malformed.status_code == 400
    assert missing.status_code == 404
    assert post.status_code == 405


def test_keyset_cursor_is_deterministic_and_ignores_newer_inserts(database):
    from services.event_radar_query import load_event_radar

    original_ids = {
        _event(
            database,
            f"page-{number}",
            occurred_at=2000,
            priority=7,
            now=2100 + number,
        )
        for number in range(5)
    }
    expected = sorted(original_ids, reverse=True)

    first = load_event_radar(limit=2)
    first_ids = [item["event_id"] for item in first["events"]]
    assert first_ids == expected[:2]
    assert first["pagination"]["has_more"] is True
    assert first["pagination"]["next_cursor"]

    newer = _event(
        database,
        "inserted-after-first-page",
        occurred_at=3000,
        priority=10,
        now=3000,
    )
    second = load_event_radar(
        cursor=first["pagination"]["next_cursor"], limit=2
    )
    second_ids = [item["event_id"] for item in second["events"]]

    assert second_ids == expected[2:4]
    assert set(first_ids).isdisjoint(second_ids)
    assert newer not in second_ids


def test_route_preserves_non_default_limit_across_event_and_pager_links(
    database, radar_client
):
    from services.event_radar_query import load_event_radar

    for number in range(3):
        _event(
            database,
            f"route-page-{number}",
            occurred_at=4000,
            priority=8,
            now=4100 + number,
        )
    first = load_event_radar(limit=2)
    cursor = first["pagination"]["next_cursor"]
    assert cursor

    first_page = radar_client.get(
        "/dashboard/events", query_string={"limit": 2}, base_url=BASE_URL
    ).get_data(as_text=True)
    second_page = radar_client.get(
        "/dashboard/events",
        query_string={"limit": 2, "cursor": cursor},
        base_url=BASE_URL,
    ).get_data(as_text=True)

    assert "limit=2" in first_page
    assert "limit=2" in second_page
    assert "返回最新" in second_page


def test_route_discloses_source_truncation(radar_client, monkeypatch):
    import routes.event_radar_routes as radar_routes

    event_id = str(uuid.uuid4())
    event = {
        "event_id": event_id,
        "title": "截断披露测试",
        "event_type": "policy",
        "direction": "看多",
        "priority": 8,
        "occurred_at": 5000,
        "version": 1,
        "source_count": 31,
    }
    monkeypatch.setattr(
        radar_routes,
        "load_event_radar",
        lambda **_kwargs: {
            "events": [dict(event)],
            "pagination": {
                "limit": 30,
                "cursor": "",
                "next_cursor": None,
                "has_more": False,
            },
            "tab": "overview",
            "selected_event_id": event_id,
            "detail": {
                "event": dict(event),
                "requested_event_id": event_id,
                "group_size": 1,
                "summary": "测试摘要",
                "source_count": 31,
                "sources": [
                    {
                        "url": "https://example.test/source",
                        "source_name": "测试来源",
                        "claim_text": "证据",
                        "excerpt": "摘要",
                        "announcement_at": 4999,
                        "observed_at": 5000,
                    }
                ],
                "sources_truncated": True,
                "stocks": [],
                "outcomes": [],
                "similar_events": [],
            },
        },
    )

    body = radar_client.get("/dashboard/events", base_url=BASE_URL).get_data(
        as_text=True
    )

    assert "已展示前 1 / 31 个来源链接" in body


def _radar_stock_views(event_id):
    from services.event_radar_query import load_event_radar

    model = load_event_radar(event_id=event_id)
    root = model["detail"]["event"]["event_id"]
    page_event = next(event for event in model["events"] if event["event_id"] == root)
    return page_event["stocks"], model["detail"]["stocks"]


def test_radar_list_and_detail_prioritize_nonconcept_before_top_three(database):
    event_id = _event(database, "shared-selection-concepts")
    concepts = []
    for ticker, relevance in [("600301", 9.9), ("600302", 9.8), ("600303", 9.7)]:
        security_id, _ = _security(
            database, event_id, ticker, relation_type="concept",
            relevance=relevance, confidence=0.99,
        )
        concepts.append(security_id)
    direct_id, _ = _security(
        database, event_id, "600304", relation_type="direct",
        relevance=2.0, confidence=0.2,
    )

    chips, detail = _radar_stock_views(event_id)
    expected = [direct_id, *concepts[:2]]
    assert [stock["security_id"] for stock in chips] == expected
    assert [stock["security_id"] for stock in detail] == expected
    assert len(chips) == len(detail) == 3


def test_radar_best_fields_share_nonconcept_row_and_keep_all_relations(database):
    event_id = _event(database, "shared-selection-real-row")
    security_id, concept_mapping = _security(
        database, event_id, "600311", relation_type="concept",
        relevance=9.9, confidence=0.99, expected_gap=80.0, now=310,
    )
    same_id, direct_mapping = _security(
        database, event_id, "600311", relation_type="direct",
        relevance=3.0, confidence=0.4, expected_gap=-12.0, now=320,
    )
    assert same_id == security_id
    for mapping_id, suffix in [(concept_mapping, "concept"), (direct_mapping, "direct")]:
        _evidence(
            database, event_id, f"https://evidence.example/{suffix}", suffix,
            security_id=security_id, mapping_id=mapping_id,
        )

    chips, detail = _radar_stock_views(event_id)
    assert [stock["security_id"] for stock in chips] == [security_id]
    assert [stock["security_id"] for stock in detail] == [security_id]
    assert chips[0]["relation_type"] == "direct"
    assert (chips[0]["relevance"], chips[0]["mapping_confidence"]) == (3.0, 0.4)
    stock = detail[0]
    assert (
        stock["best_relevance"], stock["best_confidence"],
        stock["best_mapping_confidence"], stock["best_expected_gap"],
    ) == (3.0, 0.4, 0.4, -12.0)
    relations = {row["mapping_id"]: row for row in stock["relations"]}
    assert set(relations) == {concept_mapping, direct_mapping}
    assert relations[concept_mapping]["expected_gap"] == 80.0
    assert relations[direct_mapping]["expected_gap"] == -12.0
    assert [item["claim_text"] for item in relations[concept_mapping]["evidence"]] == ["concept"]
    assert [item["claim_text"] for item in relations[direct_mapping]["evidence"]] == ["direct"]


@pytest.mark.parametrize(
    "relation_type", ["direct", "direct_impact", "affected", "upstream", "downstream", "ecosystem"],
)
def test_radar_nonconcept_relations_have_equal_priority_bucket(database, relation_type):
    event_id = _event(database, f"shared-selection-bucket-{relation_type}")
    high_id, _ = _security(
        database, event_id, "600321", relation_type=relation_type,
        relevance=7.0, confidence=0.3,
    )
    low_direct_id, _ = _security(
        database, event_id, "600322", relation_type="direct",
        relevance=3.0, confidence=0.99,
    )
    concept_id, _ = _security(
        database, event_id, "600323", relation_type="concept",
        relevance=10.0, confidence=1.0,
    )

    chips, detail = _radar_stock_views(event_id)
    expected = [high_id, low_direct_id, concept_id]
    assert [stock["security_id"] for stock in chips] == expected
    assert [stock["security_id"] for stock in detail] == expected


def test_radar_equal_rank_uses_stable_security_id_tiebreaker(database):
    event_id = _event(database, "shared-selection-stable-ties")
    ids = [
        _security(
            database, event_id, ticker, relation_type=relation,
            relevance=7.0, confidence=0.7, now=300 + index,
        )[0]
        for index, (ticker, relation) in enumerate([
            ("600334", "ecosystem"), ("600332", "upstream"),
            ("600333", "direct"), ("600331", "downstream"),
        ])
    ]
    expected = sorted(ids)[:3]

    for _ in range(2):
        chips, detail = _radar_stock_views(event_id)
        assert [stock["security_id"] for stock in chips] == expected
        assert [stock["security_id"] for stock in detail] == expected


def test_radar_merged_selection_deduplicates_and_retains_direction_conflict(database):
    from db.event_core import merge_events

    root = _event(database, "shared-selection-root")
    child = _event(database, "shared-selection-child")
    security_id, concept_mapping = _security(
        database, root, "600341", relation_type="concept",
        relevance=10.0, confidence=1.0, expected_gap=90.0,
    )
    _, root_mapping = _security(
        database, root, "600341", relation_type="direct", direction="看多",
        relevance=4.0, confidence=0.4, expected_gap=10.0,
    )
    _, child_mapping = _security(
        database, child, "600341", relation_type="affected", direction="看空",
        relevance=5.0, confidence=0.5, expected_gap=-20.0,
    )
    second_id, _ = _security(
        database, child, "600342", relation_type="upstream", relevance=6.0,
    )
    with database.get_connection() as conn:
        merge_events(conn, child, root, reason="same selection event", now=500)

    for requested in [root, child]:
        chips, detail = _radar_stock_views(requested)
        expected = [second_id, security_id]
        assert [stock["security_id"] for stock in chips] == expected
        assert [stock["security_id"] for stock in detail] == expected
        stock = next(item for item in detail if item["security_id"] == security_id)
        assert stock["best_expected_gap"] == -20.0
        assert (stock["best_relevance"], stock["best_confidence"]) == (5.0, 0.5)
        assert stock["mapping_candidate_count"] == 3
        assert stock["has_direction_conflict"] is True
        relations = {item["mapping_id"]: item for item in stock["relations"]}
        assert set(relations) == {concept_mapping, child_mapping}
        assert root_mapping not in relations
        conflict = relations[child_mapping]
        assert conflict["candidate_count"] == conflict["candidate_event_count"] == 2
        assert conflict["direction_variant_count"] == 2
        assert conflict["direction_conflict"] is True


def test_concept_pool_chips_rank_after_evidence_relations_and_are_labeled(
    database, radar_client
):
    from services.event_radar_query import load_event_stream

    event_id = _event(
        database,
        "concept-pool-ranking",
        title="概念池映射不得盖过实证关系",
        occurred_at=940,
    )
    for ticker in ("002371", "688012", "603986"):
        _security(
            database,
            event_id,
            ticker,
            name=f"概念池股票{ticker}",
            relation_type="concept",
            relevance=8.0,
            confidence=0.9,
            now=941,
        )
    direct_security, direct_mapping = _security(
        database,
        event_id,
        "600584",
        name="实证直接股票",
        relation_type="direct",
        relevance=6.0,
        confidence=0.6,
        now=942,
    )
    _evidence(
        database,
        event_id,
        "https://feed.example/concept-ranking",
        "实证直接股票原文证据",
        security_id=direct_security,
        mapping_id=direct_mapping,
        observed_at=944,
    )

    stream = load_event_stream(limit=5, has_stocks=True)
    stocks = stream["events"][0]["stocks"]
    # 实证关系必须排在概念池之前，即使概念池映射的 relevance/confidence 更高。
    assert [item["relation_type"] for item in stocks] == [
        "direct",
        "concept",
        "concept",
    ]
    assert stocks[0]["name"] == "实证直接股票"

    response = radar_client.get(
        "/event-radar-card", query_string={"limit": 5}, base_url=BASE_URL
    )
    body = response.get_data(as_text=True)
    assert response.status_code == 200
    assert 'class="event-radar-summary-stock">实证直接股票' in body
    assert "概念池股票" not in body

    radar = radar_client.get(
        "/dashboard/events", query_string={"has_stocks": 1}, base_url=BASE_URL
    )
    radar_body = radar.get_data(as_text=True)
    assert radar.status_code == 200
    assert 'class="event-stock-chip">实证直接股票' in radar_body
    assert 'class="event-stock-chip is-concept"' in radar_body


def test_card_min_priority_hides_low_score_events_but_radar_keeps_them(
    database, radar_client
):
    from services.event_radar_query import (
        EventRadarQueryError,
        load_event_stream,
    )

    low = _event(
        database,
        "min-priority-low",
        title="低分事件不应出现在摘要卡片",
        priority=5.9,
        occurred_at=960,
    )
    high = _event(
        database,
        "min-priority-high",
        title="高分事件保留在摘要卡片",
        priority=6.0,
        occurred_at=950,
    )
    low_security, low_mapping = _security(
        database, low, "300750", name="低分股票", now=961
    )
    high_security, high_mapping = _security(
        database, high, "601318", name="高分股票", now=962
    )
    _evidence(
        database,
        low,
        "https://feed.example/min-priority-low",
        "低分股票原文证据",
        security_id=low_security,
        mapping_id=low_mapping,
        observed_at=964,
    )
    _evidence(
        database,
        high,
        "https://feed.example/min-priority-high",
        "高分股票原文证据",
        security_id=high_security,
        mapping_id=high_mapping,
        observed_at=965,
    )

    default_stream = load_event_stream(limit=10, has_stocks=True)
    default_ids = {item["event_id"] for item in default_stream["events"]}
    assert {low, high} <= default_ids

    filtered = load_event_stream(limit=10, has_stocks=True, min_priority=6.0)
    filtered_ids = {item["event_id"] for item in filtered["events"]}
    assert high in filtered_ids
    assert low not in filtered_ids

    for bad_value in ("nan", -1, 11, "abc"):
        with pytest.raises(EventRadarQueryError):
            load_event_stream(limit=10, min_priority=bad_value)

    card_body = radar_client.get(
        "/event-radar-card", base_url=BASE_URL
    ).get_data(as_text=True)
    assert "高分事件保留在摘要卡片" in card_body
    assert "低分事件不应出现在摘要卡片" not in card_body

    radar_body = radar_client.get(
        "/dashboard/events", query_string={"has_stocks": 1}, base_url=BASE_URL
    ).get_data(as_text=True)
    assert "低分事件不应出现在摘要卡片" in radar_body


def test_merged_event_priority_is_identical_in_card_list_and_detail(
    database, radar_client
):
    from db.event_core import merge_events
    from services.event_radar_query import load_event_radar, load_event_stream

    target = _event(
        database,
        "merged-priority-low-target",
        title="合并评分一致事件",
        priority=5.0,
        occurred_at=940,
    )
    source = _event(
        database,
        "merged-priority-high-source",
        title="合并评分一致事件来源",
        priority=6.5,
        occurred_at=950,
    )
    source_security, source_mapping = _security(
        database, source, "600519", name="合并评分股票", now=951
    )
    _evidence(
        database,
        source,
        "https://feed.example/merged-priority",
        "合并评分股票原文证据",
        security_id=source_security,
        mapping_id=source_mapping,
        observed_at=953,
    )
    with database.get_connection() as conn:
        merge_events(conn, source, target, now=960)

    stream = load_event_stream(
        limit=10,
        has_stocks=True,
        min_priority=6.0,
        require_non_concept_stock=True,
    )
    item = next(event for event in stream["events"] if event["event_id"] == target)
    detail = load_event_radar(event_id=source, limit=10)["detail"]["event"]
    assert item["priority"] == detail["priority"] == 6.5

    card_body = radar_client.get(
        "/event-radar-card", base_url=BASE_URL
    ).get_data(as_text=True)
    radar_body = radar_client.get(
        "/dashboard/events",
        query_string={"event_id": source, "has_stocks": 1},
        base_url=BASE_URL,
        follow_redirects=True,
    ).get_data(as_text=True)
    assert "事件重要度</span>6.5/10" in card_body
    assert "<small>事件重要度</small>6.5<small>/10</small>" in radar_body


def test_card_excludes_overseas_irrelevant_and_concept_only_events(
    database, radar_client
):
    overseas = _event(
        database,
        "homepage-overseas-irrelevant",
        title="海外无关事件不进入首页",
        event_type="海外无关消息",
        priority=9.0,
        occurred_at=990,
    )
    concept_only = _event(
        database,
        "homepage-concept-only",
        title="纯概念池事件不进入首页",
        event_type="产业新闻",
        priority=8.0,
        occurred_at=980,
    )
    evidence = _event(
        database,
        "homepage-evidence",
        title="实证个股事件进入首页",
        event_type="个股公告",
        priority=7.0,
        occurred_at=970,
    )
    overseas_security, overseas_mapping = _security(
        database, overseas, "300001", relation_type="direct", now=991
    )
    _security(database, concept_only, "300002", relation_type="concept", now=981)
    evidence_security, evidence_mapping = _security(
        database, evidence, "300003", relation_type="downstream", now=971
    )
    _evidence(
        database,
        overseas,
        "https://feed.example/overseas",
        "海外无关事件的个股证据",
        security_id=overseas_security,
        mapping_id=overseas_mapping,
        observed_at=993,
    )
    _evidence(
        database,
        evidence,
        "https://feed.example/homepage-evidence",
        "实证个股事件的原文证据",
        security_id=evidence_security,
        mapping_id=evidence_mapping,
        observed_at=973,
    )

    card_body = radar_client.get(
        "/event-radar-card", base_url=BASE_URL
    ).get_data(as_text=True)
    assert "实证个股事件进入首页" in card_body
    assert "·下游" in card_body
    assert "海外无关事件不进入首页" not in card_body
    assert "纯概念池事件不进入首页" not in card_body

    radar_body = radar_client.get(
        "/dashboard/events", query_string={"has_stocks": 1}, base_url=BASE_URL
    ).get_data(as_text=True)
    assert "海外无关事件不进入首页" in radar_body
    assert "纯概念池事件不进入首页" in radar_body


# ============================================================================


# ============================================================================
# Boundary and Error Handling Tests (Phase 2 - P0)
# ============================================================================



def test_load_event_radar_with_invalid_uuid():
    """Test load_event_radar raises error for invalid UUID."""
    from services.event_radar_query import load_event_radar, EventRadarQueryError
    import pytest
    
    with pytest.raises(EventRadarQueryError):
        load_event_radar(event_id="not-a-uuid")


def test_load_event_radar_with_zero_limit():
    """Test load_event_radar handles zero limit."""
    from services.event_radar_query import load_event_radar, EventRadarQueryError
    import pytest
    
    with pytest.raises(EventRadarQueryError):
        load_event_radar(limit=0)


def test_load_event_radar_with_negative_limit():
    """Test load_event_radar handles negative limit."""
    from services.event_radar_query import load_event_radar, EventRadarQueryError
    import pytest
    
    with pytest.raises(EventRadarQueryError):
        load_event_radar(limit=-1)


def test_load_event_radar_with_invalid_tab():
    """Test load_event_radar raises error for invalid tab."""
    from services.event_radar_query import load_event_radar, EventRadarQueryError
    import pytest
    
    with pytest.raises(EventRadarQueryError):
        load_event_radar(tab="invalid_tab_name")


def test_load_event_radar_with_malformed_cursor():
    """Test load_event_radar handles malformed cursor."""
    from services.event_radar_query import load_event_radar, EventRadarQueryError
    import pytest
    
    with pytest.raises(EventRadarQueryError):
        load_event_radar(cursor="malformed!!!base64")




def test_decode_cursor_with_invalid_base64():
    """Test _decode_cursor handles invalid base64."""
    from services.event_radar_query import _decode_cursor, EventRadarQueryError
    import pytest
    
    with pytest.raises(EventRadarQueryError):
        _decode_cursor("invalid!!!base64")


def test_decode_cursor_with_empty_string():
    """Test _decode_cursor handles empty string."""
    from services.event_radar_query import _decode_cursor
    
    result = _decode_cursor("")
    
    assert result is None


def test_decode_cursor_with_none():
    """Test _decode_cursor handles None."""
    from services.event_radar_query import _decode_cursor
    
    result = _decode_cursor(None)
    
    assert result is None


def test_parse_tab_with_valid_tabs():
    """Test _parse_tab accepts valid tab names."""
    from services.event_radar_query import _parse_tab
    
    # Test valid tab aliases
    assert _parse_tab("overview") == "overview"
    assert _parse_tab("detail") == "overview"  # alias
    assert _parse_tab("market") == "market"
    assert _parse_tab("outcomes") == "market"  # alias
    assert _parse_tab("similar") == "similar"


def test_parse_tab_with_invalid_tab_raises():
    """Test _parse_tab raises for invalid tab."""
    from services.event_radar_query import _parse_tab, EventRadarQueryError
    import pytest
    
    with pytest.raises(EventRadarQueryError):
        _parse_tab("invalid_tab")


def test_parse_has_stocks_with_valid_inputs():
    """Test _parse_has_stocks handles valid boolean inputs."""
    from services.event_radar_query import _parse_has_stocks
    
    # Valid truthy values
    assert _parse_has_stocks(True) is True
    assert _parse_has_stocks(1) is True
    assert _parse_has_stocks("1") is True
    
    # Valid falsy values
    assert _parse_has_stocks(False) is False
    assert _parse_has_stocks(0) is False
    assert _parse_has_stocks("0") is False


def test_parse_has_stocks_with_invalid_input_raises():
    """Test _parse_has_stocks raises for invalid input."""
    from services.event_radar_query import _parse_has_stocks, EventRadarQueryError
    import pytest
    
    with pytest.raises(EventRadarQueryError):
        _parse_has_stocks("true")
    
    with pytest.raises(EventRadarQueryError):
        _parse_has_stocks("invalid")


def test_freeze_read_at_returns_valid_timestamp():
    """Test _freeze_read_at returns a valid timestamp."""
    from services.event_radar_query import _freeze_read_at
    import time
    
    result = _freeze_read_at()
    
    assert isinstance(result, int)
    assert result > 0
    assert result <= int(time.time())


def test_event_group_cte_returns_sql():
    """Test _event_group_cte returns valid SQL string."""
    from services.event_radar_query import _event_group_cte
    
    result = _event_group_cte()
    
    assert isinstance(result, str)
    assert "event_group" in result
