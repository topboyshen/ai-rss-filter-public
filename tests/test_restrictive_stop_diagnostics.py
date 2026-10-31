import json
import re
import time
from types import SimpleNamespace

import pytest

from scripts import activation_window_diagnostics as window
from scripts import production_activation_host_native as host
from services import production_admission_lifecycle as lifetime
from services.admission_fence import AdmissionError

SECRET = "token=abc123secret " + "f" * 40


def test_observe_stop_times_sub_calls_redacts_and_restores(monkeypatch):
    def fail(*_a, **_k):
        raise AdmissionError("generation mismatch " + SECRET)
    monkeypatch.setattr(lifetime, "_stop_sender", fail)
    monkeypatch.setattr(lifetime, "_stop_receiver", lambda *_a, **_k: {"ok": 1})
    originals = (lifetime._stop_sender, lifetime._stop_receiver)
    outcomes = {}
    with window.observe_stop(lifetime, outcomes):
        with pytest.raises(AdmissionError):
            lifetime._stop_sender()
        assert lifetime._stop_receiver() == {"ok": 1}
    assert (lifetime._stop_sender, lifetime._stop_receiver) == originals
    sender = outcomes["_stop_sender"]
    assert sender["ok"] is False and sender["error_type"] == "AdmissionError"
    assert "generation mismatch" in sender["error_message"] and "abc123secret" not in sender["error_message"]
    assert "f" * 40 not in sender["error_message"]
    assert outcomes["_stop_receiver"]["ok"] is True and outcomes["_stop_receiver"]["elapsed_seconds"] >= 0


def test_record_stop_reads_shutdown_result_redacted_and_capped(tmp_path):
    switch, life = tmp_path / "switch", tmp_path / "continuation"
    switch.mkdir()
    life.mkdir()
    (life / "shutdown-result.json").write_text(json.dumps({"status": "stop_unconfirmed", "drained": False,
        "queue_checked": False, "receiver": {"x": 1},
        "errors": {"sender": {"type": "AdmissionError", "message_sha256": "a" * 64}}}))
    child = SimpleNamespace(poll=lambda: None)
    path = window.record_stop(switch, "restrictive_stop", time.monotonic(), AdmissionError("not confirmed " + SECRET),
                              outcomes={"_stop_sender": {"ok": False, "error_message": "x" * 5000}},
                              lifecycle_root=life, child=child, closed_evidence_source="recorded_initialize")
    assert path == switch / window.STOP_RECORD_NAME
    raw = path.read_text()
    assert "abc123secret" not in raw and "a" * 64 not in raw and len(raw) <= window.RECORD_LIMIT
    value = json.loads(raw)
    assert value["shutdown_result_file"]["errors"] == {"sender": {"type": "AdmissionError"}}
    assert value["shutdown_result_file"]["parts"] == ["receiver"]
    assert value["closed_evidence_source"] == "recorded_initialize" and value["watchdog_child_alive"] is True
    assert len(value["sub_calls"]["_stop_sender"]["error_message"]) <= 64
    assert window.record_stop(tmp_path / "missing", "x", time.monotonic(), OSError(), outcomes={}) is None
    assert host.GATEWAY_COMMAND_RE.fullmatch(
        "inner/activation/receiver/gateway-native/switch/" + window.STOP_RECORD_NAME)


def test_restrictive_stop_wiring_records_and_reraises(tmp_path, monkeypatch):
    from contextlib import contextmanager

    from scripts import verify_production_activation_native as verify
    from services import production_activation_control as control

    root = tmp_path / "session"
    (root / "continuation").mkdir(parents=True)

    @contextmanager
    def locked(*_a, **_k):
        yield SimpleNamespace(root=root)
    monkeypatch.setattr(control, "locked_activation", locked)
    monkeypatch.setattr(lifetime, "stop_new", lambda *_a, **_k: {"status": "stop_unconfirmed", "drained": False,
                                                                 "errors": {"receiver": {"type": "AdmissionError"}}})
    monkeypatch.setattr(window, "pg_snapshot", lambda *_a: {"skipped": True})
    switch = tmp_path / "switch"
    switch.mkdir()
    p = {"gateway_evidence": switch, "paths": {"grants": "g", "slots": "s"}, "activation_operation": "op",
         "watchdog_prepared": {"closed_evidence_source": "recorded_initialize"}}
    with pytest.raises(AdmissionError, match=re.escape("actual restrictive stop not confirmed")):
        verify._restrictive_stop(object(), p, object(), tmp_path)
    value = json.loads((switch / window.STOP_RECORD_NAME).read_bytes())
    assert value["returned"]["errors"] == {"receiver": {"type": "AdmissionError"}}
    assert value["closed_evidence_source"] == "recorded_initialize" and value["postgres"] == {"at_failure": {"skipped": True}}


def test_restrictive_stop_success_writes_nothing(tmp_path, monkeypatch):
    from contextlib import contextmanager

    from scripts import verify_production_activation_native as verify
    from services import production_activation_control as control

    @contextmanager
    def locked(*_a, **_k):
        yield SimpleNamespace(root=tmp_path)
    monkeypatch.setattr(control, "locked_activation", locked)
    ok = {"status": "producers_stopped_drain_pending", "drained": False, "errors": {}}
    monkeypatch.setattr(lifetime, "stop_new", lambda *_a, **_k: ok)
    p = {"gateway_evidence": tmp_path, "paths": {"grants": "g", "slots": "s"}, "activation_operation": "op"}
    assert verify._restrictive_stop(object(), p, object(), tmp_path) is ok
    assert not (tmp_path / window.STOP_RECORD_NAME).exists()
