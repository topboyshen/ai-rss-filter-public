import json

from services import digest as digest_service


def test_digest_html_is_split_only_between_complete_blocks(monkeypatch):
    payloads = []

    class FakeResp:
        status = 200

        def __enter__(self):
            return self

        def __exit__(self, *args):
            return False

    def fake_urlopen(req, timeout=10):
        payloads.append(json.loads(req.data.decode("utf-8")))
        return FakeResp()

    monkeypatch.setattr(digest_service, "urlopen", fake_urlopen)
    cfg = {"telegram": {"enabled": True, "bot_token": "token", "chat_id": "chat"}}
    noisy = "<&>" * 300
    data = {
        "period_hours": 24,
        "article_count": 4,
        "confidence": 0.8,
        "overall_assessment": noisy,
        "risk_alert": noisy,
        "themes": [
            {"direction": "看空", "theme": noisy, "insight": noisy, "action": noisy}
            for _ in range(4)
        ],
    }

    assert digest_service._push_digest_to_telegram(data, cfg)
    assert len(payloads) > 1
    for payload in payloads:
        text = payload["text"]
        assert len(text) <= digest_service._TELEGRAM_HTML_LIMIT
        assert text.count("<b>") == text.count("</b>")
        assert "<&>" not in text
        assert not text.endswith(("&", "&a", "&am", "&amp"))


def test_briefing_push_hides_empty_ungrounded_commentary(monkeypatch):
    payloads = []

    class FakeResp:
        status = 200

        def __enter__(self):
            return self

        def __exit__(self, *args):
            return False

    def fake_urlopen(req, timeout=10):
        payloads.append(json.loads(req.data.decode("utf-8")))
        return FakeResp()

    monkeypatch.setattr(digest_service, "urlopen", fake_urlopen)
    cfg = {
        "telegram": {"enabled": True, "bot_token": "token", "chat_id": "chat"},
        "briefing_assembly": {"enabled": True},
    }
    data = {
        "period_hours": 24,
        "article_count": 1,
        "confidence": 0.5,
        "overall_assessment": "等待证据",
        "themes": [{
            "direction": "中性",
            "theme": "AI 算力链",
            "insight": "",
            "action": "",
            "grounding": {"status": "fallback"},
        }],
    }

    assert digest_service._push_digest_to_telegram(data, cfg)
    message = "\n".join(payload["text"] for payload in payloads)
    assert "评论证据不足，已隐藏" in message
    assert "📝 \n" not in message
    assert "🎯 \n" not in message


def test_manual_digest_route_returns_bad_gateway_on_push_failure(monkeypatch):
    import app as app_module
    import routes.dashboard_routes as dashboard

    row = (
        "https://example.com/a", "[⭐9 📉 风险] A", "标题A", 9.0,
        "风险", "风险上升", "摘要", 100, "feed", "看空",
    )
    monkeypatch.setattr(dashboard, "check_api_key", lambda: True)
    monkeypatch.setattr(dashboard, "_get_briefing_articles", lambda **_kwargs: [row])
    monkeypatch.setattr(dashboard, "_push_digest_to_telegram", lambda *_args: False)

    with app_module.app.test_client() as client:
        response = client.post("/briefing?push=true")

    assert response.status_code == 502
    assert response.get_json()["error"] == "telegram_push_failed"
