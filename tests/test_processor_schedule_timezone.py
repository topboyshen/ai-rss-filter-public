"""time_weight / quiet_mode windows are Beijing wall-clock, independent of TZ."""
import time
from datetime import datetime, timezone

import pytest

from core import processor

# 17:30 UTC == 01:30 next day in Beijing.
INSTANT = datetime(2026, 10, 16, 17, 30, tzinfo=timezone.utc)


@pytest.fixture
def utc_process(monkeypatch):
    monkeypatch.setenv("TZ", "UTC")
    time.tzset()

    class Frozen(datetime):
        @classmethod
        def now(cls, tz=None):
            return INSTANT.astimezone(tz) if tz else INSTANT.astimezone().replace(tzinfo=None)

    monkeypatch.setattr(processor, "datetime", Frozen)
    yield
    monkeypatch.undo()
    time.tzset()


def test_time_bonus_uses_beijing_hour(utc_process):
    cfg = {"time_weight": {"enabled": True, "rules": [
        {"start_hour": 0, "end_hour": 6, "bonus": 1.5, "label": "夜盘"},
        {"start_hour": 17, "end_hour": 18, "bonus": -9, "label": "utc-trap"},
    ]}}
    assert processor._get_time_bonus(cfg) == (1.5, "夜盘")


def test_quiet_mode_uses_beijing_minutes(utc_process):
    cfg = {"quiet_mode": {"enabled": True, "notify_threshold_raise": 1.0, "schedule": [
        {"start": "01:00", "end": "02:00"},
    ]}}
    quiet, _raise = processor._is_quiet_mode(cfg)
    assert quiet is True
    cfg["quiet_mode"]["schedule"] = [{"start": "17:00", "end": "18:00"}]
    assert processor._is_quiet_mode(cfg)[0] is False
