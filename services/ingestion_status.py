"""Read-only homepage coverage of the latest completed collection attempt.

Snapshot freshness and successful collection are different facts. Read the
latest attempt, not ``last_full_refresh`` which intentionally retains an older
success after a partial/failed attempt. No feed or model work runs here.
"""
from datetime import datetime
import json
import logging
import re
import time
from urllib.parse import unquote
from zoneinfo import ZoneInfo

logger = logging.getLogger(__name__)
_UNKNOWN = "最近采集：状态未知（最近一次结果不可用）；页面快照新鲜不代表采集完整。"
_PRIVATE_SOURCE = re.compile(
    r"(?:\b[a-z][a-z0-9+.-]*\s*:\s*[/\\]{2}|^\s*[/\\]{2}|"
    r"^\s*[a-z][a-z0-9+.-]*:\S|"
    r"\b(?:api[-_ ]?key|access[-_ ]?token|authorization|password|secret|token|key|"
    r"bearer|cookie|session(?:id)?|jwt)\s*[=:]|(?:密钥|密码|令牌)\s*[=:：]|"
    r"\bbearer\s+\S+|"
    r"(?:\b(?:[a-z0-9-]+\.)+[a-z]{2,63}|\b\d{1,3}(?:\.\d{1,3}){3})"
    r"(?::\d+)?[/?:#]|%[0-9a-f]{2}|[\x00-\x1f\x7f])",
    re.IGNORECASE,
)


def _public_source_name(value: str) -> str | None:
    """The writer may fall back to a private feed URL, not a publisher name."""
    raw = value.strip()
    # Check the full value before truncation so a trailing credential cannot
    # be hidden outside the display bound. Encoded URL/credential markers count.
    decoded = unquote(unquote(raw))
    if _PRIVATE_SOURCE.search(decoded):
        return None
    return raw[:200]


def _continuation_gaps(attempt: dict) -> tuple[int, int, int, int] | None:
    """Read only explicit batch gaps; historical terminal totals are not new failures."""
    continuation = attempt.get("score_continuations", {})
    if not isinstance(continuation, dict):
        return None
    remaining = continuation.get("remaining", {})
    if not isinstance(remaining, dict):
        return None
    values = (remaining.get("active", 0), continuation.get("deferred", 0),
              continuation.get("terminal_unscored", 0), continuation.get("errors", 0))
    return values if all(type(value) is int and value >= 0 for value in values) else None


def describe_refresh_coverage(attempt: object, *, now: float | None = None) -> tuple[str, str]:
    """Validate persisted counters before making a completeness claim."""
    if not isinstance(attempt, dict):
        return "unknown", _UNKNOWN
    fields = ("enabled_sources", "succeeded_sources", "failed_sources", "started_at", "finished_at")
    if any(type(attempt.get(field)) is not int for field in fields):
        return "unknown", _UNKNOWN
    total, succeeded, failed, started, finished = (attempt[field] for field in fields)
    extra = tuple(attempt.get(field, 0) for field in (
        "partial_sources", "source_unrecognized_entries", "score_deferred_entries",
        "score_terminal_unscored_entries",
    ))
    if any(type(value) is not int or value < 0 for value in extra):
        return "unknown", _UNKNOWN
    partial, unrecognized, deferred, terminal = extra
    continuation = _continuation_gaps(attempt)
    if continuation is None:
        return "unknown", _UNKNOWN
    active, continued_deferred, continued_terminal, continued_errors = continuation
    continuation_gap = sum(continuation)
    status = attempt.get("status")
    clock = time.time() if now is None else now
    if (
        min(total, succeeded, failed) < 0 or total != succeeded + partial + failed
        or started <= 0 or started > finished or finished > clock
        or type(attempt.get("version")) is not int or attempt["version"] != 1
        or not isinstance(status, str) or status not in {"success", "partial", "failed", "noop"}
        or (status == "success" and (total == 0 or (
            failed + partial + unrecognized + deferred + terminal + continuation_gap != 0
        )))
        or (status == "partial" and (succeeded + partial == 0 or failed + partial + continuation_gap == 0))
        or (status == "failed" and (succeeded + partial != 0 or failed == 0))
        or (status == "noop" and total != 0)
    ):
        return "unknown", _UNKNOWN
    try:
        stamp = datetime.fromtimestamp(finished, ZoneInfo("Asia/Shanghai")).strftime("%m-%d %H:%M")
    except (ValueError, OSError, OverflowError):
        return "unknown", _UNKNOWN
    suffix = f"（{stamp}，北京时间；完成时刻）"
    if status == "noop":
        return "noop", f"最近采集：0/0 {suffix}；未启用订阅源，不代表采集完成。"
    text = f"最近采集：{succeeded}/{total} 个订阅源完整完成{suffix}"
    if status == "success":
        return "complete", text + "；采集完整度与页面快照新鲜度分别统计。"
    if partial:
        text += (f"；部分完成 {partial} 源（未识别内容 {unrecognized} 条、待评分 {deferred} 条"
                 f"、终止未评分 {terminal} 条）"
                 f"，抓取／处理失败 {failed} 源")
    if continuation_gap:
        text += (f"；续作批次：待续作 {active} 条，本批递延 {continued_deferred} 条"
                 f"、终止未评分 {continued_terminal} 条、执行异常 {continued_errors} 条")
    failures = attempt.get("failures")
    names = []
    private_names = False
    if isinstance(failures, list):
        for item in failures[:50]:
            if isinstance(item, dict) and isinstance(item.get("source"), str):
                name = _public_source_name(item["source"])
                if name is None:
                    private_names = True
                if name and name not in names:
                    names.append(name)
    # Never expose upstream errors or the writer's feed-URL name fallback.
    if private_names:
        detail = "、".join(names) + "（另有来源名不可公开）" if names else "来源名不可公开，详情暂不可用"
    else:
        detail = "、".join(names) if names else "未记录，详情暂不可用"
    return "incomplete", text + f"；异常源：{detail}。页面快照新鲜不代表采集完整。"


def read_refresh_coverage() -> tuple[str, str]:
    """One bounded read per worker/live build; file-only Web never calls this."""
    from db.database import get_read_only_connection

    try:
        with get_read_only_connection() as conn:
            conn.execute("PRAGMA busy_timeout=100")
            row = conn.execute(
                "SELECT substr(v, 1, 64001) FROM app_meta WHERE k = ?", ("last_refresh_result",)
            ).fetchone()
        raw = row[0] if row else None
        if not isinstance(raw, str) or len(raw) > 64_000:
            return "unknown", _UNKNOWN
        return describe_refresh_coverage(json.loads(raw))
    except Exception:
        logger.warning("Could not read latest collection coverage", exc_info=True)
        return "unknown", _UNKNOWN
