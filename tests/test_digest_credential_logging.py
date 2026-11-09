"""简报推送失败时不能把凭证写进日志。

``_send_telegram_html_blocks`` 把 bot token 拼进请求 URL。异常原文经常带着这个
URL（例如 ``unknown url type: https://api.telegram.org/bot<token>/sendMessage``），
直接 ``logger.warning("... %s", exc)`` 等于把凭证落到日志里。失败日志只保留安全
分类：异常类名与 HTTP 状态码；完整 URL、请求正文、响应正文一律不记。
"""

import logging
from urllib.error import HTTPError, URLError


from services import digest as digest_service

TOKEN = "123456789:FAKE-DIGEST-TOKEN-must-not-be-logged"
CHAT_ID = "-100200300"
CFG = {"telegram": {"enabled": True, "bot_token": TOKEN, "chat_id": CHAT_ID}}


class FakeResp:
    def __init__(self, status):
        self.status = status

    def __enter__(self):
        return self

    def __exit__(self, *args):
        return False


def _assert_no_credentials(caplog):
    for record in caplog.records:
        text = record.getMessage()
        assert TOKEN not in text
        assert "api.telegram.org/bot" not in text
        assert CHAT_ID not in text


def test_url_bearing_exception_is_logged_without_the_credential(monkeypatch, caplog):
    def fake_urlopen(req, timeout=10):
        # 与 urllib 的真实行为一致：异常文本携带完整请求 URL。
        raise ValueError(f"unknown url type: {req.full_url}")

    monkeypatch.setattr(digest_service, "urlopen", fake_urlopen)

    with caplog.at_level(logging.WARNING):
        assert digest_service._send_telegram_html_blocks(["<b>摘要</b>"], CFG) is False

    _assert_no_credentials(caplog)
    messages = [item.getMessage() for item in caplog.records]
    assert any("ValueError" in message for message in messages)


def test_http_error_is_logged_by_class_and_status_only(monkeypatch, caplog):
    def fake_urlopen(req, timeout=10):
        raise HTTPError(req.full_url, 400, "Bad Request", {}, None)

    monkeypatch.setattr(digest_service, "urlopen", fake_urlopen)

    with caplog.at_level(logging.WARNING):
        assert digest_service._send_telegram_html_blocks(["<b>摘要</b>"], CFG) is False

    _assert_no_credentials(caplog)
    messages = [item.getMessage() for item in caplog.records]
    assert any("HTTPError" in message and "400" in message for message in messages)


def test_network_error_is_logged_by_class_without_its_text(monkeypatch, caplog):
    def fake_urlopen(req, timeout=10):
        raise URLError(f"dial tcp {req.full_url}: connection refused")

    monkeypatch.setattr(digest_service, "urlopen", fake_urlopen)

    with caplog.at_level(logging.WARNING):
        assert digest_service._send_telegram_html_blocks(["<b>摘要</b>"], CFG) is False

    _assert_no_credentials(caplog)
    messages = [item.getMessage() for item in caplog.records]
    assert any("URLError" in message for message in messages)
    assert not any("connection refused" in message for message in messages)


def test_successful_send_logs_nothing(monkeypatch, caplog):
    sent = []

    def fake_urlopen(req, timeout=10):
        sent.append(req.full_url)
        return FakeResp(200)

    monkeypatch.setattr(digest_service, "urlopen", fake_urlopen)

    with caplog.at_level(logging.DEBUG):
        assert digest_service._send_telegram_html_blocks(["<b>摘要</b>"], CFG) is True

    assert len(sent) == 1
    assert caplog.records == []


def test_non_200_response_returns_false_without_logging_the_url(monkeypatch, caplog):
    def fake_urlopen(req, timeout=10):
        return FakeResp(500)

    monkeypatch.setattr(digest_service, "urlopen", fake_urlopen)

    with caplog.at_level(logging.DEBUG):
        assert digest_service._send_telegram_html_blocks(["<b>摘要</b>"], CFG) is False

    assert caplog.records == []
