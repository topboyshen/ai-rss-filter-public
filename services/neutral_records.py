"""Presentation for recorded neutral decisions, not a second quality verdict."""

from datetime import datetime
from html import escape
from zoneinfo import ZoneInfo

from db.database import canonical_feed_url
from utils.helpers import format_article_display_title, safe_http_url
from services.navigation_origins import dashboard_origin


NEUTRAL_RECORDS_PATH = "/dashboard/neutral-records"
NEUTRAL_REASON_TEXT = {
    "theme": "保守处理说明：主题关联未通过现有来源校验",
    "facts": "保守处理说明：事实表述未通过现有来源校验",
    "contract": "保守处理说明：输出格式或字段未通过校验",
    "initial": "保守处理说明：初次输出为保守中性，具体原因未记录",
    "insufficient": "信息不足：模型判断标题和摘要不足以识别主体或事件，未执行事实校验",
    "": "保守处理说明：具体原因未完整记录",
}


def render_neutral_records_entry() -> str:
    """Static public link only; no private query, JS, credential or record cache."""
    origin = dashboard_origin()
    entry = (
        f'<a href="{escape(origin + NEUTRAL_RECORDS_PATH, quote=True)}" target="_blank" rel="noopener noreferrer">'
        '查看记录（需后台登录）</a>'
        if origin else '<span>后台入口未配置</span>'
    )
    return (
        '<details class="neutral-records-entry" style="margin:12px 0;font-size:13px;line-height:1.7;">'
        '<summary style="cursor:pointer;font-weight:650;">保守处理记录</summary>'
        '<p>按缓存条目时间查看当前评分契约近24小时的保守中性和信息不足记录及已记录原因。'
        '这是处理过程说明，不是新闻真实性或价值结论。</p>'
        f'{entry}</details>'
    )


def _cache_entry_time(value) -> str:
    try:
        timestamp = int(value)
        if timestamp <= 0:
            return "缓存条目时间未记录"
        label = datetime.fromtimestamp(timestamp, ZoneInfo("Asia/Shanghai"))
        return "缓存条目时间 " + label.strftime("%Y-%m-%d %H:%M") + "（北京时间）"
    except (TypeError, ValueError, OSError, OverflowError):
        return "缓存条目时间未记录"


def present_neutral_records(records: list[dict], cfg: dict) -> list[dict]:
    """Show configured primary feed names, never infer a publisher from a URL."""
    sources = {
        canonical_feed_url(cfg, feed.get("url", "")): str(feed.get("name") or "").strip()
        for feed in (cfg.get("feeds") or []) if isinstance(feed, dict) and feed.get("url")
    }
    return [
        {
            "title": format_article_display_title(row["title"], row["raw_title"]) or "标题未记录",
            "link": safe_http_url(row["link"], fallback=""),
            "source": sources.get(canonical_feed_url(cfg, row["feed_url"])) or "订阅来源未记录或配置已变更",
            "cache_entry_time": _cache_entry_time(row["created_at"]),
            "explanation": NEUTRAL_REASON_TEXT.get(row["safe_neutral_reason"], NEUTRAL_REASON_TEXT[""]),
        }
        for row in records
    ]
