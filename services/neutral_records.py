"""Presentation for recorded neutral decisions, not a second quality verdict."""

from datetime import datetime
from zoneinfo import ZoneInfo

from db.database import canonical_feed_url
from utils.helpers import format_article_display_title, safe_http_url


NEUTRAL_RECORDS_PATH = "/dashboard/neutral-records"
# Match the existing event-radar links: widgets are embedded on the Glance
# origin, while browser login and authenticated pages live on this origin.
NEUTRAL_RECORDS_URL = "https://ai.example.invalid" + NEUTRAL_RECORDS_PATH
NEUTRAL_REASON_TEXT = {
    "theme": "保守处理说明：主题关联未通过现有来源校验",
    "facts": "保守处理说明：事实表述未通过现有来源校验",
    "contract": "保守处理说明：输出格式或字段未通过校验",
    "initial": "保守处理说明：初次输出为保守中性，具体原因未记录",
    "": "保守处理说明：具体原因未完整记录",
}


def render_neutral_records_entry() -> str:
    """Static public link only; no private query, JS, credential or record cache."""
    return (
        '<details class="neutral-records-entry" style="margin:12px 0;font-size:13px;line-height:1.7;">'
        '<summary style="cursor:pointer;font-weight:650;">保守处理记录</summary>'
        '<p>查看当前评分契约下，近24小时收录的保守中性记录及已记录原因。'
        '这是处理过程说明，不是新闻真实性或价值结论。</p>'
        f'<a href="{NEUTRAL_RECORDS_URL}" target="_blank" rel="noopener noreferrer">'
        '查看记录（需后台登录）</a></details>'
    )


def _ingestion_time(value) -> str:
    try:
        timestamp = int(value)
        if timestamp <= 0:
            return "收录时间未记录"
        label = datetime.fromtimestamp(timestamp, ZoneInfo("Asia/Shanghai"))
        return "收录 " + label.strftime("%Y-%m-%d %H:%M") + "（北京时间）"
    except (TypeError, ValueError, OSError, OverflowError):
        return "收录时间未记录"


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
            "ingested_at": _ingestion_time(row["created_at"]),
            "explanation": NEUTRAL_REASON_TEXT.get(row["safe_neutral_reason"], NEUTRAL_REASON_TEXT[""]),
        }
        for row in records
    ]
