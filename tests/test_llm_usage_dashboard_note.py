from pathlib import Path

from jinja2 import Template


def _render_cost_card(stats):
    source = (Path(__file__).parents[1] / "templates" / "dashboard.html").read_text()
    start = source.index('<div class="card-label">滚动近24小时 LLM 估算费用</div>')
    end = source.index('\n        </div>', start)
    return Template(source[start:end]).render(llm_cost_stats=stats)


def test_dashboard_distinguishes_recorded_tokens_from_missing_usage_and_bill():
    html = _render_cost_card({
        "calls": 3, "total_tokens": 120, "estimated_cost": 0,
        "unknown_usage_calls": 1, "partial_usage_calls": 1,
    })
    assert "120 tokens" in html
    assert "估算非账单" in html
    assert "用量未知/未标记 1 次，部分用量 1 次" in html


def test_dashboard_legacy_fixture_without_completeness_keys_still_renders():
    html = _render_cost_card({"calls": 1, "total_tokens": 120, "estimated_cost": 0})
    assert "估算非账单" in html
    assert "用量未知/未标记" not in html


def test_dashboard_cost_window_failures_zero_pricing_and_budget_are_explicit():
    html = _render_cost_card({
        "calls": 4, "total_tokens": 120, "estimated_cost": 0,
        "failures": 2, "failed_unknown_usage_calls": 1,
        "window_start_utc": "2026-10-09T12:00:00Z",
        "window_end_utc": "2026-10-10T12:00:00Z",
    })
    assert "今日 LLM 成本" not in html
    assert "滚动近24小时" in html
    assert "跨评分契约" in html
    assert "2026-10-09T12:00:00Z — 2026-10-10T12:00:00Z" in html
    assert "失败 2 次，其中 1 次用量不完整或未知" in html
    assert "不代表免费" in html
    assert "预算配置尚未接入自动阻断" in html
    assert "未知用量不按零消耗认定" in html
