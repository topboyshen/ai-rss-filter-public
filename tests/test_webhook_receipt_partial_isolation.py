"""Mixed receipt failures must isolate evidence, not stall unrelated deliveries."""

import errno
import hashlib
import json
import os
import subprocess
import sys
import time
from pathlib import Path

import pytest

from services import webhook_receipts as receipts
from tests import test_webhook_durable_delivery as delivery_helpers
from tests.test_webhook_durable_spool import isolated_spool  # noqa: F401
from tests.test_webhook_durable_delivery import (
    claim,
    pending,
    raw_body,
    receipt,
    signed_post,
)

delivery = delivery_helpers.delivery


def _partial_write(monkeypatch, body, fragment, status="accepted"):
    """Leave the actual named intent truncated, then fail the write operation."""
    delivery_id = hashlib.sha256(body).hexdigest()
    original = receipts.ReceiptStore._write
    observed = {}

    def interrupted(store, path, payload):
        if (path.parent == store.directory and path.name == delivery_id + ".json"
                and json.loads(payload)["status"] == status):
            temporary = path.with_name(path.name + ".pending")
            raw = fragment(payload)
            fd = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
            with os.fdopen(fd, "wb") as stream:
                stream.write(raw)
                stream.flush()
                os.fsync(stream.fileno())
            observed.update(path=temporary, bytes=raw)
            raise OSError(errno.EIO, "isolated partial receipt write")
        return original(store, path, payload)

    monkeypatch.setattr(receipts.ReceiptStore, "_write", interrupted)
    return observed


def _new_store(delivery):
    """No process-memory receipts are used by this fresh store instance."""
    with delivery.webhook._webhook_replay_lock:
        delivery.webhook._webhook_replay_seen.clear()
    return receipts.ReceiptStore(delivery.webhook._webhook_spool_dir)


def _claim_only(delivery, body):
    path, envelope = claim(delivery)
    expected = json.loads(body)["entries"][0]["url"]
    assert envelope["task"]["entries"][0]["url"] == expected
    return path, envelope


@pytest.mark.parametrize("fragment", [
    lambda payload: payload[:1],
    lambda payload: payload[:20],
    lambda payload: payload[:payload.index(b"\n") + 30],
], ids=["incomplete-header-one-byte", "incomplete-header", "complete-header-incomplete-body"])
def test_partial_first_receipt_holds_own_delivery_but_not_unrelated_new_message(
    delivery, monkeypatch, fragment,
):
    broken_body, normal_body = raw_body(101), raw_body(102)
    damaged = _partial_write(monkeypatch, broken_body, fragment)
    assert signed_post(delivery, broken_body).status_code == 503
    alias = json.dumps(json.loads(broken_body), separators=(",", ":")).encode()
    assert alias != broken_body
    assert signed_post(delivery, alias).status_code == 503
    assert damaged["path"].read_bytes() == damaged["bytes"]
    with pytest.raises(receipts.ReceiptError):
        _new_store(delivery).load(hashlib.sha256(broken_body).hexdigest())
    assert signed_post(delivery, broken_body, "/webhook/receipt").status_code == 503
    assert signed_post(delivery, broken_body).status_code == 503
    accepted = signed_post(delivery, normal_body)
    assert accepted.status_code == 202
    receipt(accepted, normal_body)
    path, envelope = _claim_only(delivery, normal_body)
    assert delivery.webhook._execute_claimed_spooled_task(path, envelope) is True
    assert delivery.webhook._complete_spooled_task(path, envelope["digest"], envelope["owner"])
    assert delivery.calls == [(json.loads(normal_body)["entries"][0]["url"], delivery.cfg["scoring_strategy_version"])]
    assert delivery.webhook._claim_next_spooled_task(now=time.time() + 1000) is None
    assert damaged["path"].read_bytes() == damaged["bytes"]
    assert len(pending(delivery)) == 1


@pytest.mark.parametrize("fragment", [lambda payload: payload[:8], lambda payload: payload[:-20],
                                     lambda payload: payload[:payload.index(b'"accepted_at":') + len(b'"accepted_at":') + 3]],
                         ids=["incomplete-header", "incomplete-completion", "incomplete-numeric-identity"])
def test_partial_completion_never_reverts_to_accepted_or_reexecutes_in_mixed_batch(
    delivery, monkeypatch, fragment,
):
    broken_body, normal_body = raw_body(201), raw_body(202)
    assert signed_post(delivery, broken_body).status_code == 202
    path, envelope = _claim_only(delivery, broken_body)
    assert delivery.webhook._execute_claimed_spooled_task(path, envelope) is True
    final = delivery.webhook._webhook_spool_dir / "receipts" / (hashlib.sha256(broken_body).hexdigest() + ".json")
    original_accepted = final.read_bytes()
    damaged = _partial_write(monkeypatch, broken_body, fragment, status="completed")
    assert delivery.webhook._complete_spooled_task(path, envelope["digest"], envelope["owner"]) is False
    assert path.exists() and len(delivery.calls) == 1
    with pytest.raises(receipts.ReceiptError):
        _new_store(delivery).load(hashlib.sha256(broken_body).hexdigest())
    assert signed_post(delivery, broken_body, "/webhook/receipt").status_code == 503
    assert signed_post(delivery, broken_body).status_code == 503
    assert signed_post(delivery, normal_body).status_code == 202
    recovered = delivery.webhook._claim_next_spooled_task(now=time.time() + 1000)
    assert recovered is not None
    normal_path, normal_envelope = recovered
    assert normal_envelope["task"]["entries"][0]["url"] == json.loads(normal_body)["entries"][0]["url"]
    assert delivery.webhook._execute_claimed_spooled_task(normal_path, normal_envelope) is True
    assert delivery.webhook._complete_spooled_task(normal_path, normal_envelope["digest"], normal_envelope["owner"])
    assert len(delivery.calls) == 2
    assert delivery.webhook._claim_next_spooled_task(now=time.time() + 2000) is None
    assert final.read_bytes() == original_accepted and damaged["path"].read_bytes() == damaged["bytes"]


def test_unconfirmed_visible_receipt_barrier_failure_is_local_to_its_message(delivery, monkeypatch):
    body, other = raw_body(301), raw_body(302)
    target = delivery.webhook._webhook_spool_dir / "receipts" / (hashlib.sha256(body).hexdigest() + ".json")
    original = receipts.ReceiptStore._barrier

    def deny_own_barrier(store, path):
        if path == target:
            raise OSError(errno.EIO, "isolated one-receipt namespace sync failure")
        return original(store, path)

    with monkeypatch.context() as context:
        context.setattr(receipts.ReceiptStore, "_barrier", deny_own_barrier)
        assert signed_post(delivery, body).status_code == 503
        assert target.exists()
        kept = target.read_bytes()
        assert signed_post(delivery, body, "/webhook/receipt").status_code == 503
        assert signed_post(delivery, other).status_code == 202
        path, envelope = _claim_only(delivery, other)
        assert delivery.webhook._execute_claimed_spooled_task(path, envelope) is True
        assert delivery.webhook._complete_spooled_task(path, envelope["digest"], envelope["owner"])
        assert target.read_bytes() == kept
    duplicate = signed_post(delivery, body)
    assert duplicate.status_code == 202
    assert duplicate.get_json()["status"] == "duplicate"
    receipt(signed_post(delivery, body, "/webhook/receipt"), body)
    assert len(pending(delivery)) == 1 and len(delivery.calls) == 1


def test_capacity_reads_are_bounded_by_actual_file_size_not_whole_store_budget(delivery, monkeypatch):
    for number in range(401, 407):
        body = raw_body(number, padding="x" * (64 * 1024))
        assert signed_post(delivery, body).status_code == 202
    original = os.fdopen
    requests = []

    class BoundedReader:
        def __init__(self, stream):
            self.stream = stream

        def __enter__(self):
            return self

        def __exit__(self, *args):
            return self.stream.__exit__(*args)

        def __getattr__(self, name):
            return getattr(self.stream, name)

        def read(self, size=-1):
            actual_size = os.fstat(self.stream.fileno()).st_size
            requests.append((size, actual_size))
            assert 0 <= size <= actual_size + 1, "receipt read requested aggregate store budget"
            return self.stream.read(size)

    def bounded_fdopen(fd, mode="r", *args, **kwargs):
        stream = original(fd, mode, *args, **kwargs)
        return BoundedReader(stream) if mode == "rb" else stream

    monkeypatch.setattr(os, "fdopen", bounded_fdopen)
    body = raw_body(410)
    response = signed_post(delivery, body)
    assert response.status_code == 202
    receipt(response, body)
    assert requests and len(pending(delivery)) == 7 and delivery.calls == []


def test_partial_receipt_counts_toward_hard_record_capacity(delivery, monkeypatch):
    real_store = receipts.ReceiptStore
    monkeypatch.setattr(delivery.webhook, "ReceiptStore", lambda spool: real_store(spool, max_records=2))
    broken_body = raw_body(501)
    damaged = _partial_write(monkeypatch, broken_body, lambda payload: payload[:20])
    assert signed_post(delivery, broken_body).status_code == 503
    second = raw_body(502)
    assert signed_post(delivery, second).status_code == 202
    assert signed_post(delivery, raw_body(503)).status_code == 503
    assert signed_post(delivery, second, "/webhook/receipt").status_code == 200
    assert damaged["path"].read_bytes() == damaged["bytes"]
    assert delivery.calls == []


def test_existing_reservations_reject_new_delivery_at_exact_byte_limit(delivery, monkeypatch):
    body = raw_body(601)
    assert signed_post(delivery, body).status_code == 202
    path = delivery.webhook._webhook_spool_dir / "receipts" / (hashlib.sha256(body).hexdigest() + ".json")
    held = json.loads(path.read_bytes())["_reserved_bytes"]
    real_store = receipts.ReceiptStore
    monkeypatch.setattr(delivery.webhook, "ReceiptStore", lambda spool: real_store(spool, max_bytes=held))
    assert signed_post(delivery, raw_body(602)).status_code == 503
    duplicate = signed_post(delivery, body)
    assert duplicate.status_code == 202 and duplicate.get_json()["status"] == "duplicate"
    receipt(signed_post(delivery, body, "/webhook/receipt"), body)
    assert delivery.calls == []


def test_oversized_sparse_receipt_is_rejected_before_any_buffered_read(tmp_path, monkeypatch):
    target = tmp_path / "oversized-receipt.json"
    fd = os.open(target, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    try:
        os.ftruncate(fd, receipts.MAX_RECORD_BYTES + 1)
    finally:
        os.close(fd)

    def must_not_materialize(*_args, **_kwargs):
        raise AssertionError("oversized receipt reached buffered allocation")

    monkeypatch.setattr(os, "fdopen", must_not_materialize)
    with pytest.raises(receipts.ReceiptCorrupt):
        receipts._read(target)
    assert target.stat().st_size == receipts.MAX_RECORD_BYTES + 1


@pytest.mark.parametrize("header", ["incomplete", "complete"])
def test_unknown_partial_without_trusted_queue_or_final_cannot_be_silently_skipped(delivery, header):
    known = raw_body(701)
    assert signed_post(delivery, known).status_code == 202
    directory = delivery.webhook._webhook_spool_dir / "receipts"
    orphan = directory / ("0" * 64 + ".json.pending")
    if header == "complete":
        known_path = directory / (hashlib.sha256(known).hexdigest() + ".json")
        record = json.loads(known_path.read_bytes())
        record.update(delivery_id="0" * 64, body_sha256="0" * 64, primary_delivery_id="0" * 64)
        raw = receipts._encode_record(record)
        orphan.write_bytes(raw[:raw.index(b"\n") + 30])
    else:
        orphan.write_bytes(b'{"_reserved_bytes":')
    orphan.chmod(0o600)
    before = orphan.read_bytes()
    assert signed_post(delivery, raw_body(702)).status_code == 503
    assert orphan.read_bytes() == before
    assert signed_post(delivery, known, "/webhook/receipt").status_code == 200
    assert delivery.calls == []


@pytest.mark.parametrize("field,value", [("accepted_strategy", "different"),
                                        ("accepted_revision", "b" * 40), ("accepted_at", 42.0)])
def test_valid_json_pending_identity_conflict_is_not_treated_as_isolated_truncation(delivery, field, value):
    body = raw_body(801)
    assert signed_post(delivery, body).status_code == 202
    final = delivery.webhook._webhook_spool_dir / "receipts" / (hashlib.sha256(body).hexdigest() + ".json")
    original = final.read_bytes()
    record = json.loads(original)
    record.update(status="completed", completion={"finished_at": 200.0})
    record[field] = value
    record["_reserved_bytes"] = receipts._reservation(record)
    temporary = final.with_name(final.name + ".pending")
    temporary.write_bytes(receipts._encode_record(record))
    temporary.chmod(0o600)
    before = temporary.read_bytes()
    assert signed_post(delivery, body).status_code == 503
    assert signed_post(delivery, body, "/webhook/receipt").status_code == 503
    assert delivery.webhook._claim_next_spooled_task(now=time.time() + 1000) is None
    assert final.read_bytes() == original and temporary.read_bytes() == before
    assert delivery.calls == []


@pytest.mark.parametrize("unsafe", ["public-mode", "symlink", "hardlink"])
def test_partial_isolation_does_not_swallow_unsafe_file_permissions_or_links(delivery, tmp_path, unsafe):
    body = raw_body(901)
    assert signed_post(delivery, body).status_code == 202
    directory = delivery.webhook._webhook_spool_dir / "receipts"
    temporary = directory / (hashlib.sha256(body).hexdigest() + ".json.pending")
    if unsafe == "public-mode":
        temporary.write_bytes(b'{"_reserved_bytes":')
        temporary.chmod(0o644)
    else:
        target = tmp_path / "unsafe-pending-source"
        target.write_bytes(b'{"_reserved_bytes":')
        target.chmod(0o600)
        if unsafe == "symlink":
            temporary.symlink_to(target)
        else:
            os.link(target, temporary)
    assert signed_post(delivery, raw_body(902)).status_code == 503
    assert signed_post(delivery, body, "/webhook/receipt").status_code == 503
    assert delivery.webhook._claim_next_spooled_task(now=time.time() + 1000) is None
    assert temporary.exists() and delivery.calls == []


def test_partial_alias_uses_immutable_intent_without_blocking_unrelated_message(delivery, monkeypatch):
    body = raw_body(1001)
    assert signed_post(delivery, body).status_code == 202
    path, envelope = _claim_only(delivery, body)
    assert delivery.webhook._execute_claimed_spooled_task(path, envelope) is True
    assert delivery.webhook._complete_spooled_task(path, envelope["digest"], envelope["owner"])
    original = receipt(signed_post(delivery, body, "/webhook/receipt"), body, "completed")
    final = delivery.webhook._webhook_spool_dir / "receipts" / (original["delivery_id"] + ".json")
    final_bytes = final.read_bytes()
    alias = json.dumps(json.loads(body), separators=(",", ":")).encode()
    assert alias != body
    damaged = _partial_write(monkeypatch, alias, lambda payload: payload[:1])
    assert signed_post(delivery, alias).status_code == 503
    assert signed_post(delivery, alias, "/webhook/receipt").status_code == 503
    assert signed_post(delivery, alias).status_code == 503
    # The independent intent proves the alias body/time even after the primary
    # queue has completed. The damaged alias itself remains unacknowledged.
    assert signed_post(delivery, raw_body(1002)).status_code == 202
    assert _new_store(delivery).load(original["delivery_id"])["status"] == "completed"
    next_path, next_envelope = _claim_only(delivery, raw_body(1002))
    assert delivery.webhook._execute_claimed_spooled_task(next_path, next_envelope) is True
    assert delivery.webhook._complete_spooled_task(next_path, next_envelope["digest"], next_envelope["owner"])
    assert delivery.webhook._claim_next_spooled_task(now=time.time() + 1000) is None
    assert damaged["path"].read_bytes() == damaged["bytes"] and final.read_bytes() == final_bytes
    assert len(delivery.calls) == 2 and pending(delivery) == []


_RESTART_MIXED = r'''
import hashlib,hmac,json,os,socket,sys,time
from pathlib import Path
def no_network(*args,**kwargs): raise AssertionError("no network in isolated process")
socket.socket.connect=no_network
socket.create_connection=no_network
import app
from routes import webhook_routes as w
cfg={"scoring_strategy_version":"test:durable-delivery-contract",
     "feeds":[{"url":"https://example.invalid/delivery-feed","name":"Synthetic"}]}
w._cfg=lambda:cfg
w.resolve_scoring_contract=lambda value,**kwargs:value
w.Scorer=lambda:object()
w.resolve_prompt=lambda *args:("default","synthetic")
w._find_notify_threshold=lambda *args:99.0
w._queue_committed_dashboard_inputs=lambda:None
w.DB.set_app_meta=lambda *args:None
w.DB.update_feed_health=lambda **kwargs:None
calls=[]
def fake(entry,**kwargs):
    assert "/2?" in entry.link, "old partial completion was rescored"
    calls.append(entry.link)
    return None,"filtered"
w.process_entry=fake
bad,good=bytes.fromhex(sys.argv[1]),bytes.fromhex(sys.argv[2])
def post(body,path="/webhook"):
    headers={"Content-Type":"application/json","X-Miniflux-Signature":
      hmac.new(os.environ["MINIFLUX_WEBHOOK_SECRET"].encode(),body,hashlib.sha256).hexdigest()}
    with app.app.test_client() as c: return c.post(path,data=body,headers=headers)
temporary=w._webhook_spool_dir/"receipts"/(hashlib.sha256(bad).hexdigest()+".json.pending")
before=temporary.read_bytes()
assert post(bad,"/webhook/receipt").status_code==503
assert post(bad).status_code==503
assert post(good).status_code==202
path,envelope=w._claim_next_spooled_task(now=time.time()+1000)
assert "/2?" in envelope["task"]["entries"][0]["url"]
assert w._execute_claimed_spooled_task(path,envelope) is True
assert w._complete_spooled_task(path,envelope["digest"],envelope["owner"])
assert w._claim_next_spooled_task(now=time.time()+2000) is None
assert temporary.read_bytes()==before and len(calls)==1
print(json.dumps({"status":"passed","new_fake_scores":1,"old_rescores":0,"evidence_unchanged":True}))
'''


@pytest.mark.parametrize("status", ["completed", "terminal"])
def test_truncated_outcome_restart_allows_unrelated_message_without_old_rescore(tmp_path, status):
    delivery_helpers.process_stage(tmp_path, "accept_exit", 73)
    old = delivery_helpers.process_stage(tmp_path, f"pending_{status}_truncated_exit", 76)
    assert old["calls"] == 1
    env = {"PATH": os.defpath, "AI_FILTER_DB_PATH": str(tmp_path / "isolated-child.db"),
           "WEBHOOK_SPOOL_DIR": str(tmp_path / "subprocess-spool"),
           "MINIFLUX_WEBHOOK_SECRET": delivery_helpers.SECRET,
           "AI_FILTER_REVISION": "a" * 40, "AI_FILTER_VERSION": "test-durable-delivery"}
    result = subprocess.run([sys.executable, "-B", "-c", _RESTART_MIXED, raw_body().hex(), raw_body(2).hex()],
                            cwd=Path(__file__).resolve().parents[1], env=env,
                            text=True, capture_output=True, timeout=15, check=False)
    assert result.returncode == 0, result.stderr[-6000:]
    assert json.loads(result.stdout.strip().splitlines()[-1])["old_rescores"] == 0
