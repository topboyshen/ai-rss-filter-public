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
