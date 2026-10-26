"""AI-Filter Briefing Telegram delivery."""

import json
import os
import logging
from urllib.request import Request, urlopen

from services.quant_market_context import quant_market_context_text
from utils.helpers import direction_emoji, escape_html, normalize_direction, safe_http_url

logger = logging.getLogger(__name__)

_TELEGRAM_HTML_LIMIT = 3900


# ---------- Telegram HTML delivery ----------


def _bounded_html_text(value, max_visible: int) -> str:
    """Escape a bounded amount of visible text before adding HTML markup."""
    return escape_html(str(value or "")[:max_visible])


def _chunk_html_blocks(blocks, limit: int = _TELEGRAM_HTML_LIMIT) -> list[str]:
    """Pack complete HTML blocks without cutting tags or entities."""
    chunks: list[str] = []
    current = ""
    for raw_block in blocks:
        block = str(raw_block or "").strip()
        if not block:
            continue
        if len(block) > limit:
            raise ValueError("Telegram HTML block exceeds safe message limit")
        candidate = block if not current else f"{current}\n\n{block}"
        if len(candidate) <= limit:
            current = candidate
            continue
        chunks.append(current)
        current = block
    if current:
        chunks.append(current)
    return chunks


def _send_telegram_html_blocks(
    blocks,
    cfg: dict,
) -> bool:
    telegram = cfg.get("telegram", {}) or {}
    if not telegram.get("enabled", False):
        return False
    token = telegram.get("bot_token", "") or os.environ.get("TELEGRAM_BOT_TOKEN", "")
    chat_id = telegram.get("chat_id", "") or os.environ.get("TELEGRAM_CHAT_ID", "")
    if not token or not chat_id:
        return False
    try:
        chunks = _chunk_html_blocks(blocks)
        if not chunks:
            return False
        url = f"https://api.telegram.org/bot{token}/sendMessage"
        for message in chunks:
            payload = json.dumps({
                "chat_id": chat_id,
                "text": message,
                "parse_mode": "HTML",
                "disable_web_page_preview": True,
            }, ensure_ascii=False).encode("utf-8")
            req = Request(url, data=payload, headers={"Content-Type": "application/json"}, method="POST")
            with urlopen(req, timeout=10) as resp:
                if resp.status != 200:
                    return False
        return True
    except Exception as exc:
        logger.warning("[Digest] Telegram push failed: %s", exc)
        return False


# ---------- Digest Telegram push ----------

def _push_digest_to_telegram(data: dict, cfg: dict) -> bool:
    """推送主题聚合情报简报到 Telegram"""
    if not cfg.get("telegram", {}).get("enabled", False):
        return False

    token = cfg["telegram"].get("bot_token", "") or os.environ.get("TELEGRAM_BOT_TOKEN", "")
    chat_id = cfg["telegram"].get("chat_id", "") or os.environ.get("TELEGRAM_CHAT_ID", "")
    if not token or not chat_id:
        return False

    themes = data.get("themes", [])
    count = data.get("article_count", 0)
    period = data.get("period_hours", 24)
    overall = data.get("overall_assessment", "")
    risk = data.get("risk_alert", "")
    confidence = data.get("confidence", 0)

    blocks = [
        f"📢 <b>A股情报简报（近{_bounded_html_text(period, 12)}小时）</b>",
        f"📊 整体：{_bounded_html_text(overall, 280)}\n"
        f"📈 高分文章：{_bounded_html_text(count, 12)} 篇 | "
        f"置信度：{_bounded_html_text(f'{confidence * 100:.0f}', 8)}%",
    ]
    assembly_audit = data.get("briefing_assembly")
    if isinstance(assembly_audit, dict):
        enhanced_briefing = bool(assembly_audit.get("enabled"))
    else:
        assembly_cfg = cfg.get("briefing_assembly") or {}
        enhanced_briefing = bool(
            isinstance(assembly_cfg, dict) and assembly_cfg.get("enabled")
        )
    for i, theme in enumerate(themes[:4], 1):
        direction = normalize_direction(theme.get("direction", ""))
        if not enhanced_briefing:
            blocks.append(
                f"<b>{i}. {direction_emoji(direction)} {_bounded_html_text(theme.get('theme', ''), 64)}</b>\n"
                f"📝 {_bounded_html_text(theme.get('insight', ''), 320)}\n"
                f"🎯 {_bounded_html_text(theme.get('action', ''), 240)}"
            )
            continue
        theme_lines = [
            f"<b>{i}. {direction_emoji(direction)} {_bounded_html_text(theme.get('theme', ''), 64)}</b>"
        ]
        insight = str(theme.get("insight") or "").strip()
        why_now = str(theme.get("why_it_matters") or "").strip()
        action = str(theme.get("action") or "").strip()
        if insight:
            theme_lines.append(f"📝 {_bounded_html_text(insight, 320)}")
        if why_now and why_now != insight:
            theme_lines.append(f"⏱ {_bounded_html_text(why_now, 280)}")
        if action:
            theme_lines.append(f"🎯 {_bounded_html_text(action, 240)}")
        if not insight and not why_now and not action:
            theme_lines.append("🧾 评论证据不足，已隐藏")
        blocks.append("\n".join(theme_lines))
    if risk:
        blocks.append(f"⚠️ <b>风险提示</b>：{_bounded_html_text(risk, 280)}")
    if "quant_context" in data:
        blocks.append(
            f"🧭 {_bounded_html_text(quant_market_context_text(data.get('quant_context')), 360)}"
        )

    dash = os.environ.get("PUBLIC_BASE_URL", "").strip() or "http://127.0.0.1:9998"
    dash = safe_http_url(dash.rstrip("/"), fallback="http://127.0.0.1:9998")
    if len(dash) > 512:
        dash = "http://127.0.0.1:9998"
    blocks.append(f'📈 <a href="{escape_html(dash)}">情报总控台</a>')
    return _send_telegram_html_blocks(blocks, cfg)
