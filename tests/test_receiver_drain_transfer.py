"""Real admission/auth/queue/receipt paths; synthetic originals and scorer."""
from concurrent.futures import ThreadPoolExecutor
import http.client
import hashlib
import hmac
import json
import multiprocessing
import os
import sys
import threading
import time
from types import SimpleNamespace

import pytest

from services import admission_fence as gate
from services import receiver_drain_storage as disk
from services import receiver_drain_transfer as transfer
from services import sender_drain_contract as sender
from services.webhook_receipts import ReceiptStore
from tests.test_sender_drain_contract import synthetic_manifest
from tests.test_webhook_durable_spool import isolated_spool  # noqa: F401
from tests.test_webhook_durable_delivery import CONTRACT, SECRET, delivery, raw_body, signed_post  # noqa: F401


@pytest.fixture
def receiver(delivery, tmp_path, monkeypatch):  # noqa: F811
    if sys.platform != "linux":
        monkeypatch.setattr(gate, "_boot_identity", lambda: "synthetic-local-boot")
    now = int(time.time())
    binding = {"revision": "a" * 40, "image_id": "sha256:" + "b" * 64, "strategy": CONTRACT,
               "source_sha256": gate.source_sha256(), "deployment_nonce": "c" * 32}
    root = tmp_path / "fence"
    gate.provision(root, binding, {"not_before": now - 1, "expires_at": now + 600,
                                   "max_admissions": 128, "max_bytes": 1024**2})
    monkeypatch.setenv("AI_FILTER_ADMISSION_MODE", "enforced")
    monkeypatch.setenv("AI_FILTER_ADMISSION_ROOT", str(root))
    monkeypatch.setenv("AI_FILTER_ADMISSION_BINDING", json.dumps(binding))
    fence = gate._configured()
    fence.open_trial("1" * 64, 0, "synthetic initial open")
    delivery.webhook._ensure_spool_dir()
    fence.stop_new("2" * 64, 1, "synthetic stop all new receiver producers")
    return SimpleNamespace(delivery=delivery, fence=fence, root=tmp_path, control=transfer.DrainTransfer(fence))


def bundle_and_plan(receiver, bodies=None, *, change=None, endpoint="http://localhost/webhook", inside_fence=False):
    bodies = bodies or [raw_body()]
    bundle = (receiver.fence.root if inside_fence else receiver.root) / "sender-originals"
    bundle.mkdir(mode=0o700)
    manifest, _ = synthetic_manifest(receiver.root)
    template = manifest["entries"][0]
    manifest["entries"] = []
    for body in bodies:
        digest = hashlib.sha256(body).hexdigest()
        payload = json.loads(body)
        row = {**template, "body_file": digest + ".body", "body_sha256": digest, "delivery_id": digest,
               "body_size": len(body), "signature": hmac.new(SECRET.encode(), body, hashlib.sha256).hexdigest(),
               "target_url": endpoint, "feed_id": payload["feed"]["id"],
               "user_id": payload["feed"]["user_id"], "entry_id": payload["entries"][0]["id"]}
        manifest["entries"].append(row)
        path = bundle / row["body_file"]
        path.write_bytes(body)
        path.chmod(0o600)
    manifest["entries"].sort(key=lambda row: row["delivery_id"])
    sample = manifest["inventory_summary"]
    sample.update(rows=len(bodies), outbox_rows=len(bodies), charged_bytes=sum(map(len, bodies)),
                  outbox_charged_bytes=sum(map(len, bodies)))
    sample["states"]["pending"] = len(bodies)
    if change:
        change(manifest)
    (bundle / "manifest.json").write_bytes(sender.encoded(manifest))
    (bundle / "manifest.json").chmod(0o600)
    spool = receiver.delivery.webhook._webhook_spool_dir
    fence = receiver.fence
    request = {"schema": 1, "operation_id": "3" * 64, "stop_operation_id": "2" * 64, "generation": 2,
               "receiver_binding": fence.binding, "receiver_manifest_sha256": gate._hash(fence._manifest()),
               "source_sha256": transfer.sources(), "spool": str(spool), "spool_identity": disk.identity(spool),
               "sender_bundle": str(bundle), "sender_manifest_sha256": sender.digest(manifest),
               "sender_binding": manifest["sender_binding"], "postgres_identity": manifest["postgres_identity"],
               "endpoint": endpoint, "contract": "synthetic-v1", "config_sha256": transfer.config_sha256(),
               "body_limit": transfer.body_limit(), "max_batches": 8, "max_replays": 16}
    return request, manifest


def pending(receiver):
    return sorted(receiver.delivery.webhook._webhook_spool_dir.glob("*.json"))


def run_one(receiver):
    webhook = receiver.delivery.webhook
    with gate.admission("synthetic_consumer", kind="drain"):
        path, envelope = webhook._claim_next_spooled_task(now=time.time() + 1)
        assert webhook._execute_claimed_spooled_task(path, envelope) is True
        assert webhook._complete_spooled_task(path, envelope["digest"], envelope["owner"]) is True


def test_durable_transfer_then_exact_duplicate_drains_without_new_intake(receiver):
    request, manifest = bundle_and_plan(receiver)
    assert signed_post(receiver.delivery, raw_body()).status_code == 503
    result = receiver.control.prepare(request)
    assert result["state"] == "complete" and result["confirmed"] == 1
    assert result["queue_checked"] is result["switch_authorized"] is result["legacy_originals_proven"] is False
    assert len(pending(receiver)) == 1 and receiver.delivery.calls == []
    before = pending(receiver)[0].read_bytes()
    response = signed_post(receiver.delivery, raw_body())
    assert response.status_code == 202 and response.json["status"] == "duplicate"
    assert pending(receiver)[0].read_bytes() == before
    assert signed_post(receiver.delivery, raw_body(99)).status_code == 503
    assert receiver.fence.snapshot()["intake_count"] == 0
    run_one(receiver)
    assert len(receiver.delivery.calls) == 1 and pending(receiver) == []
    assert signed_post(receiver.delivery, raw_body(), "/webhook/receipt").json["state"] == "completed"
    assert signed_post(receiver.delivery, raw_body()).json["state"] == "completed"
    assert len(receiver.delivery.calls) == 1
    receiver.fence.freeze("4" * 64, 2, "synthetic completed")
    assert signed_post(receiver.delivery, raw_body()).status_code == 503
    assert (receiver.root / "sender-originals" / manifest["entries"][0]["body_file"]).read_bytes() == raw_body()


@pytest.mark.parametrize("mutation", ["signature", "endpoint", "contract", "producer", "expired", "pg", "source", "body_size"])
def test_whole_manifest_rejected_before_any_queue_or_receipt_write(receiver, mutation):
    def change(manifest):
        row = manifest["entries"][-1]
        if mutation == "signature":
            row["signature"] = "0" * 64
        elif mutation == "endpoint":
            row["target_url"] = "http://other.invalid/webhook"
        elif mutation == "contract":
            row["contract"] = "unreviewed-v2"
        elif mutation == "producer":
            manifest["control_receipt"]["active_producer_leases"] = 1
        elif mutation == "expired":
            manifest["window"] = {"not_before": 1, "expires_at": 2}
        elif mutation == "source":
            manifest["source_sha256"] = "0" * 64
        elif mutation == "body_size":
            row["body_size"] += 1
    request, _ = bundle_and_plan(receiver, [raw_body(1), raw_body(2)], change=change)
    if mutation == "pg":
        request["postgres_identity"] = {**request["postgres_identity"], "table_oid": 9876}
    with pytest.raises((gate.AdmissionError, sender.DrainContractError)):
        receiver.control.prepare(request)
    assert pending(receiver) == []
    assert not (receiver.delivery.webhook._webhook_spool_dir / "receipts").exists()
    assert receiver.delivery.calls == []


def test_actual_smaller_application_body_limit_not_silently_expanded(receiver, monkeypatch):
    monkeypatch.setitem(receiver.delivery.app.config, "MAX_CONTENT_LENGTH", 1024)
    request, _ = bundle_and_plan(receiver, [raw_body(), raw_body(2, padding="x" * 2048)])
    with pytest.raises(gate.AdmissionError, match="body/endpoint"):
        receiver.control.prepare(request)
    assert pending(receiver) == []


def test_actual_missing_application_limit_is_not_a_default_allowance(receiver, monkeypatch):
    monkeypatch.setitem(receiver.delivery.app.config, "MAX_CONTENT_LENGTH", None)
    with pytest.raises(gate.AdmissionError, match="body limit absent"):
        transfer.body_limit()


def test_original_signature_rotation_refuses_without_resigning(receiver, monkeypatch):
    request, _ = bundle_and_plan(receiver)
    monkeypatch.setenv("MINIFLUX_WEBHOOK_SECRET", "new-key-must-not-resign-old-originals")
    with pytest.raises(gate.AdmissionError, match="never re-sign"):
        receiver.control.prepare(request)
    assert pending(receiver) == []


def test_sender_binding_or_config_mutation_cannot_use_old_transfer(receiver):
    request, _ = bundle_and_plan(receiver)
    receiver.control.prepare(request)
    receiver.delivery.cfg["different_non_strategy_config"] = True
    assert signed_post(receiver.delivery, raw_body()).status_code == 503
    with pytest.raises(gate.AdmissionError, match="config drift"):
        receiver.control.query(request["operation_id"])


def test_active_receiver_producer_blocks_transfer_despite_stop_receipt(receiver):
    request, _ = bundle_and_plan(receiver)
    state = gate._read(receiver.fence.root / "state.json")
    gate._write(receiver.fence.root / "state.json", {**state, "phase": "open"})
    producer = receiver.fence.reserve_lease("existing_producer", kind="intake")
    gate._write(receiver.fence.root / "state.json", state)
    try:
        with pytest.raises(gate.AdmissionError, match="producers"):
            receiver.control.prepare(request)
        assert pending(receiver) == []
    finally:
        producer.finish(True)


def test_lost_reply_queries_same_operation_never_normalizes_or_scores_twice(receiver, monkeypatch):
    request, _ = bundle_and_plan(receiver)
    first = receiver.control.prepare(request)
    with pytest.raises(gate.AdmissionError, match="query exact"):
        receiver.control.prepare(request)
    monkeypatch.setattr(receiver.delivery.webhook, "normalize_webhook_delivery", lambda *_: pytest.fail("renormalized"))
    assert transfer.DrainTransfer(receiver.fence).query(request["operation_id"]) == first
    assert signed_post(receiver.delivery, raw_body()).status_code == 202
    assert len(pending(receiver)) == 1 and receiver.delivery.calls == []


def test_concurrent_exact_and_forged_delivery_share_one_durable_task(receiver):
    request, _ = bundle_and_plan(receiver)
    receiver.control.prepare(request)
    with ThreadPoolExecutor(max_workers=6) as workers:
        results = list(workers.map(lambda n: signed_post(receiver.delivery, raw_body(n)).status_code, [1, 1, 1, 2, 3, 4]))
    assert results == [202, 202, 202, 503, 503, 503]
    assert len(pending(receiver)) == 1 and receiver.delivery.calls == []
    assert receiver.fence.snapshot()["uncertain"] == 0


def test_alias_originals_use_real_receipt_reservations_and_one_queue(receiver):
    body = raw_body()
    alias = json.dumps(json.loads(body), separators=(",", ":")).encode()
    request, _ = bundle_and_plan(receiver, [body, alias])
    result = receiver.control.prepare(request)
    assert result["confirmed"] == 2 and result["state"] == "complete"
    assert len(pending(receiver)) == 1
    with receiver.delivery.webhook.locked_transfer_spool() as directory:
        headers = ReceiptStore(directory)._headers()
        assert len(headers) == 2
        assert sum(row["_reserved_bytes"] for row in headers.values()) > len(body) + len(alias)
    assert signed_post(receiver.delivery, body).status_code == signed_post(receiver.delivery, alias).status_code == 202


def test_queue_capacity_partial_transfer_preserves_pending_and_exact_recovery(receiver, monkeypatch):
    monkeypatch.setattr(receiver.delivery.webhook, "_webhook_spool_max_tasks", 1)
    request, manifest = bundle_and_plan(receiver, [raw_body(1), raw_body(2)])
    result = receiver.control.prepare(request)
    assert result["state"] == "pending" and result["confirmed"] == 1
    assert len(pending(receiver)) == 1 and len(list((receiver.root / "sender-originals").glob("*.body"))) == 2
    assert signed_post(receiver.delivery, raw_body()).status_code == 503
    run_one(receiver)
    monkeypatch.setattr(receiver.delivery.webhook, "normalize_webhook_delivery", lambda *_: pytest.fail("re-normalized partial"))
    result = receiver.control.query(request["operation_id"])
    assert result["state"] == "complete" and result["confirmed"] == 2
    run_one(receiver)
    assert len(receiver.delivery.calls) == 2 and pending(receiver) == []
    assert len(manifest["entries"]) == 2


@pytest.mark.parametrize("boundary", ["queue_barrier", "receipt_barrier", "confirmation", "complete"])
def test_uncertain_write_recovery_preserves_original_and_uses_same_intent(receiver, monkeypatch, boundary):
    request, _ = bundle_and_plan(receiver)
    original_confirm = receiver.delivery.webhook._confirm_spool_file
    original_barrier = ReceiptStore._barrier
    original_write = disk.write
    hit = []
    def fail_once():
        if not hit:
            hit.append(True)
            raise OSError("synthetic lost fsync acknowledgement")
    def queue_barrier(path):
        result = original_confirm(path)
        if boundary == "queue_barrier":
            fail_once()
        return result
    def receipt_barrier(self, path):
        result = original_barrier(self, path)
        if boundary == "receipt_barrier":
            fail_once()
        return result
    def write(path, value):
        result = original_write(path, value)
        if (boundary == "confirmation" and path.parent.name == "confirmed") or (boundary == "complete" and path.name == "complete.json"):
            fail_once()
        return result
    monkeypatch.setattr(receiver.delivery.webhook, "_confirm_spool_file", queue_barrier)
    monkeypatch.setattr(ReceiptStore, "_barrier", receipt_barrier)
    monkeypatch.setattr(disk, "write", write)
    if boundary == "complete":
        with pytest.raises(transfer.CompletionUnconfirmed):
            receiver.control.prepare(request)
    else:
        assert receiver.control.prepare(request)["state"] == "pending"
    assert hit and len(pending(receiver)) == 1 and receiver.delivery.calls == []
    assert receiver.control.query(request["operation_id"])["state"] == "complete"
    assert len(pending(receiver)) == 1 and receiver.delivery.calls == []
    assert receiver.fence.snapshot()["uncertain"] == 0


@pytest.mark.parametrize("lost", ["absent", "pending"])
def test_completed_last_batch_recovers_after_expiry_and_exhaustion_without_business_write(receiver, monkeypatch, lost):
    request, _ = bundle_and_plan(receiver)
    request["max_batches"] = 1
    original = disk.write
    def lose_complete(path, value):
        if path.name == "complete.json":
            if lost == "pending":
                original(path.with_name(path.name + ".pending"), value)
            raise OSError("synthetic completion ACK lost")
        return original(path, value)
    monkeypatch.setattr(disk, "write", lose_complete)
    with pytest.raises(transfer.CompletionUnconfirmed):
        receiver.control.prepare(request)
    before = pending(receiver)[0].read_bytes()
    assert receiver.fence.snapshot()["uncertain"] == 0
    monkeypatch.setattr(disk, "write", original)
    clock = gate._elapsed
    monkeypatch.setattr(gate, "_elapsed", lambda: clock() + 7201)
    monkeypatch.setattr(receiver.delivery.webhook, "persist_delivery_locked", lambda *_: pytest.fail("new business transfer"))
    monkeypatch.setattr(receiver.delivery.webhook, "normalize_webhook_delivery", lambda *_: pytest.fail("re-normalized"))
    result = receiver.control.query(request["operation_id"])
    assert result["state"] == "complete" and result["confirmed"] == 1
    assert pending(receiver)[0].read_bytes() == before
    assert len(list((receiver.control.root / "batches").glob("*.json"))) == 1
    assert signed_post(receiver.delivery, raw_body()).status_code == 503


def test_expired_partial_transfer_cannot_import_remaining_original(receiver, monkeypatch):
    monkeypatch.setattr(receiver.delivery.webhook, "_webhook_spool_max_tasks", 1)
    request, _ = bundle_and_plan(receiver, [raw_body(1), raw_body(2)])
    request["max_batches"] = 1
    assert receiver.control.prepare(request)["confirmed"] == 1
    run_one(receiver)
    clock = gate._elapsed
    monkeypatch.setattr(gate, "_elapsed", lambda: clock() + 7201)
    with pytest.raises(gate.AdmissionError, match="window expired"):
        receiver.control.query(request["operation_id"])
    assert pending(receiver) == [] and len(receiver.delivery.calls) == 1


def test_receipt_capacity_stops_transfer_without_eviction(receiver, monkeypatch):
    monkeypatch.setattr(receiver.delivery.webhook, "_receipt_store", lambda directory: ReceiptStore(directory, max_records=1))
    request, _ = bundle_and_plan(receiver, [raw_body(1), raw_body(2)])
    result = receiver.control.prepare(request)
    assert result["state"] == "pending" and result["confirmed"] == 1
    receipts = receiver.delivery.webhook._webhook_spool_dir / "receipts"
    before = {p.name: p.read_bytes() for p in receipts.glob("*.json")}
    assert receiver.control.query(request["operation_id"])["state"] == "pending"
    assert {p.name: p.read_bytes() for p in receipts.glob("*.json")} == before
    assert len(list((receiver.root / "sender-originals").glob("*.body"))) == 2
    assert receiver.delivery.calls == []


def test_fixed_duplicate_budget_does_not_reset(receiver):
    request, _ = bundle_and_plan(receiver)
    request.update(max_batches=1, max_replays=1)
    receiver.control.prepare(request)
    assert signed_post(receiver.delivery, raw_body()).status_code == 202
    assert signed_post(receiver.delivery, raw_body()).status_code == 503
    assert transfer.DrainTransfer(receiver.fence).query(request["operation_id"])["state"] == "complete"
    assert signed_post(receiver.delivery, raw_body()).status_code == 503


def test_elapsed_expiry_never_extends_duplicate_permission(receiver, monkeypatch):
    request, _ = bundle_and_plan(receiver)
    receiver.control.prepare(request)
    previous = gate._elapsed
    monkeypatch.setattr(gate, "_elapsed", lambda: previous() + 7201)
    assert signed_post(receiver.delivery, raw_body()).status_code == 503
    assert len(pending(receiver)) == 1


def test_wrong_target_and_fake_drain_header_cannot_create_work(receiver):
    request, _ = bundle_and_plan(receiver)
    receiver.control.prepare(request)
    body = raw_body(99)
    signature = hmac.new(SECRET.encode(), body, hashlib.sha256).hexdigest()
    with receiver.delivery.app.test_client() as client:
        response = client.post("/webhook", data=body, headers={"X-Miniflux-Signature": signature, "X-Drain": "true"})
        wrong_host = client.post("/webhook", data=raw_body(), base_url="http://other.invalid",
                                 headers={"X-Miniflux-Signature": hmac.new(SECRET.encode(), raw_body(), hashlib.sha256).hexdigest()})
    assert response.status_code == wrong_host.status_code == 503
    assert len(pending(receiver)) == 1


def test_unicode_original_and_frozen_task_use_same_real_receipt_hash(receiver):
    body = json.loads(raw_body())
    body["entries"][0]["title"] = "原始中文标题"
    raw = json.dumps(body, ensure_ascii=False).encode()
    request, _ = bundle_and_plan(receiver, [raw])
    assert receiver.control.prepare(request)["state"] == "complete"
    response = signed_post(receiver.delivery, raw)
    assert response.status_code == 202 and response.json["status"] == "duplicate"
    assert "原始中文标题" in pending(receiver)[0].read_text()


def test_real_process_crash_retains_original_queue_and_orphaned_lease(receiver):
    request, _ = bundle_and_plan(receiver)
    def child():
        original = receiver.delivery.webhook.persist_delivery_locked
        def exit_after_queue(*args, **kwargs):
            original(*args, **kwargs)
            os._exit(73)
        receiver.delivery.webhook.persist_delivery_locked = exit_after_queue
        transfer.DrainTransfer(gate._configured()).prepare(request)
        os._exit(74)
    process = multiprocessing.get_context("fork").Process(target=child)
    process.start()
    process.join(timeout=10)
    if process.is_alive():
        process.kill()
        process.join(timeout=2)
        pytest.fail("synthetic child exceeded bound")
    assert process.exitcode == 73
    assert len(pending(receiver)) == 1 and receiver.delivery.calls == []
    assert receiver.fence.snapshot()["orphaned"] == 1
    with pytest.raises(gate.AdmissionError, match="uncertain activities"):
        receiver.control.query(request["operation_id"])
    assert len(list((receiver.root / "sender-originals").glob("*.body"))) == 1
    assert len(pending(receiver)) == 1


def test_actual_loopback_http_transfer_duplicate_receipt_and_new_body_refusal(receiver):
    from werkzeug.serving import make_server
    server = make_server("127.0.0.1", 0, receiver.delivery.app, threaded=True)
    endpoint = f"http://127.0.0.1:{server.server_port}/webhook"
    request, _ = bundle_and_plan(receiver, endpoint=endpoint)
    receiver.control.prepare(request)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    def send(raw, path="/webhook"):
        connection = http.client.HTTPConnection("127.0.0.1", server.server_port, timeout=2)
        try:
            connection.request("POST", path, raw, {"Content-Type": "application/json",
                "X-Miniflux-Signature": hmac.new(SECRET.encode(), raw, hashlib.sha256).hexdigest()})
            response = connection.getresponse()
            return response.status, json.loads(response.read())
        finally:
            connection.close()
    try:
        with ThreadPoolExecutor(max_workers=4) as workers:
            results = list(workers.map(lambda n: send(raw_body(n))[0], [1, 1, 2, 3]))
        assert results == [202, 202, 503, 503]
        assert len(pending(receiver)) == 1
        run_one(receiver)
        status, receipt = send(raw_body(), "/webhook/receipt")
        assert status == 200 and receipt["state"] == "completed" and len(receiver.delivery.calls) == 1
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)
