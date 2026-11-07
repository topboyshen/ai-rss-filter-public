"""Local control-logic tests only, never an actual private-daemon execution."""
from copy import deepcopy
from contextlib import nullcontext
import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from scripts import production_sender_bootstrap_host_native as native
from scripts import sender_host_loopback_pg as pg
from services import admission_fence as gate


@pytest.mark.parametrize("state", ["normal", "running", "uncertain", "primary_failure"])
@pytest.mark.parametrize("locator_failure", [False, True])
def test_bootstrap_completion_and_daemon_drain_have_separate_diagnostics(tmp_path, monkeypatch, state, locator_failure):
    from scripts import production_offline_registry as registry
    isolated = native.isolated
    calls, records = [], {}
    plan = {"started": 100, "active_deadline": 260, "deadline": 280, "repo": str(tmp_path),
            "source": str(tmp_path), "binary": str(tmp_path / "binary"),
            "build_evidence": str(tmp_path), "sender": {"Id": "sha256:" + "a" * 64}, "bundle": {}}
    commands = SimpleNamespace(deadline=260, uncertain_mutation=state == "uncertain",
        docker=lambda args: "a" * 64 if state == "running" else "")
    context = SimpleNamespace(root=tmp_path, plan=plan, commands=commands, daemon={"pid": 44},
                              check=lambda **kw: calls.append("check"))
    process = SimpleNamespace(pid=44, send_signal=lambda sig: calls.append(sig),
                              wait=lambda timeout: calls.append("daemon_wait") or 0)
    monkeypatch.setattr(native.os, "setsid", lambda: None)
    monkeypatch.setattr(native.time, "monotonic", lambda: 100)
    monkeypatch.setattr(native.host, "proc_identity", lambda pid: {"netns": 3})
    monkeypatch.setattr(native.host, "restrict_private_ipv6", lambda *a: None)
    monkeypatch.setattr(isolated, "prepare_mounts", lambda *a: None)
    monkeypatch.setattr(isolated, "FullCommands", lambda *a: commands)
    monkeypatch.setattr(isolated, "PrivateHostContext", lambda *a: context)
    monkeypatch.setattr(isolated, "record", lambda path, value, **kw: records.update({path.name: deepcopy(value)}))
    logs = SimpleNamespace(finish=lambda end: calls.append("logs_finish"))
    monkeypatch.setattr(isolated, "start_daemon", lambda *a: (process, context.daemon, logs))
    monkeypatch.setattr(isolated, "load_dependencies", lambda *a: None)
    monkeypatch.setattr(registry, "serve_bundle", lambda *a, **kw: nullcontext(SimpleNamespace(url="synthetic")))
    monkeypatch.setattr(native.deadline, "scope", lambda *a: nullcontext())
    monkeypatch.setattr(native.bootstrap.local_pg, "native_private_host", lambda *a: nullcontext())
    monkeypatch.setattr(native.host, "check_peer", lambda *a: calls.append("peer_check"))
    monkeypatch.setattr(native.host, "RETAINERS", {44: (SimpleNamespace(wait=lambda timeout: 0),)})
    bootstrap_result = {"status": "passed", "failure": None, "errors": []}
    primary = ConnectionRefusedError("SECRET_CONNECTION_CANARY")
    if locator_failure:
        def unavailable(*args, **kwargs):
            raise OSError("SECRET_DIAGNOSTIC_CANARY")
        monkeypatch.setattr(isolated.startup, "public_exception_locations", unavailable)
    def execute(*args, **kwargs):
        if state == "primary_failure":
            raise primary
        return bootstrap_result
    monkeypatch.setattr(native.bootstrap, "execute", execute)
    value = native.inner_execute(tmp_path, plan | {"parent": {}})
    assert value["bootstrap_result"] == (None if state == "primary_failure" else bootstrap_result)
    assert value["drain_observation"] == records["DRAIN_OBSERVATION.json"] == {
        "schema": 1, "running_count": int(state == "running"),
        "uncertain_mutation": state in {"uncertain", "primary_failure"}}
    if state == "normal":
        assert value["status"] == "passed" and value["daemon_stop"]["daemon_returncode"] == 0
        assert calls[-4:] == ["peer_check", isolated.signal.SIGTERM, "daemon_wait", "logs_finish"]
        assert value["error_locations"] == []
    else:
        assert value["status"] == "failed_or_evidence_insufficient" and value["daemon_stop"] is None
        assert "peer_check" not in calls and isolated.signal.SIGTERM not in calls
        assert value["error_type"] == ("ConnectionRefusedError" if state == "primary_failure" else "AdmissionError")
        assert bool(value["error_locations"]) is not locator_failure
        assert records["INNER_RESULT.json"] == value
        assert "SECRET_DIAGNOSTIC_CANARY" not in json.dumps(value)
        assert "SECRET_CONNECTION_CANARY" not in json.dumps(value)
        if state == "primary_failure":
            assert primary.__notes__ == ["private daemon retained: AdmissionError"]


@pytest.mark.parametrize("locator_error", [OSError("secret-source"), SyntaxError("secret-source")])
def test_cli_diagnostic_failure_keeps_original_structured_refusal(monkeypatch, capsys, locator_error):
    def refused(*args):
        raise ConnectionRefusedError("SECRET_CONNECTION_CANARY")
    def unavailable(*args):
        raise locator_error
    monkeypatch.setattr(native, "arguments", refused)
    monkeypatch.setattr(native.isolated.startup, "public_exception_locations", unavailable)
    assert native.main([]) == 3
    packet = json.loads(capsys.readouterr().out)
    failure = packet["private_host"]["failure"]
    assert packet["status"] == "unconfirmed" and failure["error_type"] == "ConnectionRefusedError"
    assert failure["error_locations"] == []
    assert failure["error_sha256"] == native.hashlib.sha256(b"SECRET_CONNECTION_CANARY").hexdigest()
    assert "SECRET_CONNECTION_CANARY" not in json.dumps(packet) and "secret-source" not in json.dumps(packet)


@pytest.fixture
def context(monkeypatch, tmp_path):
    value = object.__new__(native.isolated.PrivateHostContext)
    value.root = tmp_path / "inner"
    value.path = value.root / "HOST_CONTEXT.json"
    value.proof_sha256 = "a" * 64
    value.holder = {"pid": 33, "netns": 3, "mntns": 4}
    value.daemon = {"pid": 44, "netns": 3, "mntns": 4}
    value.network = None
    proof = {"sha256": "b" * 64}
    value.plan = {"bootstrap_mode": native.MODE, "gateway_custody": False,
        "started": 10, "deadline": 190, "active_deadline": 170,
        "bootstrap_wrapper": proof, "daemon_binary": proof,
        "bootstrap_source_sha256": native.hashlib.sha256(Path(native.bootstrap.__file__).read_bytes()).hexdigest(),
        "postgres_transport_source_sha256": pg.source_sha256(),
        "parent": {"netns": 1, "mntns": 2}}
    frozen = deepcopy(value.plan)
    calls = []
    value.commands = SimpleNamespace(deadline=170, sequence=0,
        inspect=lambda identifier: calls.append(("inspect", identifier)) or {"network": "actual"})
    monkeypatch.setattr(native.isolated.PrivateHostContext, "check",
        lambda self, **kwargs: calls.append(("context_check", kwargs)))
    monkeypatch.setattr(native.isolated, "original",
        lambda path, *args: (deepcopy(frozen) if path.name == "PLAN.json" else deepcopy(value.network), {}))
    monkeypatch.setattr(native.host, "file_proof", lambda path: proof)
    monkeypatch.setattr(native.host, "proc_identity", lambda pid: deepcopy(value.holder))
    monkeypatch.setattr(native.host, "check_peer", lambda *args: calls.append(("peer", args)))
    monkeypatch.setattr(native.host, "namespace_snapshot", lambda *args, **kwargs: {"netns": 3})
    monkeypatch.setattr(pg, "_process", lambda pid: {"uid": 0})
    monkeypatch.setattr(pg, "_small", lambda *args: b"00000000-0000-0000-0000-000000000001\n")
    monkeypatch.setattr(native.os, "geteuid", lambda: 0)
    monkeypatch.setattr(native.os, "readlink", lambda path: "/usr/bin/dockerd")
    actual_stat = native.os.stat
    monkeypatch.setattr(native.os, "stat", lambda path, *a, **kw:
        SimpleNamespace(st_dev=7, st_ino=3) if str(path) == "/proc/self/ns/net" else actual_stat(path, *a, **kw))
    return value, frozen, calls


def test_initial_live_context_uses_actual_daemon_namespace_metadata(context):
    value, _, calls = context
    assert native.native_host_identity(value) == {
        "boot_id": "00000000-0000-0000-0000-000000000001", "network_device": 7, "network_inode": 3}
    assert [kind for kind, _ in calls] == ["context_check", "peer", "context_check"]
    assert value.commands.deadline == 170


@pytest.mark.parametrize("value", [None, {}, {"status": "passed"}, SimpleNamespace(check=lambda: None)])
def test_caller_receipt_or_callback_is_not_live_authority(value):
    with pytest.raises(gate.AdmissionError, match="exact live private host context"):
        native.native_host_identity(value)


@pytest.mark.parametrize("field, replacement", [
    ("bootstrap_mode", "unknown"), ("gateway_custody", True), ("started", 11),
    ("deadline", 191), ("active_deadline", 171), ("bootstrap_wrapper", {}),
    ("bootstrap_source_sha256", "0" * 64), ("daemon_binary", {}),
    ("postgres_transport_source_sha256", "0" * 64),
])
def test_mode_budget_or_source_change_is_refused(context, field, replacement):
    value, _, _ = context
    value.plan[field] = replacement
    with pytest.raises(gate.AdmissionError):
        native.native_host_identity(value)


@pytest.mark.parametrize("which", ["socket", "process", "namespace", "route"])
def test_live_peer_lifecycle_namespace_and_route_failures_are_not_receipts(context, monkeypatch, which):
    value, _, _ = context
    def refuse(*args, **kwargs):
        raise gate.AdmissionError("actual binding changed")
    if which == "socket":
        monkeypatch.setattr(native.host, "check_peer", refuse)
    elif which == "process":
        monkeypatch.setattr(native.isolated.PrivateHostContext, "check", refuse)
    elif which == "namespace":
        monkeypatch.setattr(native.host, "proc_identity", lambda pid: {"netns": 99, "mntns": 4})
    else:
        monkeypatch.setattr(native.host, "namespace_snapshot", refuse)
    with pytest.raises(gate.AdmissionError):
        native.native_host_identity(value)
    assert value.commands.deadline == 170


def test_actual_frozen_bridge_is_freshly_inspected_and_route_guard_runs(context, monkeypatch):
    value, _, calls = context
    value.network = {"network_id": "c" * 64, "owner": "owned"}
    monkeypatch.setattr(native.isolated, "validate_network",
        lambda proof, row, owner, commands: calls.append(("validate_network", (proof, row, owner))))
    native.native_host_identity(value)
    assert ("inspect", "c" * 64) in calls
    assert any(kind == "validate_network" and args[1] == {"network": "actual"} for kind, args in calls)


def test_network_proof_change_is_not_tolerated(context, monkeypatch):
    value, frozen, _ = context
    value.network = {"network_id": "c" * 64, "owner": "owned"}
    monkeypatch.setattr(native.isolated, "original", lambda path, *args:
        (deepcopy(frozen) if path.name == "PLAN.json" else {"network_id": "different"}, {}))
    with pytest.raises(gate.AdmissionError, match="network capability differs"):
        native.native_host_identity(value)


def test_private_bridge_route_violation_is_still_a_hard_failure(context, monkeypatch):
    value, _, _ = context
    value.network = {"network_id": "c" * 64, "owner": "owned"}
    def refused(*args):
        raise gate.AdmissionError("external route")
    monkeypatch.setattr(native.isolated, "validate_network", refused)
    with pytest.raises(gate.AdmissionError, match="external route"):
        native.native_host_identity(value)


def test_context_scope_is_same_live_object_and_restores_after_failure(context, monkeypatch):
    value, _, _ = context
    checked = []
    monkeypatch.setattr(native, "native_host_identity", lambda actual: checked.append(actual) or {"live": True})
    assert pg._NATIVE_HOST.get() is None
    with pytest.raises(ValueError, match="synthetic failure"):
        with pg.native_private_host(value):
            assert pg._NATIVE_HOST.get() is value and pg._host() == {"live": True}
            raise ValueError("synthetic failure")
    assert checked == [value, value] and pg._NATIVE_HOST.get() is None


def test_context_scope_rechecks_before_exit_and_does_not_accept_nesting(context, monkeypatch):
    value, _, _ = context
    checked = []
    monkeypatch.setattr(native, "native_host_identity", lambda actual: checked.append(actual) or {})
    with pg.native_private_host(value):
        with pytest.raises(Exception, match="nested native host authority"):
            with pg.native_private_host(value):
                pytest.fail("nested scope accepted")
    assert checked == [value, value] and pg._NATIVE_HOST.get() is None


def test_exit_failure_does_not_leave_context_enabled(context, monkeypatch):
    value, _, _ = context
    checked = []
    def identity(actual):
        checked.append(actual)
        if len(checked) > 1:
            raise gate.AdmissionError("lifetime ended")
        return {}
    monkeypatch.setattr(native, "native_host_identity", identity)
    with pytest.raises(gate.AdmissionError, match="lifetime ended"):
        with pg.native_private_host(value):
            pass
    assert pg._NATIVE_HOST.get() is None


def test_final_observation_uses_only_original_reserved_deadline(context, monkeypatch):
    value, _, calls = context
    seen = []
    monkeypatch.setattr(native.host, "namespace_snapshot", lambda commands, **kwargs:
        seen.append(commands.deadline) or {"netns": 3})
    token = native.deadline.STATE.set((190, 190))
    try:
        native.native_host_identity(value)
    finally:
        native.deadline.STATE.reset(token)
    assert seen == [190] and value.commands.deadline == 170
    assert calls[0] == ("context_check", {"final": True})


def test_default_host_guard_still_requires_the_actual_initial_host(monkeypatch):
    assert pg._NATIVE_HOST.get() is None
    monkeypatch.setattr(pg.os, "geteuid", lambda: 1)
    monkeypatch.setattr(native, "native_host_identity", lambda context: pytest.fail("default used native alternative"))
    with pytest.raises(Exception, match="native root host proof"):
        pg._host()


@pytest.mark.parametrize("rc", [0, 1])
def test_passed_sealed_result_also_requires_actual_child_normal_exit(monkeypatch, tmp_path, rc):
    process = SimpleNamespace(poll=lambda: None, wait=lambda timeout: rc)
    monkeypatch.setattr(native.isolated, "remaining", lambda end: 1)
    monkeypatch.setattr(native.isolated, "peer_identity", lambda: {"peer": "actual"})
    monkeypatch.setattr(native.isolated, "sealed_child_result", lambda root: {"status": "passed"})
    if rc == 0:
        assert native.observe_child(process, tmp_path, {"deadline": 20, "outer_peer": {"peer": "actual"}}) == {"status": "passed"}
    else:
        with pytest.raises(gate.AdmissionError, match="normal child exit"):
            native.observe_child(process, tmp_path, {"deadline": 20, "outer_peer": {"peer": "actual"}})


def test_unconfirmed_child_is_retained_instead_of_forced_exit(monkeypatch, tmp_path):
    process = SimpleNamespace(poll=lambda: None, wait=lambda **kw: pytest.fail("wait/kill on failed holder"))
    monkeypatch.setattr(native.isolated, "remaining", lambda end: 1)
    monkeypatch.setattr(native.isolated, "peer_identity", lambda: {})
    monkeypatch.setattr(native.isolated, "sealed_child_result", lambda root: {"status": "failed_or_evidence_insufficient"})
    assert native.observe_child(process, tmp_path, {"deadline": 20, "outer_peer": {}})["status"] == "failed_or_evidence_insufficient"


def test_receipt_context_cannot_enter_execute_without_active_live_scope(monkeypatch, tmp_path):
    monkeypatch.setattr(native.bootstrap.os, "geteuid", lambda: 0)
    monkeypatch.setattr(native.bootstrap.sys, "platform", "linux")
    with pytest.raises(gate.AdmissionError, match="active live native scope"):
        native.bootstrap.execute(native.ROOT, tmp_path, "sha256:" + "a" * 64,
            tmp_path / "binary", tmp_path / "output", tmp_path, predecessor_schema=131,
            host_loopback=True, private_host_context={"status": "passed"})
    assert not (tmp_path / "output").exists()


def test_offline_bundle_also_contains_the_real_fixed_upstream131_binary():
    from scripts.verify_miniflux_pause_native import PG_IMAGE, PYTHON_IMAGE
    assert native.references() == [PYTHON_IMAGE, PG_IMAGE, native.bootstrap._base_image(native.ROOT)]
    assert len(set(native.references())) == 3


def test_outer_plan_selects_exact_host131_profile_with_original_deadline(monkeypatch, tmp_path):
    from scripts import production_offline_registry as registry
    from scripts import verify_miniflux_pause_native as pause
    image_id, seen = "sha256:" + "a" * 64, []
    monkeypatch.setattr(native.isolated, "cgroup_setup", lambda *a: "/owned")
    monkeypatch.setattr(native.host, "host_tools", lambda *a: None)
    monkeypatch.setattr(pause, "source_binding", lambda *a: {})
    monkeypatch.setattr(pause, "image_checks", lambda *a: None)
    monkeypatch.setattr(native.private, "source_identity", lambda *a: {})
    monkeypatch.setattr(native.isolated, "peer_identity", lambda: {"pid": 1})
    monkeypatch.setattr(native.isolated, "record", lambda *a, **kw: None)
    monkeypatch.setattr(native.host, "image_binding", lambda *a, **kw: {"Id": image_id})
    monkeypatch.setattr(native.host, "file_proof", lambda *a, **kw: {})

    class Commands:
        def __init__(self, root, end):
            self.deadline = end

        def inspect(self, *a, **kw):
            return {}

        def docker(self, *a, **kw):
            return ""

    monkeypatch.setattr(native.isolated, "FullCommands", Commands)

    def prepare(root, refs, end, *, profile="default"):
        seen.append((tuple(refs), end, profile))
        raise RuntimeError("stop at exact bundle binding")

    monkeypatch.setattr(registry, "prepare_bundle", prepare)
    args = SimpleNamespace(repo=native.ROOT, source=tmp_path, binary=tmp_path / "binary", image_id=image_id)
    with pytest.raises(RuntimeError, match="stop at exact bundle binding"):
        native.outer_plan(args, tmp_path, 100)
    assert seen == [(registry.profile_references("host-loopback131"), 260, "host-loopback131")]


def test_only_live_context_delegates_network_creation_to_the_private_daemon(tmp_path):
    harness = object.__new__(native.bootstrap.HostLoopbackHarness)
    harness.output = tmp_path
    calls = []
    row = {"Internal": False, "Id": "a" * 64}
    harness.private_host_context = SimpleNamespace(prepare_network=lambda actual: calls.append(actual) or row)
    harness.prepare_network()
    assert calls == [harness]
    assert (tmp_path / "NETWORK.json").read_bytes() == gate._encoded(row)


def _running_pg_row():
    from test_production_sender_bootstrap_direct131 import _host_pg_row
    row = _host_pg_row()
    row.update(Name="/owned-pg", Created="2026-10-31T00:00:00Z", RestartCount=0)
    row["State"].update(Running=True, Dead=False, Restarting=False, Pid=100, StartedAt="2026-10-31T00:00:01Z")
    return row


def test_live_pg_readiness_identity_keeps_exact_actual_mapping_and_lifecycle():
    harness = object.__new__(native.bootstrap.HostLoopbackHarness)
    row = _running_pg_row()
    harness.images, harness.names, harness.owner, harness.network = {"pg": row["Image"]}, {"pg": "owned-pg"}, "owner", "owned-private"
    identity = harness._pg_readiness_identity(row)
    assert identity["Pid"] == 100 and identity["RestartCount"] == 0
    for replacement in ({}, {"5432/tcp": None}):
        broken = deepcopy(row)
        broken["NetworkSettings"]["Ports"] = replacement
        with pytest.raises(gate.AdmissionError, match="resource/topology"):
            harness._pg_readiness_identity(broken)


@pytest.mark.parametrize("ready, health, expected", [
    (False, "starting", False), (True, "starting", False), (True, "healthy", True),
])
def test_private_pg_readiness_reuses_the_existing_classified_probe(monkeypatch, ready, health, expected):
    from scripts.verify_production_activation_native import ActivationHarness
    harness = object.__new__(native.bootstrap.HostLoopbackHarness)
    harness.private_host_context = object()
    harness.names = {"pg": "exact-pg"}
    calls = []
    monkeypatch.setattr(ActivationHarness, "postgres_ready", lambda actual: calls.append(actual) or ready)
    row = _running_pg_row()
    row["State"]["Health"]["Status"] = health
    harness.command = lambda argv: native.bootstrap.json.dumps([row])
    assert harness.postgres_ready() is expected and calls == [harness]


@pytest.mark.parametrize("health", [None, "unhealthy", "unknown"])
def test_actual_health_failure_or_unknown_is_never_ready(monkeypatch, health):
    from scripts.verify_production_activation_native import ActivationHarness
    harness = object.__new__(native.bootstrap.HostLoopbackHarness)
    harness.private_host_context, harness.names = object(), {"pg": "exact-pg"}
    monkeypatch.setattr(ActivationHarness, "postgres_ready", lambda actual: True)
    row = _running_pg_row()
    row["State"]["Health"]["Status"] = health
    harness.command = lambda argv: native.bootstrap.json.dumps([row])
    with pytest.raises(gate.AdmissionError, match="health failed or unrecognized"):
        harness.postgres_ready()


def test_private_command_does_not_start_a_fresh_timeout(monkeypatch):
    harness = object.__new__(native.bootstrap.HostLoopbackHarness)
    calls = []
    harness.private_host_context = SimpleNamespace(plan={"active_deadline": 100, "deadline": 120},
        check=lambda **kw: calls.append(("check", kw)), commands=SimpleNamespace(uncertain_mutation=False))
    monkeypatch.setattr(native.isolated, "remaining", lambda end: 2 if end == 100 else 3)
    monkeypatch.setattr(native.bootstrap.Harness, "command",
        lambda actual, *args, **kw: calls.append(("command", kw)) or "actual")
    assert harness.command(["inspect", "exact"], timeout=30) == "actual"
    assert calls[1] == ("command", {"timeout": 2})
    calls.clear()
    assert harness.command(["inspect", "exact"], timeout=30, final=True) == "actual"
    assert calls[1] == ("command", {"timeout": 3, "final": True})


def test_unknown_command_outcome_prevents_daemon_cleanup(monkeypatch):
    harness = object.__new__(native.bootstrap.HostLoopbackHarness)
    context = SimpleNamespace(plan={"active_deadline": 100}, check=lambda **kw: None,
        commands=SimpleNamespace(uncertain_mutation=False))
    harness.private_host_context = context
    monkeypatch.setattr(native.isolated, "remaining", lambda end: 2)
    def uncertain(*args, **kwargs):
        raise TimeoutError("unknown transport")
    monkeypatch.setattr(native.bootstrap.Harness, "command", uncertain)
    with pytest.raises(TimeoutError, match="unknown transport"):
        harness.command(["start", "exact"])
    assert context.commands.uncertain_mutation is True


# Local export tests: no Docker, subprocess, network or production calls.
@pytest.fixture
def failure_export(tmp_path, monkeypatch):
    import hashlib
    import json

    isolated = native.isolated
    monkeypatch.setattr(isolated.time, "monotonic", lambda: 100)
    for module, field in ((isolated.subprocess, "run"), (isolated.subprocess, "Popen")):
        monkeypatch.setattr(module, field, lambda *a, **kw: pytest.fail("diagnostic executed a command"))
    root = tmp_path / "originals"
    command = root / "inner/activation/command-0109.json"
    command.parent.mkdir(parents=True)
    observation = {"schema": 1, "sequence": 109, "operation": "docker_create", "timeout_seconds": 3,
        "harness_remaining_seconds": 338.2, "elapsed_seconds": 3.005, "returncode": None, "outcome": "timeout"}
    value = {"argv": ["docker", "--host", "unix:///var/run/docker.sock", "create", "DO-NOT-EXPORT-ARGV"], "rc": None,
        "stdin_sha256": hashlib.sha256(b"").hexdigest(), "elapsed_seconds": 3.006,
        "utc": "2026-11-01T03:00:00Z", "stdout": "DO-NOT-EXPORT-STDOUT", "stderr": "create pending\n"}
    resource = {**isolated.SUPERVISOR, "scope": isolated.RESOURCE_SCOPE, "memory.peak": "12345",
        "memory.events": {"low": "0", "high": "0", "max": "3", "oom": "0", "oom_kill": "0"}}
    files = {"inner/activation/HARNESS_COMMAND_FAILURE.json": observation,
        "inner/activation/command-0109.json": value, "inner/RESOURCES_FINAL.json": resource}
    for relative, content in files.items():
        (root / relative).write_text(json.dumps(content))
    (root / "inner/daemon.stderr").write_text("daemon waiting\n")

    def records():
        return [{"path_sha256": hashlib.sha256(str(path.relative_to(root)).encode()).hexdigest(),
                 "bytes": path.stat().st_size, "sha256": hashlib.sha256(path.read_bytes()).hexdigest()}
                for path in root.rglob("*") if path.is_file()]

    def run(rows=None, profile=None):
        return isolated.failure_originals(root, records() if rows is None else rows,
            {"inner/activation/HARNESS_COMMAND_FAILURE.json": observation if profile is None else profile}, 600)

    return SimpleNamespace(root=root, command=command, value=value, resources=resource, observation=observation,
                           records=records, run=run, isolated=isolated)


def test_failure_export_exact_command_hash_empty_stderr_and_supervisor_scope(failure_export):
    import hashlib
    import json

    case = failure_export
    case.value["stderr"] = ""
    case.command.write_text(json.dumps(case.value))
    result = case.run()
    assert result["observation"]["sequence"] == 109 and result["observation"]["returncode"] is None
    assert result["command"]["original"]["sha256"] == hashlib.sha256(case.command.read_bytes()).hexdigest()
    assert result["command"]["text"] == "" and result["command"]["stderr_bytes"] == 0
    assert result["resources"]["profile"]["memory.peak"] == 12345
    assert result["resources"]["profile"]["memory.events"]["max"] == 3
    assert result["resources"]["profile"]["scope"] == case.isolated.RESOURCE_SCOPE
    for kind in ("command", "daemon", "resources"):
        assert not {"stage", "reason_code", "error_sha256"} & result[kind].keys()
    assert set(result["resources"]) == {"status", "original", "profile"}
    assert "DO-NOT-EXPORT" not in json.dumps(result)
    assert "sender" not in result["resources"]["profile"]["scope"]


@pytest.mark.parametrize("which", ["command", "daemon", "resources", "observation"])
@pytest.mark.parametrize("fault", ["missing", "wrong_hash", "wrong_size", "duplicate", "symlink"])
def test_failure_export_requires_unique_original_commitment(failure_export, which, fault):
    case = failure_export
    names = {"command": "inner/activation/command-0109.json", "daemon": "inner/daemon.stderr",
        "resources": "inner/RESOURCES_FINAL.json", "observation": "inner/activation/HARNESS_COMMAND_FAILURE.json"}
    import hashlib

    path = case.root / names[which]
    rows = case.records()
    row = next(value for value in rows if value["path_sha256"] == hashlib.sha256(names[which].encode()).hexdigest())
    if fault == "missing":
        rows.remove(row)
    elif fault == "wrong_hash":
        row["sha256"] = "f" * 64
    elif fault == "wrong_size":
        row["bytes"] += 1
    elif fault == "duplicate":
        rows.append(dict(row))
    else:
        target = path.with_name(path.name + ".original")
        path.rename(target)
        path.symlink_to(target)
    result = case.run(rows)
    assert (result if which == "observation" else result[which])["status"] == "evidence_insufficient"


def test_failure_export_wrong_sequence_does_not_find_an_alternate(failure_export):
    case = failure_export
    profile = {**case.observation, "sequence": 110}
    assert case.run(profile=profile)["status"] == "evidence_insufficient"


@pytest.mark.parametrize("field,value", [("argv", ["docker", "create"]), ("rc", 0),
    ("elapsed_seconds", float("nan")), ("stderr", None), ("stdin_sha256", "invalid")])
def test_failure_export_malformed_or_different_command_is_missing(failure_export, field, value):
    import json
    case = failure_export
    case.value[field] = value
    case.command.write_text(json.dumps(case.value))
    assert case.run()["command"]["status"] == "evidence_insufficient"


@pytest.mark.parametrize("secret", ["password=top-secret", "Bearer top-secret", "--api-token top-secret",
    "-----BEGIN PRIVATE KEY----- top-secret", "https://user:top-secret@example.invalid/x",
    "A" * 64, "cookie: top-secret", "PRIVATE KEY top-secret"])
def test_failure_export_hashes_sensitive_whole_lines(failure_export, secret):
    import json
    case = failure_export
    ending = "-----END PRIVATE KEY-----\n" if "BEGIN PRIVATE KEY" in secret else ""
    case.value["stderr"] = secret + "\n" + ending + "ordinary diagnostic\n"
    case.command.write_text(json.dumps(case.value))
    (case.root / "inner/daemon.stderr").write_text(secret + "\n" + ending + "ordinary daemon\n")
    result = case.run()
    assert "top-secret" not in json.dumps(result) and "A" * 64 not in json.dumps(result)
    assert "<sensitive-line-sha256:" in result["command"]["text"]
    assert "ordinary diagnostic" in result["command"]["text"]
    assert "ordinary daemon" in result["daemon"]["text"]


@pytest.mark.parametrize("kind", ["command", "daemon"])
def test_failure_export_mutation_during_read_is_missing_without_retry(failure_export, monkeypatch, kind):
    case = failure_export
    actual = case.isolated.host.file_proof
    target = case.command if kind == "command" else case.root / "inner/daemon.stderr"
    count = 0

    def changed(path, *args, **kwargs):
        nonlocal count
        value = actual(path, *args, **kwargs)
        if path == target:
            count += 1
            if count == 1:
                path.write_bytes(path.read_bytes() + b"changed")
        return value

    monkeypatch.setattr(case.isolated.host, "file_proof", changed)
    assert case.run()[kind]["status"] == "evidence_insufficient"
    assert count == (1 if kind == "command" else 2)  # Initial/final proofs, never a retry.


@pytest.mark.parametrize("fault", ["scope", "peak", "event", "extra_event", "extra_field"])
def test_failure_export_invalid_resource_scope_or_counters_not_success(failure_export, fault):
    import json
    case = failure_export
    if fault == "scope":
        del case.resources["scope"]
    elif fault == "peak":
        case.resources["memory.peak"] = "-1"
    elif fault == "event":
        case.resources["memory.events"]["oom"] = True
    elif fault == "extra_event":
        case.resources["memory.events"]["secret"] = "1"
    else:
        case.resources["secret"] = "x"
    (case.root / "inner/RESOURCES_FINAL.json").write_text(json.dumps(case.resources))
    assert case.run()["resources"]["status"] == "evidence_insufficient"


def test_failure_export_bounded_tails_and_oversized_command(failure_export):
    import json
    case = failure_export
    case.value["stderr"] = "discarded-partial-secret" + "x" * 4096 + "\nlast diagnostic\n"
    case.command.write_text(json.dumps(case.value))
    (case.root / "inner/daemon.stderr").write_text("discarded-partial-secret" + "x" * 8192 + "\nlast daemon\n")
    result = case.run()
    assert result["command"]["truncated"] and result["daemon"]["truncated"]
    assert len(result["command"]["text"].encode()) <= 4096 and len(result["daemon"]["text"].encode()) <= 8192
    assert "discarded-partial-secret" not in json.dumps(result)
    case.value["stdout"] = "x" * case.isolated.FAILURE_COMMAND_BYTES
    case.command.write_text(json.dumps(case.value))
    assert case.run()["command"]["status"] == "evidence_insufficient"


def test_failure_export_not_observed_never_reads_other_files(failure_export, monkeypatch):
    case = failure_export
    monkeypatch.setattr(case.isolated, "_failure_original", lambda *a, **kw: pytest.fail("unexpected file read"))
    assert case.isolated.failure_originals(case.root, [], {}, 600) == {"status": "not_observed"}


def test_safe_inventory_exports_diagnostics_without_altering_completion(failure_export, monkeypatch):
    import json
    case = failure_export
    # Existing sealed INNER_RESULT capability is outside this local export unit.
    monkeypatch.setattr(case.isolated, "original", lambda path: (json.loads(path.read_text()), {}))
    for relative in ("inner/INNER_RESULT.json", "inner/activation/RESULT.json", "inner/DAEMON_STOP.json"):
        (case.root / relative).write_text("{}")
    packet = case.isolated.safe_inventory(case.root, 600)
    assert packet["complete"] is True and packet["full_raw_archive_verified"] is False
    assert packet["failure_originals"]["command"]["status"] == "available"
    assert len(case.isolated.host.encoded(packet)) <= 384 * 1024


@pytest.mark.parametrize("kind", ["command", "daemon", "resources", "observation"])
def test_failure_export_missing_actual_original_is_missing(failure_export, kind):
    case = failure_export
    names = {"command": "inner/activation/command-0109.json", "daemon": "inner/daemon.stderr",
        "resources": "inner/RESOURCES_FINAL.json", "observation": "inner/activation/HARNESS_COMMAND_FAILURE.json"}
    rows = case.records()
    (case.root / names[kind]).unlink()
    result = case.run(rows)
    assert (result if kind == "observation" else result[kind])["status"] == "evidence_insufficient"


@pytest.mark.parametrize("allow_fallback", [True, False])
def test_optional_failure_diagnostics_never_displace_original_packet(failure_export, monkeypatch, allow_fallback):
    import json
    case = failure_export
    for relative in ("inner/INNER_RESULT.json", "inner/activation/RESULT.json", "inner/DAEMON_STOP.json"):
        (case.root / relative).write_text("{}")
    monkeypatch.setattr(case.isolated, "original", lambda path: (json.loads(path.read_text()), {}))
    actual = case.isolated.host.encoded

    def bounded(value):
        if isinstance(value, dict) and "failure_originals" in value:
            fallback = value["failure_originals"].get("status") == "export_budget_exhausted"
            if not fallback or not allow_fallback:
                return b"x" * (384 * 1024 + 1)
        return actual(value)

    monkeypatch.setattr(case.isolated.host, "encoded", bounded)
    packet = case.isolated.safe_inventory(case.root, 600)
    assert packet["complete"] is True and packet["full_raw_archive_verified"] is False
    if allow_fallback:
        assert packet["failure_originals"] == {"status": "export_budget_exhausted"}
    else:
        assert "failure_originals" not in packet


@pytest.mark.parametrize("profile", [None, {}, {"schema": 1, "sequence": True}, {"status": "success"}])
def test_unknown_failure_observation_never_becomes_not_observed_or_success(failure_export, monkeypatch, profile):
    case = failure_export
    monkeypatch.setattr(case.isolated, "_failure_original", lambda *a, **kw: pytest.fail("invalid observation read files"))
    assert case.isolated.failure_originals(case.root, [],
        {"inner/activation/HARNESS_COMMAND_FAILURE.json": profile}, 600) == {"status": "evidence_insufficient"}


def test_private_key_short_body_lines_remain_hashes(failure_export):
    raw = b"-----BEGIN PRIVATE KEY-----\nsh0rtB0dy\n-----END PRIVATE KEY-----\nnormal daemon\n"
    result = failure_export.isolated._failure_text(raw, 8192)
    assert "sh0rtB0dy" not in result["text"] and "normal daemon" in result["text"]
    assert result["text"].count("<sensitive-line-sha256:") == 3


@pytest.mark.parametrize("path", ["/私有目录/配置.json", "/tmp/客户数据/配置.json", "/tmp/private/config.json"])
def test_failure_export_masks_unicode_and_ascii_absolute_paths(failure_export, path):
    text = failure_export.isolated._failure_text(("open " + path + ": permission denied").encode(), 8192)["text"]
    assert path not in text and "<path>" in text and "permission denied" in text


@pytest.mark.parametrize("ending", [b"-----END PRIVATE KEY-----\n", b""])
def test_truncated_private_key_without_header_does_not_export_short_body(failure_export, ending):
    raw = b"discarded header\n" + b"x" * 8192 + b"\nQUJDREVGR0hJSg==\n" + ending + b"normal daemon\n"
    text = failure_export.isolated._failure_text(raw, 8192)["text"]
    assert "QUJDREVGR0hJSg==" not in text and "<sensitive-line-sha256:" in text
    assert "normal daemon" in text


@pytest.mark.parametrize("fault,stage,reason,message", [
    ("json", "parse_json", "invalid_json", None),
    ("scope", "validate_resources", "resource_scope_invalid", "failure diagnostic resource scope invalid"),
    ("counters", "validate_resources", "resource_counters_invalid", "failure diagnostic resource counters invalid"),
])
def test_failure_resource_parse_or_schema_keeps_verified_original(failure_export, fault, stage, reason, message):
    import hashlib
    import json

    case = failure_export
    path = case.root / "inner/RESOURCES_FINAL.json"
    if fault == "json":
        raw = b"{DO-NOT-EXPORT-RESOURCE-CONTENT"
        with pytest.raises(json.JSONDecodeError) as invalid:
            json.loads(raw)
        message = str(invalid.value)
    else:
        if fault == "scope":
            case.resources["scope"] = "DO-NOT-EXPORT-RESOURCE-CONTENT"
        else:
            case.resources["memory.events"]["oom"] = "DO-NOT-EXPORT-RESOURCE-CONTENT"
        raw = json.dumps(case.resources).encode()
    path.write_bytes(raw)
    result = case.run()
    item = result["resources"]
    expected = next(row for row in case.records()
                    if row["path_sha256"] == hashlib.sha256(b"inner/RESOURCES_FINAL.json").hexdigest())
    assert item["status"] == "evidence_insufficient" and item["original"] == expected
    assert item["stage"] == stage and item["reason_code"] == reason
    assert item["error_sha256"] == hashlib.sha256(message.encode()).hexdigest()
    assert "profile" not in item and "DO-NOT-EXPORT" not in json.dumps(result)


@pytest.mark.parametrize("fault,reason,proof_reads", [
    ("missing_commitment", "original_commitment_missing", 0),
    ("wrong_hash", "original_hash_changed", 1),
    ("expired", "shared_deadline_rejected", 0),
    ("missing_file", "stage_failure", 0),
])
def test_failure_resource_read_rejection_never_exports_a_partial_proof(
        failure_export, monkeypatch, fault, reason, proof_reads):
    import hashlib
    import json

    case = failure_export
    path, relative = case.root / "inner/RESOURCES_FINAL.json", "inner/RESOURCES_FINAL.json"
    rows, errors, observed_reads = case.records(), [], []
    row = next(item for item in rows if item["path_sha256"] == hashlib.sha256(relative.encode()).hexdigest())
    if fault == "missing_commitment":
        rows.remove(row)
    elif fault == "wrong_hash":
        row["sha256"] = "f" * 64
    elif fault == "missing_file":
        path.unlink()
    actual, actual_proof = case.isolated._failure_original, case.isolated.host.file_proof

    def observe(root, name, records, deadline, **kwargs):
        try:
            return actual(root, name, records, 99 if name == relative and fault == "expired" else deadline, **kwargs)
        except Exception as error:
            if name == relative:
                errors.append(error)
            raise

    def proof(file, *args, **kwargs):
        if file == path:
            observed_reads.append(file)
        return actual_proof(file, *args, **kwargs)

    monkeypatch.setattr(case.isolated, "_failure_original", observe)
    monkeypatch.setattr(case.isolated.host, "file_proof", proof)
    result = case.run(rows)
    item = result["resources"]
    assert len(errors) == 1 and len(observed_reads) == proof_reads
    assert item["status"] == "evidence_insufficient" and item["stage"] == "read_original"
    assert item["reason_code"] == reason and "original" not in item and "profile" not in item
    assert item["error_sha256"] == hashlib.sha256(str(errors[0]).encode()).hexdigest()
    assert str(path) not in json.dumps(result)


@pytest.mark.parametrize("message", ["password=DO-NOT-EXPORT https://user:secret@example.invalid/private",
                                     "DO-NOT-EXPORT-PRIVATE-PATH-/private/\udcff"])
def test_failure_resource_unknown_sensitive_exception_is_hash_only(failure_export, monkeypatch, message):
    import hashlib
    import json

    case = failure_export

    def failed(_value):
        raise gate.AdmissionError(message)

    monkeypatch.setattr(case.isolated, "_failure_resources", failed)
    result = case.run()
    item = result["resources"]
    assert item["status"] == "evidence_insufficient" and item["stage"] == "validate_resources"
    assert item["reason_code"] == "stage_failure" and "original" in item and "profile" not in item
    assert item["error_sha256"] == hashlib.sha256(message.encode("utf-8", errors="backslashreplace")).hexdigest()
    assert "DO-NOT-EXPORT" not in json.dumps(result) and "secret" not in json.dumps(result)


@pytest.mark.parametrize("raw,category", [
    (b"context deadline exceeded", "timeout_literal"),
    (b"no space left on device", "capacity_literal"),
    (b"permission denied", "permission_literal"),
    (b"OCI runtime create failed", "runtime_failure_literal"),
    (b"cannot allocate memory", "memory_failure_literal"),
    (b"level=error", "other_error_marker"),
])
def test_daemon_literal_category_is_observation_not_cause(raw, category):
    result = native.isolated._daemon_error_counts(raw)
    assert result["counts"][category] == 1
    assert result["scope"] == "bounded_daemon_tail_literals_not_root_cause"
    assert result["error_absence_not_proven"] is True
    assert "oom_proven" not in result and "status" not in result


@pytest.mark.parametrize("raw,unclassified", [(b"", 0), (b"new unspecified failure", 1),
    (b"<sensitive-line-sha256:" + b"a" * 64 + b">", 1)])
def test_daemon_zero_or_hash_only_categories_never_claim_no_error(raw, unclassified):
    result = native.isolated._daemon_error_counts(raw)
    assert not any(result["counts"].values())
    assert result["unclassified_nonempty_lines"] == unclassified
    assert result["error_absence_not_proven"] is True


def test_daemon_category_counts_per_complete_line_and_can_overlap():
    raw = b"permission denied prefix\nlevel=error context deadline exceeded i/o timeout\n"
    result = native.isolated._daemon_error_counts(raw, partial=True)
    assert result["counts"]["permission_literal"] == 0
    assert result["counts"]["timeout_literal"] == result["counts"]["other_error_marker"] == 1
    assert result["scanned_nonempty_lines"] == 1 and result["window_partial"] is True
    empty = native.isolated._daemon_error_counts(b"permission denied fragment", partial=True)
    assert empty["scanned_nonempty_lines"] == 0 and empty["error_absence_not_proven"] is True


@pytest.mark.parametrize("raw,partial", [(b"x" * 8193, False), ("not bytes", False), (b"x", 1)])
def test_daemon_category_guard_rejects_input_outside_existing_tail_bound(raw, partial):
    with pytest.raises(ValueError, match="fixed bounded daemon literal input required"):
        native.isolated._daemon_error_counts(raw, partial=partial)


def test_daemon_error_categories_keep_original_redaction_proof_and_four_read_calls(failure_export, monkeypatch):
    import json

    case, reads = failure_export, []
    raw = b"level=error permission denied password=DO-NOT-EXPORT /PRIVATE/argv\n"
    path = case.root / "inner/daemon.stderr"
    path.write_bytes(raw)
    rows = case.records()
    original = case.isolated._failure_original
    expected = next(item for item in rows if item["path_sha256"] ==
                    case.isolated.hashlib.sha256(b"inner/daemon.stderr").hexdigest())
    def observe(root, relative, records, deadline, **kwargs):
        reads.append((relative, kwargs))
        return original(root, relative, records, deadline, **kwargs)
    monkeypatch.setattr(case.isolated, "_failure_original", observe)
    result = case.run(rows)
    assert len(reads) == 4 and reads[2] == ("inner/daemon.stderr", {"limit": 8 * 1024**2, "tail": 8192})
    assert result["daemon"]["original"] == expected
    assert result["daemon"]["redacted"] is True and "sensitive-line-sha256" in result["daemon"]["text"]
    assert result["daemon"]["error_literals"]["counts"]["permission_literal"] == 1
    assert result["daemon"]["error_literals"]["error_absence_not_proven"] is True
    assert "DO-NOT-EXPORT" not in json.dumps(result) and "PRIVATE" not in json.dumps(result)
    assert result["status"] == "diagnostic_only_not_execution_proof"


def test_daemon_read_failure_never_produces_error_counts_or_partial_proof(failure_export):
    case = failure_export
    rows = case.records()
    (case.root / "inner/daemon.stderr").unlink()
    item = case.run(rows)["daemon"]
    assert item["status"] == "evidence_insufficient" and item["stage"] == "read_original"
    assert "original" not in item and "error_literals" not in item and "text" not in item


def _resource_compat_record():
    return {**native.isolated.SUPERVISOR, "scope": native.isolated.RESOURCE_SCOPE,
        "memory.peak": "12345", "memory.events": {key: "0" for key in
            ("low", "high", "max", "oom", "oom_kill", "oom_group_kill")}}


@pytest.mark.parametrize("with_sock_counter", [False, True])
def test_failure_resources_preserve_six_key_and_observed_seven_key_forms(with_sock_counter):
    value = _resource_compat_record()
    if with_sock_counter:
        value["memory.events"]["sock_throttled"] = "0"
    result = native.isolated._failure_resources(value)
    assert result["memory.events"] == {key: 0 for key in value["memory.events"]}
    assert result["memory.peak"] == 12345 and result["scope"] == native.isolated.RESOURCE_SCOPE


def test_failure_resources_still_reject_unknown_event_even_with_observed_sock_counter():
    value = _resource_compat_record()
    value["memory.events"].update(sock_throttled="0", unreviewed_counter="0")
    with pytest.raises(gate.AdmissionError, match="failure diagnostic resource counters invalid"):
        native.isolated._failure_resources(value)


def test_failure_resources_keep_canonical_decimal_type_and_length_guards():
    for key in ("sock_throttled", "max"):
        for invalid in ("-1", "01", "1.0", "1" * 21, "", 1, True, None):
            value = _resource_compat_record()
            value["memory.events"][key] = invalid
            with pytest.raises(gate.AdmissionError, match="failure diagnostic resource counters invalid"):
                native.isolated._failure_resources(value)


class _ResourceCompatText:
    """Synthetic fixed cgroup text only; no filesystem access."""
    def __init__(self, values, name=""):
        self.values, self.name = values, name

    def __truediv__(self, name):
        return type(self)(self.values, name)

    def read_text(self):
        return self.values[self.name]


def _resource_compat_kernel(monkeypatch):
    value = _resource_compat_record()
    files = {**native.isolated.SUPERVISOR, "memory.peak": "12345\n",
        "memory.events": "".join(key + " " + count + "\n" for key, count in value["memory.events"].items())
            + "sock_throttled 7\n"}
    monkeypatch.setattr(native.isolated.host, "CGROUP", _ResourceCompatText(files))
    return files


def test_nonzero_sock_counter_is_reported_without_becoming_an_oom_finding(monkeypatch):
    _resource_compat_kernel(monkeypatch)
    record = native.isolated.resource_check({"cgroup": "/synthetic"})
    result = native.isolated._failure_resources(record)
    assert result["memory.events"]["sock_throttled"] == 7
    assert result["memory.events"]["oom"] == result["memory.events"]["oom_kill"] == 0
    assert set(result) == set(native.isolated.SUPERVISOR) | {"memory.peak", "memory.events", "scope"}


@pytest.mark.parametrize("key", ["oom", "oom_kill"])
def test_observed_sock_counter_never_relaxes_operational_oom_stop(monkeypatch, key):
    files = _resource_compat_kernel(monkeypatch)
    files["memory.events"] = files["memory.events"].replace(key + " 0\n", key + " 1\n")
    with pytest.raises(gate.AdmissionError, match="private host supervisor OOM"):
        native.isolated.resource_check({"cgroup": "/synthetic"})


def test_observed_sock_counter_never_relaxes_operational_resource_limits(monkeypatch):
    files = _resource_compat_kernel(monkeypatch)
    files["memory.max"] = "999999999"
    with pytest.raises(gate.AdmissionError, match="private host supervisor resource drift"):
        native.isolated.resource_check({"cgroup": "/synthetic"})


# Optional resource original retention is diagnostic-only; no new execution or IO.
def test_failure_resource_unknown_counter_preserves_exact_original_but_stays_insufficient(failure_export):
    import hashlib
    import json

    case = failure_export
    case.resources["memory.events"]["future_event"] = "7"
    path = case.root / "inner/RESOURCES_FINAL.json"
    raw = json.dumps(case.resources, sort_keys=True).encode()
    path.write_bytes(raw)
    result = case.run()
    item = result["resources"]
    assert item["status"] == "evidence_insufficient"
    assert item["reason_code"] == "resource_counters_invalid" and "profile" not in item
    payload = item["original_payload"]
    assert payload["content"].encode(payload["encoding"]) == raw
    assert payload["bytes"] == len(raw)
    assert payload["sha256"] == item["original"]["sha256"] == hashlib.sha256(raw).hexdigest()
    assert "original_payload" not in result["command"] and "original_payload" not in result["daemon"]
    assert "DO-NOT-EXPORT" not in json.dumps(result)


def test_failure_resource_valid_projection_shape_is_unchanged(failure_export):
    result = failure_export.run()
    assert set(result["resources"]) == {"status", "original", "profile"}


@pytest.mark.parametrize("kind", ["duplicate_top", "duplicate_event", "malformed", "sensitive_name",
    "sensitive_value", "wrong_scope", "wrong_limit", "integer_count", "leading_zero", "negative",
    "non_ascii_name", "long_name", "too_many_events", "oversized_raw"])
def test_failure_resource_payload_rejects_untrusted_raw_without_export(failure_export, kind):
    import json

    case = failure_export
    case.resources["memory.events"]["future_event"] = "1"
    value = deepcopy(case.resources)
    if kind == "sensitive_name":
        value["memory.events"]["api_token"] = "1"
    elif kind == "sensitive_value":
        value["memory.events"]["future_event"] = "PRIVATE-SECRET"
    elif kind == "wrong_scope":
        value["scope"] = "private scope"
    elif kind == "wrong_limit":
        value["memory.max"] = "1"
    elif kind == "integer_count":
        value["memory.events"]["future_event"] = 1
    elif kind == "leading_zero":
        value["memory.events"]["future_event"] = "01"
    elif kind == "negative":
        value["memory.events"]["future_event"] = "-1"
    elif kind == "non_ascii_name":
        value["memory.events"]["秘密"] = "1"
    elif kind == "long_name":
        value["memory.events"]["a" * 33] = "1"
    elif kind == "too_many_events":
        value["memory.events"] = {"oom": "0", "oom_kill": "0", **{chr(97 + i): "0" for i in range(15)}}
    raw = json.dumps(value, ensure_ascii=True).encode()
    if kind == "duplicate_top":
        raw = b'{"scope":"PRIVATE-SECRET",' + raw[1:]
    elif kind == "duplicate_event":
        raw = raw.replace(b'"future_event": "1"', b'"future_event":"PRIVATE-SECRET","future_event":"1"')
    elif kind == "malformed":
        raw = raw[:-1]
    elif kind == "oversized_raw":
        raw += b" " * 4096
    (case.root / "inner/RESOURCES_FINAL.json").write_bytes(raw)
    result = case.run()
    assert result["resources"]["status"] == "evidence_insufficient"
    assert "original_payload" not in result["resources"]
    assert "PRIVATE-SECRET" not in json.dumps(result)


@pytest.mark.parametrize("field", ["path_sha256", "sha256", "bytes"])
def test_failure_resource_payload_requires_same_original_proof(failure_export, field):
    import hashlib
    import json

    case = failure_export
    case.resources["memory.events"]["future_event"] = "1"
    raw = json.dumps(case.resources).encode()
    proof = {"path_sha256": hashlib.sha256(b"inner/RESOURCES_FINAL.json").hexdigest(),
             "sha256": hashlib.sha256(raw).hexdigest(), "bytes": len(raw)}
    proof[field] = proof[field] + 1 if field == "bytes" else "f" * 64
    assert case.isolated._failure_resource_payload(raw, case.resources, proof) is None


def test_failure_resource_payload_keeps_existing_combined_budget(failure_export, monkeypatch):
    import json

    case = failure_export
    case.resources["memory.events"]["future_event"] = "1"
    (case.root / "inner/RESOURCES_FINAL.json").write_text(json.dumps(case.resources))
    assert case.isolated.FAILURE_TEXT_BYTES == 16 * 1024
    actual = case.isolated.host.encoded
    def encoded(value):
        if type(value) is dict and value.get("scope") == "resource_original_only_not_counter_validation":
            return b"x" * 4097
        return actual(value)
    monkeypatch.setattr(case.isolated.host, "encoded", encoded)
    assert "original_payload" not in case.run()["resources"]


def test_optional_resource_original_cannot_displace_existing_failure_diagnostics(failure_export):
    import json

    case = failure_export
    case.value["stderr"] = "safe diagnostic text line\n" * 300
    case.command.write_text(json.dumps(case.value))
    (case.root / "inner/daemon.stderr").write_text("safe daemon diagnostic line\n" * 400)
    case.resources["memory.events"]["future_event"] = "1"
    # This original is within its own 4 KiB cap; the full packet exceeds 16 KiB.
    (case.root / "inner/RESOURCES_FINAL.json").write_bytes(json.dumps(case.resources).encode() + b" " * 2100)
    result = case.run()
    assert result["status"] == "diagnostic_only_not_execution_proof"
    assert result["observation"] == case.observation
    assert result["command"]["status"] == result["daemon"]["status"] == "available"
    assert result["resources"]["status"] == "evidence_insufficient"
    assert result["resources"]["reason_code"] == "resource_counters_invalid"
    assert "original" in result["resources"] and "original_payload" not in result["resources"]
    assert len(case.isolated.host.encoded(result)) <= 16 * 1024


def test_original_combined_failure_budget_still_rejects_after_optional_payload_removed(failure_export, monkeypatch):
    import json

    case = failure_export
    case.resources["memory.events"]["future_event"] = "1"
    (case.root / "inner/RESOURCES_FINAL.json").write_text(json.dumps(case.resources))
    actual, observed = case.isolated.host.encoded, []
    def encoded(value):
        if type(value) is dict and "observation" in value and "resources" in value:
            observed.append("original_payload" in value["resources"])
            return b"x" * (16 * 1024 + 1)
        return actual(value)
    monkeypatch.setattr(case.isolated.host, "encoded", encoded)
    assert case.run() == {"status": "export_budget_exhausted"}
    assert observed == [True, False]


def test_optional_command_packet_counts_final_newline(tmp_path, monkeypatch, capsys):
    root = tmp_path / "outer"
    result = {"status": "failed_or_evidence_insufficient", "padding": "", "command_failure": {
        "execution_proof": False, "operation": "docker_info", "sequence": 1, "returncode": None,
        "elapsed_seconds": 3, "budget_seconds": 3, "error": "ProofError"}}
    monkeypatch.setattr(native, "arguments", lambda argv: SimpleNamespace(output=root, inside=False))
    monkeypatch.setattr(native.host, "private_directory", lambda path, **kw: path.mkdir())
    monkeypatch.setattr(native, "outer_execute", lambda *args: deepcopy(result))
    monkeypatch.setattr(native.time, "monotonic", lambda: 100)
    assert native.main([]) == 3
    raw = capsys.readouterr().out
    assert raw.endswith("\n")
    result["padding"] = "x" * (512 * 1024 - (len(raw.encode()) - 1))
    root = tmp_path / "outer-second"
    assert native.main([]) == 3
    raw = capsys.readouterr().out
    value = json.loads(raw)
    assert len(raw.encode()) <= 512 * 1024
    assert "command_failure" not in value["private_host"]["result"]
    assert value["private_host"]["result"]["padding"] == result["padding"]
    assert value["status"] == "unconfirmed"


@pytest.mark.parametrize("diagnostic_error", [None, OSError("SECRET_DIAGNOSTIC"), TimeoutError("SECRET_TIMEOUT")])
def test_early_projection_uses_original_remaining_hard_deadline_and_keeps_failure(tmp_path, monkeypatch, diagnostic_error):
    from contextlib import contextmanager
    from scripts import production_gateway_switch as switch
    result = {"status": "failed_or_evidence_insufficient", "bootstrap_result": None, "daemon_stop": None,
              "error_type": "ProofError", "error_sha256": "a" * 64}
    failure = {"error_type": "ConnectionRefusedError", "error_sha256": "b" * 64}
    packet = {"status": "unconfirmed", "private_host": {"result": deepcopy(result), "failure": deepcopy(failure)},
              "safe_evidence": {"complete": False, "files": {}}}
    entered = []
    @contextmanager
    def bounded(seconds):
        entered.append(seconds)
        yield
        entered.append("exit")
    def export(root, actual, end):
        assert entered == [7.0] and end == 280 and actual == result
        if diagnostic_error:
            raise diagnostic_error
        return {"complete": False, "execution_proof": False, "status": "diagnostic_only"}
    monkeypatch.setattr(native.time, "monotonic", lambda: 273.0)
    monkeypatch.setattr(switch, "_hard_deadline", bounded)
    monkeypatch.setattr(native.early, "export", export)
    monkeypatch.setattr(native.bootstrap, "safe_export", lambda *a: pytest.fail("no bootstrap original exists"))
    native.export_diagnostics(packet, tmp_path, 280)
    assert entered[0] == 7.0 and packet["private_host"] == {"result": result, "failure": failure}
    assert packet["status"] == "unconfirmed" and not packet["safe_evidence"]["complete"]
    assert "SECRET" not in json.dumps(packet)


def test_expired_export_does_not_read_files_or_get_fresh_budget(tmp_path, monkeypatch):
    from scripts import production_gateway_switch as switch
    packet = {"status": "unconfirmed", "private_host": {"result": None, "failure": {"error_type": "ProofError"}},
              "safe_evidence": {"complete": False, "files": {}}}
    monkeypatch.setattr(native.time, "monotonic", lambda: 280.0)
    monkeypatch.setattr(switch, "_hard_deadline", lambda *a: pytest.fail("expired run received a new deadline"))
    monkeypatch.setattr(native.early, "export", lambda *a: pytest.fail("expired run attempted reads"))
    native.export_diagnostics(packet, tmp_path, 280)
    assert packet["early_diagnostics"]["status"] == "evidence_insufficient"
    assert packet["private_host"]["failure"] == {"error_type": "ProofError"}


@pytest.mark.parametrize("resource_failure", [False, True])
def test_daemon_start_failure_records_resources_without_confirming_cleanup(tmp_path, monkeypatch, resource_failure):
    from scripts import production_offline_registry as registry
    records, actions = {}, []
    plan = {"started": 100.0, "active_deadline": 260.0, "deadline": 280.0, "bundle": {}, "parent": {}}
    primary = gate.AdmissionError("SECRET_PRIMARY")
    primary.native_host_command = {"execution_proof": False, "operation": "docker_info", "sequence": 11,
        "returncode": None, "elapsed_seconds": 3.001, "budget_seconds": 3.0, "error": "AdmissionError"}
    monkeypatch.setattr(native.os, "setsid", lambda: None)
    monkeypatch.setattr(native.time, "monotonic", lambda: 120.0)
    monkeypatch.setattr(native.host, "proc_identity", lambda *a: {"netns": 3})
    monkeypatch.setattr(native.host, "restrict_private_ipv6", lambda *a: None)
    monkeypatch.setattr(native.isolated, "FullCommands", lambda *a: SimpleNamespace())
    monkeypatch.setattr(native.isolated, "prepare_mounts", lambda *a: None)
    monkeypatch.setattr(registry, "serve_bundle", lambda *a, **kw: nullcontext(SimpleNamespace(url="synthetic")))
    def start(*args):
        raise primary
    monkeypatch.setattr(native.isolated, "start_daemon", start)
    monkeypatch.setattr(native.isolated, "stop_daemon", lambda *a: pytest.fail("start did not return a context"))
    def resources(actual):
        actions.append("resources")
        assert actual == plan
        if resource_failure:
            raise gate.AdmissionError("SECRET_RESOURCE_GUARD")
        return {"fixed_counters": True}
    monkeypatch.setattr(native.isolated, "resource_check", resources)
    monkeypatch.setattr(native.isolated, "record", lambda path, value, **kw: records.update({path.name: deepcopy(value)}))
    value = native.inner_execute(tmp_path, plan)
    assert actions == ["resources"]
    assert value["bootstrap_result"] is value["daemon_stop"] is value["drain_observation"] is None
    assert value["status"] == "failed_or_evidence_insufficient"
    assert value["error_sha256"] == native.hashlib.sha256(b"SECRET_PRIMARY").hexdigest()
    assert value["command_failure"]["sequence"] == 11
    assert ("RESOURCES_FINAL.json" in records) is not resource_failure
    assert (value["resource_diagnostic_failure"] is not None) is resource_failure
    assert "SECRET" not in json.dumps(value) and records["INNER_RESULT.json"] == value


def test_bootstrap_export_also_stays_inside_original_hard_deadline(tmp_path, monkeypatch):
    from contextlib import contextmanager
    from scripts import production_gateway_switch as switch
    path = tmp_path / "inner/bootstrap"
    path.mkdir(parents=True)
    (path / "RESULT.json").write_text("{}")
    active = []
    @contextmanager
    def bounded(seconds):
        assert seconds == 5
        active.append(True)
        yield
        active.pop()
    monkeypatch.setattr(native.time, "monotonic", lambda: 275.0)
    monkeypatch.setattr(switch, "_hard_deadline", bounded)
    monkeypatch.setattr(native.early, "export", lambda *a: {"status": "not_applicable_bootstrap_started"})
    def export(path):
        assert active == [True]
        return {"complete": True, "files": {}}
    monkeypatch.setattr(native.bootstrap, "safe_export", export)
    packet = {"status": "passed", "private_host": {"result": {"bootstrap_result": {"status": "passed"}}}}
    native.export_diagnostics(packet, tmp_path, 280.0)
    assert packet["status"] == "passed" and packet["safe_evidence"]["complete"]


def test_final_packet_budget_includes_later_bootstrap_export(tmp_path, monkeypatch, capsys):
    root = tmp_path / "first"
    result = {"status": "passed", "bootstrap_result": {"status": "passed"}, "padding": ""}
    safe = {"complete": True, "files": {"retained": "s" * (200 * 1024)}}
    def directory(path, **kwargs):
        leaf = path / "inner/bootstrap"
        leaf.mkdir(parents=True)
        (leaf / "RESULT.json").write_text("{}")
    monkeypatch.setattr(native, "arguments", lambda argv: SimpleNamespace(output=root, inside=False))
    monkeypatch.setattr(native.host, "private_directory", directory)
    monkeypatch.setattr(native, "outer_execute", lambda *args: deepcopy(result))
    monkeypatch.setattr(native.time, "monotonic", lambda: 100)
    monkeypatch.setattr(native.early, "export", lambda *args: {"status": "diagnostic_only", "fixed": "d" * 15000})
    monkeypatch.setattr(native.bootstrap, "safe_export", lambda *args: deepcopy(safe))
    assert native.main([]) == 0
    first = capsys.readouterr().out
    result["padding"] = "p" * (512 * 1024 - len(first.encode()) + 1000)
    root = tmp_path / "second"
    assert native.main([]) == 0  # Optional diagnostics do not change the independent operational result.
    raw = capsys.readouterr().out
    packet = json.loads(raw)
    assert len(raw.encode()) <= 512 * 1024
    assert packet["early_diagnostics"]["reason_code"] == "outer_packet_budget_exhausted"
    assert packet["early_diagnostics"]["status"] == "evidence_insufficient"
    assert packet["private_host"]["result"] == result and packet["safe_evidence"] == safe


def test_still_oversized_packet_is_refused_without_printing_success(tmp_path, monkeypatch, capsys):
    root = tmp_path / "oversize"
    result = {"status": "passed", "padding": "p" * (512 * 1024)}
    monkeypatch.setattr(native, "arguments", lambda argv: SimpleNamespace(output=root, inside=False))
    monkeypatch.setattr(native.host, "private_directory", lambda path, **kw: path.mkdir())
    monkeypatch.setattr(native, "outer_execute", lambda *args: deepcopy(result))
    monkeypatch.setattr(native.time, "monotonic", lambda: 100)
    monkeypatch.setattr(native.early, "export", lambda *args: {"status": "not_applicable_bootstrap_started"})
    with pytest.raises(gate.AdmissionError, match="host131 output budget exhausted"):
        native.main([])
    assert capsys.readouterr().out == "" and result["padding"] == "p" * (512 * 1024)


def test_caught_early_deadline_does_not_allow_subsequent_bootstrap_stat(tmp_path, monkeypatch):
    now = [275.0]
    packet = {"status": "unconfirmed", "private_host": {"result": None, "failure": {"error_type": "ProofError"}},
              "safe_evidence": {"complete": False, "files": {}}}
    monkeypatch.setattr(native.time, "monotonic", lambda: now[0])
    def export(*args):
        now[0] = 280.0
        return {"status": "evidence_insufficient", "reason_code": "original_deadline_exhausted"}
    monkeypatch.setattr(native.early, "export", export)
    monkeypatch.setattr(Path, "exists", lambda *args: pytest.fail("deadline exhausted before next stat"))
    native.export_diagnostics(packet, tmp_path, 280.0)
    assert packet["status"] == "unconfirmed" and packet["safe_evidence"]["complete"] is False
    assert packet["private_host"]["failure"] == {"error_type": "ProofError"}
