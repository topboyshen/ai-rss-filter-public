"""Fixture wiring guards; mocked Docker/HTTP does not prove native execution."""

from copy import deepcopy
import errno
import hashlib
import json
from pathlib import Path
from types import SimpleNamespace
import time

import pytest

from scripts import production_gateway_native as native
from scripts import production_gateway_switch as gateway
from services import admission_fence as gate


def mapping_fault(row, fault):
    if fault == "empty":
        row["NetworkSettings"]["Ports"] = {}
    elif fault == "missing":
        row["NetworkSettings"].pop("Ports")
    elif fault == "null":
        row["NetworkSettings"]["Ports"] = None
    elif fault == "wrong_port":
        row["NetworkSettings"]["Ports"]["5000/tcp"][0]["HostPort"] = "35679"
    elif fault == "wrong_host":
        row["NetworkSettings"]["Ports"]["5000/tcp"][0]["HostIp"] = "0.0.0.0"
    elif fault == "extra_bound":
        row["NetworkSettings"]["Ports"]["80/tcp"] = [{"HostIp": "127.0.0.1", "HostPort": "80"}]
    elif fault == "extra_unpublished":
        row["NetworkSettings"]["Ports"]["80/tcp"] = None
    elif fault == "requested":
        row["HostConfig"]["PortBindings"]["5000/tcp"][0]["HostPort"] = "35679"


@pytest.fixture
def case(tmp_path, monkeypatch):
    directory = tmp_path.resolve() / "receiver"
    directory.mkdir(mode=0o700)
    manifest = {
        "name": "synthetic-app",
        "services": {
            name: {"environment": {"UNCHANGED": "synthetic"}} for name in ("ai-filter-api", "ai-filter-worker")
        },
    }
    gate._write(directory / "manifest.json", manifest)
    p = {
        "directory": directory,
        "manifest": manifest,
        "manifest_path": directory / "manifest.json",
        "project": "synthetic-app",
        "source": {"source_sha": "a" * 40},
    }
    owner, identifier, image_id, network_id = "b" * 32, "c" * 64, "sha256:" + "d" * 64, "e" * 64
    network = {
        "Name": "owned-internal",
        "Id": network_id,
        "Internal": True,
        "Labels": {native.LABEL: owner},
        "Driver": "bridge",
        "Scope": "local",
    }
    image = {
        "Id": image_id,
        "Architecture": "amd64",
        "Os": "linux",
        "RepoDigests": ["nginx@" + gateway.NGINX_REF.split("@", 1)[1]],
    }
    state = SimpleNamespace(rows={}, calls=[], fail=None, mapping_fault=None, runtime_fault=None,
                            network=network, image=image, kills=0)
    harness = SimpleNamespace(
        owner=owner,
        prefix="synthetic-owned",
        network=network["Name"],
        output=tmp_path.resolve(),
        names={},
        images={},
        created=[],
    )

    def command(args, **kwargs):
        state.calls.append((deepcopy(args), kwargs))
        if args[:2] == ["network", "inspect"]:
            return json.dumps([network])
        if args[:2] == ["image", "inspect"]:
            assert args[2] == gateway.NGINX_REF
            return json.dumps([image])
        if args[0] == "create":
            labels = dict(args[i + 1].split("=", 1) for i, value in enumerate(args) if value == "--label")
            source = args[args.index("--mount") + 1].split(",src=", 1)[1].split(",dst=", 1)[0]
            state.rows[identifier] = {
                "Id": identifier,
                "Image": image_id,
                "Name": "/" + args[args.index("--name") + 1],
                "RestartCount": 0,
                "Config": {"Labels": labels},
                "State": {"Running": False, "Status": "created", "StartedAt": "", "ExitCode": 0, "OOMKilled": False},
                "HostConfig": {"Memory": 128 * 1024**2, "MemorySwap": 128 * 1024**2, "PidsLimit": 64,
                               "PortBindings": {"5000/tcp": [{"HostIp": "127.0.0.1", "HostPort": "35678"}]}},
                "Mounts": [{"Source": source, "Destination": gateway.CONFIG_TARGET, "RW": False}],
                "NetworkSettings": {"Networks": {network["Name"]: {"NetworkID": network_id}},
                                    "Ports": {"5000/tcp": [{"HostIp": "127.0.0.1", "HostPort": "35678"}]}},
            }
            mapping_fault(state.rows[identifier], state.mapping_fault)
            if state.fail == "create":
                raise TimeoutError("synthetic create ACK lost")
            return identifier + "\n"
        if args[0] == "start":
            state.rows[args[-1]]["State"].update(Running=True, Status="running", StartedAt="actual-synthetic-start")
            row = state.rows[args[-1]]
            if state.runtime_fault == "image":
                row["Image"] = "sha256:" + "f" * 64
            elif state.runtime_fault == "owner":
                row["Config"]["Labels"][native.LABEL] = "f" * 32
            elif state.runtime_fault == "restarted":
                row["RestartCount"] = 1
            elif state.runtime_fault:
                row["State"][state.runtime_fault] = False if state.runtime_fault == "Running" else True
            return identifier
        if args[0] == "kill":
            assert args == ["kill", "--signal", "TERM", identifier]
            state.kills += 1
            state.rows[identifier]["State"].update(Running=False, Status="exited", ExitCode=143, FinishedAt="2026-10-31T13:00:00.123456789Z")
            if state.fail == "term":
                raise TimeoutError("synthetic TERM ACK lost")
            return identifier
        if args[0] == "inspect":
            row = next(r for r in state.rows.values() if args[1] in {r["Id"], r["Name"].lstrip("/")})
            return json.dumps([row])
        if args[0] == "logs":
            return "synthetic retained nginx logs\n"
        raise AssertionError(args)

    def wait(function, **_kwargs):
        result = function()
        if result is None:
            raise TimeoutError("synthetic bounded wait")
        return result

    harness.command, harness.wait = command, wait
    harness.safe_inspect = lambda row: {k: deepcopy(row[k]) for k in ("Id", "State", "HostConfig", "Mounts")}
    monkeypatch.setattr(native, "_port", lambda: 35678)
    monkeypatch.setattr(
        gateway.NativeTransport,
        "health",
        lambda *_args: {
            "status": 200,
            "body_sha256": hashlib.sha256(b"OLD").hexdigest(),
            "operation": None,
            "upstream": None,
        },
    )
    monkeypatch.setattr(gateway.NativeTransport, "find", lambda *_args: None)
    return SimpleNamespace(p=p, harness=harness, state=state, identifier=identifier)


@pytest.mark.parametrize("fault", [None, "missing", "null", "empty", "wrong_port", "extra_bound", "extra_unpublished", "requested"])
def test_private_host_proof_never_replaces_actual_ports_guard(case, monkeypatch, fault):
    from scripts.production_activation_host_native import PrivateHostContext
    c, calls, contexts = case, [], []
    context = object.__new__(PrivateHostContext)
    context.check_network = lambda row, owner: contexts.append((row["Id"], owner))
    c.harness.private_host_context = context
    c.state.network["Internal"] = False
    c.state.mapping_fault = fault
    def health(*args):
        calls.append("actual_host_http_after_port_guard")
        return {"status": 200, "body_sha256": hashlib.sha256(b"OLD").hexdigest(), "operation": None, "upstream": None}
    monkeypatch.setattr(gateway.NativeTransport, "health", health)
    if fault not in {None, "extra_unpublished"}:
        with pytest.raises(gate.AdmissionError):
            native.prepare(c.harness, c.p)
        assert calls == []
    else:
        native.prepare(c.harness, c.p)
        assert len(calls) == 1
    assert contexts == [(c.state.network["Id"], c.harness.owner)]


def test_ordinary_bridge_without_exact_context_refuses_before_create_or_http(case, monkeypatch):
    c = case
    c.state.network["Internal"] = False
    c.harness.private_host_context = SimpleNamespace(check_network=lambda *a: None)
    monkeypatch.setattr(gateway.NativeTransport, "health", lambda *a: pytest.fail("must reject before host HTTP"))
    with pytest.raises(gate.AdmissionError):
        native.prepare(c.harness, c.p)
    assert not c.state.rows


def test_prepare_keeps_two_apps_fixed_resources_old_original_and_real_port_preconditions(case):
    c = case
    before = c.p["manifest_path"].read_bytes()
    template = native.prepare(c.harness, c.p)
    assert set(c.p["manifest"]["services"]) == {"ai-filter-api", "ai-filter-worker"}
    name = gateway.gateway_name(template["operation_id"])
    for service in c.p["manifest"]["services"].values():
        assert service["environment"]["AI_FILTER_TRUSTED_PROXY_HOST"] == name
        assert service["environment"]["AI_FILTER_WEB_BASE_URL"] == "http://" + name + ":5000"
        assert service["environment"]["UNCHANGED"] == "synthetic"
    root = c.p["gateway_native_root"]
    assert (root / "manifest.before.json").read_bytes() == before
    assert Path(template["old_config"]["path"]).read_bytes() == native.OLD_CONFIG
    assert c.state.kills == 1 and c.harness.created == []
    assert c.state.rows[c.identifier]["State"]["Status"] == "exited"
    create = next(args for args, _kwargs in c.state.calls if args[0] == "create")
    assert create[create.index("--memory") + 1] == create[create.index("--memory-swap") + 1] == str(128 * 1024**2)
    assert create[create.index("--pids-limit") + 1] == "64"
    assert create[create.index("--publish") + 1] == "127.0.0.1:35678:5000/tcp"
    assert create[create.index("--network") + 1] == template["network_id"]
    assert not any(args[0] in {"pull", "rm", "stop", "restart"} for args, _ in c.state.calls)
    permit = {"not_before": 100, "expires_at": 900, "source": {"revision": "a" * 40}}
    plan = native.bind_plan(c.p, permit)
    assert set(plan) == gateway.PLAN_KEYS and plan["window"] == {"not_before": 100, "expires_at": 900}
    with pytest.raises(gate.AdmissionError, match="one source-bound"):
        native.bind_plan(c.p, permit)
    native.preserve(c.harness, c.p)
    assert c.state.kills == 1
    assert (root / "FINAL_old.json").exists()


@pytest.mark.parametrize("change", ["network_external", "network_owner", "image_arch", "third_app", "sealed"])
def test_rejects_non_owned_or_already_sealed_fixture_without_creation(case, change):
    c = case
    if change == "network_external":
        c.state.network["Internal"] = False
    elif change == "network_owner":
        c.state.network["Labels"][native.LABEL] = "other-owner"
    elif change == "image_arch":
        c.state.image["Architecture"] = "arm64"
    elif change == "third_app":
        c.p["manifest"]["services"]["third"] = {}
    else:
        (c.p["directory"] / "sealed").mkdir(mode=0o700)
    with pytest.raises((gate.AdmissionError, ValueError)):
        native.prepare(c.harness, c.p)
    assert not any(args[0] == "create" for args, _ in c.state.calls)


@pytest.mark.parametrize("stage", ["create", "term"])
def test_unknown_fixture_mutation_retained_without_repeat_or_forced_cleanup(case, stage):
    c = case
    c.state.fail = stage
    with pytest.raises(TimeoutError, match="ACK lost"):
        native.prepare(c.harness, c.p)
    count = len([args for args, _ in c.state.calls if args[0] == stage or (stage == "term" and args[0] == "kill")])
    if stage == "create":
        with pytest.raises(gate.AdmissionError, match="normal owned"):
            native.preserve(c.harness, c.p)
    else:
        native.preserve(c.harness, c.p)
    assert (
        len([args for args, _ in c.state.calls if args[0] == stage or (stage == "term" and args[0] == "kill")]) == count
    )
    assert not any(args[0] in {"rm", "stop", "restart"} for args, _ in c.state.calls)
    assert (c.p["gateway_native_root"] / "PREPARE_INTENT.json").exists()
    assert "gateway_plan" not in c.p


def test_bind_rejects_changed_original_and_does_not_reprepare(case):
    c = case
    template = native.prepare(c.harness, c.p)
    Path(template["old_config"]["path"]).write_bytes(b"changed")
    calls = deepcopy(c.state.calls)
    with pytest.raises(gate.AdmissionError, match="original changed"):
        native.bind_plan(c.p, {"not_before": 1, "expires_at": 2, "source": {"revision": "a" * 40}})
    assert c.state.calls == calls


def test_preparation_requires_original_old_http_evidence(case, monkeypatch):
    c = case
    monkeypatch.setattr(
        gateway.NativeTransport,
        "health",
        lambda *_args: {
            "status": 200,
            "body_sha256": hashlib.sha256(b"other").hexdigest(),
            "operation": None,
            "upstream": None,
        },
    )
    with pytest.raises(gate.AdmissionError, match="old synthetic HTTP"):
        native.prepare(c.harness, c.p)
    assert "gateway_preparation" not in c.p
    native.preserve(c.harness, c.p)
    assert c.state.kills == 1
    assert not c.state.rows[c.identifier]["State"]["Running"]


def test_declared_binding_without_actual_mapping_cannot_pass_on_http_body_alone(case):
    case.state.mapping_fault = "empty"
    with pytest.raises(gate.AdmissionError, match="actual.*port"):
        native.prepare(case.harness, case.p)
    assert "gateway_preparation" not in case.p


@pytest.mark.parametrize("fault", ["empty", "missing", "null", "wrong_port", "wrong_host", "extra_bound", "requested"])
def test_actual_mapping_failure_precedes_http_and_preserves_originals(case, monkeypatch, fault):
    c = case
    c.state.mapping_fault = fault
    health = []
    monkeypatch.setattr(gateway.NativeTransport, "health", lambda *args: health.append(args))
    with pytest.raises(gate.AdmissionError, match="actual.*port"):
        native.prepare(c.harness, c.p)
    root = c.p["gateway_native_root"]
    profile = gate._read(root / "OLD_INGRESS_PROFILE.json")
    assert profile["identity_matches"] is True
    assert profile["actual_matches"] is (fault == "requested")
    assert profile["actual_field_present"] is (fault != "missing")
    assert health == [] and c.state.kills == 0
    assert not (root / "OLD_HTTP_OBSERVATIONS.json").exists()
    assert (root / "old-nginx.conf").read_bytes() == native.OLD_CONFIG
    native.preserve(c.harness, c.p)
    assert c.state.kills == 1 and (root / "STOPPED_old.json").exists()


def test_unpublished_image_exposure_is_not_an_extra_host_binding(case):
    case.state.mapping_fault = "extra_unpublished"
    native.prepare(case.harness, case.p)
    root = case.p["gateway_native_root"]
    assert gate._read(root / "OLD_INGRESS_PROFILE.json")["actual_matches"] is True
    assert gate._read(root / "OLD_INGRESS_CONFIRMED.json")["actual_matches"] is True


def test_actual_mapping_drift_after_http_still_stops(case, monkeypatch):
    def health(*_args):
        case.state.rows[case.identifier]["NetworkSettings"]["Ports"] = {}
        return {"status": 200, "body_sha256": hashlib.sha256(b"OLD").hexdigest(), "operation": None, "upstream": None}

    monkeypatch.setattr(gateway.NativeTransport, "health", health)
    with pytest.raises(gate.AdmissionError, match="actual.*port"):
        native.prepare(case.harness, case.p)
    root = case.p["gateway_native_root"]
    assert gate._read(root / "OLD_INGRESS_CONFIRMED.json")["actual_matches"] is False
    assert case.state.kills == 0 and "gateway_preparation" not in case.p


@pytest.mark.parametrize("fault", ["image", "owner", "restarted", "Running", "OOMKilled", "Restarting", "Paused", "Dead"])
def test_running_identity_failure_precedes_any_http(case, monkeypatch, fault):
    case.state.runtime_fault = fault
    seen = []
    monkeypatch.setattr(gateway.NativeTransport, "health", lambda *args: seen.append(args))
    with pytest.raises(gate.AdmissionError, match="execution changed"):
        native.prepare(case.harness, case.p)
    profile = gate._read(case.p["gateway_native_root"] / "OLD_INGRESS_PROFILE.json")
    assert profile["identity_matches"] is False and seen == []
    assert case.state.kills == 0 and "gateway_preparation" not in case.p


@pytest.mark.parametrize("change", ["StartedAt", "endpoint"])
def test_identity_and_network_state_drift_during_http_are_not_confirmed(case, monkeypatch, change):
    def health(*_args):
        row = case.state.rows[case.identifier]
        if change == "StartedAt":
            row["State"]["StartedAt"] = "a-different-start"
        else:
            row["NetworkSettings"]["Networks"][case.state.network["Name"]]["EndpointID"] = "f" * 64
        return {"status": 200, "body_sha256": hashlib.sha256(b"OLD").hexdigest(), "operation": None, "upstream": None}

    monkeypatch.setattr(gateway.NativeTransport, "health", health)
    with pytest.raises(gate.AdmissionError, match="changed during HTTP"):
        native.prepare(case.harness, case.p)
    assert case.state.kills == 0 and "gateway_preparation" not in case.p


def test_http_failures_are_bounded_classified_and_do_not_replace_timeout(case, monkeypatch):
    secret = "http://synthetic-user:DO_NOT_EXPORT@private.invalid/private/config"
    original = TimeoutError("original fixed phase timeout")
    seen = []

    def health(transport, host, port, operation):
        seen.append((host, port, transport.deadline))
        raise ConnectionRefusedError(errno.ECONNREFUSED, secret)

    def wait(function, *, seconds):
        assert seconds == 10
        assert function() is None
        assert function() is None
        raise original

    case.harness.phase_deadline = time.monotonic() + 2
    case.harness.wait = wait
    monkeypatch.setattr(gateway.NativeTransport, "health", health)
    with pytest.raises(TimeoutError) as caught:
        native.prepare(case.harness, case.p)
    assert caught.value is original
    raw = (case.p["gateway_native_root"] / "OLD_HTTP_OBSERVATIONS.json").read_bytes()
    record = json.loads(raw)
    assert len(record["attempts"]) == 2 and len(raw) <= 32768
    assert all(row["class"] == "connection_refused" and row["errno"] == errno.ECONNREFUSED for row in record["attempts"])
    assert all(host == "127.0.0.1" and port == 35678 and deadline <= case.harness.phase_deadline for host, port, deadline in seen)
    assert secret.encode() not in raw and b"DO_NOT_EXPORT" not in raw and b"private.invalid" not in raw
    assert "gateway_preparation" not in case.p and case.state.kills == 0


@pytest.mark.parametrize("evidence_fails", [False, True])
def test_http_unknown_error_and_evidence_sync_error_keep_original_failure(case, monkeypatch, evidence_fails):
    original = RuntimeError("unknown error with private body DO_NOT_EXPORT")
    actual_write = gate._write

    def fail_health(*_args):
        raise original

    def fail_observation(path, value):
        if evidence_fails and path.name == "OLD_HTTP_OBSERVATIONS.json":
            raise OSError("synthetic directory sync failure")
        return actual_write(path, value)

    monkeypatch.setattr(gateway.NativeTransport, "health", fail_health)
    monkeypatch.setattr(gate, "_write", fail_observation)
    with pytest.raises(RuntimeError) as caught:
        native.prepare(case.harness, case.p)
    assert caught.value is original
    if evidence_fails:
        assert isinstance(caught.value.__cause__, OSError)
    else:
        raw = (case.p["gateway_native_root"] / "OLD_HTTP_OBSERVATIONS.json").read_bytes()
        assert b"DO_NOT_EXPORT" not in raw and b"private body" not in raw
        assert json.loads(raw)["attempts"][0]["class"] == "unclassified"
    assert "gateway_preparation" not in case.p


def test_successful_http_without_durable_observation_is_not_confirmed(case, monkeypatch):
    actual_write = gate._write
    original = OSError("synthetic observation sync failure")

    def fail_observation(path, value):
        if path.name == "OLD_HTTP_OBSERVATIONS.json":
            raise original
        return actual_write(path, value)

    monkeypatch.setattr(gate, "_write", fail_observation)
    with pytest.raises(OSError) as caught:
        native.prepare(case.harness, case.p)
    assert caught.value is original and "gateway_preparation" not in case.p
    assert case.state.kills == 0


def test_http_attempt_and_expired_phase_limits_are_hard(case, monkeypatch):
    calls = []

    def fail_health(*_args):
        calls.append(True)
        raise TimeoutError("bounded synthetic timeout")

    def wait(function, *, seconds):
        assert seconds == 10
        for _ in range(65):
            function()

    case.harness.wait = wait
    monkeypatch.setattr(gateway.NativeTransport, "health", fail_health)
    with pytest.raises(gate.AdmissionError, match="diagnostic/phase bound"):
        native.prepare(case.harness, case.p)
    record = gate._read(case.p["gateway_native_root"] / "OLD_HTTP_OBSERVATIONS.json")
    assert len(calls) == len(record["attempts"]) == 64
    assert all(row["class"] == "timeout" for row in record["attempts"])
    expired_root = case.p["directory"] / "expired-observation"
    expired_root.mkdir(mode=0o700)
    case.harness.phase_deadline = time.monotonic() - 1
    with pytest.raises(gate.AdmissionError, match="diagnostic/phase bound"):
        native._old_http(case.harness, expired_root, 35678, "a" * 64)
    assert len(calls) == 64
    assert gate._read(expired_root / "OLD_HTTP_OBSERVATIONS.json")["attempts"] == []


def test_mapping_inspection_consumes_same_phase_and_failure_restores_outer_deadline(case, monkeypatch):
    clock, health = [100.0], []
    original_command = case.harness.command
    case.harness.phase_deadline = 1000.0

    def command(args, **kwargs):
        result = original_command(args, **kwargs)
        if args[0] == "inspect":
            clock[0] += 11
        return result

    case.harness.command = command
    monkeypatch.setattr(native.time, "monotonic", lambda: clock[0])
    monkeypatch.setattr(gateway.NativeTransport, "health", lambda *args: health.append(args))
    with pytest.raises(gate.AdmissionError, match="diagnostic/phase bound"):
        native.prepare(case.harness, case.p)
    assert health == []
    assert case.harness.phase_deadline == 1000.0
    assert gate._read(case.p["gateway_native_root"] / "OLD_HTTP_OBSERVATIONS.json")["attempts"] == []


def diagnostic_packets(capsys):
    return [json.loads(line.split("=", 1)[1]) for line in capsys.readouterr().out.splitlines()
            if line.startswith("gateway_ingress_observation=")]


def test_selected_originals_are_exclusive_private_and_stdout_has_exact_port_shape(case, capsys):
    native.prepare(case.harness, case.p)
    root = case.p["gateway_native_root"]
    packets = diagnostic_packets(capsys)
    assert [row["phase"] for row in packets] == ["before_http", "after_http"]
    for phase, packet in zip(("BEFORE", "AFTER"), packets):
        path = root / f"OLD_INGRESS_{phase}.original.json"
        assert path.stat().st_mode & 0o777 == 0o600
        assert len(path.read_bytes()) <= native.DIAGNOSTIC_BYTES
        assert packet["observation_original"]["sha256"] == hashlib.sha256(path.read_bytes()).hexdigest()
        assert packet["diagnostic_durability_confirmed"] is True
        assert packet["diagnostic_only_not_execution_proof"] is True
        actual = packet["observation"]["actual"]
        assert actual["entries"] == {"5000/tcp": [{"host": "127.0.0.1", "port": 35678,
                "host_proof": native._value_proof("127.0.0.1")}]}
        assert actual["kind"] == "mapping" and actual["shape_valid"] is True


@pytest.mark.parametrize("fault,kind,shape", [("missing", "missing", False), ("null", "null", False),
        ("empty", "mapping", True), ("wrong_port", "mapping", True), ("wrong_host", "mapping", True),
        ("extra_bound", "mapping", True), ("requested", "mapping", True)])
def test_failed_port_guard_diagnostic_is_available_before_any_http(case, monkeypatch, capsys, fault, kind, shape):
    case.state.mapping_fault = fault
    health = []
    monkeypatch.setattr(gateway.NativeTransport, "health", lambda *a: health.append(a))
    with pytest.raises(gate.AdmissionError, match="actual.*port"):
        native.prepare(case.harness, case.p)
    assert health == [] and "gateway_preparation" not in case.p
    packet, = diagnostic_packets(capsys)
    assert packet["phase"] == "before_http" and packet["actual_matches"] is (fault == "requested")
    assert packet["observation"]["actual"]["kind"] == kind
    assert packet["observation"]["actual"]["shape_valid"] is shape
    assert packet["diagnostic_durability_confirmed"] is True
    assert not (case.p["gateway_native_root"] / "OLD_INGRESS_AFTER.original.json").exists()


@pytest.mark.parametrize("change", ["StartedAt", "Pid", "RestartCount"])
def test_before_after_startup_diagnostics_retain_drift_without_confirming_http(case, monkeypatch, capsys, change):
    def health(*_args):
        row = case.state.rows[case.identifier]
        if change == "StartedAt":
            row["State"][change] = "2026-10-28T03:01:02.123456789Z"
        elif change == "Pid":
            row["State"][change] = 20001
        else:
            row[change] = 1
        return {"status": 200, "body_sha256": hashlib.sha256(b"OLD").hexdigest(), "operation": None, "upstream": None}
    monkeypatch.setattr(gateway.NativeTransport, "health", health)
    with pytest.raises(gate.AdmissionError):
        native.prepare(case.harness, case.p)
    before, after = diagnostic_packets(capsys)
    assert before["phase"] == "before_http" and after["phase"] == "after_http"
    assert before["observation"]["startup"] != after["observation"]["startup"]
    assert case.state.kills == 0 and "gateway_preparation" not in case.p
    assert not (case.p["gateway_native_root"] / "OLD_HTTP.json").exists()


@pytest.mark.parametrize("where", ["host", "port", "entry", "key", "state", "networks", "env"])
def test_unknown_and_malicious_fields_are_not_repeated_in_new_originals_or_stdout(case, monkeypatch, capsys, where):
    secret = "DO_NOT_EXPORT_https://private.invalid/token?secret=123"
    command = case.harness.command
    def altered(args, **kwargs):
        value = command(args, **kwargs)
        if args[0] == "start":
            row = case.state.rows[case.identifier]
            actual = row["NetworkSettings"]["Ports"]
            if where == "host":
                actual["5000/tcp"][0]["HostIp"] = secret
            elif where == "port":
                actual["5000/tcp"][0]["HostPort"] = secret
            elif where == "entry":
                actual["5000/tcp"][0]["UNKNOWN"] = secret
            elif where == "key":
                actual[secret] = [{"HostIp": "0.0.0.0", "HostPort": "1"}]
            elif where == "state":
                row["State"]["StartedAt"] = secret
            elif where == "networks":
                row["NetworkSettings"]["Networks"][secret] = {"Secret": secret}
            else:
                row["Config"]["Env"] = [secret]
        return value
    case.harness.command = altered
    if where in {"state", "networks", "env"}:
        native.prepare(case.harness, case.p)
    else:
        with pytest.raises(gate.AdmissionError, match="actual.*port"):
            native.prepare(case.harness, case.p)
    public = capsys.readouterr().out
    assert secret not in public and "private.invalid" not in public
    root = case.p["gateway_native_root"]
    for name in ("OLD_INGRESS_BEFORE.original.json", "OLD_INGRESS_PROFILE.json"):
        assert secret.encode() not in (root / name).read_bytes()
        assert b"Env" not in (root / name).read_bytes()


@pytest.mark.parametrize("stage", ["before_original", "before_profile", "after_original", "after_profile"])
def test_diagnostic_failure_keeps_partial_evidence_and_never_confirms(case, monkeypatch, capsys, stage):
    actual = native._diagnostic_original
    names = {"before_original": "OLD_INGRESS_BEFORE.original.json", "before_profile": "OLD_INGRESS_PROFILE.json",
             "after_original": "OLD_INGRESS_AFTER.original.json", "after_profile": "OLD_INGRESS_CONFIRMED.json"}
    original = OSError("DO_NOT_EXPORT sync failure")
    def fail(path, value):
        if path.name == names[stage]:
            raise original
        return actual(path, value)
    monkeypatch.setattr(native, "_diagnostic_original", fail)
    with pytest.raises(OSError) as caught:
        native.prepare(case.harness, case.p)
    assert caught.value is original
    packets = diagnostic_packets(capsys)
    assert packets[-1]["diagnostic_durability_confirmed"] is False
    assert "DO_NOT_EXPORT" not in json.dumps(packets)
    assert case.state.kills == 0 and "gateway_preparation" not in case.p
    assert not (case.p["gateway_native_root"] / "OLD_HTTP.json").exists()


@pytest.mark.parametrize("fault", ["exists", "symlink", "fsync", "directory_sync", "stdout"])
def test_exclusive_and_sync_boundaries_never_allow_http_after_diagnostic_failure(case, monkeypatch, capsys, fault):
    original_record = native._diagnostic_original
    health, written = [], []
    def record(path, value):
        if path.name == "OLD_INGRESS_BEFORE.original.json":
            if fault == "exists":
                path.write_bytes(b"retained original")
            elif fault == "symlink":
                target = path.parent / "kept-original"
                target.write_bytes(b"retained original")
                path.symlink_to(target)
            elif fault == "fsync":
                monkeypatch.setattr(native.os, "fsync", lambda *_: (_ for _ in ()).throw(OSError("sync failed")))
            elif fault == "directory_sync":
                monkeypatch.setattr(gate, "_sync_directory", lambda *_: (_ for _ in ()).throw(OSError("directory sync failed")))
        result = original_record(path, value)
        written.append(path)
        return result
    monkeypatch.setattr(native, "_diagnostic_original", record)
    monkeypatch.setattr(gateway.NativeTransport, "health", lambda *a: health.append(a))
    if fault == "stdout":
        monkeypatch.setattr(native, "_emit_ingress_diagnostic", lambda *_: (_ for _ in ()).throw(BrokenPipeError("closed output")))
    with pytest.raises((OSError, gate.AdmissionError)):
        native.prepare(case.harness, case.p)
    assert health == [] and "gateway_preparation" not in case.p
    path = case.p["gateway_native_root"] / "OLD_INGRESS_BEFORE.original.json"
    if fault in {"exists", "symlink"}:
        assert path.read_bytes() == b"retained original"
    if fault == "symlink":
        assert path.is_symlink()


def test_mapping_diagnostic_has_fixed_shape_caps_and_never_prints_unknown_scalars():
    text = "DO_NOT_EXPORT"
    for value in (text, True, 1, [], {text: None}, {f"{i}/tcp": None for i in range(1, 10)},
                  {"5000/tcp": [{"HostIp": "127.0.0.1", "HostPort": "1"}] * 9}):
        result = native._mapping_diagnostic(value)
        assert result["shape_valid"] is False and text not in json.dumps(result)
    too_large = {text: "s" * (native.DIAGNOSTIC_BYTES + 1)}
    result = native._mapping_diagnostic(too_large)
    assert result["proof"] == {"complete": False, "reason": "byte_bound", "sha256": None}
    assert "entries" not in result
    assert native._value_proof(float("nan"))["reason"] == "encoding_invalid"


def test_real_format_startup_identity_is_present_on_both_sides_of_http(case, capsys):
    actual = case.harness.command
    started = "2026-10-28T03:01:02.123456789Z"
    def command(args, **kwargs):
        result = actual(args, **kwargs)
        if args[0] == "start":
            case.state.rows[case.identifier]["State"].update(StartedAt=started, Pid=20000)
        return result
    case.harness.command = command
    native.prepare(case.harness, case.p)
    before, after = diagnostic_packets(capsys)
    assert before["observation"]["startup"] == after["observation"]["startup"]
    assert before["observation"]["startup"]["started_at"] == started
    assert before["observation"]["startup"]["pid"] == 20000
    assert before["observation"]["startup"]["restart_count"] == 0


def test_proof_serialization_preflight_limits_depth_count_and_scalar_size():
    deep = value = []
    for _ in range(18):
        item = []
        value.append(item)
        value = item
    for item in (deep, [None] * 513, 2**1024):
        assert native._value_proof(item) == {"complete": False, "reason": "shape_bound", "sha256": None}
    assert native._value_proof(object())["reason"] == "encoding_invalid"


def test_failed_diagnostic_stdout_cannot_hide_original_durability_error(case, monkeypatch):
    original = OSError("original sync failure DO_NOT_EXPORT")
    def fail(*_args, **_kwargs):
        raise original
    monkeypatch.setattr(native, "_diagnostic_original", fail)
    monkeypatch.setattr(native, "_emit_ingress_diagnostic", lambda *_: (_ for _ in ()).throw(BrokenPipeError("closed output")))
    with pytest.raises(OSError) as caught:
        native.prepare(case.harness, case.p)
    assert caught.value is original
    assert case.state.kills == 0 and "gateway_preparation" not in case.p


@pytest.mark.parametrize("change", ["exit", "oom", "created", "dead", "paused", "restarting", "unfinished", "invalid_time"])
def test_preserve_already_stopped_requires_real_normal_exit(case, change):
    native.prepare(case.harness, case.p)
    row = case.state.rows[case.identifier]
    if change == "exit":
        row["State"]["ExitCode"] = 137
    elif change == "created":
        row["State"]["Status"] = "created"
    elif change == "unfinished":
        row["State"]["FinishedAt"] = "0001-01-01T00:00:00Z"
    elif change == "invalid_time":
        row["State"]["FinishedAt"] = "not-a-time"
    else:
        row["State"][{"oom": "OOMKilled", "dead": "Dead", "paused": "Paused", "restarting": "Restarting"}[change]] = True
    kills = case.state.kills
    with pytest.raises(gate.AdmissionError, match="normal owned"):
        native.preserve(case.harness, case.p)
    assert not (case.p["gateway_native_root"] / "STOPPED_old.json").exists()
    assert case.state.kills == kills
    assert (case.p["gateway_native_root"] / "FINAL_old.json").exists()


@pytest.mark.parametrize("field", ["Image", "Config", "HostConfig", "Mounts", "RestartCount", "Name", "StartedAt"])
def test_preserve_identity_drift_after_original_is_retained_blocks_success(case, field):
    native.prepare(case.harness, case.p)
    original = case.harness.command
    def command(args, **kwargs):
        if args[0] == "logs":
            row = case.state.rows[case.identifier]
            if field == "StartedAt":
                row["State"][field] = "changed"
            elif field == "RestartCount":
                row[field] += 1
            elif field == "Mounts":
                row[field] = [{"Destination": "/changed"}]
            elif field in {"Config", "HostConfig"}:
                row[field] = {**row[field], "changed": True}
            else:
                row[field] = "changed"
        return original(args, **kwargs)
    case.harness.command = command
    kills = case.state.kills
    with pytest.raises(gate.AdmissionError, match="identity changed"):
        native.preserve(case.harness, case.p)
    assert case.state.kills == kills
    assert not (case.p["gateway_native_root"] / "STOPPED_old.json").exists()


@pytest.mark.parametrize("exit_code", [0, 143])
def test_preserve_config_test_requires_zero_even_if_other_gateway_term_is_normal(case, monkeypatch, exit_code):
    native.prepare(case.harness, case.p)
    op = gate._read(case.p["gateway_native_root"] / "PREPARE_INTENT.json")["operation_id"]
    case.p["gateway_plan"] = {"operation_id": op}
    row = deepcopy(case.state.rows[case.identifier])
    row.update(Id="f" * 64, Name="/" + gateway.gateway_name(op) + "-config-test")
    row["Config"]["Labels"]["io.ai-filter.gateway.plan"] = gate._hash(case.p["gateway_plan"])
    row["State"]["ExitCode"] = exit_code
    case.state.rows[row["Id"]] = row
    monkeypatch.setattr(gateway.NativeTransport, "find", lambda _self, _operation, purpose: deepcopy(row) if purpose == "config-test" else None)
    if exit_code:
        with pytest.raises(gate.AdmissionError, match="normal owned"):
            native.preserve(case.harness, case.p)
        assert not (case.p["gateway_native_root"] / "STOPPED_config-test.json").exists()
    else:
        native.preserve(case.harness, case.p)
        assert (case.p["gateway_native_root"] / "STOPPED_config-test.json").exists()
    assert case.state.kills == 1
