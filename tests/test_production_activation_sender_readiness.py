"""Fixed sender readiness contracts; all transports here are synthetic and local."""
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


@pytest.fixture(autouse=True)
def private_evidence_mode():
    previous = os.umask(0o077)
    try:
        yield
    finally:
        os.umask(previous)


def container(role, identifier, image, owner, network, pid):
    return {"Id": identifier, "Name": "/synthetic-owned-" + role, "Image": image,
        "Created": "2026-10-30T00:00:00Z", "RestartCount": 0,
        "Config": {"Labels": {common.LABEL: owner}},
        "NetworkSettings": {"Networks": {network: {"NetworkID": "e" * 64,
            "IPAddress": "172.20.0.2" if role == "fixture" else "172.20.0.3"}}},
        "State": {"Running": True, "Dead": False, "Restarting": False, "OOMKilled": False,
                  "Pid": pid, "StartedAt": "2026-10-30T00:00:01Z"},
        "HostConfig": {"NetworkMode": network, "PortBindings": {}, "Memory": common.MEMORY,
                       "MemorySwap": common.MEMORY, "PidsLimit": common.PIDS, "NanoCpus": 1_000_000_000,
                       "Privileged": False, "SecurityOpt": ["no-new-privileges"]}}


@pytest.fixture
def rig(tmp_path, monkeypatch):
    harness = object.__new__(native.ActivationHarness)
    harness.output, harness.sequence, harness.started = tmp_path, 0, time.monotonic()
    harness.phase_deadline, harness.env = None, {}
    harness.password, harness.token, harness.auth = "unused-password", "unused-token", "private-synthetic-auth"
    harness.owner, harness.prefix, harness.network = "a" * 32, "synthetic-owned", "synthetic-network"
    harness.names, harness.images, harness.created = {}, {}, []
    harness.private_host_context = SimpleNamespace(check=lambda **kwargs: None,
        plan={"deadline": time.monotonic() + 60, "active_deadline": time.monotonic() + 40},
        commands=SimpleNamespace(uncertain_mutation=False))
    rows = {role: container(role, char * 64, "sha256:" + char * 64, harness.owner, harness.network, pid)
            for role, char, pid in (("fixture", "b", 12345), ("sender", "c", 12346))}
    value = SimpleNamespace(harness=harness, rows=rows, calls=[], inspect_rows={"fixture": [], "sender": []},
                            results=[], outer_rc=0, marker_change=None, probe_sequence=None)

    def run(argv, **kwargs):
        args = argv[len(common.DOCKER):]
        value.calls.append((list(args), kwargs))
        if args[0] == "run":
            role = args[args.index("--name") + 1].rsplit("-", 1)[1]
            return subprocess.CompletedProcess(argv, 0, rows[role]["Id"] + "\n", "")
        if args[0] == "inspect":
            role = next(role for role, row in rows.items() if args[1] in (row["Id"], row["Name"].lstrip("/")))
            queue = value.inspect_rows[role]
            row = queue.pop(0) if queue else rows[role]
            return subprocess.CompletedProcess(argv, 0, json.dumps([row]), "")
        assert args[0] == "exec", "synthetic transport refuses unexpected operations"
        value.probe_sequence = harness.sequence
        outcome = value.results.pop(0) if value.results else ("ready", "http200")
        if outcome == "timeout":
            raise subprocess.TimeoutExpired(argv, kwargs["timeout"], output=b"partial-result")
        if isinstance(outcome, BaseException):
            raise outcome
        if native.SENDER_READINESS_PROBE in args:
            payload = json.loads(kwargs["input"])
            marker = {"schema": 1, "probe": "sender_healthcheck", "nonce": payload["nonce"],
                      "status": outcome[0], "reason": outcome[1]}
            raw = json.dumps(marker, separators=(",", ":")) + "\n"
            if value.marker_change:
                raw = value.marker_change(marker)
            return subprocess.CompletedProcess(argv, value.outer_rc, raw, "")
        return subprocess.CompletedProcess(argv, 1, "", "synthetic generic client failure")

    monkeypatch.setattr(common.subprocess, "run", run)
    for role, row in rows.items():
        harness.launch(role, row["Image"])
    value.calls.clear()
    return value


def records(rig):
    return [json.loads(p.read_bytes()) for p in sorted(rig.harness.output.glob("SENDER_READINESS_*.json"))]


@pytest.mark.parametrize("status,reason,expected", [
    ("ready", "http200", True), ("not_ready", "http200_empty", False),
    ("not_ready", "http200_falsey", False),
    ("not_ready", "http503", False), ("not_ready", "econnrefused", False)])
def test_only_fixed_confirmed_marker_classifies_readiness(rig, status, reason, expected):
    rig.results = [(status, reason)]
    assert rig.harness.sender_ready() is expected
    assert not rig.harness.private_host_context.commands.uncertain_mutation
    actual, = records(rig)
    assert actual["status"] == status and actual["reason"] == reason
    assert actual["transport_returncode"] == 0
    commands = [args for args, _ in rig.calls]
    probes = [(args, kw) for args, kw in rig.calls if args[0] == "exec"]
    assert len(probes) == 1
    args, kwargs = probes[0]
    assert args == ["exec", "-i", rig.rows["fixture"]["Id"], "python", "-B", "-c", native.SENDER_READINESS_PROBE]
    assert 0 < kwargs["timeout"] <= 15
    payload = json.loads(kwargs["input"])
    assert set(payload) == {"nonce", "auth", "sender_ip"} and payload["auth"] == rig.harness.auth
    assert payload["sender_ip"] == "172.20.0.3"
    assert len(payload["nonce"]) == 64 and int(payload["nonce"], 16) >= 0
    for row in rig.rows.values():
        assert commands.count(["inspect", row["Id"]]) == 2
    assert actual["nonce_sha256"] == hashlib.sha256(payload["nonce"].encode()).hexdigest()
    original = rig.harness.output / f"command-{rig.probe_sequence:04d}.json"
    receipt = original.with_suffix(".result.json")
    saved = common.read_command_result(original, rig.probe_sequence, json.loads(receipt.read_bytes()))
    assert saved["returncode"] == 0
    assert actual["command_original_sha256"] == hashlib.sha256(original.read_bytes()).hexdigest()
    assert all(stat.S_IMODE(p.stat().st_mode) == 0o600 for p in (original, receipt))
    assert rig.harness.sender_readiness_counts == {"ready": int(expected), "not_ready": int(not expected)}
    assert rig.harness.auth not in json.dumps(actual)


def test_known_not_ready_then_ready_keeps_fresh_receipts_without_sticky(rig):
    rig.results = [("not_ready", "econnrefused"), ("ready", "http200")]
    assert rig.harness.sender_ready() is False
    assert rig.harness.sender_ready() is True
    assert [r["status"] for r in records(rig)] == ["not_ready", "ready"]
    assert len({r["nonce_sha256"] for r in records(rig)}) == 2
    assert rig.harness.sender_readiness_counts == {"ready": 1, "not_ready": 1}
    assert not rig.harness.private_host_context.commands.uncertain_mutation


@pytest.mark.parametrize("status,reason", [("ready", "http200"), ("not_ready", "econnrefused")])
def test_classification_never_clears_prior_uncertainty(rig, status, reason):
    rig.harness.private_host_context.commands.uncertain_mutation = True
    rig.results = [(status, reason)]
    assert rig.harness.sender_ready() is (status == "ready")
    assert rig.harness.private_host_context.commands.uncertain_mutation is True


@pytest.mark.parametrize("outcome", [("unknown", "unknown"), ("ready", "econnrefused"),
                                    ("not_ready", "http200"), ("not_ready", "timeout"),
                                    ("not_ready", "dns"), ("not_ready", "permission")])
def test_unknown_or_inconsistent_marker_remains_sticky(rig, outcome):
    rig.results = [outcome]
    with pytest.raises((gate.AdmissionError, ValueError)):
        rig.harness.sender_ready()
    assert rig.harness.private_host_context.commands.uncertain_mutation
    assert not records(rig)


@pytest.mark.parametrize("outer_rc", [1, 2, 124, None, True])
def test_outer_transport_nonzero_cannot_masquerade_as_confirmed_readiness(rig, outer_rc):
    rig.outer_rc = outer_rc
    with pytest.raises((gate.AdmissionError, ValueError)):
        rig.harness.sender_ready()
    assert rig.harness.private_host_context.commands.uncertain_mutation
    assert not records(rig)


@pytest.mark.parametrize("outcome", ["timeout", OSError("synthetic transport denied")])
def test_outer_transport_failure_preserves_unknown_and_never_retries(rig, outcome):
    rig.results = [outcome]
    with pytest.raises((gate.AdmissionError, ValueError, OSError)) as caught:
        rig.harness.sender_ready()
    if isinstance(outcome, BaseException):
        assert caught.value is outcome
    assert rig.harness.private_host_context.commands.uncertain_mutation
    assert len([args for args, _ in rig.calls if args[0] == "exec"]) == 1
    assert not records(rig)


@pytest.mark.parametrize("fault", ["nonce", "schema", "probe", "extra", "missing", "not_json", "too_large",
                                    "list", "duplicate", "whitespace", "trailing"])
def test_marker_must_be_fresh_canonical_complete_and_bounded(rig, fault):
    def change(marker):
        if fault == "nonce":
            marker["nonce"] = "0" * 64
        elif fault == "schema":
            marker["schema"] = True
        elif fault == "probe":
            marker["probe"] = "other_endpoint"
        elif fault == "extra":
            marker["raw_error"] = "private-auth-secret"
        elif fault == "missing":
            del marker["status"]
        elif fault == "not_json":
            return "connection refused"
        elif fault == "too_large":
            return json.dumps(marker) + " " * 512
        elif fault == "list":
            return json.dumps([marker])
        elif fault == "duplicate":
            return json.dumps(marker, separators=(",", ":")).replace('"schema":1,', '"schema":1,"schema":1,') + "\n"
        elif fault == "whitespace":
            return json.dumps(marker) + "\n"
        elif fault == "trailing":
            return json.dumps(marker, separators=(",", ":")) + "\ntrailing"
        return json.dumps(marker, separators=(",", ":")) + "\n"
    rig.marker_change = change
    with pytest.raises((gate.AdmissionError, ValueError)):
        rig.harness.sender_ready()
    assert rig.harness.private_host_context.commands.uncertain_mutation
    assert not records(rig)


def set_nested(row, path, value):
    if path == "owner":
        row["Config"]["Labels"][common.LABEL] = value
        return
    parts = path.split(".")
    for part in parts[:-1]:
        row = row[part]
    row[parts[-1]] = value


@pytest.mark.parametrize("role", ["fixture", "sender"])
@pytest.mark.parametrize("path,value", [
    ("Id", "d" * 64), ("Name", "/wrong-owned-role"), ("Image", "sha256:" + "d" * 64),
    ("Created", "2026-10-30T00:00:02Z"), ("owner", "wrong-owner"),
    ("HostConfig.NetworkMode", "host"), ("HostConfig.Memory", 384 * 1024**2),
    ("HostConfig.MemorySwap", -1), ("HostConfig.PidsLimit", 512),
    ("HostConfig.PortBindings", {"8080/tcp": [{"HostPort": "8080"}]}),
    ("HostConfig.Privileged", True), ("State.Running", False), ("State.Pid", 0),
    ("State.Pid", True), ("State.StartedAt", ""), ("State.Dead", True),
    ("State.Restarting", True), ("State.OOMKilled", True), ("RestartCount", True)])
def test_both_probe_client_and_sender_must_have_bound_owned_identity(rig, role, path, value):
    changed = deepcopy(rig.rows[role])
    set_nested(changed, path, value)
    rig.inspect_rows[role] = [changed]
    with pytest.raises((gate.AdmissionError, ValueError)):
        rig.harness.sender_ready()
    assert rig.harness.private_host_context.commands.uncertain_mutation
    assert not any(args[0] == "exec" for args, _ in rig.calls)
    assert not records(rig)


@pytest.mark.parametrize("role", ["fixture", "sender"])
@pytest.mark.parametrize("path,value", [("Id", "e" * 64), ("RestartCount", 1),
                                       ("State.Pid", 12347), ("State.StartedAt", "2026-10-30T00:00:03Z")])
def test_post_probe_restart_or_identity_drift_is_not_ready_confirmation(rig, role, path, value):
    changed = deepcopy(rig.rows[role])
    set_nested(changed, path, value)
    rig.inspect_rows[role] = [deepcopy(rig.rows[role]), changed]
    with pytest.raises((gate.AdmissionError, ValueError)):
        rig.harness.sender_ready()
    assert rig.harness.private_host_context.commands.uncertain_mutation
    assert len([args for args, _ in rig.calls if args[0] == "exec"]) == 1
    assert not records(rig)


@pytest.mark.parametrize("role", ["fixture", "sender"])
@pytest.mark.parametrize("fault", ["extra_network", "network_id", "loopback", "unspecified", "multicast", "hostname", "ipv6"])
def test_network_identity_never_broadens_fixed_sender_target(rig, role, fault):
    changed = deepcopy(rig.rows[role])
    networks = changed["NetworkSettings"]["Networks"]
    if fault == "extra_network":
        networks["other-network"] = deepcopy(networks[rig.harness.network])
    elif fault == "network_id":
        networks[rig.harness.network]["NetworkID"] = "f" * 64
    else:
        networks[rig.harness.network]["IPAddress"] = {
            "loopback": "127.0.0.1", "unspecified": "0.0.0.0", "multicast": "224.0.0.1",
            "hostname": "sender", "ipv6": "::1"}[fault]
    rig.inspect_rows[role] = [changed]
    with pytest.raises((gate.AdmissionError, ValueError)):
        rig.harness.sender_ready()
    assert rig.harness.private_host_context.commands.uncertain_mutation
    assert not any(args[0] == "exec" for args, _ in rig.calls)
    assert not records(rig)


@pytest.mark.parametrize("role", ["fixture", "sender"])
def test_probe_window_ip_change_is_rejected(rig, role):
    changed = deepcopy(rig.rows[role])
    changed["NetworkSettings"]["Networks"][rig.harness.network]["IPAddress"] = "172.20.0.7"
    rig.inspect_rows[role] = [deepcopy(rig.rows[role]), changed]
    with pytest.raises(gate.AdmissionError, match="identity changed"):
        rig.harness.sender_ready()
    assert rig.harness.private_host_context.commands.uncertain_mutation
    assert not records(rig)


@pytest.mark.parametrize("fault", ["raw", "same_length", "receipt", "receipt_missing"])
def test_probe_requires_real_original_and_exact_durable_receipt(rig, monkeypatch, fault):
    actual_read = native.read_command_result
    def damage(path, sequence, expected):
        if sequence == rig.probe_sequence:
            receipt = path.with_suffix(".result.json")
            if fault == "raw":
                path.write_bytes(path.read_bytes() + b" ")
            elif fault == "same_length":
                before = path.stat()
                raw = path.read_bytes()
                modified = raw.replace(b"sender_healthcheck", b"sender_healthfake!")
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
        rig.harness.sender_ready()
    assert rig.harness.private_host_context.commands.uncertain_mutation
    assert not records(rig)


@pytest.mark.parametrize("point", ["raw_sync", "receipt_sync", "readiness_sync"])
def test_no_readiness_classification_before_all_durable_evidence(rig, monkeypatch, point):
    original_save, original_durable = common.save, host.durable
    def save(path, value, **kwargs):
        if point == "raw_sync" and native.SENDER_READINESS_PROBE in value.get("argv", []):
            original_sync = os.fsync
            with monkeypatch.context() as patch:
                def fail_sync(fd):
                    if stat.S_ISREG(os.fstat(fd).st_mode):
                        raise OSError("synthetic sender original sync refused")
                    return original_sync(fd)
                patch.setattr(os, "fsync", fail_sync)
                return original_save(path, value, **kwargs)
        return original_save(path, value, **kwargs)
    def durable(path, value):
        target = (point == "receipt_sync" and path.name == f"command-{rig.probe_sequence:04d}.result.json"
                  if rig.probe_sequence is not None else False)
        target = target or (point == "readiness_sync" and path.name.startswith("SENDER_READINESS_"))
        if target:
            with monkeypatch.context() as patch:
                patch.setattr(os, "fsync", lambda fd: (_ for _ in ()).throw(OSError("synthetic sync refused")))
                return original_durable(path, value)
        return original_durable(path, value)
    monkeypatch.setattr(common, "save", save)
    monkeypatch.setattr(host, "durable", durable)
    with pytest.raises(OSError, match="sync refused"):
        rig.harness.sender_ready()
    assert rig.harness.private_host_context.commands.uncertain_mutation
    assert not hasattr(rig.harness, "sender_readiness_counts")


def test_same_phase_and_active_deadlines_bound_the_single_probe(rig):
    rig.harness.phase_deadline = time.monotonic() + 0.75
    rig.results = [("not_ready", "econnrefused")]
    assert rig.harness.sender_ready() is False
    assert len([args for args, _ in rig.calls if args[0] == "exec"]) == 1
    assert all(0 < kw["timeout"] <= 0.75 for _, kw in rig.calls)


def test_default_sender_ready_keeps_existing_generic_http(rig, monkeypatch):
    rig.harness.private_host_context = None
    calls = []
    monkeypatch.setattr(rig.harness, "http", lambda *args, **kwargs: calls.append((args, kwargs)) or "OK")
    assert rig.harness.sender_ready() == "OK"
    assert calls == [(("sender", "/healthcheck"), {})]
    assert not rig.calls and not records(rig)


def test_generic_client_and_arbitrary_exec_failure_are_unchanged(rig):
    assert hashlib.sha256(common.CLIENT.encode()).hexdigest() == "e3c4ec053fa9d4949a8af374d35d06d1ba44d18e6baa9115b7410d75be0ca324"
    with pytest.raises(RuntimeError, match="failed rc=1"):
        rig.harness.command(["exec", rig.rows["fixture"]["Id"], "python", "-B", "-c", common.CLIENT])
    assert rig.harness.private_host_context.commands.uncertain_mutation
    assert not records(rig)


def test_fixed_probe_in_local_python_has_no_network_and_no_raw_leaks():
    # A separate interpreter replaces only urllib's transport. The real fixed
    # probe executes with its real parsing and exception classifier, not a copy.
    program = r'''
import errno, io, json, socket, sys, urllib.error, urllib.request
payload = json.loads(sys.stdin.read())
case, nonce, source = payload['case'], payload['nonce'], payload['source']
seen = []
class Response:
 status = 200
 def __enter__(self): return self
 def __exit__(self, *args): pass
 def read(self, limit):
  assert limit <= 1048576
  if case == 'body_timeout': raise TimeoutError('PRIVATE_BODY_TIMEOUT')
  return {'empty': b'', 'false': b'false', 'null': b'null', 'zero': b'0',
          'empty_object': b'{}', 'empty_list': b'[]', 'empty_string': b'""', 'invalid_utf8': b'\xff',
          'truthy_json': b'{"ok":true}', 'false_string': b'"false"'}.get(case, b'PRIVATE_RESPONSE_BODY')
class Opener:
 def open(self, request, timeout):
  assert request.full_url == 'http://172.20.0.3:8080/healthcheck'
  assert request.get_method() == 'GET' and timeout == 12
  seen.append(1)
  if case == 'refused': raise urllib.error.URLError(ConnectionRefusedError(errno.ECONNREFUSED, 'PRIVATE_ERROR'))
  if case == 'refused_oserror': raise urllib.error.URLError(OSError(errno.ECONNREFUSED, 'PRIVATE_ERROR'))
  if case == 'float_errno':
   error = OSError(errno.ECONNREFUSED, 'PRIVATE_ERROR'); error.errno = float(errno.ECONNREFUSED)
   raise urllib.error.URLError(error)
  if case == 'refused_text': raise urllib.error.URLError('Connection refused errno 111 PRIVATE_ERROR')
  if case == 'dns': raise urllib.error.URLError(socket.gaierror(-2, 'PRIVATE_ERROR'))
  if case == 'permission': raise urllib.error.URLError(PermissionError(errno.EACCES, 'PRIVATE_ERROR'))
  if case == 'permission_direct': raise PermissionError(errno.EACCES, 'PRIVATE_ERROR')
  if case == 'reset': raise urllib.error.URLError(ConnectionResetError(errno.ECONNRESET, 'PRIVATE_ERROR'))
  if case == 'timeout': raise urllib.error.URLError(TimeoutError('PRIVATE_ERROR'))
  if case == 'timeout_direct': raise TimeoutError('PRIVATE_ERROR')
  if case in ('503', '401', '302'):
   raise urllib.error.HTTPError(request.full_url, int(case), 'PRIVATE_ERROR', {}, io.BytesIO(b'PRIVATE_BODY'))
  return Response()
def opener(*handlers):
 assert any(isinstance(h, urllib.request.ProxyHandler) and h.proxies == {} for h in handlers)
 assert any(isinstance(h, urllib.request.HTTPRedirectHandler) and h.redirect_request(None, None, None, None, None, None) is None for h in handlers)
 return Opener()
urllib.request.build_opener = opener
urllib.request.urlopen = lambda *a, **k: (_ for _ in ()).throw(AssertionError('uncontrolled transport'))
sys.stdin = io.StringIO(json.dumps({'nonce': nonce, 'auth': 'PRIVATE_AUTH', 'sender_ip': '172.20.0.3'}))
exec(compile(source, '<fixed-sender-probe>', 'exec'), {'__name__': '__main__'})
assert len(seen) == 1
'''
    cases = {"ready": ("ready", "http200"), "truthy_json": ("ready", "http200"),
             "false_string": ("ready", "http200"),
             "empty": ("not_ready", "http200_empty"),
             "false": ("not_ready", "http200_falsey"), "null": ("not_ready", "http200_falsey"),
             "zero": ("not_ready", "http200_falsey"), "empty_object": ("not_ready", "http200_falsey"),
             "empty_list": ("not_ready", "http200_falsey"), "empty_string": ("not_ready", "http200_falsey"),
             "invalid_utf8": ("unknown", "unknown"), "body_timeout": ("unknown", "unknown"),
             "503": ("not_ready", "http503"), "refused": ("not_ready", "econnrefused"),
             "refused_oserror": ("not_ready", "econnrefused"), "float_errno": ("unknown", "unknown"),
             "refused_text": ("unknown", "unknown"), "dns": ("unknown", "unknown"),
             "permission": ("unknown", "unknown"), "permission_direct": ("unknown", "unknown"),
             "reset": ("unknown", "unknown"), "timeout": ("unknown", "unknown"),
             "timeout_direct": ("unknown", "unknown"),
             "401": ("unknown", "unknown"), "302": ("unknown", "unknown")}
    for case, (status, reason) in cases.items():
        nonce = "d" * 64
        result = subprocess.run([sys.executable, "-I", "-B", "-c", program],
            input=json.dumps({"case": case, "nonce": nonce, "source": native.SENDER_READINESS_PROBE}),
            capture_output=True, text=True, timeout=3, check=False)
        assert result.returncode == 0, result.stderr
        assert result.stderr == ""
        expected = {"schema": 1, "probe": "sender_healthcheck", "nonce": nonce, "status": status, "reason": reason}
        assert result.stdout == json.dumps(expected, separators=(",", ":")) + "\n"
        assert len(result.stdout.encode()) <= 512 and "PRIVATE" not in result.stdout
