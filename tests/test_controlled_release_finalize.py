"""Synthetic capability transports; actual local flock, journal and fsync paths."""
from copy import deepcopy
import fcntl
import json
import os
from pathlib import Path
import sys
from types import SimpleNamespace

import pytest

from scripts import controlled_release_finalize as final
from services import admission_fence as gate


def put(path, value):
    path.write_bytes(value)
    path.chmod(0o600)


@pytest.fixture
def case(tmp_path, monkeypatch):
    root = tmp_path.resolve()
    registry, evidence, control, lifecycle = [root / name for name in ("registry", "finalize", "control", "lifecycle")]
    for directory in (registry, evidence, control, lifecycle, registry / "build-contracts"):
        directory.mkdir(mode=0o700)
    put(registry / "release.lock", b"")
    lock = os.open(registry / "release.lock", os.O_RDWR)
    fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
    info = os.fstat(lock)
    plan = {"schema": 1, "source_sha": "a" * 40, "image_ref": "my-ai-filter:synthetic-final",
            "lifecycle_root": str(lifecycle), "lifecycle_operation_id": "1" * 64,
            "sender_closed_operation_id": "2" * 64, "sender_generation": 0,
            "finalize_operation_id": "3" * 64, "registry_root": str(registry)}
    source = {"revision": plan["source_sha"], "tree": "b" * 40, "image_id": "sha256:" + "c" * 64,
              "private_contract_sha256": "d" * 64, "manifest_sha256": "e" * 64}
    contract = {"source_sha": source["revision"], "source_tree": source["tree"],
                "files": {"compose.json": "e" * 64}, "example_config_sha256": "f" * 64,
                "review": {"health_services": ["candidate-web", "candidate-worker"]}}
    proof = {"operation_id": "4" * 64, "descriptor_sha256": "5" * 64,
             "manifest_sha256": "6" * 64, "binding_sha256": "7" * 64, "generation": 1,
             "slots_sha256": "8" * 64, "window": {"not_before": 1, "expires_at": 2}}
    pg = {"synthetic_pg_identity": "9" * 64}
    permit = {"source": source, "config": {"private_contract_path": str(root / "contract.json")},
              "locks": {"release": {"path": str(registry / "release.lock"), "device": info.st_dev, "inode": info.st_ino}},
              "sender": {"expected_identity": pg, "manifest": "b" * 64, "closed_operation_id": "2" * 64}}
    receiver = object.__new__(final.activation.ReceiverActivationSession)
    receiver.permit, receiver.shadow = permit, SimpleNamespace(fds=[("release", lock)])
    driver = object.__new__(final.sender.SenderActivationDriver)
    driver.control = SimpleNamespace(root=control, binding_sha256="c" * 64)
    driver.plan = {"postgres_identity": pg, "binding": {"manifest": "b" * 64}}
    intent = {"operation_id": "d" * 64, "closed_operation_id": "2" * 64,
              "closed_generation": 0, "receiver_activation_proof": proof}
    gate._write(control / "production_resume.intent.json", intent)
    old = ["my-ai-filter:synthetic-old", "sha256:" + "a" * 64, "b" * 64, "old-web old-worker"]
    gate._write(lifecycle / "create.intent.json", {"previous_containers": {
        "3" * 64: {"image": old[1]}, "4" * 64: {"image": old[1]}}})
    for field, value in zip(final.FIELDS, old):
        put(registry / ("current." + field), (value + "\n").encode())
    put(registry / "current.state", ("\n".join(old) + "\n").encode())
    put(registry / "history.tsv", b"original\thistorical\tbytes\n")
    build = [plan["image_ref"], source["image_id"], "e" * 64, "f" * 64, plan["source_sha"] + ":clean",
             "event-mapping-store-v5-provider-attribution-v1", "d" * 64]
    build_path = registry / "build-contracts" / (final._sha(plan["image_ref"].encode()) + ".state")
    put(build_path, ("\n".join(build) + "\n").encode())
    calls, state = [], {"expired": False, "healthy": True, "owner": "same"}
    def identity(_self, *, active=False):
        final.activation._owned_lock(str(registry / "release.lock"), lock)
        gate._require(not active or not state["expired"], "synthetic permission expired")
    monkeypatch.setattr(final.activation.ReceiverActivationSession, "_identity", identity)
    monkeypatch.setattr(final.activation.ReceiverActivationSession, "check_sender_resume",
                        lambda _self, **kw: deepcopy(proof))
    monkeypatch.setattr(final.activation.ReceiverActivationSession, "verification_proof",
                        lambda _self, **kw: {**deepcopy(proof), "source": source, "owner": state["owner"]})
    def query(_self, operation, cap):
        assert cap is receiver and operation == intent["operation_id"]
        calls.append(("GET", operation))
        return {"operation_id": operation, "pg_receipt": {"state": "open", "active_leases": len(calls)}}
    monkeypatch.setattr(final.sender.SenderActivationDriver, "query_production_resume", query)
    monkeypatch.setattr(final.private, "load", lambda *a, **kw: (deepcopy(contract), {}, "d" * 64))
    monkeypatch.setattr(final.lifecycle, "verify_active", lambda *a, **kw: {
        "operation_id": plan["lifecycle_operation_id"], "containers": {"web": "1" * 64, "worker": "2" * 64},
        "retained_previous_ids": ["3" * 64, "4" * 64]})
    def check(args, env, inherited, *, evidence_root=None):
        final.activation._owned_lock(str(registry / "release.lock"), inherited)
        assert env["RELEASE_AUTO_ROLLBACK"] == "0" and env["RELEASE_PRIVATE_CONTRACT"] == permit["config"]["private_contract_path"]
        assert args[2] == "controlled-check" and "up" not in args and "start" not in args
        calls.append(("CHECK", args[5], args[-1]))
        gate._require(state["healthy"], "synthetic formal health failed")
        return {"exit_code": 0, "output_sha256": "f" * 64, "bytes": 1}
    monkeypatch.setattr(final, "_check_process", check)
    try:
        yield SimpleNamespace(receiver=receiver, sender=driver, plan=plan, root=evidence, registry=registry,
            lock=lock, calls=calls, state=state, old=old, build=build_path, contract=contract, permit=permit, check=check)
    finally:
        os.close(lock)


def execute(p):
    return final.finalize(p.receiver, p.sender, p.plan, p.root)


def recover(p):
    return final.query(p.receiver, p.sender, p.plan, p.root)


def test_register_once_and_query_never_reposts_or_appends(case):
    p = case
    result = execute(p)
    assert result["release_registered"] and result["durable"]
    assert final._state(p.registry, "previous")[0] == p.old
    assert final._state(p.registry, "current")[0][0] == p.plan["image_ref"]
    history = (p.registry / "history.tsv").read_bytes()
    assert history.startswith(b"original\thistorical\tbytes\n") and history.count(b"controlled-finalize") == 1
    calls = list(p.calls)
    assert recover(p)["query_only"] and p.calls == calls
    assert (p.registry / "history.tsv").read_bytes() == history
    assert not (p.registry / final.MARKER).exists()
    with pytest.raises(gate.AdmissionError, match="query original"):
        execute(p)


@pytest.mark.parametrize("point", [0, 4, 8, 10])
def test_partial_registry_commit_queries_same_transition_without_duplicate_history(case, monkeypatch, point):
    p, replace = case, final._replace
    count = [0]
    def fail(f, row):
        replace(f, row)
        number, count[0] = count[0], count[0] + 1
        if number == point:
            raise OSError("synthetic commit ACK lost")
    monkeypatch.setattr(final, "_replace", fail)
    with pytest.raises(OSError, match="ACK lost"):
        execute(p)
    assert (p.registry / final.MARKER).exists() and (p.root / "intent.json").exists()
    monkeypatch.setattr(final, "_replace", replace)
    p.state["expired"] = True
    assert recover(p)["release_registered"]
    assert (p.registry / "history.tsv").read_bytes().count(b"controlled-finalize") == 1
    assert all(call[0] != "POST" for call in p.calls)


def test_result_rename_sync_failure_requires_real_confirmation_and_no_new_business(case, monkeypatch):
    p, sync = case, gate._sync_directory
    def fail(directory):
        if directory == p.root and (p.root / "result.json").exists():
            raise OSError("synthetic final fsync failure")
        sync(directory)
    monkeypatch.setattr(gate, "_sync_directory", fail)
    with pytest.raises(OSError, match="fsync failure"):
        execute(p)
    original = (p.root / "result.json").read_bytes()
    calls = list(p.calls)
    with pytest.raises(OSError, match="fsync failure"):
        recover(p)
    monkeypatch.setattr(gate, "_sync_directory", sync)
    assert recover(p)["release_registered"] and p.calls == calls
    assert (p.root / "result.json").read_bytes() == original


@pytest.mark.parametrize("reason", ["health", "build", "source", "pg", "manifest", "closed", "override", "mirror", "old-image"])
def test_required_gate_rejects_before_registry_intent(case, reason):
    p = case
    before = (p.registry / "current.state").read_bytes()
    if reason == "health":
        p.state["healthy"] = False
    elif reason == "build":
        p.build.unlink()
    elif reason == "source":
        p.plan["source_sha"] = "7" * 40
    elif reason == "pg":
        p.sender.plan["postgres_identity"] = {"wrong": "pg"}
    elif reason == "manifest":
        p.sender.plan["binding"]["manifest"] = "9" * 64
    elif reason == "closed":
        p.plan["sender_closed_operation_id"] = "9" * 64
    elif reason == "override":
        put(p.registry / "current.override", b"forced")
    elif reason == "old-image":
        path = Path(p.plan["lifecycle_root"]) / "create.intent.json"
        value = gate._read(path)
        value["previous_containers"]["3" * 64]["image"] = "sha256:" + "b" * 64
        gate._write(path, value)
    else:
        put(p.registry / "current.ref", b"different\n")
    with pytest.raises((gate.AdmissionError, FileNotFoundError)):
        execute(p)
    assert not (p.root / "intent.json").exists() and not (p.registry / final.MARKER).exists()
    assert (p.registry / "current.state").read_bytes() == before


def test_health_wait_allows_dynamic_counters_but_rejects_owner_change(case, monkeypatch):
    p = case
    def change(*a, **kw):
        result = p.check(*a, **kw)
        p.state["owner"] = "replaced"
        return result
    monkeypatch.setattr(final, "_check_process", change)
    with pytest.raises(gate.AdmissionError, match="owners changed"):
        execute(p)
    assert not (p.root / "intent.json").exists()


def test_ready_dict_and_unowned_release_descriptor_rejected(case):
    p = case
    with pytest.raises(gate.AdmissionError, match="real activation"):
        final.finalize({"ready": True}, p.sender, p.plan, p.root)
    fcntl.flock(p.lock, fcntl.LOCK_UN)
    with pytest.raises(gate.AdmissionError, match="no longer held"):
        execute(p)
    assert not (p.root / "intent.json").exists()


@pytest.mark.parametrize("kind", ["symlink", "hardlink", "oversize"])
def test_unsafe_or_full_registry_preserved(case, kind):
    p = case
    history = p.registry / "history.tsv"
    if kind == "symlink":
        history.rename(p.registry / "original-history")
        history.symlink_to("original-history")
    elif kind == "hardlink":
        os.link(history, p.registry / "original-history")
    else:
        put(history, b"x" * final.MAX_FILE_BYTES + b"\n")
    with pytest.raises((gate.AdmissionError, OSError)):
        execute(p)
    assert not (p.registry / final.MARKER).exists()


def test_external_change_after_partial_commit_stops_recovery(case, monkeypatch):
    p, replace = case, final._replace
    def fail(f, row):
        if row["name"] == "current.ref":
            raise OSError("synthetic crash")
        replace(f, row)
    monkeypatch.setattr(final, "_replace", fail)
    with pytest.raises(OSError):
        execute(p)
    monkeypatch.setattr(final, "_replace", replace)
    put(p.registry / "current.ref", b"foreign\n")
    with pytest.raises(gate.AdmissionError, match="outside original"):
        recover(p)
    assert (p.registry / final.MARKER).exists() and not (p.root / "result.json").exists()


def test_contract_preflight_has_no_health_and_no_registration(case):
    p = case
    value = final.check_contract(p.permit, p.plan, p.lock)
    assert not value["health_checked"] and not value["release_registered"]
    assert p.calls == [("CHECK", "fresh", "preflight")]
    assert not list(p.root.iterdir())


@pytest.mark.parametrize("variable,value", [("RELEASE_HEALTH_TIMEOUT_SECONDS", "181"),
    ("RELEASE_HEALTH_TIMEOUT_SECONDS", "1+2"), ("RELEASE_HEALTH_POLL_SECONDS", "0"),
    ("RELEASE_HEALTH_POLL_SECONDS", "3")])
def test_health_environment_cannot_enlarge_bounds_or_inject_arithmetic(case, monkeypatch, variable, value):
    monkeypatch.setenv(variable, value)
    with pytest.raises(gate.AdmissionError, match="health bounds"):
        final.check_contract(case.permit, case.plan, case.lock)
    assert not case.calls


def test_historical_registration_query_rejects_altered_result(case):
    execute(case)
    value = gate._read(case.root / "result.json")
    value["target"][0] = "wrong:image"
    gate._write(case.root / "result.json", value)
    with pytest.raises(gate.AdmissionError, match="receipt differs"):
        recover(case)


def test_retained_evidence_corruption_cannot_inject_registry_filename(case, monkeypatch):
    p, complete = case, final._complete
    monkeypatch.setattr(final, "_complete", lambda *_: (_ for _ in ()).throw(OSError("synthetic stop")))
    with pytest.raises(OSError):
        execute(p)
    original = gate._read(p.root / "intent.json")
    original["entries"][0]["name"] = "../external"
    gate._write(p.root / "intent.json", original)
    monkeypatch.setattr(final, "_complete", complete)
    with pytest.raises(gate.AdmissionError, match="transition inventory"):
        recover(p)
    assert not (p.registry.parent / "external").exists()


def test_cli_never_accepts_caller_ready_document(tmp_path):
    request = tmp_path / "fake.json"
    put(request, json.dumps({"ready": True}).encode())
    import subprocess
    env = {**os.environ, "PATH": str(Path(sys.executable).parent) + os.pathsep + os.environ["PATH"]}
    result = subprocess.run(["bash", str(final.ROOT / "scripts/release.sh"), "controlled-finalize",
                             "image:immutable", str(request)], capture_output=True, timeout=10, env=env)
    assert result.returncode != 0 and b"exact controlled finalize CLI request" in result.stderr
