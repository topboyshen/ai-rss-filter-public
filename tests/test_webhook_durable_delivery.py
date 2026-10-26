"""Signed HTTP delivery/receipt integration; private spool and no real model."""

import hashlib
import hmac
import json
import errno
import os
import subprocess
import sys
import threading
import time
from pathlib import Path
from concurrent.futures import Future
from types import SimpleNamespace

import pytest

from tests.test_webhook_durable_spool import isolated_spool  # noqa: F401

SECRET = "isolated-delivery-signing-secret"
CONTRACT = "test:durable-delivery-contract"
FEED = "https://example.invalid/delivery-feed"


@pytest.fixture
def delivery(isolated_spool, monkeypatch):  # noqa: F811
    import app as app_module

    webhook = isolated_spool
    cfg = {"scoring_strategy_version": CONTRACT,
           "feeds": [{"url": FEED, "name": "Isolated delivery"}]}
    calls = []
    monkeypatch.setitem(app_module.app.config, "MAX_CONTENT_LENGTH", 1024 * 1024)
    monkeypatch.setenv("MINIFLUX_WEBHOOK_SECRET", SECRET)
    monkeypatch.setenv("AI_FILTER_REVISION", "a" * 40)
    monkeypatch.setenv("AI_FILTER_VERSION", "test-durable-delivery")
    monkeypatch.setattr(webhook, "_cfg", lambda: cfg)
    monkeypatch.setattr(webhook, "resolve_scoring_contract", lambda value, **_kw: value)
    monkeypatch.setattr(webhook, "Scorer", lambda: object())
    monkeypatch.setattr(webhook, "resolve_prompt", lambda *_a: ("default", "isolated prompt"))
    monkeypatch.setattr(webhook, "_find_notify_threshold", lambda *_a: 99.0)
    monkeypatch.setattr(webhook, "_queue_committed_dashboard_inputs", lambda: None)
    monkeypatch.setattr(webhook.DB, "set_app_meta", lambda *_a: None)
    monkeypatch.setattr(webhook.DB, "update_feed_health", lambda **_kw: None)

    def score(entry, **kwargs):
        calls.append((entry.link, kwargs["cfg"]["scoring_strategy_version"]))
        return None, "filtered"

    monkeypatch.setattr(webhook, "process_entry", score)
    return SimpleNamespace(webhook=webhook, app=app_module.app, cfg=cfg, calls=calls)


def raw_body(number=1, feed=FEED, **overrides):
    body = {"event_type": "new_entries", "feed": {"id": 7, "user_id": 3, "feed_url": feed},
            "entries": [{"id": number, "hash": f"entry-{number}", "title": "Isolated article",
                         "url": f"https://example.invalid/article/{number}?edition=1",
                         "content": "Synthetic article body only"}]}
    body.update(overrides)
    return json.dumps(body, sort_keys=True, ensure_ascii=False).encode()


def signed_post(delivery, body, path="/webhook", signature=None):
    signature = signature or hmac.new(SECRET.encode(), body, hashlib.sha256).hexdigest()
    with delivery.app.test_client() as client:
        return client.post(path, data=body, headers={"Content-Type": "application/json",
                                                   "X-Miniflux-Signature": signature})


def pending(delivery):
    return sorted(delivery.webhook._webhook_spool_dir.glob("*.json"))


@pytest.mark.parametrize("path", ["/webhook", "/webhook/receipt"])
@pytest.mark.parametrize("signature", [None, "0" * 64, "not-hex"])
def test_unsigned_or_wrongly_signed_delivery_and_query_never_touch_spool(delivery, path, signature):
    headers = {"Content-Type": "application/json"}
    if signature is not None:
        headers["X-Miniflux-Signature"] = signature
    with delivery.app.test_client() as client:
        response = client.post(path, data=raw_body(), headers=headers)
    assert response.status_code == 401
    assert not delivery.webhook._webhook_spool_dir.exists()
    assert delivery.calls == []


def test_signed_query_does_not_enqueue_or_execute_unknown_delivery(delivery):
    response = signed_post(delivery, raw_body(), "/webhook/receipt")
    assert response.status_code == 404
    assert response.get_json()["durable"] is False
    assert pending(delivery) == []
    assert delivery.calls == []


def test_changed_body_cannot_reuse_original_query_signature(delivery):
    original = raw_body()
    signature = hmac.new(SECRET.encode(), original, hashlib.sha256).hexdigest()
    response = signed_post(delivery, raw_body(2), "/webhook/receipt", signature)
    assert response.status_code == 401
    assert pending(delivery) == []
    assert delivery.calls == []


def test_process_memory_duplicate_cannot_acknowledge_failed_durable_write(delivery, monkeypatch):
    body = raw_body()
    key = hashlib.sha256(body).hexdigest()
    calls = []
    with delivery.webhook._webhook_replay_lock:
        delivery.webhook._webhook_replay_seen[key] = time.time()

    def unavailable(task, exact_body):
        calls.append((task, exact_body))
        return "full", None

    monkeypatch.setattr(delivery.webhook, "_submit_webhook_delivery", unavailable)
    response = signed_post(delivery, body)
    assert response.status_code == 503
    assert len(calls) == 1 and calls[0][1] == body
    assert pending(delivery) == []


@pytest.mark.parametrize("body", [b"{broken", b"[]", b'{"entries":"wrong"}'])
def test_invalid_signed_payload_does_not_create_durable_ack(delivery, body):
    response = signed_post(delivery, body)
    assert response.status_code == 400
    assert pending(delivery) == []
    assert delivery.calls == []


@pytest.mark.parametrize("path", ["/webhook", "/webhook/receipt"])
def test_oversize_signed_payload_rejected_before_queue_or_receipt(delivery, monkeypatch, path):
    monkeypatch.setitem(delivery.app.config, "MAX_CONTENT_LENGTH", 1024)
    body = raw_body(padding="x" * 4096)
    response = signed_post(delivery, body, path)
    assert response.status_code == 413
    assert pending(delivery) == []
    assert delivery.calls == []


def receipt(response, body, expected_state="accepted"):
    data = response.get_json()
    assert data["schema"] == 1
    assert data["durable"] is True
    assert data["delivery_id"] == hashlib.sha256(body).hexdigest()
    assert data["body_sha256"] == hashlib.sha256(body).hexdigest()
    assert data["state"] == expected_state
    assert data["accepted_strategy"] == CONTRACT
    assert data["accepted_revision"] == "a" * 40
    assert "Synthetic article body only" not in response.get_data(as_text=True)
    assert "example.invalid" not in response.get_data(as_text=True)
    return data


def test_http_202_binds_actual_body_and_query_is_not_a_second_delivery(delivery):
    body = raw_body()
    accepted = signed_post(delivery, body)
    assert accepted.status_code == 202
    first = receipt(accepted, body)
    files = pending(delivery)
    assert len(files) == 1
    before = {path: path.read_bytes() for path in files}
    queried = signed_post(delivery, body, "/webhook/receipt")
    assert queried.status_code == 200
    result = receipt(queried, body)
    assert result["status"] == "receipt"
    assert result["task_digest"] == first["task_digest"]
    assert {path: path.read_bytes() for path in pending(delivery)} == before
    assert delivery.calls == []


def test_repeated_http_delivery_after_memory_loss_uses_one_durable_task(delivery):
    body = raw_body()
    first = receipt(signed_post(delivery, body), body)
    with delivery.webhook._webhook_replay_lock:
        delivery.webhook._webhook_replay_seen.clear()
    repeated = signed_post(delivery, body)
    assert repeated.status_code == 202
    duplicate = receipt(repeated, body)
    assert duplicate["status"] == "duplicate"
    assert duplicate["task_digest"] == first["task_digest"]
    assert len(pending(delivery)) == 1
    assert delivery.calls == []


def test_different_raw_layout_deduplicates_task_without_misattributing_receipt(delivery):
    one = raw_body()
    two = json.dumps(json.loads(one), separators=(",", ":")).encode()
    assert one != two
    first = receipt(signed_post(delivery, one), one)
    second_response = signed_post(delivery, two)
    assert second_response.status_code == 202
    second = receipt(second_response, two)
    assert first["task_digest"] == second["task_digest"]
    assert first["delivery_id"] != second["delivery_id"]
    assert len(pending(delivery)) == 1
    assert receipt(signed_post(delivery, one, "/webhook/receipt"), one)["delivery_id"] == first["delivery_id"]
    assert receipt(signed_post(delivery, two, "/webhook/receipt"), two)["delivery_id"] == second["delivery_id"]


def claim(delivery):
    result = delivery.webhook._claim_next_spooled_task(now=time.time() + 1)
    assert result is not None
    return result


def test_worker_requires_all_entries_before_completed_receipt(delivery):
    body = raw_body()
    assert signed_post(delivery, body).status_code == 202
    path, envelope = claim(delivery)
    assert delivery.webhook._complete_spooled_task(path, envelope["digest"], envelope["owner"]) is False
    assert path.exists()
    receipt(signed_post(delivery, body, "/webhook/receipt"), body)
    assert delivery.calls == []


def test_real_claim_execute_complete_and_duplicate_query_never_rescore(delivery):
    body = raw_body()
    assert signed_post(delivery, body).status_code == 202
    path, envelope = claim(delivery)
    assert delivery.webhook._execute_claimed_spooled_task(path, envelope) is True
    assert len(delivery.calls) == 1
    assert delivery.webhook._complete_spooled_task(path, envelope["digest"], envelope["owner"]) is True
    assert not path.exists()
    receipt(signed_post(delivery, body, "/webhook/receipt"), body, "completed")
    response = signed_post(delivery, body)
    assert response.status_code == 202
    receipt(response, body, "completed")
    assert delivery.webhook._claim_next_spooled_task(now=time.time() + 1000) is None
    assert len(delivery.calls) == 1


def test_stale_worker_cannot_complete_another_owners_delivery(delivery):
    body = raw_body()
    assert signed_post(delivery, body).status_code == 202
    path, envelope = claim(delivery)
    assert delivery.webhook._execute_claimed_spooled_task(path, envelope) is True
    assert delivery.webhook._complete_spooled_task(path, envelope["digest"], "wrong-owner") is False
    assert path.exists()
    receipt(signed_post(delivery, body, "/webhook/receipt"), body)


def test_execution_contract_change_preserves_accepted_work_without_model_call(delivery):
    body = raw_body()
    assert signed_post(delivery, body).status_code == 202
    path, envelope = claim(delivery)
    delivery.cfg["scoring_strategy_version"] = "test:new-contract-after-acceptance"
    outcome = delivery.webhook._execute_claimed_spooled_task(path, envelope)
    assert outcome == delivery.webhook._DELIVERY_CONTRACT_CHANGED
    assert delivery.calls == []
    assert path.exists()
    receipt(signed_post(delivery, body, "/webhook/receipt"), body)
    completed = Future()
    completed.set_result(outcome)
    assert delivery.webhook._webhook_slots.acquire(blocking=False)
    delivery.webhook._finish_spooled_task(completed, path, envelope["digest"], envelope["owner"])
    files = pending(delivery)
    assert len(files) == 1
    held = delivery.webhook._load_spooled_task(files[0])
    assert held["attempts"] == envelope["attempts"] - 1
    assert delivery.webhook._claim_next_spooled_task(now=time.time() + 1000) is None
    assert delivery.calls == []


@pytest.mark.parametrize("error", [errno.ENOSPC, errno.EIO])
def test_queue_without_receipt_never_202_or_executes_until_receipt_repaired(delivery, monkeypatch, error):
    body = raw_body()
    original = delivery.webhook._delivery_receipt

    def unavailable(*_args, **_kwargs):
        raise OSError(error, "isolated receipt persistence failure")

    monkeypatch.setattr(delivery.webhook, "_delivery_receipt", unavailable)
    assert signed_post(delivery, body).status_code == 503
    assert len(pending(delivery)) == 1
    assert signed_post(delivery, body, "/webhook/receipt").status_code == 404
    assert delivery.webhook._claim_next_spooled_task(now=time.time() + 1) is None
    assert delivery.calls == []
    monkeypatch.setattr(delivery.webhook, "_delivery_receipt", original)
    path, envelope = claim(delivery)
    receipt(signed_post(delivery, body, "/webhook/receipt"), body)
    assert delivery.webhook._execute_claimed_spooled_task(path, envelope) is True
    assert len(delivery.calls) == 1


def test_queue_visible_after_directory_sync_failure_is_not_acknowledged(delivery, monkeypatch):
    body = raw_body()
    original = delivery.webhook._fsync_directory
    observed = []

    def fail_queue_directory(path, **kwargs):
        if Path(path) == delivery.webhook._webhook_spool_dir and pending(delivery):
            observed.append(path)
            raise OSError(errno.EIO, "isolated queue directory sync failure")
        return original(path, **kwargs)

    monkeypatch.setattr(delivery.webhook, "_fsync_directory", fail_queue_directory)
    assert signed_post(delivery, body).status_code == 503
    assert observed
    assert len(pending(delivery)) == 1
    assert delivery.calls == []
    monkeypatch.setattr(delivery.webhook, "_fsync_directory", original)
    response = signed_post(delivery, body)
    assert response.status_code == 202
    receipt(response, body)
    assert len(pending(delivery)) == 1


def test_concurrent_identical_http_waits_for_first_failed_persistence(delivery, monkeypatch):
    body = raw_body()
    entered, release, second_started, second_done = (threading.Event() for _ in range(4))
    original = delivery.webhook._delivery_receipt
    attempts = []
    answers = {}
    errors = []

    def first_failure(*args, **kwargs):
        attempts.append(1)
        if len(attempts) == 1:
            entered.set()
            if not release.wait(2):
                raise RuntimeError("test release missing")
            raise OSError(errno.ENOSPC, "isolated first receipt failure")
        return original(*args, **kwargs)

    def request_one(name):
        try:
            if name == "second":
                second_started.set()
            answers[name] = signed_post(delivery, body)
        except BaseException as exc:
            errors.append(exc)
        finally:
            if name == "second":
                second_done.set()

    monkeypatch.setattr(delivery.webhook, "_delivery_receipt", first_failure)
    first = threading.Thread(target=request_one, args=("first",))
    second = threading.Thread(target=request_one, args=("second",))
    first.start()
    try:
        assert entered.wait(1)
        second.start()
        assert second_started.wait(1)
        assert not second_done.wait(0.05)
    finally:
        release.set()
        first.join(2)
        if second.ident is not None:
            second.join(2)
    assert not errors and not first.is_alive() and not second.is_alive()
    assert answers["first"].status_code == 503
    assert answers["second"].status_code == 202
    receipt(answers["second"], body)
    assert len(pending(delivery)) == 1
    assert delivery.calls == []


def test_completed_receipt_survives_unlink_failure_and_exhausted_expired_claim(delivery, monkeypatch):
    body = raw_body()
    assert signed_post(delivery, body).status_code == 202
    path, envelope = claim(delivery)
    assert delivery.webhook._execute_claimed_spooled_task(path, envelope) is True
    with delivery.webhook._locked_spool() as directory:
        current = delivery.webhook._load_spooled_task(path)
        current.update(attempts=delivery.webhook._webhook_max_attempts, lease_until=0)
        delivery.webhook._atomic_write(directory, path, delivery.webhook._canonical_json(current))
    original = Path.unlink

    def deny_source_unlink(self, *args, **kwargs):
        if self == path:
            raise OSError(errno.EIO, "isolated queue unlink failure")
        return original(self, *args, **kwargs)

    monkeypatch.setattr(Path, "unlink", deny_source_unlink)
    assert delivery.webhook._complete_spooled_task(path, envelope["digest"], envelope["owner"]) is False
    assert path.exists()
    receipt(signed_post(delivery, body, "/webhook/receipt"), body, "completed")
    monkeypatch.setattr(Path, "unlink", original)
    assert delivery.webhook._claim_next_spooled_task(now=time.time() + 1000) is None
    assert not path.exists()
    assert len(delivery.calls) == 1
    assert list((delivery.webhook._webhook_spool_dir / "dead-letter").glob("*.json")) == []


def test_delivery_completion_independent_of_weak_legacy_ledger(delivery, monkeypatch):
    body = raw_body()
    assert signed_post(delivery, body).status_code == 202
    path, envelope = claim(delivery)
    assert delivery.webhook._execute_claimed_spooled_task(path, envelope) is True

    def ledger_failure(*_args, **_kwargs):
        raise OSError(errno.EIO, "isolated weak ledger failure")

    monkeypatch.setattr(delivery.webhook, "_write_receipts_unlocked", ledger_failure)
    monkeypatch.setattr(delivery.webhook, "_load_receipts_unlocked", ledger_failure)
    assert delivery.webhook._complete_spooled_task(path, envelope["digest"], envelope["owner"]) is True
    receipt(signed_post(delivery, body, "/webhook/receipt"), body, "completed")
    assert delivery.webhook._claim_next_spooled_task(now=time.time() + 1000) is None
    assert len(delivery.calls) == 1


def test_unconfirmed_delivery_does_not_block_unrelated_background_task(delivery, monkeypatch):
    body = raw_body()

    def unavailable(*_args, **_kwargs):
        raise OSError(errno.ENOSPC, "isolated receipt failure")

    monkeypatch.setattr(delivery.webhook, "_delivery_receipt", unavailable)
    assert signed_post(delivery, body).status_code == 503
    normal = {"task_type": "refresh"}
    assert delivery.webhook._persist_webhook_task(normal) == "stored"
    _path, envelope = claim(delivery)
    assert envelope["task"] == normal
    assert len(pending(delivery)) == 1
    assert delivery.calls == []


def test_completed_visible_receipt_needs_successful_barrier_before_query_or_recovery(delivery, monkeypatch):
    from services.webhook_receipts import ReceiptStore

    body = raw_body()
    assert signed_post(delivery, body).status_code == 202
    path, envelope = claim(delivery)
    assert delivery.webhook._execute_claimed_spooled_task(path, envelope) is True
    original = ReceiptStore._barrier

    def fail_completed(self, target):
        value = json.loads(target.read_bytes())
        if value.get("status") == "completed":
            raise OSError(errno.EIO, "isolated completed-receipt namespace failure")
        return original(self, target)

    monkeypatch.setattr(ReceiptStore, "_barrier", fail_completed)
    assert delivery.webhook._complete_spooled_task(path, envelope["digest"], envelope["owner"]) is False
    assert path.exists()
    assert signed_post(delivery, body, "/webhook/receipt").status_code == 503
    assert delivery.webhook._claim_next_spooled_task(now=time.time() + 1000) is None
    assert path.exists() and len(delivery.calls) == 1
    monkeypatch.setattr(ReceiptStore, "_barrier", original)
    receipt(signed_post(delivery, body, "/webhook/receipt"), body, "completed")
    assert delivery.webhook._claim_next_spooled_task(now=time.time() + 1000) is None
    assert not path.exists() and len(delivery.calls) == 1


def test_mixed_terminal_batch_retains_negative_outcome_and_processes_later_entry(delivery, monkeypatch):
    value = json.loads(raw_body())
    value["entries"].extend(json.loads(raw_body(2))["entries"])
    body = json.dumps(value).encode()
    seen = []

    def mixed(entry, **_kwargs):
        seen.append(entry.link)
        if "/1?" in entry.link:
            return None, delivery.webhook.SCORE_TERMINAL_UNSCORED_REASON
        return None, "filtered"

    monkeypatch.setattr(delivery.webhook, "process_entry", mixed)
    assert signed_post(delivery, body).status_code == 202
    path, envelope = claim(delivery)
    assert delivery.webhook._execute_claimed_spooled_task(path, envelope) == delivery.webhook._WEBHOOK_PARTIAL
    assert len(seen) == 2
    assert delivery.webhook._complete_spooled_task(path, envelope["digest"], envelope["owner"]) is False
    assert delivery.webhook._archive_partial_task(path, envelope["digest"], envelope["owner"]) is True
    result = receipt(signed_post(delivery, body, "/webhook/receipt"), body, "terminal")
    assert result["completion"]["terminal_count"] == 1
    repeated = signed_post(delivery, body)
    assert repeated.status_code == 202
    assert repeated.get_json()["processed"] is False
    assert delivery.webhook._claim_next_spooled_task(now=time.time() + 1000) is None
    assert len(seen) == 2


def test_exhausted_delivery_receipt_survives_ordinary_dead_letter_eviction(delivery, monkeypatch):
    first_body = raw_body()
    assert signed_post(delivery, first_body).status_code == 202
    first_path, first_envelope = claim(delivery)
    with delivery.webhook._locked_spool() as directory:
        delivery.webhook._dead_letter_unlocked(directory, first_path, first_envelope, "isolated exhaustion")
    monkeypatch.setattr(delivery.webhook, "_webhook_dlq_max_tasks", 1)
    second_body = raw_body(2)
    assert signed_post(delivery, second_body).status_code == 202
    second_path, second_envelope = claim(delivery)
    with delivery.webhook._locked_spool() as directory:
        delivery.webhook._dead_letter_unlocked(directory, second_path, second_envelope, "isolated second exhaustion")
    assert not list((delivery.webhook._webhook_spool_dir / "dead-letter").glob(first_envelope["digest"] + ".*.json"))
    receipt(signed_post(delivery, first_body, "/webhook/receipt"), first_body, "terminal")
    repeated = signed_post(delivery, first_body)
    assert repeated.status_code == 202 and repeated.get_json()["processed"] is False
    assert pending(delivery) == []
    assert delivery.calls == []


@pytest.mark.parametrize("field,new", [("strategy", "test:wrong-contract"), ("revision", "b" * 40)])
def test_queue_context_must_match_already_committed_acceptance_identity(delivery, field, new):
    body = raw_body()
    assert signed_post(delivery, body).status_code == 202
    path = pending(delivery)[0]
    with delivery.webhook._locked_spool() as directory:
        current = delivery.webhook._load_spooled_task(path)
        current["receipt_context"][field] = new
        delivery.webhook._atomic_write(directory, path, delivery.webhook._canonical_json(current))
    if field == "strategy":
        delivery.cfg["scoring_strategy_version"] = new
    assert delivery.webhook._claim_next_spooled_task(now=time.time() + 1000) is None
    assert path.exists()
    assert delivery.calls == []
    receipt(signed_post(delivery, body, "/webhook/receipt"), body)


def test_terminal_receipt_never_reexecutes_leftover_active_checkpoint(delivery, monkeypatch):
    body = raw_body()
    assert signed_post(delivery, body).status_code == 202
    path, envelope = claim(delivery)
    original = delivery.webhook._move_atomic

    def fail_archive_move(source, target):
        if target.parent.name == "dead-letter":
            raise OSError(errno.EIO, "isolated interruption after terminal receipt")
        return original(source, target)

    monkeypatch.setattr(delivery.webhook, "_move_atomic", fail_archive_move)
    with delivery.webhook._locked_spool() as directory:
        with pytest.raises(OSError):
            delivery.webhook._dead_letter_unlocked(directory, path, envelope, "isolated permanent failure")
    assert path.exists()
    receipt(signed_post(delivery, body, "/webhook/receipt"), body, "terminal")
    monkeypatch.setattr(delivery.webhook, "_move_atomic", original)
    assert delivery.webhook._claim_next_spooled_task(now=time.time() + 1000) is None
    assert delivery.calls == []
    receipt(signed_post(delivery, body, "/webhook/receipt"), body, "terminal")


_PROCESS_SCRIPT = r'''
import errno, hashlib, hmac, json, os, socket, sys, time
from pathlib import Path
def no_network(*args, **kwargs):
    raise AssertionError("isolated subprocess must not access network")
socket.socket.connect = no_network
socket.create_connection = no_network
import app
from routes import webhook_routes as w
from services.webhook_receipts import ReceiptError, ReceiptStore
mode = sys.argv[1]
extra = {}
cfg = {"scoring_strategy_version": "test:durable-delivery-contract",
       "feeds": [{"url": "https://example.invalid/delivery-feed", "name": "Synthetic"}]}
w._cfg = lambda: cfg
w.resolve_scoring_contract = lambda value, **kw: value
w.Scorer = lambda: object()
w.resolve_prompt = lambda *args: ("default", "synthetic")
w._find_notify_threshold = lambda *args: 99.0
w._queue_committed_dashboard_inputs = lambda: None
w.DB.set_app_meta = lambda *args: None
w.DB.update_feed_health = lambda **kw: None
calls = []
def fake_process(entry, **kw):
    calls.append(entry.link)
    if "terminal" in mode:
        return None, w.SCORE_TERMINAL_UNSCORED_REASON
    return None, "filtered"
w.process_entry = fake_process
body = bytes.fromhex(sys.argv[2])
headers = {"Content-Type": "application/json", "X-Miniflux-Signature":
    hmac.new(os.environ["MINIFLUX_WEBHOOK_SECRET"].encode(), body, hashlib.sha256).hexdigest()}
def post(path):
    with app.app.test_client() as client:
        return client.post(path, data=body, headers=headers)
def query(expected):
    response = post("/webhook/receipt")
    value = response.get_json()
    assert response.status_code == 200 and value["durable"] is True
    assert value["body_sha256"] == hashlib.sha256(body).hexdigest()
    assert value["state"] == expected
    return value
if mode == "accept_exit":
    response = post("/webhook")
    assert response.status_code == 202 and response.get_json()["durable"] is True
    print(json.dumps({"stage": mode, "calls": len(calls), "durable": True}), flush=True)
    os._exit(73)
if mode == "read_accepted":
    query("accepted")
    response = post("/webhook")
    assert response.status_code == 202 and response.get_json()["status"] == "duplicate"
    assert len(list(w._webhook_spool_dir.glob("*.json"))) == 1
    assert calls == []
elif mode == "complete_unlink_exit":
    path, envelope = w._claim_next_spooled_task(now=time.time() + 1)
    assert w._execute_claimed_spooled_task(path, envelope) is True and len(calls) == 1
    original = Path.unlink
    def interrupted_unlink(self, *args, **kwargs):
        if self == path:
            raise OSError(errno.EIO, "isolated interrupted queue unlink")
        return original(self, *args, **kwargs)
    Path.unlink = interrupted_unlink
    assert w._complete_spooled_task(path, envelope["digest"], envelope["owner"]) is False
    assert path.exists()
    query("completed")
    print(json.dumps({"stage": mode, "calls": len(calls), "retained_queue": True}), flush=True)
    os._exit(74)
elif mode == "read_completed":
    extra["finished_at"] = query("completed")["completion"]["finished_at"]
    assert w._claim_next_spooled_task(now=time.time() + 1000) is None
    response = post("/webhook")
    assert response.status_code == 202 and response.get_json()["status"] == "duplicate"
    assert response.get_json()["state"] == "completed"
    assert list(w._webhook_spool_dir.glob("*.json")) == []
    assert list((w._webhook_spool_dir / "inflight").glob("*.json")) == []
    assert calls == []
elif mode == "terminal_receipt_failure_exit":
    path, envelope = w._claim_next_spooled_task(now=time.time() + 1)
    assert w._execute_claimed_spooled_task(path, envelope) == w._WEBHOOK_PARTIAL
    assert len(calls) == 1
    synced_archive = []
    original_sync = w._fsync_directory
    def trace_sync(directory, *args, **kwargs):
        result = original_sync(directory, *args, **kwargs)
        if directory.name == "dead-letter" and list(directory.glob("*.terminal-pending.json")):
            synced_archive.append(True)
        return result
    w._fsync_directory = trace_sync
    def receipt_failure(self, *args, **kwargs):
        assert synced_archive
        raise ReceiptError("isolated receipt failure after negative archive barrier")
    ReceiptStore.update_status = receipt_failure
    assert w._archive_partial_task(path, envelope["digest"], envelope["owner"]) is False
    assert path.exists() and synced_archive
    assert len(list((w._webhook_spool_dir / "dead-letter").glob("*.terminal-pending.json"))) == 1
    assert w._claim_next_spooled_task(now=time.time() + 1000) is None
    assert path.exists() and len(calls) == 1
    query("accepted")
    print(json.dumps({"stage": mode, "calls": len(calls), "retained_queue": True,
                      "archive_barrier_confirmed": True}), flush=True)
    os._exit(75)
elif mode.startswith("pending_"):
    _, status, failure, _ = mode.split("_")
    path, envelope = w._claim_next_spooled_task(now=time.time() + 1)
    expected = w._WEBHOOK_PARTIAL if status == "terminal" else True
    assert w._execute_claimed_spooled_task(path, envelope) == expected and len(calls) == 1
    final = w._webhook_spool_dir / "receipts" / (hashlib.sha256(body).hexdigest() + ".json")
    temporary = final.with_name(final.name + ".pending")
    original_sync, original_replace = os.fsync, os.replace
    def fail_sync(fd):
        if temporary.exists() and os.fstat(fd).st_ino == temporary.stat().st_ino:
            if failure == "truncated":
                os.ftruncate(fd, 8)
            raise OSError(errno.EIO, "isolated outcome pending fsync failure")
        return original_sync(fd)
    def fail_replace(source, target):
        if Path(source) == temporary:
            raise OSError(errno.EIO, "isolated outcome pending rename failure")
        return original_replace(source, target)
    if failure in {"fsync", "truncated"}:
        os.fsync = fail_sync
    else:
        os.replace = fail_replace
    if status == "terminal":
        assert w._archive_partial_task(path, envelope["digest"], envelope["owner"]) is False
    else:
        assert w._complete_spooled_task(path, envelope["digest"], envelope["owner"]) is False
    assert path.exists() and json.loads(final.read_bytes())["status"] == "accepted"
    before = temporary.read_bytes()
    if failure != "truncated":
        intended = json.loads(before)
        assert intended["status"] == status
        extra["finished_at"] = intended["completion"]["finished_at"]
    assert post("/webhook/receipt").status_code == 503
    assert w._claim_next_spooled_task(now=time.time() + 1000) is None
    assert len(calls) == 1 and path.exists() and temporary.read_bytes() == before
    print(json.dumps({"stage": mode, "calls": len(calls), "retained_queue": True, **extra}), flush=True)
    os._exit(76)
elif mode == "read_terminal":
    assert w._claim_next_spooled_task(now=time.time() + 1000) is None
    value = query("terminal")
    extra["finished_at"] = value["completion"]["finished_at"]
    assert value["completion"]["terminal_count"] == 1
    response = post("/webhook")
    assert response.status_code == 202 and response.get_json()["state"] == "terminal"
    assert response.get_json()["processed"] is False
    assert list(w._webhook_spool_dir.glob("*.json")) == []
    assert list((w._webhook_spool_dir / "inflight").glob("*.json")) == []
    assert len(list((w._webhook_spool_dir / "dead-letter").glob("*.terminal.json"))) == 1
    assert calls == []
elif mode == "read_held":
    temporary = w._webhook_spool_dir / "receipts" / (hashlib.sha256(body).hexdigest() + ".json.pending")
    before = temporary.read_bytes()
    assert post("/webhook/receipt").status_code == 503
    assert w._claim_next_spooled_task(now=time.time() + 1000) is None
    assert post("/webhook").status_code == 503
    assert len(list((w._webhook_spool_dir / "inflight").glob("*.json"))) == 1
    assert temporary.read_bytes() == before and calls == []
else:
    raise AssertionError("unexpected mode")
print(json.dumps({"stage": mode, "calls": len(calls), "ok": True, **extra}), flush=True)
'''


def process_stage(tmp_path, stage, expected_rc):
    """Real interpreter re-entry and os._exit; this is not a power-loss test."""
    env = {"PATH": os.defpath, "AI_FILTER_DB_PATH": str(tmp_path / "isolated-child.db"),
           "WEBHOOK_SPOOL_DIR": str(tmp_path / "subprocess-spool"),
           "MINIFLUX_WEBHOOK_SECRET": SECRET, "AI_FILTER_REVISION": "a" * 40,
           "AI_FILTER_VERSION": "test-durable-delivery"}
    result = subprocess.run([sys.executable, "-B", "-c", _PROCESS_SCRIPT, stage, raw_body().hex()],
                            cwd=Path(__file__).resolve().parents[1], env=env,
                            text=True, capture_output=True, timeout=15, check=False)
    assert result.returncode == expected_rc, result.stderr[-6000:]
    return json.loads(result.stdout.strip().splitlines()[-1])


def test_real_process_exit_after_acceptance_keeps_query_and_duplicate_proof(tmp_path):
    first = process_stage(tmp_path, "accept_exit", 73)
    second = process_stage(tmp_path, "read_accepted", 0)
    assert first["durable"] is True
    assert first["calls"] == second["calls"] == 0


def test_real_process_exit_after_completed_receipt_never_claims_again(tmp_path):
    process_stage(tmp_path, "accept_exit", 73)
    interrupted = process_stage(tmp_path, "complete_unlink_exit", 74)
    recovered = process_stage(tmp_path, "read_completed", 0)
    assert interrupted["retained_queue"] is True
    assert interrupted["calls"] == 1 and recovered["calls"] == 0


def test_negative_archive_retains_source_until_terminal_receipt_and_recovers_in_new_process(tmp_path):
    process_stage(tmp_path, "accept_exit", 73)
    interrupted = process_stage(tmp_path, "terminal_receipt_failure_exit", 75)
    recovered = process_stage(tmp_path, "read_terminal", 0)
    assert interrupted["retained_queue"] and interrupted["archive_barrier_confirmed"]
    assert interrupted["calls"] == 1 and recovered["calls"] == 0


@pytest.mark.parametrize("status", ["completed", "terminal"])
@pytest.mark.parametrize("failure", ["fsync", "rename"])
def test_pending_outcome_recovers_in_new_process_without_reexecution_or_new_finish_time(tmp_path, status, failure):
    process_stage(tmp_path, "accept_exit", 73)
    interrupted = process_stage(tmp_path, f"pending_{status}_{failure}_exit", 76)
    recovered = process_stage(tmp_path, f"read_{status}", 0)
    assert interrupted["retained_queue"] and interrupted["calls"] == 1
    assert recovered["calls"] == 0
    assert recovered["finished_at"] == interrupted["finished_at"]


@pytest.mark.parametrize("status", ["completed", "terminal"])
def test_truncated_pending_outcome_holds_after_process_restart(tmp_path, status):
    process_stage(tmp_path, "accept_exit", 73)
    interrupted = process_stage(tmp_path, f"pending_{status}_truncated_exit", 76)
    recovered = process_stage(tmp_path, "read_held", 0)
    assert interrupted["retained_queue"] and interrupted["calls"] == 1
    assert recovered["calls"] == 0
