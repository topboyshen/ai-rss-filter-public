"""Display fields for the rejection review page; no verdict is computed here."""

from datetime import datetime
from urllib.parse import quote
from zoneinfo import ZoneInfo

from db.database import canonical_feed_url
from db.rejection_review import PATH_FILTER_NONE
from utils.helpers import format_article_display_title, safe_http_url


REJECTIONS_PATH = "/dashboard/rejections"
REASON_TEXT = {
    "boundary_score": "分数位于阈值边界，标记为复核",
    "fast_pass_hint": "规则提示可快速放行，但仍被模型判为低分",
    "similar_higher_score": "同一事件已有更高分记录",
    "high_value_source": "来源属高价值源，低分被标记复核",
}
PATH_TEXT = {
    "llm": "模型评分",
    "stage2": "二阶段评分",
    "similarity": "相似去重",
    "insufficient_input": "信息不足",
    "local_reject": "本地规则拒绝",
    "market_flash_rule": "行情快讯规则",
}
DECISION_TEXT = {
    "restored": "已恢复（人工放行）",
    "confirmed": "已确认拒绝",
}
DECIDED_TEXT = {
    "restore": "已记录：恢复（人工放行）",
    "confirm": "已记录：确认拒绝",
    "revoke": "已撤销上一次判断",
}


def _ingestion_time(value) -> str:
    try:
        timestamp = int(value)
        if timestamp <= 0:
            return "收录时间未记录"
        label = datetime.fromtimestamp(timestamp, ZoneInfo("Asia/Shanghai"))
        return "收录 " + label.strftime("%Y-%m-%d %H:%M") + "（北京时间）"
    except (TypeError, ValueError, OSError, OverflowError):
        return "收录时间未记录"


def _score(value: float) -> str:
    return f"{value:g}"


def _path_label(code: str) -> str:
    if code in PATH_TEXT:
        return PATH_TEXT[code]
    if "safe_neutral" in code:
        return "保守中性（未判定）"
    return code or "评分路径未记录"


def _query(
    path_filter: str,
    reason_filter: str,
    page: int = 1,
    decision: str = "",
) -> str:
    parts = []
    if path_filter:
        parts.append("path=" + quote(path_filter, safe=""))
    if reason_filter:
        parts.append("reason=" + quote(reason_filter, safe=""))
    if decision:
        parts.append("decision=" + quote(decision, safe=""))
    if page > 1:
        parts.append("page=" + str(page))
    return REJECTIONS_PATH + ("?" + "&".join(parts) if parts else "")


def present_rejection_page(snapshot: dict, cfg: dict) -> dict:
    if not snapshot.get("available"):
        return {"records": [], "summary": None, "decided_text": ""}

    sources = {
        canonical_feed_url(cfg, feed.get("url", "")): str(
            feed.get("name") or ""
        ).strip()
        for feed in (cfg.get("feeds") or [])
        if isinstance(feed, dict) and feed.get("url")
    }
    path_filter = snapshot["filters"]["path"]
    reason_filter = snapshot["filters"]["reason"]
    decision_filter = snapshot["filters"]["decision"]

    items = []
    for row in snapshot["items"]:
        decision = row["decision"]
        items.append(
            {
                "title": format_article_display_title(
                    row["title"], row["raw_title"]
                )
                or "标题未记录",
                "link": safe_http_url(row["link"], fallback=""),
                "source": sources.get(
                    canonical_feed_url(cfg, row["feed_url"])
                )
                or "订阅来源未记录或配置已变更",
                "ingested_at": _ingestion_time(row["created_at"]),
                "score_text": _score(row["score"]),
                "threshold_text": _score(row["threshold"]),
                "score_path": row["score_path"] or "评分路径未记录",
                "path_label": _path_label(row["score_path"]),
                "reasons": [
                    {"code": code, "text": REASON_TEXT.get(code, code)}
                    for code in row["reason_codes"]
                ],
                "logic": row["logic"],
                "decision": decision,
                "decision_label": DECISION_TEXT.get(decision, ""),
                "decision_note": row["decision_note"],
            }
        )

    summary = snapshot["summary"]
    by_path = [
        {
            "code": code,
            "label": _path_label(code),
            "count": count,
            "href": _query(
                code or PATH_FILTER_NONE, reason_filter, 1, decision_filter
            ),
            "active": (code or PATH_FILTER_NONE) == path_filter,
        }
        for code, count in summary["by_path"]
    ]
    by_reason = [
        {
            "code": code,
            "label": REASON_TEXT.get(code, code),
            "count": count,
            "href": _query(path_filter, code, 1, decision_filter),
            "active": code == reason_filter,
        }
        for code, count in summary["by_reason"]
    ]

    return {
        "records": items,
        "summary": {
            "rejected": summary["rejected"],
            "with_rule_reason": summary["with_rule_reason"],
            "restored": summary["restored"],
            "confirmed": summary["confirmed"],
            "by_path": by_path,
            "by_reason": by_reason,
        },
        "all_href": _query("", ""),
        "rule_href": _query(path_filter, "rule", 1, decision_filter),
        "none_href": _query(path_filter, "none", 1, decision_filter),
        "restored_href": _query(
            path_filter, reason_filter, 1, "restored"
        ),
        "confirmed_href": _query(
            path_filter, reason_filter, 1, "confirmed"
        ),
        "all_active": not path_filter and not reason_filter and not decision_filter,
        "rule_active": reason_filter == "rule",
        "none_active": reason_filter == "none",
        "restored_active": decision_filter == "restored",
        "confirmed_active": decision_filter == "confirmed",
        "prev_href": _query(
            path_filter, reason_filter, snapshot["page"] - 1, decision_filter
        ),
        "next_href": _query(
            path_filter, reason_filter, snapshot["page"] + 1, decision_filter
        ),
        "decided_text": "",
    }


def decided_text(value: str) -> str:
    return DECIDED_TEXT.get(str(value or ""), "")
