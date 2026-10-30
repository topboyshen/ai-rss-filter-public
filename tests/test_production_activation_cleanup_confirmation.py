"""Independent cleanup confirmations; synthetic calls never stop a real daemon."""
import json
import os
import signal
import stat
import subprocess
from types import SimpleNamespace

import pytest

from scripts import production_activation_host_native as native
from services import admission_fence as gate


@pytest.fixture
def cleanup(tmp_path, monkeypatch):
    previous = os.umask(0o077)
    case = SimpleNamespace(events=[], failures={}, inventory="", daemon_rc=0,
                           retainer_rc=0, signals=[], waits=[], clock=100.0)

    def stage(name):
        case.events.append(name)
        if name in case.failures:
            raise case.failures[name]

    def check(*, final):
        assert final is True
        stage("context")

    def inventory(args):
        assert args == ["ps", "--quiet", "--no-trunc"]
        stage("inventory")
        return case.inventory

    def peer(pid, identity):
        assert pid == 456 and identity is case.identity
        stage("peer")

    def send(signum):
        case.signals.append(signum)
        stage("signal")

    def daemon_wait(*, timeout):
        case.waits.append(("daemon", timeout))
        stage("daemon_wait")
        case.clock += 2
        return case.daemon_rc

    def finish(deadline):
        assert deadline == 600
        stage("logs")
        case.clock += 3

    def retainer_wait(*, timeout):
        case.waits.append(("retainer", timeout))
        stage("retainer_wait")
        return case.retainer_rc

    real_record = native.record

    def record(path, value, **kwargs):
        stage("drain_record" if path.name == "DRAIN_OBSERVATION.json" else "record")
        return real_record(path, value, **kwargs)

    case.identity = {"fixed": "synthetic-daemon-identity"}
    case.context = SimpleNamespace(root=tmp_path, plan={"deadline": 600}, daemon=case.identity,
        check=check, commands=SimpleNamespace(deadline=580, docker=inventory, uncertain_mutation=False))
    case.process = SimpleNamespace(pid=456, send_signal=send, wait=daemon_wait)
    case.logs = SimpleNamespace(finish=finish)
    case.retainer = SimpleNamespace(wait=retainer_wait)
    case.receipt = tmp_path / "DAEMON_STOP.json"
    case.drain_receipt = tmp_path / "DRAIN_OBSERVATION.json"
    monkeypatch.setattr(native.host, "check_peer", peer)
    monkeypatch.setattr(native.host, "RETAINERS", {456: (case.retainer, "synthetic-only")})
    monkeypatch.setattr(native.time, "monotonic", lambda: case.clock)
    monkeypatch.setattr(native, "record", record)
    try:
        yield case
    finally:
        os.umask(previous)


def run(case):
    return native.stop_daemon(case.context, case.process, case.logs)


@pytest.mark.parametrize("inventory", ["a" * 64, "a" * 64 + "\n" + "b" * 64,
                                      "unknown-inventory-entry"])
def test_nonempty_inventory_never_authorizes_daemon_signal(cleanup, inventory):
    cleanup.inventory = inventory
    with pytest.raises(gate.AdmissionError, match="running or uncertain containers"):
        run(cleanup)
    assert cleanup.events == ["context", "inventory", "drain_record"]
    assert json.loads(cleanup.drain_receipt.read_bytes()) == {
        "schema": 1, "running_count": len(inventory.splitlines()), "uncertain_mutation": False}
    assert cleanup.signals == [] and not cleanup.receipt.exists()


def test_empty_inventory_does_not_clear_unknown_mutation(cleanup):
    cleanup.context.commands.uncertain_mutation = True
    with pytest.raises(gate.AdmissionError, match="running or uncertain containers"):
        run(cleanup)
    assert cleanup.context.commands.uncertain_mutation is True
    assert cleanup.events == ["context", "inventory", "drain_record"]
    assert json.loads(cleanup.drain_receipt.read_bytes()) == {
        "schema": 1, "running_count": 0, "uncertain_mutation": True}
    assert cleanup.signals == [] and not cleanup.receipt.exists()


@pytest.mark.parametrize("failed_stage", ["context", "inventory", "peer"])
def test_identity_or_inventory_failure_preserves_original_and_never_signals(cleanup, failed_stage):
    failure = gate.AdmissionError("synthetic missing or changed cleanup proof")
    cleanup.failures[failed_stage] = failure
    with pytest.raises(gate.AdmissionError) as caught:
        run(cleanup)
    assert caught.value is failure
    assert cleanup.events[-1] == failed_stage
    assert cleanup.signals == [] and not cleanup.receipt.exists()


def test_drain_fact_write_failure_blocks_signal_without_masking_original(cleanup):
    failure = OSError("synthetic drain proof durable write failed")
    cleanup.failures["drain_record"] = failure
    with pytest.raises(OSError) as caught:
        run(cleanup)
    assert caught.value is failure
    assert cleanup.events == ["context", "inventory", "drain_record"]
    assert cleanup.signals == [] and cleanup.waits == []
    assert not cleanup.drain_receipt.exists() and not cleanup.receipt.exists()


def test_existing_drain_observation_is_not_overwritten_or_used_as_fresh_proof(cleanup):
    original = b'{"schema":1,"running_count":0,"uncertain_mutation":false}\n'
    cleanup.drain_receipt.write_bytes(original)
    with pytest.raises(FileExistsError):
        run(cleanup)
    assert cleanup.drain_receipt.read_bytes() == original
    assert cleanup.signals == [] and cleanup.waits == []
    assert not cleanup.receipt.exists()


@pytest.mark.parametrize("returncode", [1, -signal.SIGTERM, None])
def test_daemon_exit_must_be_confirmed_normal_zero(cleanup, returncode):
    cleanup.daemon_rc = returncode
    with pytest.raises(gate.AdmissionError, match="private daemon normal stop unconfirmed"):
        run(cleanup)
    assert cleanup.signals == [signal.SIGTERM]
    assert cleanup.events[-1] == "daemon_wait"
    assert "logs" not in cleanup.events and "retainer_wait" not in cleanup.events
    assert not cleanup.receipt.exists()


def test_daemon_wait_timeout_never_becomes_confirmed_stop_or_forced_kill(cleanup):
    failure = subprocess.TimeoutExpired("synthetic-daemon", 500)
    cleanup.failures["daemon_wait"] = failure
    with pytest.raises(subprocess.TimeoutExpired) as caught:
        run(cleanup)
    assert caught.value is failure
    assert cleanup.signals == [signal.SIGTERM]
    assert cleanup.events[-1] == "daemon_wait"
    assert not cleanup.receipt.exists()


def test_failed_term_delivery_is_not_retried_or_replaced_by_kill(cleanup):
    failure = ProcessLookupError("synthetic peer vanished after identity check")
    cleanup.failures["signal"] = failure
    with pytest.raises(ProcessLookupError) as caught:
        run(cleanup)
    assert caught.value is failure
    assert cleanup.signals == [signal.SIGTERM]
    assert cleanup.waits == [] and not cleanup.receipt.exists()


def test_log_finish_failure_prevents_retainer_and_success_receipt(cleanup):
    failure = OSError("synthetic log closure proof unavailable")
    cleanup.failures["logs"] = failure
    with pytest.raises(OSError) as caught:
        run(cleanup)
    assert caught.value is failure
    assert cleanup.events[-1] == "logs"
    assert cleanup.signals == [signal.SIGTERM]
    assert not cleanup.receipt.exists()


def test_missing_retainer_prevents_success_even_after_daemon_zero(cleanup, monkeypatch):
    monkeypatch.setattr(native.host, "RETAINERS", {})
    with pytest.raises(gate.AdmissionError, match="daemon FD retainer did not observe closure"):
        run(cleanup)
    assert cleanup.events[-1] == "logs"
    assert "record" not in cleanup.events and not cleanup.receipt.exists()


@pytest.mark.parametrize("returncode", [1, -signal.SIGTERM, None])
def test_retainer_must_independently_confirm_normal_zero(cleanup, returncode):
    cleanup.retainer_rc = returncode
    with pytest.raises(gate.AdmissionError, match="daemon FD retainer did not observe closure"):
        run(cleanup)
    assert cleanup.events[-1] == "retainer_wait"
    assert cleanup.signals == [signal.SIGTERM]
    assert not cleanup.receipt.exists()


def test_retainer_timeout_is_not_replaced_by_daemon_zero(cleanup):
    failure = subprocess.TimeoutExpired("synthetic-retainer", 495)
    cleanup.failures["retainer_wait"] = failure
    with pytest.raises(subprocess.TimeoutExpired) as caught:
        run(cleanup)
    assert caught.value is failure
    assert cleanup.events[-1] == "retainer_wait"
    assert not cleanup.receipt.exists()


def test_remaining_deadline_is_recomputed_before_retainer_wait(cleanup):
    result = run(cleanup)
    assert result["status"] == "stopped"
    assert cleanup.waits == [("daemon", 500.0), ("retainer", 495.0)]
    assert cleanup.context.commands.deadline == cleanup.context.plan["deadline"] == 600


def test_expired_deadline_after_daemon_exit_cannot_complete_retainer_proof(cleanup):
    def expire_logs(deadline):
        assert deadline == 600
        cleanup.events.append("logs")
        cleanup.clock = 601
    cleanup.logs.finish = expire_logs
    with pytest.raises(gate.AdmissionError, match="shared deadline expired or replaced"):
        run(cleanup)
    assert cleanup.events[-1] == "logs"
    assert cleanup.waits == [("daemon", 500.0)]
    assert cleanup.signals == [signal.SIGTERM]
    assert not cleanup.receipt.exists()


def test_success_requires_all_independent_confirmations_before_durable_receipt(cleanup):
    result = run(cleanup)
    assert cleanup.events == ["context", "inventory", "drain_record", "peer", "signal", "daemon_wait",
                              "logs", "retainer_wait", "record"]
    assert cleanup.signals == [signal.SIGTERM]
    assert cleanup.context.commands.uncertain_mutation is False
    assert result == {"status": "stopped", "all_containers_stopped": True,
                      "daemon_returncode": 0, "forced_kill": False, "removed": False}
    assert json.loads(cleanup.receipt.read_bytes()) == result
    assert stat.S_IMODE(cleanup.receipt.stat().st_mode) == 0o600
    assert json.loads(cleanup.drain_receipt.read_bytes()) == {
        "schema": 1, "running_count": 0, "uncertain_mutation": False}
    assert stat.S_IMODE(cleanup.drain_receipt.stat().st_mode) == 0o600


def test_receipt_write_failure_is_not_reported_as_success(cleanup):
    failure = OSError("synthetic exclusive receipt write failed")
    cleanup.failures["record"] = failure
    with pytest.raises(OSError) as caught:
        run(cleanup)
    assert caught.value is failure
    assert cleanup.events[-1] == "record"
    assert not cleanup.receipt.exists()


def test_existing_stop_receipt_is_preserved_not_replaced(cleanup):
    original = b'{"status":"original-stop-proof"}\n'
    cleanup.receipt.write_bytes(original)
    with pytest.raises(FileExistsError):
        run(cleanup)
    assert cleanup.receipt.read_bytes() == original
    assert cleanup.signals == [signal.SIGTERM]
