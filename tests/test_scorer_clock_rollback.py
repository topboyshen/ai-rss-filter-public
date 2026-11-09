"""墙钟回拨不能让评分链路永久降级。

限流排队、失败衰减、评分缓存 TTL 都是进程内间隔，必须用单调时钟。用墙钟时，
一次向后的时间跳变（NTP 校正、VM 快照恢复、宿主时钟调整）会让排队等待恒大于
预算、失败计数永不衰减、缓存永不过期，而且不会自愈。
"""

import threading
from collections import OrderedDict

import pytest

from core import scorer as module
from core.scorer import LLMBackend, Scorer, ScoringResult


class Clock:
    """分叉的墙钟/单调钟：可以单独回拨墙钟来模拟时间跳变。"""

    def __init__(self):
        self.mono = 1000.0
        self.wall = 1_700_000_000.0
        self.sleeps = []

    def monotonic(self):
        return self.mono

    def time(self):
        return self.wall

    def sleep(self, seconds):
        self.sleeps.append(seconds)
        self.mono += seconds


@pytest.fixture
def clock(monkeypatch):
    clock = Clock()
    monkeypatch.setattr(module.time, "monotonic", clock.monotonic)
    monkeypatch.setattr(module.time, "sleep", clock.sleep)
    monkeypatch.setattr(module.time, "time", clock.time)
    monkeypatch.setattr(
        module, "_REMOTE_BACKEND_COOLDOWNS", module._RemoteBackendCooldowns()
    )
    return clock


def _backend(**overrides):
    values = {
        "name": "primary",
        "api_key": "test-only",
        "base_url": "https://backend.invalid/v1",
        "model": "primary",
        "rate_limit": 5,
        "max_wait_seconds": 8,
    }
    values.update(overrides)
    return LLMBackend(**values)


def _cache_only_scorer(ttl=60.0):
    """A Scorer with just the score-cache state; no backends or network clients."""
    scorer = object.__new__(Scorer)
    scorer._score_cache = OrderedDict()
    scorer._score_cache_lock = threading.Lock()
    scorer._score_cache_ttl = ttl
    scorer._score_cache_max = 10
    return scorer


def test_rate_limit_queue_ignores_wall_clock_rollback(clock):
    backend = _backend()
    backend.rate_limit_wait()  # 预约 5 秒的最小调用间隔

    clock.wall -= 3600  # 墙钟回拨一小时

    backend.rate_limit_wait()

    # 真实需要等的只有预约的 5 秒，不是回拨出来的 3600 秒。
    assert clock.sleeps == [5.0]


def test_failure_decay_uses_elapsed_time_not_wall_clock(clock):
    backend = _backend(max_fail_count=1, fail_decay_seconds=300.0)
    backend.record_fail()
    assert backend.is_healthy() is False

    clock.wall -= 3600  # 回拨
    clock.mono += 301  # 真实流逝超过衰减间隔

    assert backend.is_healthy() is True
    assert backend.fail_count == 0


def test_score_cache_expires_by_elapsed_time_after_wall_clock_rollback(clock):
    scorer = _cache_only_scorer(ttl=60.0)
    result = ScoringResult(
        values=(8.0, "科技", "标题", "摘要"), model_name="primary"
    )
    scorer._set_cached_score("key", result)

    clock.wall -= 3600
    clock.mono += 61

    assert scorer._get_cached_score("key") is None


def test_score_cache_still_serves_within_ttl_after_wall_clock_rollback(clock):
    scorer = _cache_only_scorer(ttl=60.0)
    result = ScoringResult(
        values=(8.0, "科技", "标题", "摘要"), model_name="primary"
    )
    scorer._set_cached_score("key", result)

    clock.wall -= 3600
    clock.mono += 30

    assert scorer._get_cached_score("key") is result
