"""The public quality overview reads worker snapshots, never private records."""

import re
from copy import deepcopy

import pytest
from flask import Flask

from services import backtest, dashboard_snapshots
from tests.test_model_quality_sample_explanation import _result as _research_result


STRATEGY = "score:reading-overview-test"
MAPPING = "asset-map:reading-overview-test"
RESEARCH_SUMMARY = "市场研究与历史统计（仅事后参考）"
PRIVATE_MARKERS = (
    "private-model-sentinel",
    "private-backend-sentinel",
    "private-title-sentinel",
    "https://private.example.test/article",
    "private-logic-sentinel",
)


def overview_fixture_result(state="unknown"):
    """Deterministic test-only states; never use these as production evidence."""
    if state not in {"unknown", "unassessed", "empty", "zero", "failed", "populated"}:
        raise ValueError("unknown fixture state")
    result = _research_result()
    result.update(strategy_version=STRATEGY, mapping_version=MAPPING)
    result["cohort_history"].update(
        current_version=STRATEGY, current_mapping_version=MAPPING
    )
    if state in {"unknown", "unassessed"}:
        return result
    result["_processing_overview"] = {
        "available": state != "failed",
        "hours": 24,
        "strategy_version": STRATEGY,
        "observed_at": "2026-09-20T05:20:00+00:00",
        "transport_attempts": 0,
        "transport_succeeded": 0,
        "transport_failed": 0,
        "validation_attempts": 0,
        "final_paths": {},
    }
    if state == "populated":
        result["_processing_overview"].update(
            transport_attempts=30,
            transport_succeeded=26,
            transport_failed=4,
            validation_attempts=23,
            final_paths={
                "llm": 10,
                "stage2": 3,
                "llm_safe_neutral_grounding": 4,
                "llm_safe_neutral_initial": 1,
                "fallback_local": 1,
                "parse_error": 1,
            },
        )
    return result


def overview_fixture_mapping():
    return {
        "strategy_version": STRATEGY,
        "mapping_version": MAPPING,
        "data_complete": True,
        "enabled": True,
        "formal_pairs": 0,
        "status_label": "当前契约积累中",
        "horizons": [],
    }


def overview_fixture_html(state="unknown", compact=True):
    """Actual renderer output for local labelled UI fixtures/screenshots."""
    render = (
        backtest.render_backtest_compact_html
        if compact
        else backtest.render_backtest_html
    )
    result = overview_fixture_result(state)
    result["_include_full_research"] = True  # Match the worker's copied view data.
    return render(result, overview_fixture_mapping())


def _research_app(monkeypatch, result=None, mapping=None):
    from routes import dashboard_routes, security

    result = overview_fixture_result() if result is None else result
    mapping = overview_fixture_mapping() if mapping is None else mapping
    with dashboard_routes._BACKTEST_CACHE_LOCK:
        dashboard_routes._BACKTEST_CACHE.clear()
    with security._rate_limit_lock:
        security._rate_limit_store.clear()
    monkeypatch.setattr(
        dashboard_routes, "_backtest_cache_contract", lambda: (STRATEGY, MAPPING)
    )
    monkeypatch.setattr(
        dashboard_routes, "bridge_preview_data_version", lambda *_args: "none"
    )
    monkeypatch.setattr(
        dashboard_routes, "run_score_backtest", lambda **_kwargs: result
    )
    monkeypatch.setattr(
        dashboard_routes, "run_mapping_validation", lambda **_kwargs: mapping
    )
    app = Flask(__name__)
    app.config["TESTING"] = True
    app.register_blueprint(dashboard_routes.dash_bp)
    return app, dashboard_routes, result


def _forbid_private_reads(monkeypatch):
    from db import neutral_records
    from routes import neutral_records_routes
    from services import model_quality_overview

    def forbidden(*_args, **_kwargs):
        pytest.fail(
            "public quality HTML must not query private records or processing counts"
        )

    monkeypatch.setattr(neutral_records, "read_neutral_records", forbidden)
    monkeypatch.setattr(neutral_records_routes, "read_neutral_records", forbidden)
    monkeypatch.setattr(
        model_quality_overview, "collect_processing_overview", forbidden
    )
    return forbidden


def _sections(html):
    match = re.search(r'<details\b([^>]*\bclass="quality-market-research"[^>]*)>', html)
    assert match, "market research must remain in a distinct details disclosure"
    assert not re.search(r"\bopen(?:\s|=|$)", match.group(1))
    assert RESEARCH_SUMMARY in html[match.end() :]
    return html[: match.start()], html[match.end() :]


@pytest.mark.parametrize(
    "path", ["/score-backtest", "/score-backtest?days=90&view=compact"]
)
def test_actual_live_route_renders_overview_and_closed_research_without_new_reads(
    monkeypatch, path
):
    monkeypatch.setenv("AI_FILTER_DASHBOARD_SNAPSHOTS", "0")
    app, _routes, result = _research_app(monkeypatch)
    before = deepcopy(result)
    _forbid_private_reads(monkeypatch)

    response = app.test_client().get(path)
    first_screen, research = _sections(response.get_data(as_text=True))

    assert response.status_code == 200
    assert "quality-overview" in first_screen
    assert "未知" in first_screen
    assert "当前契约全分数 19" in research
    assert "全库历史修订 42713" in research
    assert "不等同独立样本" in research
    assert "个股映射后验" in research
    assert "调参研究" not in first_screen
    assert "42713" not in first_screen
    assert result == before


def _snapshot_app(monkeypatch, tmp_path, state="unknown"):
    app, routes, _result = _research_app(monkeypatch)
    directory = tmp_path / "dashboard_snapshots"
    monkeypatch.setenv("AI_FILTER_DASHBOARD_SNAPSHOT_DIR", str(directory))
    monkeypatch.setenv("AI_FILTER_DASHBOARD_SNAPSHOTS", "1")
    monkeypatch.setenv("AI_FILTER_VERSION", "my-ai-filter:test-quality-overview")
    monkeypatch.setenv("AI_FILTER_ROLE", "worker")
    html = overview_fixture_html(state)
    dashboard_snapshots.write_dashboard_snapshot(
        "model_quality",
        html,
        generated_at=_fresh_snapshot_generated_at(),
        strategy_version=STRATEGY,
        mapping_version=MAPPING,
        data_version="1",
        snapshot_dir=directory,
        db_path=tmp_path / "cache.db",
    )
    monkeypatch.setenv("AI_FILTER_ROLE", "web")
    return app, routes, html


@pytest.mark.parametrize(
    "path",
    [
        "/score-backtest",
        "/score-backtest?days=90",
        "/score-backtest?days=90&view=compact",
    ],
)
def test_canonical_public_views_read_same_snapshot_without_live_queries(
    monkeypatch, tmp_path, path
):
    from db import database

    app, routes, expected = _snapshot_app(monkeypatch, tmp_path)
    forbidden = _forbid_private_reads(monkeypatch)
    for name in (
        "_backtest_cache_contract",
        "run_score_backtest",
        "run_mapping_validation",
    ):
        monkeypatch.setattr(routes, name, forbidden)
    monkeypatch.setattr(database, "get_connection", forbidden)
    monkeypatch.setattr(database, "get_read_only_connection", forbidden)

    response = app.test_client().get(path)

    assert response.status_code == 200
    assert response.headers["X-Card-Snapshot-State"] == "ready"
    import routes.dashboard_routes as dashboard_routes

    expected_body = expected
    if "view=compact" not in path:
        # The full page drops the self-link; the compact card keeps it.
        expected_body = dashboard_routes._without_quality_card_entry(expected)
    assert response.get_data(as_text=True) == expected_body
    _sections(expected)


@pytest.mark.parametrize(
    "path",
    [
        "/score-backtest?format=json",
        "/score-backtest?days=30",
        "/score-backtest?days=90&min_score=7",
        "/score-backtest?days=30&view=compact",
    ],
)
def test_custom_or_json_view_is_not_replaced_by_default_snapshot(monkeypatch, path):
    monkeypatch.setenv("AI_FILTER_DASHBOARD_SNAPSHOTS", "1")
    monkeypatch.setenv("AI_FILTER_ROLE", "web")
    app, _routes, _result = _research_app(monkeypatch)
    forbidden = _forbid_private_reads(monkeypatch)
    monkeypatch.setattr(dashboard_snapshots, "read_dashboard_snapshot", forbidden)

    response = app.test_client().get(path)

    assert response.status_code == 200
    assert "X-Card-Snapshot-State" not in response.headers
    if "format=json" in path:
        assert response.is_json
        assert "_processing_overview" not in response.get_json()
    else:
        first_screen, _research = _sections(response.get_data(as_text=True))
        assert "未知" in first_screen


def test_worker_collects_only_after_contract_validation_without_mutating_result(
    monkeypatch,
):
    from services import model_quality_overview

    monkeypatch.setenv("AI_FILTER_ROLE", "worker")
    monkeypatch.setenv("AI_FILTER_DASHBOARD_SNAPSHOTS", "1")
    app, routes, result = _research_app(monkeypatch)
    result["buckets"] = [{
        "bucket": "8.0-8.9", "samples": 4, "avg_score": 8.2,
        "mapping_coverage": 25.0, "asset_t1_direction_samples": 4,
        "asset_t1_direction_win_rate": 100.0,
        "asset_t1_direction_sample_sufficient": False,
    }]
    calls = []
    before = deepcopy(result)
    monkeypatch.setattr(
        routes,
        "_backtest_cache_contract",
        lambda: ("score:before-compute", "map:before-compute"),
    )

    def collect(version):
        calls.append(version)
        return {"available": False, "strategy_version": version, "hours": 24}

    monkeypatch.setattr(model_quality_overview, "collect_processing_overview", collect)
    rendered = dashboard_snapshots._render_model_quality_card(app)

    assert calls == [STRATEGY]
    assert rendered.strategy_version == STRATEGY
    assert rendered.mapping_version == MAPPING
    assert result == before
    assert "_processing_overview" not in result
    assert "_include_full_research" not in result
    assert routes._BACKTEST_CACHE
    assert all(
        not {"_processing_overview", "_include_full_research"}.intersection(cached[1])
        for cached in routes._BACKTEST_CACHE.values()
    )
    first_screen, _research = _sections(rendered.html)
    assert "处理状态暂不可用" in first_screen
    assert '<details class="quality-full-statistics">' in _research
    assert "8.0-8.9" in _research and "映射资产 T+1方向" in _research
    assert "100.0%" not in rendered.html  # Existing maturity rules still apply.


@pytest.mark.parametrize("marker", [None, False, "true", 1])
def test_compact_sparse_buckets_require_explicit_full_details_opt_in(marker):
    result = overview_fixture_result()
    result["buckets"] = [{"bucket": "8.0-8.9", "samples": 4}]
    result["_include_full_research"] = marker
    before = deepcopy(result)
    html = backtest.render_backtest_compact_html(result)
    assert "quality-full-statistics" not in html
    assert "8.0-8.9" not in html
    assert result == before


@pytest.mark.parametrize(
    "invalid", ["missing_score_contract", "mapping_mismatch", "mapping_incomplete"]
)
def test_worker_rejects_invalid_contract_before_processing_collection(
    monkeypatch, invalid
):
    from services import model_quality_overview

    result = overview_fixture_result()
    mapping = overview_fixture_mapping()
    if invalid == "missing_score_contract":
        result.pop("strategy_version")
    elif invalid == "mapping_mismatch":
        mapping["strategy_version"] = "score:other"
    else:
        mapping["data_complete"] = False
    monkeypatch.setenv("AI_FILTER_ROLE", "worker")
    monkeypatch.setenv("AI_FILTER_DASHBOARD_SNAPSHOTS", "0")
    app, _routes, _result = _research_app(monkeypatch, result, mapping)

    def forbidden(*_args, **_kwargs):
        pytest.fail("processing collection must follow the computed-contract checks")

    monkeypatch.setattr(
        model_quality_overview, "collect_processing_overview", forbidden
    )
    with pytest.raises(dashboard_snapshots.DashboardSnapshotBuildError):
        dashboard_snapshots._render_model_quality_card(app)


@pytest.mark.parametrize(
    "state,expected",
    [
        ("unassessed", "处理状态未知"),
        ("zero", "调用尝试 0 次"),
        ("failed", "处理状态暂不可用"),
        ("populated", "调用尝试 30 次"),
    ],
)
def test_testonly_states_survive_real_snapshot_publication_and_read(
    monkeypatch, tmp_path, state, expected
):
    app, _routes, html = _snapshot_app(monkeypatch, tmp_path, state)
    _forbid_private_reads(monkeypatch)

    response = app.test_client().get("/score-backtest")
    first_screen, _research = _sections(response.get_data(as_text=True))

    assert response.status_code == 200
    # The stored artifact keeps the card entry for the lightweight card; the
    # full page drops the link back to itself and relies on the inline entry.
    assert '<span class="quality-card-entry">' in html
    import routes.dashboard_routes as dashboard_routes

    assert response.get_data(as_text=True) == (
        dashboard_routes._without_quality_card_entry(html)
    )
    assert expected in first_screen
    assert "尚未评估（本页未接入人工核对结果）" in first_screen
    assert "模型准确率" not in first_screen
    assert "保守中性不是模型错误率" in first_screen
    if state in {"unassessed", "failed"}:
        assert "调用尝试 0 次" not in first_screen
    if state == "populated":
        assert "保守中性 5" in first_screen
        assert "本地兜底 1" in first_screen


def test_worker_publishes_aggregate_allowlist_without_backend_or_article_details(
    monkeypatch, tmp_path
):
    from db import neutral_records
    from services import model_quality_overview

    monkeypatch.setenv("AI_FILTER_ROLE", "worker")
    monkeypatch.setenv("AI_FILTER_DASHBOARD_SNAPSHOTS", "1")
    monkeypatch.setenv("AI_FILTER_VERSION", "my-ai-filter:test-quality-overview")
    app, _routes, result = _research_app(monkeypatch)
    paths = dict(
        overview_fixture_result("populated")["_processing_overview"]["final_paths"]
    )
    paths["private-path-sentinel"] = 987
    calls = []

    def private_reader(*_args, **_kwargs):
        pytest.fail(
            "worker overview must not query private article/neutral record rows"
        )

    def telemetry(version, hours):
        calls.append((version, hours))
        return {
            "available": True,
            "strategy_version": version,
            "hours": hours,
            "transport": [
                {
                    "backend": PRIVATE_MARKERS[1],
                    "model": PRIVATE_MARKERS[0],
                    "attempts": 30,
                    "succeeded": 26,
                }
            ],
            "validation": [{"model": PRIVATE_MARKERS[0], "observed": 23}],
            "final_paths": paths,
            "records": [
                {
                    "title": PRIVATE_MARKERS[2],
                    "link": PRIVATE_MARKERS[3],
                    "logic": PRIVATE_MARKERS[4],
                }
            ],
        }

    monkeypatch.setattr(neutral_records, "read_neutral_records", private_reader)
    monkeypatch.setattr(model_quality_overview, "score_quality_snapshot", telemetry)
    rendered = dashboard_snapshots._render_model_quality_card(app)
    assert calls == [(STRATEGY, 24)]
    assert "_processing_overview" not in result
    for private in (*PRIVATE_MARKERS, "private-path-sentinel"):
        assert private not in rendered.html
    assert "调用尝试 30 次" in rendered.html
    assert "基础评分记录 20 条" in rendered.html
    assert rendered.html.count('class="model-quality-overview"') == 1
    directory = tmp_path / "privacy-snapshot"
    envelope = dashboard_snapshots.write_dashboard_snapshot(
        "model_quality",
        rendered.html,
        generated_at=rendered.generated_at,
        strategy_version=rendered.strategy_version,
        mapping_version=rendered.mapping_version,
        snapshot_dir=directory,
        db_path=tmp_path / "cache.db",
        data_version="1",
    )
    assert set(envelope) == {
        "schema",
        "card",
        "generated_at",
        "published_at",
        "release_version",
        "content_type",
        "html_sha256",
        "html",
        "strategy_version",
        "mapping_version",
        "data_version",
    }
    encoded = (directory / "model_quality.json").read_text(encoding="utf-8")
    for private in (*PRIVATE_MARKERS, "private-path-sentinel"):
        assert private not in encoded
def _fresh_snapshot_generated_at() -> str:
    from datetime import datetime, timedelta, timezone

    return (datetime.now(timezone.utc) - timedelta(seconds=30)).strftime(
        "%Y-%m-%dT%H:%M:%S+00:00"
    )
