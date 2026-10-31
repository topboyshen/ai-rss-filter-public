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
