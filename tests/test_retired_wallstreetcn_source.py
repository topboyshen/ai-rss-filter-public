"""An unsupported China category must not be presented through warm caches."""

from flask import Flask
import pytest

import routes.feed_routes as routes


CHINA = "http://rsshub:1200/wallstreetcn/news/china"
GLOBAL = "http://rsshub:1200/wallstreetcn/news/global"
XML = "<rss><channel><item><title>global article</title><pubDate>Fri, 16 Oct 2026 10:00:00 GMT</pubDate></item></channel></rss>"


@pytest.fixture
def client(monkeypatch):
    cfg = {"cache_ttl": 60, "output": {"max_items": 20}}
    monkeypatch.setattr(routes, "_cfg", lambda: cfg)
    monkeypatch.setattr(routes, "check_api_key", lambda: True)
    monkeypatch.setattr(routes, "validate_target_url", lambda *_: (True, ""))
    monkeypatch.setattr(routes, "resolve_scoring_contract", lambda *_a, **_k: cfg)

    def forbidden(*_args, **_kwargs):
        pytest.fail("retired source must not read caches, query rows or call models")

    monkeypatch.setattr(routes, "get_cached", forbidden)
    monkeypatch.setattr(routes.DB, "get_entries_for_cached_rss", forbidden)
    monkeypatch.setattr(routes.DB, "get_recent_entries_for_feed_preview", forbidden)
    monkeypatch.setattr(routes, "process_feed", forbidden)
    app = Flask(__name__)
    app.config["TESTING"] = True
    app.register_blueprint(routes.feed_bp)
    return app.test_client()


@pytest.mark.parametrize("url", [CHINA, CHINA + "/", CHINA + "?limit=8",
                                  "http://127.0.0.1:1200/wallstreetcn/news/%63hina"])
@pytest.mark.parametrize("endpoint,key", [("/filter", "url"), ("/filter/multi", "urls"),
                                         ("/cached_feed", "url"), ("/feed/rss", "url"),
                                         ("/feed/json", "url"), ("/feed/html", "url")])
def test_retired_source_cannot_be_resurrected_from_cache(client, endpoint, key, url):
    response = client.get(endpoint, query_string={key: url})
    assert response.status_code == 410
    assert "入口已停用" in response.get_data(as_text=True)
    assert response.headers["X-RSSHub-Source-State"] == "retired"
    assert response.headers["Cache-Control"] == "no-store"
    assert b"<item>" not in response.data


def test_mixed_request_explicitly_rejects_retired_source_instead_of_silent_partial_feed(client):
    response = client.get("/filter/multi", query_string={"urls": GLOBAL + "," + CHINA})
    assert response.status_code == 410
    assert "入口已停用" in response.get_data(as_text=True)


def test_supported_global_warm_filter_remains_unchanged(client, monkeypatch):
    monkeypatch.setattr(routes, "get_cached", lambda *_: XML)
    monkeypatch.setattr(routes, "strategy_cache_key", lambda value, _cfg: value)
    monkeypatch.setattr(routes, "resolve_feed_url_to_cache_key", lambda value, _cfg: value)
    response = client.get("/filter", query_string={"url": GLOBAL})
    assert response.status_code == 200
    assert response.get_data(as_text=True) == XML
    assert "X-RSSHub-Source-State" not in response.headers


@pytest.mark.parametrize("url", [GLOBAL, "all", "https://example.com/article/china",
                                  "https://example.com/wallstreetcn/news/china-other"])
def test_retirement_is_not_a_generic_china_word_filter(url):
    assert routes._retired_rsshub_source_response([url]) is None


def test_filter_retains_authentication_before_retirement_response(client, monkeypatch):
    monkeypatch.setattr(routes, "check_api_key", lambda: False)
    response = client.get("/filter", query_string={"url": CHINA})
    assert response.status_code == 401
    assert "X-RSSHub-Source-State" not in response.headers
