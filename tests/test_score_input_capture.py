"""Capture integration in isolated SQLite and temporary private directories."""

import json
from types import SimpleNamespace

import pytest

from core.processor import process_entry
from core.scorer import LLMBackend, Scorer, capture_llm_calls
from db import database
from services.score_evidence_capture import (
    capture_article_score,
    capture_validation_attempt,
    persist_scoring_evidence,
)
from services.score_gold import validate_evidence


class FeedEntry(SimpleNamespace):
    def get(self, key, default=None):
        return getattr(self, key, default)


def _context(directory, **options):
    return SimpleNamespace(
        cfg={
            "score_evidence_capture": {
                "enabled": True,
                "directory": str(directory),
                **options,
            }
        },
        strategy_version="synthetic-contract",
        raw_title="甲公司订单3亿元",
        entry=FeedEntry(link="https://invalid.test/article?token=do-not-capture"),
    )


def _snapshot():
    return {
        "event_id": "synthetic-event",
        "event_key": "synthetic-event-key",
        "strategy_version": "synthetic-contract",
    }


@capture_article_score
def _capture_fixture(
    ctx, result, scorer, summary, *, response="model response", mismatch=False
):
    capture_validation_attempt(
        raw=response,
        parsed=(8, "公司公告", "原始失败理由", "📈"),
        title="wrong source" if mismatch else ctx.raw_title,
        summary=summary,
        backend=SimpleNamespace(name="fixture", model="fixture"),
        stage="stage1",
        attempt="initial",
        reason="ungrounded_score_facts",
        detail="",
        repair_trigger="",
        fact_failure_kind="numeric_fact_failure",
        messages=[
            {"role": "user", "content": f"标题：{ctx.raw_title}\n摘要：{summary}\n"}
        ],
    )


def test_capture_freezes_exact_inputs_and_failed_output(tmp_path):
    ctx = _context(tmp_path / "evidence")
    source = "清洗后评分来源" * 500
    _capture_fixture(ctx, None, None, source)
    persist_scoring_evidence(ctx.entry, _snapshot())
    paths = list((tmp_path / "evidence").glob("*.json"))
    assert len(paths) == 1
    record = json.loads(paths[0].read_text())
    validate_evidence(record)
    assert record["source"] == {"raw_title": ctx.raw_title, "summary": source}
    assert record["attempts"][0]["parsed"][2] == "原始失败理由"
    assert record["attempts"][0]["fact_failure_kind"] == "numeric_fact_failure"
    assert "do-not-capture" not in paths[0].read_text()
    assert paths[0].stat().st_mode & 0o777 == 0o600
    assert paths[0].parent.stat().st_mode & 0o777 == 0o700


@pytest.mark.parametrize(
    "case", ["disabled", "budget", "oversized", "missing_event", "mismatch", "symlink"]
)
def test_capture_failures_do_not_modify_scores_or_fake_complete_evidence(
    tmp_path, case
):
    directory = tmp_path / "evidence"
    ctx = _context(directory, max_record_bytes=1024 if case == "oversized" else 262144)
    if case == "disabled":
        ctx.cfg["score_evidence_capture"]["enabled"] = False
    elif case == "budget":
        ctx.cfg["score_evidence_capture"]["max_records"] = 0
    elif case == "symlink":
        real = tmp_path / "real"
        real.mkdir(mode=0o700)
        directory.symlink_to(real, target_is_directory=True)
    _capture_fixture(
        ctx, None, None, "source", response="x" * 2000, mismatch=case == "mismatch"
    )
    persist_scoring_evidence(ctx.entry, {} if case == "missing_event" else _snapshot())
    assert not list(directory.glob("*.json"))


def test_capture_bound_and_nested_context_isolation(tmp_path):
    first = _context(tmp_path / "first", max_records=1)
    second = _context(tmp_path / "second")
    _capture_fixture(first, None, None, "first")
    _capture_fixture(second, None, None, "second")
    persist_scoring_evidence(first.entry, _snapshot())
    persist_scoring_evidence(second.entry, _snapshot())
    _capture_fixture(first, None, None, "new")
    persist_scoring_evidence(first.entry, _snapshot())
    first_record = json.loads(next((tmp_path / "first").glob("*.json")).read_text())
    second_record = json.loads(next((tmp_path / "second").glob("*.json")).read_text())
    assert first_record["source"]["summary"] == "first"
    assert second_record["source"]["summary"] == "second"
    assert len(list((tmp_path / "first").glob("*.json"))) == 1


@pytest.mark.parametrize(
    "stage2,substantive", [(False, False), (True, False), (False, True)]
)
def test_real_processor_binds_actual_source_not_cache_summary(
    monkeypatch, tmp_path, stage2, substantive
):
    database.init_db()
    scorer = Scorer()
    scorer.clear_runtime_cache()
    backend = LLMBackend("fixture", "", "https://invalid.test", "fixture-model")
    monkeypatch.setattr(backend, "client", object())
    monkeypatch.setattr(scorer, "backends_for", lambda *a, **kw: [backend])
    neutral = json.dumps(
        dict(score=4, tag="其他", logic="来源未显示明确投资催化", sentiment="中性"),
        ensure_ascii=False,
    )
    first = json.dumps(
        dict(score=8, tag="公司公告", logic="甲公司已建设工厂", sentiment="看多"),
        ensure_ascii=False,
    )
    responses = iter(
        [first, neutral] if stage2 else [first if substantive else neutral]
    )
    monkeypatch.setattr(scorer, "summarize", lambda *a: "评分后生成摘要")
    monkeypatch.setattr(
        scorer, "_call_structured_llm", lambda *a, **kw: next(responses)
    )
    monkeypatch.setattr(
        "core.processor._entry_stage2_reasons",
        lambda *a: ["fixture-review"] if stage2 else [],
    )
    cfg = {
        "scoring_strategy_version": "synthetic-contract",
        "threshold": {"pass": 7 if substantive else 3, "similarity": 0.6, "notify": 99},
        "_blacklist_re": None,
        "cache_rejected_entries": True,
        "generate_summary": substantive,
        "similarity_window_hours": 72,
        "local_reject_rules": {"enabled": False},
        "fast_pass": {"enabled": False},
        "two_stage_scoring": {"enabled": stage2},
        "feeds": [],
        "score_evidence_capture": {
            "enabled": True,
            "directory": str(tmp_path / "evidence"),
        },
    }
    entry = FeedEntry(
        title="甲公司已建设工厂",
        link="https://invalid.test/article",
        summary="<p>当时实际来源摘要</p>",
    )
    result, reason = process_entry(
        entry, [], "prompt", cfg, scorer=scorer, feed_url="https://invalid.test/feed"
    )
    assert (
        (result is entry and reason == "scored")
        if substantive
        else (result is None and reason == "filtered")
    )
    record = json.loads(next((tmp_path / "evidence").glob("*.json")).read_text())
    validate_evidence(record)
    assert record["source"]["summary"] == "当时实际来源摘要"
    assert len(record["attempts"]) == (2 if stage2 else 1)
    path = (
        "llm"
        if substantive
        else "stage2_safe_neutral_initial"
        if stage2
        else "llm_safe_neutral_initial"
    )
    assert record["final_decision"]["score_path"] == path
    assert record["final_decision"]["selected"] is substantive
    with database.get_connection() as conn:
        row = conn.execute(
            "SELECT summary, score_path FROM news_cache WHERE link = ?", (entry.link,)
        ).fetchone()
        assert row is not None and row["summary"] == (
            "评分后生成摘要" if substantive else ""
        )
        assert row["score_path"] == record["final_decision"]["score_path"]
        if not substantive:
            assert (
                conn.execute(
                    "SELECT COUNT(*) FROM integration_signal_outbox"
                ).fetchone()[0]
                == 0
            )
    if not substantive:
        assert database.get_recent_entries(cfg=cfg) == []


def test_legacy_initial_template_is_read_filtered_without_history_rewrite(monkeypatch):
    import time
    from core.processor import _process_entry_similarity

    database.init_db()
    cfg = {
        "scoring_strategy_version": "synthetic-contract",
        "similarity_window_hours": 72,
        "threshold": {"similarity": 0.6},
    }
    with database.get_connection() as conn:
        conn.execute(
            "INSERT INTO news_cache (link,title,raw_title,score,tag,logic,created_at,strategy_version,score_path,direction) VALUES (?,?,?,?,?,?,?,?,?,?)",
            (
                "legacy",
                "甲公司订单",
                "甲公司订单",
                4,
                "其他",
                "来源未显示明确投资催化",
                int(time.time()),
                "synthetic-contract",
                "llm",
                "中性",
            ),
        )
        conn.commit()
    assert database.get_recent_entries(cfg=cfg) == []
    with database.get_connection() as conn:
        assert (
            conn.execute(
                "SELECT score_path FROM news_cache WHERE link='legacy'"
            ).fetchone()[0]
            == "llm"
        )
    row = (
        "legacy",
        "甲公司订单",
        "甲公司订单",
        4,
        "其他",
        "来源未显示明确投资催化",
        "中性",
    )
    monkeypatch.setattr("core.processor.find_similar_article", lambda *a: (row, 1.0))
    ctx = SimpleNamespace(
        raw_title="甲公司订单更新", cfg=cfg, entry=FeedEntry(link="new")
    )
    assert _process_entry_similarity(ctx, [row], 3) == (None, None)


def test_local_neutral_records_only_source_bound_rejected_model_attempt(monkeypatch, tmp_path):
    database.init_db()
    ctx = _context(tmp_path / "evidence")
    ctx.cfg["scoring_strategy_version"] = ctx.strategy_version
    ctx.cfg["llm_semantic_repair"] = {"enabled": True}
    scorer = Scorer()
    backend = LLMBackend(
        "qwen-fixture",
        "",
        "https://invalid.test",
        "qwen-fixture",
        rate_limit=0,
        compact_score_contract=True,
    )
    monkeypatch.setattr(scorer, "backends_for", lambda *a, **kw: [backend])
    rejected = json.dumps(
        dict(score=8, tag="公司公告", logic="甲公司订单4亿元", sentiment="看多")
    )
    requests = []

    def respond(**kwargs):
        requests.append(kwargs)
        return SimpleNamespace(
            choices=[SimpleNamespace(message=SimpleNamespace(content=rejected))],
            usage=None,
            model="qwen-fixture",
        )

    backend.client = SimpleNamespace(chat=SimpleNamespace(
        completions=SimpleNamespace(create=respond)
    ))

    @capture_article_score
    def invoke(ctx, result, scorer, summary):
        return scorer.score_stage1_with_identity(
            ctx.raw_title, summary, "prompt", ctx.cfg
        )

    with capture_llm_calls() as captured:
        result = invoke(ctx, None, scorer, "")
    assert result.model_name == "local_safe_neutral"
    assert result.semantic_resolution == "safe_neutral_grounding"
    assert result.neutral_reason == "facts"
    assert len(requests) == captured.total == 1
    persist_scoring_evidence(ctx.entry, _snapshot())
    record = json.loads(next((tmp_path / "evidence").glob("*.json")).read_text())
    validate_evidence(record)
    assert record["evidence_complete"] is True
    first, = record["attempts"]
    assert first["fact_failure_kind"] == "numeric_fact_failure"
    assert first["source_in_request"] is True
    assert first["attempt"] == "initial"
    assert first["model_name"] == "qwen-fixture"
    assert first["raw_response"] == rejected
    assert first["accepted"] is False
    assert first["failure_reason"] == "ungrounded_score_facts"
    assert first["repair_trigger"] == ""
