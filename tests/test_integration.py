"""
AI-Filter 集成测试：覆盖所有关键端点的组合场景
运行: python3 -m pytest tests/test_integration.py -v
"""
import pytest
import os
import subprocess
import sys
import time
import json
from pathlib import Path

# conftest sets a unique process-local path before pytest imports this module.
# Refuse to collect this suite without that isolation instead of falling back
# to a shared /tmp or project database.
if not os.environ.get("AI_FILTER_DB_PATH"):
    raise RuntimeError("tests require conftest to set an isolated AI_FILTER_DB_PATH")
os.environ.setdefault("AI_FILTER_CONFIG_PATH", os.path.join(os.path.dirname(__file__), "..", "config.yaml"))

# 注意：不要在这里弹出 sys.modules 重新导入——那会产生第二个 ConfigHolder/模块实例，
# 导致 routes.shared 持有的旧单例与测试 patch 的新单例错位，污染整个测试会话。
import app as app_module


@pytest.fixture
def client():
    app_module.initialize_app()
    app_module.app.config["TESTING"] = True
    # Protected endpoints are fail-closed; tests use an explicit key.
    orig_key = app_module.config.cfg.get("security", {}).get("api_key", "")
    app_module.config.cfg.setdefault("security", {})["api_key"] = "test-api-key"
    try:
        with app_module.app.test_client() as c:
            c.environ_base["HTTP_X_API_KEY"] = "test-api-key"
            yield c
    finally:
        app_module.config.cfg.setdefault("security", {})["api_key"] = orig_key


def test_collect_only_never_touches_requested_external_database(tmp_path):
    """Collection must override caller paths before importing the Flask app."""
    project_root = Path(__file__).resolve().parents[1]
    external_db = tmp_path / "must-not-be-opened.db"
    sentinel = b"not-a-sqlite-database\n"
    external_db.write_bytes(sentinel)
    before = external_db.stat()
    env = os.environ.copy()
    env["AI_FILTER_DB_PATH"] = str(external_db)

    result = subprocess.run(
        [
            sys.executable,
            "-m",
            "pytest",
            "--collect-only",
            "-q",
            "tests/test_integration.py",
        ],
        cwd=project_root,
        env=env,
        text=True,
        capture_output=True,
        timeout=60,
        check=False,
    )

    assert result.returncode == 0, result.stdout + result.stderr
    assert external_db.read_bytes() == sentinel
    assert external_db.stat().st_mtime_ns == before.st_mtime_ns
    assert not Path(f"{external_db}-wal").exists()
    assert not Path(f"{external_db}-shm").exists()


class TestHealthAndReadiness:
    def test_health_returns_200(self, client):
        resp = client.get("/health")
        assert resp.status_code == 200
        # /health 返回 "OK" 纯文本
        assert resp.data.decode().strip() == "OK"

    def test_web_health_fails_closed_when_browser_auth_is_not_ready(
        self, client, monkeypatch
    ):
        import routes.browser_auth as browser_auth

        monkeypatch.setenv("AI_FILTER_ROLE", "web")
        monkeypatch.setattr(browser_auth, "browser_login_ready", lambda: False)

        resp = client.get("/health")

        assert resp.status_code == 503
        assert resp.data.decode().strip() == "NOT READY"

    def test_web_health_accepts_ready_browser_auth(self, client, monkeypatch):
        import routes.browser_auth as browser_auth

        monkeypatch.setenv("AI_FILTER_ROLE", "web")
        monkeypatch.setattr(browser_auth, "browser_login_ready", lambda: True)

        resp = client.get("/health")

        assert resp.status_code == 200
        assert resp.data.decode().strip() == "OK"

    def test_readiness_without_strict(self, client):
        resp = client.get("/readiness")
        assert resp.status_code == 200

    def test_strict_readiness_rejects_latest_partial_refresh(self, client, monkeypatch):
        import routes.admin_routes as admin_routes

        monkeypatch.setattr(
            admin_routes,
            "_staleness_info",
            lambda _cfg: {
                "last_full_refresh_at": "2026-07-15T10:00:00",
                "seconds_since_full_refresh": 60,
                "staleness_threshold_seconds": 14400,
                "stale": False,
                "degraded": True,
                "last_attempt": {"status": "partial"},
                "last_ingestion_at": None,
            },
        )

        resp = client.get("/readiness?strict=1")

        assert resp.status_code == 503
        assert resp.get_json()["reason"] == "degraded"


class TestSecurityControls:
    def test_ssrf_blocked_localhost_non_feed(self, client):
        """非白名单的本地地址应被拦截"""
        resp = client.get("/cached_feed?url=http://127.0.0.1:22/")
        assert resp.status_code == 400

    def test_ssrf_blocked_aws_metadata(self, client):
        resp = client.get("/cached_feed?url=http://169.254.169.254/latest/meta-data/")
        assert resp.status_code == 400

    def test_ssrf_allowed_rsshub_feed(self, client):
        """config 中配置的 RSSHub 源应被放行"""
        resp = client.get("/cached_feed?url=http://127.0.0.1:1200/cls/telegraph/watch")
        # 即使返回空 RSS，也应是 200（不是 400 被 SSRF 拦截）
        assert resp.status_code == 200
        assert b"rss" in resp.data.lower() or b"xml" in resp.data.lower()

    def test_filter_ssrf_blocked(self, client):
        resp = client.get("/filter?url=http://192.168.1.1/secret")
        assert resp.status_code == 400

    def test_filter_multi_ssrf_blocked(self, client):
        resp = client.get("/filter/multi?urls=http://10.0.0.1/feed")
        assert resp.status_code == 400


class TestProtectedEndpointsWithoutKey:
    def test_stats_401_without_key(self, client):
        # 模拟有 API Key 环境
        orig_sec = app_module.config.cfg.get("security", {})
        old_key = orig_sec.get("api_key", "")
        orig_sec["api_key"] = "test-secret-key-12345"
        try:
            resp = client.get("/stats")
            assert resp.status_code == 401
        finally:
            orig_sec["api_key"] = old_key


class TestDashboardRendering:
    def test_dashboard_200(self, client):
        resp = client.get("/dashboard")
        assert resp.status_code == 200
        assert b"AI-Filter" in resp.data

    def test_dashboard_no_server_error(self, client):
        """Dashboard 不应返回 500（防止回归日志列索引错位问题）"""
        resp = client.get("/dashboard")
        assert resp.status_code != 500

    def test_dashboard_briefing_uses_configured_default_window(self, client, monkeypatch):
        import routes.dashboard_routes as dashboard_routes

        captured = []
        monkeypatch.setattr(
            dashboard_routes,
            "_get_briefing_articles",
            lambda **kwargs: captured.append(kwargs) or [],
        )
        monkeypatch.setattr(
            dashboard_routes,
            "attach_quant_market_context",
            lambda result: result,
        )

        response = client.get("/dashboard")

        assert response.status_code == 200
        assert captured == [{"limit": 12, "hours": 24, "overfetch": True}]

    def test_dashboard_sanitizes_all_article_links_before_render(self, client, monkeypatch):
        import routes.dashboard_routes as dashboard_routes

        unsafe_links = [
            "javascript:alert(1)",
            "data:text/html,<svg/onload=alert(1)>",
            'https://example.com/" onmouseover="alert(1)',
        ]
        summary_rows = [
            (
                link,
                "安全标题",
                "安全标题",
                8.0,
                "主题",
                "逻辑",
                "足够长的安全摘要内容用于仪表盘展示",
                int(time.time()),
            )
            for link in unsafe_links
        ]
        briefing_rows = [row + ("https://feed.example/rss",) for row in summary_rows]
        captured = {}

        monkeypatch.setattr(
            dashboard_routes,
            "_get_dashboard_summary_articles",
            lambda **_kwargs: summary_rows,
        )
        monkeypatch.setattr(
            dashboard_routes,
            "_get_briefing_articles",
            lambda **_kwargs: briefing_rows,
        )

        def fake_local_briefing(articles, hours=48, **_kwargs):
            captured["links"] = [article["link"] for article in articles]
            return {
                "themes": [],
                "overall_assessment": "安全",
                "market_impact": "",
                "risk_alert": "",
                "generated_at": "",
            }

        monkeypatch.setattr(dashboard_routes, "_local_briefing_from_articles", fake_local_briefing)

        response = client.get("/dashboard")
        body = response.get_data(as_text=True)

        assert response.status_code == 200
        assert captured["links"][:2] == ["#", "#"]
        assert "javascript:" not in body
        assert "data:text/html" not in body
        assert body.count('href="#"') >= 2
        assert 'href="https://example.com/&#34; onmouseover=&#34;alert(1)"' in body
        assert '" onmouseover="alert(1)' not in body
        assert 'rel="noopener noreferrer"' in body


class TestDigestEngine:
    def test_digest_uses_configured_deepseek_backend_without_name_filter(self):
        from core.digest_engine import aggregate_themes

        class _Backend:
            name = "deepseek-compatible"
            timeout = 1

            def is_healthy(self):
                return True

            def record_fail(self):
                raise AssertionError("configured backend should not be skipped or marked failed")

        class _Scorer:
            backends = [_Backend()]

            def _call_llm(self, backend, messages, max_tokens, timeout=None, purpose="score", cfg=None):
                assert backend.name == "deepseek-compatible"
                return '{"themes":[{"theme":"流动性","direction":"中性","importance":7,"insight":"央行操作","action":"观察","background":"","why_it_matters":"","market_impact":""}],"overall_assessment":"ok","market_impact":"","background":"","why_it_matters":"","risk_alert":"","action_hint":"","confidence":0.7}'

        result = aggregate_themes([
            {"title": "央行开展逆回购", "score": 8.0, "tag": "流动性", "logic": "", "summary": ""}
        ], scorer=_Scorer())
        # The backend supplies commentary, while the configured local bucket
        # remains authoritative for the theme identity.
        assert result["themes"][0]["theme"] == "国内货币政策与流动性"
        assert result["source"] == "llm"


class TestRateLimit:
    def test_refresh_rate_limit(self, client, monkeypatch):
        import routes.webhook_routes as webhook_routes

        queued = []
        monkeypatch.setattr(
            webhook_routes,
            "_submit_background_task",
            lambda task: queued.append(task) or "stored",
        )

        # 模拟无 API Key 环境
        orig_sec = app_module.config.cfg.get("security", {})
        old_key = orig_sec.get("api_key", "")
        orig_sec["api_key"] = "test-api-key"
        try:
            # 第一次请求
            resp1 = client.post("/refresh")
            assert resp1.status_code == 200
            # 第二次请求应被限流
            resp2 = client.post("/refresh")
            assert resp2.status_code == 429
            data = json.loads(resp2.data)
            assert "过于频繁" in data.get("error", "")
            assert queued[0]["task_type"] == "refresh"
        finally:
            orig_sec["api_key"] = old_key


class TestCachedFeedFallback:
    def test_cached_feed_returns_rss_structure(self, client):
        resp = client.get("/cached_feed?url=http://127.0.0.1:1200/cls/telegraph/watch")
        assert resp.status_code == 200
        data = resp.data.decode()
        assert "<rss" in data
        assert "</rss>" in data
        assert "<channel>" in data


class TestBriefingRoutes:
    def test_briefing_display_title_removes_search_highlight_markup(self):
        from utils.helpers import format_article_display_title

        assert format_article_display_title(
            "<em>A股</em>超4600只个<em>股</em>飘绿",
            "<em>A股</em>超4600只个<em>股</em>飘绿",
        ) == "A股超4600只个股飘绿"
        assert format_article_display_title(
            "&lt;em&gt;A股&lt;/em&gt;超4600只个股飘绿",
            "&lt;em&gt;A股&lt;/em&gt;超4600只个股飘绿",
        ) == "A股超4600只个股飘绿"

    def test_briefing_noise_helpers_use_article_dict_shape(self):
        import routes.dashboard_routes as dashboard_routes

        row = (
            "https://example.com/a", "AI 算力链出现订单变化", "【快讯】AI 算力链出现订单变化",
            8.2, "AI 算力链", "多源验证", "摘要内容足够长用于展示", int(time.time()),
            "http://rsshub:1200/jin10",
        )
        article = dashboard_routes._briefing_article_from_row(row)
        assert article["feed_url"] == "http://rsshub:1200/jin10"
        article_tuple = dashboard_routes._briefing_tuple_from_article(article)
        assert article_tuple[0] == row[0]
        assert article_tuple[2:8] == row[2:8]
        assert article_tuple[8] == row[8]
        assert dashboard_routes._is_briefing_noise(row[:8]) is False

    def test_briefing_theme_uses_configurable_rules(self):
        import routes.dashboard_routes as dashboard_routes

        article = {"title": "低空经济产业链订单落地", "tag": "", "logic": ""}
        cfg = {"briefing_themes": [{"name": "低空经济", "keywords": ["低空经济", "eVTOL"]}]}
        assert dashboard_routes._briefing_theme_for_article(article, cfg=cfg) == "低空经济"

    def test_briefing_theme_falls_back_to_tag_when_config_has_no_match(self):
        import routes.dashboard_routes as dashboard_routes

        article = {"title": "普通市场新闻", "tag": "市场观察", "logic": ""}
        assert dashboard_routes._briefing_theme_for_article(article, cfg={"briefing_themes": []}) == "市场观察"

    def test_briefing_card_llm_switch_queues_worker_and_returns_local_result(self, client, monkeypatch):
        import routes.dashboard_routes as dashboard_routes

        submitted = []
        monkeypatch.setattr(
            dashboard_routes,
            "_get_briefing_articles",
            lambda **kwargs: [(
                "https://example.com/a", "央行开展逆回购", "央行开展逆回购", 8.5,
                "流动性", "政策追踪", "央行开展逆回购操作", int(time.time()),
                "http://rsshub:1200/jin10",
            )],
        )
        monkeypatch.setattr(
            "services.shared_cache.get_shared_json",
            lambda _name: None,
        )
        monkeypatch.setattr(
            "routes.webhook_routes._submit_background_task",
            lambda task: submitted.append(task) or "stored",
        )
        monkeypatch.setattr(
            dashboard_routes,
            "aggregate_themes",
            lambda *_args, **_kwargs: (_ for _ in ()).throw(
                AssertionError("Gunicorn request must not run LLM aggregation")
            ),
        )
        resp = client.get("/briefing-card?llm=true")
        assert resp.status_code == 200
        assert resp.headers["X-Briefing-Refresh"] == "stored"
        assert len(submitted) == 1
        assert submitted[0]["task_type"] == "briefing_refresh"
        assert submitted[0]["cache_version"] == dashboard_routes._BRIEFING_CACHE_VERSION
        assert isinstance(submitted[0]["request_window"], int)

    def test_briefing_worker_refresh_writes_shared_llm_result(self, monkeypatch):
        import routes.dashboard_routes as dashboard_routes

        row = (
            "https://example.com/a", "央行开展逆回购", "央行开展逆回购", 8.5,
            "流动性", "政策追踪", "央行开展逆回购操作", int(time.time()),
            "http://rsshub:1200/jin10", "中性",
        )
        stored = {}
        monkeypatch.setattr(dashboard_routes, "_get_briefing_articles", lambda **_kwargs: [row])
        monkeypatch.setattr(
            dashboard_routes,
            "aggregate_themes",
            lambda articles, scorer, period_hours, **_kwargs: {
                "themes": [],
                "overall_assessment": "worker result",
                "article_count": len(articles),
                "generated_at": "2026-07-14T00:00:00",
                "source": "llm",
            },
        )
        monkeypatch.setattr(
            "services.shared_cache.set_shared_json",
            lambda name, value, ttl: stored.update(name=name, value=value, ttl=ttl) or True,
        )

        assert dashboard_routes.run_briefing_refresh_command({
            "cache_version": dashboard_routes._BRIEFING_CACHE_VERSION,
            "hours": 48,
            "min_score": 7,
            "limit": 12,
            "feed_url": "",
        }) is True
        assert stored["name"].startswith("briefing:")
        assert stored["value"]["result"]["overall_assessment"] == "worker result"
        assert stored["ttl"] == dashboard_routes._BRIEFING_LLM_CACHE_TTL_SEC

    def test_briefing_cache_hit_keeps_worker_article_id_snapshot(self, client, monkeypatch):
        import routes.dashboard_routes as dashboard_routes

        cfg = {
            "briefing_assembly": {
                "enabled": True,
                "max_themes": 4,
                "max_articles_per_theme": 3,
                "max_share_ratio": 0.4,
                "reserved_non_head_slots": 1,
                "grounding": {"enabled": True},
            },
            "briefing_themes": [],
        }
        current_row = (
            "https://current.example/b", "当前滚动文章B", "当前滚动文章B", 9.0,
            "当前主题", "", "当前摘要", int(time.time()),
            "https://current.example/rss", "中性",
        )
        cached = {
            "themes": [{
                "theme": "缓存主题",
                "direction": "中性",
                "importance": 9.0,
                "insight": "缓存快照",
                "evidence_article_ids": ["a1"],
                "articles": [{
                    "briefing_article_id": "a1",
                    "title": "工作线程文章A",
                    "link": "https://worker.example/a",
                    "score": 9.1,
                }],
            }],
            "articles": [{
                "briefing_article_id": "a1",
                "title": "工作线程文章A",
                "link": "https://worker.example/a",
                "score": 9.1,
            }],
            "article_count": 1,
            "candidate_count": 1,
            "overall_assessment": "缓存结果",
            "source": "llm",
        }
        monkeypatch.setattr(dashboard_routes, "_cfg", lambda: cfg)
        monkeypatch.setattr(
            dashboard_routes,
            "_get_briefing_articles",
            lambda **_kwargs: [current_row],
        )
        monkeypatch.setattr(
            dashboard_routes,
            "_get_cached_briefing_llm",
            lambda _name: cached,
        )
        monkeypatch.setattr(
            dashboard_routes,
            "_queue_briefing_llm_refresh",
            lambda *_args, **_kwargs: (_ for _ in ()).throw(
                AssertionError("cache hit must not queue")
            ),
        )
        monkeypatch.setattr(
            dashboard_routes,
            "attach_quant_market_context",
            lambda result: result,
        )

        response = client.get("/briefing?llm=true&format=json")
        payload = response.get_json()

        assert response.status_code == 200
        assert payload["articles"][0]["link"] == "https://worker.example/a"
        assert payload["articles"][0]["briefing_article_id"] == "a1"
        assert payload["themes"][0]["evidence_article_ids"] == ["a1"]
        assert "https://current.example/b" not in response.get_data(as_text=True)

    def test_briefing_fingerprint_covers_every_input_field(self):
        import routes.dashboard_routes as dashboard_routes

        base = ["link", "title", "raw", 8.5, "tag", "logic", "summary", 123, "feed", "看多"]
        fingerprint = dashboard_routes._briefing_rows_fingerprint([tuple(base)])
        for index in range(10):
            changed = list(base)
            changed[index] = f"changed-{index}"
            assert dashboard_routes._briefing_rows_fingerprint([tuple(changed)]) != fingerprint

    def test_briefing_worker_retries_fallback_without_caching(self, monkeypatch):
        import routes.dashboard_routes as dashboard_routes

        row = (
            "https://example.com/a", "标题", "原标题", 8.5, "主题", "逻辑", "摘要",
            int(time.time()), "http://rsshub:1200/jin10", "中性",
        )
        monkeypatch.setattr(dashboard_routes, "_get_briefing_articles", lambda **_kwargs: [row])
        monkeypatch.setattr(dashboard_routes, "_get_cached_briefing_llm", lambda _name: None)
        monkeypatch.setattr(
            dashboard_routes,
            "aggregate_themes",
            lambda *_args, **_kwargs: {"themes": [], "source": "fallback"},
        )
        monkeypatch.setattr(
            "services.shared_cache.set_shared_json",
            lambda *_args, **_kwargs: (_ for _ in ()).throw(AssertionError("fallback must not be cached")),
        )
        assert dashboard_routes.run_briefing_refresh_command({
            "cache_version": dashboard_routes._BRIEFING_CACHE_VERSION,
            "hours": 48, "limit": 12, "feed_url": "",
        }) is False

    def test_briefing_llm_push_waits_for_cache_hit(self, client, monkeypatch):
        import routes.dashboard_routes as dashboard_routes

        row = (
            "https://example.com/a", "AI订单上修", "AI订单上修", 8.5,
            "AI算力", "景气改善", "摘要内容", int(time.time()),
            "http://rsshub:1200/jin10", "看多",
        )
        monkeypatch.setattr(dashboard_routes, "_get_briefing_articles", lambda **_kwargs: [row])
        monkeypatch.setattr("services.shared_cache.get_shared_json", lambda _name: None)
        monkeypatch.setattr("routes.webhook_routes._submit_background_task", lambda _task: "stored")
        monkeypatch.setattr(
            dashboard_routes,
            "_push_digest_to_telegram",
            lambda *_args: (_ for _ in ()).throw(AssertionError("local fallback must not be pushed")),
        )
        response = client.post("/briefing?llm=true&push=true")
        assert response.status_code == 202
        assert response.headers["X-Briefing-Refresh"] == "stored"
        assert response.get_json()["status"] == "queued"

    def test_briefing_llm_push_reports_durable_queue_full(self, client, monkeypatch):
        import routes.dashboard_routes as dashboard_routes

        row = (
            "https://example.com/a", "AI订单上修", "AI订单上修", 8.5,
            "AI算力", "景气改善", "摘要内容", int(time.time()),
            "http://rsshub:1200/jin10", "看多",
        )
        monkeypatch.setattr(
            dashboard_routes, "_get_briefing_articles", lambda **_kwargs: [row]
        )
        monkeypatch.setattr(
            "services.shared_cache.get_shared_json", lambda _name: None
        )
        monkeypatch.setattr(
            "routes.webhook_routes._submit_background_task", lambda _task: "full"
        )

        response = client.post("/briefing?llm=true&push=true")

        assert response.status_code == 503
        assert response.headers["Retry-After"] == "30"
        assert response.headers["X-Briefing-Refresh"] == "full"
        assert response.get_json()["status"] == "queue_full"

    def test_durable_dispatcher_routes_briefing_refresh_to_worker_command(self, monkeypatch):
        import routes.dashboard_routes as dashboard_routes
        import routes.webhook_routes as webhook_routes

        seen = []
        monkeypatch.setattr(
            dashboard_routes,
            "run_briefing_refresh_command",
            lambda task: seen.append(task) or True,
        )
        task = {"task_type": "briefing_refresh", "fingerprint": "abc"}

        assert webhook_routes._execute_spooled_task(task) is True
        assert seen == [task]
