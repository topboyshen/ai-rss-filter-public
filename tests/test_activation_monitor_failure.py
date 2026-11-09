"""A dead activation monitor must leave evidence and recover from transients.

The monitor thread owns the only pulse that keeps business admission open.  It
used to swallow every exception raised before ``RuntimeActivation`` existed:
no log, no receipt, no retry.  Readiness then failed closed forever while
``/health`` kept reporting 200, so nothing recorded why business stayed 503.
"""
from __future__ import annotations

import json
import logging
import threading

import pytest

from services import production_activation as activation
from services import production_startup as startup


@pytest.fixture
def monitor_env(tmp_path, monkeypatch):
    root = tmp_path / "startup"
    root.mkdir(mode=0o700)
    root.chmod(0o700)
    monkeypatch.setattr(activation, "_MONITOR", None)
    monkeypatch.setenv(startup.ENV_ROOT, str(root))
    monkeypatch.setattr(activation, "PULSE_SECONDS", 0)
    monkeypatch.setattr(activation, "MONITOR_BACKOFF_SECONDS", (0, 0, 0, 0))
    yield root
    monitor = activation._MONITOR
    if monitor is not None:
        monitor.join(timeout=5)
    monkeypatch.setattr(activation, "_MONITOR", None)


def _run_monitor():
    activation.start_web_monitor()
    activation._MONITOR.join(timeout=5)
    assert not activation._MONITOR.is_alive()


def test_monitor_logs_records_and_stops_after_bounded_retries(
    monitor_env, monkeypatch, caplog
):
    """Five consecutive failures: one log line each, then a queryable receipt."""
    monkeypatch.setattr(startup, "_OWNERS", {})

    with caplog.at_level(logging.WARNING):
        _run_monitor()

    record = json.loads(
        (monitor_env / "activation.web-monitor.failed.json").read_bytes()
    )
    assert record["error_type"] == "StopIteration"
    assert record["attempts"] >= 2
    assert record["retry"] is False
    attempts = [
        item for item in caplog.records if "monitor attempt" in item.getMessage()
    ]
    stopped = [
        item for item in caplog.records if "monitor stopped" in item.getMessage()
    ]
    assert len(attempts) == activation.MONITOR_ATTEMPTS
    assert len(stopped) == 1
    assert all("StopIteration" in item.getMessage() for item in attempts + stopped)


def test_monitor_retries_a_transient_failure_without_recording_one(
    monitor_env, monkeypatch, caplog
):
    """One failed owner check is retried; a later success leaves no receipt."""
    class FakeActivation:
        def __init__(self):
            self.stop = threading.Event()

        def prepared(self, _threads, _dependencies):
            self.stop.set()

        def pulse(self):
            raise AssertionError("no pulse expected after the monitor stopped")

    calls = {"check": 0}
    permitted = {"value": False}

    def check_owner(_key):
        calls["check"] += 1
        permitted["value"] = True
        raise RuntimeError("synthetic transient owner race")

    monkeypatch.setattr(startup, "_OWNERS", {"synthetic-owner": {}})
    monkeypatch.setattr(startup, "check_owner", check_owner)
    monkeypatch.setattr(activation, "permission_present", lambda: permitted["value"])
    monkeypatch.setattr(activation, "RuntimeActivation", FakeActivation)

    with caplog.at_level(logging.WARNING):
        _run_monitor()

    assert calls["check"] == 1
    assert not (monitor_env / "activation.web-monitor.failed.json").exists()
    warnings = [
        item for item in caplog.records if item.name == activation.__name__
    ]
    assert len(warnings) == 1
    assert "RuntimeError" in warnings[0].getMessage()
