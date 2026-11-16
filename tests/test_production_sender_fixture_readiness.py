"""Host-loopback's real prepare dispatch and its separate daemon drain proof."""
import json
import os
import subprocess
import time
from types import MethodType, SimpleNamespace

import pytest

from scripts import production_sender_bootstrap_native as bootstrap
from scripts import production_activation_host_native as isolated
from scripts import verify_miniflux_pause_native as common
from scripts import verify_production_activation_native as activation
from services import admission_fence as gate


@pytest.fixture(autouse=True)
def private_evidence_mode():
    previous = os.umask(0o077)
    try:
        yield
    finally:
        os.umask(previous)


@pytest.fixture
def preparation(tmp_path, monkeypatch):
    monkeypatch.setattr(common.platform, "system", lambda: "Linux")
    monkeypatch.setattr(common.platform, "machine", lambda: "x86_64")
    image = "sha256:" + "b" * 64
    h = bootstrap.HostLoopbackHarness(tmp_path, tmp_path, image, tmp_path / "binary", tmp_path / "evidence")
    signals, operations, probes = [], [], []
    h.private_host_context = SimpleNamespace(check=lambda **kw: None, root=h.output,
        plan={"active_deadline": time.monotonic() + 40, "deadline": time.monotonic() + 60},
        commands=SimpleNamespace(uncertain_mutation=False, docker=lambda args: ""))
    row = {"Id": "c" * 64, "Name": "/" + h.prefix + "-fixture", "Image": image,
        "Created": "2026-11-06T08:00:00Z", "RestartCount": 0,
        "Config": {"Labels": {common.LABEL: h.owner}},
        "NetworkSettings": {"Networks": {h.network: {"NetworkID": "e" * 64, "IPAddress": "172.20.0.2"}}},
        "State": {"Running": True, "Dead": False, "Restarting": False, "OOMKilled": False,
                  "Pid": 12345, "StartedAt": "2026-11-06T08:00:01Z"},
        "HostConfig": {"NetworkMode": h.network, "PortBindings": {}, "Memory": common.MEMORY,
                       "MemorySwap": common.MEMORY, "PidsLimit": common.PIDS, "NanoCpus": 1_000_000_000,
                       "Privileged": False, "SecurityOpt": ["no-new-privileges"]}}

    def transport(argv, **kwargs):
        args = argv[len(common.DOCKER):]
        operations.append(list(args))
        if args[0] == "info":
            value = json.dumps({"Architecture": "x86_64"})
        elif args[:2] == ["image", "inspect"]:
            value = json.dumps([{"Id": image, "Architecture": "amd64", "Os": "linux", "RepoDigests": []}])
        elif args[:2] == ["volume", "create"]:
            value = "synthetic-volume"
        elif args[0] == "run":
            value = row["Id"]
        elif args[0] == "inspect":
            value = json.dumps([row])
        else:
            assert args[0] == "exec"
            probes.append(list(args))
            first = len(probes) == 1
            if activation.FIXTURE_READINESS_PROBE in args:
                p = json.loads(kwargs["input"])
                value = json.dumps({"schema": 1, "probe": "fixture_healthcheck", "nonce": p["nonce"],
                    "status": "not_ready" if first else "ready", "reason": "econnrefused" if first else "http200"},
                    separators=(",", ":")) + "\n"
            else:
                assert common.CLIENT in args
                if first:
                    return subprocess.CompletedProcess(argv, 1, "", "urllib.error.URLError: <urlopen error [Errno 111] Connection refused>")
                value = json.dumps({"status": 200, "body": '{"status":"synthetic_fixture_ready"}'})
        return subprocess.CompletedProcess(argv, 0, value, "")

    monkeypatch.setattr(common.subprocess, "run", transport)
    monkeypatch.setattr(common, "native_host", lambda *a: None)
    monkeypatch.setattr(common, "image_checks", lambda *a: None)
    monkeypatch.setattr(h, "prepare_network", lambda: None)
    actual_launch = h.launch
    def launch(role, *args, **kwargs):
        # PG is not part of this local HTTP-startup experiment. The real fixture
        # launch, identity capture, prepare, command, HTTP and wait remain real.
        if role == "pg":
            return None
        return actual_launch(role, *args, **kwargs)
    monkeypatch.setattr(h, "launch", launch)
    monkeypatch.setattr(h, "postgres_ready", lambda: True)
    process = SimpleNamespace(pid=44, send_signal=lambda sig: signals.append(sig))
    logs = SimpleNamespace(finish=lambda end: pytest.fail("uncertain cleanup must not close logs"))
    return SimpleNamespace(harness=h, operations=operations, probes=probes, signals=signals, process=process, logs=logs)


def test_historical_generic_prepare_recovery_retains_unknown_and_blocks_cleanup(preparation, monkeypatch):
    p = preparation
    # Preserve the old behavior as a mechanism reproduction after the narrow fix.
    monkeypatch.setattr(p.harness, "fixture_ready", MethodType(common.Harness.fixture_ready, p.harness))
    assert p.harness.prepare() is None
    assert len(p.probes) == 2 and all(common.CLIENT in args for args in p.probes)
    assert p.harness.private_host_context.commands.uncertain_mutation is True
    with pytest.raises(gate.AdmissionError, match="running or uncertain containers"):
        isolated.stop_daemon(p.harness.private_host_context, p.process, p.logs)
    assert json.loads((p.harness.output / "DRAIN_OBSERVATION.json").read_bytes()) == {
        "schema": 1, "running_count": 0, "uncertain_mutation": True}
    assert p.signals == [] and not (p.harness.output / "DAEMON_STOP.json").exists()


def test_actual_host_loopback_prepare_uses_existing_fixed_probe_without_sticky(preparation):
    p = preparation
    assert p.harness.prepare() is None
    assert len(p.probes) == 2 and all(activation.FIXTURE_READINESS_PROBE in args for args in p.probes)
    assert p.harness.private_host_context.commands.uncertain_mutation is False
    assert p.harness.fixture_readiness_counts == {"not_ready": 1, "ready": 1}
    assert [json.loads(f.read_bytes())["status"] for f in sorted(p.harness.output.glob("FIXTURE_READINESS_*.json"))] == [
        "not_ready", "ready"]
    assert p.harness.sender_readiness_launch_identities["fixture"]["Name"] == "/" + p.harness.prefix + "-fixture"


def test_ready_fixture_does_not_replace_independent_daemon_exit_confirmation(preparation, monkeypatch):
    p = preparation
    p.harness.prepare()
    context = p.harness.private_host_context
    context.daemon = {"pid": p.process.pid}
    monkeypatch.setattr(isolated.host, "check_peer", lambda *a: None)
    p.process.wait = lambda timeout: 1
    with pytest.raises(gate.AdmissionError, match="private daemon normal stop unconfirmed"):
        isolated.stop_daemon(context, p.process, p.logs)
    assert context.commands.uncertain_mutation is False
    assert p.signals == [isolated.signal.SIGTERM]
    assert not (p.harness.output / "DAEMON_STOP.json").exists()


def test_other_role_cannot_borrow_host_loopback_fixture_identity(preparation):
    with pytest.raises(gate.AdmissionError, match="only the fixed host-loopback fixture"):
        preparation.harness._sender_readiness_identity({}, "sender")
