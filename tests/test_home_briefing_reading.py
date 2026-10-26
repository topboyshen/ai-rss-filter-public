from copy import deepcopy
from html import escape
from html.parser import HTMLParser

from flask import Flask
import pytest

from core.digest_engine import render_themes_html
from services import briefing_reading


def _result(count=5):
    return {
        "overall_assessment": "既有总体观察",
        "market_impact": "既有市场影响",
        "risk_alert": "既有风险提示",
        "data_as_of": "2026-09-19T15:00:00+08:00",
        "generated_at": _fresh_snapshot_generated_at(),
        "source": "llm",
        "briefing_assembly": {
            "enabled": True,
            "input_count": 12,
            "selected_count": 8,
            "grounding_policy": {"enabled": False},
        },
        "quant_context": {"available": False},
        "themes": [
            {
                "theme": f"完整既有主题{i}",
                "theme_display_name": f"既有标题{i}",
                "direction": "中性",
                "importance": i + 4,
                "insight": f"既有洞察{i}",
                "action": f"既有观察{i}",
                "source_confirmation": "头部事件仅覆盖 1 个入选发布方；转载谱系未核验",
                "articles": [{
                    "title": f"原文{i}",
                    "link": f"https://example.com/{i}",
                    "score": 8,
                    "source_name": "既有来源",
                    "time": "09-19 14:50",
                }],
            }
            for i in range(count)
        ],
    }


def _split(html):
    preview, disclosure = html.split('<details class="briefing-reading-full"', 1)
    full = disclosure.split("</summary>\n", 1)[1].removesuffix("\n</details></section>")
    return preview, full


@pytest.mark.parametrize("count", [0, 1, 3, 5])
def test_preview_is_bounded_and_full_content_keeps_existing_order(count):
    result = _result(count)
    before = deepcopy(result)
    html = briefing_reading.render_briefing_reading_html(result, 48)
    preview, full = _split(html)

    assert preview.count('class="briefing-reading-theme"') == min(count, 3)
    shown = min(count, 3)
    expected_counter = (
        f"展示前 {shown} 个，共 {count} 个主题" if count > shown else f"共 {count} 个主题"
    )
    assert expected_counter in preview
    assert full == render_themes_html(result, 48)
    assert result == before
    for index in range(count):
        assert (f"既有标题{index}" in preview) == (index < 3)
        assert f"既有标题{index}" in full
    assert [full.index(f"既有标题{i}") for i in range(count)] == sorted(
        full.index(f"既有标题{i}") for i in range(count)
    )
    if count:
        assert "转载谱系未核验" in preview
        assert "来源覆盖说明" in preview
    else:
        assert "当前结果暂无主题" in preview
        assert "既有总体观察" in full


def test_wrapper_passes_identical_full_result_once_and_uses_native_details(monkeypatch):
    result = _result()
    calls = []
    sentinel = "<section>完整渲染，包括所有原有内容</section>"
    monkeypatch.setattr(
        briefing_reading, "render_themes_html",
        lambda value, hours: calls.append((value, hours)) or sentinel,
    )
    html = briefing_reading.render_briefing_reading_html(result, 36)
    starts = []
    parser = HTMLParser()
    parser.handle_starttag = lambda tag, attrs: starts.append((tag, dict(attrs)))
    parser.feed(html)

    assert calls == [(result, 36)]
    assert calls[0][0] is result
    assert _split(html)[1] == sentinel
    details = [attrs for tag, attrs in starts if tag == "details"]
    assert details and all("open" not in attrs for attrs in details)
    assert len([tag for tag, _attrs in starts if tag == "summary"]) == len(details)
    assert "<script" not in html


def test_full_disclosure_keeps_grounding_quant_caveats_risk_and_both_times():
    from datetime import datetime

    result = _result()
    generated_label = "生成：" + datetime.fromisoformat(
        str(result["generated_at"]).replace("Z", "+00:00")
    ).strftime("%Y-%m-%d %H:%M")
    _preview, full = _split(briefing_reading.render_briefing_reading_html(result, 48))
    for label in (
        "候选 12 · 入选 8", "事实锚定已关闭", "转载谱系未核验",
        "既有总体观察", "既有市场影响", "既有风险提示", "AI × Quant 双轴旁证",
        "不可达或已过期，已中性降级", "非当日涨跌判断",
        "仅作旁证，不参与 AI 筛选、评分或交易决策", "09-19 14:50",
        "数据截至：2026-09-19 15:00", generated_label,
    ):
        assert label in full


def test_preview_escapes_every_dynamic_field_and_preserves_safe_full_links():
    result = _result(1)
    payload = '\"><svg/onload=alert(1)>'
    theme = result["themes"][0]
    for key in ("theme", "theme_display_name", "direction", "importance", "source_confirmation"):
        theme[key] = payload
    theme["articles"][0].update(title=payload, link="javascript:alert(1)")
    html = briefing_reading.render_briefing_reading_html(result, 48)
    preview, full = _split(html)

    assert payload not in html and "<svg" not in html
    assert preview.count(escape(payload)) == 5
    assert "javascript:" not in html
    assert 'href="#"' in full
    assert 'rel="noopener noreferrer"' in full


def test_missing_source_is_explicit_and_existing_display_fallback_is_used():
    result = _result(1)
    del result["themes"][0]["source_confirmation"]
    del result["themes"][0]["theme_display_name"]
    preview, _full = _split(briefing_reading.render_briefing_reading_html(result, 48))
    assert "来源覆盖信息未记录" in preview
    assert "完整既有主题0" in preview
    assert "来源覆盖说明" in preview


@pytest.mark.parametrize("result", [
    None, [], {"themes": None}, {"themes": "invalid"}, {"themes": [None]},
    {"themes": [{"articles": [None]}]},
    {"themes": [], "briefing_assembly": {"enabled": True, "input_count": "invalid"}},
])
def test_invalid_result_is_not_reported_as_an_empty_briefing(result):
    with pytest.raises((AttributeError, TypeError, ValueError, OverflowError)):
        briefing_reading.render_briefing_reading_html(result, 48)


def _app():
    import routes.dashboard_routes as routes

    app = Flask(__name__)
    app.config["TESTING"] = True
    app.register_blueprint(routes.dash_bp)
    return app, routes


def test_worker_uses_same_homepage_profile_and_complete_result(monkeypatch):
    from services import dashboard_snapshots as snapshots

    app, routes = _app()
    result = _result()
    calls = []
    monkeypatch.setenv("AI_FILTER_ROLE", "worker")
    monkeypatch.setenv("AI_FILTER_DASHBOARD_SNAPSHOTS", "1")
    monkeypatch.setattr(routes, "_briefing_default_hours", lambda: 48)
    monkeypatch.setattr(routes, "_get_briefing_articles", lambda **kw: calls.append(kw) or [])
    monkeypatch.setattr(routes, "_select_briefing_result", lambda *args, **kw: (result, "local"))
    monkeypatch.setattr(routes, "attach_quant_market_context", lambda value: value)

    rendered = snapshots._render_card(app, "briefing")

    assert snapshots.CARD_PATHS["briefing"] == "/briefing-card?min_score=7&limit=12"
    assert calls == [{"min_score": 7.0, "limit": 12, "hours": 48, "feed_url": None, "overfetch": True}]
    assert _split(rendered.html)[1] == render_themes_html(result, 48)


def test_web_homepage_serves_same_worker_snapshot_without_live_work(monkeypatch):
    from services import dashboard_snapshots as snapshots

    app, routes = _app()
    html = briefing_reading.render_briefing_reading_html(_result(), 48)
    reads = []
    monkeypatch.setenv("AI_FILTER_ROLE", "web")
    monkeypatch.setenv("AI_FILTER_DASHBOARD_SNAPSHOTS", "1")
    monkeypatch.setattr(
        snapshots, "read_dashboard_snapshot",
        lambda card, expected_contract=None: reads.append((card, expected_contract)) or {
            "html": html, "generated_at": _fresh_snapshot_generated_at(),
        },
    )

    def forbidden(*_args, **_kwargs):
        pytest.fail("Web must serve the worker snapshot without any new live work")

    for name in ("_get_briefing_articles", "_select_briefing_result", "attach_quant_market_context", "render_briefing_reading_html"):
        monkeypatch.setattr(routes, name, forbidden)
    response = app.test_client().get("/briefing-card?min_score=7&limit=12")
    assert response.status_code == 200
    assert response.headers["X-Card-Snapshot-State"] == "ready"
    assert response.headers["Widget-Title"] == "Briefing"
    assert html in response.get_data(as_text=True)
    assert reads == [("briefing", None)]


def test_full_briefing_html_route_keeps_original_rendering(monkeypatch):
    app, routes = _app()
    result = _result()
    expected = render_themes_html(result, 48)
    monkeypatch.setattr(routes, "_cfg", lambda: {})
    monkeypatch.setattr(routes, "_get_briefing_articles", lambda **kw: [("row",)])
    monkeypatch.setattr(routes, "_briefing_article_from_row", lambda row: {"title": "原文"})
    monkeypatch.setattr(routes, "enrich_article_analysis", lambda value, **kw: value)
    monkeypatch.setattr(routes, "dedupe_articles", lambda values: values)
    monkeypatch.setattr(routes, "_select_briefing_result", lambda *args, **kw: (result, "local"))
    monkeypatch.setattr(routes, "attach_quant_market_context", lambda value: value)
    response = app.test_client().get("/briefing?format=html&hours=48")
    assert response.status_code == 200
    assert response.get_data(as_text=True) == expected
    assert "briefing-reading" not in response.get_data(as_text=True)


@pytest.mark.parametrize("result", [
    {"themes": None},
    {"themes": [], "briefing_assembly": {"enabled": True, "input_count": "invalid"}},
])
def test_invalid_briefing_preserves_worker_last_good_snapshot(tmp_path, monkeypatch, result):
    from services import dashboard_snapshots as snapshots

    app, routes = _app()
    app.config["TESTING"] = False
    directory = tmp_path / "snapshots"
    database = tmp_path / "cache.db"
    monkeypatch.setenv("AI_FILTER_ROLE", "worker")
    monkeypatch.setenv("AI_FILTER_DASHBOARD_SNAPSHOTS", "1")
    monkeypatch.setenv("AI_FILTER_VERSION", "my-ai-filter:test-reading")
    monkeypatch.setattr(routes, "_get_briefing_articles", lambda **kw: [])
    monkeypatch.setattr(routes, "_select_briefing_result", lambda *args, **kw: (result, "local"))
    monkeypatch.setattr(routes, "attach_quant_market_context", lambda value: value)
    good_html = briefing_reading.render_briefing_reading_html(_result(), 48)
    snapshots.write_dashboard_snapshot(
        "briefing", good_html, generated_at=_fresh_snapshot_generated_at(),
        snapshot_dir=directory, db_path=database,
    )
    saved = (directory / "briefing.json").read_bytes()

    with pytest.raises(snapshots.DashboardSnapshotBuildError, match="HTTP 500"):
        snapshots._build_one_dashboard_snapshot(
            "briefing", app, snapshot_dir=directory, db_path=database,
        )

    assert (directory / "briefing.json").read_bytes() == saved
    snapshot = snapshots.read_dashboard_snapshot(
        "briefing", snapshot_dir=directory, db_path=database,
    )
    assert snapshot["html"] == good_html
def _fresh_snapshot_generated_at() -> str:
    from datetime import datetime, timedelta, timezone

    return (datetime.now(timezone.utc) - timedelta(seconds=30)).strftime(
        "%Y-%m-%dT%H:%M:%S+00:00"
    )
