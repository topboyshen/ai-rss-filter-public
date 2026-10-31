"""Guardian handoff failures record fixed-vocabulary facts only; the guard itself is unchanged."""
from __future__ import annotations

import json
import time
from pathlib import Path
from types import SimpleNamespace

import pytest

from scripts import controlled_production_release as release
from scripts import production_activation_host_native as host_native
from scripts import verify_production_activation_native as native
from services import admission_fence as gate

REPO = Path(__file__).resolve().parents[1]
LITERAL = "bound guardian has stopped or stop is uncertain"
SECRET = "token=supersecret-" + "f" * 40


def _roots(tmp_path):
    wd, sup = tmp_path / "watchdog", tmp_path / "continuation"
    wd.mkdir()
    sup.mkdir()
    return {"watchdog": {"plan_path": str(wd / "plan.json")}, "supervisor": {"plan_path": str(sup / "supervisor.plan.json")}}, wd, sup


def _write(path, value):
    path.write_text(json.dumps(value))


def test_summary_reports_which_stop_tripped_and_why(tmp_path):
    active, wd, sup = _roots(tmp_path)
    now = time.time()
    _write(wd / "STOP.json", {"reason": "parent_or_preopen_window_lost", "watchdog_plan_sha256": "a" * 64})
    _write(wd / "result.json", {"status": "producers_stopped_drain_pending",
                                "errors": {"sender": {"type": "TimeoutError", "message_sha256": "b" * 64}}})
    _write(wd / "handoff.failed.json", {"error": {"type": "AdmissionError", "message_sha256": "c" * 64}})
    _write(wd / "pulse.json", {"phase": "preopen", "clock": {"wall": now - 5}, "owner": {"pid": 1}})
    _write(wd / "plan.json", {"clock": {"expires_at": now + 100}, "parent": {"pid": 999999999, "ppid": 0}})
    child = SimpleNamespace(poll=lambda: 1)
    text = native.guardian_handoff_summary(active, child)
    fields = dict(item.split("=", 1) for item in text.split(","))
    assert fields["sup_stop"] == "0" and fields["sup_stop_pending"] == "0"
    assert fields["wd_stop"] == "1" and fields["wd_stop_pending"] == "0"
    assert fields["wd_stop_reason"] == "parent_or_preopen_window_lost"
    assert fields["wd_result_status"] == "producers_stopped_drain_pending"
    assert fields["wd_result_error_sender"] == "TimeoutError"
    assert fields["wd_handoff_failed"] == "1" and fields["wd_handoff_error"] == "AdmissionError"
    assert fields["wd_handoff_message_sha12"] == "c" * 12
    assert fields["wd_pulse_phase"] == "preopen" and 4 <= float(fields["wd_pulse_age_s"]) < 30
    assert 70 < float(fields["wd_window_remaining_s"]) <= 100
    assert fields["wd_parent_same"].startswith("0")
    assert fields["child_alive"] == "0" and fields["child_returncode"] == "1"


def test_summary_never_copies_runtime_text(tmp_path):
    active, wd, _ = _roots(tmp_path)
    _write(wd / "STOP.json", {"reason": SECRET})
    _write(wd / "result.json", {"status": SECRET, "errors": {"receiver": {"type": SECRET}}})
    _write(wd / "pulse.json", {"phase": SECRET})
    text = native.guardian_handoff_summary(active)
    assert "supersecret" not in text and "token" not in text and "ffff" not in text
    assert "wd_stop_reason=other" in text and "wd_result_status=other" in text


def test_summary_never_raises_on_bad_input():
    assert native.guardian_handoff_summary(None).startswith("roots_unreadable=")


def test_handoff_failure_note_reaches_failure_record_and_safe_result(tmp_path, capsys, monkeypatch):
    active, wd, _ = _roots(tmp_path)
    _write(wd / "STOP.json", {"reason": "parent_or_preopen_window_lost"})

    def handoff(_active):
        gate._require(False, LITERAL)
    monkeypatch.setattr(release, "_handoff", handoff)
    harness = SimpleNamespace(stage=lambda name: None)
    with pytest.raises(gate.AdmissionError, match="^" + LITERAL) as caught:
        native.complete_native_supervision(harness, {"watchdog_child": SimpleNamespace(poll=lambda: None)},
                                           active, None, None)
    failure = native.record_failure(caught.value, REPO, tmp_path, "actual_same_guardian_handoff_and_live_tick")
    summary = json.loads(capsys.readouterr().out.removeprefix("activation_native_failure="))
    assert "wd_stop=1" in failure["guardian_handoff"] and "child_alive=1" in summary["guardian_handoff"]
    result = host_native.safe_activation({"failure": failure})
    assert result["failure_guardian_handoff"] == failure["guardian_handoff"]
    assert result["failure_message_literal"] == LITERAL


def test_guard_still_rejects_present_and_pending_stop(tmp_path):
    for name in ("STOP.json", "STOP.json.pending"):
        root = tmp_path / name
        root.mkdir()
        (root / name).write_text("{}")
        with pytest.raises(gate.AdmissionError, match="^" + LITERAL + "$"):
            release._actual_child_receipt(SimpleNamespace(poll=lambda: None), lambda: {}, 2,
                                          stop_paths=(root / "STOP.json",))


EXIT_LITERAL = "actual guardian did not exit after confirmed original controlled stop"


def _exit_setup(tmp_path, monkeypatch, child):
    active, wd, sup = _roots(tmp_path)
    monkeypatch.setattr(release, "_handoff", lambda _active: {"owner": {"pid": child.pid}})
    monkeypatch.setattr(native, "_restrictive_stop", lambda *a: {"status": "producers_stopped_drain_pending"})
    monkeypatch.setattr(native.fixture, "save", lambda *a, **k: None)
    monkeypatch.setattr(native, "guardian_exit_seconds", lambda: 3)
    p = {"watchdog_child": child, "directory": tmp_path}
    return active, wd, sup, p


class Child:
    def __init__(self, code=None, expire=False):
        self.pid, self.code, self.expire = 4242, code, expire

    def wait(self, timeout):
        if self.expire:
            raise native.subprocess.TimeoutExpired("guardian", timeout)
        return self.code

    def poll(self):
        return self.code


def test_nonzero_guardian_exit_still_fails_and_records_supervisor_facts(tmp_path, monkeypatch, capsys):
    child = Child(code=1)
    active, wd, sup, p = _exit_setup(tmp_path, monkeypatch, child)
    _write(sup / "supervisor.tick.json", {"result": {"status": "stop_unconfirmed",
           "trigger": {"type": "TimeoutError", "message_sha256": "d" * 64}}, "clock": {"wall": time.time() - 2}})
    _write(sup / "supervisor.failed.json", {"error": {"type": "AdmissionError", "message_sha256": "e" * 64}})
    with pytest.raises(gate.AdmissionError, match="^" + EXIT_LITERAL) as caught:
        native.complete_native_supervision(SimpleNamespace(stage=lambda name: None), p, active, None, None)
    failure = native.record_failure(caught.value, REPO, tmp_path, "actual_restrictive_stop_without_false_drain")
    capsys.readouterr()
    text = failure["guardian_handoff"]
    assert text.startswith("phase=exit_wait,exit_code=1,waited_s=") and ",bound_s=3," in text
    for item in ("sup_tick_status=stop_unconfirmed", "sup_tick_trigger=TimeoutError", "sup_tick_trigger_sha12=" + "d" * 12,
                 "sup_failed=1", "sup_failed_error=AdmissionError", "sup_failed_sha12=" + "e" * 12, "child_alive=0"):
        assert item in text
    assert host_native.safe_activation({"failure": failure})["failure_guardian_handoff"] == text


def test_signal_exit_reported_as_signal(tmp_path, monkeypatch, capsys):
    child = Child(code=-9)
    active, _, _, p = _exit_setup(tmp_path, monkeypatch, child)
    with pytest.raises(gate.AdmissionError) as caught:
        native.complete_native_supervision(SimpleNamespace(stage=lambda name: None), p, active, None, None)
    assert any("exit_signal=9" in note for note in caught.value.__notes__)


def test_non_exiting_guardian_still_fails_with_timeout(tmp_path, monkeypatch):
    child = Child(code=None, expire=True)
    active, _, _, p = _exit_setup(tmp_path, monkeypatch, child)
    with pytest.raises(native.subprocess.TimeoutExpired) as caught:
        native.complete_native_supervision(SimpleNamespace(stage=lambda name: None), p, active, None, None)
    assert any("exit=none" in note and "child_alive=1" in note for note in caught.value.__notes__)


def test_summary_names_supervisor_tick_restrictive_errors_by_type_and_hash(tmp_path):
    active, wd, sup = _roots(tmp_path)
    _write(wd / "result.json", {"status": "stop_unconfirmed",
                                "errors": {"receiver": {"type": "AdmissionError", "message_sha256": "d" * 64}}})
    _write(sup / "supervisor.tick.json", {"clock": {"wall": time.time()}, "result": {
        "status": "stop_unconfirmed", "trigger": {"type": "AdmissionBusy", "message_sha256": "a" * 64},
        "errors": {"sender": {"type": "CutoverError", "message_sha256": "1" * 64},
                   "receiver": {"type": "AdmissionError", "message_sha256": "2" * 64, "extra": SECRET}},
        "sender_construction_error": {"type": "OSError", "message_sha256": "3" * 64}}})
    fields = dict(item.split("=", 1) for item in native.guardian_handoff_summary(active).split(","))
    assert fields["wd_result_error_receiver_sha12"] == "d" * 12
    assert fields["sup_tick_error_sender"] == "CutoverError" and fields["sup_tick_error_sender_sha12"] == "1" * 12
    assert fields["sup_tick_error_receiver"] == "AdmissionError" and fields["sup_tick_error_receiver_sha12"] == "2" * 12
    assert fields["sup_tick_sender_construction"] == "OSError"
    assert "sup_tick_error_marker" not in fields
    assert "supersecret" not in ",".join(fields.values())


def test_full_guardian_summary_survives_host_relay_bound():
    # The CI run 41027597583 summary was 636 chars; the added fields must not push it past the relay cap,
    # where the host would silently drop the whole field.
    text = "phase=exit_wait,exit_code=1," + ",".join("field_%02d=AdmissionError" % i for i in range(40))
    assert 800 < len(text) <= native.GUARDIAN_TEXT_CHARS
    relayed = {"failure": {"type": "AdmissionError", "message_sha256": "0" * 64, "guardian_handoff": text}}
    assert host_native.safe_activation(relayed)["failure_guardian_handoff"] == text
