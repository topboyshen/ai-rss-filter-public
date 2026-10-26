"""Public home projection, real worker artifact, file-only Web and date safety."""
from datetime import datetime, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

from flask import Flask
import pytest

from test_event_daily_report import _report_event, _ts


@pytest.fixture
def client(monkeypatch):
    import db.database as database
    import routes.event_radar_routes as routes
    import routes.security as security

    database.init_db()
    security._rate_limit_store.clear()
    monkeypatch.setenv("AI_FILTER_DASHBOARD_SNAPSHOTS", "1")
    monkeypatch.setenv("AI_FILTER_ROLE", "worker")
    monkeypatch.setenv("AI_FILTER_VERSION", "my-ai-filter:test")
    app = Flask(__name__, template_folder=str(Path(__file__).resolve().parents[1] / "templates"))
    app.config["TESTING"] = True
    app.register_blueprint(routes.event_radar_bp)
    yield app.test_client()
    security._rate_limit_store.clear()


def _seed(monkeypatch):
    import db.database as database
    import routes.event_radar_routes as routes
    from services.event_home_digest import load_home_event_digest

    events = []
    for index in range(5):
        events.append(_report_event(
            database, f"highlight-{index}", f"公司{index}计划建设新工厂",
            f"https://news.example/{index}", at=_ts(5, 10), priority=8,
        ))
    _report_event(database, "denial", "甲公司否认获得订单", "https://news.example/denial",
                  at=_ts(5, 11), priority=1)
    _report_event(database, "request", "交易所要求乙公司澄清", "https://news.example/request",
                  at=_ts(5, 11), priority=7)
    monkeypatch.setattr(routes, "load_home_event_digest", lambda: load_home_event_digest(now=_ts(5, 12)))
    return events


def test_live_home_uses_existing_report_gates_and_bounded_public_fields(client, monkeypatch):
    import routes.event_radar_routes as routes
    import services.ingestion_status as coverage

    _seed(monkeypatch)
    monkeypatch.setattr(routes, "load_event_stream", lambda **_: pytest.fail("home must use report rules"))
    calls = []
    monkeypatch.setattr(coverage, "read_refresh_coverage", lambda: calls.append(1) or (
        "incomplete", "最近采集：19/20 个订阅源成功；异常源：全球源。",
    ))
    response = client.get("/event-radar-card?limit=8")
    body = response.get_data(as_text=True)
    assert response.status_code == 200
    assert response.headers["Widget-Title"] == "Event Changes"
    assert body.count('<article class="event-radar-summary-item"') == 5
    assert "待验证" in body and "要求澄清／问询 · 非否认" in body
    assert "甲公司否认获得订单" in body  # low-score denial remains visible
    assert "data-report-date=\"2026-10-05\"" in body
    assert "daily?date=2026-10-05" in body and "weekly?week=2026-10-05" in body
    assert "19/20" in body and "全球源" in body and calls == [1]
    assert 'href="https://news.example/denial"' in body
    assert "入库 2026-10-05 11:00" in body
    for private in ("score_path", "strategy_version", "mapping_version", "rule_association_count"):
        assert private not in body
    assert "有留存股票关联" not in body and "事件重要度" not in body


def test_real_worker_artifact_web_read_never_queries_or_rebuilds(client, monkeypatch, tmp_path):
    import routes.event_radar_routes as routes
    import services.dashboard_snapshots as snapshots
    import services.ingestion_status as coverage
    from services.event_home_digest import load_home_event_digest

    monkeypatch.setenv("AI_FILTER_DASHBOARD_SNAPSHOT_DIR", str(tmp_path / "snapshots"))
    # Real loader, template, worker writer and Web reader. No simulated read envelope.
    digest = load_home_event_digest()
    built = client.get("/event-radar-card?limit=8").get_data(as_text=True)
    snapshots.write_dashboard_snapshot("event_radar", built)
    monkeypatch.setenv("AI_FILTER_ROLE", "web")

    def forbidden(*_args, **_kwargs):
        pytest.fail("Web requests must use only the published artifact")

    monkeypatch.setattr(routes, "load_home_event_digest", forbidden)
    monkeypatch.setattr(routes, "load_daily_report", forbidden)
    monkeypatch.setattr(routes, "load_event_stream", forbidden)
    monkeypatch.setattr(coverage, "read_refresh_coverage", forbidden)
    import db.database as database
    monkeypatch.setattr(database, "get_read_only_connection", forbidden)
    response = client.get("/event-radar-card?limit=8")
    assert response.status_code == 200
    assert response.headers["X-Card-Snapshot-State"] == "ready"
    assert response.get_data(as_text=True) == built
    assert f'data-report-date="{digest["date"]}"' in built
    assert "证据不足" in built and "暂无达到首页展示门槛的事件变化" not in built


@pytest.mark.parametrize("case", ["low_new", "reposted_denial"])
def test_fresh_empty_home_does_not_claim_no_changes(client, monkeypatch, case):
    import db.database as database
    import routes.event_radar_routes as routes
    from services.event_daily_report import load_daily_report
    from services.event_home_digest import load_home_event_digest

    if case == "low_new":
        _report_event(database, "low-new", "甲公司发布新品", "https://news.example/new",
                      at=_ts(5, 11), priority=5)
    else:
        for day in (4, 5):
            _report_event(database, f"denial-{day}", "甲公司否认订单传闻",
                          f"https://news.example/denial-{day}", at=_ts(day, 11), priority=1)
    report = load_daily_report("2026-10-05", now=_ts(5, 12))
    if case == "low_new":
        assert next(block for block in report["blocks"] if block["key"] == "new")["total"] == 1
    else:
        assert report["repeat_event_count"] == 1
    digest = load_home_event_digest(now=_ts(5, 12))
    assert digest["data_state"] == "ok" and not digest["freshness"]["stale"]
    assert digest["highlights"] == [] and digest["corrections"] == []
    monkeypatch.setattr(routes, "load_home_event_digest", lambda: load_home_event_digest(now=_ts(5, 12)))
    body = client.get("/event-radar-card?limit=8").get_data(as_text=True)
    assert "暂无达到首页展示门槛的事件变化，更多内容见日报" in body
    assert "未识别到新变化" not in body
    assert "daily?date=2026-10-05" in body
    assert '<article class="event-radar-summary-item"' not in body


@pytest.mark.parametrize("case", ["unknown", "stale"])
def test_unknown_or_stale_empty_home_never_uses_normal_empty_copy(client, monkeypatch, case):
    import db.database as database
    import routes.event_radar_routes as routes
    from services.event_home_digest import load_home_event_digest

    if case == "stale":
        _report_event(database, "low-old", "甲公司发布新品", "https://news.example/old",
                      at=_ts(5, 1), priority=5)
    digest = load_home_event_digest(now=_ts(5, 12))
    assert digest["data_state"] == ("stale" if case == "stale" else "before_coverage")
    assert digest["highlights"] == [] and digest["corrections"] == []
    monkeypatch.setattr(routes, "load_home_event_digest", lambda: load_home_event_digest(now=_ts(5, 12)))
    body = client.get("/event-radar-card?limit=8").get_data(as_text=True)
    assert "证据不足，暂无法判断当前事件变化；不代表没有发生。" in body
    assert "暂无达到首页展示门槛的事件变化" not in body
    assert "未识别到新变化" not in body


def test_failed_build_is_503_and_preserves_published_artifact(client, monkeypatch, tmp_path):
    import routes.event_radar_routes as routes
    import services.dashboard_snapshots as snapshots

    monkeypatch.setenv("AI_FILTER_DASHBOARD_SNAPSHOT_DIR", str(tmp_path / "snapshots"))
    snapshots.write_dashboard_snapshot("event_radar", "<div>previous successful card</div>")
    previous = (tmp_path / "snapshots/event_radar.json").read_bytes()

    def failed():
        raise routes.EventRadarUnavailableError("private database details")

    monkeypatch.setattr(routes, "load_home_event_digest", failed)
    response = client.get("/event-radar-card?limit=8")
    assert response.status_code == 503
    assert response.headers["Cache-Control"] == "no-store"
    assert "private database" not in response.get_data(as_text=True)
    with pytest.raises(snapshots.DashboardSnapshotBuildError):
        snapshots._build_one_dashboard_snapshot("event_radar", client.application)
    assert (tmp_path / "snapshots/event_radar.json").read_bytes() == previous


@pytest.mark.parametrize("day_delta", [-1, 1])
def test_calendar_rollover_marks_stale_without_rewriting_date_links(client, monkeypatch, tmp_path, day_delta):
    import services.dashboard_snapshots as snapshots

    monkeypatch.setenv("AI_FILTER_DASHBOARD_SNAPSHOT_DIR", str(tmp_path / "snapshots"))
    today = datetime.now(ZoneInfo("Asia/Shanghai")).date()
    day = (today + timedelta(days=day_delta)).isoformat()
    html = f'<div data-report-date="{day}"><a href="/dashboard/events/daily?date={day}">日报</a></div>'
    # Fresh file is not enough: the report's calendar date still differs.
    snapshots.write_dashboard_snapshot("event_radar", html)
    monkeypatch.setenv("AI_FILTER_ROLE", "web")
    response = client.get("/event-radar-card?limit=8")
    body = response.get_data(as_text=True)
    assert response.headers["X-Card-Snapshot-State"] == "stale"
    assert "今日变化待刷新" in body and html in body
    assert f"date={today.isoformat()}" not in body


def test_expired_event_snapshot_retains_body_and_stale_warning(client, monkeypatch, tmp_path):
    import services.dashboard_snapshots as snapshots

    monkeypatch.setenv("AI_FILTER_DASHBOARD_SNAPSHOT_DIR", str(tmp_path / "snapshots"))
    old = (datetime.now(timezone.utc) - timedelta(days=2)).isoformat()
    snapshots.write_dashboard_snapshot("event_radar", "<div>stored events</div>", generated_at=old)
    monkeypatch.setenv("AI_FILTER_ROLE", "web")
    response = client.get("/event-radar-card?limit=8")
    assert response.headers["X-Card-Snapshot-State"] == "stale"
    assert "数据已过期" in response.get_data(as_text=True)
    assert "stored events" in response.get_data(as_text=True)


def test_home_content_and_sources_are_escaped(client, monkeypatch):
    import db.database as database
    import routes.event_radar_routes as routes
    from services.event_home_digest import load_home_event_digest

    title = '<svg/onload=alert(1)>公司否认收购传闻'
    _report_event(database, "hostile", title, "https://news.example/x?a=1&b=2",
                  at=_ts(5, 11), priority=7)
    monkeypatch.setattr(routes, "load_home_event_digest", lambda: load_home_event_digest(now=_ts(5, 12)))
    body = client.get("/event-radar-card?limit=8").get_data(as_text=True)
    assert "<svg" not in body and "&lt;svg/onload" in body
    assert 'target="_blank" rel="noopener noreferrer"' in body
    assert 'href="https://news.example/x?a=1&amp;b=2"' in body
