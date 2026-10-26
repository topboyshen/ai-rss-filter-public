"""Actual receiver HTTP/files/SQLite; sender PG here is an explicit protocol fake.

The native Go/Flask/PG fixture supplies the independent real database proof.
"""
from contextlib import contextmanager
import copy
import hashlib
import hmac
import sqlite3

import pytest

from tests.test_receiver_drain_driver import driver  # noqa: F401
from tests.test_receiver_drain_transfer import receiver, run_one, pending  # noqa: F401
from tests.test_webhook_durable_delivery import delivery, raw_body, SECRET  # noqa: F401
from tests.test_webhook_durable_spool import isolated_spool  # noqa: F401
from tests.test_production_sender_driver import setup, clear  # noqa: F401
from services import receiver_drain_storage as disk
from services import receiver_drain_transfer as transfer
from services import sender_drain_contract as originals
from scripts.receiver_drain_fixture_runtime import prepare_storage
import joint_drain_controller as joint
import independent_drain_inventory as inventory
from production_sender_driver import SenderDriver
from sender_drain_inventory import FrozenPostgres, write_blob


@pytest.fixture
def combined(receiver, driver, setup, monkeypatch):  # noqa: F811
    original, transport, reader = setup
    sender = SenderDriver(original.plan, transport, reader, receiver.fence.root / "sender")
    raw = raw_body()
    key = hashlib.sha256(raw).hexdigest()
    reader.sample.update(charged_bytes=len(raw), outbox_charged_bytes=len(raw))
    def freeze(binding, receipt, window, root):
        # Deliberately fake PG protocol fixture; the receiver path stays real.
        disk.directory(root, create=True)
        row = {"delivery_id": key, "body_sha256": key, "body_size": len(raw), "body_file": key + ".body",
               "target_url": "http://localhost/webhook", "signature": hmac.new(SECRET.encode(), raw, hashlib.sha256).hexdigest(),
               "contract": "synthetic-v1", "user_id": 3, "feed_id": 7, "entry_id": 1, "state": "pending", "attempts": 1}
        value = {"schema": 1, "kind": "miniflux-durable-drain-v1", "sender_binding": binding,
                 "postgres_identity": reader.expected, "control_receipt": receipt, "window": window,
                 "source_sha256": originals.source_sha256(), "inventory_summary": copy.deepcopy(reader.sample),
                 "retained_lease_budget": {"records": 4, "limit": 10000, "remaining": 9996}, "entries": [row],
                 "legacy_original_coverage": "registered_paths_since_instrumentation_only", "queue_checked": False, "switch_authorized": False}
        write_blob(root / row["body_file"], raw, originals.MAX_BODY_BYTES)
        write_blob(root / "manifest.json", originals.encoded(value), originals.MAX_MANIFEST_BYTES)
        return value
    monkeypatch.setattr(reader, "freeze", freeze)
    @contextmanager
    def locked(operation, manifest):
        value = FrozenPostgres(reader, operation, manifest)
        value.snapshot = reader.snapshot()
        value._closed(value.snapshot)
        value.lock_identity = {"synthetic_protocol_only": True, "operation": operation}
        monkeypatch.setattr(value, "check", lambda: value._closed(reader.snapshot()))
        yield value
    monkeypatch.setattr(reader, "locked", locked)
    prepare_storage(receiver.root)
    for name in ("release", "refresh"):
        disk.create_lock(receiver.root / (name + ".lock"))
    spool = receiver.delivery.webhook._webhook_spool_dir
    # ReceiptStore construction creates the real private directory, no records.
    receiver.delivery.webhook._receipt_store(spool)
    spec = {"spool": str(spool), "journal": str(receiver.root / "journal"), "database": str(receiver.root / "fixture.sqlite"),
            "identities": {"spool": inventory.directory_identity(spool), "journal": inventory.directory_identity(receiver.root / "journal"),
                           "database": inventory._identity((receiver.root / "fixture.sqlite").stat())}}
    plan = {"schema": 1, "scope": "instrumented_joint_drain_only", "sender_plan_sha256": sender.plan_sha256,
            "receiver_plan_sha256": receiver.host.plan_sha256, "operations": {name: hashlib.sha256(name.encode()).hexdigest() for name in joint.OPERATIONS},
            "transfer_policy": {"endpoint": "http://localhost/webhook", "contract": "synthetic-v1", "config_sha256": transfer.config_sha256(),
                                "body_limit": transfer.body_limit(), "max_batches": 8, "max_replays": 16, "source_sha256": transfer.sources()},
            "inventory": spec, "locks": {name: joint._file_identity(receiver.root / (name + ".lock")) for name in ("release", "refresh")},
            "source_sha256": joint.sources()}
    controller = joint.JointDrainController(sender, receiver.host, plan, receiver.root / "joint")
    return controller, reader, transport


@pytest.mark.parametrize("receiver", ["open"], indirect=True)
def test_actual_receiver_queue_must_drain_before_any_joint_freeze_intent(receiver, combined):  # noqa: F811
    controller, reader, sender = combined
    stopped = controller.stop_new()
    assert stopped["sender"]["state"] == "draining" and stopped["receiver"]["phase"] == "draining"
    assert controller.transfer_originals()["state"] == "complete" and len(pending(receiver)) == 1
    with pytest.raises(Exception, match="sender still draining"):
        controller.freeze_sender()
    assert not (controller.root / "sender_freeze.intent.json").exists()
    clear(reader)
    assert controller.freeze_sender()["receipt"]["state"] == "closed"
    with pytest.raises(Exception, match="not independently drained"):
        controller.freeze_receiver()
    assert receiver.fence.snapshot()["phase"] == "draining"
    assert not (controller.root / "receiver_freeze.intent.json").exists()
    run_one(receiver)
    result = controller.freeze_receiver()
    assert result["receipt"]["phase"] == "frozen" and result["queue_checked"] is True
    assert result["inventory"]["storage_drained"] is True
    assert result["switch_authorized"] is result["legacy_originals_proven"] is result["external_ingress_proven"] is False
    assert len(receiver.delivery.calls) == 1
    assert [p for m, p, _ in sender.calls if m == "POST"] == ["/stop-new", "/pause"]


@pytest.mark.parametrize("receiver", ["open"], indirect=True)
def test_completed_joint_transfer_exact_query_after_window_does_not_write_more(receiver, combined):  # noqa: F811
    controller, _, _ = combined
    controller.transfer_originals()
    controller.sender.deadline = controller.receiver.deadline = 0
    assert controller.query_transfer()["state"] == "complete"
    with pytest.raises(Exception, match="window expired"):
        controller.transfer_originals()
    assert len(pending(receiver)) == 1


@pytest.mark.parametrize("receiver", ["open"], indirect=True)
@pytest.mark.parametrize("blocker", ["publication", "uncertain", "live_consumer", "held"])
def test_joint_real_inventory_and_activity_blockers_leave_drain_enabled(receiver, combined, blocker):  # noqa: F811
    controller, reader, _ = combined
    controller.transfer_originals()
    clear(reader)
    controller.freeze_sender()
    run_one(receiver)
    lease = None
    if blocker == "publication":
        with sqlite3.connect(controller.plan["inventory"]["database"]) as connection:
            connection.execute("UPDATE app_meta SET v='1' WHERE k='dashboard_realtime_input_revision'")
    elif blocker in {"uncertain", "live_consumer"}:
        lease = receiver.fence.reserve_lease("synthetic_existing_consumer", kind="drain")
        if blocker == "uncertain":
            lease.finish(False)
            lease = None
    else:
        reader.sample["states"]["acked"] = 0
        reader.sample["states"]["held"] = 1
    try:
        with pytest.raises(Exception):
            controller.freeze_receiver()
        assert receiver.fence.snapshot()["phase"] == "draining"
        assert not (controller.root / "receiver_freeze.intent.json").exists()
    finally:
        if lease:
            lease.finish(True)


@pytest.mark.parametrize("receiver", ["open"], indirect=True)
def test_joint_freeze_lost_result_queries_exact_operation_after_expiry(receiver, combined, monkeypatch):  # noqa: F811
    controller, reader, _ = combined
    controller.transfer_originals()
    clear(reader)
    controller.freeze_sender()
    run_one(receiver)
    original = disk.write
    hit = []
    def lost(path, value):
        result = original(path, value)
        if path == controller.root / "receiver_freeze.json" and not hit:
            hit.append(True)
            raise OSError("synthetic lost completion ACK")
        return result
    monkeypatch.setattr(disk, "write", lost)
    with pytest.raises(OSError):
        controller.freeze_receiver()
    assert receiver.fence.snapshot()["phase"] == "frozen"
    controller.sender.deadline = controller.receiver.deadline = 0
    result = controller.freeze_receiver()
    assert result["receipt"]["operation_id"] == controller.plan["operations"]["receiver_freeze"]
    assert result["inventory"]["storage_drained"] is True


@pytest.mark.parametrize("receiver", ["open"], indirect=True)
def test_sender_pause_lost_ack_queries_original_before_phase_or_expiry_gate(receiver, combined):  # noqa: F811
    controller, reader, transport = combined
    controller.transfer_originals()
    clear(reader)
    transport.lost = True
    with pytest.raises(TimeoutError):
        controller.freeze_sender()
    controller.sender.deadline = controller.receiver.deadline = 0
    result = controller.freeze_sender()
    assert result["receipt"]["state"] == "closed"
    assert [path for method, path, _ in transport.calls if method == "POST"] == ["/stop-new", "/pause"]


@pytest.mark.parametrize("receiver", ["open"], indirect=True)
def test_lost_sender_stop_exact_query_after_expiry_does_not_start_receiver_stop(receiver, combined):  # noqa: F811
    controller, _, transport = combined
    transport.lost = True
    with pytest.raises(TimeoutError):
        controller.stop_new()
    controller.sender.deadline = controller.receiver.deadline = 0
    result = controller.query_stop_new()
    assert result["sender"]["state"] == "draining" and result["receiver"] is None and result["both_stopped"] is False
    assert receiver.fence.snapshot()["phase"] == "open"
    assert not [row for row in receiver.transport.calls if row[0] == "POST"]
    assert [path for method, path, _ in transport.calls if method == "POST"] == ["/stop-new"]


@pytest.mark.parametrize("receiver", ["open"], indirect=True)
def test_lost_receiver_stop_exact_query_after_expiry_confirms_both_without_repost(receiver, combined):  # noqa: F811
    controller, _, _ = combined
    receiver.transport.lose_ack = True
    with pytest.raises(TimeoutError):
        controller.stop_new()
    controller.sender.deadline = controller.receiver.deadline = 0
    result = controller.query_stop_new()
    assert result["receiver"]["phase"] == "draining" and result["both_stopped"] is True
    assert len([row for row in receiver.transport.calls if row[0] == "POST"]) == 1


@pytest.mark.parametrize("receiver", ["open"], indirect=True)
def test_staged_host_adopts_only_original_actual_control_queries(receiver, combined):  # noqa: F811
    controller, reader, _ = combined
    controller.transfer_originals()
    clear(reader)
    controller.freeze_sender()
    restarted = joint.JointDrainController(controller.sender, controller.receiver, controller.plan, receiver.root / "staged-joint")
    stopped = {"sender": disk.confirm(controller.root / "sender_stop.json"), "receiver": disk.confirm(controller.root / "receiver_stop.json")}
    adapter = controller._transfer_driver(stopped, disk.confirm(controller.root / "bundle.json"))
    result = restarted.confirm_existing_boundaries(adapter)
    assert result["queue_checked"] is False
    with pytest.raises(Exception, match="not independently drained"):
        restarted.freeze_receiver()
    assert not (restarted.root / "receiver_freeze.intent.json").exists()
    run_one(receiver)
    assert restarted.freeze_receiver()["queue_checked"] is True


@pytest.mark.parametrize("receiver", ["open"], indirect=True)
def test_original_sender_consumer_counters_may_drain_between_stop_and_seal(receiver, combined):  # noqa: F811
    controller, reader, transport = combined
    transport.active = 2
    first = controller.stop_new()
    assert first["sender"]["active_leases"] == 2
    transport.active = 0
    assert controller.transfer_originals()["state"] == "complete"
    manifest, _ = originals.load_bundle(controller.sender.root / "originals")
    assert manifest["control_receipt"]["active_leases"] == 0
    clear(reader)
    controller.freeze_sender()
    staged = joint.JointDrainController(controller.sender, controller.receiver, controller.plan, receiver.root / "counter-staged")
    adapter = controller._transfer_driver(first, disk.confirm(controller.root / "bundle.json"))
    staged.confirm_existing_boundaries(adapter)
    evidence = disk.confirm(staged.root / "original-transfer-chain.json")
    assert evidence["sender_stop"]["pg_receipt"]["active_leases"] == 2
    assert evidence["sealed_stop"]["active_leases"] == 0


@pytest.mark.parametrize("receiver", ["open"], indirect=True)
@pytest.mark.parametrize("bad", ["boolean", "wrong_transfer", "changed_original"])
def test_staged_adoption_cannot_skip_or_forge_the_original_transfer_chain(receiver, combined, bad):  # noqa: F811
    controller, reader, _ = combined
    controller.transfer_originals()
    clear(reader)
    controller.freeze_sender()
    staged = joint.JointDrainController(controller.sender, controller.receiver, controller.plan, receiver.root / "bad-staged")
    stopped = {"sender": disk.confirm(controller.root / "sender_stop.json"), "receiver": disk.confirm(controller.root / "receiver_stop.json")}
    adapter = controller._transfer_driver(stopped, disk.confirm(controller.root / "bundle.json"))
    if bad == "boolean":
        adapter = True
    elif bad == "wrong_transfer":
        adapter.request["operation_id"] = "f" * 64
    else:
        original = next((controller.sender.root / "originals").glob("*.body"))
        original.write_bytes(original.read_bytes() + b" ")
    with pytest.raises(Exception):
        staged.confirm_existing_boundaries(adapter)
    assert not (staged.root / "sender_freeze.json").exists()
    assert receiver.fence.snapshot()["phase"] == "draining"


@pytest.mark.parametrize("receiver", ["open"], indirect=True)
def test_missing_wal_sidecar_refuses_before_readonly_sqlite_can_create_it(receiver, combined):  # noqa: F811
    controller, reader, _ = combined
    controller.transfer_originals()
    clear(reader)
    controller.freeze_sender()
    run_one(receiver)
    path = controller.plan["inventory"]["database"]
    with sqlite3.connect(path) as connection:
        assert connection.execute("PRAGMA journal_mode=WAL").fetchone() == ("wal",)
    connection.close()
    # The last connection naturally checkpoints/removes the sidecar pair.
    from pathlib import Path
    assert not Path(path + "-wal").exists() and not Path(path + "-shm").exists()
    with pytest.raises(FileNotFoundError):
        controller.freeze_receiver()
    assert not Path(path + "-wal").exists() and not Path(path + "-shm").exists()
    assert receiver.fence.snapshot()["phase"] == "draining"


def test_raw_freeze_helper_requires_actual_lock_descriptors(receiver):  # noqa: F811
    with pytest.raises(Exception, match="live receiver lock"):
        joint._finish_freeze_held(receiver.fence, {}, True)
