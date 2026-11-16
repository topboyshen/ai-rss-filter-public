"""超长告警的降级顺序：先缩短正文，再考虑链接，绝不改写链接。

``_bounded_telegram_message`` 原先一进入降级就丢掉链接：标题稍长时，读者拿到
一条没有原文入口的告警。链接是这条消息唯一的来源入口，应当最后才让位；只有链接
本身超出单条消息预算时才省略，并且明确写出「已省略」，绝不截断成另一个地址。
"""

import json
import logging

import core.notifier as notifier

TOKEN = "123456:secret-bot-token"
CFG = {"telegram": {"enabled": True, "bot_token": TOKEN, "chat_id": "chat"}}
LINK = "https://example.test/a?x=1"


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


def _send(monkeypatch, *, title, link=LINK, tag="标签", logic="逻辑"):
    captured = _capture(monkeypatch)
    assert notifier.send_telegram_notification(
        title, link, 9.0, tag, logic, CFG, 9.0
    ) is True
    return captured["payload"]["text"]


def test_oversized_title_is_shortened_but_keeps_a_normal_link(monkeypatch):
    text = _send(monkeypatch, title="公" * 5000)

    assert len(text) <= 4096
    assert f'<a href="{LINK}">' in text
    assert "公" * 100 in text
    assert "公" * 5000 not in text


def test_oversized_tag_and_logic_are_shortened_before_the_link(monkeypatch):
    text = _send(monkeypatch, title="甲公司获得订单", tag="标" * 4000, logic="逻" * 4000)

    assert len(text) <= 4096
    assert f'<a href="{LINK}">' in text
    assert "标" * 4000 not in text
    assert "逻" * 4000 not in text


def test_link_over_budget_is_omitted_explicitly_and_never_truncated(monkeypatch):
    long_link = "https://example.test/a?x=1" + "&y=1" * 2000

    text = _send(monkeypatch, title="公" * 5000, link=long_link)

    assert len(text) <= 4096
    assert "<a href=" not in text
    # 省略要说出来，而不是悄悄丢掉唯一的原文入口。
    assert "原文链接超过单条消息上限，已省略" in text
    # 绝不能把长链接截成另一个仍然可点击的地址。
    assert long_link[:200] not in text
    assert "example.test/a?x=1" not in text


def test_oversized_alert_still_never_leaks_the_token(monkeypatch, caplog):
    def failing_urlopen(request, timeout=10):
        raise RuntimeError(f"cannot POST {request.full_url}")

    monkeypatch.setattr(notifier, "urlopen", failing_urlopen)

    with caplog.at_level(logging.WARNING):
        assert notifier.send_telegram_notification(
            "公" * 5000, LINK, 9.0, "标签", "逻辑", CFG, 9.0
        ) is False

    assert TOKEN not in caplog.text
    assert "RuntimeError" in caplog.text
