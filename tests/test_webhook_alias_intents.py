"""Immutable alias intents isolate partial writes without executing aliases."""

import errno
import hashlib
import json
import os
import stat
import subprocess
import sys
import threading
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import pytest

from services import webhook_receipts as receipts
from tests import test_webhook_durable_delivery as delivery_helpers
from tests.test_webhook_durable_delivery import pending, raw_body, receipt, signed_post
from tests.test_webhook_durable_spool import isolated_spool  # noqa: F401

delivery = delivery_helpers.delivery


@pytest.fixture
def store(tmp_path):
    spool = tmp_path / "spool"
    spool.mkdir(mode=0o700)
    return receipts.ReceiptStore(spool)


def accept(store, body=b"primary", task=None, **kwargs):
    return store.accept(body, task or {"feed_url": "synthetic", "entries": []},
                        strategy="first-strategy", revision="first-revision",
                        accepted_at=kwargs.pop("accepted_at", 123.5), **kwargs)


def alias_body(body):
    result = json.dumps(json.loads(body), separators=(",", ":"), ensure_ascii=False).encode()
    assert result != body
    return result


def private_bytes(path, data):
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(fd, "wb") as stream:
        stream.write(data)
        stream.flush()
        os.fsync(stream.fileno())


def interrupt_alias(monkeypatch, body, *, namespace="intent", cut=1):
    delivery_id = hashlib.sha256(body).hexdigest()
    original = receipts.ReceiptStore._write
    captured = {}

    def partial(store, path, payload):
        parent = store.intents if namespace == "intent" else store.directory
        if path.parent == parent and path.name == delivery_id + ".json":
            damaged = path.with_name(path.name + ".pending")
            end = payload.index(b"\n") + 30 if cut == "header" else cut
            private_bytes(damaged, payload[:end])
            captured.update(path=damaged, bytes=payload[:end], full=payload)
            raise OSError(errno.ENOSPC, "synthetic interrupted alias write")
        return original(store, path, payload)

    monkeypatch.setattr(receipts.ReceiptStore, "_write", partial)
    return captured


@pytest.mark.parametrize("cut", [0, 1, "header"])
def test_partial_alias_intent_holds_exact_body_preserves_bytes_and_allows_unrelated(delivery, monkeypatch, cut):
    primary, normal = raw_body(1001), raw_body(1002)
    assert signed_post(delivery, primary).status_code == 202
    alias = alias_body(primary)
    captured = interrupt_alias(monkeypatch, alias, cut=cut)
    assert signed_post(delivery, alias).status_code == 503
    assert signed_post(delivery, alias, "/webhook/receipt").status_code == 503
    assert signed_post(delivery, alias).status_code == 503
    assert captured["path"].read_bytes() == captured["bytes"]
    assert signed_post(delivery, normal).status_code == 202
    assert signed_post(delivery, primary).status_code == 202
    assert len(pending(delivery)) == 2 and delivery.calls == []
    replacement = receipts.ReceiptStore(delivery.webhook._webhook_spool_dir)
    headers = replacement._headers()
    held = headers[hashlib.sha256(alias).hexdigest()]
    assert held["task_digest"] is None and held["primary_delivery_id"] is None
    assert held["_reserved_bytes"] == receipts.MAX_ALIAS_RESERVATION
    with pytest.raises(receipts.ReceiptError):
        replacement.load(hashlib.sha256(alias).hexdigest())
    assert captured["path"].read_bytes() == captured["bytes"]


@pytest.mark.parametrize("cut", [0, 1, "header"])
def test_partial_alias_receipt_uses_synced_full_intent_without_acknowledging_alias(delivery, monkeypatch, cut):
    primary, normal = raw_body(1011), raw_body(1012)
    assert signed_post(delivery, primary).status_code == 202
    alias = alias_body(primary)
    captured = interrupt_alias(monkeypatch, alias, namespace="receipt", cut=cut)
    assert signed_post(delivery, alias).status_code == 503
    directory = delivery.webhook._webhook_spool_dir / "receipts"
    intent = directory / "alias-intents" / (hashlib.sha256(alias).hexdigest() + ".json")
    kept = intent.read_bytes()
    assert json.loads(kept)["raw_body_b64"]
    assert signed_post(delivery, alias, "/webhook/receipt").status_code == 503
    assert signed_post(delivery, normal).status_code == 202
    assert len(pending(delivery)) == 2 and delivery.calls == []
    assert captured["path"].read_bytes() == captured["bytes"] and intent.read_bytes() == kept


@pytest.mark.parametrize("stage", ["file-sync", "rename", "directory-sync"])
def test_complete_intent_failure_recovers_original_identity_after_new_store(store, monkeypatch, stage):
    primary, _ = accept(store)
    alias = b"alias"
    delivery_id = hashlib.sha256(alias).hexdigest()
    store._intent_directory(create=True)
    target = store.intents / (delivery_id + ".json")
    temporary = target.with_name(target.name + ".pending")
    fsync, replace, barrier = os.fsync, os.replace, receipts.ReceiptStore._barrier

    def fail_fsync(fd):
        info = os.fstat(fd)
        if temporary.exists() and stat.S_ISREG(info.st_mode) and info.st_ino == temporary.stat().st_ino:
            raise OSError(errno.EIO, "synthetic intent file sync")
        return fsync(fd)

    def fail_replace(source, destination):
        if destination == target:
            raise OSError(errno.EIO, "synthetic intent rename")
        return replace(source, destination)

    def fail_barrier(instance, path):
        if path == target:
            raise OSError(errno.EIO, "synthetic post-rename directory sync")
        return barrier(instance, path)

    with monkeypatch.context() as context:
        context.setattr(os, "fsync", fail_fsync if stage == "file-sync" else fsync)
        context.setattr(os, "replace", fail_replace if stage == "rename" else replace)
        context.setattr(receipts.ReceiptStore, "_barrier", fail_barrier if stage == "directory-sync" else barrier)
        with pytest.raises(OSError):
            accept(store, alias, accepted_at=456.25)
        assert not (store.directory / (delivery_id + ".json")).exists()
    held = (target if target.exists() else temporary).read_bytes()
    replacement = receipts.ReceiptStore(store.spool)
    with pytest.raises(receipts.ReceiptConflict):
        replacement.load(delivery_id)
    record, created = replacement.accept(alias, primary["task"], strategy="new", revision="new", accepted_at=999)
    assert created and record["accepted_at"] == 456.25
    assert record["accepted_strategy"] == primary["accepted_strategy"]
    assert target.read_bytes() == held
    assert replacement.load(delivery_id) == record
    assert len(replacement._headers()) == 2


@pytest.mark.parametrize("status", ["completed", "terminal"])
def test_intent_before_primary_outcome_resolves_current_state_without_queue_or_scoring(delivery, monkeypatch, status):
    primary = raw_body(1021)
    first = receipt(signed_post(delivery, primary), primary)
    alias = alias_body(primary)
    target = delivery.webhook._webhook_spool_dir / "receipts" / (hashlib.sha256(alias).hexdigest() + ".json")
    original = receipts.ReceiptStore._write

    def stop_before_receipt(instance, path, payload):
        if path == target:
            raise OSError(errno.EIO, "crash after durable alias intent")
        return original(instance, path, payload)

    with monkeypatch.context() as context:
        context.setattr(receipts.ReceiptStore, "_write", stop_before_receipt)
        assert signed_post(delivery, alias).status_code == 503
    store = receipts.ReceiptStore(delivery.webhook._webhook_spool_dir)
    store.update_status(first["delivery_id"], status=status, completion={"synthetic": True})
    before = {path: path.read_bytes() for path in pending(delivery)}
    response = signed_post(delivery, alias)
    assert response.status_code == 202
    receipt(response, alias, status)
    receipt(signed_post(delivery, alias, "/webhook/receipt"), alias, status)
    assert {path: path.read_bytes() for path in pending(delivery)} == before
    assert delivery.calls == []


def test_alias_max_headroom_is_checked_before_creating_intent_and_actual_charge_counts_once(store):
    primary, _ = accept(store)
    held = primary["_reserved_bytes"]
    store.max_bytes = held + receipts.MAX_ALIAS_RESERVATION - 1
    with pytest.raises(receipts.ReceiptCapacityError):
        accept(store, b"alias")
    assert list(store.intents.iterdir()) == []
    store.max_bytes += 1
    alias, _ = accept(store, b"alias")
    headers = store._headers()
    assert len(headers) == 2
    assert headers[alias["delivery_id"]]["_reserved_bytes"] == alias["_reserved_bytes"]
    assert alias["_reserved_bytes"] < receipts.MAX_ALIAS_RESERVATION
    charge = sum(value["_reserved_bytes"] for value in headers.values())
    store.max_bytes = charge
    duplicate, created = accept(store, b"alias", accepted_at=999)
    assert not created and duplicate == alias
    assert store.update_status(primary["delivery_id"], status="completed", completion={"count": 1})
    with pytest.raises(receipts.ReceiptCapacityError):
        accept(store, b"next", {"entries": [1]})


def test_partial_intent_counts_as_one_record_and_maximum_slot(store, monkeypatch):
    primary, _ = accept(store)
    captured = interrupt_alias(monkeypatch, b"alias", cut=1)
    with pytest.raises(OSError):
        accept(store, b"alias")
    store.max_records = 2
    with pytest.raises(receipts.ReceiptCapacityError):
        accept(store, b"normal", {"entries": [2]})
    store.max_records = 3
    store.max_bytes = primary["_reserved_bytes"] + receipts.MAX_ALIAS_RESERVATION
    with pytest.raises(receipts.ReceiptCapacityError):
        accept(store, b"normal", {"entries": [2]})
    assert captured["path"].read_bytes() == captured["bytes"]
    assert len(store._headers()) == 2


@pytest.mark.parametrize("related", ["receipt-final", "receipt-pending", "intent-final"])
def test_partial_intent_is_not_pre_receipt_if_related_evidence_exists(store, monkeypatch, related):
    accept(store)
    captured = interrupt_alias(monkeypatch, b"alias", cut=1)
    with pytest.raises(OSError):
        accept(store, b"alias")
    delivery_id = hashlib.sha256(b"alias").hexdigest()
    parent = store.intents if related == "intent-final" else store.directory
    suffix = ".json.pending" if related == "receipt-pending" else ".json"
    private_bytes(parent / (delivery_id + suffix), captured["full"])
    with pytest.raises(receipts.ReceiptError):
        accept(store, b"normal", {"entries": [3]})
    assert captured["path"].read_bytes() == captured["bytes"]


@pytest.mark.parametrize("mutation", ["task", "raw-body", "strategy", "revision", "primary", "reservation", "status", "schema"])
def test_valid_json_intent_conflicts_are_not_downgraded_to_partial_holds(store, mutation):
    primary, _ = accept(store)
    alias, _ = accept(store, b"alias")
    path = store.intents / (alias["delivery_id"] + ".json")
    forged = dict(alias)
    if mutation == "task":
        forged["task"] = {"entries": ["different"]}
        forged["task_digest"] = hashlib.sha256(receipts._json(forged["task"])).hexdigest()
    elif mutation == "raw-body":
        forged["raw_body_b64"] = primary["raw_body_b64"]
    elif mutation == "strategy":
        forged["accepted_strategy"] = "other"
    elif mutation == "revision":
        forged["accepted_revision"] = "other"
    elif mutation == "primary":
        forged["primary_delivery_id"] = "1" * 64
    elif mutation == "reservation":
        forged["_reserved_bytes"] = 1
    elif mutation == "status":
        forged["status"] = "completed"
    else:
        forged["alias_intent_schema"] = True
    if mutation != "reservation":
        forged["_reserved_bytes"] = receipts._reservation(forged)
    path.write_bytes(receipts._encode_record(forged))
    with pytest.raises((receipts.ReceiptError, OSError)):
        accept(store, b"normal", {"entries": [4]})
    assert store.find_task(primary["task_digest"])["delivery_id"] == primary["delivery_id"]


@pytest.mark.parametrize("unsafe", ["mode", "symlink", "hardlink"])
def test_unsafe_partial_intent_never_qualifies_as_isolated_hold(store, monkeypatch, unsafe):
    accept(store)
    captured = interrupt_alias(monkeypatch, b"alias", cut=1)
    with pytest.raises(OSError):
        accept(store, b"alias")
    path = captured["path"]
    if unsafe == "mode":
        path.chmod(0o644)
    elif unsafe == "hardlink":
        os.link(path, store.spool / "retained-copy")
    else:
        kept = store.spool / "retained-original"
        path.rename(kept)
        path.symlink_to(kept)
    with pytest.raises((receipts.ReceiptError, OSError)):
        accept(store, b"normal", {"entries": [5]})


def test_partial_intent_failed_hold_barrier_never_allows_new_acceptance(store, monkeypatch):
    accept(store)
    captured = interrupt_alias(monkeypatch, b"alias", cut=1)
    with pytest.raises(OSError):
        accept(store, b"alias")
    original = receipts.ReceiptStore._barrier

    def fail_held(instance, path):
        if path == captured["path"]:
            raise OSError(errno.EIO, "synthetic hold barrier failure")
        return original(instance, path)

    monkeypatch.setattr(receipts.ReceiptStore, "_barrier", fail_held)
    with pytest.raises(OSError):
        accept(store, b"normal", {"entries": [6]})
    assert captured["path"].read_bytes() == captured["bytes"]


def test_concurrent_signed_aliases_keep_one_primary_and_no_extra_queue(delivery):
    primary = raw_body(1031)
    assert signed_post(delivery, primary).status_code == 202
    bodies = [json.dumps(json.loads(primary), indent=indent).encode() for indent in (1, 2, 3)]
    start = threading.Barrier(6)

    def submit(body):
        start.wait(timeout=10)
        response = signed_post(delivery, body)
        assert response.status_code == 202
        return receipt(response, body)

    with ThreadPoolExecutor(max_workers=6) as executor:
        results = list(executor.map(submit, bodies * 2))
    assert len({item["primary_delivery_id"] for item in results}) == 1
    assert len(pending(delivery)) == 1 and delivery.calls == []
    store = receipts.ReceiptStore(delivery.webhook._webhook_spool_dir)
    assert len(store._headers()) == 4
    assert len(list(store.intents.glob("*.json"))) == 3
    assert not list(store.intents.glob("*.pending"))


def test_legacy_alias_without_intent_retains_compatibility_and_unknown_partial_stays_closed(store):
    primary, _ = accept(store)
    old = receipts.accepted_record(b"legacy-alias", primary["task"], strategy="unused", revision="unused",
                                   accepted_at=789, primary=primary)
    old.pop("alias_intent_schema")
    old["_reserved_bytes"] = receipts._reservation(old)
    path = store.directory / (old["delivery_id"] + ".json")
    private_bytes(path, receipts._encode_record(old))
    assert store.load(old["delivery_id"]) == old
    assert not store.intents.exists()
    missing = receipts.accepted_record(b"legacy-partial", primary["task"], strategy="unused", revision="unused",
                                       accepted_at=790, primary=primary)
    missing.pop("alias_intent_schema")
    missing["_reserved_bytes"] = receipts._reservation(missing)
    partial = store.directory / (missing["delivery_id"] + ".json.pending")
    private_bytes(partial, receipts._encode_record(missing)[:1])
    with pytest.raises(receipts.ReceiptError):
        accept(store, b"normal", {"entries": [7]})
    assert partial.read_bytes() == b"{"


def test_every_incomplete_intent_byte_prefix_is_counted_without_trusting_partial_identity(store):
    primary, _ = accept(store)
    alias = receipts.accepted_record(b"alias-\xe4\xb8\xad", primary["task"], strategy="unused", revision="unused",
                                     accepted_at=1792630824.142329, primary=primary)
    store._intent_directory(create=True)
    payload = receipts._encode_record(alias)
    temporary = store.intents / (alias["delivery_id"] + ".json.pending")
    private_bytes(temporary, b"")
    for cut in range(len(payload)):
        damaged = payload[:cut]
        temporary.write_bytes(damaged)
        headers = store._headers()
        held = headers[alias["delivery_id"]]
        assert held == {"delivery_id": alias["delivery_id"], "task_digest": None,
                        "primary_delivery_id": None, "_reserved_bytes": receipts.MAX_ALIAS_RESERVATION}
        assert temporary.read_bytes() == damaged
    temporary.write_bytes(payload)
    resumed, created = accept(store, b"alias-\xe4\xb8\xad", accepted_at=999)
    assert created and resumed == alias


def test_every_incomplete_alias_receipt_prefix_uses_original_synced_intent(store):
    primary, _ = accept(store)
    alias = receipts.accepted_record(b"alias", primary["task"], strategy="unused", revision="unused",
                                     accepted_at=1792630824.142329, primary=primary)
    store._prepare_alias(alias)
    intent = store.intents / (alias["delivery_id"] + ".json")
    kept = intent.read_bytes()
    payload = receipts._encode_record(alias)
    temporary = store.directory / (alias["delivery_id"] + ".json.pending")
    private_bytes(temporary, b"")
    for cut in range(len(payload)):
        damaged = payload[:cut]
        temporary.write_bytes(damaged)
        assert store._headers()[alias["delivery_id"]] == {key: alias[key] for key in receipts._HEAD_KEYS}
        assert temporary.read_bytes() == damaged and intent.read_bytes() == kept


def test_complete_intent_and_pending_with_equal_header_but_changed_timestamp_conflict(store):
    primary, _ = accept(store)
    alias = receipts.accepted_record(b"alias", primary["task"], strategy="unused", revision="unused",
                                     accepted_at=123.5, primary=primary)
    store._prepare_alias(alias)
    changed = dict(alias, accepted_at=223.5)
    changed["_reserved_bytes"] = receipts._reservation(changed)
    assert {key: alias[key] for key in receipts._HEAD_KEYS} == {key: changed[key] for key in receipts._HEAD_KEYS}
    temporary = store.intents / (alias["delivery_id"] + ".json.pending")
    private_bytes(temporary, receipts._encode_record(changed))
    kept = temporary.read_bytes()
    with pytest.raises(receipts.ReceiptConflict):
        accept(store, b"new", {"entries": [8]})
    assert temporary.read_bytes() == kept


def test_equal_complete_intent_pending_recovers_before_capacity_accounting(store):
    primary, _ = accept(store)
    alias = receipts.accepted_record(b"alias", primary["task"], strategy="unused", revision="unused",
                                     accepted_at=123.5, primary=primary)
    store._prepare_alias(alias)
    final = store.intents / (alias["delivery_id"] + ".json")
    kept = final.read_bytes()
    temporary = final.with_name(final.name + ".pending")
    private_bytes(temporary, kept)
    replacement = receipts.ReceiptStore(store.spool)
    headers = replacement._headers()
    assert len(headers) == 2
    assert sum(header["_reserved_bytes"] for header in headers.values()) == primary["_reserved_bytes"] + alias["_reserved_bytes"]
    assert not temporary.exists() and final.read_bytes() == kept
    restored, created = accept(replacement, b"alias", accepted_at=999)
    assert created and restored == alias and len(replacement._headers()) == 2


def test_conflicting_ordinary_alias_pending_is_not_moved_over_final_or_intent(store):
    primary, _ = accept(store)
    alias = receipts.accepted_record(b"alias", primary["task"], strategy="unused", revision="unused",
                                     accepted_at=123.5, primary=primary)
    store._prepare_alias(alias)
    changed = dict(alias, accepted_at=223.5)
    changed["_reserved_bytes"] = receipts._reservation(changed)
    final = store.directory / (alias["delivery_id"] + ".json")
    temporary = final.with_name(final.name + ".pending")
    private_bytes(temporary, receipts._encode_record(changed))
    intent = store.intents / (alias["delivery_id"] + ".json")
    kept_pending, kept_intent = temporary.read_bytes(), intent.read_bytes()
    replacement = receipts.ReceiptStore(store.spool)
    with pytest.raises(receipts.ReceiptConflict):
        replacement.load(alias["delivery_id"])
    assert not final.exists() and temporary.read_bytes() == kept_pending and intent.read_bytes() == kept_intent


@pytest.mark.parametrize("case", ["missing-primary", "changed-index", "partial-primary-outcome"])
def test_intent_does_not_create_or_reopen_primary_when_existing_primary_is_unsafe(store, case):
    primary, _ = accept(store)
    alias = receipts.accepted_record(b"alias", primary["task"], strategy="unused", revision="unused",
                                     accepted_at=123.5, primary=primary)
    store._prepare_alias(alias)
    primary_path = store.directory / (primary["delivery_id"] + ".json")
    if case == "missing-primary":
        primary_path.rename(store.spool / "retained-primary")
    elif case == "changed-index":
        index = store.tasks / (primary["task_digest"] + ".json")
        value = json.loads(index.read_bytes())
        value["primary_delivery_id"] = "f" * 64
        index.write_bytes(receipts._json(value))
    else:
        outcome = dict(primary, status="terminal", completion={"synthetic": True})
        private_bytes(primary_path.with_name(primary_path.name + ".pending"), receipts._encode_record(outcome)[:1])
    with pytest.raises((receipts.ReceiptError, OSError)):
        accept(receipts.ReceiptStore(store.spool), b"alias", accepted_at=999)
    assert not (store.directory / (alias["delivery_id"] + ".json")).exists()


_RESTART_SCRIPT = r'''
import errno, hashlib, json, os, pathlib, sys
sys.path.insert(0, sys.argv[1])
from services import webhook_receipts as r
spool, phase, namespace, outcome = pathlib.Path(sys.argv[2]), sys.argv[3], sys.argv[4], sys.argv[5]
task = {"feed_url":"synthetic", "entries":[]}
store = r.ReceiptStore(spool)
if phase == "interrupt":
    spool.mkdir(mode=0o700)
    primary, _ = store.accept(b"primary", task, strategy="s", revision="r", accepted_at=111)
    store.update_status(primary["delivery_id"], status=outcome, completion={"synthetic":True})
    original = store._write
    def write(path, payload):
        directory = store.intents if namespace == "intent" else store.directory
        if path.parent == directory and path.name == hashlib.sha256(b"alias").hexdigest()+".json":
            pending = path.with_name(path.name+".pending")
            fd=os.open(pending, os.O_CREAT|os.O_EXCL|os.O_WRONLY, 0o600)
            with os.fdopen(fd,"wb") as f:
                f.write(payload[:1]);f.flush();os.fsync(f.fileno())
            raise OSError(errno.EIO,"synthetic alias interruption")
        return original(path,payload)
    store._write=write
    try:
        store.accept(b"alias",task,strategy="new",revision="new",accepted_at=222)
    except OSError:
        os._exit(73)
    raise AssertionError("expected interrupted acceptance")
delivery=hashlib.sha256(b"alias").hexdigest()
directory = store.intents if namespace == "intent" else store.directory
partial=directory/(delivery+".json.pending")
assert partial.read_bytes()==b"{"
try:
    store.load(delivery)
except r.ReceiptError:
    pass
else:
    raise AssertionError("partial alias acknowledged")
new,created=store.accept(b"normal",{"entries":[2]},strategy="s",revision="r",accepted_at=333)
assert created and new["durable"]
primary=store.find_task(hashlib.sha256(r._json(task)).hexdigest())
assert primary["status"]==outcome and primary["accepted_at"]==111
assert partial.read_bytes()==b"{"
print(json.dumps({"new_accepted":True,"primary_outcome":outcome,"partial_unchanged":True,
                  "receipt_count":len(store._headers())}))
'''


@pytest.mark.parametrize("namespace", ["intent", "receipt"])
@pytest.mark.parametrize("outcome", ["completed", "terminal"])
def test_abrupt_exit_and_new_process_keep_partial_alias_held_without_reopening_primary(tmp_path, namespace, outcome):
    source = Path(__file__).resolve().parents[1]
    spool = tmp_path / "restart-spool"
    command = [sys.executable, "-I", "-B", "-c", _RESTART_SCRIPT, str(source), str(spool)]
    interrupted = subprocess.run([*command, "interrupt", namespace, outcome], capture_output=True, timeout=20)
    assert interrupted.returncode == 73, interrupted.stderr.decode()
    recovered = subprocess.run([*command, "recover", namespace, outcome], capture_output=True, timeout=20)
    assert recovered.returncode == 0, recovered.stderr.decode()
    assert json.loads(recovered.stdout) == {"new_accepted": True, "primary_outcome": outcome,
                                          "partial_unchanged": True, "receipt_count": 3}
