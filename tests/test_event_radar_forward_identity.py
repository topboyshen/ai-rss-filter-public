"""Forward-only reader labels must not reinterpret persisted historical rows."""

import hashlib
import uuid

import pytest


@pytest.fixture
def radar_db():
    import db.database as database

    database.init_db()
    return database


def _seed(database, *, version, source_key, mapping_key=None):
    from core.source_contract import MAPPING_CLAIM_PREFIX, is_current_source_claim
    from db.event_core import resolve_or_create_event, upsert_event_evidence
    from db.event_core import upsert_event_security, upsert_security

    with database.get_connection() as conn:
        event_id = resolve_or_create_event(
            conn, {"fixture": str(uuid.uuid4())}, title="浦发银行获得项目订单",
            event_type="company", direction="看多", priority=8, occurred_at=100,
            strategy_version="historical-score-v1", model_version="test", now=100,
        )
        security_id = upsert_security(
            conn, exchange="SSE", ticker="600000", name="浦发银行", now=100,
        )
        mapping_id = upsert_event_security(
            conn, event_id, security_id, relation_type="direct", direction="看多",
            confidence=0.9, relevance=9, rationale="原文直接点名浦发银行。",
            mapping_version=version, now=100,
        )
        bound_key = mapping_key or (
            MAPPING_CLAIM_PREFIX + f"{mapping_id}:{uuid.uuid4()}"
            if is_current_source_claim(source_key) else source_key + "-mapping"
        )
        for bound, key in ((False, source_key), (True, bound_key)):
            upsert_event_evidence(
                conn, event_id, source_uri="https://example.com/article", source_name="测试发布方",
                claim_key=key, claim_text="浦发银行获得项目订单", excerpt="留存摘录",
                stance="supports", reliability=0.9, observed_at=100, now=100,
                mapping_id=mapping_id if bound else None,
                security_id=security_id if bound else None,
            )
    return event_id, security_id, mapping_id


def _current_key():
    from core.source_contract import ARTICLE_CLAIM_PREFIX

    return ARTICLE_CLAIM_PREFIX + hashlib.sha256(b"publisher title").hexdigest()


def _read(database, event_id):
    from services.event_radar_query import _load_page_stock_chips, _load_sources, _load_stocks

    with database.get_read_only_connection() as conn:
        before = [tuple(row) for row in conn.execute("SELECT * FROM event_evidence ORDER BY evidence_id")]
        chips = _load_page_stock_chips(conn, [event_id], read_at=200)
        stocks = _load_stocks(conn, event_id, 200)
        sources, _, _, _ = _load_sources(conn, event_id, 200)
        after = [tuple(row) for row in conn.execute("SELECT * FROM event_evidence ORDER BY evidence_id")]
    assert after == before
    return chips[event_id][0], stocks[0]["relations"][0], sources[0]


@pytest.mark.parametrize(
    "version_kind,key_kind,expected",
    [
        ("legacy", "legacy", "legacy_unknown"),
        ("legacy", "current", "legacy_unknown"),
        ("old_marked", "current", "historical"),
        ("current", "legacy", "source_unknown"),
        ("current", "malformed", "source_unknown"),
        ("current", "current", "current"),
        ("current", "mapping_current", "current"),
    ],
)
def test_mapping_labels_need_exact_contract_and_valid_bound_source(
    radar_db, version_kind, key_kind, expected,
):
    from core.asset_mapping import get_mapping_version
    from core.source_contract import ARTICLE_CLAIM_PREFIX, MAPPING_CLAIM_PREFIX

    version = {
        "legacy": "asset-map:old-contract",
        "old_marked": "asset-map:title-only-v2:older-rules",
        "current": get_mapping_version(),
    }[version_kind]
    key = {
        "legacy": "article_snapshot:legacy",
        "malformed": ARTICLE_CLAIM_PREFIX + "not-a-digest",
        "current": _current_key(),
        "mapping_current": MAPPING_CLAIM_PREFIX + f"{uuid.uuid4()}:{uuid.uuid4()}",
    }[key_kind]
    event_id, _, _ = _seed(radar_db, version=version, source_key=key)
    chip, relation, source = _read(radar_db, event_id)
    assert chip["mapping_contract_state"] == expected
    assert relation["mapping_contract_state"] == expected
    assert chip["mapping_contract_current"] is (expected == "current")
    assert relation["confidence"] == 0.9
    assert relation["rationale"] == "原文直接点名浦发银行。"
    source_expected = "current" if key_kind in {"current", "mapping_current"} else "legacy_unknown"
    assert source["source_contract_state"] == source_expected
    assert "不等于事实核验" in source["stance_record_label"]


def test_current_mapping_cannot_hide_legacy_evidence_beyond_display_limit(radar_db):
    from core.asset_mapping import get_mapping_version
    from db.event_core import upsert_event_evidence

    event_id, security_id, mapping_id = _seed(
        radar_db, version=get_mapping_version(), source_key=_current_key(),
    )
    with radar_db.get_connection() as conn:
        for index in range(8):
            upsert_event_evidence(
                conn, event_id, source_uri=f"https://example.com/old/{index}",
                claim_key=f"legacy:{index}", claim_text="旧来源", stance="supports",
                reliability=0.1, observed_at=100, now=100,
                mapping_id=mapping_id, security_id=security_id,
            )
    chip, relation, _ = _read(radar_db, event_id)
    assert chip["mapping_contract_state"] == "source_unknown"
    assert relation["mapping_contract_state"] == "source_unknown"
    assert len(relation["evidence"]) == 5


def test_future_legacy_source_does_not_change_frozen_read_boundary(radar_db):
    from core.asset_mapping import get_mapping_version
    from db.event_core import upsert_event_evidence

    event_id, security_id, mapping_id = _seed(
        radar_db, version=get_mapping_version(), source_key=_current_key(),
    )
    with radar_db.get_connection() as conn:
        upsert_event_evidence(
            conn, event_id, source_uri="https://example.com/future", claim_key="legacy:future",
            claim_text="未来观察", stance="supports", observed_at=300, now=300,
            mapping_id=mapping_id, security_id=security_id,
        )
    chip, relation, _ = _read(radar_db, event_id)
    assert chip["mapping_contract_state"] == relation["mapping_contract_state"] == "current"


def test_long_contract_is_exact_and_identity_failure_is_unavailable(radar_db, monkeypatch):
    import services.event_enrichment as enrichment
    from services.event_radar_query import _current_mapping_contracts, _mapping_contract_view

    version = "asset-map:store:writer-title-only-v2:rules-a:master-b"
    calls = []
    monkeypatch.setattr(enrichment, "_mapping_contract_version", lambda conn: (calls.append(conn) or version, 1))
    with radar_db.get_read_only_connection() as conn:
        versions, available = _current_mapping_contracts(conn, [version] * 30)
    assert len(calls) == 1
    assert _mapping_contract_view(version, True, versions, available)["mapping_contract_state"] == "current"

    def unavailable(_conn):
        raise RuntimeError("identity unavailable")

    monkeypatch.setattr(enrichment, "_mapping_contract_version", unavailable)
    with radar_db.get_read_only_connection() as conn:
        versions, available = _current_mapping_contracts(conn, [version])
    assert _mapping_contract_view(version, True, versions, available)["mapping_contract_state"] == "unavailable"


def test_legacy_badges_are_visible_in_card_list_detail_and_evidence(radar_db, monkeypatch):
    import app as app_module
    import routes.event_radar_routes as routes
    from flask import render_template
    from services.event_radar_query import load_event_radar

    event_id, _, _ = _seed(radar_db, version="asset-map:legacy", source_key="article_snapshot:legacy")
    model = load_event_radar(event_id=event_id)
    detail = model["detail"]
    monkeypatch.setattr(routes, "load_event_radar", lambda **_kwargs: model)
    with app_module.app.test_request_context("/dashboard/events"):
        card = render_template("event_radar_card.html", events=model["events"], has_more=False)
        page = routes._render_model(model, None)
    body = page.get_data(as_text=True) if hasattr(page, "get_data") else str(page)
    assert "历史映射 · 写入契约未知" in card
    assert "历史映射 · 写入契约未知" in body
    assert "存档标题（来源契约未核验）" in body
    assert "历史立场字段：支持（不等于事实核验）" in body
    assert "存档摘录（不作为原文证据）" in body
    assert "原文直接点名浦发银行。" in body
    assert detail["sources"][0]["original_claim"] == "浦发银行获得项目订单"


def test_market_page_discloses_frozen_contract_without_upgrading_old_results(radar_db):
    import app as app_module
    from routes.event_radar_routes import _render_model
    from services.event_radar_query import load_event_radar

    event_id, _, _ = _seed(radar_db, version="asset-map:legacy", source_key="legacy:source")
    model = load_event_radar(event_id=event_id)
    model["tab"] = "market"
    model["detail"]["outcomes"] = [{
        "horizon": "T+1", "strategy_version": "legacy-score-v1",
        "mapping_version": "asset-map:legacy", "mapping_current": True,
        "method_supported": False, "materialized": True,
        "security": {"name": "浦发银行", "ticker": "600000"},
    }]
    with app_module.app.test_request_context("/dashboard/events?tab=reaction"):
        body = _render_model(model, None).get_data(as_text=True)
    assert "冻结评分契约：legacy-score-v1" in body
    assert "冻结映射契约：asset-map:legacy" in body
    assert "不同评分／映射契约不混算" in body
    assert "旧版或无效契约" in body
