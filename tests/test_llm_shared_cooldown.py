"""Offline throttle/failover regressions: no production credentials or model calls."""

import threading
from concurrent.futures import ThreadPoolExecutor
from types import SimpleNamespace

import pytest

from core import scorer as module
from core.scorer import LLMBackend, RateLimitTimeoutException, Scorer, capture_llm_calls


class Clock:
    def __init__(self):
        self.now = 1000.0
        self.sleeps = []
        self.on_sleep = None

    def monotonic(self):
        return self.now

    def sleep(self, seconds):
        self.sleeps.append(seconds)
        self.now += seconds
        if self.on_sleep:
            hook, self.on_sleep = self.on_sleep, None
            hook()


@pytest.fixture
def cooldown(monkeypatch):
    state = module._RemoteBackendCooldowns()
    monkeypatch.setattr(module, "_REMOTE_BACKEND_COOLDOWNS", state)
    return state


@pytest.fixture
def clock(monkeypatch):
    clock = Clock()
    monkeypatch.setattr(module.time, "monotonic", clock.monotonic)
    monkeypatch.setattr(module.time, "sleep", clock.sleep)
    monkeypatch.setattr(module.time, "time", clock.monotonic)
    return clock


def backend(name="primary", *, key="test-only", url="https://backend.invalid/v1", wait=8):
    item = LLMBackend(
        name=name, api_key="", base_url=url, model=name,
        rate_limit=0, max_wait_seconds=wait,
    )
    # Avoid constructing any network client; this is a harmless synthetic identity.
    item.api_key = key
    return item


def remote_error(value=None, *, body=None):
    return SimpleNamespace(
        status_code=429,
        response=SimpleNamespace(headers={} if value is None else {"retry-after": value}),
        body=body,
    )


def test_shared_identity_ignores_task_name_model_and_trailing_slash(cooldown, clock):
    first = backend("score")
    other = backend("summary", url="https://backend.invalid/v1/")
    cooldown.defer(first, remote_error("10"))
    assert cooldown.identity(first) == cooldown.identity(other)
    assert "test-only" not in repr(cooldown.deadlines)
    with pytest.raises(RateLimitTimeoutException):
        other.rate_limit_wait()
    assert clock.sleeps == []
    for independent in (backend(key="another"), backend(url="https://other.invalid/v1")):
        independent.rate_limit_wait()
    assert clock.sleeps == []


@pytest.mark.parametrize("value", ["90", "86400", "1e6"])
def test_long_valid_retry_after_is_not_truncated_or_retried(cooldown, clock, value):
    item = backend()
    cooldown.defer(item, remote_error(value))
    assert cooldown.deadlines[cooldown.identity(item)][0] == clock.now + float(value)
    with pytest.raises(RateLimitTimeoutException):
        item.rate_limit_wait()
    assert clock.sleeps == []


@pytest.mark.parametrize("value", ["nan", "inf", "-5", "garbage", "1e400", None])
def test_invalid_retry_after_uses_finite_local_backoff(cooldown, clock, value):
    item = backend()
    assert cooldown.defer(item, remote_error(value)) == 1.5
    item.rate_limit_wait()
    assert clock.sleeps == [1.5]


def test_retry_after_http_date_is_respected(monkeypatch):
    from datetime import datetime, timezone

    class FixedDateTime:
        @staticmethod
        def now(_tz):
            return datetime(2026, 10, 10, 12, 0, 0, tzinfo=timezone.utc)

    monkeypatch.setattr(module, "datetime", FixedDateTime)
    assert module._remote_retry_after_seconds(remote_error("Sat, 10 Oct 2026 12:02:00 GMT")) == 120
    assert module._remote_retry_after_seconds(remote_error("Sat, 10 Oct 2026 11:58:00 GMT")) == 0


def test_wait_rechecks_extension_but_does_not_reset_total_budget(cooldown, clock):
    item = backend(wait=4)
    cooldown.defer(item, remote_error("3"))
    clock.on_sleep = lambda: cooldown.defer(item, remote_error("3"))
    with pytest.raises(RateLimitTimeoutException):
        item.rate_limit_wait()
    assert clock.sleeps == [3]
    assert clock.now == 1003


def test_default_cooldown_escalates_and_success_does_not_clear_active_wait(cooldown, clock):
    item = backend()
    assert cooldown.defer(item, remote_error()) == 1.5
    item.rate_limit_wait()
    assert cooldown.defer(item, remote_error()) == 3
    cooldown.record_success(item)
    item.rate_limit_wait()
    assert clock.sleeps == [1.5, 3]
    assert cooldown.defer(item, remote_error()) == 1.5


def test_successful_dynamic_identities_do_not_accumulate_state(cooldown, clock):
    for number in range(200):
        cooldown.record_success(backend(key=f"fake-{number}"))
    assert cooldown.deadlines == {}
    item = backend()
    cooldown.defer(item, remote_error("2"))
    clock.now += 2
    cooldown.record_success(item)
    assert cooldown.deadlines == {}


def test_local_slot_and_remote_cooldown_share_one_wait_budget(cooldown, clock):
    item = backend(wait=4)
    item.next_available_at = clock.now + 3
    cooldown.defer(item, remote_error("5"))
    with pytest.raises(RateLimitTimeoutException):
        item.rate_limit_wait()
    assert clock.sleeps == [3]


@pytest.mark.parametrize("code", [
    "insufficient_quota", "credit_balance_exhausted", "organization_spend_limit_exceeded",
    "project_spend_limit_exceeded", "organization_usage_limit_exceeded", "insufficient_balance",
])
def test_quota_429_is_not_temporary_rate_limit(code):
    error = remote_error(body={"error": {"code": code}})
    assert LLMBackend.is_rate_limit_error(error) is False


def _response():
    return SimpleNamespace(
        choices=[SimpleNamespace(message=SimpleNamespace(
            content='{"score":8,"tag":"创新药","logic":"甲公司创新药项目获得订单","sentiment":"看多"}',
        ), finish_reason="stop")],
        usage=SimpleNamespace(prompt_tokens=10, completion_tokens=10, total_tokens=20),
        model="fake",
    )


def test_actual_shared_entry_blocks_parallel_tasks_but_other_backend_runs(cooldown, monkeypatch):
    import time

    first, same, other = backend(), backend("summary"), backend(url="https://other.invalid/v1")
    calls = []
    for item in (first, same, other):
        item.client = SimpleNamespace(chat=SimpleNamespace(completions=SimpleNamespace(
            create=lambda **kw: (calls.append(kw["model"]) or _response()),
        )))
    cooldown.defer(first, remote_error("120"))
    scorer = object.__new__(Scorer)
    monkeypatch.setattr(scorer, "_log_llm_call", lambda *a, **k: None)
    started = threading.Barrier(3)

    def call(item):
        started.wait(timeout=3)
        with capture_llm_calls() as capture:
            try:
                scorer._call_llm_result(item, [], 50, purpose="summary", cfg={})
                return "success", capture.total
            except RateLimitTimeoutException:
                return "deferred", capture.total

    start = time.monotonic()
    with ThreadPoolExecutor(max_workers=3) as pool:
        results = list(pool.map(call, (first, same, other)))
    assert results == [("deferred", 0), ("deferred", 0), ("success", 1)]
    assert time.monotonic() - start < 2
    assert len(calls) == 1


def test_real_call_error_publishes_cooldown_then_scores_on_existing_fallback(cooldown, monkeypatch):
    from openai import APIError

    class Limited(APIError):
        status_code = 429

    limited = Limited("temporary rate limit", None, body={"code": "slow_down"})
    limited.response = SimpleNamespace(headers={"retry-after": "90"})
    first, other = backend(), backend("fallback", url="https://other.invalid/v1")
    calls = []

    def reject(**_kw):
        calls.append("primary")
        raise limited

    first.client = SimpleNamespace(chat=SimpleNamespace(completions=SimpleNamespace(create=reject)))
    other.client = SimpleNamespace(chat=SimpleNamespace(completions=SimpleNamespace(
        create=lambda **kw: (calls.append("fallback") or _response()),
    )))
    scorer = object.__new__(Scorer)
    scorer.backends = [first, other]
    monkeypatch.setattr(scorer, "_log_llm_call", lambda *a, **k: None)
    cfg = {"llm_semantic_repair": {"enabled": False}}
    result = scorer.score_stage1_with_identity(
        "甲公司创新药项目获得订单", "甲公司创新药项目获得订单", "提示词", cfg,
    )
    assert result.values[0] == 8
    assert result.model_name == other.name
    assert calls == ["primary", "fallback"]
    assert first.fail_count == 0
    with capture_llm_calls() as capture:
        with pytest.raises(RateLimitTimeoutException):
            scorer._call_llm_result(first, [], 50, cfg=cfg)
    assert capture.total == 0
    assert calls == ["primary", "fallback"]


def test_sdk_preparation_does_not_bypass_a_new_shared_cooldown(cooldown, monkeypatch):
    item = backend()
    calls = []

    class Completions:
        @property
        def create(self):
            cooldown.defer(item, remote_error("90"))
            return lambda **_kw: (calls.append("sent") or _response())

    item.client = SimpleNamespace(chat=SimpleNamespace(completions=Completions()))
    scorer = object.__new__(Scorer)
    monkeypatch.setattr(scorer, "_log_llm_call", lambda *a, **k: None)
    with capture_llm_calls() as capture:
        with pytest.raises(RateLimitTimeoutException):
            scorer._call_llm_result(item, [], 50, cfg={})
    assert capture.total == 0
    assert calls == []
    secondary = backend("fallback", url="https://other.invalid/v1")
    secondary.client = SimpleNamespace(chat=SimpleNamespace(completions=SimpleNamespace(
        create=lambda **kw: (calls.append("fallback") or _response()),
    )))
    # Clear the first check's state, so this time the second check during
    # request preparation (inside the try block) is what causes failover.
    cooldown.deadlines.clear()
    scorer.backends = [item, secondary]
    result = scorer.score_stage1_with_identity(
        "甲公司创新药项目获得订单", "甲公司创新药项目获得订单", "提示词",
        {"llm_semantic_repair": {"enabled": False}},
    )
    assert result.model_name == "fallback"
    assert result.values[0] == 8
    assert item.fail_count == 0
    assert calls == ["fallback"]
