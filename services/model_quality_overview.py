"""Public, aggregate-only overview; private records stay behind browser auth."""

from datetime import datetime, timezone
from html import escape
from zoneinfo import ZoneInfo

from scoring_provenance import NON_SUBSTANTIVE_SCORE_PATHS, RECOGNIZED_SCORE_PATHS
from services.neutral_records import render_neutral_records_entry
from services.score_quality import score_quality_snapshot
from services.shared_cache import process_role


_BEIJING_TZ = ZoneInfo("Asia/Shanghai")
_UNKNOWN_TIME = "时间未知"


def _format_beijing_time(value) -> str:
    """Format an aware ISO timestamp for display without guessing its timezone."""
    if not isinstance(value, str) or not value.strip():
        return _UNKNOWN_TIME
    try:
        parsed = datetime.fromisoformat(value.strip().replace("Z", "+00:00"))
        if parsed.tzinfo is None or parsed.utcoffset() is None:
            return _UNKNOWN_TIME
        local = parsed.astimezone(_BEIJING_TZ)
    except (TypeError, ValueError, OSError, OverflowError):
        return _UNKNOWN_TIME
    return local.strftime("%Y-%m-%d %H:%M:%S") + "（北京时间 UTC+8）"


def _count(value):
    if type(value) is not int or value < 0:
        raise ValueError("Unobserved or invalid count")
    return value


def collect_processing_overview(strategy_version: str) -> dict:
    """Worker-only projection of existing 24h telemetry, never private records."""
    overview = {
        "available": False, "hours": 24, "strategy_version": strategy_version,
        "observed_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "transport_attempts": None, "transport_succeeded": None,
        "transport_failed": None, "validation_attempts": None, "final_paths": None,
    }
    if process_role() != "worker" or not strategy_version:
        return overview
    try:
        data = score_quality_snapshot(strategy_version, 24)
        if (data.get("available") is not True or data.get("hours") != 24
                or data.get("strategy_version") != strategy_version):
            return overview
        attempts = sum(_count(row["attempts"]) for row in data["transport"])
        succeeded = sum(_count(row["succeeded"]) for row in data["transport"])
        failed = sum(_count(row["attempts"] - row["succeeded"]) for row in data["transport"])
        validation = sum(_count(row["observed"]) for row in data["validation"])
        paths = {key: _count(value) for key, value in data["final_paths"].items()
                 if key in RECOGNIZED_SCORE_PATHS}
    except Exception:
        return overview
    return {**overview, "available": True, "transport_attempts": attempts,
            "transport_succeeded": succeeded, "transport_failed": failed,
            "validation_attempts": validation, "final_paths": paths}


def render_quality_overview(result: dict) -> str:
    """Render a frozen allowlisted summary without queries or input mutations."""
    overview = result.get("_processing_overview")
    state = "处理状态未知：本快照尚未接入处理汇总。"
    details = ""
    if isinstance(overview, dict):
        if (not result.get("strategy_version")
                or overview.get("strategy_version") != result.get("strategy_version")
                or overview.get("hours") != 24):
            state = "处理状态未知：汇总与当前评分契约或24小时窗口不匹配。"
        elif overview.get("available") is not True:
            state = "处理状态暂不可用：不能据此判断是否有处理记录。"
        else:
            try:
                attempts, succeeded, failed, validation = (
                    _count(overview[key]) for key in ("transport_attempts", "transport_succeeded",
                                                       "transport_failed", "validation_attempts"))
                if failed != _count(attempts - succeeded):
                    raise ValueError("Inconsistent transport totals")
                paths = {key: _count(value) for key, value in overview["final_paths"].items()
                         if key in RECOGNIZED_SCORE_PATHS}
                neutral = sum(paths.get(path, 0) for path in NON_SUBSTANTIVE_SCORE_PATHS)
                other = sum(value for path, value in paths.items() if path not in NON_SUBSTANTIVE_SCORE_PATHS
                            and path not in {"llm", "stage2", "fallback_local", "parse_error", "quality_quarantine"})
                state = "处理状态：当前评分策略，近24小时（不按映射版本拆分）。"
                validation_text = (f"校验尝试 {validation} 次" if validation
                                   else "未观测到校验记录；校验结果未知")
                details = (
                    f'<p class="mq-note">汇总采集时间：{escape(_format_beijing_time(overview.get("observed_at")))}；'
                    '表示处理汇总开始采集的时间，不是页面打开时间，也不是新闻更新时间。</p>'
                    '<div class="mq-metrics">'
                    '<div class="mq-metric"><h4 class="mq-metric-label">模型调用</h4>'
                    f'<p class="mq-metric-value">调用尝试 {attempts} 次 · 成功 {succeeded} 次 · 失败 {failed} 次</p></div>'
                    '<div class="mq-metric"><h4 class="mq-metric-label">输出校验</h4>'
                    f'<p class="mq-metric-value">{validation_text}；调用成功不表示输出正确。</p></div>'
                    '<div class="mq-metric"><h4 class="mq-metric-label">基础评分记录</h4>'
                    f'<p class="mq-metric-value">已识别路径的基础评分记录 {sum(paths.values())} 条：'
                    f'实质模型结果 {paths.get("llm", 0) + paths.get("stage2", 0)} · '
                    f'保守中性 {neutral} · 本地兜底 {paths.get("fallback_local", 0)} · '
                    f'处理异常 {paths.get("parse_error", 0)} · 隔离待处理 {paths.get("quality_quarantine", 0)} · '
                    f'其他已识别路径 {other}</p></div></div>'
                )
            except (KeyError, TypeError, ValueError, AttributeError):
                state = "处理状态未知：汇总字段不完整。"
    history = result.get("cohort_history") or {}
    history_text = "当前精确契约的研究样本数尚未提供。"
    if (isinstance(history, dict) and result.get("strategy_version") and result.get("mapping_version")
            and history.get("current_version") == result["strategy_version"]
            and history.get("current_mapping_version") == result["mapping_version"]):
        labels = (("current_scored_signals", "评分"), ("current_selected_signals", "入选"))
        counts = [f"{label} {history[key]} 条" for key, label in labels
                  if type(history.get(key)) is int and history[key] >= 0]
        if counts:
            history_text = "当前评分策略 × 映射版本，研究查询窗口：" + " · ".join(counts)
    return (
        '<section class="model-quality-overview" style="font-size:13px;line-height:1.7;">'
        '<div class="mq-context">'
        '<h3 style="margin:0 0 8px;">模型质量概览</h3>'
        f'<p>当前评分策略：{escape(str(result.get("strategy_version") or "未知"))}</p></div>'
        '<div class="mq-processing">'
        f'<p>{state}</p>{details}'
        '<p class="mq-note">调用次数、校验尝试、基础评分记录是三种计量单位，不能组成逐条对应的通过率漏斗。'
        '基础评分记录按评分时间统计，排除相似副本。保守中性不是模型错误率，也不证明拒收正确。</p></div>'
        '<div class="mq-assessment">'
        '<p>解释质量：尚未评估（本页未接入人工核对结果）。无法据此评定新闻解释是否可靠；'
        '这不表示其他地方不存在人工核对。</p>'
        f'{render_neutral_records_entry()}'
        '<p class="mq-note">上方入口固定查看当前契约近24小时收录的现存缓存，最新50条；'
        '按收录时间查询，与按评分时间统计的账本数量不同，不是上述数字的逐行明细。</p></div>'
        '<div class="mq-research">'
        f'<p>{escape(history_text)}</p>'
        '<p class="mq-note">市场表现仅作事后参考，详细口径与统计在下方。</p></div></section>'
    )
