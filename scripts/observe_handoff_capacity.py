"""Bounded capacity observation and one durable stop intent, never resume/evict.

Storage samplers read actual receipt files and PostgreSQL rows. They never call
the recovery-writing receipt HTTP API. A separate, trusted admission adapter
must supply live identity/generation and durable stop receipts; production
admission fencing is NOT implemented by this module.
"""
from __future__ import annotations

import argparse
import fcntl
import hashlib
import importlib.util
import json
import math
import os
from pathlib import Path
import queue
import re
import threading
import time


def _module(name, path):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


ROOT = Path(__file__).resolve().parents[1]
receipt = _module("capacity_receipt_format", ROOT / "services/webhook_receipts.py")
journal_module = _module("capacity_private_journal", ROOT / "scripts/durable_handoff_cutover.py")
RECEIVER_ROWS = 4096
SENDER_ROWS = 5000
CAPACITY_BYTES = 256 * 1024 * 1024
MAX_CHECKS = 48
MAX_SCAN_FILES = 3 * RECEIVER_ROWS + 8
MAX_SCAN_BYTES = CAPACITY_BYTES


class ObservationError(Exception):
    """Uncertain observation requires stop; it is never an empty successful sample."""


def require(condition, reason):
    if not condition:
        raise ObservationError(reason)


def digest(value):
    return hashlib.sha256(journal_module.encoded(value)).hexdigest()


def integer(value, minimum, maximum, name):
    require(type(value) is int and minimum <= value <= maximum, "invalid " + name)
    return value


def bounded(operation, seconds):
    """Bound the caller, not the remote operation. Timeout means UNKNOWN, not cancelled."""
    result = queue.Queue(maxsize=1)

    def call():
        try:
            result.put((True, operation()))
        except Exception as error:
            result.put((False, error))

    threading.Thread(target=call, daemon=True).start()
    try:
        ok, value = result.get(timeout=seconds)
    except queue.Empty as error:
        raise ObservationError("operation deadline; remote completion unknown") from error
    if not ok:
        raise ObservationError("adapter or sampler failed: " + type(value).__name__) from value
    return value


def spool_identity(path):
    """Actual local mount/inode identity; image/contract must be separately live-bound."""
    path = Path(path)
    receipt._private_directory(path)
    info = path.lstat()
    return {"device": info.st_dev, "inode": info.st_ino, "uid": info.st_uid}


def _files(directory, deadline, count):
    receipt._private_directory(directory)
    with os.scandir(directory) as entries:
        for entry in entries:
            require(time.monotonic() <= deadline, "receipt scan deadline")
            count[0] += 1
            require(count[0] <= MAX_SCAN_FILES, "receipt scan file budget")
            yield Path(entry.path)


def _read_record(path, deadline, total):
    require(time.monotonic() <= deadline, "receipt scan deadline")
    require(re.fullmatch(r"[0-9a-f]{64}\.json", path.name) is not None,
            "pending, partial or unknown receipt requires separate recovery")
    raw = receipt._read(path)
    total[0] += len(raw)
    require(total[0] <= MAX_SCAN_BYTES, "receipt scan byte budget")
    record = receipt._validate(journal_module.parsed(raw), path.stem)
    require(receipt._head(path) == {key: record[key] for key in receipt._HEAD_KEYS},
            "receipt header does not match actual validated payload")
    return record


def sample_receiver(spool, expected, *, seconds=3):
    """Stable SH flock + strict bounded reads only; every pending artifact stops sampling.

    No ReceiptStore methods, creation, chmod, synchronization, recovery or eviction.
    It is intentionally more conservative than online partial-write isolation.
    """
    integer(seconds, 1, 5, "receiver sample seconds")
    spool = Path(spool)
    require(spool_identity(spool) == expected, "receiver filesystem identity changed")
    deadline, count, total = time.monotonic() + seconds, [0], [0]
    lock = receipt._open_private(spool / ".lock")
    try:
        fcntl.flock(lock, fcntl.LOCK_SH | fcntl.LOCK_NB)
        return _receiver_locked(spool, expected, deadline, count, total)
    except BlockingIOError as error:
        raise ObservationError("receiver writer lock busy") from error
    finally:
        os.close(lock)


def _receiver_locked(spool, expected, deadline, count, total):
    directory = spool / "receipts"
    records, states, aliases, reserved = {}, {key: 0 for key in ("accepted", "completed", "terminal")}, {}, 0
    for path in _files(directory, deadline, count):
        if path.name in {"tasks", "alias-intents"}:
            receipt._private_directory(path)
            continue
        record = _read_record(path, deadline, total)
        key = record["delivery_id"]
        require(len(records) < RECEIVER_ROWS, "receiver hard row limit exceeded")
        reserved += record["_reserved_bytes"]
        require(reserved <= CAPACITY_BYTES, "receiver hard byte limit exceeded")
        states[record["status"]] += 1
        records[key] = {field: record[field] for field in (
            "delivery_id", "task_digest", "primary_delivery_id", "accepted_strategy", "accepted_revision")}
        if record.get("alias_intent_schema") == 1:
            aliases[key] = digest(record)
    primary = {row["task_digest"]: row for key, row in records.items() if key == row["primary_delivery_id"]}
    require(len(primary) == sum(key == row["primary_delivery_id"] for key, row in records.items()),
            "duplicate primary task identity")
    _check_indices(directory / "tasks", primary, deadline, count, total)
    seen = set()
    intent_dir = directory / "alias-intents"
    if intent_dir.exists() or intent_dir.is_symlink():
        for path in _files(intent_dir, deadline, count):
            value = _read_record(path, deadline, total)
            key = value["delivery_id"]
            require(key in aliases and digest(value) == aliases[key], "alias intent is missing or conflicts")
            seen.add(key)
    require(seen == set(aliases), "alias has no complete intent")
    for row in records.values():
        original = primary.get(row["task_digest"])
        require(original is not None and original["delivery_id"] == row["primary_delivery_id"]
                and all(original[field] == row[field] for field in ("accepted_strategy", "accepted_revision")),
                "receipt primary identity conflict")
    require(time.monotonic() <= deadline and spool_identity(spool) == expected, "receiver sample changed or expired")
    free = os.statvfs(spool)
    return {"records": len(records), "reserved_bytes": reserved, "states": states,
            "physical_free_bytes": free.f_bavail * free.f_frsize,
            "files_read": count[0], "bytes_read": total[0], "filesystem_identity": expected}


def _check_indices(directory, primary, deadline, count, total):
    seen = set()
    for path in _files(directory, deadline, count):
        require(re.fullmatch(r"[0-9a-f]{64}\.json", path.name) is not None, "unknown or pending task index")
        raw = receipt._read(path)
        total[0] += len(raw)
        require(len(raw) <= 2048 and total[0] <= MAX_SCAN_BYTES, "task index size exceeds reservation")
        value = journal_module.parsed(raw)
        row = primary.get(path.stem)
        require(row is not None and value == {"schema": 1, "durable": True,
                "task_digest": path.stem, "primary_delivery_id": row["delivery_id"]}, "task index mismatch")
        seen.add(path.stem)
    require(seen == set(primary), "missing primary task index")


PG_OUTBOX_IDENTITY_SQL = """SELECT current_database(), current_schema(),
 (SELECT system_identifier::text FROM pg_control_system()),
 pg_postmaster_start_time()::text, 'webhook_outbox'::regclass::oid::bigint"""
PG_IDENTITY_SQL = PG_OUTBOX_IDENTITY_SQL + ", 'webhook_legacy_originals'::regclass::oid::bigint"
PG_OUTBOX_SAMPLE_SQL = """SELECT state,count(*),
 coalesce(sum(octet_length(body)+octet_length(target_url)+octet_length(contract)+1024),0),
 coalesce(max(attempts),0),
 count(*) FILTER (WHERE state='acked' AND acked_at IS NULL),
 count(*) FILTER (WHERE state='acked' AND acked_at<=transaction_timestamp()-interval '7 days'),
 count(*) FILTER (WHERE state<>'acked' AND created_at<=transaction_timestamp()-interval '7 days')
 FROM (SELECT state,body,target_url,contract,attempts,acked_at,created_at
       FROM webhook_outbox LIMIT 5001) bounded GROUP BY state"""
PG_SAMPLE_SQL = """SELECT 'outbox'::text, * FROM (""" + PG_OUTBOX_SAMPLE_SQL + """) outbox
 UNION ALL SELECT 'legacy',state,count(*),
 coalesce(sum(octet_length(body)+octet_length(target_url)+octet_length(contract)+1024),0),
 0::bigint,0::bigint,0::bigint,
 count(*) FILTER (WHERE created_at<=transaction_timestamp()-interval '7 days')
 FROM (SELECT state,body,target_url,contract,created_at
       FROM webhook_legacy_originals LIMIT 5001) bounded GROUP BY state"""


def sample_postgres(connect, dsn, expected, *, seconds=3):
    """Real PostgreSQL DB-API (psycopg): repeatable-read READ ONLY, SQL deadline.

    No SQLite substitute, no copying raw bodies, no cleanup. Explicit connection
    factory/DSN only; no environment credentials or default production connection.
    """
    integer(seconds, 1, 3, "PostgreSQL SQL seconds")
    started = time.monotonic()
    connection = connect(dsn, connect_timeout=seconds)
    try:
        connection.autocommit = True
        with connection.cursor() as cursor:
            cursor.execute("BEGIN ISOLATION LEVEL REPEATABLE READ READ ONLY")
            cursor.execute("SET LOCAL statement_timeout = '" + str(seconds * 1000) + "ms'")
            cursor.execute("SET LOCAL lock_timeout = '" + str(seconds * 1000) + "ms'")
            cursor.execute("SHOW transaction_read_only")
            require(cursor.fetchone()[0] == "on", "database transaction not read-only")
            cursor.execute(PG_IDENTITY_SQL)
            actual = dict(zip(("database", "schema", "system_identifier", "postmaster_started_at",
                               "table_oid", "legacy_table_oid"), cursor.fetchone()))
            require(type(actual.get("legacy_table_oid")) is int and actual["legacy_table_oid"] > 0,
                    "legacy original table identity missing")
            require(actual == expected, "actual PostgreSQL identity changed")
            cursor.execute(PG_SAMPLE_SQL)
            result = _postgres_counts(cursor.fetchall())
            cursor.execute("ROLLBACK")
        require(time.monotonic() - started <= 3 * seconds + 1, "PostgreSQL observation wall budget exceeded")
        return {**result, "database_identity": actual, "read_only_transaction": True}
    finally:
        connection.close()


def _outbox_counts(rows):
    states = {key: 0 for key in ("pending", "inflight", "held", "acked")}
    total = charged = expired = eligible = maximum = 0
    seen = set()
    for state, count, size, attempts, missing_ack, old_ack, old_unacked in rows:
        require(state in states and state not in seen, "unknown or duplicate outbox state")
        seen.add(state)
        for value in (count, size, attempts, missing_ack, old_ack, old_unacked):
            require(type(value) is int and value >= 0, "invalid outbox aggregate")
        require(attempts <= 20 and missing_ack == 0, "outbox budget or ACK identity invalid")
        states[state], total, charged = count, total + count, charged + size
        maximum, expired, eligible = max(maximum, attempts), expired + old_unacked, eligible + old_ack
    require(total <= SENDER_ROWS and charged <= CAPACITY_BYTES, "sender hard capacity exceeded")
    return {"rows": total, "charged_bytes": charged, "states": states,
            "max_attempts": maximum, "expired_unacknowledged": expired,
            "acked_at_least_7_days": eligible, "cleanup_credit": 0}


def _postgres_counts(rows):
    """0002 two-table sample only. An old seven-column outbox sample is NOT zero."""
    outbox, legacy = [], {"rows": 0, "charged_bytes": 0, "states": {"held": 0},
                         "older_than_7_days": 0, "cleanup_credit": 0, "forwarding_allowed": False}
    seen = False
    for row in rows:
        require(len(row) == 8, "two-table sender coverage required")
        source, state, count, size, attempts, missing_ack, old_ack, old_unacked = row
        if source == "outbox":
            outbox.append(row[1:])
            continue
        require(source == "legacy" and state == "held" and not seen, "unknown or duplicate legacy state")
        seen = True
        for value in row[2:]:
            require(type(value) is int and value >= 0, "invalid legacy aggregate")
        require(attempts == missing_ack == old_ack == 0 and old_unacked <= count,
                "legacy originals have no send budget or cleanup eligibility")
        legacy.update(rows=count, charged_bytes=size, states={"held": count}, older_than_7_days=old_unacked)
    result = _outbox_counts(outbox)
    total, charged = result["rows"] + legacy["rows"], result["charged_bytes"] + legacy["charged_bytes"]
    require(total <= SENDER_ROWS and charged <= CAPACITY_BYTES, "shared sender hard capacity exceeded")
    return {**result, "coverage": "outbox_and_legacy_v1", "outbox_rows": result["rows"],
            "outbox_charged_bytes": result["charged_bytes"], "legacy": legacy,
            "rows": total, "charged_bytes": charged}


def validate_sender_sample(sender):
    """Do not accept an old/minimal sample as a complete shared-capacity proof."""
    require(isinstance(sender, dict) and sender.get("coverage") == "outbox_and_legacy_v1",
            "two-table sender coverage missing")
    require(set(sender["states"]) == {"pending", "inflight", "held", "acked"}, "invalid outbox states")
    legacy = sender["legacy"]
    require(set(legacy["states"]) == {"held"} and legacy.get("forwarding_allowed") is False
            and legacy.get("cleanup_credit") == 0, "invalid legacy retention boundary")
    for obj, row_key, byte_key in ((sender, "outbox_rows", "outbox_charged_bytes"),
                                   (legacy, "rows", "charged_bytes")):
        rows = integer(obj[row_key], 0, SENDER_ROWS, "sender rows")
        integer(obj[byte_key], 0, CAPACITY_BYTES, "sender bytes")
        require(sum(integer(v, 0, SENDER_ROWS, "state count") for v in obj["states"].values()) == rows,
                "sender states disagree with actual rows")
    require(integer(sender["rows"], 0, SENDER_ROWS, "shared rows") == sender["outbox_rows"] + legacy["rows"]
            and integer(sender["charged_bytes"], 0, CAPACITY_BYTES, "shared bytes")
            == sender["outbox_charged_bytes"] + legacy["charged_bytes"], "shared totals mismatch")
    integer(legacy["older_than_7_days"], 0, legacy["rows"], "legacy old rows")
    return sender


class StorageSampler:
    """Actual local file/PG reads bracketed by the admission adapter's live identity."""

    def __init__(self, adapter, spool, spool_binding, connect, dsn, pg_binding):
        self.adapter, self.spool, self.spool_binding = adapter, spool, spool_binding
        self.connect, self.dsn, self.pg_binding = connect, dsn, pg_binding

    def snapshot(self, identity, generation):
        before = self.adapter.snapshot(identity, generation)
        receiver = sample_receiver(self.spool, self.spool_binding)
        sender = sample_postgres(self.connect, self.dsn, self.pg_binding)
        after = self.adapter.snapshot(identity, generation)
        for item in (before, after):
            require(item.get("identity") == identity and type(item.get("generation")) is int
                    and item.get("generation") == generation,
                    "live adapter identity or generation changed")
            integer(item.get("admission_total"), 0, 2**63 - 1, "admission counter")
        require(after["admission_total"] >= before["admission_total"], "admission counter went backwards")
        return {"identity": identity, "generation": generation, "observed_epoch": time.time(),
                "admission_total": after["admission_total"], "receiver": receiver, "sender": sender}

    def stop(self, operation_id, reason, generation):
        return self.adapter.stop(operation_id, reason, generation)

    def query_operation(self, operation_id):
        return self.adapter.query_operation(operation_id)


def validate_policy(policy):
    require(isinstance(policy, dict) and policy.get("schema") == 1, "invalid observer policy")
    require(isinstance(policy.get("run_id"), str)
            and re.fullmatch(r"[0-9a-f]{32}", policy["run_id"]) is not None, "invalid observer run")
    require(isinstance(policy.get("identity"), dict) and policy["identity"], "live identity binding required")
    require(isinstance(policy.get("adapter_manifest_sha256"), str)
            and re.fullmatch(r"[0-9a-f]{64}", policy["adapter_manifest_sha256"]) is not None,
            "separate adapter manifest hash required")
    integer(policy.get("generation"), 0, 2**63 - 1, "generation")
    integer(policy.get("baseline_admission_total"), 0, 2**63 - 1, "pre-admission baseline counter")
    for key, low, high in (("duration_seconds", 1, 7200), ("interval_seconds", 1, 60),
                          ("max_checks", 1, MAX_CHECKS), ("max_new_deliveries", 1, RECEIVER_ROWS),
                          ("receiver_stop_records", 1, RECEIVER_ROWS),
                          ("sender_stop_rows", 1, SENDER_ROWS),
                          ("receiver_stop_bytes", 1, CAPACITY_BYTES),
                          ("sender_stop_bytes", 1, CAPACITY_BYTES),
                          ("sampling_seconds", 1, 15), ("stop_seconds", 1, 5),
                          ("minimum_free_bytes", receipt.MAX_ALIAS_RESERVATION, 2**63 - 1)):
        integer(policy.get(key), low, high, key)
    return policy


class CapacityObserver:
    """Only an adapter's exact durable receipt confirms stop; never resume/delete."""

    def __init__(self, policy, backend, journal_root, *, clock=time.time, monotonic=time.monotonic):
        self.policy, self.backend, self.clock, self.monotonic = validate_policy(policy), backend, clock, monotonic
        self.journal = journal_module.Journal(Path(journal_root))
        try:
            self.state = self.journal.latest()
            binding = digest(policy)
            if self.state is None:
                started = clock()
                require(type(started) in (int, float) and math.isfinite(started), "initial clock unavailable")
                self.state = self.journal.append(None, {"policy_sha256": binding, "phase": "observing",
                    "started_epoch": started, "expires_epoch": started + policy["duration_seconds"],
                    "checks": 0, "baseline_admission_total": policy["baseline_admission_total"], "last_admission_total": None,
                    "last_observed_epoch": None, "intent": None})
            require(self.state["policy_sha256"] == binding, "observer policy changed across restart")
            self._validate_saved_state()
            self._initialize_deadlines()
        except BaseException:
            self.journal.close()
            raise

    def _initialize_deadlines(self):
        self._deadline_monotonic = None
        self._last_wall_epoch = max(self.state["started_epoch"],
                                    self.state["last_observed_epoch"] or self.state["started_epoch"])
        try:
            wall, mono = self.clock(), self.monotonic()
            require(all(type(value) in (int, float) and math.isfinite(value) for value in (wall, mono)),
                    "clock unavailable")
            require(wall >= self._last_wall_epoch, "clock reversed across restart")
            self._last_wall_epoch = wall
            self._last_monotonic = mono
            remaining = min(self.policy["duration_seconds"], max(0, self.state["expires_epoch"] - wall))
            self._deadline_monotonic = mono + remaining
        except Exception:
            # A saved stop can still be queried; otherwise tick requests one stop.
            return

    def _remaining(self):
        if self._deadline_monotonic is None:
            return 0
        try:
            wall, mono = self.clock(), self.monotonic()
            require(all(type(value) in (int, float) and math.isfinite(value) for value in (wall, mono)),
                    "clock unavailable")
            require(wall >= self._last_wall_epoch and mono >= self._last_monotonic, "clock reversed")
            self._last_wall_epoch, self._last_monotonic = wall, mono
            return max(0, min(self.state["expires_epoch"] - wall, self._deadline_monotonic - mono))
        except Exception:
            self._deadline_monotonic = None
            return 0

    def close(self):
        self.journal.close()

    def _save(self, **changes):
        self.state = self.journal.append(self.state, {**self.state, **changes})

    def _validate_saved_state(self):
        phase = self.state.get("phase")
        require(phase in {"observing", "stop_pending", "stopped"}, "unknown saved phase")
        integer(self.state.get("checks"), 0, self.policy["max_checks"], "saved observation count")
        baseline = integer(self.state.get("baseline_admission_total"), 0, 2**63 - 1, "saved baseline")
        require(baseline == self.policy["baseline_admission_total"], "saved baseline changed")
        start, end = self.state.get("started_epoch"), self.state.get("expires_epoch")
        require(all(type(value) in (int, float) and math.isfinite(value) for value in (start, end))
                and end == start + self.policy["duration_seconds"], "saved time budget changed")
        if phase == "observing":
            require(self.state.get("intent") is None and self.state.get("stop_receipt") is None,
                    "observing state contains stop outcome")
        else:
            self._validate_intent()
        if phase == "stopped":
            self._validate_stop_receipt(self.state.get("stop_receipt"))
        elif phase == "stop_pending":
            require(self.state.get("stop_receipt") is None, "pending state contains confirmed receipt")

    def _validate_intent(self):
        intent = self.state.get("intent")
        require(isinstance(intent, dict) and set(intent) == {"operation_id", "reason"}
                and intent.get("operation_id") == digest({"run_id": self.policy["run_id"], "action": "capacity_stop"})
                and isinstance(intent.get("reason"), str) and 0 < len(intent["reason"]) <= 200,
                "saved stop intent identity invalid")
        return intent

    def _validate_stop_receipt(self, value):
        """Pure binding check shared by new ACK and recovered stopped state."""
        intent = self._validate_intent()
        require(isinstance(value, dict) and value.get("durable") is True
                and value.get("operation_id") == intent["operation_id"]
                and value.get("reason") == intent["reason"]
                and value.get("identity") == self.policy["identity"]
                and type(value.get("previous_generation")) is int
                and type(value.get("generation")) is int
                and value.get("previous_generation") == self.policy["generation"]
                and value.get("generation") == self.policy["generation"] + 1
                and value.get("state") == "closed"
                and value.get("manifest_sha256") == self.policy["adapter_manifest_sha256"],
                "stop receipt missing or does not bind exact operation")

    def _acknowledge(self, value):
        self._validate_stop_receipt(value)
        self._save(phase="stopped", stop_receipt=value)
        intent = self.state["intent"]
        return {"status": "stop_confirmed", "operation_id": intent["operation_id"], "reason": intent["reason"]}

    def _stop(self, reason):
        fresh = self.state["intent"] is None
        if fresh:
            intent = {"operation_id": digest({"run_id": self.policy["run_id"], "action": "capacity_stop"}), "reason": reason}
            self._save(phase="stop_pending", intent=intent)
        intent = self.state["intent"]
        try:
            operation = (lambda: self.backend.stop(intent["operation_id"], intent["reason"], self.policy["generation"])) if fresh else (
                lambda: self.backend.query_operation(intent["operation_id"]))
            value = bounded(operation, self.policy["stop_seconds"])
            return self._acknowledge(value)
        except Exception as error:
            return {"status": "stop_unconfirmed", "operation_id": intent["operation_id"],
                    "reason": intent["reason"], "error_type": type(error).__name__,
                    "remote_operation_may_still_run": True, "must_not_resume": True}

    def tick(self):
        self._validate_saved_state()
        if self.state["phase"] == "stopped":
            return {"status": "stop_confirmed", "operation_id": self.state["intent"]["operation_id"], "reused": True}
        if self.state["intent"] is not None:
            return self._stop(self.state["intent"]["reason"])
        remaining = self._remaining()
        if remaining <= 0:
            return self._stop("trial_time_limit_or_clock_reversal")
        if self.state["checks"] >= self.policy["max_checks"]:
            return self._stop("observation_count_limit")
        try:
            sample = bounded(lambda: self.backend.snapshot(self.policy["identity"], self.policy["generation"]),
                             min(self.policy["sampling_seconds"], remaining))
            reason = self._inspect(sample)
            self._save(checks=self.state["checks"] + 1, last_sample=sample,
                       last_admission_total=sample["admission_total"], last_observed_epoch=sample["observed_epoch"])
        except Exception as error:
            return self._stop("observation_failed_" + type(error).__name__)
        if reason:
            return self._stop(reason)
        return {"status": "observing", "checks": self.state["checks"], "sample_sha256": digest(sample)}

    def _inspect(self, sample):
        now = self.clock()
        require(isinstance(sample, dict) and sample.get("identity") == self.policy["identity"]
                and type(sample.get("generation")) is int
                and sample.get("generation") == self.policy["generation"], "sample identity changed")
        observed = sample.get("observed_epoch")
        require(type(observed) in (int, float) and now - self.policy["sampling_seconds"] <= observed <= now,
                "sample stale, missing or future-dated")
        previous_observed = self.state["last_observed_epoch"]
        require(previous_observed is None or observed >= previous_observed, "sample clock decreased")
        admitted = integer(sample.get("admission_total"), 0, 2**63 - 1, "admission total")
        old = self.state["last_admission_total"]
        require(admitted >= self.state["baseline_admission_total"], "admission total below bound baseline")
        require(old is None or admitted >= old, "admission total decreased")
        receiver, sender = sample["receiver"], sample["sender"]
        for obj, key, maximum in ((receiver, "records", RECEIVER_ROWS), (receiver, "reserved_bytes", CAPACITY_BYTES),
                                  (sender, "rows", SENDER_ROWS), (sender, "charged_bytes", CAPACITY_BYTES)):
            integer(obj.get(key), 0, maximum, key)
        integer(receiver.get("physical_free_bytes"), 0, 2**63 - 1, "physical free bytes")
        validate_sender_sample(sender)
        for obj, names, total in ((receiver, {"accepted", "completed", "terminal"}, receiver["records"]),):
            require(isinstance(obj.get("states"), dict) and set(obj["states"]) == names, "missing storage states")
            require(sum(integer(value, 0, SENDER_ROWS, "state count") for value in obj["states"].values()) == total,
                    "state counts disagree with total")
        integer(sender.get("expired_unacknowledged"), 0, SENDER_ROWS, "expired outbox count")
        if self._remaining() <= 0:
            return "trial_time_limit"
        if admitted - self.state["baseline_admission_total"] >= self.policy["max_new_deliveries"]:
            return "trial_delivery_limit"
        for obj, key, limit in ((receiver, "records", "receiver_stop_records"), (receiver, "reserved_bytes", "receiver_stop_bytes"),
                                (sender, "rows", "sender_stop_rows"), (sender, "charged_bytes", "sender_stop_bytes")):
            if obj[key] >= self.policy[limit]:
                return limit
        if receiver["physical_free_bytes"] < self.policy["minimum_free_bytes"]:
            return "receiver_physical_space_limit"
        if CAPACITY_BYTES - receiver["reserved_bytes"] < receipt.MAX_ALIAS_RESERVATION:
            return "receiver_alias_reservation_headroom"
        if sender["legacy"]["rows"]:
            return "sender_legacy_held_requires_disposition"
        if sender["states"]["held"] or sender["expired_unacknowledged"]:
            return "sender_held_or_expired_requires_disposition"
        return None

    def run(self):
        while True:
            result = self.tick()
            if result["status"] != "observing":
                return result
            delay = min(self.policy["interval_seconds"], self._remaining())
            time.sleep(delay)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--sample-receiver", type=Path)
    parser.add_argument("--receiver-binding", type=Path)
    parser.add_argument("--sample-pg", action="store_true")
    parser.add_argument("--pg-binding", type=Path)
    args = parser.parse_args()
    try:
        require(bool(args.sample_receiver) != args.sample_pg, "choose one explicit read-only sampler")
        if args.sample_receiver:
            result = sample_receiver(args.sample_receiver, journal_module.read_private(args.receiver_binding))
        else:
            import psycopg
            dsn = os.environ.get("HANDOFF_CAPACITY_ISOLATED_PG_DSN", "")
            from urllib.parse import urlsplit
            target = urlsplit(dsn)
            require(target.hostname in {"localhost", "127.0.0.1"} and target.path == "/synthetic_outbox",
                    "CLI PostgreSQL sampler only accepts explicit isolated synthetic database")
            result = sample_postgres(psycopg.connect, dsn, journal_module.read_private(args.pg_binding))
        print(json.dumps({"status": "sampled_not_stop_authorization", "sample": result}, sort_keys=True))
        return 0
    except Exception as error:
        print(json.dumps({"status": "observation_failed", "error_type": type(error).__name__}))
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
