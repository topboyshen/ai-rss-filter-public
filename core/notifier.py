import os
import json
from urllib.request import Request, urlopen

from utils.helpers import escape_html, safe_http_url

# Telegram rejects messages above 4096 characters, so keep headroom for entity
# parsing instead of losing an oversized alert inside the failure branch.
_MAX_MESSAGE_CHARS = 3900
_FALLBACK_TITLE_CHARS = 300
_FALLBACK_TAG_CHARS = 120
_FALLBACK_LOGIC_CHARS = 300
_LAST_RESORT_TITLE_CHARS = 120
_LAST_RESORT_TAG_CHARS = 60
_LAST_RESORT_LOGIC_CHARS = 200
_LINK_OMITTED = "\n\U0001f517 原文链接超过单条消息上限，已省略"


def _telegram_message(title, link, score, tag, logic):
    msg = (
        "\U0001f6a8 <b>超重磅 A 股情报</b>\n\n"
        f"\U0001f4ca 评分：<b>{escape_html(score)}分</b>\n"
        f"\U0001f3f7 标签：{escape_html(tag)}\n"
        f"\U0001f4a1 逻辑：{escape_html(logic)}\n\n"
        f"\U0001f4c4 标题：{escape_html(title)}"
    )
    if link:
        msg += f'\n\U0001f517 链接：<a href="{escape_html(link)}">打开原文</a>'
    return msg


def _clip(value, limit):
    return str(value or "")[:limit]


def _bounded_telegram_message(title, link, score, tag, logic):
    """Degrade an oversized alert instead of letting the provider reject it whole.

    The source link is the only way back to the article, so it survives the
    first two degradations: title, tag and explanation are shortened before the
    link is considered.  A link that still cannot fit the smallest message is
    omitted explicitly; it is never truncated, which would forge a different
    address.  tag/logic are contract-bounded upstream; shortening them only
    protects callers outside that contract.
    """
    msg = _telegram_message(title, link, score, tag, logic)
    if len(msg) <= _MAX_MESSAGE_CHARS:
        return msg
    msg = _telegram_message(
        _clip(title, _FALLBACK_TITLE_CHARS),
        link,
        score,
        _clip(tag, _FALLBACK_TAG_CHARS),
        _clip(logic, _FALLBACK_LOGIC_CHARS),
    )
    if len(msg) <= _MAX_MESSAGE_CHARS:
        return msg
    msg = _telegram_message(
        _clip(title, _LAST_RESORT_TITLE_CHARS),
        link,
        score,
        _clip(tag, _LAST_RESORT_TAG_CHARS),
        _clip(logic, _LAST_RESORT_LOGIC_CHARS),
    )
    if len(msg) <= _MAX_MESSAGE_CHARS:
        return msg
    return _telegram_message(
        _clip(title, _LAST_RESORT_TITLE_CHARS),
        "",
        score,
        _clip(tag, _LAST_RESORT_TAG_CHARS),
        _clip(logic, _LAST_RESORT_LOGIC_CHARS),
    ) + _LINK_OMITTED


def send_telegram_notification(title, link, score, tag, logic, cfg, notify_threshold=None):
    if not cfg.get("telegram", {}).get("enabled"):
        return False
    if notify_threshold is not None:
        threshold = notify_threshold
    else:
        from db.database import get_feed_thresholds
        threshold = get_feed_thresholds(cfg, "")["notify"]
    if score < threshold:
        return False
    token = cfg["telegram"]["bot_token"] or os.environ.get("TELEGRAM_BOT_TOKEN", "")
    chat_id = cfg["telegram"]["chat_id"] or os.environ.get("TELEGRAM_CHAT_ID", "")
    if not token or not chat_id:
        return False
    try:
        safe_link = safe_http_url(link, fallback="")
        msg = _bounded_telegram_message(title, safe_link, score, tag, logic)
        url = f"https://api.telegram.org/bot{token}/sendMessage"
        payload = json.dumps({
            "chat_id": chat_id,
            "text": msg,
            "parse_mode": "HTML",
            "disable_web_page_preview": False
        }, ensure_ascii=False).encode("utf-8")
        req = Request(url, data=payload, headers={"Content-Type": "application/json"}, method="POST")
        with urlopen(req, timeout=10) as resp:
            return resp.status == 200
    except Exception as exc:
        import logging
        # Never interpolate the exception text: provider errors can embed the
        # request URL, which carries the bot token.
        logging.warning(
            "[WARN] Telegram 推送失败 type=%s score=%s title=%s",
            type(exc).__name__,
            score,
            str(title or "")[:40],
        )
        return False
