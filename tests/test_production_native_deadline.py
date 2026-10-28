"""Deadline opt-in never adds time, and default production callers stay unbound."""
import sys
import time
from types import SimpleNamespace

import pytest

from scripts import production_native_deadline as native
from scripts import private_release_contract as private
from scripts import verify_production_startup_native as startup


def test_default_calls_keep_original_phase_seconds():
    assert native.STATE.get() is None and native.clamp(120) == 120


@pytest.mark.parametrize("active,total", [(True, 600), ("580", 600), (float("nan"), 600), (580, float("inf")), (601, 600), (580, 701), (0, 600)])
def test_scope_rejects_new_or_invalid_budget(monkeypatch, active, total):
    monkeypatch.setattr(native.time, "monotonic", lambda: 100)
    with pytest.raises(TimeoutError):
        with native.scope(active, total):
            pytest.fail("invalid budget cannot enter")


def test_caught_failure_cleanup_keeps_only_original_total_remainder(monkeypatch):
    clock = [100]
    monkeypatch.setattr(native.time, "monotonic", lambda: clock[0])
    with native.scope(580, 600):
        assert native.clamp(120) == 120
        clock[0] = 579
        assert native.clamp(120) == 1
        clock[0] = 581
        try:
            native.clamp(120)
        except BaseException:
            with native.final_phase():
                assert native.clamp(120) == 19
                clock[0] = 601
                with pytest.raises(TimeoutError):
                    native.clamp(120)
        with pytest.raises(TimeoutError):
            native.clamp(1)
    assert native.STATE.get() is None


@pytest.mark.parametrize("active,total", [(580, 601), (581, 600), (700, 700)])
def test_nested_scope_cannot_replace_original_budget(monkeypatch, active, total):
    monkeypatch.setattr(native.time, "monotonic", lambda: 100)
    with native.scope(580, 600):
        with pytest.raises(TimeoutError):
            with native.scope(active, total):
                pytest.fail("nested budget must not expand")


def test_nested_tightening_and_final_phase_restore_original_bounds(monkeypatch):
    monkeypatch.setattr(native.time, "monotonic", lambda: 570)
    with native.scope(580, 600):
        with native.scope(575, 590):
            assert native.clamp(120) == 5
            with native.final_phase():
                assert native.clamp(120) == 20
            assert native.clamp(120) == 5
        assert native.clamp(120) == 10
        with native.final_phase():
            assert native.clamp(120) == 30
    assert native.STATE.get() is None


def test_opt_in_process_does_not_invoke_unbounded_popen_exit(monkeypatch):
    calls = []
    child = SimpleNamespace(stdin=None, stdout=SimpleNamespace(close=lambda: calls.append("out.close")),
                            stderr=SimpleNamespace(close=lambda: calls.append("err.close")))
    monkeypatch.setattr(native.time, "monotonic", lambda: 100)
    with native.scope(580, 600):
        with native.process(lambda *a, **k: child, ["fixed"]):
            pass
    assert calls == ["out.close", "err.close"]


def test_expired_client_exit_does_not_wait_again_or_hide_primary(monkeypatch):
    calls = []
    clock = [100]
    monkeypatch.setattr(native.time, "monotonic", lambda: clock[0])
    monkeypatch.setattr(native.os, "killpg", lambda pid, sig: calls.append("client.kill.only"))
    child = SimpleNamespace(pid=123, poll=lambda: None, wait=lambda **k: pytest.fail("cannot restart expired wait"))
    original = RuntimeError("original failure")
    with native.scope(580, 600):
        clock[0] = 601
        native.finish_client(child, original)
    assert calls == ["client.kill.only"]
    assert any("exit unconfirmed" in note for note in original.__notes__)


def test_private_run_expired_budget_rejects_before_process(monkeypatch):
    clock = [100]
    monkeypatch.setattr(native.time, "monotonic", lambda: clock[0])
    monkeypatch.setattr(private.subprocess, "Popen", lambda *a, **k: pytest.fail("no subprocess after expiration"))
    with native.scope(580, 600):
        clock[0] = 581
        with pytest.raises(TimeoutError):
            private.run(["fixed"])


def test_archived_command_expired_budget_rejects_before_files_or_process(tmp_path, monkeypatch):
    clock = [100]
    monkeypatch.setattr(native.time, "monotonic", lambda: clock[0])
    monkeypatch.setattr(startup.subprocess, "Popen", lambda *a, **k: pytest.fail("no process after expiration"))
    with native.scope(580, 600):
        clock[0] = 601
        with pytest.raises(TimeoutError):
            startup.archive_command(["fixed"], tmp_path / "original", seconds=120)
    assert not list(tmp_path.iterdir())


@pytest.mark.parametrize("transport", ["private", "archive"])
def test_real_local_client_is_clamped_without_models_or_native_resources(tmp_path, transport):
    started = time.monotonic()
    with native.scope(started + 0.05, started + 0.3):
        with pytest.raises(ValueError):
            if transport == "private":
                private.run([sys.executable, "-I", "-c", "import time;time.sleep(2)"])
            else:
                startup.archive_command([sys.executable, "-I", "-c", "import time;time.sleep(2)"], tmp_path / "client", seconds=120)
    assert time.monotonic() - started < 1
