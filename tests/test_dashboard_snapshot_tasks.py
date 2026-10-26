import sqlite3

import pytest


def test_dashboard_snapshot_task_requires_all_four_cards(monkeypatch):
    import routes.webhook_routes as webhook_routes
    import services.dashboard_snapshots as snapshots

    monkeypatch.setattr(
        snapshots,
        "build_all_dashboard_snapshots",
        lambda: {
            "ok": ["briefing", "realtime", "event_radar", "model_quality"],
            "failed": [],
        },
    )
    task = {"task_type": "dashboard_snapshot_refresh", "snapshot_version": 1}
    assert webhook_routes._execute_spooled_task(task) is True

    monkeypatch.setattr(
        snapshots,
        "build_all_dashboard_snapshots",
        lambda: {
            "ok": ["briefing", "realtime", "event_radar"],
            "failed": ["model_quality"],
        },
    )
    assert webhook_routes._execute_spooled_task(task) is False

    monkeypatch.setattr(
        snapshots,
        "build_all_dashboard_snapshots",
        lambda: {
            "ok": ["briefing", "realtime", "event_radar"],
            "failed": [],
            "deferred": ["model_quality"],
        },
    )
    assert (
        webhook_routes._execute_spooled_task(task)
        == webhook_routes._DASHBOARD_SNAPSHOT_INPUTS_CHANGED
    )


def test_build_all_classifies_superseded_cards_as_deferred(
    monkeypatch, tmp_path
):
    import services.dashboard_snapshots as snapshots

    monkeypatch.setenv("AI_FILTER_ROLE", "worker")

    def build_one(card, *_args, **_kwargs):
        if card == "model_quality":
            raise snapshots.DashboardSnapshotSupersededError("inputs changed")

    monkeypatch.setattr(snapshots, "_build_one_dashboard_snapshot", build_one)
    result = snapshots.build_all_dashboard_snapshots(
        object(), snapshot_dir=tmp_path / "snapshots", db_path=tmp_path / "cache.db"
    )

    assert result["ok"] == ["briefing", "realtime", "event_radar"]
    assert result["failed"] == []
    assert result["deferred"] == ["model_quality"]
    assert "DashboardSnapshotSupersededError" in result["errors"]["model_quality"]


@pytest.mark.parametrize("drift", ["data_version", "live_contract", "invalidation"])
def test_model_quality_live_drift_is_superseded(monkeypatch, tmp_path, drift):
    import services.dashboard_snapshots as snapshots

    directory = tmp_path / drift / "snapshots"
    database = tmp_path / drift / "cache.db"
    database.parent.mkdir(parents=True)
    with sqlite3.connect(database) as connection:
        connection.execute("CREATE TABLE observations (value TEXT NOT NULL)")

    if drift == "live_contract":
        contracts = iter(
            [("score:v1", "asset-map:v1"), ("score:v2", "asset-map:v1")]
        )
        monkeypatch.setattr(
            snapshots, "_current_model_quality_contract", lambda: next(contracts)
        )
    else:
        monkeypatch.setattr(
            snapshots,
            "_current_model_quality_contract",
            lambda: ("score:v1", "asset-map:v1"),
        )

    def render_with_drift(_app, card):
        assert card == "model_quality"
        if drift == "data_version":
            with sqlite3.connect(database) as writer:
                writer.execute("INSERT INTO observations (value) VALUES ('changed')")
        elif drift == "invalidation":
            with snapshots.model_quality_feedback_invalidation(
                snapshot_dir=directory, db_path=database
            ) as invalidation:
                invalidation.keep_invalidated()
        return snapshots._RenderedCard(
            html="<section>quality</section>",
            generated_at="2000-01-01T01:00:00Z",
            strategy_version="score:v1",
            mapping_version="asset-map:v1",
        )

    monkeypatch.setattr(snapshots, "_render_card", render_with_drift)
    with pytest.raises(snapshots.DashboardSnapshotSupersededError):
        snapshots._build_one_dashboard_snapshot(
            "model_quality", object(), snapshot_dir=directory, db_path=database
        )


def test_stable_computed_contract_mismatch_remains_hard_failure(
    monkeypatch, tmp_path
):
    import services.dashboard_snapshots as snapshots

    directory = tmp_path / "snapshots"
    database = tmp_path / "cache.db"
    with sqlite3.connect(database) as connection:
        connection.execute("CREATE TABLE observations (value TEXT NOT NULL)")
    monkeypatch.setattr(
        snapshots,
        "_current_model_quality_contract",
        lambda: ("score:v1", "asset-map:v1"),
    )
    monkeypatch.setattr(
        snapshots,
        "_render_card",
        lambda _app, _card: snapshots._RenderedCard(
            html="<section>wrong contract</section>",
            generated_at="2000-01-01T01:00:00Z",
            strategy_version="score:v2",
            mapping_version="asset-map:v1",
        ),
    )

    with pytest.raises(
        snapshots.DashboardSnapshotBuildError, match="does not match"
    ) as raised:
        snapshots._build_one_dashboard_snapshot(
            "model_quality", object(), snapshot_dir=directory, db_path=database
        )
    assert not isinstance(raised.value, snapshots.DashboardSnapshotSupersededError)


def test_successful_refresh_queues_snapshot_after_refresh(monkeypatch):
    import routes.webhook_routes as webhook_routes
    import services.refresh as refresh

    submitted = []
    monkeypatch.setattr(refresh, "run_refresh_command", lambda: True)
    monkeypatch.setattr(
        webhook_routes,
        "_submit_background_task",
        lambda task: submitted.append(task) or "stored",
    )

    assert webhook_routes._execute_spooled_task({"task_type": "refresh"}) is True
    assert len(submitted) == 1
    assert submitted[0]["task_type"] == "dashboard_snapshot_refresh"
    assert submitted[0]["snapshot_version"] == 1
    assert isinstance(submitted[0]["request_window"], int)


def test_dashboard_snapshot_tasks_coalesce_in_durable_spool(
    monkeypatch, tmp_path
):
    import routes.webhook_routes as webhook_routes

    monkeypatch.setattr(webhook_routes, "_webhook_spool_dir", tmp_path / "spool")
    monkeypatch.setattr(webhook_routes, "_webhook_spool_max_tasks", 8)
    if not hasattr(webhook_routes.os, "fchmod"):
        monkeypatch.setattr(
            webhook_routes.os, "fchmod", lambda *_args: None, raising=False
        )

    first = {
        "task_type": "dashboard_snapshot_refresh",
        "snapshot_version": 1,
        "request_window": 1,
    }
    second = {**first, "request_window": 2}
    assert webhook_routes._persist_webhook_task(first) == "stored"
    assert webhook_routes._persist_webhook_task(second) == "duplicate"


def test_snapshot_successor_is_bounded(monkeypatch, tmp_path):
    import routes.webhook_routes as webhook_routes

    monkeypatch.setattr(webhook_routes, "_webhook_spool_dir", tmp_path / "spool")
    monkeypatch.setattr(webhook_routes, "_webhook_spool_max_tasks", 8)
    if not hasattr(webhook_routes.os, "fchmod"):
        monkeypatch.setattr(
            webhook_routes.os, "fchmod", lambda *_args: None, raising=False
        )

    first = {
        "task_type": "dashboard_snapshot_refresh",
        "snapshot_version": 1,
        "invalidation_token": "first",
    }
    second = {**first, "invalidation_token": "second"}
    third = {**first, "invalidation_token": "third"}
    assert webhook_routes._persist_webhook_task(first) == "stored"
    claimed_path, claimed = webhook_routes._claim_next_spooled_task()
    assert claimed_path is not None
    assert claimed["task"]["invalidation_token"] == "first"

    assert webhook_routes._persist_webhook_task(second) == "stored"
    assert webhook_routes._persist_webhook_task(third) == "duplicate"
    directory = webhook_routes.Path(webhook_routes._webhook_spool_dir)
    assert len(webhook_routes._inflight_paths_unlocked(directory)) == 1
    assert len(webhook_routes._pending_paths_unlocked(directory)) == 1

    second_path, second_claim = webhook_routes._claim_next_spooled_task()
    assert second_path is not None
    assert second_claim["task"]["invalidation_token"] == "second"
    assert webhook_routes._persist_webhook_task(third) == "duplicate"
    assert len(webhook_routes._inflight_paths_unlocked(directory)) == 2
    assert len(webhook_routes._pending_paths_unlocked(directory)) == 0

    assert webhook_routes._defer_spooled_task_without_attempt(
        claimed_path,
        claimed["digest"],
        claimed["owner"],
        webhook_routes._DASHBOARD_SNAPSHOT_INPUTS_CHANGED,
    )
    assert len(webhook_routes._inflight_paths_unlocked(directory)) == 1
    assert len(webhook_routes._pending_paths_unlocked(directory)) == 1
    assert webhook_routes._persist_webhook_task(third) == "duplicate"
