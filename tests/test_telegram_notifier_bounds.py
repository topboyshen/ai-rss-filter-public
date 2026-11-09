"""Telegram alerts must stay inside the provider limit and never leak the token.

Telegram rejects messages above 4096 characters outright, and the polling path
does not cap feed titles.  An oversized message must degrade (bounded title,
optional link dropped) instead of disappearing inside the warning branch.
"""

import http.client
import json
import logging

import core.notifier as notifier

TOKEN = "123456:secret-bot-token"
CFG = {"telegram": {"enabled": True, "bot_token": TOKEN, "chat_id": "chat"}}


class _FakeResponse:
    status = 200

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return False


def _capture(monkeypatch):
    captured = {}

    def fake_urlopen(request, timeout=10):
        captured["payload"] = json.loads(request.data.decode("utf-8"))
        return _FakeResponse()

    monkeypatch.setattr(notifier, "urlopen", fake_urlopen)
    return captured


def test_oversized_alert_degrades_instead_of_exceeding_the_provider_limit(monkeypatch):
    captured = _capture(monkeypatch)
    long_link = "https://example.test/a?x=1" + "&y=1" * 2000
    title = "公" * 5000

    assert notifier.send_telegram_notification(
        title, long_link, 9.0, "标签", "逻辑", CFG, 9.0
    ) is True

    text = captured["payload"]["text"]
    assert len(text) <= 4096
    assert "<a href=" not in text
    assert "标题：" in text
    assert "公" * 100 in text


def test_normal_alert_keeps_the_whole_title_and_link(monkeypatch):
    captured = _capture(monkeypatch)
    title = "甲公司获得三亿元订单"

    assert notifier.send_telegram_notification(
        title, "https://example.test/a", 9.0, "公司订单", "订单落地", CFG, 9.0
    ) is True

    text = captured["payload"]["text"]
    assert title in text
    assert '<a href="https://example.test/a">' in text


def test_failure_log_never_repeats_the_exception_text(monkeypatch, caplog):
    def failing_urlopen(request, timeout=10):
        raise http.client.InvalidURL(
            f"URL can't contain control characters. {request.full_url!r}"
        )

    monkeypatch.setattr(notifier, "urlopen", failing_urlopen)

    with caplog.at_level(logging.WARNING):
        assert notifier.send_telegram_notification(
            "标题", "https://example.test/a", 9.0, "标签", "逻辑", CFG, 9.0
        ) is False

    assert TOKEN not in caplog.text
    assert "InvalidURL" in caplog.text
