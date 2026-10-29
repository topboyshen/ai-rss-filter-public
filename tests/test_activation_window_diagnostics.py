import json

import pytest
import socket
import time

from scripts import activation_window_diagnostics as window
from scripts import production_activation_host_native as native
from tests.test_production_activation_host_native import _gateway_switch_dir

SECRET = "Bearer " + "ab" * 32


def _harness_output(tmp_path, port):
    out = tmp_path / "activation"
    bridge = out / "native-control-bridge"
    bridge.mkdir(parents=True)
    (bridge / "START.json").write_text(json.dumps({"port": port, "request_seconds": 3}))
    (bridge / "request-001.intent.json").write_text(json.dumps({"method": "GET", "route": "/activation-registration"}))
    (bridge / "STOP.json").write_text(json.dumps({"request": 1, "stage": "request", "error_type": "RuntimeError"}))
    (out / "command-0098.json").write_text(json.dumps({"argv": ["docker", "--host", "unix:///x", "run", SECRET],
                                                        "rc": None, "elapsed_seconds": 2.0004}))
    return out


def test_timeout_record_written_redacted_bounded_and_exported(tmp_path, monkeypatch):
    root = tmp_path / "evidence"
    switch = _gateway_switch_dir(root)
    listener = socket.socket()
    listener.bind(("127.0.0.1", 0))
    listener.listen(1)
    try:
        out = _harness_output(tmp_path, listener.getsockname()[1])
        error = TimeoutError("timed out " + SECRET + " token=leak123 " + "x" * 2000)
        path = window.record(switch, "prepare", time.monotonic() - 2.5, error, harness_output=out)
    finally:
        listener.close()
    raw = path.read_bytes()
    assert path.name == "watchdog-window.stderr" and len(raw) <= window.RECORD_LIMIT
    assert not list(switch.glob(".*.tmp"))
    value = json.loads(raw)
    assert value["sub_stage"] == "prepare" and value["error_type"] == "TimeoutError"
    assert value["elapsed_seconds"] >= 2.5 and value["configured_seconds"]["native_transport_client"] == 2
    assert "GET /activation-registration" in value["endpoints"]
    assert len(value["error_message"]) <= window.TEXT_LIMIT and value["error_message"].startswith("timed out")
    assert value["bridge"]["requests"] == 1 and value["bridge"]["results"] == 0
    assert value["bridge"]["stop"]["error_type"] == "RuntimeError" and value["bridge"]["port_listening"] is True
    assert value["last_harness_command"] == {"name": "command-0098.json", "subcommand": "run", "rc": None,
                                             "elapsed_seconds": 2.0}
    assert "ab" * 32 not in raw.decode() and "leak123" not in raw.decode()
    monkeypatch.setattr(native.time, "monotonic", lambda: 100)
    rows = native.safe_inventory(root, 600)["gateway_command_stderr"]
    assert [r["path"].rsplit("/", 1)[1] for r in rows] == ["watchdog-window.stderr"]
    assert '"sub_stage": "prepare"' in rows[0]["text"] and "leak123" not in rows[0]["text"]


def test_record_is_capped_and_never_raises(tmp_path, monkeypatch):
    switch = tmp_path / "s"
    switch.mkdir()
    monkeypatch.setitem(window.ENDPOINTS, "start", ["GET /" + "y" * 20000])
    path = window.record(switch, "start", time.monotonic(), OSError("boom"))
    assert path is not None and len(path.read_bytes()) == window.RECORD_LIMIT
    assert window.record(tmp_path / "missing", "ready", time.monotonic(), OSError("x")) is None


def _window_case(tmp_path, monkeypatch, failing):
    import inspect
    from types import SimpleNamespace

    from scripts import controlled_production_release as release
    from scripts import production_admission_watchdog as watchdog
    from scripts import verify_production_activation_native as verify

    assert "_watchdog_window(p, harness, receiver, native_driver)" in inspect.getsource(verify.activate)
    error = TimeoutError("timed out")
    plan = tmp_path / "guardian" / "plan.json"
    child = SimpleNamespace(pid=1, poll=lambda: None)

    def step(name, value):
        def run(*_args, **_kwargs):
            if name == failing:
                raise error
            return value
        return run
    monkeypatch.setattr(watchdog, "prepare", step("prepare", {"plan_path": str(plan)}))
    monkeypatch.setattr(watchdog, "start", step("start", child))
    monkeypatch.setattr(release, "_actual_child_receipt", step("ready", {"ready": True}))
    switch = tmp_path / "switch"
    switch.mkdir()
    p = {"gateway_evidence": switch}
    return verify, p, error, switch


@pytest.mark.parametrize("failing", ["prepare", "start", "ready"])
def test_activate_window_failure_writes_record_and_reraises_original(tmp_path, monkeypatch, failing):
    verify, p, error, switch = _window_case(tmp_path, monkeypatch, failing)
    with pytest.raises(TimeoutError) as caught:
        verify._watchdog_window(p, SimpleHarness(tmp_path), object(), object())
    assert caught.value is error and str(caught.value) == "timed out"
    value = json.loads((switch / "watchdog-window.stderr").read_bytes())
    assert value["sub_stage"] == failing and value["error_type"] == "TimeoutError"


def test_activate_window_success_writes_nothing(tmp_path, monkeypatch):
    verify, p, _error, switch = _window_case(tmp_path, monkeypatch, None)
    guardian, ready = verify._watchdog_window(p, SimpleHarness(tmp_path), object(), object())
    assert ready == {"ready": True} and p["watchdog_prepared"] is guardian
    assert not (switch / "watchdog-window.stderr").exists()


class SimpleHarness:
    def __init__(self, output):
        self.output = output
