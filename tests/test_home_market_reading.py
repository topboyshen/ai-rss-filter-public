import pytest
from flask import Flask

from routes import dashboard_routes as dashboard
from services.reading_market import fold_market_quotes


def test_quotes_fold_is_closed_and_preserves_existing_html():
    cached = '<div class="quotes">Existing quote values</div>'
    result = fold_market_quotes(cached)
    assert cached in result
    assert "open" not in result.partition(">")[0]
    assert "不是新闻评分依据" in result


def test_reading_quotes_only_reads_same_cache(monkeypatch):
    app = Flask(__name__)
    cached = '<div>same frozen quote data</div>'
    monkeypatch.setattr("services.shared_cache.get_shared_json", lambda key: {"html": cached})
    monkeypatch.setattr(dashboard, "_refresh_quotes_cache", lambda: pytest.fail("refresh"))
    for path, folded in [("/quotes", False), ("/quotes?view=reading", True)]:
        with app.test_request_context(path):
            response = dashboard.quotes()
            body = response.get_data(as_text=True)
            assert ("<details" in body) == folded
            assert cached in body
            assert response.headers["Cache-Control"] == "no-cache, no-store, must-revalidate"


def test_missing_quotes_keep_unknown_placeholder(monkeypatch):
    app = Flask(__name__)
    monkeypatch.setattr("services.shared_cache.get_shared_json", lambda key: None)
    monkeypatch.setitem(dashboard._quotes_cache, "html", None)
    with app.test_request_context("/quotes?view=reading"):
        body = dashboard.quotes().get_data(as_text=True)
    assert "后台刷新中" in body
    assert "<details" in body
