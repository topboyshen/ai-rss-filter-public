"""Processing attribution uses real scoring/persistence with offline SDK responses."""

import json
import re
import sqlite3
import threading
from collections import OrderedDict
from concurrent.futures import ThreadPoolExecutor
from types import SimpleNamespace

import httpx
import pytest
from feedparser import FeedParserDict
from openai import APITimeoutError

from core import processor
from core.scorer import LLMBackend, Scorer
from db import database
from services import news_processing_trace as tracing
from tests.test_scorer import POLLUTED_TECH_EXAMPLE, _valid_quality_circuit_response
from utils.helpers import FeedEntry


FEED = "https://feed.invalid/processing-trace"
LINK = "https://article.invalid/processing-trace"
TITLE = "company reports stronger demand"


def _response(content=None, *, usage=None, choices=None):
    return SimpleNamespace(
        choices=choices if choices is not None else [SimpleNamespace(
            message=SimpleNamespace(content=content or _valid_quality_circuit_response()),
            finish_reason="stop",
        )],
        usage=usage,
        model="sdk-reported-model",
    )


def _backend(name, respond):
    backend = LLMBackend(
        name=name, model=name + "-configured", api_key="", base_url="",
        rate_limit=0, structured_output_mode="json_schema",
    )
    backend.client = SimpleNamespace(chat=SimpleNamespace(
        completions=SimpleNamespace(create=respond),
    ))
    return backend


def _table(name):
    assert name in {"news_processing_logs", "llm_call_logs", "backtest_signals", "news_cache"}
    with database.get_connection() as conn:
        return [dict(row) for row in conn.execute(f"SELECT * FROM {name} ORDER BY rowid")]


def _process(scorer, cfg, link=LINK, *, title=TITLE, summary=""):
    return processor.process_entry(
        FeedParserDict({"title": title, "link": link, "summary": summary}),
        [], "prompt", cfg, scorer=scorer, feed_url=FEED,
    )


@pytest.fixture
def pipeline(monkeypatch):
    database.init_db()
    monkeypatch.setattr(processor, "_transport_retry_counts", OrderedDict())
    monkeypatch.setattr(processor, "_transport_pending_writes", {})
    monkeypatch.setattr(processor, "map_article_to_assets", lambda *_a, **_kw: {})
    monkeypatch.setattr(processor, "send_telegram_notification", lambda *_a, **_kw: None)
    scorer = Scorer()
    scorer.clear_runtime_cache()
    requests = []
    control = {"respond": lambda _name, _kwargs: _response()}

    def respond(name, kwargs):
        requests.append((name, kwargs))
        return control["respond"](name, kwargs)

    monkeypatch.setattr(scorer, "backends", [
        _backend("primary", lambda **kwargs: respond("primary", kwargs)),
        _backend("backup", lambda **kwargs: respond("backup", kwargs)),
    ])
    cfg = {
        "scoring_strategy_version": "processing-trace-fixture",
        "_blacklist_re": re.compile(r"$^"),
        "llm_semantic_repair": {"enabled": True},
        "llm_score_cache": {"enabled": False},
        "llm_routing": {"stage1": ["primary", "backup"], "summary": ["primary"]},
        "threshold": {"pass": 7.0, "notify": 10.0, "similarity": 0.9},
        "cache_rejected_entries": True,
        "generate_summary": False,
        "fast_pass": {"enabled": False},
        "two_stage_scoring": {"enabled": False},
        "feeds": [{"url": FEED, "tier": "primary_news"}],
    }
    yield SimpleNamespace(scorer=scorer, cfg=cfg, requests=requests, control=control)
    scorer.clear_runtime_cache()


def test_parallel_articles_keep_physical_calls_and_final_scores_separate(pipeline):
    state = pipeline
    barrier = threading.Barrier(2)

    def respond(_name, _kwargs):
        barrier.wait(timeout=3)
        return _response()

    state.control["respond"] = respond
    links = [LINK + "/first", LINK + "/second"]
    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(lambda link: _process(state.scorer, state.cfg, link), links))
    assert [reason for _, reason in results] == ["scored", "scored"]
    rows, calls = _table("news_processing_logs"), _table("llm_call_logs")
    assert len(rows) == len(calls) == len(state.requests) == 2
    assert len({row["processing_id"] for row in rows}) == 2
    assert len({row["request_id"] for row in calls}) == 2
    for row in rows:
        call, = [call for call in calls if call["processing_id"] == row["processing_id"]]
        assert row["article_key"] == call["article_key"] == database._backtest_signal_key(
            row["link"], state.cfg["scoring_strategy_version"],
        )
        assert row["state"] == "completed" and row["outcome"] == "scored"
        assert row["model_attempts"] == 1 and row["score"] == 8.0
        assert row["signal_key"] and row["aggregation_selected"] == 1
        assert row["signal_association"] == "written_in_processing"


def test_same_link_single_flight_records_one_paid_execution_and_one_reuse(pipeline):
    state = pipeline
    entered, release = threading.Event(), threading.Event()

    def respond(_name, _kwargs):
        entered.set()
        assert release.wait(timeout=3)
        return _response()

    state.control["respond"] = respond
    with ThreadPoolExecutor(max_workers=2) as pool:
        first = pool.submit(_process, state.scorer, state.cfg)
        assert entered.wait(timeout=3)
        second = pool.submit(_process, state.scorer, state.cfg)
        release.set()
        results = [first.result(timeout=5), second.result(timeout=5)]
    assert sorted(reason for _, reason in results) == ["cache", "scored"]
    rows, calls = _table("news_processing_logs"), _table("llm_call_logs")
    assert len(rows) == 2 and len(calls) == len(state.requests) == 1
    assert len({row["processing_id"] for row in rows}) == 2
    assert len({row["article_key"] for row in rows}) == 1
    paid, = [row for row in rows if row["model_attempts"] == 1]
    reused, = [row for row in rows if row["model_attempts"] == 0]
    assert calls[0]["processing_id"] == paid["processing_id"]
    assert reused["reuse_kind"] == "cache" and reused["outcome"] == "cache"
    assert paid["signal_key"] == reused["signal_key"]
    assert paid["signal_association"] == "written_in_processing"
    assert reused["signal_association"] == "existing_at_completion"
    assert len(_table("news_cache")) == len(_table("backtest_signals")) == 1


def test_cache_then_ledger_reuse_does_not_change_frozen_signal_or_add_calls(pipeline):
    state = pipeline
    assert _process(state.scorer, state.cfg)[1] == "scored"
    signal_before, = _table("backtest_signals")
    cache_before, = _table("news_cache")
    assert _process(state.scorer, state.cfg)[1] == "cache"
    assert _table("news_cache") == [cache_before]
    with database.get_connection() as conn:
        conn.execute("DELETE FROM news_cache WHERE link=?", (LINK,))
        conn.commit()
    assert _process(state.scorer, state.cfg) == (None, "filtered")
    assert _table("backtest_signals") == [signal_before]
    assert len(state.requests) == len(_table("llm_call_logs")) == 1
    rows = _table("news_processing_logs")
    assert [row["model_attempts"] for row in rows] == [1, 0, 0]
    assert [row["reuse_kind"] for row in rows] == ["", "cache", "ledger"]
    assert all(row["signal_key"] == signal_before["signal_key"] for row in rows)
    assert [row["signal_association"] for row in rows] == [
        "written_in_processing", "existing_at_completion", "existing_at_completion",
    ]


def test_runtime_score_cache_reuses_decision_without_borrowing_other_article_calls(pipeline):
    state = pipeline
    state.cfg["llm_score_cache"] = {"enabled": True, "ttl_seconds": 3600}
    assert _process(state.scorer, state.cfg, LINK + "/first")[1] == "scored"
    assert _process(state.scorer, state.cfg, LINK + "/second")[1] == "scored"
    rows, calls = _table("news_processing_logs"), _table("llm_call_logs")
    assert [row["model_attempts"] for row in rows] == [1, 0]
    assert len(calls) == len(state.requests) == 1
    assert len({row["article_key"] for row in rows}) == 2
    assert len({row["signal_key"] for row in rows}) == 2
    assert rows[1]["reuse_kind"] == "score_cache"
    assert all(row["signal_association"] == "written_in_processing" for row in rows)
    assert calls[0]["processing_id"] == rows[0]["processing_id"]
    assert database.DB.get_news_processing_trace(rows[1]["processing_id"])["calls"] == []


def test_local_filter_and_later_direct_model_call_do_not_inherit_news_identity(pipeline):
    state = pipeline
    state.cfg["_blacklist_re"] = re.compile("stronger")
    assert _process(state.scorer, state.cfg) == (None, "blacklist")
    row, = _table("news_processing_logs")
    assert row["state"] == "completed" and row["outcome"] == "blacklist"
    assert row["model_attempts"] == 0 and not row["signal_key"]
    assert _table("llm_call_logs") == []
    assert tracing.get_news_processing_call_metadata() == {}
    state.scorer._call_llm_result(state.scorer.backends[0], [], 100, purpose="digest", cfg={})
    call, = _table("llm_call_logs")
    assert not call["processing_id"] and not call["article_key"]
    assert call["request_id"] and call["attempt_kind"] == "direct"


def test_structured_format_retries_preserve_existing_request_identity(pipeline):
    state = pipeline

    def respond(_name, kwargs):
        if "response_format" in kwargs:
            raise ValueError("response_format not supported")
        return _response()

    state.control["respond"] = respond
    assert _process(state.scorer, state.cfg)[1] == "scored"
    row, = _table("news_processing_logs")
    calls = _table("llm_call_logs")
    assert row["model_attempts"] == len(calls) == len(state.requests) == 3
    assert {call["processing_id"] for call in calls} == {row["processing_id"]}
    assert {call["article_key"] for call in calls} == {row["article_key"]}
    assert len({call["request_id"] for call in calls}) == 1
    assert [call["response_mode"] for call in calls] == [
        "json_schema", "json_object", "prompt_only",
    ]
    assert [call["format_retry"] for call in calls] == [0, 1, 1]
    assert [call["success"] for call in calls] == [0, 0, 1]


def test_semantic_fallback_keeps_same_execution_and_final_ledger_score(pipeline):
    state = pipeline
    state.control["respond"] = lambda name, _kwargs: _response(
        POLLUTED_TECH_EXAMPLE if name == "primary" else _valid_quality_circuit_response(),
    )
    entry, reason = _process(state.scorer, state.cfg)
    assert reason == "scored" and entry.scoring_meta["model_name"] == "backup-configured"
    row, = _table("news_processing_logs")
    calls = _table("llm_call_logs")
    assert row["model_attempts"] == 2 and row["score"] == 8.0
    assert [call["attempt_kind"] for call in calls] == ["initial", "fallback"]
    assert len({call["request_id"] for call in calls}) == 1
    assert {call["processing_id"] for call in calls} == {row["processing_id"]}
    assert row["signal_key"] == _table("backtest_signals")[0]["signal_key"]


def test_stage1_and_stage2_share_execution_but_final_trace_uses_reviewed_score(pipeline):
    state = pipeline
    state.cfg["two_stage_scoring"] = {"enabled": True}
    state.cfg["llm_routing"]["stage1"] = ["primary"]
    state.cfg["llm_routing"]["stage2"] = ["backup"]

    def respond(name, _kwargs):
        body = json.loads(_valid_quality_circuit_response())
        body["score"] = 8.0 if name == "primary" else 6.0
        return _response(json.dumps(body))

    state.control["respond"] = respond
    assert _process(state.scorer, state.cfg) == (None, "filtered")
    row, = _table("news_processing_logs")
    calls = _table("llm_call_logs")
    signal, = _table("backtest_signals")
    assert row["model_attempts"] == len(calls) == len(state.requests) == 2
    assert [call["purpose"] for call in calls] == ["stage1", "stage2"]
    assert len({call["request_id"] for call in calls}) == 2
    assert {call["processing_id"] for call in calls} == {row["processing_id"]}
    assert {call["article_key"] for call in calls} == {row["article_key"]}
    assert row["outcome"] == "filtered" and row["state"] == "completed"
    assert row["score"] == signal["score"] == 6.0
    assert row["score_path"] == signal["score_path"] == "stage2"
    assert row["aggregation_selected"] == signal["selected"] == 0
    assert row["signal_key"] == signal["signal_key"]
    assert row["signal_association"] == "written_in_processing"


def test_summary_repair_shares_news_execution_but_has_its_own_request_id(pipeline):
    state = pipeline
    state.cfg["generate_summary"] = True
    summary_attempts = []

    def respond(_name, kwargs):
        if "正文：" not in kwargs["messages"][-1]["content"]:
            return _response()
        summary_attempts.append(True)
        return _response("invalid" if len(summary_attempts) == 1 else json.dumps({"summary": TITLE}))

    state.control["respond"] = respond
    entry, reason = _process(state.scorer, state.cfg, summary=(TITLE + ". ") * 8)
    assert reason == "scored" and entry.ai_summary == TITLE
    row, = _table("news_processing_logs")
    calls = _table("llm_call_logs")
    assert row["model_attempts"] == len(calls) == 3
    assert {call["processing_id"] for call in calls} == {row["processing_id"]}
    assert [call["attempt_kind"] for call in calls] == ["initial", "initial", "repair"]
    assert [call["purpose"] for call in calls] == ["stage1", "summary", "summary"]
    assert calls[1]["request_id"] == calls[2]["request_id"] != calls[0]["request_id"]


def test_deferred_retry_reuses_article_key_but_not_execution_or_request_id(pipeline):
    state = pipeline
    control = {"recover": False}

    def respond(name, _kwargs):
        if name == "primary":
            return _response(POLLUTED_TECH_EXAMPLE)
        if not control["recover"]:
            raise APITimeoutError(request=httpx.Request("POST", "https://offline.invalid/v1"))
        return _response()

    state.control["respond"] = respond
    assert _process(state.scorer, state.cfg) == (None, processor.SCORE_DEFERRED_REASON)
    deferred, = _table("news_processing_logs")
    assert deferred["state"] == "deferred" and not deferred["signal_key"]
    assert _table("news_cache") == _table("backtest_signals") == []
    control["recover"] = True
    assert _process(state.scorer, state.cfg)[1] == "scored"
    rows, calls = _table("news_processing_logs"), _table("llm_call_logs")
    assert [row["state"] for row in rows] == ["deferred", "completed"]
    assert [row["model_attempts"] for row in rows] == [2, 2]
    assert len({row["article_key"] for row in rows}) == 1
    assert len({row["processing_id"] for row in rows}) == 2
    assert len({call["request_id"] for call in calls}) == 2
    assert len(_table("backtest_signals")) == len(_table("news_cache")) == 1


def test_pending_transport_decision_write_retry_has_no_new_model_calls(pipeline, monkeypatch):
    state = pipeline

    def respond(name, _kwargs):
        if name == "primary":
            return _response(POLLUTED_TECH_EXAMPLE)
        raise APITimeoutError(request=httpx.Request("POST", "https://offline.invalid/v1"))

    state.control["respond"] = respond
    for _ in range(2):
        assert _process(state.scorer, state.cfg) == (None, processor.SCORE_DEFERRED_REASON)
    original_write = processor.DB.set_cached_entry
    original_error = sqlite3.OperationalError("pending terminal decision could not commit")

    def unavailable(*_args, **_kwargs):
        raise original_error

    monkeypatch.setattr(processor.DB, "set_cached_entry", unavailable)
    with pytest.raises(sqlite3.OperationalError) as caught:
        _process(state.scorer, state.cfg)
    assert caught.value is original_error
    rows_before = _table("news_processing_logs")
    assert [row["state"] for row in rows_before] == ["deferred", "deferred", "error"]
    assert [row["model_attempts"] for row in rows_before] == [2, 2, 2]
    assert all(row["signal_key"] is None for row in rows_before)
    assert _table("backtest_signals") == _table("news_cache") == []
    assert len(state.requests) == 6 and len(processor._transport_pending_writes) == 1
    monkeypatch.setattr(processor.DB, "set_cached_entry", original_write)
    assert _process(state.scorer, state.cfg) == (None, "filtered")
    rows, calls = _table("news_processing_logs"), _table("llm_call_logs")
    signal, = _table("backtest_signals")
    final = rows[-1]
    assert len(state.requests) == len(calls) == 6
    assert len({row["article_key"] for row in rows}) == 1
    assert len({row["processing_id"] for row in rows}) == 4
    assert len({call["request_id"] for call in calls}) == 3
    assert final["state"] == "completed" and final["model_attempts"] == 0
    assert final["signal_key"] == signal["signal_key"] == final["article_key"]
    assert final["score"] == signal["score"] == 4.0
    assert final["score_path"] == signal["score_path"] == "llm_safe_neutral_grounding"
    assert final["aggregation_selected"] == 0
    assert final["signal_association"] == "written_in_processing"
    assert database.DB.get_news_processing_trace(final["processing_id"])["calls"] == []
    assert processor._transport_retry_counts == processor._transport_pending_writes == {}
    assert _table("news_processing_logs")[:3] == rows_before


@pytest.mark.parametrize("usage,status,reported_prompt,reported_total", [
    (None, "unavailable", 0, 0),
    (SimpleNamespace(prompt_tokens=123), "partial", 123, 0),
    (SimpleNamespace(prompt_tokens=123, completion_tokens=45), "complete", 123, 168),
    (SimpleNamespace(prompt_tokens=0, completion_tokens=0), "complete", 0, 0),
])
def test_trace_reports_missing_usage_as_unknown_or_partial(
    pipeline, usage, status, reported_prompt, reported_total,
):
    state = pipeline
    state.control["respond"] = lambda _name, _kwargs: _response(usage=usage)
    assert _process(state.scorer, state.cfg)[1] == "scored"
    row, = _table("news_processing_logs")
    trace = database.DB.get_news_processing_trace(row["processing_id"])
    assert trace["found"] and trace["calls_linked"] == 1 and not trace["truncated"]
    assert trace["calls"][0]["usage_status"] == status
    totals = trace["usage"]
    assert totals["complete_calls"] == int(status == "complete")
    assert totals["partial_calls"] == int(status == "partial")
    assert totals["unknown_calls"] == int(status == "unavailable")
    assert totals["reported_prompt_tokens"] == reported_prompt
    assert totals["reported_total_tokens"] == reported_total
    assert totals["unknown_or_partial"] == (status != "complete")


def test_known_usage_on_malformed_sdk_response_survives_failed_execution(pipeline):
    state = pipeline
    usage = SimpleNamespace(prompt_tokens=123, completion_tokens=45, total_tokens=168)
    backend = _backend("malformed", lambda **_kwargs: _response(usage=usage, choices=[]))
    with pytest.raises(IndexError):
        with tracing.trace_news_processing(FeedEntry(TITLE, LINK), state.cfg, FEED):
            state.scorer._call_llm_result(backend, [], 100, purpose="stage1", cfg=state.cfg)
    row, = _table("news_processing_logs")
    assert row["state"] == "error" and row["error_type"] == "IndexError"
    assert row["model_attempts"] == 1 and not row["signal_key"]
    trace = database.DB.get_news_processing_trace(row["processing_id"])
    assert trace["calls"][0]["success"] == 0
    assert trace["usage"]["complete_calls"] == 1
    assert trace["usage"]["reported_total_tokens"] == 168
    assert not trace["usage"]["unknown_or_partial"]
    assert tracing.get_news_processing_call_metadata() == {}


def test_observation_write_failure_keeps_original_score_and_frozen_ledger(pipeline, monkeypatch):
    state = pipeline

    def fail_observation(_row):
        raise sqlite3.OperationalError("observation unavailable")

    monkeypatch.setattr(database.DB, "record_news_processing", fail_observation)
    entry, reason = _process(state.scorer, state.cfg)
    assert reason == "scored" and entry.scoring_meta["score_path"] == "llm"
    cache, = _table("news_cache")
    signal, = _table("backtest_signals")
    assert cache["score"] == signal["score"] == 8.0
    assert len(state.requests) == len(_table("llm_call_logs")) == 1
    assert _table("news_processing_logs") == []
    assert _process(state.scorer, state.cfg)[1] == "cache"
    assert _table("backtest_signals") == [signal]


def test_observation_failure_preserves_original_processing_exception(pipeline, monkeypatch):
    state = pipeline
    original = RuntimeError("original processing write failure")

    def fail_score(*_args, **_kwargs):
        raise original

    def fail_observation(_row):
        raise ValueError("diagnostic storage failure")

    monkeypatch.setattr(processor.DB, "set_cached_entry", fail_score)
    monkeypatch.setattr(database.DB, "record_news_processing", fail_observation)
    with pytest.raises(RuntimeError) as caught:
        _process(state.scorer, state.cfg)
    assert caught.value is original
    assert len(_table("llm_call_logs")) == 1
    assert _table("news_cache") == _table("backtest_signals") == []
    assert tracing.get_news_processing_call_metadata() == {}


@pytest.mark.parametrize("terminal", ["deferred", "error"])
def test_unfinished_execution_never_borrows_existing_signal(pipeline, terminal):
    state = pipeline
    assert _process(state.scorer, state.cfg)[1] == "scored"
    previous, = _table("backtest_signals")
    if terminal == "error":
        with pytest.raises(RuntimeError, match="still unfinished"):
            with tracing.trace_news_processing(FeedEntry(TITLE, LINK), state.cfg, FEED):
                raise RuntimeError("still unfinished")
    else:
        with tracing.trace_news_processing(FeedEntry(TITLE, LINK), state.cfg, FEED) as trace:
            trace["result"] = (None, processor.SCORE_DEFERRED_REASON)
    row = _table("news_processing_logs")[-1]
    assert row["state"] == terminal and row["model_attempts"] == 0
    assert row["article_key"] == previous["signal_key"]
    assert row["signal_key"] is None and row["score"] is None
    assert row["score_path"] is None and row["aggregation_selected"] is None
    assert row["signal_association"] == "none"
    assert _table("backtest_signals") == [previous]


def test_trace_initialization_failure_keeps_original_score_without_stale_context(pipeline, monkeypatch):
    state = pipeline

    def unavailable(*_args):
        raise RuntimeError("diagnostic initialization unavailable")

    monkeypatch.setattr(tracing, "_new_processing_state", unavailable)
    entry, reason = _process(state.scorer, state.cfg)
    assert reason == "scored" and entry.scoring_meta["score_path"] == "llm"
    signal, = _table("backtest_signals")
    assert signal["score"] == 8.0 and signal["selected"] == 1
    call, = _table("llm_call_logs")
    assert not call["processing_id"] and not call["article_key"]
    assert _table("news_processing_logs") == []
    assert tracing.get_news_processing_call_metadata() == {}


def test_local_sdk_preparation_failure_is_not_counted_as_model_attempt(pipeline):
    state = pipeline
    backend = _backend("no-client", lambda **_kwargs: pytest.fail("must not dispatch"))
    backend.client = None
    with pytest.raises(RuntimeError, match="no client"):
        with tracing.trace_news_processing(FeedEntry(TITLE, LINK), state.cfg, FEED):
            state.scorer._call_llm_result(backend, [], 100, cfg=state.cfg)
    row, = _table("news_processing_logs")
    assert row["state"] == "error" and row["model_attempts"] == 0
    assert not row["signal_key"] and row["signal_association"] == "none"
    assert _table("llm_call_logs") == []


def test_nested_trace_initialization_failure_does_not_charge_outer_article(pipeline, monkeypatch):
    state = pipeline
    original = tracing._new_processing_state

    def unavailable_for_inner(entry, cfg, feed_url):
        if entry.link == LINK + "/inner":
            raise RuntimeError("inner diagnostic unavailable")
        return original(entry, cfg, feed_url)

    monkeypatch.setattr(tracing, "_new_processing_state", unavailable_for_inner)
    with tracing.trace_news_processing(FeedEntry(TITLE, LINK), state.cfg, FEED) as outer:
        outer_id = tracing.get_news_processing_call_metadata()["processing_id"]
        with tracing.trace_news_processing(FeedEntry(TITLE, LINK + "/inner"), state.cfg, FEED):
            assert tracing.get_news_processing_call_metadata() == {}
            state.scorer._call_llm_result(state.scorer.backends[0], [], 100, cfg=state.cfg)
        assert tracing.get_news_processing_call_metadata()["processing_id"] == outer_id
        state.scorer._call_llm_result(state.scorer.backends[0], [], 100, cfg=state.cfg)
        outer["result"] = (None, "filtered")
    row, = _table("news_processing_logs")
    calls = _table("llm_call_logs")
    assert row["processing_id"] == outer_id and row["model_attempts"] == 1
    assert not calls[0]["processing_id"] and not calls[0]["article_key"]
    assert calls[1]["processing_id"] == outer_id
    assert tracing.get_news_processing_call_metadata() == {}


@pytest.mark.parametrize("failure", [KeyboardInterrupt("interrupted"), GeneratorExit("closed")])
def test_trace_finally_preserves_base_exception_and_clears_context(pipeline, failure):
    state = pipeline
    with pytest.raises(type(failure)) as caught:
        with tracing.trace_news_processing(FeedEntry(TITLE, LINK), state.cfg, FEED):
            raise failure
    assert caught.value is failure
    row, = _table("news_processing_logs")
    assert row["state"] == "error" and row["error_type"] == type(failure).__name__
    assert row["model_attempts"] == 0 and row["signal_association"] == "none"
    assert tracing.get_news_processing_call_metadata() == {}
