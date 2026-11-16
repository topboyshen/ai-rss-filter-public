"""Real private files/flocks/fence accounting; sender and host observations are doubles."""
from copy import deepcopy
from pathlib import Path
import sys
from types import SimpleNamespace

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
from production_sender_driver import SenderActivationDriver  # noqa: E402
from services import admission_fence as gate, production_activation as runtime
from services import production_admission_lifecycle as life
from services.production_activation_control import ReceiverActivationSession
from services import production_startup as startup, webhook_receipts


def operation(number):
    return f"{number:064x}"


@pytest.fixture
def case(tmp_path, monkeypatch):
    clock = {"wall": 1800000000.0, "elapsed": 10000.0}
    monkeypatch.setattr(gate.time, "time", lambda: clock["wall"])
    monkeypatch.setattr(gate, "_elapsed", lambda: clock["elapsed"])
    monkeypatch.setattr(gate, "_boot_identity", lambda: "linux:" + "a" * 36)
    root, spool = tmp_path / "grants", tmp_path / "spool"
    root.mkdir(mode=0o700)
    spool.mkdir(mode=0o700)
    (spool / ".lock").touch(mode=0o600)
    store = webhook_receipts.ReceiptStore(spool)
    store._directories(create=True)
    binding = {"revision": "a" * 40, "image_id": "sha256:" + "b" * 64, "strategy": "synthetic",
               "source_sha256": gate.source_sha256(), "deployment_nonce": "c" * 32}
    fence_root = tmp_path / "fence"
    gate.provision(fence_root, binding, {"not_before": int(clock["wall"])-1, "expires_at": int(clock["wall"])+600,
                                       "max_admissions": 3, "max_bytes": 300})
    fence = gate.Fence(fence_root, binding)
    fence.open_trial(operation(1), 0, "synthetic continuation protocol")
    descriptor = {"freeze": {"root": str(fence_root), "binding": binding}}
    gate._write(root / "descriptor.json", descriptor)
    proof = {"operation_id": operation(1), "descriptor_sha256": gate._hash(descriptor),
             "manifest_sha256": gate._hash(fence._manifest()), "binding_sha256": gate._hash(binding),
             "generation": 1, "slots_sha256": operation(2),
             "window": {"not_before": int(clock["wall"])-1, "expires_at": int(clock["wall"])+600}}
    session = ReceiverActivationSession.__new__(ReceiverActivationSession)
    session.root, session.live = root, True
    session.descriptor, session.operation_id = descriptor, operation(1)
    session.migration_root, session.state = tmp_path / "migration", tmp_path / "runtime-state"
    session.migration_root.mkdir(mode=0o700)
    session.state.mkdir(mode=0o700)
    session._identity = lambda **kw: gate._require(session.live, "synthetic session no longer held")
    session.check_sender_resume = lambda **kw: deepcopy(proof)
    info = spool.stat()
    session.permit = {"config": {"path": str(tmp_path / "config.yaml")}, "freeze": {"root": str(fence_root), "binding": binding},
                      "inventory": {"spool": str(spool), "identities": {"spool": {
                          "device": info.st_dev, "inode": info.st_ino, "uid": info.st_uid}}}}
    gate._write(session.migration_root / "permit.json", session.permit)
    sender = SenderActivationDriver.__new__(SenderActivationDriver)
    sender.plan_sha256 = operation(3)
    sender.control = SimpleNamespace(root=tmp_path / "sender")
    sender.control.root.mkdir(mode=0o700)
    receipt = {"operation_id": operation(4), "manifest": operation(5), "generation": 1,
               "state": "open", "action": "resume", "confirmed": True, "durable": True}
    gate._write(sender.control.root / "production_resume.confirmed.json", {"operation_id": operation(4), "pg_receipt": receipt})
    gate._write(sender.control.root / "production_resume.intent.json", {"operation_id": operation(4), "receiver_activation_proof": proof})
    sample = {"gate": {"current_operation": operation(4), "manifest": operation(5), "generation": 1, "phase": "open"},
              "database_identity": {"cluster": "synthetic-only"}, "rows": 0, "charged_bytes": 0,
              "retained_leases": {"records": 0, "uncertain": 0}, "retained_operations": 2,
              "legacy": {"rows": 0}, "states": {"held": 0}, "expired_unacknowledged": 0}
    sender.bind = lambda: deepcopy(sample)
    policy = {"schema": life.SCHEMA, "operation_id": operation(6), "approved_by": "synthetic reviewer",
              "not_before": int(clock["wall"])-1, "expires_at": int(clock["wall"])+86400,
              "lease_seconds": 3600, "renew_before_seconds": 600, "max_renewals": 4,
              "receiver_stop_records": 100, "receiver_stop_bytes": 64*1024**2,
              "sender_stop_records": 100, "sender_stop_bytes": 64*1024**2, "minimum_free_bytes": 64*1024**2}
    monkeypatch.setenv(startup.ENV_ROOT, str(root))
    monkeypatch.setenv(startup.ENV_OPERATION, operation(7))
    monkeypatch.setattr(runtime, "require_business_ready", lambda: None)
    return SimpleNamespace(session=session, sender=sender, policy=policy, fence=fence, root=root,
                           clock=clock, sample=sample, proof=proof, monkeypatch=monkeypatch)


def advance(p, seconds):
    p.clock["wall"] += seconds
    p.clock["elapsed"] += seconds


def first(p):
    life.authorize(p.session, p.sender, p.policy)
    return life.renew(p.session, p.sender, operation_id=operation(10), expected_sequence=-1)


def test_explicit_continuation_passes_original_time_but_never_resets_quota(case):
    p = case
    original = (p.fence.root / "manifest.json").read_bytes()
    first(p)
    lease = p.fence.reserve_lease("synthetic", charge_bytes=100)
    lease.finish()
    advance(p, 3300)
    renewed = life.renew(p.session, p.sender, operation_id=operation(11), expected_sequence=0)
    assert renewed["sequence"] == 1 and p.fence.snapshot()["generation"] == 1
    assert (p.fence.root / "manifest.json").read_bytes() == original
    for _ in range(2):
        lease = p.fence.reserve_lease("synthetic", charge_bytes=100)
        lease.finish()
    with pytest.raises(gate.AdmissionClosed, match="capacity"):
        p.fence.reserve_lease("synthetic")
    assert p.fence.snapshot()["intake_count"] == 3


@pytest.mark.parametrize("change", ["expired", "wall_backward", "elapsed_backward", "clock_divergence", "boot"])
def test_expiry_clock_and_boot_changes_refuse_intake_and_renewal(case, change):
    p = case
    first(p)
    if change == "expired":
        advance(p, 3601)
    elif change == "wall_backward":
        p.clock["wall"] -= 1
    elif change == "elapsed_backward":
        p.clock["elapsed"] -= 1
    elif change == "clock_divergence":
        p.clock["wall"] += 31
    else:
        p.monkeypatch.setattr(gate, "_boot_identity", lambda: "linux:" + "f" * 36)
    with pytest.raises(gate.AdmissionError):
        p.fence.reserve_lease("synthetic")
    with pytest.raises(gate.AdmissionError):
        life.renew(p.session, p.sender, operation_id=operation(11), expected_sequence=0)
    assert len(list((p.root / "continuation/leases").iterdir())) == 2


@pytest.mark.parametrize("kind", ["sender_gate", "sender_identity", "sender_plan", "receiver", "fake_capability"])
def test_changed_actual_binding_or_caller_flag_never_extends(case, kind):
    p = case
    first(p)
    advance(p, 3300)
    if kind == "sender_gate":
        p.sample["gate"]["current_operation"] = operation(99)
    elif kind == "sender_identity":
        p.sample["database_identity"]["cluster"] = "other"
    elif kind == "sender_plan":
        p.sender.plan_sha256 = operation(99)
    elif kind == "receiver":
        p.proof["generation"] = 99
    else:
        p.session = {"ready": True}
    with pytest.raises(gate.AdmissionError):
        life.renew(p.session, p.sender, operation_id=operation(11), expected_sequence=0)
    assert len(list((p.root / "continuation/leases").iterdir())) == 2


@pytest.mark.parametrize("kind", ["records", "bytes", "held", "legacy", "uncertain", "retained_operations", "retained_leases"])
def test_capacity_or_uncertainty_blocks_extension_without_cleanup(case, kind):
    p = case
    first(p)
    advance(p, 3300)
    if kind == "records":
        p.sample["rows"] = p.policy["sender_stop_records"]
    elif kind == "bytes":
        p.sample["charged_bytes"] = p.policy["sender_stop_bytes"]
    elif kind == "held":
        p.sample["states"]["held"] = 1
    elif kind == "legacy":
        p.sample["legacy"]["rows"] = 1
    elif kind == "uncertain":
        p.sample["retained_leases"]["uncertain"] = 1
    elif kind == "retained_operations":
        p.sample["retained_operations"] = 9999
    else:
        p.sample["retained_leases"]["records"] = 9998
    before = deepcopy(p.sample)
    with pytest.raises(gate.AdmissionError):
        life.renew(p.session, p.sender, operation_id=operation(11), expected_sequence=0)
    assert p.sample == before


def test_fsync_unknown_visible_current_can_query_after_expiry_without_new_lease(case):
    p = case
    life.authorize(p.session, p.sender, p.policy)
    sync = gate._sync_directory
    def fail_current(path):
        if Path(path) == p.root / "continuation" and (Path(path) / "current.json").exists():
            raise OSError("synthetic current directory sync failure")
        return sync(path)
    p.monkeypatch.setattr(gate, "_sync_directory", fail_current)
    with pytest.raises(OSError):
        life.renew(p.session, p.sender, operation_id=operation(10), expected_sequence=-1)
    p.monkeypatch.setattr(gate, "_sync_directory", sync)
    advance(p, 3601)
    result = life.query(p.session, p.sender, operation_id=operation(10))
    assert result["query_only"] and result["expired"] and not result["current_identity_or_readiness_proven"]
    with pytest.raises(gate.AdmissionError):
        p.fence.reserve_lease("synthetic")


def test_unpublished_partial_result_cannot_become_first_lease_after_original_window(case):
    p = case
    life.authorize(p.session, p.sender, p.policy)
    write = gate._write
    def fail_pointer(path, value):
        if Path(path).name == "current.json":
            raise OSError("synthetic lost publication")
        return write(path, value)
    p.monkeypatch.setattr(gate, "_write", fail_pointer)
    with pytest.raises(OSError):
        life.renew(p.session, p.sender, operation_id=operation(10), expected_sequence=-1)
    p.monkeypatch.setattr(gate, "_write", write)
    advance(p, 601)
    with pytest.raises(gate.AdmissionError):
        life.query(p.session, p.sender, operation_id=operation(10))
    assert not (p.root / "continuation/current.json").exists()


def test_stop_record_blocks_intake_and_cannot_reopen_from_renewal(case):
    p = case
    first(p)
    gate._write(p.root / "continuation/STOP.json", {"reason": "synthetic capacity boundary"})
    with pytest.raises(gate.AdmissionError, match="stopped"):
        p.fence.reserve_lease("synthetic")
    advance(p, 3300)
    with pytest.raises(gate.AdmissionError, match="stopped"):
        life.renew(p.session, p.sender, operation_id=operation(11), expected_sequence=0)


def test_renewal_prematurity_cas_and_finite_record_budget(case):
    p = case
    p.policy["max_renewals"] = 2
    first(p)
    with pytest.raises(gate.AdmissionError, match="premature"):
        life.renew(p.session, p.sender, operation_id=operation(11), expected_sequence=0)
    advance(p, 3300)
    with pytest.raises(gate.AdmissionError, match="CAS"):
        life.renew(p.session, p.sender, operation_id=operation(11), expected_sequence=-1)
    life.renew(p.session, p.sender, operation_id=operation(11), expected_sequence=0)
    advance(p, 3300)
    with pytest.raises(gate.AdmissionError, match="budget"):
        life.renew(p.session, p.sender, operation_id=operation(12), expected_sequence=1)
    assert len(list((p.root / "continuation/leases").iterdir())) == 4


@pytest.fixture
def staged(case, request):
    from test_production_sender_driver import setup
    # Use the existing full Docker/HTTP/PG protocol fixture, not a stop callback.
    existing, transport, _reader = setup.__wrapped__(request.getfixturevalue("tmp_path"))
    p = case
    sender = SenderActivationDriver.__new__(SenderActivationDriver)
    sender.plan, sender.plan_sha256 = existing.plan, existing.plan_sha256
    sender.root, sender.transport, sender.postgres = existing.root, existing.transport, existing.postgres
    sender.bind = existing.bind
    sender.control = SimpleNamespace(root=existing.root / "activation")
    sender.control.root.mkdir(mode=0o700)
    transport.action = "resume"
    receipt = transport.receipt()
    gate._write(sender.control.root / "production_resume.confirmed.json", {
        "operation_id": transport.current, "pg_receipt": receipt})
    gate._write(sender.control.root / "production_resume.intent.json", {
        "operation_id": transport.current, "receiver_activation_proof": p.proof})
    p.sender, p.transport = sender, transport
    return p


def test_actual_staged_sender_and_receiver_stop_new_keep_consumers_and_originals(staged):
    p = staged
    initialized = life.initialize(p.session, p.sender, p.policy)
    assert life.query(p.session, p.sender, operation_id=initialized["lease"]["operation_id"])["query_only"]
    value = life.stop_new(p.session, p.sender)
    assert value["status"] == "producers_stopped_drain_pending" and not value["drained"]
    assert p.transport.state == "draining" and p.fence.snapshot()["phase"] == "draining"
    assert [r[1] for r in p.transport.calls if r[0] == "POST"] == ["/stop-new"]
    lease = p.fence.reserve_lease("synthetic-durable-consumer", kind="drain")
    lease.finish()
    with pytest.raises(gate.AdmissionClosed):
        p.fence.reserve_lease("synthetic-new-producer")
    again = life.stop_new(p.session, p.sender)
    assert again["status"] == value["status"]
    assert [r[1] for r in p.transport.calls if r[0] == "POST"] == ["/stop-new"]


def test_sender_ack_unknown_still_stops_receiver_and_exact_query_recovers(staged):
    p = staged
    life.initialize(p.session, p.sender, p.policy)
    p.transport.lost = True
    value = life.stop_new(p.session, p.sender)
    assert value["status"] == "stop_unconfirmed" and "sender" in value["errors"]
    assert p.fence.snapshot()["phase"] == p.transport.state == "draining"
    advance(p, 121)
    value = life.stop_new(p.session, p.sender)
    assert value["status"] == "producers_stopped_drain_pending"
    assert len([row for row in p.transport.calls if row[0] == "POST"]) == 1


def test_supervisor_capacity_failure_performs_both_actual_restrictive_calls(staged):
    p = staged
    life.initialize(p.session, p.sender, p.policy)
    p.sender.postgres.sample["rows"] = p.policy["sender_stop_records"]
    value = life.supervise_step(p.session, p.sender)
    assert value["status"] == "producers_stopped_drain_pending" and "trigger" in value
    assert p.transport.state == p.fence.snapshot()["phase"] == "draining"


def test_stop_marker_sync_failure_does_not_skip_either_restrictive_call(staged):
    p = staged
    life.initialize(p.session, p.sender, p.policy)
    original = gate._write
    def fail_marker(path, value):
        if path.name == "STOP.json":
            raise OSError("synthetic marker fsync failed")
        return original(path, value)
    p.monkeypatch.setattr(gate, "_write", fail_marker)
    value = life.stop_new(p.session, p.sender)
    assert value["status"] == "stop_unconfirmed" and set(value["errors"]) == {"marker"}
    assert p.transport.state == p.fence.snapshot()["phase"] == "draining"


def test_concurrent_renewals_only_one_cas_survives(case):
    from concurrent.futures import ThreadPoolExecutor
    p = case
    first(p)
    advance(p, 3300)
    def attempt(number):
        try:
            return life.renew(p.session, p.sender, operation_id=operation(number), expected_sequence=0)
        except gate.AdmissionClosed:
            return None
    with ThreadPoolExecutor(max_workers=2) as pool:
        values = list(pool.map(attempt, (11, 12)))
    assert sum(value is not None for value in values) == 1
    assert len(list((p.root / "continuation/leases").iterdir())) == 4


def test_conflicting_existing_result_is_not_overwritten_by_query(case):
    p = case
    receipt = first(p)
    final = p.root / "continuation/leases" / (receipt["operation_id"] + ".json")
    changed = {**receipt, "capacity_sha256": operation(99)}
    gate._write(final, changed)
    with pytest.raises(gate.AdmissionError, match="conflicts"):
        life.query(p.session, p.sender, operation_id=receipt["operation_id"])
    assert gate._read(final) == changed


def test_failed_authority_before_first_lease_can_actually_abort_resumed_activation(staged):
    p = staged
    original = gate._write
    def fail_authority(path, value):
        if path.name == "authority.json":
            raise OSError("synthetic authority fsync failed")
        return original(path, value)
    p.monkeypatch.setattr(gate, "_write", fail_authority)
    with pytest.raises(OSError):
        life.initialize(p.session, p.sender, p.policy)
    assert not (p.root / "continuation/current.json").exists()
    result = life.abort_activation(p.session, p.sender)
    assert result["status"] == "producers_stopped_drain_pending"
    assert p.transport.state == p.fence.snapshot()["phase"] == "draining"
    assert not (p.root / "continuation/authority.json").exists()


def test_old_query_cannot_rollback_a_concurrent_new_pointer(case):
    from concurrent.futures import ThreadPoolExecutor
    p = case
    first(p)
    advance(p, 3300)
    def old_query():
        try:
            return life.query(p.session, p.sender, operation_id=operation(10))
        except gate.AdmissionClosed:
            return None
    with ThreadPoolExecutor(max_workers=2) as pool:
        old = pool.submit(old_query)
        newer = pool.submit(life.renew, p.session, p.sender, operation_id=operation(11), expected_sequence=0)
        old.result()
        newer.result()
    current = gate._read(p.root / "continuation/current.json")
    assert current["operation_id"] == operation(11) and current["sequence"] == 1
