"""Public aggregation, unknown states and private-record boundaries."""

from copy import deepcopy
import json

import pytest

from services import model_quality_overview as overview


def _snapshot(**updates):
    return {"available": True, "strategy_version": "score:current", "hours": 24,
            "transport": [], "validation": [], "final_paths": {}, **updates}


def _collect(monkeypatch, snapshot):
    calls = []
    monkeypatch.setattr(overview, "process_role", lambda: "worker")

    def read(version, hours):
        calls.append((version, hours))
        return snapshot

    monkeypatch.setattr(overview, "score_quality_snapshot", read)
    result = overview.collect_processing_overview("score:current")
    assert calls == [("score:current", 24)]
    return result


def _render(data=None, **updates):
    result = {"strategy_version": "score:current", "mapping_version": "map:current", **updates}
    if data is not None:
        result["_processing_overview"] = data
    return overview.render_quality_overview(result)


def test_empty_observed_window_is_zero_without_inventing_validation(monkeypatch):
    data = _collect(monkeypatch, _snapshot())
    assert data["available"] and data["transport_attempts"] == 0
    assert data["validation_attempts"] == 0 and data["final_paths"] == {}
    html = _render(data)
    assert "调用尝试 0 次" in html and "基础评分记录 0 条" in html
    assert "未观测到校验记录；校验结果未知" in html
    assert "0%" not in html and "人工核对结果" in html


@pytest.mark.parametrize("snapshot", [
    _snapshot(available=False), _snapshot(strategy_version="old"),
    _snapshot(hours=2),
    _snapshot(transport=None), _snapshot(transport=[{"attempts": 1, "succeeded": 2}]),
    _snapshot(transport=[{"attempts": 1, "succeeded": 2}, {"attempts": 9, "succeeded": 0}]),
    _snapshot(final_paths={"llm": "private-value"}),
])
def test_unavailable_or_malformed_is_not_empty(monkeypatch, snapshot):
    data = _collect(monkeypatch, snapshot)
    assert data["available"] is False
    assert data["transport_attempts"] is None and data["final_paths"] is None
    html = _render(data)
    assert "暂不可用" in html and "基础评分记录 0 条" not in html


def test_collector_fault_does_not_expose_error(monkeypatch):
    monkeypatch.setattr(overview, "process_role", lambda: "worker")

    def fail(*_args):
        raise RuntimeError("private database path /secret")

    monkeypatch.setattr(overview, "score_quality_snapshot", fail)
    data = overview.collect_processing_overview("score:current")
    assert data["available"] is False and "secret" not in json.dumps(data)


@pytest.mark.parametrize("role", ["web", "all", ""])
def test_non_worker_never_collects(monkeypatch, role):
    monkeypatch.setattr(overview, "process_role", lambda: role)
    monkeypatch.setattr(overview, "score_quality_snapshot", lambda *_args: pytest.fail("private read"))
    assert overview.collect_processing_overview("score:current")["available"] is False


def test_aggregate_allowlist_and_separate_processing_categories(monkeypatch):
    raw = _snapshot(
        transport=[{"attempts": 5, "succeeded": 3, "backend": "secret-backend", "model": "secret-model"}],
        validation=[{"observed": 7, "reason": "secret-reason", "accepted": 6}],
        final_paths={"llm": 2, "stage2": 1, "llm_safe_neutral_contract": 4,
                     "stage2_safe_neutral_grounding": 2, "fallback_local": 1,
                     "parse_error": 3, "quality_quarantine": 2, "local_reject": 8,
                     "fast_pass": 4, "secret-path": 500},
        title="secret-title", source="secret-source", link="https://secret.example/record",
    )
    saved = deepcopy(raw)
    data = _collect(monkeypatch, raw)
    assert raw == saved
    assert set(data) == {"available", "hours", "strategy_version", "observed_at", "transport_attempts",
                         "transport_succeeded", "transport_failed", "validation_attempts", "final_paths"}
    assert data["transport_failed"] == 2 and data["validation_attempts"] == 7
    assert "secret" not in json.dumps(data)
    html = _render(data)
    assert "实质模型结果 3" in html and "保守中性 6" in html
    assert "本地兜底 1" in html and "处理异常 3" in html and "隔离待处理 2" in html
    assert "其他已识别路径 12" in html and "基础评分记录 27 条" in html
    assert "校验尝试 7 次" in html and "不是模型错误率" in html
    assert "secret" not in html


def _processing_summary(**updates):
    return {
        "available": True, "strategy_version": "score:current", "hours": 24,
        "transport_attempts": 0, "transport_succeeded": 0, "transport_failed": 0,
        "validation_attempts": 0, "final_paths": {}, **updates,
    }


def test_processing_overview_time_converts_to_beijing_across_day_boundary():
    html = _render(_processing_summary(observed_at="2026-09-20T18:30:00Z"))
    assert "汇总采集时间：2026-09-21 02:30:00（北京时间 UTC+8）" in html
    assert "表示处理汇总开始采集的时间，不是页面打开时间，也不是新闻更新时间" in html
    assert "2026-09-20T18:30:00Z" not in html


def test_processing_overview_time_converts_aware_offset_to_beijing():
    html = _render(_processing_summary(observed_at="2026-09-20T01:15:30-04:00"))
    assert "汇总采集时间：2026-09-20 13:15:30（北京时间 UTC+8）" in html
    assert "2026-09-20T01:15:30-04:00" not in html


@pytest.mark.parametrize(
    "observed_at", [None, "", "not-a-time", "2026-09-20T05:20:00"],
)
def test_processing_overview_time_missing_or_invalid_is_unknown(observed_at):
    html = _render(_processing_summary(observed_at=observed_at))
    assert "汇总采集时间：时间未知" in html
    assert "时间未记录" not in html


def test_missing_mismatched_and_partial_summary_stay_unknown(monkeypatch):
    assert "尚未接入处理汇总" in _render()
    data = _collect(monkeypatch, _snapshot())
    for updates in ({"strategy_version": "old"}, {"hours": 2}):
        assert "不匹配" in _render({**data, **updates})
    assert "汇总字段不完整" in _render({**data, "final_paths": None})
    assert "汇总字段不完整" in _render({**data, "transport_failed": 1})
    assert "当前评分策略：score:current" in _render()


def test_pure_render_escapes_preserves_input_and_static_authenticated_link(monkeypatch):
    data = _collect(monkeypatch, _snapshot())
    data["observed_at"] = '<script>alert("timestamp")</script>'
    data["final_paths"]["<script>private</script>"] = 100
    saved = deepcopy(data)
    monkeypatch.setattr(overview, "score_quality_snapshot", lambda *_args: pytest.fail("render read"))
    monkeypatch.setattr(overview, "collect_processing_overview", lambda *_args: pytest.fail("render collect"))
    html = _render(data)
    assert data == saved
    assert "时间未知" in html and "<script" not in html
    assert "private" not in html and "<iframe" not in html
    assert 'href="https://ai.example.invalid/dashboard/neutral-records"' in html
    assert "查看记录（需后台登录）" in html and "最新50条" in html
    assert "不是上述数字的逐行明细" in html
    assert "?token=" not in html and "?key=" not in html


def test_history_requires_exact_scoring_and_mapping_identity():
    history = {"current_version": "score:current", "current_mapping_version": "map:current",
               "current_scored_signals": 20, "current_selected_signals": 0,
               "family_scored_signals": 9000}
    assert "研究查询窗口：评分 20 条 · 入选 0 条" in _render(cohort_history=history)
    assert "9000" not in _render(cohort_history=history)
    for key in ("current_version", "current_mapping_version"):
        assert "研究样本数尚未提供" in _render(cohort_history={**history, key: "old"})
    assert "研究样本数尚未提供" in _render(cohort_history={})
