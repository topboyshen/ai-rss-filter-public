"""Image-internal synthetic capacity probe; no network, worker or real model.

Completed fixture prepopulation uses the actual store encoder/write/index after
validation, bypassing admission's capacity scan only while constructing known
synthetic fixtures. All measured acceptance/query requests use the stock Flask
routes. Alias failure cases inject EIO only at their named receipt write or
intent-directory synchronization cutpoint; they retain the actual written
bytes. This is not a physical storage fault or power-loss test. Each invocation
is a separate container process.
"""
from __future__ import annotations

import argparse
import base64
import concurrent.futures
from contextlib import contextmanager
import errno
import hashlib
import hmac
import importlib
import json
import os
from pathlib import Path
import resource
import socket
import stat
import sys
import time
import traceback
import tracemalloc

SECRET = "native-capacity-synthetic-signing-key-not-production"
FEED = "https://example.invalid/native-capacity"
CASES = ("allocation", "gradient", "bytes", "maxbody", "partial", "restart", "enospc",
         "alias", "alias-restart", "alias-sync", "alias-capacity", "alias-intent", "alias-intent-restart",
         "alias-inventory")


def digest(data):
    return hashlib.sha256(data).hexdigest()


def require(condition, message):
    if not condition:
        raise AssertionError(message)


def emit(event, **values):
    print(json.dumps({"event": event, **values}, sort_keys=True), flush=True)


def private_write(path, data):
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(fd, "wb") as stream:
        stream.write(data)
        stream.flush()
        os.fsync(stream.fileno())
    fd = os.open(path.parent, os.O_RDONLY | os.O_DIRECTORY)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def counters():
    memory = {}
    for name in ("memory.current", "memory.peak", "memory.max", "memory.events", "pids.max"):
        path = Path("/sys/fs/cgroup") / name
        memory[name] = path.read_text().strip() if path.exists() else None
    for line in Path("/proc/self/status").read_text().splitlines():
        if line.startswith(("VmRSS:", "VmHWM:")):
            key, value = line.split(":", 1)
            memory[key] = value.strip()
    usage = resource.getrusage(resource.RUSAGE_SELF)
    return {"memory": memory, "maxrss_kib": usage.ru_maxrss,
            "user_seconds": usage.ru_utime, "system_seconds": usage.ru_stime}


def raw_body(number, padding=0):
    payload = {"event_type": "new_entries", "feed": {"id": 7, "user_id": 3, "feed_url": FEED},
               "entries": [{"id": number, "hash": f"synthetic-{number}",
                            "title": f"Synthetic capacity {number}",
                            "url": f"https://example.invalid/article/{number}",
                            "content": "Synthetic capacity fixture, no scoring"}]}
    if padding:
        payload["padding"] = "x" * padding
    return json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()


def setup(args):
    state = Path("/state") / args.case
    state.mkdir(mode=0o700, exist_ok=False)
    spool = Path("/state/partial/spool") if args.case == "restart" else state / "spool"
    if args.case in {"alias-restart", "alias-intent-restart"}:
        spool = Path("/state") / args.case.removesuffix("-restart") / "spool"
    if args.case == "enospc":
        spool = Path("/limited/spool")
    expected = json.loads(args.source_hashes)
    for name, value in expected.items():
        require(digest((Path("/app") / name).read_bytes()) == value, "image source hash mismatch: " + name)
    require(os.environ.get("AI_FILTER_REVISION") == args.revision, "image revision mismatch")
    config = {"feeds": [{"url": FEED, "name": "Synthetic"}], "few_shot_feedback": {"enabled": False},
              "telegram": {"enabled": False}, "score_evidence_capture": {"enabled": False},
              "first_rejection_retention": {"enabled": False}}
    private_write(state / "config.yaml", json.dumps(config).encode())
    os.environ.update(AI_FILTER_CONFIG_PATH=str(state / "config.yaml"), AI_FILTER_ROLE="web",
                      AI_FILTER_DB_PATH=str(state / "synthetic.db"), WEBHOOK_SPOOL_DIR=str(spool),
                      MINIFLUX_WEBHOOK_SECRET=SECRET)

    def forbidden(*_args, **_kwargs):
        raise AssertionError("network/model/processing forbidden in capacity probe")

    socket.socket.connect = forbidden
    socket.socket.connect_ex = forbidden
    socket.create_connection = forbidden
    sys.path.insert(0, "/app")
    webhook = importlib.import_module("routes.webhook_routes")
    webhook.process_entry = forbidden
    webhook.Scorer = forbidden
    app = importlib.import_module("app").app
    app.config["TESTING"] = True
    require(not webhook.webhook_dispatcher_alive(), "dispatcher unexpectedly active")
    module = importlib.import_module("services.webhook_receipts")
    return state, spool, webhook, app, module


def normalize(webhook, raw):
    payload = json.loads(raw)
    return {"feed_url": webhook.DB.canonical_feed_url(webhook._cfg(), payload["feed"]["feed_url"]),
            "entries": webhook._normalize_webhook_entries(payload["entries"], webhook._cfg())}


def request(app, raw, path="/webhook"):
    signature = hmac.new(SECRET.encode(), raw, hashlib.sha256).hexdigest()
    start = time.monotonic()
    with app.test_client() as client:
        response = client.post(path, data=raw, content_type="application/json",
                               headers={"X-Miniflux-Signature": signature})
        payload = response.get_json()
    require(isinstance(payload, dict), "non-JSON route response")
    if response.status_code in (200, 202):
        require(payload.get("durable") is True and payload.get("delivery_id") == digest(raw)
                and payload.get("body_sha256") == digest(raw), "unbound durable response")
    else:
        require(not payload.get("durable"), "failure advertised durable")
    return {"status": response.status_code, "elapsed_seconds": time.monotonic() - start,
            "delivery_id": digest(raw), "durable": payload.get("durable", False),
            "receipt_state": payload.get("state")}


def timed(label, operation):
    start = time.monotonic()
    result = operation()
    value = {"label": label, "elapsed_seconds": time.monotonic() - start,
             "result": result, "resources": counters()}
    emit("measurement", **value)
    return value


def census(spool, module):
    count, total, actual = 0, 0, 0
    for path in (spool / "receipts").glob("*.json"):
        count += 1
        total += module._head(path)["_reserved_bytes"]
        actual += path.stat().st_size
    return {"records": count, "reserved_bytes": total, "actual_receipt_bytes": actual,
            "queued_files": sum(1 for _ in spool.glob("*.json"))}


def fixture(webhook, module, raw, revision):
    ident = digest(raw)
    task = normalize(webhook, raw)
    record = {"schema": 1, "durable": True, "delivery_id": ident, "body_sha256": ident,
              "task_digest": webhook._task_digest(task), "primary_delivery_id": ident,
              "accepted_strategy": webhook._delivery_strategy(), "accepted_revision": revision,
              "accepted_at": 1792627200.0, "status": "completed",
              "completion": {"synthetic_storage_fixture": True},
              "raw_body_b64": base64.b64encode(raw).decode(), "task": task}
    record["_reserved_bytes"] = module._reservation(record)
    module._validate(record, ident)
    return record


def seed(webhook, module, spool, revision, *, target=None, near_bytes=False):
    start = time.monotonic()
    with webhook._locked_spool():
        store = module.ReceiptStore(spool)
        store._directories(create=True)
        current = census(spool, module)
        count, total = current["records"], current["reserved_bytes"]
        while count < (target if target is not None else module.MAX_RECORDS):
            padding = 65536 if near_bytes else 0
            raw = raw_body(100000 + count, padding)
            record = fixture(webhook, module, raw, revision)
            if total + record["_reserved_bytes"] > module.MAX_BYTES and near_bytes:
                record = fixture(webhook, module, raw_body(100000 + count), revision)
            if total + record["_reserved_bytes"] > module.MAX_BYTES:
                require(near_bytes, "record fixture hit byte cap unexpectedly")
                break
            store._write(store.directory / (record["delivery_id"] + ".json"), module._encode_record(record))
            store._index(record)
            count += 1
            total += record["_reserved_bytes"]
            require(time.monotonic() - start < 180, "fixture construction exceeded 180 seconds")
    emit("fixture", elapsed_seconds=time.monotonic() - start, census=census(spool, module),
         bypass="prepopulation skips admission scan only; validate/write/index are stock")


def concurrent_same(app, raw):
    with concurrent.futures.ThreadPoolExecutor(max_workers=4) as pool:
        return list(pool.map(lambda _n: request(app, raw), range(4)))


def known_paths(app, raw, prefix):
    duplicate = timed(prefix + "_duplicate", lambda: request(app, raw))
    query = timed(prefix + "_signed_query", lambda: request(app, raw, "/webhook/receipt"))
    require(duplicate["result"]["status"] == 202, "known duplicate rejected")
    require(query["result"]["status"] == 200, "known signed query rejected")
    return [duplicate, query]


def rejected_capacity(app, raw, before, spool, module):
    after = census(spool, module)
    for key in ("records", "reserved_bytes", "actual_receipt_bytes"):
        require(after[key] == before[key], "capacity failure mutated accepted receipts")
    require(after["queued_files"] == before["queued_files"] + 1,
            "failed admission must preserve exactly one unconfirmed queue item")
    query = request(app, raw, "/webhook/receipt")
    require(query["status"] in (404, 503) and not query["durable"], "failed admission got a durable query")
    return {"query": query, "queue_preserved_unconfirmed": True}


def gradient(args, spool, webhook, app, module):
    sentinel = raw_body(1)
    results = [timed("0_new", lambda: request(app, sentinel))]
    require(results[0]["result"]["status"] == 202, "empty admission failed")
    results += known_paths(app, sentinel, "1")
    for target, number in ((1024, 2), (4095, 3)):
        seed(webhook, module, spool, args.revision, target=target)
        before = census(spool, module)
        result = timed(f"{target}_concurrent_new", lambda: concurrent_same(app, raw_body(number)))
        require(all(row["status"] == 202 for row in result["result"]), "concurrent delivery rejected")
        after = census(spool, module)
        require(after["records"] == before["records"] + 1, "duplicate created extra receipt")
        require(after["queued_files"] == before["queued_files"] + 1, "duplicate created extra queue item")
        results.append(result)
        results += known_paths(app, sentinel, str(target + 1))
    require(census(spool, module)["records"] == 4096, "record cap fixture mismatch")
    before = census(spool, module)
    result = timed("4096_new", lambda: request(app, raw_body(4)))
    require(result["result"]["status"] == 503, "record cap falsely acknowledged")
    unconfirmed = rejected_capacity(app, raw_body(4), before, spool, module)
    return {"measurements": results + [result] + known_paths(app, sentinel, "4096_after_rejection"),
            "final": census(spool, module), "unconfirmed": unconfirmed}


def near_bytes(args, spool, webhook, app, module):
    raw = raw_body(10)
    require(request(app, raw)["status"] == 202, "byte baseline admission failed")
    seed(webhook, module, spool, args.revision, near_bytes=True)
    before = census(spool, module)
    require(before["records"] < 4096 and before["reserved_bytes"] > module.MAX_BYTES * .99,
            "byte cap must be independent of record cap and above99percent")
    result = timed("near_byte_cap_new", lambda: request(app, raw_body(11, 65536)))
    require(result["result"]["status"] == 503, "byte cap falsely acknowledged")
    unconfirmed = rejected_capacity(app, raw_body(11, 65536), before, spool, module)
    return {"measurements": [result] + known_paths(app, raw, "near_byte_cap"), "final": before,
            "unconfirmed": unconfirmed}


def maxbody(spool, webhook, app, module):
    raw = raw_body(40, 1)
    raw = raw_body(40, 1024 * 1024 - len(raw) + 1)
    require(len(raw) == 1024 * 1024, "maximum body fixture size mismatch")
    accepted = timed("one_MiB_raw_body", lambda: request(app, raw))
    require(accepted["result"]["status"] == 202, "maximum normal request rejected")
    results = [accepted] + known_paths(app, raw, "one_MiB")
    with webhook._locked_spool():
        store = module.ReceiptStore(spool)
        path = store.directory / "oversized-read-control.bin"
        private_write(path, b"x" * (8 * 1024 * 1024 + 1))
    tracemalloc.start()
    rejected = False
    try:
        module._read(path)
    except module.ReceiptError:
        rejected = True
    _current, peak = tracemalloc.get_traced_memory()
    tracemalloc.stop()
    require(rejected, "oversized per-record file not rejected before read/decode")
    require(peak < 1024 * 1024, "oversized read allocated the body before rejection")
    return {"measurements": results, "oversized_record_rejected": rejected,
            "oversized_read_tracemalloc_peak_bytes": peak}


def allocation(spool, webhook, module):
    with webhook._locked_spool():
        store = module.ReceiptStore(spool)
        store._directories(create=True)
        path = store.directory / "allocation-fixture.bin"
        private_write(path, b"x" * 1177)
    tracemalloc.start()
    data = module._read(path)
    current, peak = tracemalloc.get_traced_memory()
    tracemalloc.stop()
    require(data == b"x" * 1177, "read control changed bytes")
    emit("allocation_control", returned_bytes=len(data), tracemalloc_current_bytes=current,
         tracemalloc_peak_bytes=peak, resources=counters())
    require(peak < 1024 * 1024, "tiny receipt read allocated an oversized buffer")
    return {"returned_bytes": len(data), "tracemalloc_current_bytes": current,
            "tracemalloc_peak_bytes": peak, "resources": counters(),
            "meaning": "actual image _read allocation, separate from native OOM outcome"}


def preserve_failure_fixture(case, spool):
    """Copy only small synthetic failure evidence, not the capacity corpus."""
    if case not in {"partial", "restart", "enospc"} and not case.startswith("alias"):
        return None
    destination = Path("/state") / ("artifacts-" + case)
    destination.mkdir(mode=0o700, exist_ok=False)
    rows, total = [], 0
    alias_names = None
    if case in {"alias-capacity", "alias-inventory"}:
        manifest = json.loads((spool.parent / "alias.json").read_text())
        alias_names = (manifest["primary_id"], manifest["alias_id"], manifest["task_digest"])
    for path in sorted(spool.rglob("*")):
        info = path.lstat()
        if stat.S_ISDIR(info.st_mode):
            continue
        if alias_names and path.parent != spool and not any(value in path.name for value in alias_names):
            continue
        require(stat.S_ISREG(info.st_mode) and info.st_nlink == 1, "unsafe synthetic evidence file")
        require(info.st_size <= 65536 and len(rows) < 100, "synthetic failure evidence cap exceeded")
        data = path.read_bytes()
        total += len(data)
        require(total <= 4 * 1024 * 1024, "synthetic failure evidence total cap exceeded")
        after = path.stat()
        require((info.st_ino, info.st_size, info.st_mtime_ns) ==
                (after.st_ino, after.st_size, after.st_mtime_ns), "failure evidence changed during copy")
        relative = path.relative_to(spool)
        target = destination / relative
        target.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        private_write(target, data)
        require(digest(target.read_bytes()) == digest(data), "failure fixture copy mismatch")
        rows.append({"path": relative.as_posix(), "bytes": len(data), "sha256": digest(data)})
    manifest = {"status": "passed", "files": rows, "total_bytes": total, "synthetic_only": True}
    private_write(destination / "MANIFEST.json", json.dumps(manifest, indent=2).encode())
    return manifest


def alias_manifest(state, case):
    parent = state.parent / case.removesuffix("-restart") if case.endswith("-restart") else state
    return parent / "alias.json"


def prepare_completed_primary(state, spool, webhook, app, module, case):
    primary = raw_body(50)
    alias = json.dumps(json.loads(primary), indent=1).encode()
    require(primary != alias and normalize(webhook, primary) == normalize(webhook, alias),
            "alias must change raw bytes but not normalized task")
    path = spool / "receipts" / (digest(primary) + ".json")
    if not case.endswith("-restart"):
        require(request(app, primary)["status"] == 202, "primary admission failed")
        with webhook._locked_spool():
            module.ReceiptStore(spool).update_status(digest(primary), status="completed",
                completion={"synthetic_completed_fixture": True})
        record = json.loads(path.read_bytes())
        manifest = {"primary_id": digest(primary), "alias_id": digest(alias),
                    "primary_sha256": digest(path.read_bytes()), "task_digest": record["task_digest"]}
        private_write(alias_manifest(state, case), json.dumps(manifest, sort_keys=True).encode())
    manifest = json.loads(alias_manifest(state, case).read_text())
    require(digest(path.read_bytes()) == manifest["primary_sha256"], "primary completed bytes changed")
    return primary, alias, manifest


@contextmanager
def interrupt_alias_write(module, alias, *, intent=False):
    """An explicit injected EIO after a real partial write, not a fake ACK."""
    original = module.ReceiptStore._write
    observed = {}

    def interrupted(store, path, payload):
        parent = store.directory / "alias-intents" if intent else store.directory
        if path.parent == parent and path.name == digest(alias) + ".json":
            pending = path.with_name(path.name + ".pending")
            fragment = payload[:8] if intent else payload[:payload.index(b"\n") + 80]
            private_write(pending, fragment)
            observed.update(path=str(pending), sha256=digest(fragment), bytes=len(fragment))
            raise OSError(errno.EIO, "injected named alias partial write")
        return original(store, path, payload)

    module.ReceiptStore._write = interrupted
    try:
        yield observed
    finally:
        module.ReceiptStore._write = original


def alias_concurrent_requests(app, primary, alias, unrelated):
    requests = [(alias, "/webhook"), (alias, "/webhook/receipt"),
                (primary, "/webhook"), (unrelated, "/webhook")]
    with concurrent.futures.ThreadPoolExecutor(max_workers=4) as pool:
        return list(pool.map(lambda item: request(app, *item), requests))


def confirm_completed_primary(app, spool, primary, manifest):
    duplicate, query = known_paths(app, primary, "completed_primary")
    require(duplicate["result"]["receipt_state"] == "completed"
            and query["result"]["receipt_state"] == "completed", "primary completion was reset")
    path = spool / "receipts" / (digest(primary) + ".json")
    require(digest(path.read_bytes()) == manifest["primary_sha256"], "completed primary mutated")
    return [duplicate, query]


def alias_partial(args, state, spool, webhook, app, module):
    primary, alias, manifest = prepare_completed_primary(state, spool, webhook, app, module, args.case)
    intent = args.case.startswith("alias-intent")
    parent = spool / "receipts/alias-intents" if intent else spool / "receipts"
    pending = parent / (digest(alias) + ".json.pending")
    if args.case == "alias-capacity":
        seed(webhook, module, spool, args.revision, target=4095)
    if not args.case.endswith("-restart"):
        with interrupt_alias_write(module, alias, intent=intent) as interrupted:
            failed = timed(args.case + "_injected_alias_partial", lambda: request(app, alias))
        require(failed["result"]["status"] == 503 and interrupted, "alias cutpoint was not exercised")
        private_write(state / "ALIAS_INTERRUPTED.json", json.dumps(interrupted).encode())
    original = json.loads((alias_manifest(state, args.case).parent / "ALIAS_INTERRUPTED.json").read_text())
    require(digest(pending.read_bytes()) == original["sha256"], "partial alias evidence changed")
    fresh = raw_body(52 if args.case.endswith("-restart") else 51)
    concurrent = timed(args.case + "_concurrent", lambda: alias_concurrent_requests(app, primary, alias, fresh))
    codes = [row["status"] for row in concurrent["result"]]
    expected = [503, 503, 202, 503 if args.case == "alias-capacity" else 202]
    require(codes == expected, "partial alias concurrency/global isolation failed: " + str(codes))
    require(digest(pending.read_bytes()) == original["sha256"], "partial alias was rewritten")
    with webhook._locked_spool():
        headers = webhook._receipt_store(spool)._headers()
    require(manifest["alias_id"] in headers, "held alias did not retain a capacity slot")
    head = headers[manifest["alias_id"]]
    if intent:
        require(head["primary_delivery_id"] is None and head["task_digest"] is None,
                "partial intent invented a task/primary identity")
        require(head["_reserved_bytes"] == module.MAX_ALIAS_RESERVATION, "partial intent lost worst-case budget")
    else:
        require(head["primary_delivery_id"] == manifest["primary_id"], "held alias primary mismatch")
    if args.case == "alias-capacity":
        require(len(headers) == 4096, "held alias count was double-counted or lost")
    return {"measurements": [concurrent] + confirm_completed_primary(app, spool, primary, manifest),
            "held_slots": len(headers), "alias_reserved_bytes": headers[manifest["alias_id"]]["_reserved_bytes"],
            "partial_sha256": original["sha256"], "completed_primary_unchanged": True,
            "restart_process": args.case.endswith("-restart"), "partial_intent": intent,
            "model_execution": "forbidden; completed primary is an explicit synthetic fixture"}


@contextmanager
def interrupt_intent_sync(module, spool):
    original = module._fsync_directory
    observed = {"failures": 0}

    def interrupted(path):
        if path == spool / "receipts/alias-intents":
            observed["failures"] += 1
            raise OSError(errno.EIO, "injected alias intent namespace sync failure")
        return original(path)

    module._fsync_directory = interrupted
    try:
        yield observed
    finally:
        module._fsync_directory = original


def alias_sync(args, state, spool, webhook, app, module):
    primary, alias, manifest = prepare_completed_primary(state, spool, webhook, app, module, args.case)
    with interrupt_intent_sync(module, spool) as failure:
        failed = timed("alias_intent_sync_failed", lambda: request(app, alias))
        query_failed = timed("alias_intent_query_sync_failed", lambda: request(app, alias, "/webhook/receipt"))
    require(failure["failures"] > 0, "intent synchronization cutpoint did not run")
    require(failed["result"]["status"] == 503 and query_failed["result"]["status"] == 503,
            "unsynced intent falsely acknowledged")
    intent_path = spool / "receipts/alias-intents" / (digest(alias) + ".json")
    original = digest(intent_path.read_bytes())
    before_post = timed("alias_intent_only_query", lambda: request(app, alias, "/webhook/receipt"))
    require(before_post["result"]["status"] == 503, "intent alone was exposed as durable delivery")
    recovered = timed("alias_reconfirmed_after_sync_recovery", lambda: request(app, alias))
    require(recovered["result"]["status"] == 202 and recovered["result"]["receipt_state"] == "completed",
            "complete frozen intent did not safely resume alias acceptance")
    require(digest(intent_path.read_bytes()) == original, "retry rewrote immutable alias intent")
    return {"measurements": [failed, query_failed, before_post, recovered]
            + confirm_completed_primary(app, spool, primary, manifest),
            "intent_sha256": original, "injected_sync_failures": failure["failures"],
            "completed_primary_unchanged": True, "model_execution": "forbidden"}


def seed_alias_inventory(webhook, module, spool, primary, count):
    """Validated synthetic history, not claimed to have arrived through HTTP."""
    started = time.monotonic()
    primary_id = digest(primary)
    task = normalize(webhook, primary)
    with webhook._locked_spool():
        store = module.ReceiptStore(spool)
        original = store.load(primary_id)
        require(original["status"] == "completed", "inventory primary must already be completed")
        intents = hasattr(store, "intents")
        if intents:
            store._intent_directory(create=True)
        reserved = sum(head["_reserved_bytes"] for head in store._headers().values())
        for number in range(1, count + 1):
            raw = primary + b" " * number
            require(normalize(webhook, raw) == task, "alias fixture changed normalized task")
            record = module.accepted_record(raw, task, strategy=original["accepted_strategy"],
                revision=original["accepted_revision"], accepted_at=1792627200.0 + number, primary=original)
            module._validate(record, digest(raw))
            reserved += record["_reserved_bytes"]
            require(reserved <= module.MAX_BYTES and number + 1 <= module.MAX_RECORDS,
                    "alias prepopulation exceeded actual image capacity")
            if intents:
                store._validate_intent(record, digest(raw))
                store._write(store.intents / (digest(raw) + ".json"), module._encode_record(record))
            store._write(store.directory / (digest(raw) + ".json"), module._encode_record(record))
            require(time.monotonic() - started < 180, "alias fixture construction exceeded bounded phase")
    result = {"alias_count": count, "with_intents": intents, "reserved_bytes": reserved,
              "elapsed_seconds": time.monotonic() - started, "resources": counters(),
              "construction": "actual accepted_record/validate/write; prepopulation skips admission scan; not HTTP history"}
    emit("alias_inventory_fixture", **result)
    return result


def alias_inventory(args, state, spool, webhook, app, module, *, count=1024):
    primary, _alias, manifest = prepare_completed_primary(state, spool, webhook, app, module, args.case)
    fixture_result = seed_alias_inventory(webhook, module, spool, primary, count)
    first, last = primary + b" ", primary + b" " * count
    before = census(spool, module)
    require(before["records"] == count + 1, "alias inventory count mismatch")
    requests = [(first, "/webhook"), (last, "/webhook/receipt"),
                (raw_body(60), "/webhook"), (primary, "/webhook")]

    def simultaneous():
        with concurrent.futures.ThreadPoolExecutor(max_workers=4) as pool:
            return list(pool.map(lambda item: request(app, *item), requests))

    measurement = timed("alias_inventory_concurrent", simultaneous)
    results = measurement["result"]
    require([row["status"] for row in results] == [202, 200, 202, 202],
            "large alias inventory blocked duplicate/query/unrelated request")
    require(all(results[index]["receipt_state"] == "completed" for index in (0, 1, 3)),
            "alias inventory changed primary completion")
    after = census(spool, module)
    require(after["records"] == before["records"] + 1 and after["queued_files"] == before["queued_files"] + 1,
            "alias inventory replay created extra receipts or queue tasks")
    intent_count = sum(1 for _ in (spool / "receipts/alias-intents").glob("*.json"))
    require(intent_count == (count if fixture_result["with_intents"] else 0), "alias intent count changed")
    return {"fixture": fixture_result, "measurements": [measurement]
            + confirm_completed_primary(app, spool, primary, manifest), "final": after,
            "intent_count": intent_count, "completed_primary_unchanged": True,
            "model_execution": "forbidden; no native latency threshold added"}


def partial(args, state, spool, webhook, app, module):
    sentinel, damaged = raw_body(20), raw_body(21)
    pending = spool / "receipts" / (digest(damaged) + ".json.pending")
    manifest = state.parent / "partial/partial.json"
    if args.case == "partial":
        require(request(app, sentinel)["status"] == 202, "partial baseline failed")
        record = fixture(webhook, module, damaged, args.revision)
        record.pop("completion")
        record["status"] = "accepted"
        envelope = webhook._new_spool_envelope(record["task"], record["task_digest"],
                                               created_at=record["accepted_at"])
        envelope["receipt_context"] = {"raw_body_b64": record["raw_body_b64"],
            "strategy": record["accepted_strategy"], "revision": record["accepted_revision"],
            "accepted_at": record["accepted_at"]}
        with webhook._locked_spool() as directory:
            webhook._atomic_write(directory, directory / (record["task_digest"] + ".json"),
                                  webhook._canonical_json(envelope))
        payload = module._encode_record(record)
        private_write(pending, payload[:payload.index(b"\n") + 80])
        private_write(manifest, json.dumps({"pending_sha256": digest(pending.read_bytes())}).encode())
    expected = json.loads(manifest.read_text())
    require(digest(pending.read_bytes()) == expected["pending_sha256"], "partial original changed")
    results = known_paths(app, sentinel, args.case + "_unrelated")
    for raw in (damaged, json.dumps(json.loads(damaged), indent=1).encode()):
        result = timed(args.case + "_damaged_or_alias", lambda raw=raw: request(app, raw))
        require(result["result"]["status"] == 503, "partial body or alias falsely acknowledged")
        results.append(result)
    query = timed(args.case + "_damaged_query", lambda: request(app, damaged, "/webhook/receipt"))
    require(query["result"]["status"] == 503, "partial query falsely acknowledged")
    fresh = timed(args.case + "_unrelated_new", lambda: request(app, raw_body(22 if args.case == "partial" else 23)))
    require(fresh["result"]["status"] == 202, "partial globally blocked unrelated admission")
    require(digest(pending.read_bytes()) == expected["pending_sha256"], "partial original not preserved")
    return {"measurements": results + [query, fresh], "partial_sha256": expected["pending_sha256"],
            "restart_process": args.case == "restart", "state_directory": str(state)}


def enospc(spool, app):
    sentinel = raw_body(30)
    require(request(app, sentinel)["status"] == 202, "ENOSPC baseline failed")
    filler = Path("/limited/owned-synthetic-fill")
    written = 0
    with filler.open("xb", buffering=0) as stream:
        os.chmod(filler, 0o600)
        while written <= 20 * 1024 * 1024:
            try:
                written += stream.write(b"f" * 65536)
            except OSError as error:
                require(error.errno == errno.ENOSPC, "unexpected filler error")
                break
        else:
            raise AssertionError("bounded tmpfs did not reach ENOSPC")
    inode = filler.stat().st_ino
    failed = timed("kernel_ENOSPC_new", lambda: request(app, raw_body(31)))
    require(failed["result"]["status"] == 503, "ENOSPC falsely acknowledged")
    require(filler.stat().st_ino == inode, "owned filler changed")
    with filler.open("r+b") as stream:
        stream.truncate(0)
        stream.flush()
        os.fsync(stream.fileno())
    recovered = timed("space_released_new", lambda: request(app, raw_body(32)))
    require(recovered["result"]["status"] == 202, "released space globally blocked unrelated task")
    return {"measurements": [failed, recovered] + known_paths(app, sentinel, "ENOSPC_prior"),
            "kernel_errno": errno.ENOSPC, "filler_bytes": written,
            "scope": "bounded tmpfs capacity, not real disk or hardware power loss"}


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--case", choices=CASES, required=True)
    parser.add_argument("--revision", required=True)
    parser.add_argument("--source-hashes", required=True)
    args = parser.parse_args(argv)
    start = time.monotonic()
    spool = None
    result = {"case": args.case, "revision": args.revision, "model_calls": 0,
              "production_queries": 0, "network": "none plus transport denial"}
    try:
        state, spool, webhook, app, module = setup(args)
        if args.case == "gradient":
            detail = gradient(args, spool, webhook, app, module)
        elif args.case == "bytes":
            detail = near_bytes(args, spool, webhook, app, module)
        elif args.case == "allocation":
            detail = allocation(spool, webhook, module)
        elif args.case == "maxbody":
            detail = maxbody(spool, webhook, app, module)
        elif args.case in {"partial", "restart"}:
            detail = partial(args, state, spool, webhook, app, module)
        elif args.case == "alias-sync":
            detail = alias_sync(args, state, spool, webhook, app, module)
        elif args.case == "alias-inventory":
            detail = alias_inventory(args, state, spool, webhook, app, module)
        elif args.case.startswith("alias"):
            detail = alias_partial(args, state, spool, webhook, app, module)
        else:
            detail = enospc(spool, app)
        require(not webhook.webhook_dispatcher_alive(), "dispatcher unexpectedly active")
        result.update(status="passed", detail=detail)
    except Exception as error:
        result.update(status="failed", error_type=type(error).__name__,
                      error=str(error), traceback=traceback.format_exc())
    if spool is not None:
        try:
            result["evidence_manifest"] = preserve_failure_fixture(args.case, spool)
        except Exception as error:
            result.update(status="failed", evidence_error=str(error))
    result.update(elapsed_seconds=time.monotonic() - start, resources=counters())
    emit("final", **result)
    return 0 if result["status"] == "passed" else 1


if __name__ == "__main__":
    raise SystemExit(main())
