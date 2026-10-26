import sqlite3
import time
import uuid

from services import backtest
from services import quality_bridge_preview as preview


RUN_SCHEMA = """
CREATE TABLE score_bridge_runs (
    run_id TEXT PRIMARY KEY,
    source_strategy_version TEXT NOT NULL,
    target_strategy_version TEXT NOT NULL,
    source_mapping_version TEXT NOT NULL,
    target_mapping_version TEXT NOT NULL,
    policy_version TEXT NOT NULL,
    mode TEXT NOT NULL,
    approval_status TEXT NOT NULL,
    approved_at INTEGER,
    approved_by TEXT,
    created_at INTEGER NOT NULL,
    source_row_count INTEGER NOT NULL,
    eligible_count INTEGER NOT NULL,
    ineligible_count INTEGER NOT NULL,
    unknown_count INTEGER NOT NULL,
    manifest_hash TEXT NOT NULL
);
CREATE TABLE backtest_signals (
    id INTEGER PRIMARY KEY,
    strategy_version TEXT NOT NULL,
    mapping_version TEXT NOT NULL,
    available_at INTEGER NOT NULL,
    selected INTEGER NOT NULL,
    is_similarity_copy INTEGER NOT NULL DEFAULT 0
);
CREATE TABLE score_bridge_members (
    run_id TEXT NOT NULL,
    ledger_signal_id INTEGER NOT NULL,
    source_signal_key TEXT NOT NULL,
    source_available_at INTEGER NOT NULL,
    selected INTEGER NOT NULL,
    eligibility TEXT NOT NULL,
    reason_code TEXT NOT NULL,
    stage_metadata_json TEXT NOT NULL,
    output_hash TEXT NOT NULL,
    evidence_hash TEXT NOT NULL,
    PRIMARY KEY (run_id, ledger_signal_id)
);
"""


def _insert_run(
    connection,
    *,
    run_id="approved-run",
    status="approved",
    source="score:old",
    target="score:current",
    source_mapping="asset-map:v1",
    target_mapping="asset-map:v1",
):
    connection.execute(
        "INSERT INTO score_bridge_runs VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (
            run_id,
            source,
            target,
            source_mapping,
            target_mapping,
            "score-bridge-policy:v1",
            "preview_only",
            status,
            100 if status == "approved" else None,
            "quality-owner" if status == "approved" else None,
            90,
            5,
            2,
            2,
            1,
            f"manifest-{run_id}",
        ),
    )


def _insert_members(connection, *, run_id="approved-run"):
    rows = [
        (1, 1, "eligible", "compatible_no_gpt"),
        (2, 0, "eligible", "compatible_no_gpt"),
        (3, 1, "ineligible", "explicit_gpt_participation"),
        (4, 0, "unknown", "provenance_strategy_mismatch"),
        (5, 0, "ineligible", "other_incompatibility"),
    ]
    for signal_id, selected, eligibility, reason in rows:
        available_at = 1_725_000_000 + signal_id
        connection.execute(
            "INSERT INTO backtest_signals VALUES (?, ?, ?, ?, ?, 0)",
            (signal_id, "score:old", "asset-map:v1", available_at, selected),
        )
        connection.execute(
            "INSERT INTO score_bridge_members VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                run_id,
                signal_id,
                f"signal-{signal_id}",
                available_at,
                selected,
                eligibility,
                reason,
                "{}",
                f"output-{signal_id}",
                f"evidence-{signal_id}",
            ),
        )


def test_missing_or_pending_bridge_keeps_preview_absent(isolate_ai_filter_db):
    assert preview.bridge_preview_data_version("score:current", "asset-map:v1") == "none"
    assert preview.load_bridge_preview("score:current", "asset-map:v1") is None

    with sqlite3.connect(isolate_ai_filter_db) as connection:
        connection.executescript(RUN_SCHEMA)
        _insert_run(connection, status="pending")

    # A run header without its complete immutable member manifest fails closed.
    assert preview.load_bridge_preview("score:current", "asset-map:v1") is None


def test_approved_bridge_is_validated_and_remains_preview_only(
    isolate_ai_filter_db,
):
    with sqlite3.connect(isolate_ai_filter_db) as connection:
        connection.executescript(RUN_SCHEMA)
        _insert_run(connection)
        _insert_members(connection)

    result = preview.load_bridge_preview("score:current", "asset-map:v1")

    assert result is not None
    assert result["source_total"] == 5
    assert result["candidate"] == 2
    assert result["selected_candidate"] == 1
    assert result["explicit_gpt"] == 1
    assert result["unknown"] == 1
    assert result["formal_eligible"] is False
    assert result["included_in_current_gate"] is False
    assert result["formal_gate_effect"] is False
    assert result["posterior_gate_effect"] is False
    assert result["control_gate_effect"] is False
    assert result["candidate_gate_effect"] is False
    assert result["auto_apply"] is False
    assert result["data_version"] != "none"
    assert (
        preview.bridge_preview_data_version("score:current", "asset-map:v1")
        == result["data_version"]
    )
    assert preview.load_bridge_preview("score:other", "asset-map:v1") is None


def test_complete_pending_manifest_is_visible_but_requires_approval(
    isolate_ai_filter_db,
):
    with sqlite3.connect(isolate_ai_filter_db) as connection:
        connection.executescript(RUN_SCHEMA)
        _insert_run(connection, status="pending")
        _insert_members(connection)

    result = preview.load_bridge_preview("score:current", "asset-map:v1")

    assert result is not None
    assert result["candidate"] == 2
    assert result["approval_required"] is True
    assert result["formal_gate_effect"] is False
    assert result["included_in_current_gate"] is False
    assert result["auto_apply"] is False


def _bridge_payload(data_version="bridge-v1"):
    return {
        "mode": "preview_only",
        "scope": "audited_compatibility_preview",
        "target_strategy_version": "score:current",
        "target_mapping_version": "asset-map:v1",
        "data_version": data_version,
        "manifest_count": 1,
        "approval_required": True,
        "source_total": 5,
        "candidate": 2,
        "selected_candidate": 1,
        "explicit_gpt": 1,
        "ineligible": 2,
        "unknown": 1,
        "candidate_signal_days": 1,
        "source_contracts": [{"strategy_version": "score:old"}],
        "manifests": [],
        "formal_eligible": False,
        "included_in_current_gate": False,
        "formal_gate_effect": False,
        "posterior_gate_effect": False,
        "control_gate_effect": False,
        "candidate_gate_effect": False,
        "auto_apply": False,
        "note": "preview only",
    }


def _run_one_score(monkeypatch, bridge_payload):
    timestamp = int(time.time()) - 60
    row = {
        "link": "https://example.com/one",
        "event_id": str(uuid.uuid4()),
        "score": 8.0,
        "tag": "AI算力",
        "direction": "中性",
        "created_at": timestamp,
        "available_at": timestamp,
        "strategy_version": "score:current",
        "mapping_version": "asset-map:v1",
        "mapped_theme": "AI算力",
        "backtest_symbol": None,
        "selected": True,
        "is_similarity_copy": False,
        "_signal_snapshot": True,
    }
    history = {
        "count": 1,
        "first_available_at": timestamp,
        "collection_started_at": timestamp,
        "data_source": "signal_snapshots",
        "cohort_history": {
            "current_scored_signals": 1,
            "current_selected_signals": 1,
            "current_unselected_signals": 0,
            "current_selected_signal_days": 1,
        },
    }
    monkeypatch.setattr(
        backtest,
        "load_config",
        lambda: {"scoring_strategy_version": "score:current"},
    )
    monkeypatch.setattr(backtest, "get_mapping_version", lambda: "asset-map:v1")
    monkeypatch.setattr(
        backtest,
        "_load_backtest_samples",
        lambda *_args, **_kwargs: ([row], history),
    )
    monkeypatch.setattr(backtest, "_same_day_snapshot", lambda *_args: None)
    monkeypatch.setattr(backtest, "_future_snapshot", lambda *_args: None)
    monkeypatch.setattr(
        backtest,
        "load_bridge_preview",
        lambda *_args: bridge_payload,
    )
    return backtest.run_score_backtest(days=90)


def test_backtest_json_attaches_bridge_without_changing_formal_gates(monkeypatch):
    without_bridge = _run_one_score(monkeypatch, None)
    assert "audited_compatibility" not in without_bridge

    with_bridge = _run_one_score(monkeypatch, _bridge_payload())
    comparison = with_bridge["audited_compatibility"]
    assert comparison["formal_gate_scope"] == "exact_current_only"
    assert comparison["formal_gate_effect"] is False
    assert comparison["auto_apply"] is False
    assert comparison["exact_current"]["scored"] == 1
    assert comparison["bridge_preview"]["candidate"] == 2
    assert with_bridge["optimization"] == without_bridge["optimization"]
    assert with_bridge["control_cohort"] == without_bridge["control_cohort"]
    assert with_bridge["buckets"] == without_bridge["buckets"]


def test_bridge_preview_renders_separately_and_escapes_dynamic_content():
    bridge = _bridge_payload()
    bridge["source_contracts"] = [
        {"strategy_version": 'score:<svg/onload=alert(1)>'}
    ]
    result = {
        "audited_compatibility": {
            "scope": "side_by_side_preview",
            "formal_gate_scope": "exact_current_only",
            "formal_gate_effect": False,
            "auto_apply": False,
            "exact_current": {
                "strategy_version": "score:current",
                "mapping_version": "asset-map:v1",
                "scored": 3,
                "selected": 2,
                "unselected": 1,
                "selected_signal_days": 2,
                "formal_eligible": True,
                "included_in_current_gate": True,
            },
            "bridge_preview": bridge,
        }
    }

    full = backtest._render_audited_compatibility_html(result, compact=False)
    compact = backtest._render_audited_compatibility_html(result, compact=True)

    assert "Exact current" in full
    assert "Bridge preview" in full
    assert "源总数 5 → 候选 2" in full
    assert "明确 GPT 参与 1" in compact
    assert "formal_gate_effect=false" in full
    assert "included_in_current_gate=false" in full
    assert "auto_apply=false" in full
    assert "<svg/onload=alert(1)>" not in full
    assert "&lt;svg/onload=alert(1)&gt;" in full


def test_route_cache_is_invalidated_when_bridge_manifest_changes(monkeypatch):
    import app as app_module
    import routes.dashboard_routes as dashboard_routes
    import routes.security as security

    bridge_version = ["none"]
    calls = []
    with dashboard_routes._BACKTEST_CACHE_LOCK:
        dashboard_routes._BACKTEST_CACHE.clear()
    with security._rate_limit_lock:
        security._rate_limit_store.clear()

    monkeypatch.setattr(
        dashboard_routes,
        "_backtest_cache_contract",
        lambda: ("score:current", "asset-map:v1"),
    )
    monkeypatch.setattr(
        dashboard_routes,
        "bridge_preview_data_version",
        lambda *_args: bridge_version[0],
    )

    def fake_backtest(days, min_score):
        calls.append((days, min_score, bridge_version[0]))
        result = {
            "revision": len(calls),
            "strategy_version": "score:current",
            "mapping_version": "asset-map:v1",
            "buckets": [],
        }
        if bridge_version[0] != "none":
            result["audited_compatibility"] = {
                "bridge_preview": _bridge_payload(bridge_version[0])
            }
        return result

    monkeypatch.setattr(dashboard_routes, "run_score_backtest", fake_backtest)
    monkeypatch.setattr(
        dashboard_routes,
        "run_mapping_validation",
        lambda **_kwargs: {},
    )

    with app_module.app.test_client() as client:
        first = client.get("/score-backtest?format=json&days=90")
        cached = client.get("/score-backtest?format=json&days=90")
        bridge_version[0] = "bridge-v1"
        refreshed = client.get("/score-backtest?format=json&days=90")
        cached_bridge = client.get("/score-backtest?format=json&days=90")

    responses = (first, cached, refreshed, cached_bridge)
    assert [response.status_code for response in responses] == [200, 200, 200, 200]
    assert [response.get_json()["revision"] for response in responses] == [1, 1, 2, 2]
    assert calls == [(90, None, "none"), (90, None, "bridge-v1")]
