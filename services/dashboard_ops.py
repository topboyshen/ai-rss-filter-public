"""Bounded, read-only presentation of existing ops evidence; never run probes."""

from __future__ import annotations

import json
import math
import os
import re
import stat
import time
from datetime import date, datetime
from pathlib import Path
from zoneinfo import ZoneInfo

from services.market_close_result import VALID_STATUSES, result_dir_from_env
from services.release_info import release_version
from services.worker_health import QUEUE_MAX_AGE_SECONDS, _spool_dir
from utils.helpers import feed_data_health

MAX_JSON_BYTES = 65536
MAX_QUEUE_ENTRIES = 512
QUEUE_SCAN_SECONDS = 0.05
CST = ZoneInfo("Asia/Shanghai")
_TASK = re.compile(r"^[0-9a-f]{64}\.json$")
_DEAD = re.compile(r"^[0-9a-f]{64}\.[0-9]+\.[0-9]+\.json$")
_KINDS = (("collector", "收盘采集"), ("repair", "待结算修复"), ("history", "历史行情"))
HOST_SNAPSHOT_MAX_AGE = 90 * 60
_HOST_CHECKS = {
    "release_containers": "发布与容器",
    "metrics": "应用指标",
    "notification_transport": "通知通道",
    "event_enrichment": "事件丰富化",
    "market_timer": "收盘任务",
    "market_history": "历史行情任务",
    "root_disk": "根分区空间",
    "backup": "备份",
    "restore_drill": "恢复演练",
    "offsite_upload": "异地上传",
}
_HOST_STATES = {
    "PASS": ("ok", "检查通过"),
    "WARN": ("warn", "检查有警告"),
    "FAIL": ("warn", "检查失败"),
    "SKIPPED_BY_POLICY": ("info", "按策略未执行"),
}


def read_host_ops_snapshot(path, expected_release, now=None) -> dict:
    """Display the existing hourly checker, never execute it from the page."""
    unknown = {
        **_state(
            "unknown",
            "宿主巡检未知",
            "快照未接入、不可读、无效、过期或不属于当前版本。",
        ),
        "checked_at": "未记录",
        "checks": [],
    }
    try:
        payload = read_small_json(Path(path))
        current = time.time() if now is None else now
        if set(payload) != {
            "version",
            "producer",
            "started_at",
            "completed_at",
            "release_ref",
            "checks",
        }:
            return unknown
        if (
            type(payload.get("version")) is not int
            or payload["version"] != 1
            or payload.get("producer") != "ai-filter-ops-check"
        ):
            return unknown
        started = _timestamp(payload.get("started_at"))
        completed = _timestamp(payload.get("completed_at"))
        if (
            not started <= completed <= current
            or current - started > HOST_SNAPSHOT_MAX_AGE
        ):
            return unknown
        if (
            expected_release == "development"
            or not expected_release
            or payload.get("release_ref") != expected_release
        ):
            return unknown
        checks = payload.get("checks")
        if not isinstance(checks, list) or len(checks) > len(_HOST_CHECKS):
            return unknown
        by_name = {}
        for check in checks:
            if not isinstance(check, dict) or set(check) != {"name", "status"}:
                return unknown
            name, status = check["name"], check["status"]
            if not isinstance(name, str) or not isinstance(status, str):
                return unknown
            if (
                name not in _HOST_CHECKS
                or name in by_name
                or status not in _HOST_STATES
            ):
                return unknown
            if status == "SKIPPED_BY_POLICY" and name not in {
                "backup",
                "restore_drill",
                "offsite_upload",
            }:
                return unknown
            by_name[name] = status
        rows = []
        for name, title in _HOST_CHECKS.items():
            status, label = _HOST_STATES.get(by_name.get(name), ("unknown", "未记录"))
            rows.append(
                {"name": name, "title": title, "status": status, "label": label}
            )
        service = rows[0]
        return {
            **_state(
                service["status"],
                "发布与容器：" + service["label"],
                "最近一次宿主巡检结果，不是实时健康；其它检查分别列示。",
            ),
            "checked_at": _time_label(completed),
            "checks": rows,
        }
    except (OSError, ValueError, TypeError, OverflowError):
        return unknown


def read_small_json(path: Path) -> dict:
    """Reject non-regular, linked, or oversized receipts/tasks without blocking."""
    fd = os.open(path, os.O_RDONLY | os.O_NONBLOCK | os.O_NOFOLLOW)
    with os.fdopen(fd, "rb") as stream:
        metadata = os.fstat(stream.fileno())
        if not stat.S_ISREG(metadata.st_mode) or metadata.st_size > MAX_JSON_BYTES:
            raise ValueError("not a bounded regular JSON file")
        encoded = stream.read(MAX_JSON_BYTES + 1)
        if len(encoded) > MAX_JSON_BYTES:
            raise ValueError("JSON read limit reached")
    try:
        payload = json.loads(encoded)
    except RecursionError as exc:
        raise ValueError("JSON nesting limit reached") from exc
    if not isinstance(payload, dict):
        raise ValueError("JSON object required")
    return payload


def _timestamp(value) -> float:
    if isinstance(value, bool) or value is None:
        raise ValueError("timestamp missing")
    try:
        number = float(value)
    except (TypeError, ValueError):
        parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
        number = (
            parsed.replace(tzinfo=CST).timestamp()
            if parsed.tzinfo is None
            else parsed.timestamp()
        )
    if not math.isfinite(number) or number <= 0:
        raise ValueError("invalid timestamp")
    return number


def _time_label(value) -> str:
    try:
        return datetime.fromtimestamp(_timestamp(value), CST).strftime(
            "%m-%d %H:%M:%S CST"
        )
    except (ValueError, TypeError, OverflowError, OSError):
        return "未记录"


def _state(status: str, label: str, detail: str) -> dict:
    return {"status": status, "label": label, "detail": detail}


def _attention(title, impact, href, link_label) -> dict:
    return {
        "title": title,
        "impact": impact,
        "timing": "持续时间未记录",
        "href": href,
        "link_label": link_label,
    }


def feed_attention(health_rows, cfg) -> list[dict]:
    items = []
    for row in health_rows:
        data = feed_data_health(row, cfg)
        name = str(row[1] or "未命名订阅源")
        if row[9]:
            # Last error is evidence of a reported error, not its starting time.
            items.append(
                _attention(
                    f"{name}：最近记录有错误",
                    "可能影响该源抓取或条目处理；详见源记录。",
                    "#source-health",
                    "查看源记录",
                )
            )
        elif data["data_degraded"]:
            items.append(
                _attention(
                    f"{name}：连续空检查 {data['consecutive_empty_checks']} 次",
                    "该源持续未抓到文章；不等同于评分未达标。",
                    "/ops-feeds",
                    "查看零输出说明",
                )
            )
    return items


def news_status(freshness, refresh_running=False) -> dict:
    attempt = freshness.get("last_attempt") or {}
    if not isinstance(attempt, dict):
        attempt = {}
    status = attempt.get("status")
    counts = [
        attempt.get(key)
        for key in ("enabled_sources", "succeeded_sources", "failed_sources")
    ]
    valid_counts = all(type(value) is int and value >= 0 for value in counts)
    partial = attempt.get("partial_sources", 0)
    valid_counts = valid_counts and type(partial) is int and partial >= 0
    valid_counts = valid_counts and counts[0] == counts[1] + partial + counts[2]
    result = "最近一轮结果未完整记录"
    if valid_counts:
        result = (f"最近一轮：启用 {counts[0]} / 完整完成 {counts[1]} / "
                  f"部分完成 {partial} / 失败 {counts[2]}")
    if (
        freshness.get("degraded")
        or freshness.get("stale")
        or status in {"partial", "failed"}
    ):
        label = (
            "刷新结果需关注" if freshness.get("degraded") else "完整刷新已超时或未记录"
        )
        card = _state("warn", label, "按现有刷新状态判断；不是容器健康或行情采集结论。")
    elif (
        _time_label(freshness.get("last_full_refresh_at")) == "未记录"
        or _time_label(attempt.get("finished_at")) == "未记录"
        or not valid_counts
        or status not in {"success", "partial", "failed", "skipped"}
    ):
        card = _state(
            "unknown", "刷新证据不完整", "缺少完整刷新或最近一轮结果，不能判断正常。"
        )
    elif status == "success" and counts[0] > 0 and counts[2] + partial == 0:
        card = _state("ok", "最近完整刷新成功", "仅说明已记录的新闻刷新结果。")
    else:
        card = _state(
            "info", "查看最近一轮结果", "未将累计源成功/失败次数当作本轮结果。"
        )
    if refresh_running:
        card["detail"] += " 当前正在刷新；尚未代表本轮完成。"
    return {
        **card,
        "last_full": _time_label(freshness.get("last_full_refresh_at")),
        "attempt_time": _time_label(attempt.get("finished_at")),
        "attempt_result": result,
    }


def _queue_metrics(directory: Path, now: float) -> dict:
    counts = {"pending": 0, "inflight": 0, "dead_letter": 0, "invalid_files": 0}
    oldest = now
    deadline = time.monotonic() + QUEUE_SCAN_SECONDS
    inspected = 0
    # A missing root is unknown, not proof of an empty queue. Optional subdirs
    # may legitimately not exist until the first task is dispatched.
    for folder, key in (
        (directory, "pending"),
        (directory / "inflight", "inflight"),
        (directory / "dead-letter", "dead_letter"),
    ):
        try:
            entries = os.scandir(folder)
        except FileNotFoundError:
            if key == "pending":
                raise
            continue
        with entries:
            for entry in entries:
                inspected += 1
                if inspected > MAX_QUEUE_ENTRIES or time.monotonic() > deadline:
                    raise ValueError("queue read budget reached")
                pattern = _DEAD if key == "dead_letter" else _TASK
                if not pattern.fullmatch(entry.name):
                    continue
                counts[key] += 1
                if key == "dead_letter":
                    continue
                try:
                    payload = read_small_json(Path(entry.path))
                    created = payload.get("created_at")
                    created = (
                        entry.stat(follow_symlinks=False).st_mtime
                        if created is None
                        else _timestamp(created)
                    )
                    if created > now:
                        raise ValueError("future task timestamp")
                    oldest = min(oldest, created)
                except FileNotFoundError:
                    counts[key] -= 1  # Normal race with the existing dispatcher.
                except (OSError, ValueError, TypeError, OverflowError):
                    counts["invalid_files"] += 1
    return {**counts, "oldest_age": max(0, now - oldest)}


def read_queue_status(directory, now=None) -> dict:
    try:
        metrics = _queue_metrics(Path(directory), time.time() if now is None else now)
    except (OSError, ValueError, TypeError):
        return _state(
            "unknown",
            "队列未知",
            "未接入、不可读或超出首页读取上限；未据此判断队列正常。",
        )
    stale = metrics["oldest_age"] > QUEUE_MAX_AGE_SECONDS
    needs_attention = (
        stale or metrics["dead_letter"] > 0 or metrics["invalid_files"] > 0
    )
    detail = (
        f"待处理 {metrics['pending']} / 处理中 {metrics['inflight']} / "
        f"死信 {metrics['dead_letter']} / 无效文件 {metrics['invalid_files']}"
    )
    if stale:
        detail += "；最老任务超过现有等待阈值"
    return _state(
        "warn" if needs_attention else "ok",
        "队列需关注" if needs_attention else "队列读取未见异常",
        detail,
    )


def _market_card(payload, kind, title, now) -> dict:
    status = payload.get("status")
    if (
        payload.get("version") != 1
        or payload.get("kind") != kind
        or status not in VALID_STATUSES
    ):
        raise ValueError("invalid receipt contract")
    completed = _timestamp(payload.get("completed_at"))
    if completed > now:
        raise ValueError("future receipt")
    trade_date = str(payload.get("trade_date") or "")
    skipped = status in {"skipped_nontrading", "skipped_maintenance"}
    if not trade_date and not skipped:
        raise ValueError("trade date missing")
    if trade_date:
        date.fromisoformat(trade_date)
        if trade_date > datetime.fromtimestamp(now, CST).date().isoformat():
            raise ValueError("future trade date")
    replay = "replay:" in str(payload.get("detail") or "").lower()
    old = trade_date != datetime.fromtimestamp(now, CST).date().isoformat()
    if replay:
        card = _state("info", "回放收据", "回放不代表自然收盘成功。")
    elif skipped:
        label = "非交易日跳过" if status == "skipped_nontrading" else "维护跳过"
        card = _state("info", label, "跳过不是本次自然收盘成功。")
    elif old:
        card = _state(
            "info",
            "历史收据",
            f"记录结果：{status}；未验证交易日历，不当作本次自然收盘成功。",
        )
    elif status in {"incomplete", "error"}:
        card = _state(
            "warn", "收据记录未完成或失败", "影响范围以该任务收据为准；不是整站故障。"
        )
    else:
        card = _state(
            "info", "收据记录成功", "仅展示收据结果；尚不据此确认本次自然收盘验收通过。"
        )
    return {
        **card,
        "kind": kind,
        "title": title,
        "trade_date": trade_date or "未记录",
        "completed_at": _time_label(completed),
        "origin": f"{kind}_last.json",
    }


def read_market_receipts(directory, now=None) -> list[dict]:
    cards = []
    for kind, title in _KINDS:
        try:
            payload = read_small_json(Path(directory) / f"{kind}_last.json")
            card = _market_card(
                payload, kind, title, time.time() if now is None else now
            )
        except (OSError, ValueError, TypeError, OverflowError):
            card = {
                **_state(
                    "unknown", "收据未知", "缺失、损坏或不可读；未据此判断任务成功。"
                ),
                "kind": kind,
                "title": title,
                "trade_date": "未记录",
                "completed_at": "未记录",
                "origin": f"{kind}_last.json",
            }
        cards.append(card)
    return cards


def build_dashboard_ops(
    cfg, health_rows, freshness, *, refresh_running=False, db_path, now=None
) -> dict:
    """Reuse current data. No network, probes, subprocesses, or state mutation."""
    attention = feed_attention(health_rows, cfg)
    service = read_host_ops_snapshot(
        Path(db_path).parent / "ops" / "host_status.json",
        release_version(),
        now=now,
    )
    for check in service["checks"]:
        if check["status"] == "warn":
            attention.append(
                _attention(
                    "宿主巡检 · " + check["title"] + "：" + check["label"],
                    "这是最近一次巡检记录；具体诊断保留在宿主日志，不代表其它项目失败。",
                    "#host-checks",
                    "查看宿主巡检",
                )
            )
    news = news_status(freshness, refresh_running)
    queue = read_queue_status(_spool_dir(), now=now)
    try:
        directory = result_dir_from_env(db_path=Path(db_path))
    except ValueError:
        directory = None
    market = read_market_receipts(directory, now=now)
    if news["status"] == "warn":
        attention.append(
            _attention(news["label"], news["detail"], "#news-status", "查看刷新记录")
        )
    if queue["status"] == "warn":
        attention.append(
            _attention(queue["label"], queue["detail"], "#news-status", "查看队列记录")
        )
    for card in market:
        if card["status"] == "warn":
            attention.append(
                _attention(
                    card["title"] + "：" + card["label"],
                    card["detail"],
                    "#market-close-details",
                    "查看收盘收据",
                )
            )
    return {
        "attention": attention,
        "news": news,
        "queue": queue,
        "market": market,
        "service": service,
    }
