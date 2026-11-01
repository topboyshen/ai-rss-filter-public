"""Local control-logic tests only, never an actual private-daemon execution."""
from copy import deepcopy
from pathlib import Path
from types import SimpleNamespace

import pytest

from scripts import production_sender_bootstrap_host_native as native
from scripts import sender_host_loopback_pg as pg
from services import admission_fence as gate


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
