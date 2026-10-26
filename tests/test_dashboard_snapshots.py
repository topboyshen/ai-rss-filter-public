import json
import os
import sqlite3
import stat
import threading
import time
from pathlib import Path

import pytest
from flask import Flask

from services import dashboard_snapshots as snapshots


def test_runtime_snapshot_directory_is_gitignored():
    project_root = Path(__file__).resolve().parents[1]
    ignored = {
        line.strip()
        for line in (project_root / ".gitignore").read_text(encoding="utf-8").splitlines()
    }
    assert "data/dashboard_snapshots/" in ignored


@pytest.fixture
def snapshot_env(tmp_path, monkeypatch):
    monkeypatch.setenv("AI_FILTER_ROLE", "worker")
    directory = tmp_path / "dashboard_snapshots"
    database = tmp_path / "cache.db"
    return directory, database


def _write_raw_snapshot(path: Path, payload: bytes) -> None:
    path.parent.mkdir(mode=0o700, exist_ok=True)
    path.write_bytes(payload)
    if os.name != "nt":
        path.parent.chmod(0o700)
        path.chmod(0o600)


def test_atomic_publish_replaces_complete_snapshot(snapshot_env, monkeypatch):
    directory, database = snapshot_env
    original = snapshots.write_dashboard_snapshot(
        "briefing",
        "<div>old</div>",
        generated_at="2000-01-01T01:00:00Z",
        snapshot_dir=directory,
        db_path=database,
    )
    calls = []
    real_replace = snapshots.os.replace

    def recording_replace(source, target):
        calls.append((Path(source), Path(target)))
        return real_replace(source, target)

    monkeypatch.setattr(snapshots.os, "replace", recording_replace)
    published = snapshots.write_dashboard_snapshot(
        "briefing",
        "<div>new</div>",
        generated_at="2000-01-01T01:01:00Z",
        snapshot_dir=directory,
        db_path=database,
    )

    assert original["html"] == "<div>old</div>"
    assert published["html"] == "<div>new</div>"
    assert len(calls) == 1
    assert calls[0][0].parent == calls[0][1].parent == directory
    assert snapshots.read_dashboard_snapshot(
        "briefing", snapshot_dir=directory, db_path=database
    )["html"] == "<div>new</div>"
    assert not list(directory.glob(".*.tmp"))
    if os.name != "nt":
        assert stat.S_IMODE(directory.stat().st_mode) == 0o700
        assert stat.S_IMODE((directory / "briefing.json").stat().st_mode) == 0o600


def test_snapshot_is_bound_to_immutable_release(snapshot_env, monkeypatch):
    directory, database = snapshot_env
    monkeypatch.setenv("AI_FILTER_VERSION", "my-ai-filter:2026.08.17.3")
    snapshots.write_dashboard_snapshot(
        "briefing",
        "<div>old release</div>",
        generated_at="2000-01-01T01:00:00Z",
        snapshot_dir=directory,
        db_path=database,
    )
    monkeypatch.setenv("AI_FILTER_VERSION", "my-ai-filter:2026.08.17.4")
    assert (
        snapshots.read_dashboard_snapshot(
            "briefing", snapshot_dir=directory, db_path=database
        )
        is None
    )

    snapshots.write_dashboard_snapshot(
        "briefing",
        "<div>new release</div>",
        generated_at="2000-01-01T01:01:00Z",
        snapshot_dir=directory,
        db_path=database,
    )
    assert snapshots.read_dashboard_snapshot(
        "briefing", snapshot_dir=directory, db_path=database
    )["html"] == "<div>new release</div>"


def test_reader_rejects_bad_json(snapshot_env):
    directory, database = snapshot_env
    _write_raw_snapshot(directory / "briefing.json", b'{"schema":')

    assert (
        snapshots.read_dashboard_snapshot(
            "briefing", snapshot_dir=directory, db_path=database
        )
        is None
    )


def test_reader_rejects_contract_mismatch(snapshot_env):
    directory, database = snapshot_env
    snapshots.write_dashboard_snapshot(
        "model_quality",
        "<section>quality</section>",
        generated_at="2000-01-01T01:00:00Z",
        strategy_version="score:v1",
        mapping_version="asset-map:v1",
        data_version="7",
        snapshot_dir=directory,
        db_path=database,
    )

    assert snapshots.read_dashboard_snapshot(
        "model_quality",
        expected_contract=("score:v1", "asset-map:v1"),
        snapshot_dir=directory,
        db_path=database,
    ) is not None
    assert (
        snapshots.read_dashboard_snapshot(
            "model_quality",
            expected_contract=("score:v2", "asset-map:v1"),
            snapshot_dir=directory,
            db_path=database,
        )
        is None
    )


def test_feedback_invalidation_preserves_bytes_and_failed_write_restores_ready(
    snapshot_env,
):
    directory, database = snapshot_env
    snapshots.write_dashboard_snapshot(
        "model_quality",
        "<section>quality v1</section>",
        generated_at="2000-01-01T01:00:00Z",
        strategy_version="score:v1",
        mapping_version="asset-map:v1",
        data_version="1",
        snapshot_dir=directory,
        db_path=database,
    )


def test_model_quality_reader_fails_closed_without_waiting_for_feedback_lock(
    snapshot_env,
):
    directory, database = snapshot_env
    snapshots.write_dashboard_snapshot(
        "model_quality",
        "<section>quality v1</section>",
        generated_at="2000-01-01T01:00:00Z",
        strategy_version="score:v1",
        mapping_version="asset-map:v1",
        data_version="1",
        snapshot_dir=directory,
        db_path=database,
    )
    held = threading.Event()
    release = threading.Event()

    def hold_feedback_lock():
        with snapshots._model_quality_coordination_lock(
            directory, exclusive=True
        ):
            held.set()
            release.wait(timeout=5)

    thread = threading.Thread(target=hold_feedback_lock, daemon=True)
    thread.start()
    assert held.wait(timeout=2)
    started = time.monotonic()
    try:
        assert (
            snapshots.read_dashboard_snapshot(
                "model_quality", snapshot_dir=directory, db_path=database
            )
            is None
        )
        assert time.monotonic() - started < 0.25
    finally:
        release.set()
        thread.join(timeout=2)
    assert not thread.is_alive()
    path = directory / "model_quality.json"
    original = path.read_bytes()

    with pytest.raises(RuntimeError, match="feedback failed"):
        with snapshots.model_quality_feedback_invalidation(
            snapshot_dir=directory, db_path=database
        ):
            assert path.read_bytes() == original
            raise RuntimeError("feedback failed")

    assert path.read_bytes() == original
    assert snapshots.read_dashboard_snapshot(
        "model_quality", snapshot_dir=directory, db_path=database
    )["html"] == "<section>quality v1</section>"

    with snapshots.model_quality_feedback_invalidation(
        snapshot_dir=directory, db_path=database
    ) as invalidation:
        invalidation.keep_invalidated()
    assert path.read_bytes() == original
    assert (
        snapshots.read_dashboard_snapshot(
            "model_quality", snapshot_dir=directory, db_path=database
        )
        is None
    )


def test_publisher_cannot_clear_invalidation_created_during_render(
    snapshot_env, monkeypatch
):
    directory, database = snapshot_env
    with sqlite3.connect(database) as connection:
        connection.execute("CREATE TABLE observations (value TEXT NOT NULL)")
    snapshots.write_dashboard_snapshot(
        "model_quality",
        "<section>quality v1</section>",
        generated_at="2000-01-01T01:00:00Z",
        strategy_version="score:v1",
        mapping_version="asset-map:v1",
        data_version="1",
        snapshot_dir=directory,
        db_path=database,
    )
    path = directory / "model_quality.json"
    original = path.read_bytes()
    monkeypatch.setattr(
        snapshots,
        "_current_model_quality_contract",
        lambda: ("score:v1", "asset-map:v1"),
    )

    def render_then_invalidate(_app, card):
        assert card == "model_quality"
        with snapshots.model_quality_feedback_invalidation(
            snapshot_dir=directory, db_path=database
        ) as invalidation:
            invalidation.keep_invalidated()
        return snapshots._RenderedCard(
            html="<section>stale render</section>",
            generated_at="2000-01-01T01:01:00Z",
            strategy_version="score:v1",
            mapping_version="asset-map:v1",
        )

    monkeypatch.setattr(snapshots, "_render_card", render_then_invalidate)
    with pytest.raises(
        snapshots.DashboardSnapshotBuildError, match="invalidation changed"
    ):
        snapshots._build_one_dashboard_snapshot(
            "model_quality",
            object(),
            snapshot_dir=directory,
            db_path=database,
        )

    assert path.read_bytes() == original
    assert (
        snapshots.read_dashboard_snapshot(
            "model_quality", snapshot_dir=directory, db_path=database
        )
        is None
    )


def test_coherent_publisher_clears_the_generation_it_rebuilt(
    snapshot_env, monkeypatch
):
    directory, database = snapshot_env
    with sqlite3.connect(database) as connection:
        connection.execute("CREATE TABLE observations (value TEXT NOT NULL)")
    snapshots.write_dashboard_snapshot(
        "model_quality",
        "<section>quality v1</section>",
        generated_at="2000-01-01T01:00:00Z",
        strategy_version="score:v1",
        mapping_version="asset-map:v1",
        data_version="1",
        snapshot_dir=directory,
        db_path=database,
    )
    with snapshots.model_quality_feedback_invalidation(
        snapshot_dir=directory, db_path=database
    ) as invalidation:
        invalidation.keep_invalidated()
    monkeypatch.setattr(
        snapshots,
        "_current_model_quality_contract",
        lambda: ("score:v2", "asset-map:v1"),
    )
    monkeypatch.setattr(
        snapshots,
        "_render_card",
        lambda _app, _card: snapshots._RenderedCard(
            html="<section>quality v2</section>",
            generated_at="2000-01-01T01:01:00Z",
            strategy_version="score:v2",
            mapping_version="asset-map:v1",
        ),
    )

    snapshots._build_one_dashboard_snapshot(
        "model_quality",
        object(),
        snapshot_dir=directory,
        db_path=database,
    )
    ready = snapshots.read_dashboard_snapshot(
        "model_quality",
        expected_contract=("score:v2", "asset-map:v1"),
        snapshot_dir=directory,
        db_path=database,
    )
    assert ready is not None
    assert ready["html"] == "<section>quality v2</section>"


def test_failed_replace_preserves_last_good_snapshot(snapshot_env, monkeypatch):
    directory, database = snapshot_env
    snapshots.write_dashboard_snapshot(
        "realtime",
        "<div>last good</div>",
        generated_at="2000-01-01T01:00:00Z",
        snapshot_dir=directory,
        db_path=database,
    )

    def fail_replace(_source, _target):
        raise OSError("injected replace failure")

    monkeypatch.setattr(snapshots.os, "replace", fail_replace)
    with pytest.raises(OSError, match="injected replace failure"):
        snapshots.write_dashboard_snapshot(
            "realtime",
            "<div>not published</div>",
            generated_at="2000-01-01T01:01:00Z",
            snapshot_dir=directory,
            db_path=database,
        )

    assert snapshots.read_dashboard_snapshot(
        "realtime", snapshot_dir=directory, db_path=database
    )["html"] == "<div>last good</div>"
    assert not list(directory.glob(".*.tmp"))


def test_realtime_sql_error_preserves_last_good_snapshot(
    snapshot_env, monkeypatch
):
    import sqlite3 as sqlite_module
    import sse_engine

    directory, database = snapshot_env
    monkeypatch.setenv("AI_FILTER_DASHBOARD_SNAPSHOTS", "1")
    snapshots.write_dashboard_snapshot(
        "realtime",
        "<div>last good realtime</div>",
        generated_at="2000-01-01T01:00:00Z",
        snapshot_dir=directory,
        db_path=database,
    )

    def fail_connect(*_args, **_kwargs):
        raise sqlite_module.OperationalError("injected read failure")

    monkeypatch.setattr(sqlite_module, "connect", fail_connect)
    app = Flask(__name__)
    app.add_url_rule(
        "/glance-sse",
        "snapshot_realtime_failure",
        sse_engine.glance_sse_widget,
        methods=["GET"],
    )

    with pytest.raises(snapshots.DashboardSnapshotBuildError):
        snapshots._build_one_dashboard_snapshot(
            "realtime",
            app,
            snapshot_dir=directory,
            db_path=database,
        )

    assert snapshots.read_dashboard_snapshot(
        "realtime", snapshot_dir=directory, db_path=database
    )["html"] == "<div>last good realtime</div>"


def test_model_quality_data_version_drift_preserves_last_good(
    snapshot_env, monkeypatch
):
    directory, database = snapshot_env
    with sqlite3.connect(database) as connection:
        connection.execute("CREATE TABLE observations (value TEXT NOT NULL)")
    snapshots.write_dashboard_snapshot(
        "model_quality",
        "<section>old quality</section>",
        generated_at="2000-01-01T01:00:00Z",
        strategy_version="score:v1",
        mapping_version="asset-map:v1",
        data_version="1",
        snapshot_dir=directory,
        db_path=database,
    )
    monkeypatch.setattr(
        snapshots,
        "_current_model_quality_contract",
        lambda: ("score:v1", "asset-map:v1"),
    )

    def render_while_database_changes(_app, card):
        assert card == "model_quality"
        with sqlite3.connect(database) as writer:
            writer.execute("INSERT INTO observations (value) VALUES ('changed')")
        return snapshots._RenderedCard(
            html="<section>new but incoherent</section>",
            generated_at="2000-01-01T01:01:00Z",
            strategy_version="score:v1",
            mapping_version="asset-map:v1",
        )

    monkeypatch.setattr(snapshots, "_render_card", render_while_database_changes)

    with pytest.raises(
        snapshots.DashboardSnapshotBuildError, match="inputs changed"
    ):
        snapshots._build_one_dashboard_snapshot(
            "model_quality",
            object(),
            snapshot_dir=directory,
            db_path=database,
        )

    assert snapshots.read_dashboard_snapshot(
        "model_quality",
        expected_contract=("score:v1", "asset-map:v1"),
        snapshot_dir=directory,
        db_path=database,
    )["html"] == "<section>old quality</section>"


def test_model_quality_builder_clears_route_cache_before_render(monkeypatch):
    import routes.dashboard_routes as dashboard_routes

    dashboard_routes._BACKTEST_CACHE = {("stale",): (1, {"old": True}, {})}
    monkeypatch.setattr(
        dashboard_routes,
        "render_backtest_compact_html",
        lambda result, _mapping: f"<div>{result['marker']}</div>",
    )

    class Response:
        status_code = 200
        mimetype = "text/html"

        @staticmethod
        def get_data():
            return b"<div>fresh</div>"

    class Client:
        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return False

        @staticmethod
        def get(path):
            assert path == snapshots.CARD_PATHS["model_quality"]
            assert dashboard_routes._BACKTEST_CACHE == {}
            dashboard_routes.render_backtest_compact_html(
                {
                    "marker": "fresh",
                    "strategy_version": "score:v1",
                    "mapping_version": "asset-map:v1",
                },
                {
                    "strategy_version": "score:v1",
                    "mapping_version": "asset-map:v1",
                    "data_complete": True,
                },
            )
            return Response()

    class App:
        @staticmethod
        def test_client():
            return Client()

    rendered = snapshots._render_model_quality_card(App())
    assert rendered.html == "<div>fresh</div>"
    assert rendered.strategy_version == "score:v1"
    assert rendered.mapping_version == "asset-map:v1"


def test_model_quality_incomplete_mapping_preserves_last_good(
    snapshot_env, monkeypatch
):
    import routes.dashboard_routes as dashboard_routes

    directory, database = snapshot_env
    with sqlite3.connect(database) as connection:
        connection.execute("CREATE TABLE observations (value TEXT NOT NULL)")
    snapshots.write_dashboard_snapshot(
        "model_quality",
        "<section>old quality</section>",
        generated_at="2000-01-01T01:00:00Z",
        strategy_version="score:v1",
        mapping_version="asset-map:v1",
        data_version="1",
        snapshot_dir=directory,
        db_path=database,
    )
    monkeypatch.setattr(
        snapshots,
        "_current_model_quality_contract",
        lambda: ("score:v1", "asset-map:v1"),
    )
    monkeypatch.setattr(
        dashboard_routes,
        "render_backtest_compact_html",
        lambda _result, _mapping: "<section>incomplete quality</section>",
    )

    class Response:
        status_code = 200
        mimetype = "text/html"

        @staticmethod
        def get_data():
            return b"<section>incomplete quality</section>"

    class Client:
        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return False

        @staticmethod
        def get(path):
            assert path == snapshots.CARD_PATHS["model_quality"]
            dashboard_routes.render_backtest_compact_html(
                {
                    "strategy_version": "score:v1",
                    "mapping_version": "asset-map:v1",
                },
                {
                    "strategy_version": "score:v1",
                    "mapping_version": "asset-map:v1",
                    "data_complete": False,
                    "status": "data_failed",
                },
            )
            return Response()

    class App:
        @staticmethod
        def test_client():
            return Client()

    with pytest.raises(
        snapshots.DashboardSnapshotBuildError,
        match="mapping-validation data is incomplete",
    ):
        snapshots._build_one_dashboard_snapshot(
            "model_quality",
            App(),
            snapshot_dir=directory,
            db_path=database,
        )

    assert snapshots.read_dashboard_snapshot(
        "model_quality",
        expected_contract=("score:v1", "asset-map:v1"),
        snapshot_dir=directory,
        db_path=database,
    )["html"] == "<section>old quality</section>"


def test_build_all_is_independent_and_keeps_fixed_order(snapshot_env, monkeypatch):
    directory, database = snapshot_env
    with sqlite3.connect(database) as connection:
        connection.execute("CREATE TABLE observations (value TEXT NOT NULL)")
    monkeypatch.setattr(
        snapshots,
        "_current_model_quality_contract",
        lambda: ("score:v1", "asset-map:v1"),
    )
    rendered = []

    def fake_render(_app, card):
        rendered.append((card, snapshots.CARD_PATHS[card]))
        if card == "briefing":
            raise snapshots.DashboardSnapshotBuildError("briefing unavailable")
        return snapshots._RenderedCard(
            html=f"<div>{card}</div>",
            generated_at="2000-01-01T01:00:00Z",
            strategy_version="score:v1" if card == "model_quality" else None,
            mapping_version="asset-map:v1" if card == "model_quality" else None,
        )

    monkeypatch.setattr(snapshots, "_render_card", fake_render)
    result = snapshots.build_all_dashboard_snapshots(
        object(), snapshot_dir=directory, db_path=database
    )

    assert [item[0] for item in rendered] == [
        "briefing",
        "realtime",
        "event_radar",
        "model_quality",
    ]
    assert dict(rendered) == snapshots.CARD_PATHS
    assert result["ok"] == ["realtime", "event_radar", "model_quality"]
    assert result["failed"] == ["briefing"]
    assert "DashboardSnapshotBuildError" in result["errors"]["briefing"]


@pytest.mark.skipif(os.name == "nt", reason="POSIX permission contract")
def test_reader_rejects_symlink_and_unsafe_directory_mode(snapshot_env):
    directory, database = snapshot_env
    outside = directory.parent / "outside.json"
    _write_raw_snapshot(outside, json.dumps({"not": "a snapshot"}).encode())
    directory.mkdir(mode=0o700)
    directory.chmod(0o700)
    (directory / "briefing.json").symlink_to(outside)
    assert (
        snapshots.read_dashboard_snapshot(
            "briefing", snapshot_dir=directory, db_path=database
        )
        is None
    )

    (directory / "briefing.json").unlink()
    directory.chmod(0o755)
    assert (
        snapshots.read_dashboard_snapshot(
            "briefing", snapshot_dir=directory, db_path=database
        )
        is None
    )


def test_writer_rejects_unknown_card_and_incomplete_html(snapshot_env):
    directory, database = snapshot_env
    with pytest.raises(snapshots.DashboardSnapshotValidationError):
        snapshots.write_dashboard_snapshot(
            "unknown",
            "<div>no</div>",
            snapshot_dir=directory,
            db_path=database,
        )
    with pytest.raises(snapshots.DashboardSnapshotValidationError):
        snapshots.write_dashboard_snapshot(
            "briefing",
            "not html",
            snapshot_dir=directory,
            db_path=database,
        )
