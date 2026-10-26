"""Offline SDK stubs: physical attempts, not final scores or billed totals."""

from concurrent.futures import ThreadPoolExecutor
from types import SimpleNamespace

import pytest

from core.scorer import LLMBackend, Scorer, _SCORE_RESPONSE_SCHEMA, capture_llm_calls
from db import database


def _response(content="invalid", usage=None, choices=None):
    return SimpleNamespace(
        choices=choices if choices is not None else [
            SimpleNamespace(message=SimpleNamespace(content=content))
        ],
        usage=usage, model="reported-model",
    )


def _backend(name, respond):
    backend = LLMBackend(
        name=name, model=name + "-configured", api_key="", base_url="",
        rate_limit=0, structured_output_mode="json_schema",
    )
    backend.client = SimpleNamespace(chat=SimpleNamespace(
        completions=SimpleNamespace(create=respond)
    ))
    return backend


def _rows():
    with database.get_connection() as conn:
        return [dict(row) for row in conn.execute("SELECT * FROM llm_call_logs ORDER BY id")]


@pytest.mark.parametrize("broken", ["empty_choices", "null_content"])
def test_known_usage_survives_response_content_failure(broken):
    database.init_db()
    usage = SimpleNamespace(prompt_tokens=123, completion_tokens=45, total_tokens=168)
    response = _response(content=None, usage=usage, choices=[] if broken == "empty_choices" else None)
    backend = _backend("broken", lambda **_kwargs: response)
    scorer = object.__new__(Scorer)

    with pytest.raises((IndexError, AttributeError)):
        scorer._call_llm_result(
            backend, [], 100, purpose="stage1", cfg={
                "scoring_strategy_version": "fixture",
                "llm_pricing": {"reported-model": {
                    "input_per_million": 1, "output_per_million": 2,
                }},
            },
        )

    row, = _rows()
    assert row["success"] == 0
    assert row["model_name"] == "reported-model"
    assert (row["prompt_tokens"], row["completion_tokens"], row["total_tokens"]) == (123, 45, 168)
    assert row["estimated_cost"] == pytest.approx(0.000213)
    assert row["usage_status"] == "complete"
    assert row["request_id"] and row["attempt_kind"] == "direct"
    assert database.get_stats()[1] == 1


@pytest.mark.parametrize("usage,status,total", [
    (None, "unavailable", 0),
    (SimpleNamespace(prompt_tokens=123), "partial", 0),
    (SimpleNamespace(prompt_tokens=123, completion_tokens=45), "complete", 168),
    (SimpleNamespace(prompt_tokens=0, completion_tokens=0), "complete", 0),
    (SimpleNamespace(prompt_tokens=123, completion_tokens=45, total_tokens=200), "complete", 200),
    (SimpleNamespace(prompt_tokens=-1, completion_tokens=float("inf")), "unavailable", 0),
])
def test_usage_completeness_and_missing_total_are_not_false_zero(usage, status, total):
    database.init_db()
    backend = _backend("usage", lambda **_kwargs: _response("content", usage))
    result = object.__new__(Scorer)._call_llm_result(backend, [], 100, cfg={})
    row, = _rows()
    assert result.usage_status == row["usage_status"] == status
    assert result.usage["total_tokens"] == row["total_tokens"] == total
    assert row["success"] == 1


def test_structured_format_retries_keep_correlated_but_distinct_attempt_rows():
    database.init_db()
    requests = []

    def respond(**kwargs):
        requests.append(kwargs)
        if "response_format" in kwargs:
            raise ValueError("response_format not supported")
        return _response("content")

    scorer = object.__new__(Scorer)
    backend = _backend("formats", respond)
    assert scorer._call_structured_llm(
        backend, [], 100, "stage1", _SCORE_RESPONSE_SCHEMA, cfg={},
    ) == "content"
    rows = _rows()
    assert len(requests) == len(rows) == database.get_stats()[1] == 3
    assert len({row["id"] for row in rows}) == 3
    assert len({row["request_id"] for row in rows}) == 1
    assert rows[0]["request_id"]
    assert [row["response_mode"] for row in rows] == ["json_schema", "json_object", "prompt_only"]
    assert [row["format_retry"] for row in rows] == [0, 1, 1]
    assert [row["success"] for row in rows] == [0, 0, 1]
    assert all(row["usage_status"] == "unavailable" for row in rows)
    # Cached format capability avoids retries in the next logical request.
    scorer._call_structured_llm(backend, [], 100, "stage1", _SCORE_RESPONSE_SCHEMA, cfg={})
    next_row = _rows()[-1]
    assert next_row["request_id"] != rows[0]["request_id"]
    assert next_row["format_retry"] == 0


def test_scoring_backend_attribution_and_cache_local_neutral_have_no_extra_rows(monkeypatch):
    database.init_db()
    first = _backend("first", lambda **_kwargs: _response("invalid"))
    second = _backend("second", lambda **_kwargs: _response("invalid"))
    scorer = Scorer()
    scorer.clear_runtime_cache()
    monkeypatch.setattr(scorer, "backends", [first, second])
    cfg = {
        "scoring_strategy_version": "fixture",
        "llm_semantic_repair": {"enabled": True},
        "llm_score_cache": {"enabled": True, "ttl_seconds": 3600},
        "llm_routing": {"stage1": ["first", "second"]},
    }
    result = scorer.score_stage1_with_identity("company update", "report", "prompt", cfg)
    assert result.model_name == "local_safe_neutral"
    rows = _rows()
    assert len(rows) == database.get_stats()[1] == 2
    assert [row["attempt_kind"] for row in rows] == ["initial", "fallback"]
    assert len({row["request_id"] for row in rows}) == 1
    assert all(row["purpose"] == "stage1" for row in rows)
    assert all(row["model_name"] == "reported-model" for row in rows)
    assert scorer.score_stage1_with_identity("company update", "report", "prompt", cfg) == result
    assert len(_rows()) == 2
    scorer.clear_runtime_cache()


def test_summary_repair_is_still_charged_and_attributed_separately(monkeypatch):
    database.init_db()
    replies = iter(["invalid", '{"summary":"公司公布经营信息"}'])
    backend = _backend("summary", lambda **_kwargs: _response(next(replies)))
    scorer = Scorer()
    monkeypatch.setattr(scorer, "backends", [backend])
    result = scorer.summarize("公司公布经营信息", "公司公布经营信息。" * 12, {
        "generate_summary": True, "llm_semantic_repair": {"enabled": True},
        "llm_routing": {"summary": ["summary"]},
    })
    assert result == "公司公布经营信息"
    rows = _rows()
    assert len(rows) == 2
    assert [row["attempt_kind"] for row in rows] == ["initial", "repair"]
    assert len({row["request_id"] for row in rows}) == 1
    assert all(row["purpose"] == "summary" for row in rows)


def test_request_attribution_does_not_leak_between_parallel_or_direct_calls(monkeypatch):
    database.init_db()
    backend = _backend("parallel", lambda **_kwargs: _response("invalid"))
    scorer = Scorer()
    monkeypatch.setattr(scorer, "backends", [backend])
    cfg = {"llm_semantic_repair": {"enabled": True}}
    with ThreadPoolExecutor(max_workers=2) as executor:
        results = list(executor.map(
            lambda number: scorer.score_stage1_with_identity(str(number), "", "prompt", cfg),
            range(2),
        ))
    assert all(result.model_name == "local_safe_neutral" for result in results)
    scorer._call_llm_result(backend, [], 100, purpose="digest", cfg={})
    rows = _rows()
    assert len(rows) == 3
    assert len({row["request_id"] for row in rows}) == 3
    assert [row["attempt_kind"] for row in rows] == ["initial", "initial", "direct"]


@pytest.mark.parametrize("failure", ["no_client", "request_preparation"])
def test_local_preparation_failure_is_not_a_paid_sdk_attempt(monkeypatch, failure):
    database.init_db()
    requests = []
    backend = _backend("local", lambda **_kwargs: requests.append(1))
    if failure == "no_client":
        backend.client = None
    else:
        def unavailable():
            raise RuntimeError("local request preparation failed")
        monkeypatch.setattr(backend, "request_extra_body", unavailable)
    with capture_llm_calls() as calls:
        with pytest.raises(RuntimeError):
            object.__new__(Scorer)._call_llm_result(backend, [], 100, cfg={})
    assert requests == []
    assert calls.total == 0
    assert _rows() == []
    assert database.get_stats()[1] == 0
