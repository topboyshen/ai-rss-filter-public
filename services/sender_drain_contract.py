"""Pure bounded sender-original bundle contract; no database or process access.

A valid file is not proof of historical coverage. Live sender/PG binding and
actual read-only creation belong to the host driver; receiver must also bind
the frozen manifest and verify the original HMAC/target/strategy contract.
"""
from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import re
import stat
import time

MAX_MANIFEST_BYTES = 8 * 1024**2
MAX_BODY_BYTES = 1024**2
MAX_ROWS = 5000
MAX_BYTES = 256 * 1024**2
HEX64 = re.compile(r"[0-9a-f]{64}\Z")
IDENTITY_KEYS = ("database", "schema", "system_identifier", "postmaster_started_at", "table_oid", "legacy_table_oid",
                 "state_table_oid", "operations_table_oid", "leases_table_oid", "schema_version", "current_user", "session_user")


class DrainContractError(ValueError):
    pass


def require(value, reason):
    if not value:
        raise DrainContractError(reason)


def encoded(value):
    return json.dumps(value, sort_keys=True, separators=(",", ":"), allow_nan=False).encode()


def parsed(raw):
    def pairs(items):
        result = {}
        for key, value in items:
            require(key not in result, "duplicate manifest field")
            result[key] = value
        return result
    return json.loads(raw, object_pairs_hook=pairs, parse_constant=lambda _: require(False, "nonfinite manifest"))


def private_directory(path):
    info = Path(path).lstat()
    require(stat.S_ISDIR(info.st_mode) and info.st_uid == os.geteuid()
            and stat.S_IMODE(info.st_mode) == 0o700, "unsafe original directory")


def source_sha256():
    return hashlib.sha256(Path(__file__).read_bytes()).hexdigest()


def validate_binding(value):
    keys = {"schema", "scope", "run_id", "control_id", "component_name", "component", "manifest",
            "application_source_sha256", "source_sha256", "generation", "initial_operation_id", "entrances"}
    require(isinstance(value, dict) and set(value) == keys and type(value["schema"]) is int and value["schema"] == 1
            and value["scope"] == "instrumented_sender_only", "exact sender binding required")
    for field in ("manifest", "application_source_sha256", "source_sha256", "initial_operation_id"):
        require(isinstance(value[field], str) and HEX64.fullmatch(value[field]), "complete sender source identity required")
    require(isinstance(value["run_id"], str) and re.fullmatch(r"[0-9a-f]{32}", value["run_id"])
            and all(isinstance(value[k], str) and re.fullmatch(r"[a-z][a-z0-9_-]{0,63}", value[k])
                    for k in ("control_id", "component_name"))
            and type(value["generation"]) is int and 0 <= value["generation"] < 2**63 - 2
            and value["entrances"] == ["miniflux_scheduler", "miniflux_manual", "legacy_async_sender"], "invalid sender control identity")
    _component(value["component"])
    return value


def _component(value):
    keys = {"id", "image", "revision", "started_at", "restart_count", "role", "user", "memory", "pids", "mounts"}
    require(isinstance(value, dict) and set(value) == keys and isinstance(value["id"], str) and HEX64.fullmatch(value["id"])
            and isinstance(value["image"], str) and re.fullmatch(r"sha256:[0-9a-f]{64}", value["image"])
            and isinstance(value["revision"], str) and re.fullmatch(r"[0-9a-f]{40}", value["revision"])
            and isinstance(value["started_at"], str) and re.fullmatch(r"\d{4}-\d\d-\d\dT\d\d:\d\d:\d\d(?:\.\d{1,9})?Z", value["started_at"])
            and type(value["restart_count"]) is int and value["restart_count"] >= 0
            and value["role"] == "miniflux" and isinstance(value["user"], str) and re.fullmatch(r"\d+(?::\d+)?", value["user"])
            and type(value["memory"]) is int and 0 < value["memory"] <= 768*1024**2
            and type(value["pids"]) is int and 0 < value["pids"] <= 256, "sender component binding differs")
    require(isinstance(value["mounts"], list) and 1 <= len(value["mounts"]) <= 16, "sender mount binding missing")
    for mount in value["mounts"]:
        require(isinstance(mount, dict) and set(mount) == {"Type", "Source", "Destination", "RW"}
                and mount["Type"] in {"bind", "volume"} and type(mount["RW"]) is bool
                and all(isinstance(mount[k], str) and Path(mount[k]).is_absolute() for k in ("Source", "Destination")),
                "sender mount binding differs")


def validate_stop_receipt(value, binding):
    keys = {"schema", "manifest", "operation_id", "action", "previous_generation", "generation", "state", "durable",
            "confirmed", "active_leases", "uncertain_leases", "active_producer_leases", "uncertain_producer_leases"}
    require(isinstance(value, dict) and set(value) == keys, "complete producer-stop receipt required")
    expected = {"schema": 1, "manifest": binding["manifest"], "action": "stop_new", "previous_generation": binding["generation"],
                "generation": binding["generation"] + 1, "state": "draining", "durable": True, "confirmed": True,
                "active_producer_leases": 0, "uncertain_producer_leases": 0}
    require(all(type(value[k]) is type(v) and value[k] == v for k,v in expected.items())
            and isinstance(value["operation_id"], str) and HEX64.fullmatch(value["operation_id"])
            and value["operation_id"] != binding["initial_operation_id"], "producer stop is unconfirmed or mismatched")
    require(all(type(value[k]) is int and 0 <= value[k] <= 10000 for k in ("active_leases", "uncertain_leases")),
            "invalid retained consumer counters")
    return value


def validate_sample(value):
    require(isinstance(value, dict) and value.get("coverage") == "outbox_and_legacy_v1"
            and isinstance(value.get("states"), dict) and set(value["states"]) == {"pending", "inflight", "held", "acked"},
            "complete original capacity sample required")
    require(type(value.get("cleanup_credit")) is int and value["cleanup_credit"] == 0,
            "outbox originals cannot grant cleanup credit")
    legacy = value.get("legacy")
    require(isinstance(legacy, dict) and legacy.get("forwarding_allowed") is False and legacy.get("cleanup_credit") == 0
            and isinstance(legacy.get("states"), dict) and set(legacy["states"]) == {"held"}, "legacy original boundary differs")
    for obj, rowkey, bytekey in ((value,"outbox_rows","outbox_charged_bytes"),(legacy,"rows","charged_bytes")):
        require(type(obj.get(rowkey)) is int and 0 <= obj[rowkey] <= MAX_ROWS
                and type(obj.get(bytekey)) is int and 0 <= obj[bytekey] <= MAX_BYTES
                and all(type(v) is int and 0 <= v <= MAX_ROWS for v in obj["states"].values())
                and sum(obj["states"].values()) == obj[rowkey], "invalid original row/byte totals")
    require(type(value.get("rows")) is int and value["rows"] == value["outbox_rows"] + legacy["rows"] <= MAX_ROWS
            and type(value.get("charged_bytes")) is int and value["charged_bytes"] == value["outbox_charged_bytes"] + legacy["charged_bytes"] <= MAX_BYTES
            and type(value.get("expired_unacknowledged")) is int and value["expired_unacknowledged"] >= 0, "shared original capacity exceeded")
    return value


def digest(value):
    return hashlib.sha256(encoded(value)).hexdigest()


def validate_identity(value):
    require(isinstance(value, dict) and set(value) == set(IDENTITY_KEYS), "complete sender PostgreSQL identity required")
    require(type(value["schema_version"]) is int and value["schema_version"] in (134, 135), "sender schema 134 or 135 required")
    for key in IDENTITY_KEYS:
        if key.endswith("_oid"):
            require(type(value[key]) is int and 0 < value[key] < 2**32, "invalid sender table OID")
        elif key != "schema_version":
            require(isinstance(value[key], str) and 0 < len(value[key]) <= 256, "invalid PostgreSQL identity")
    return value


def validate_entry(row):
    keys = {"delivery_id", "body_sha256", "body_size", "body_file", "target_url", "signature", "contract",
            "user_id", "feed_id", "entry_id", "state", "attempts"}
    require(isinstance(row, dict) and set(row) == keys, "complete original identity required")
    for key in ("delivery_id", "body_sha256", "signature"):
        require(isinstance(row[key], str) and HEX64.fullmatch(row[key]), "invalid original hash/signature")
    require(row["delivery_id"] == row["body_sha256"] and row["body_file"] == row["delivery_id"] + ".body",
            "original body identity differs")
    require(type(row["body_size"]) is int and 1 <= row["body_size"] <= MAX_BODY_BYTES,
            "original body exceeds existing sender bound")
    for key in ("user_id", "feed_id", "entry_id"):
        require(type(row[key]) is int and 0 < row[key] < 2**63, "invalid original business identity")
    require(row["state"] in {"pending", "inflight"} and type(row["attempts"]) is int and 0 <= row["attempts"] < 20,
            "held/exhausted/unknown original cannot enter drain manifest")
    for key in ("target_url", "contract"):
        require(isinstance(row[key], str) and 0 < len(row[key].encode()) <= 8192, "invalid original target/contract")
    return row


def validate_manifest(value, *, check_window=True):
    keys = {"schema", "kind", "sender_binding", "postgres_identity", "control_receipt", "window", "source_sha256",
            "inventory_summary", "retained_lease_budget", "entries", "legacy_original_coverage", "queue_checked", "switch_authorized"}
    require(isinstance(value, dict) and set(value) == keys and type(value["schema"]) is int and value["schema"] == 1
            and value["kind"] == "miniflux-durable-drain-v1" and value["source_sha256"] == source_sha256(),
            "exact source-bound original manifest required")
    binding = validate_binding(value["sender_binding"])
    validate_identity(value["postgres_identity"])
    validate_stop_receipt(value["control_receipt"], binding)
    window = value["window"]
    require(isinstance(window, dict) and set(window) == {"not_before", "expires_at"}
            and all(type(v) is int for v in window.values()) and 0 < window["expires_at"] - window["not_before"] <= 7200,
            "finite manifest window required")
    if check_window:
        require(window["not_before"] <= time.time() < window["expires_at"], "original manifest window expired")
    sample = validate_sample(value["inventory_summary"])
    require(sample["database_identity"] == value["postgres_identity"] and sample["read_only_transaction"] is True,
            "original sample database differs")
    require(sample["states"]["held"] == sample["legacy"]["rows"] == sample["expired_unacknowledged"] == 0,
            "held/legacy/expired originals block this drain")
    entries = value["entries"]
    require(isinstance(entries, list) and len(entries) <= MAX_ROWS
            and len(entries) == sample["states"]["pending"] + sample["states"]["inflight"], "manifest does not cover pending originals")
    ids = [validate_entry(row)["delivery_id"] for row in entries]
    require(ids == sorted(set(ids)) and sum(r["body_size"] for r in entries) <= MAX_BYTES,
            "duplicate, unsorted or oversized originals")
    budget = value["retained_lease_budget"]
    require(isinstance(budget, dict) and set(budget) == {"records", "limit", "remaining"}
            and all(type(v) is int for v in budget.values()) and budget["limit"] == 10000
            and 0 <= budget["records"] <= 10000 and budget["remaining"] == 10000 - budget["records"],
            "exact finite retained lease budget required")
    require(budget["remaining"] >= 2 * len(entries), "insufficient worst-case first-pass lease capacity; abort window")
    require(value["legacy_original_coverage"] == "registered_paths_since_instrumentation_only"
            and value["queue_checked"] is False and value["switch_authorized"] is False, "manifest is not history or switch proof")
    require(len(encoded(value)) <= MAX_MANIFEST_BYTES, "original manifest byte limit")
    return value


def read_blob(path, maximum):
    fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW)
    try:
        before = os.fstat(fd)
        require(stat.S_ISREG(before.st_mode) and before.st_uid == os.geteuid() and before.st_nlink == 1
                and stat.S_IMODE(before.st_mode) == 0o600 and before.st_size <= maximum, "unsafe original file")
        chunks, total = [], 0
        while chunk := os.read(fd, min(65536, maximum + 1 - total)):
            total += len(chunk)
            require(total <= maximum, "original file over budget")
            chunks.append(chunk)
        after = os.fstat(fd)
        require((before.st_ino, before.st_dev, before.st_size, before.st_mtime_ns)
                == (after.st_ino, after.st_dev, after.st_size, after.st_mtime_ns)
                and os.stat(path, follow_symlinks=False).st_ino == before.st_ino and total == before.st_size,
                "original file changed")
        return b"".join(chunks)
    finally:
        os.close(fd)


def read_original(root, row):
    validate_entry(row)
    private_directory(Path(root))
    raw = read_blob(Path(root) / row["body_file"], MAX_BODY_BYTES)
    require(len(raw) == row["body_size"] and hashlib.sha256(raw).hexdigest() == row["body_sha256"], "exact original bytes changed")
    return raw


def load_bundle(root, *, check_window=True):
    root = Path(root)
    private_directory(root)
    value = validate_manifest(parsed(read_blob(root / "manifest.json", MAX_MANIFEST_BYTES)), check_window=check_window)
    names = {"manifest.json", *[r["body_file"] for r in value["entries"]]}
    with os.scandir(root) as files:
        actual = set()
        for row in files:
            require(len(actual) <= MAX_ROWS + 1, "bundle file budget")
            actual.add(row.name)
    require(actual == names, "partial or unknown bundle file; preserve and abort")
    for row in value["entries"]:
        read_original(root, row)
    return value, digest(value)
