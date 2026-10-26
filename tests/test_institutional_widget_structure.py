"""Presentation sections retain existing counters, states and access boundaries."""

from copy import deepcopy
from html.parser import HTMLParser

import pytest
from flask import Flask

from services.model_quality_overview import render_quality_overview


class _Sections(HTMLParser):
    def __init__(self, html):
        super().__init__()
        self.nodes, self.stack = [], []
        self.feed(html)

    def handle_starttag(self, tag, attrs):
        node = {"tag": tag, "attrs": dict(attrs), "text": ""}
        self.nodes.append(node)
        if tag not in {"br", "hr", "img", "input", "meta", "link"}:
            self.stack.append(node)

    def handle_data(self, data):
        for node in self.stack:
            node["text"] += data

    def handle_endtag(self, tag):
        assert self.stack and self.stack[-1]["tag"] == tag
        self.stack.pop()

    def matching(self, css_class):
        return [node for node in self.nodes
                if css_class in node["attrs"].get("class", "").split()]

    def text(self, css_class):
        return "\n".join(node["text"] for node in self.matching(css_class))


def _quality_result():
    return {
        "strategy_version": "score:current", "mapping_version": "map:current",
        "cohort_history": {
            "current_version": "score:current", "current_mapping_version": "map:current",
            "current_scored_signals": 23, "current_selected_signals": 3,
        },
        "_processing_overview": {
            "available": True, "strategy_version": "score:current", "hours": 24,
            "observed_at": "2026-09-20T05:20:00+00:00",
            "transport_attempts": 8, "transport_succeeded": 5, "transport_failed": 3,
            "validation_attempts": 7,
            "final_paths": {
                "llm": 3, "stage2": 2, "llm_safe_neutral_grounding": 4,
                "llm_safe_neutral_initial": 2, "fallback_local": 1,
                "parse_error": 2, "quality_quarantine": 1, "local_reject": 3,
            },
        },
    }


def test_quality_sections_preserve_original_counters_context_and_auth_explanation():
    result = _quality_result()
    saved = deepcopy(result)
    parsed = _Sections(render_quality_overview(result))

    assert result == saved and not parsed.stack
    for css_class in ("mq-context", "mq-processing", "mq-metrics", "mq-assessment", "mq-research"):
        assert len(parsed.matching(css_class)) == 1
    assert len(parsed.matching("mq-metric")) == 3
    assert len(parsed.matching("mq-metric-label")) == 3
    assert len(parsed.matching("mq-metric-value")) == 3
    assert "当前评分策略：score:current" in parsed.text("mq-context")
    processing = parsed.text("mq-processing")
    for sentence in (
        "当前评分策略，近24小时（不按映射版本拆分）",
        "汇总采集时间：2026-09-20 13:20:00（北京时间 UTC+8）",
        "不是页面打开时间，也不是新闻更新时间",
        "调用尝试 8 次 · 成功 5 次 · 失败 3 次",
        "校验尝试 7 次；调用成功不表示输出正确",
        "已识别路径的基础评分记录 18 条：实质模型结果 5 · 保守中性 6 · 本地兜底 1 · "
        "处理异常 2 · 隔离待处理 1 · 其他已识别路径 3",
        "三种计量单位，不能组成逐条对应的通过率漏斗",
        "基础评分记录按评分时间统计，排除相似副本",
        "保守中性不是模型错误率，也不证明拒收正确",
    ):
        assert sentence in processing
    assessment = parsed.text("mq-assessment")
    assert "解释质量：尚未评估（本页未接入人工核对结果）" in assessment
    assert "这不表示其他地方不存在人工核对" in assessment
    assert "查看记录（需后台登录）" in assessment
    assert "当前契约近24小时收录的现存缓存，最新50条" in assessment
    assert "不是上述数字的逐行明细" in assessment
    assert "研究查询窗口：评分 23 条 · 入选 3 条" in parsed.text("mq-research")
    assert "市场表现仅作事后参考" in parsed.text("mq-research")
    links = [node["attrs"]["href"] for node in parsed.nodes if node["tag"] == "a"]
    assert links == ["https://ai.example.invalid/dashboard/neutral-records"]


@pytest.mark.parametrize("state,expected", [
    ("unknown", "处理状态未知：本快照尚未接入处理汇总"),
    ("unavailable", "处理状态暂不可用：不能据此判断是否有处理记录"),
    ("mismatch", "汇总与当前评分契约或24小时窗口不匹配"),
    ("malformed", "处理状态未知：汇总字段不完整"),
])
def test_quality_unobserved_processing_does_not_become_zero_or_quality_failure(state, expected):
    result = _quality_result()
    if state == "unknown":
        result.pop("_processing_overview")
    elif state == "unavailable":
        result["_processing_overview"]["available"] = False
    elif state == "mismatch":
        result["_processing_overview"]["hours"] = 48
    else:
        result["_processing_overview"]["final_paths"] = None
    parsed = _Sections(render_quality_overview(result))

    assert expected in parsed.text("mq-processing")
    assert not parsed.matching("mq-metric")
    assert "调用尝试 0 次" not in parsed.text("mq-processing")
    assert "解释质量：尚未评估" in parsed.text("mq-assessment")
    assert "处理状态" not in parsed.text("mq-assessment")
    assert "解释质量" not in parsed.text("mq-processing")


def test_quality_observed_empty_window_retains_zero_and_unknown_validation():
    result = _quality_result()
    result["_processing_overview"].update(
        transport_attempts=0, transport_succeeded=0, transport_failed=0,
        validation_attempts=0, final_paths={},
    )
    parsed = _Sections(render_quality_overview(result))

    assert len(parsed.matching("mq-metric")) == 3
    assert "调用尝试 0 次 · 成功 0 次 · 失败 0 次" in parsed.text("mq-metrics")
    assert "未观测到校验记录；校验结果未知" in parsed.text("mq-metrics")
    assert "基础评分记录 0 条" in parsed.text("mq-metrics")
    assert "0%" not in parsed.text("mq-metrics")
    assert "解释质量：尚未评估" in parsed.text("mq-assessment")


@pytest.mark.parametrize("stale,degraded,running,status,color", [
    (False, False, False, "运行正常", "#16a34a"),
    (False, True, False, "部分源异常", "#d97706"),
    (True, True, False, "数据过期", "#dc2626"),
    (True, True, True, "刷新中", "#2563eb"),
])
def test_ops_classes_keep_existing_status_precedence_colors_values_and_cache(
    monkeypatch, stale, degraded, running, status, color,
):
    from routes import dashboard_routes as routes

    observed_calls = []
    state = {"stale": stale, "degraded": degraded, "last_full_refresh_at": 1770000000}

    def cached(key, ttl, _loader):
        observed_calls.append((key, ttl))
        return state, running, (10, 2, 3, 1, 0, 0, 4, 6, 1770000000)

    monkeypatch.setattr(routes, "_ops_cached_value", cached)
    monkeypatch.setattr(routes.time, "time", lambda: 1770000060)
    app = Flask(__name__)
    app.register_blueprint(routes.dash_bp)
    response = app.test_client().get("/ops-summary")
    parsed = _Sections(response.get_data(as_text=True))

    assert response.status_code == 200 and not parsed.stack
    assert observed_calls == [("ops-summary", routes._OPS_SUMMARY_CACHE_TTL_SEC)]
    assert response.headers["Cache-Control"] == "public, max-age=30"
    assert response.headers["Widget-Title"] == "Ops Summary"
    assert response.headers["Widget-Content-Type"] == "html"
    for css_class in ("ops-summary", "ops-summary-head", "ops-summary-title",
                      "ops-summary-subtitle", "ops-summary-status", "ops-summary-grid"):
        assert len(parsed.matching(css_class)) == 1
    for css_class in ("ops-summary-card", "ops-summary-label", "ops-summary-value", "ops-summary-note"):
        assert len(parsed.matching(css_class)) == 4
    assert parsed.text("ops-summary-title") == "运维巡检摘要"
    assert parsed.text("ops-summary-subtitle") == "AI-Filter / Feed / LLM 关键指标"
    assert parsed.text("ops-summary-status") == status
    assert f"color:{color}" in parsed.matching("ops-summary-status")[0]["attrs"]["style"]
    assert parsed.text("ops-summary-label").splitlines() == ["运行状态", "最近刷新", "通过率", "API 节省率"]
    assert parsed.text("ops-summary-value").splitlines() == [status, "1分钟前", "60.0%", "40.0%"]
    assert "LLM 调用 2 次" in parsed.text("ops-summary-note")
    assert "6/10 篇" in parsed.text("ops-summary-note")
    assert ("刷新任务正在执行" if running else "Feed 数据新鲜度") in parsed.text("ops-summary-note")


def test_ops_unobserved_refresh_keeps_existing_empty_labels_and_zero_stats(monkeypatch):
    from routes import dashboard_routes as routes

    monkeypatch.setattr(routes, "_ops_cached_value", lambda *_args: ({}, False, None))
    app = Flask(__name__)
    with app.test_request_context("/ops-summary"):
        response = routes.ops_summary_widget()
    parsed = _Sections(response.get_data(as_text=True))

    assert parsed.text("ops-summary-value").splitlines() == ["运行正常", "无记录", "0%", "0%"]
    assert "等待首次全量刷新" in parsed.text("ops-summary-note")
    assert "0/0 篇" in parsed.text("ops-summary-note")
    assert "LLM 调用 0 次" in parsed.text("ops-summary-note")
