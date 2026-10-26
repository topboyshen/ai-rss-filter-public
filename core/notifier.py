import os
import json
from urllib.request import Request, urlopen

from utils.helpers import escape_html, safe_http_url


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
        msg = (
            "\U0001f6a8 <b>\u8d85\u91cd\u78c5 A \u80a1\u60c5\u62a5</b>\n\n"
            f"\U0001f4ca \u8bc4\u5206\uff1a<b>{escape_html(score)}\u5206</b>\n"
            f"\U0001f3f7 \u6807\u7b7e\uff1a{escape_html(tag)}\n"
            f"\U0001f4a1 \u903b\u8f91\uff1a{escape_html(logic)}\n\n"
            f"\U0001f4c4 \u6807\u9898\uff1a{escape_html(title)}"
        )
        safe_link = safe_http_url(link, fallback="")
        if safe_link:
            msg += f'\n\U0001f517 \u94fe\u63a5\uff1a<a href="{escape_html(safe_link)}">\u6253\u5f00\u539f\u6587</a>'
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
    except Exception as e:
        import logging
        logging.warning(f"[WARN] Telegram \u63a8\u9001\u5931\u8d25: {e}")
        return False
