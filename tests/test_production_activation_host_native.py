"""Local negative contracts; these mocks never stand in for a native proof."""
from copy import deepcopy
from contextlib import nullcontext
import hashlib
import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from scripts import production_activation_host_native as native
from scripts import verify_production_activation_native as activation
from scripts import verify_production_startup_native as startup
from services import admission_fence as gate


@pytest.fixture(autouse=True)
def reset_local_phase_counter(monkeypatch):
    monkeypatch.setattr(native, "PHASE", "initial_validation")
    monkeypatch.setattr(native, "PHASE_SEQUENCE", 0)


@pytest.mark.parametrize("deadline", [True, False, None, "600", float("nan"), float("inf"), -1, 0, 701])
def test_original_deadline_rejects_invalid_or_replaced_values(monkeypatch, deadline):
    monkeypatch.setattr(native.time, "monotonic", lambda: 100)
    with pytest.raises(gate.AdmissionError):
        native.remaining(deadline)


def test_deadline_is_not_reset_between_phases(monkeypatch):
    clock = [100]
    monkeypatch.setattr(native.time, "monotonic", lambda: clock[0])
    assert native.remaining(600) == 500
    clock[0] = 599
    assert native.remaining(600) == 1
    clock[0] = 601
    with pytest.raises(gate.AdmissionError):
        native.remaining(600)


def test_outer_supervisor_caps_precede_download_export_and_other_heavy_work(tmp_path, monkeypatch):
    seen = []
    class Stop(Exception):
        pass
    monkeypatch.setattr(native, "cgroup_setup", lambda owner: seen.append("caps_installed") or "/owned")
    monkeypatch.setattr(native, "FullCommands", lambda *a: seen.append("commands_created") or object())
    monkeypatch.setattr(native, "phase", lambda *a: None)
    def first_heavy(*args):
        seen.append("first_heavy_operation")
        raise Stop
    monkeypatch.setattr(native.host, "host_tools", first_heavy)
    with pytest.raises(Stop):
        native.outer_plan(SimpleNamespace(), tmp_path, 100)
    assert seen == ["caps_installed", "commands_created", "first_heavy_operation"]


def network_proof():
    holder = {"netns": 17}
    proof = {"holder": holder, "bridge": "afa" + "a" * 10, "network_id": "f" * 64,
             "network_name": "synthetic", "owner": "a" * 32}
    row = {"Id": proof["network_id"], "Name": "synthetic", "Internal": False, "Driver": "bridge", "Scope": "local",
           "Labels": {native.LABEL: proof["owner"]}, "Options": {"com.docker.network.bridge.name": proof["bridge"]}}
    return proof, row


@pytest.mark.parametrize("field,value", [("Id", "b" * 64), ("Name", "other"), ("Internal", True), ("Internal", None),
    ("Driver", "host"), ("Scope", "swarm"), ("Labels", {}), ("Options", {})])
def test_private_network_still_requires_every_actual_boundary(tmp_path, monkeypatch, field, value):
    proof, row = network_proof()
    row[field] = value
    monkeypatch.setattr(native.host, "namespace_snapshot", lambda *a, **k: pytest.fail("must reject before observation"))
    with pytest.raises(gate.AdmissionError):
        native.validate_network(proof, row, proof["owner"], SimpleNamespace(root=tmp_path, sequence=1))


def test_private_network_records_unique_route_observation(tmp_path, monkeypatch):
    proof, row = network_proof()
    seen = []
    def snapshot(commands, **kwargs):
        seen.append(kwargs["evidence_path"])
        commands.sequence += 3
        return {"netns": 17}
    monkeypatch.setattr(native.host, "namespace_snapshot", snapshot)
    commands = SimpleNamespace(root=tmp_path, sequence=1)
    native.validate_network(proof, row, proof["owner"], commands)
    native.validate_network(proof, row, proof["owner"], commands)
    assert seen == [tmp_path / "NAMESPACE_CHECK_0001.json", tmp_path / "NAMESPACE_CHECK_0004.json"]


@pytest.mark.parametrize("candidate", [None, object(), SimpleNamespace(check_network=lambda *_: None)])
def test_ordinary_bridge_never_accepts_duck_typed_context(candidate):
    with pytest.raises(gate.AdmissionError):
        native.require_harness_network(SimpleNamespace(private_host_context=candidate), {})


def identity_setup(monkeypatch):
    parent = {"pid": 1, "ppid": 0, "starttime": 1, "netns": 10, "mntns": 11, "cgroup": "/owned/supervisor"}
    holder = {"pid": 2, "ppid": 1, "starttime": 2, "netns": 12, "mntns": 13, "cgroup": "/owned/supervisor"}
    daemon = {**holder, "pid": 3, "ppid": 2, "starttime": 3}
    plan = {"deadline": 700, "active_deadline": 680, "parent": parent, "cgroup": "/owned", "wrapper_source": {"sha256": "a" * 64}}
    actual = {1: deepcopy(parent), 2: deepcopy(holder), 3: deepcopy(daemon)}
    monkeypatch.setattr(native.time, "monotonic", lambda: 100)
    monkeypatch.setattr(native.os, "getpid", lambda: 2)
    monkeypatch.setattr(native.host, "proc_identity", lambda pid: actual[pid])
    monkeypatch.setattr(native.host, "file_proof", lambda *a, **k: {"sha256": "a" * 64})
    monkeypatch.setattr(native.host, "check_peer", lambda pid, expected: gate._require(actual[pid] == expected, "peer drift"))
    monkeypatch.setattr(native, "resource_check", lambda p: None)
    return plan, holder, daemon, actual


def test_private_host_binds_live_parent_holder_daemon_and_source(monkeypatch):
    plan, holder, daemon, _actual = identity_setup(monkeypatch)
    native.process_check(plan, holder, daemon)


@pytest.mark.parametrize("pid,key,value", [(1, "starttime", 7), (2, "netns", 10), (2, "mntns", 11),
    (2, "cgroup", "/other"), (3, "netns", 10), (3, "pid", 30)])
def test_private_host_identity_drift_never_authorizes_mutation(monkeypatch, pid, key, value):
    plan, holder, daemon, actual = identity_setup(monkeypatch)
    actual[pid][key] = value
    with pytest.raises(gate.AdmissionError):
        native.process_check(plan, holder, daemon)


def test_context_final_uses_same_total_deadline_not_new_budget(monkeypatch):
    plan, holder, daemon, _actual = identity_setup(monkeypatch)
    monkeypatch.setattr(native.time, "monotonic", lambda: 690)
    with pytest.raises(gate.AdmissionError):
        native.process_check(plan, holder, daemon)
    native.process_check(plan, holder, daemon, final=True)
    monkeypatch.setattr(native.time, "monotonic", lambda: 701)
    with pytest.raises(gate.AdmissionError):
        native.process_check(plan, holder, daemon, final=True)


@pytest.mark.parametrize("fault", [None, "memory", "pids", "owner", "image", "name", "cgroup", "host_netns", "host_mntns"])
def test_running_inventory_binds_exact_pg_caps_owner_image_name_and_placement(tmp_path, monkeypatch, fault):
    image = "sha256:" + "a" * 64
    row = {"Id": "b" * 64, "Name": "/owned-pg", "Image": image,
           "State": {"Pid": 123, "OOMKilled": False}, "Config": {"Labels": {native.LABEL: "c" * 32}},
           "HostConfig": {"Memory": 384 * 1024**2, "MemorySwap": 384 * 1024**2, "PidsLimit": 64, "NanoCpus": 10**9}}
    process = {"cgroup": "/owned/containers/postgres", "netns": 20, "mntns": 30}
    if fault == "memory":
        row["HostConfig"].update(Memory=768 * 1024**2, MemorySwap=768 * 1024**2)
    elif fault == "pids":
        row["HostConfig"]["PidsLimit"] = 256
    elif fault == "owner":
        row["Config"]["Labels"][native.LABEL] = "d" * 32
    elif fault == "image":
        row["Image"] = "sha256:" + "d" * 64
    elif fault == "name":
        row["Name"] = "/unowned-pg"
    elif fault == "cgroup":
        process["cgroup"] = "/owned/supervisor"
    elif fault == "host_netns":
        process["netns"] = 10
    elif fault == "host_mntns":
        process["mntns"] = 11
    context = native.PrivateHostContext(tmp_path, {"cgroup": "/owned", "images": {}, "references": [], "sender": {"Id": image}},
        SimpleNamespace(docker=lambda *a: "b" * 64, inspect=lambda *a: row), {"netns": 10, "mntns": 11}, {})
    monkeypatch.setattr(context, "check", lambda: None)
    monkeypatch.setattr(native.host, "proc_identity", lambda pid: process)
    harness = SimpleNamespace(owner="c" * 32, names={"pg": "owned-pg"}, images={"pg": image})
    if fault:
        with pytest.raises(gate.AdmissionError):
            context.check_running_inventory(harness)
    else:
        context.check_running_inventory(harness)


def test_frozen_proof_is_exclusive_and_read_only(tmp_path):
    path = tmp_path / "proof.json"
    proof = native.record(path, {"schema": 1}, sealed=True)
    assert native.original(path, proof["sha256"])[0] == {"schema": 1}
    assert path.stat().st_mode & 0o777 == 0o400
    with pytest.raises(FileExistsError):
        native.record(path, {"schema": 2}, sealed=True)
    with pytest.raises(gate.AdmissionError):
        native.original(path, "b" * 64)


def test_sealed_publication_is_invisible_until_complete_mode_and_single_link(tmp_path, monkeypatch):
    path = tmp_path / "BUILD_REQUEST.json"
    real = native.rename_noreplace
    seen = []
    def publish(source, target):
        assert not target.exists() and target == path
        info = source.stat()
        assert info.st_mode & 0o777 == 0o400 and info.st_nlink == 1
        assert json.loads(source.read_bytes()) == {"status": "complete"}
        seen.append("complete_sealed_inode_before_atomic_publish")
        real(source, target)
    monkeypatch.setattr(native, "rename_noreplace", publish)
    native.record(path, {"status": "complete"}, sealed=True)
    assert seen == ["complete_sealed_inode_before_atomic_publish"]
    assert path.stat().st_nlink == 1


def test_sealed_file_fsync_failure_leaves_private_original_not_visible_final(tmp_path, monkeypatch):
    path = tmp_path / "BUILD_RESULT.json"
    real, calls = native.os.fsync, []
    def sync(fd):
        calls.append(fd)
        if len(calls) == 3:
            raise OSError("sealed original fsync failed")
        return real(fd)
    monkeypatch.setattr(native.os, "fsync", sync)
    with pytest.raises(OSError):
        native.record(path, {"status": "complete"}, sealed=True)
    assert not path.exists()
    assert len(list(tmp_path.glob("BUILD_RESULT.json.sealed-prepared-*"))) == 1


@pytest.mark.parametrize("mutation", ["extra", "source", "tag", "sha"])
def test_build_ipc_rejects_unreviewed_request_before_build(tmp_path, monkeypatch, mutation):
    request = {"schema": 1, "contract_sha256": "a" * 64, "source": {"source_sha": "b" * 40},
               "image": "ai-filter-activation-native:fixed-project"}
    plan = {"repo": str(tmp_path / "repo"), "source_identity": deepcopy(request["source"])}
    contract = {"review": {"project": "fixed-project"}}
    if mutation == "extra":
        request["argv"] = ["arbitrary"]
    if mutation == "source":
        request["source"] = {"source_sha": "c" * 40}
    if mutation == "tag":
        request["image"] = "my-ai-filter:.126"
    if mutation == "sha":
        request["contract_sha256"] = "d" * 64
    monkeypatch.setattr(native, "original", lambda path: (request, {"sha256": "e" * 64}))
    monkeypatch.setattr(native.private, "load", lambda path, root, **kwargs: (contract, {}, "a" * 64))
    with pytest.raises(gate.AdmissionError):
        native.checked_build_request(tmp_path, plan)


def test_exact_build_ipc_reads_only_fixed_contract(tmp_path, monkeypatch):
    request = {"schema": 1, "contract_sha256": "a" * 64, "source": {"source_sha": "b" * 40},
               "image": "ai-filter-activation-native:fixed-project"}
    monkeypatch.setattr(native, "original", lambda path: (request, {"sha256": "e" * 64}))
    seen = []
    def load(path, repo, **kwargs):
        seen.append((path, repo, kwargs))
        return {"review": {"project": "fixed-project"}}, {}, "a" * 64
    monkeypatch.setattr(native.private, "load", load)
    native.checked_build_request(tmp_path, {"repo": str(tmp_path), "source_identity": request["source"]})
    assert seen == [(tmp_path / "inner/activation/receiver/sealed/contract.json", tmp_path, {"require_fresh": True})]


@pytest.mark.parametrize("state", ["uncertain", "running"])
def test_unknown_or_running_resources_never_authorize_daemon_signal(monkeypatch, state):
    context = SimpleNamespace(check=lambda **kw: None, plan={"deadline": 600},
        commands=SimpleNamespace(deadline=580, docker=lambda args: "a" * 64 if state == "running" else "", uncertain_mutation=state == "uncertain"))
    process = SimpleNamespace(send_signal=lambda *_: pytest.fail("must retain unknown resources"))
    with pytest.raises(gate.AdmissionError):
        native.stop_daemon(context, process, None)


def test_transfer_write_failure_preserves_original_and_sticky_unknown(tmp_path, monkeypatch):
    commands = native.FullCommands(tmp_path, 600)
    monkeypatch.setattr(native.time, "monotonic", lambda: 100)
    class Failure(Exception):
        pass
    original = Failure("original capture failure")
    process = SimpleNamespace(poll=lambda: None, terminate=lambda: None)
    monkeypatch.setattr(native.subprocess, "Popen", lambda *a, **k: process)
    monkeypatch.setattr(commands, "_capture", lambda *a, **k: (_ for _ in ()).throw(original))
    monkeypatch.setattr(native, "record", lambda *a, **k: (_ for _ in ()).throw(OSError("evidence full")))
    with pytest.raises(Failure) as caught:
        commands.docker(["image", "save", "sha256:" + "a" * 64], transfer=True, output_path=tmp_path / "image.tar")
    assert caught.value is original and commands.uncertain_mutation
    assert any("evidence failed" in note for note in caught.value.__notes__)


def test_transfer_client_term_race_preserves_capture_failure_and_receipt(tmp_path, monkeypatch):
    commands = native.FullCommands(tmp_path, 600)
    monkeypatch.setattr(native.time, "monotonic", lambda: 100)
    original = RuntimeError("original capture failure")
    process = SimpleNamespace(poll=lambda: None, terminate=lambda: (_ for _ in ()).throw(ProcessLookupError()))
    monkeypatch.setattr(native.subprocess, "Popen", lambda *a, **k: process)
    monkeypatch.setattr(commands, "_capture", lambda *a, **k: (_ for _ in ()).throw(original))
    with pytest.raises(RuntimeError) as caught:
        commands.docker(["image", "save", "sha256:" + "a" * 64], transfer=True, output_path=tmp_path / "image.tar")
    assert caught.value is original and commands.uncertain_mutation
    assert native.host.read_original_json(tmp_path / "command-0001.json")["client_term_error_type"] == "ProcessLookupError"


@pytest.mark.parametrize("failure_point", ["launch_registered_before_return", "stdout", "fsync"])
def test_outermost_guard_holds_original_daemon_fds_on_every_failure(monkeypatch, failure_point):
    class Held(Exception):
        pass
    calls = []
    monkeypatch.setattr(native.host, "LIVE_DAEMONS", [(SimpleNamespace(poll=lambda: None), None, None, None)])
    monkeypatch.setattr(native, "_main", lambda argv: (_ for _ in ()).throw(OSError(failure_point)))
    def paused():
        calls.append("original_fd_holder_preserved")
        raise Held
    monkeypatch.setattr(native.signal, "pause", paused)
    with pytest.raises(Held):
        native.main([])
    assert calls == ["original_fd_holder_preserved"]


def test_safe_profile_never_forwards_unrecognized_text():
    evil = "secret-password-DO-NOT-FORWARD"
    value = {"status": evil, "source_sha": evil, "last_stage": evil, "Env": [evil], "failure": {"type": evil, "message_sha256": evil},
             "evidence_errors": [evil], "elapsed_seconds": evil}
    serialized = json.dumps(native.safe_activation(value))
    assert evil not in serialized and "Env" not in serialized
    assert native.safe_activation(value)["status"] == "unrecognized"


def test_actual_stage_allowlist_matches_frozen_source_not_arbitrary_text():
    import ast
    tree = ast.parse(Path(activation.__file__).read_text())
    actual = {node.args[0].value for node in ast.walk(tree) if isinstance(node, ast.Call)
              and isinstance(node.func, ast.Attribute) and node.func.attr == "stage"
              and node.args and isinstance(node.args[0], ast.Constant)}
    assert actual == native.ACTIVATION_STAGES
    stage = "actual_fixed_gateway_switch_while_closed"
    assert native.safe_activation({"last_stage": stage})["last_stage"] == stage


def test_unknown_error_class_is_not_forwarded():
    evil = type("SecretPasswordMustNotAppear", (Exception,), {})
    assert native.error_type(evil()) == "unrecognized_exception"


def test_failed_inventory_keeps_minimal_phase_without_exception_text(tmp_path, monkeypatch, capsys):
    root = tmp_path / "private-originals"
    monkeypatch.setattr(native, "_arguments", lambda argv: SimpleNamespace(output=root, inside=False))
    monkeypatch.setattr(native.host, "private_directory", lambda path, **kwargs: path.mkdir(mode=0o700))
    secret = "DO-NOT-EXPORT-PRIVATE-PATH-OR-TOKEN"
    def fail(*args):
        native.PHASE = "candidate_build"
        raise RuntimeError(secret)
    monkeypatch.setattr(native, "outer_execute", fail)
    monkeypatch.setattr(native, "safe_inventory", lambda *args: (_ for _ in ()).throw(OSError(secret)))
    assert native._main([]) == 1
    text = capsys.readouterr().out
    value = json.loads(text)
    assert secret not in text and value["phase"] == "candidate_build"
    assert value["safe_evidence"] is None and value["full_activation_verified"] is False


def test_inventory_never_follows_link_or_claims_full_archive(tmp_path, monkeypatch):
    target = tmp_path / "private-target"
    target.mkdir()
    (target / "never-read").write_text("secret")
    root = tmp_path / "evidence"
    root.mkdir(mode=0o700)
    (root / "external").symlink_to(target, target_is_directory=True)
    native.record(root / "ORIGINAL.json", {"status": "failed"})
    monkeypatch.setattr(native.time, "monotonic", lambda: 100)
    result = native.safe_inventory(root, 600)
    assert result["full_raw_archive_verified"] is False and result["complete"] is False
    assert len(result["originals"]) == 1 and len(result["omissions"]) == 1
    assert hashlib.sha256(b"external").hexdigest() == result["omissions"][0]["path_sha256"]


def test_inventory_budget_exhaustion_stops_before_hashing(tmp_path, monkeypatch):
    (tmp_path / "one").write_text("one")
    monkeypatch.setattr(native.time, "monotonic", lambda: 601)
    monkeypatch.setattr(native.host, "file_proof", lambda *a, **k: pytest.fail("cannot hash after original deadline"))
    with pytest.raises(gate.AdmissionError):
        native.safe_inventory(tmp_path, 600)


def test_inventory_one_directory_cannot_exceed_entry_count(tmp_path, monkeypatch):
    for number in range(4097):
        (tmp_path / str(number)).touch(mode=0o600)
    monkeypatch.setattr(native.time, "monotonic", lambda: 100)
    with pytest.raises(gate.AdmissionError, match="inventory count bound"):
        native.safe_inventory(tmp_path, 600)


def test_full_activation_final_command_is_clamped_and_timeout_is_sticky(tmp_path, monkeypatch):
    context = SimpleNamespace(check=lambda **kw: None, plan={"deadline": 600, "active_deadline": 580},
                              commands=SimpleNamespace(uncertain_mutation=False))
    harness = object.__new__(activation.ActivationHarness)
    harness.private_host_context, harness.output, harness.sequence = context, tmp_path, 0
    seen = []
    monkeypatch.setattr(native.time, "monotonic", lambda: 599)
    def command(self, args, **kwargs):
        seen.append(kwargs["timeout"])
        self.sequence += 1
        gate._write(self.output / f"command-{self.sequence:04d}.json", {"rc": None})
        return ""
    monkeypatch.setattr(activation.Harness, "command", command)
    assert harness.command(["stop", "owned"], final=True, timeout=30) == ""
    assert seen == [1] and context.commands.uncertain_mutation


def test_harness_missing_durable_command_evidence_is_sticky_unknown(tmp_path, monkeypatch):
    context = SimpleNamespace(check=lambda **kw: None, plan={"deadline": 600, "active_deadline": 580},
                              commands=SimpleNamespace(uncertain_mutation=False))
    harness = object.__new__(activation.ActivationHarness)
    harness.private_host_context, harness.output, harness.sequence = context, tmp_path, 0
    monkeypatch.setattr(native.time, "monotonic", lambda: 100)
    monkeypatch.setattr(activation.Harness, "command", lambda *a, **k: "created-without-retained-ACK")
    with pytest.raises(Exception):
        harness.command(["create", "owned"])
    assert context.commands.uncertain_mutation


def test_failed_build_receipt_write_never_hides_original_failure(tmp_path, monkeypatch):
    request = {"source": {}, "image": "fixed", "contract_sha256": "a" * 64}
    plan = {"outer_peer": {}, "repo": str(tmp_path), "source_identity": {}, "active_deadline": 600}
    monkeypatch.setattr(native, "checked_build_request", lambda *a: (request, {"sha256": "b" * 64}, {}))
    monkeypatch.setattr(native, "peer_identity", lambda: {})
    monkeypatch.setattr(native.private, "source_identity", lambda *a: {})
    original = RuntimeError("original actual build failure")
    monkeypatch.setattr(native.startup, "build_candidate", lambda *a, **k: (_ for _ in ()).throw(original))
    def record(path, *args, **kwargs):
        if path.name == "BUILD_RESULT.json":
            raise OSError("receipt fsync failed")
    monkeypatch.setattr(native, "record", record)
    with pytest.raises(RuntimeError) as caught:
        native.build_requested_candidate(tmp_path, plan, None)
    assert caught.value is original
    assert any("receipt could not be persisted" in note for note in original.__notes__)


@pytest.mark.parametrize("point", ["receipt", "fsync"])
def test_outer_preservation_failure_does_not_hide_primary(tmp_path, monkeypatch, point):
    primary, attempted = RuntimeError("actual original guard failure"), []
    def record(*args, **kwargs):
        if point == "receipt":
            raise OSError("private receipt full")
    monkeypatch.setattr(native, "record", record)
    def fsync(fd):
        attempted.append(fd)
        if point == "fsync":
            raise OSError("private stream sync full")
    monkeypatch.setattr(native.os, "fsync", fsync)
    streams = [SimpleNamespace(flush=lambda: None, fileno=lambda: 10, close=lambda: None),
               SimpleNamespace(flush=lambda: None, fileno=lambda: 11, close=lambda: None)]
    native.preserve_outer_outputs(SimpleNamespace(poll=lambda: None, pid=123), tmp_path, {"deadline": 600}, streams, primary)
    assert attempted == [10, 11]
    assert any("outer child evidence failed" in note for note in primary.__notes__)


def test_outer_evidence_failure_is_not_silently_ignored_when_no_primary(tmp_path, monkeypatch):
    monkeypatch.setattr(native.os, "fsync", lambda fd: (_ for _ in ()).throw(OSError("full")))
    with pytest.raises(OSError):
        native.preserve_outer_outputs(None, tmp_path, {}, [SimpleNamespace(flush=lambda: None, fileno=lambda: 10, close=lambda: None)], None)


@pytest.mark.parametrize("point", ["direct_mutator_exception", "direct_mutator_failed_report", "launch_before_tuple_return"])
def test_failed_inner_paths_preserve_live_daemon_and_never_term_even_if_ps_empty(tmp_path, monkeypatch, point):
    from scripts import production_offline_registry as registry
    monkeypatch.setattr(native.os, "setsid", lambda: None)
    monkeypatch.setattr(native.time, "monotonic", lambda: 100)
    monkeypatch.setattr(native.host, "proc_identity", lambda pid: {"netns": 10})
    monkeypatch.setattr(native.host, "restrict_private_ipv6", lambda *a: None)
    monkeypatch.setattr(native, "prepare_mounts", lambda *a: None)
    monkeypatch.setattr(native, "resource_check", lambda *a: {})
    monkeypatch.setattr(native, "record", lambda *a, **k: {})
    monkeypatch.setattr(native, "load_dependencies", lambda *a: None)
    monkeypatch.setattr(native.PrivateHostContext, "check", lambda *a, **k: None)
    commands = SimpleNamespace(deadline=580, uncertain_mutation=False, docker=lambda *a: "")
    monkeypatch.setattr(native, "FullCommands", lambda *a: commands)
    monkeypatch.setattr(registry, "serve_bundle", lambda *a, **k: nullcontext(SimpleNamespace(url="http://127.0.0.1:3333")))
    process = SimpleNamespace(poll=lambda: None, send_signal=lambda *a: pytest.fail("unknown direct mutation forbids daemon TERM"))
    monkeypatch.setattr(native.host, "LIVE_DAEMONS", [])
    def launched(*args):
        native.host.LIVE_DAEMONS.append((process, None, tmp_path, {}))
        if point == "launch_before_tuple_return":
            raise RuntimeError("retainer setup failed after original FD registration")
        return process, {}, None
    monkeypatch.setattr(native, "start_daemon", launched)
    def execute(*args, **kwargs):
        if point == "direct_mutator_exception":
            raise OSError("direct private.run mutation outcome unknown")
        return {"status": "failed_or_evidence_insufficient"}
    monkeypatch.setattr(activation, "execute", execute)
    plan = {"active_deadline": 580, "deadline": 600, "bundle": {}, "parent": {}, "repo": str(tmp_path),
            "source": str(tmp_path), "binary": str(tmp_path), "sender": {"Id": "sha256:" + "a" * 64}}
    result = native.inner_execute(tmp_path, plan)
    assert result["status"] == "failed_or_evidence_insufficient" and result["retained_live_daemon"] is True
    assert result["daemon_stop"] is None
    if point != "launch_before_tuple_return":
        assert commands.uncertain_mutation is True


@pytest.mark.parametrize("deadline", [True, "600", float("inf"), float("nan"), 100])
def test_native_source_build_rejects_bad_deadline_before_any_archive(monkeypatch, deadline):
    monkeypatch.setattr(startup.time, "monotonic", lambda: 100)
    monkeypatch.setattr(startup, "_oneoff_public_source", lambda *a, **k: pytest.fail("must not start source read"))
    with pytest.raises(ValueError):
        startup.build_candidate(None, None, None, None, None, None, deadline=deadline)


def test_probe_each_command_uses_fresh_remaining_original_deadline(tmp_path, monkeypatch):
    clock, seen = [100], []
    monkeypatch.setattr(startup.time, "monotonic", lambda: clock[0])
    def capture(args, path, **kwargs):
        seen.append(kwargs["seconds"])
        Path(str(path) + ".stdout").write_bytes(b"{}")
        clock[0] += 50
    def probe(contract, directory, digest, runner):
        runner(["first"])
        runner(["second"])
    monkeypatch.setattr(startup, "archive_command", capture)
    monkeypatch.setattr(startup.private, "image_probe", probe)
    startup.probe_candidate({}, tmp_path, "image", tmp_path, deadline=200)
    assert seen == [100, 50]


def test_source_preparation_cannot_replay_elapsed_build_budget(tmp_path, monkeypatch):
    clock, seen = [100], []
    monkeypatch.setattr(startup.time, "monotonic", lambda: clock[0])
    def prepared(*args, **kwargs):
        assert kwargs["deadline"] == 200
        clock[0] = 201
        return tmp_path / "context"
    monkeypatch.setattr(startup, "_oneoff_public_source", prepared)
    monkeypatch.setattr(startup, "archive_command", lambda *a, **k: seen.append(k))
    source = {"source_sha": "a" * 40, "example_config_sha256": "b" * 64}
    contract = {"files": {"compose.json": "c" * 64, "runtime.yaml": "d" * 64}}
    with pytest.raises(ValueError, match="deadline expired"):
        startup.build_candidate(tmp_path, tmp_path, source, "fixed", contract, "e" * 64, deadline=200)
    assert seen == []
