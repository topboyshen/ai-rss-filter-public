"""Only completed, identity-bound fixture startup probes may be classified."""
from copy import deepcopy
import hashlib
import json
import os
import stat
import subprocess
import sys
import time
from types import SimpleNamespace

import pytest

from scripts import production_host_ingress_native as host
from scripts import verify_production_activation_native as native
from scripts import verify_miniflux_pause_native as common
from services import admission_fence as gate


@pytest.fixture(params=["activation", "host_loopback"])
def rig(tmp_path, monkeypatch, request):
    previous = os.umask(0o077)
    from scripts.production_sender_bootstrap_native import HostLoopbackHarness
    cls = native.ActivationHarness if request.param == "activation" else HostLoopbackHarness
    harness = object.__new__(cls)
    harness.output, harness.sequence, harness.started = tmp_path, 0, time.monotonic()
    harness.phase_deadline, harness.env = None, {}
    harness.password, harness.token, harness.auth = "unused-password", "unused-token", "private-synthetic-auth"
    harness.owner, harness.prefix, harness.network = "a" * 32, "synthetic-owned", "synthetic-network"
    harness.names, harness.images, harness.created = {}, {}, []
    harness.private_host_context = SimpleNamespace(check=lambda **kwargs: None,
        plan={"deadline": time.monotonic() + 60, "active_deadline": time.monotonic() + 40},
        commands=SimpleNamespace(uncertain_mutation=False))
    row = {"Id": "b" * 64, "Name": "/synthetic-owned-fixture", "Image": "sha256:" + "b" * 64,
        "Created": "2026-10-30T00:00:00Z", "RestartCount": 0,
        "Config": {"Labels": {common.LABEL: harness.owner}},
        "NetworkSettings": {"Networks": {harness.network: {"NetworkID": "e" * 64, "IPAddress": "172.20.0.2"}}},
        "State": {"Running": True, "Dead": False, "Restarting": False, "OOMKilled": False,
                  "Pid": 12345, "StartedAt": "2026-10-30T00:00:01Z"},
        "HostConfig": {"NetworkMode": harness.network, "PortBindings": {}, "Memory": common.MEMORY,
                       "MemorySwap": common.MEMORY, "PidsLimit": common.PIDS, "NanoCpus": 1_000_000_000,
                       "Privileged": False, "SecurityOpt": ["no-new-privileges"]}}
    value = SimpleNamespace(harness=harness, row=row, calls=[], inspect_rows=[], results=[],
                            outer_rc=0, marker_change=None, probe_sequence=None)

    def run(argv, **kwargs):
        args = argv[len(common.DOCKER):]
        value.calls.append((list(args), kwargs))
        if args[0] == "run":
            return subprocess.CompletedProcess(argv, 0, row["Id"] + "\n", "")
        if args[0] == "inspect":
            actual = value.inspect_rows.pop(0) if value.inspect_rows else row
            return subprocess.CompletedProcess(argv, 0, json.dumps([actual]), "")
        assert args[0] == "exec", "synthetic transport refuses unexpected operations"
        value.probe_sequence = harness.sequence
        outcome = value.results.pop(0) if value.results else ("ready", "http200")
        if outcome == "timeout":
            raise subprocess.TimeoutExpired(argv, kwargs["timeout"], output=b"partial-result")
        if isinstance(outcome, BaseException):
            raise outcome
        if native.FIXTURE_READINESS_PROBE not in args:
            return subprocess.CompletedProcess(argv, 1, "", "synthetic generic client connection refused")
        payload = json.loads(kwargs["input"])
        marker = {"schema": 1, "probe": "fixture_healthcheck", "nonce": payload["nonce"],
                  "status": outcome[0], "reason": outcome[1]}
        raw = json.dumps(marker, separators=(",", ":")) + "\n"
        if value.marker_change:
            raw = value.marker_change(marker)
        return subprocess.CompletedProcess(argv, value.outer_rc, raw, "")

    monkeypatch.setattr(common.subprocess, "run", run)
    try:
        harness.launch("fixture", row["Image"])
        value.calls.clear()
        yield value
    finally:
        os.umask(previous)


def records(rig):
    return [json.loads(p.read_bytes()) for p in sorted(rig.harness.output.glob("FIXTURE_READINESS_*.json"))]


@pytest.mark.parametrize("status,reason,expected", [
    ("ready", "http200", True), ("not_ready", "http503", False), ("not_ready", "econnrefused", False)])
def test_fixed_fixture_probe_classifies_only_after_identity_and_durable_receipt(rig, status, reason, expected):
    rig.results = [(status, reason)]
    assert rig.harness.fixture_ready() is expected
    assert not rig.harness.private_host_context.commands.uncertain_mutation
    actual, = records(rig)
    assert actual["status"] == status and actual["reason"] == reason
    assert actual["transport_returncode"] == 0
    probes = [(args, kw) for args, kw in rig.calls if args[0] == "exec"]
    assert len(probes) == 1
    args, kwargs = probes[0]
    assert args == ["exec", "-i", rig.row["Id"], "python", "-B", "-c", native.FIXTURE_READINESS_PROBE]
    assert 0 < kwargs["timeout"] <= 15
    payload = json.loads(kwargs["input"])
    assert set(payload) == {"nonce", "fixture_ip"} and payload["fixture_ip"] == "172.20.0.2"
    assert len(payload["nonce"]) == 64 and int(payload["nonce"], 16) >= 0
    assert [a for a, _ in rig.calls].count(["inspect", rig.row["Id"]]) == 2
    original = rig.harness.output / f"command-{rig.probe_sequence:04d}.json"
    receipt = original.with_suffix(".result.json")
    saved = common.read_command_result(original, rig.probe_sequence, json.loads(receipt.read_bytes()))
    assert saved["returncode"] == 0
    assert actual["command_original_sha256"] == hashlib.sha256(original.read_bytes()).hexdigest()
    assert actual["nonce_sha256"] == hashlib.sha256(payload["nonce"].encode()).hexdigest()
    assert all(stat.S_IMODE(p.stat().st_mode) == 0o600 for p in (original, receipt))
    assert rig.harness.fixture_readiness_counts == {"ready": int(expected), "not_ready": int(not expected)}


def test_not_ready_then_ready_does_not_pollute_sticky_uncertainty(rig):
    rig.results = [("not_ready", "econnrefused"), ("ready", "http200")]
    assert rig.harness.wait(rig.harness.fixture_ready, readiness=True) is True
    assert [r["status"] for r in records(rig)] == ["not_ready", "ready"]
    assert len({r["nonce_sha256"] for r in records(rig)}) == 2
    assert not rig.harness.private_host_context.commands.uncertain_mutation


@pytest.mark.parametrize("outcome", [("ready", "http200"), ("not_ready", "econnrefused")])
def test_confirmed_probe_never_clears_existing_sticky_uncertainty(rig, outcome):
    rig.harness.private_host_context.commands.uncertain_mutation = True
    rig.results = [outcome]
    assert rig.harness.fixture_ready() is (outcome[0] == "ready")
    assert rig.harness.private_host_context.commands.uncertain_mutation is True


@pytest.mark.parametrize("outcome", [("unknown", "unknown"), ("ready", "econnrefused"),
    ("not_ready", "http200"), ("not_ready", "http200_empty"), ("not_ready", "timeout"),
    ("not_ready", "dns"), ("not_ready", "permission")])
def test_unknown_or_inconsistent_completion_still_blocks(rig, outcome):
    rig.results = [outcome]
    with pytest.raises((gate.AdmissionError, ValueError)):
        rig.harness.fixture_ready()
    assert rig.harness.private_host_context.commands.uncertain_mutation
    assert not records(rig)


@pytest.mark.parametrize("outer_rc", [1, 2, 124, None, True])
def test_outer_transport_status_is_not_the_probe_status(rig, outer_rc):
    rig.outer_rc = outer_rc
    with pytest.raises((gate.AdmissionError, ValueError)):
        rig.harness.fixture_ready()
    assert rig.harness.private_host_context.commands.uncertain_mutation and not records(rig)


@pytest.mark.parametrize("outcome", ["timeout", OSError("synthetic transport denied")])
def test_transport_failure_is_unknown_and_not_retried(rig, outcome):
    rig.results = [outcome]
    with pytest.raises((gate.AdmissionError, ValueError, OSError)):
        rig.harness.fixture_ready()
    assert rig.harness.private_host_context.commands.uncertain_mutation
    assert len([a for a, _ in rig.calls if a[0] == "exec"]) == 1 and not records(rig)


@pytest.mark.parametrize("fault", ["nonce", "schema", "probe", "extra", "missing", "not_json", "too_large",
                                    "duplicate", "whitespace", "trailing"])
def test_marker_requires_exact_fresh_bounded_encoding(rig, fault):
    def change(marker):
        if fault == "nonce":
            marker["nonce"] = "0" * 64
        elif fault == "schema":
            marker["schema"] = True
        elif fault == "probe":
            marker["probe"] = "sender_healthcheck"
        elif fault == "extra":
            marker["raw_error"] = "private-secret"
        elif fault == "missing":
            del marker["status"]
        elif fault == "not_json":
            return "connection refused"
        elif fault == "too_large":
            return json.dumps(marker) + " " * 512
        elif fault == "duplicate":
            return json.dumps(marker, separators=(",", ":")).replace('"schema":1,', '"schema":1,"schema":1,') + "\n"
        elif fault == "whitespace":
            return json.dumps(marker) + "\n"
        elif fault == "trailing":
            return json.dumps(marker, separators=(",", ":")) + "\ntrailing"
        return json.dumps(marker, separators=(",", ":")) + "\n"
    rig.marker_change = change
    with pytest.raises((gate.AdmissionError, ValueError)):
        rig.harness.fixture_ready()
    assert rig.harness.private_host_context.commands.uncertain_mutation and not records(rig)


def changed_row(row, path, value):
    changed = deepcopy(row)
    target = changed
    parts = path.split(".")
    for part in parts[:-1]:
        target = target[part]
    target[parts[-1]] = value
    return changed


@pytest.mark.parametrize("path,value", [
    ("Id", "d" * 64), ("Name", "/wrong-owned-role"), ("Image", "sha256:" + "d" * 64),
    ("Created", "2026-10-30T00:00:02Z"), ("HostConfig.NetworkMode", "host"),
    ("HostConfig.Memory", 384 * 1024**2), ("HostConfig.MemorySwap", -1), ("HostConfig.PidsLimit", 512),
    ("HostConfig.PortBindings", {"8080/tcp": [{"HostPort": "8080"}]}), ("HostConfig.Privileged", True),
    ("State.Running", False), ("State.Pid", 0), ("State.Pid", True), ("State.StartedAt", ""),
    ("State.Dead", True), ("State.Restarting", True), ("State.OOMKilled", True), ("RestartCount", True)])
def test_owned_identity_and_unchanged_resource_limits_are_required(rig, path, value):
    rig.inspect_rows = [changed_row(rig.row, path, value)]
    with pytest.raises((gate.AdmissionError, ValueError)):
        rig.harness.fixture_ready()
    assert rig.harness.private_host_context.commands.uncertain_mutation
    assert not any(a[0] == "exec" for a, _ in rig.calls) and not records(rig)


@pytest.mark.parametrize("path,value", [("Id", "e" * 64), ("RestartCount", 1),
    ("State.Pid", 12347), ("State.StartedAt", "2026-10-30T00:00:03Z"),
    ("NetworkSettings.Networks.synthetic-network.IPAddress", "172.20.0.7")])
def test_restart_or_identity_change_during_probe_still_blocks(rig, path, value):
    rig.inspect_rows = [deepcopy(rig.row), changed_row(rig.row, path, value)]
    with pytest.raises((gate.AdmissionError, ValueError)):
        rig.harness.fixture_ready()
    assert rig.harness.private_host_context.commands.uncertain_mutation
    assert len([a for a, _ in rig.calls if a[0] == "exec"]) == 1 and not records(rig)


@pytest.mark.parametrize("fault", ["owner", "extra_network", "network_id", "loopback", "unspecified", "multicast", "hostname", "ipv6"])
def test_fixed_owned_network_and_address_cannot_be_broadened(rig, fault):
    row = deepcopy(rig.row)
    networks = row["NetworkSettings"]["Networks"]
    if fault == "owner":
        row["Config"]["Labels"][common.LABEL] = "wrong-owner"
    elif fault == "extra_network":
        networks["other-network"] = deepcopy(networks[rig.harness.network])
    elif fault == "network_id":
        networks[rig.harness.network]["NetworkID"] = "f" * 64
    else:
        networks[rig.harness.network]["IPAddress"] = {"loopback": "127.0.0.1", "unspecified": "0.0.0.0",
            "multicast": "224.0.0.1", "hostname": "fixture", "ipv6": "::1"}[fault]
    rig.inspect_rows = [row]
    with pytest.raises((gate.AdmissionError, ValueError)):
        rig.harness.fixture_ready()
    assert rig.harness.private_host_context.commands.uncertain_mutation
    assert not any(a[0] == "exec" for a, _ in rig.calls) and not records(rig)


@pytest.mark.parametrize("fault", ["raw", "same_length", "receipt", "receipt_missing"])
def test_real_original_and_receipt_must_remain_bound(rig, monkeypatch, fault):
    actual_read = native.read_command_result
    def damage(path, sequence, expected):
        if sequence == rig.probe_sequence:
            receipt = path.with_suffix(".result.json")
            if fault == "raw":
                path.write_bytes(path.read_bytes() + b" ")
            elif fault == "same_length":
                before = path.stat()
                raw = path.read_bytes()
                modified = raw.replace(b"fixture_healthcheck", b"fixture_healthfake!")
                assert modified != raw and len(modified) == len(raw)
                path.write_bytes(modified)
                os.utime(path, ns=(before.st_atime_ns, before.st_mtime_ns))
            elif fault == "receipt":
                value = json.loads(receipt.read_bytes())
                value["returncode"] = 1
                receipt.write_text(json.dumps(value))
            else:
                receipt.rename(receipt.with_suffix(".retained"))
        return actual_read(path, sequence, expected)
    monkeypatch.setattr(native, "read_command_result", damage)
    with pytest.raises((gate.AdmissionError, host.ProofError, OSError)):
        rig.harness.fixture_ready()
    assert rig.harness.private_host_context.commands.uncertain_mutation and not records(rig)


@pytest.mark.parametrize("point", ["raw_sync", "receipt_sync", "readiness_sync"])
def test_no_classification_before_all_evidence_is_durable(rig, monkeypatch, point):
    original_save, original_durable = common.save, host.durable
    def save(path, value, **kwargs):
        if point == "raw_sync" and native.FIXTURE_READINESS_PROBE in value.get("argv", []):
            original_sync = os.fsync
            with monkeypatch.context() as patch:
                def fail_sync(fd):
                    if stat.S_ISREG(os.fstat(fd).st_mode):
                        raise OSError("synthetic fixture original sync refused")
                    return original_sync(fd)
                patch.setattr(os, "fsync", fail_sync)
                return original_save(path, value, **kwargs)
        return original_save(path, value, **kwargs)
    def durable(path, value):
        target = (point == "receipt_sync" and path.name == f"command-{rig.probe_sequence:04d}.result.json"
                  if rig.probe_sequence is not None else False)
        target = target or (point == "readiness_sync" and path.name.startswith("FIXTURE_READINESS_"))
        if target:
            with monkeypatch.context() as patch:
                patch.setattr(os, "fsync", lambda fd: (_ for _ in ()).throw(OSError("synthetic sync refused")))
                return original_durable(path, value)
        return original_durable(path, value)
    monkeypatch.setattr(common, "save", save)
    monkeypatch.setattr(host, "durable", durable)
    with pytest.raises(OSError, match="sync refused"):
        rig.harness.fixture_ready()
    assert rig.harness.private_host_context.commands.uncertain_mutation
    assert not hasattr(rig.harness, "fixture_readiness_counts")


def test_existing_phase_and_active_deadlines_still_bound_every_command(rig):
    rig.harness.phase_deadline = time.monotonic() + 0.75
    rig.results = [("not_ready", "econnrefused")]
    assert rig.harness.fixture_ready() is False
    assert all(0 < kw["timeout"] <= 0.75 for _, kw in rig.calls)


def test_default_fixture_ready_keeps_generic_http_contract(rig, monkeypatch):
    rig.harness.private_host_context = None
    calls = []
    monkeypatch.setattr(rig.harness, "http", lambda *args, **kwargs: calls.append((args, kwargs)) or {"status": "ready"})
    assert rig.harness.fixture_ready() == {"status": "ready"}
    assert calls == [(("fixture", "/health"), {})] and not rig.calls and not records(rig)


def test_generic_http_failure_stays_blocking_instead_of_clearing_old_evidence(rig):
    with pytest.raises(RuntimeError, match="failed rc=1"):
        rig.harness.http("fixture", "/health")
    assert rig.harness.private_host_context.commands.uncertain_mutation
    assert not records(rig)


def test_prepare_dispatches_through_fixed_fixture_readiness_hook(rig, monkeypatch):
    h = rig.harness
    h.repo, h.image_id = h.output, "sha256:" + "c" * 64
    image = {"Id": h.image_id, "Architecture": "amd64", "Os": "linux", "RepoDigests": []}
    def command(args, **kwargs):
        if args[0] == "info":
            return json.dumps({"Architecture": "x86_64"})
        if args[:2] == ["image", "inspect"]:
            return json.dumps([image])
        assert args[:2] == ["volume", "create"]
        return "synthetic-owned-volume"
    monkeypatch.setattr(h, "command", command)
    monkeypatch.setattr(h, "prepare_network", lambda: None)
    monkeypatch.setattr(h, "launch", lambda *a, **k: None)
    monkeypatch.setattr(h, "postgres_ready", lambda: True)
    monkeypatch.setattr(common, "native_host", lambda info: None)
    monkeypatch.setattr(common, "image_checks", lambda *a: None)
    outcomes, calls = iter([False, True]), []
    def ready():
        calls.append("fixed-fixture")
        return next(outcomes)
    monkeypatch.setattr(h, "fixture_ready", ready)
    monkeypatch.setattr(h, "http", lambda *a, **k: pytest.fail("generic HTTP cannot classify fixture readiness"))
    h.prepare()
    assert calls == ["fixed-fixture", "fixed-fixture"]


def test_real_probe_program_with_isolated_mock_transport_and_no_raw_leaks():
    program = r'''
import errno, io, json, socket, sys, urllib.error, urllib.request
p=json.loads(sys.stdin.read()); case=p['case']; seen=[]
class Response:
 status=200
 def __enter__(self): return self
 def __exit__(self,*args): pass
 def read(self,limit):
  assert limit==513
  if case=='body_timeout': raise TimeoutError('PRIVATE_BODY_TIMEOUT')
  return {'empty':b'', 'false':b'false', 'wrong':b'{"status":"other"}',
   'extra':b'{"status":"synthetic_fixture_ready","extra":1}', 'whitespace':b' {"status":"synthetic_fixture_ready"}',
   'duplicate':b'{"status":"other","status":"synthetic_fixture_ready"}',
   'oversize':b'x'*513,'invalid_utf8':b'\xff'}.get(case,b'{"status":"synthetic_fixture_ready"}')
class Opener:
 def open(self,request,timeout):
  assert request.full_url=='http://172.20.0.2:8080/health'
  assert request.get_method()=='GET' and timeout==12 and not request.headers
  seen.append(1)
  if case=='refused': raise urllib.error.URLError(ConnectionRefusedError(errno.ECONNREFUSED,'PRIVATE_ERROR'))
  if case=='refused_oserror': raise urllib.error.URLError(OSError(errno.ECONNREFUSED,'PRIVATE_ERROR'))
  if case=='float_errno':
   error=OSError(errno.ECONNREFUSED,'PRIVATE_ERROR'); error.errno=float(errno.ECONNREFUSED)
   raise urllib.error.URLError(error)
  if case=='refused_text': raise urllib.error.URLError('Connection refused errno 111 PRIVATE_ERROR')
  if case=='dns': raise urllib.error.URLError(socket.gaierror(-2,'PRIVATE_ERROR'))
  if case=='permission': raise urllib.error.URLError(PermissionError(errno.EACCES,'PRIVATE_ERROR'))
  if case=='reset': raise urllib.error.URLError(ConnectionResetError(errno.ECONNRESET,'PRIVATE_ERROR'))
  if case=='timeout': raise urllib.error.URLError(TimeoutError('PRIVATE_ERROR'))
  if case=='timeout_direct': raise TimeoutError('PRIVATE_ERROR')
  if case in ('503','401','302'):
   raise urllib.error.HTTPError(request.full_url,int(case),'PRIVATE_ERROR',{},io.BytesIO(b'PRIVATE_BODY'))
  return Response()
def opener(*handlers):
 assert any(isinstance(h,urllib.request.ProxyHandler) and h.proxies=={} for h in handlers)
 assert any(isinstance(h,urllib.request.HTTPRedirectHandler) and h.redirect_request(None,None,None,None,None,None) is None for h in handlers)
 return Opener()
urllib.request.build_opener=opener
urllib.request.urlopen=lambda *a,**k: (_ for _ in ()).throw(AssertionError('uncontrolled transport'))
sys.stdin=io.StringIO(json.dumps({'nonce':p['nonce'],'fixture_ip':'172.20.0.2'}))
exec(compile(p['source'],'<fixed-fixture-probe>','exec'),{'__name__':'__main__'})
assert len(seen)==1
'''
    known = {"ready": ("ready", "http200"), "503": ("not_ready", "http503"),
             "refused": ("not_ready", "econnrefused"), "refused_oserror": ("not_ready", "econnrefused")}
    unknown = ["empty", "false", "wrong", "extra", "whitespace", "duplicate", "oversize", "invalid_utf8",
               "body_timeout", "float_errno", "refused_text", "dns", "permission", "reset", "timeout",
               "timeout_direct", "401", "302"]
    for case, (status, reason) in (known | {name: ("unknown", "unknown") for name in unknown}).items():
        nonce = "d" * 64
        result = subprocess.run([sys.executable, "-I", "-B", "-c", program],
            input=json.dumps({"case": case, "nonce": nonce, "source": native.FIXTURE_READINESS_PROBE}),
            capture_output=True, text=True, timeout=3, check=False)
        assert result.returncode == 0, result.stderr
        assert result.stderr == ""
        expected = {"schema": 1, "probe": "fixture_healthcheck", "nonce": nonce, "status": status, "reason": reason}
        assert result.stdout == json.dumps(expected, separators=(",", ":")) + "\n"
        assert len(result.stdout.encode()) <= 512 and "PRIVATE" not in result.stdout
