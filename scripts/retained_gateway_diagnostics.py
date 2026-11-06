"""Optional synthetic-only socket diagnostics; never an authority or retry policy.

Raw originals stay in new private /tmp directories OUTSIDE fixture/upload roots.
Only bounded typed counts, conditions and hashes may enter public evidence.
"""
from contextlib import contextmanager
from contextvars import ContextVar
import hashlib
import json
import os
from pathlib import Path
import re
import tempfile
import threading
import time

READ_BYTES = 512 * 1024
PUBLIC_BYTES = 4096
PRIVATE_BYTES = 8 * 1024**2
PRIVATE_EVENTS = 4
STAGES = {"initial_listener", "initial_confirm", "peer_confirm", "final_confirm", "unspecified"}
STATES = {"complete", "partial", "unavailable", "not_started"}
RETENTION = {"retained_privately", "deadline_exhausted", "capacity_exhausted", "write_failed", "not_attempted"}
COUNTS = {"target_rows", "bad_columns", "bad_inode", "duplicate_inode", "duplicate_positive_inode",
          "pending_zero", "unknown_shape", "listeners", "alias_rows"}
REASONS = {"ascii", "header", "rows", "columns", "inode", "duplicate", "shape", "listener_count", "alias", "none"}
_SCOPE = ContextVar("gateway_socket_diagnostic_scope", default=None)
_PHASE = ContextVar("gateway_socket_diagnostic_phase", default=("unspecified", None, {}))


def encoded(value):
    return json.dumps(value, sort_keys=True, separators=(",", ":"), allow_nan=False).encode()


def sha(raw):
    return hashlib.sha256(raw).hexdigest()


@contextmanager
def scope(*, enabled=False):
    token = _SCOPE.set({"events": 0, "bytes": 0} if enabled is True else None)
    try:
        yield
    finally:
        _SCOPE.reset(token)


def identity_context(authority, peer=None):
    """Private anchor snapshot only, not a claim that current checks completed."""
    result = {"scope": "anchor_snapshot_not_current_proof"}
    for key in ("pid", "listener"):
        value = getattr(authority, key, None)
        if type(value) is int and 0 <= value < 2**64:
            result[key] = value
    for key, value in (("namespace", getattr(authority, "namespace", None)), ("peer", peer)):
        if type(value) is tuple and len(value) in (2, 3) and all(type(x) is int and 0 <= x < 2**64 for x in value):
            result[key] = list(value)
    return result


@contextmanager
def phase(stage, authority, peer=None):
    if _SCOPE.get() is None:
        yield
        return
    try:
        context = identity_context(authority, peer)
        deadline = getattr(authority, "deadline", None)
        value = (stage if stage in STAGES else "unspecified", deadline, context)
    except Exception:
        value = ("unspecified", None, {})
    token = _PHASE.set(value)
    try:
        yield
    finally:
        _PHASE.reset(token)


def read_failed(error, raw, *, eof=False):
    """Retain a truthful bounded prefix only; never call it a complete read."""
    if _SCOPE.get() is None:
        return
    try:
        if type(raw) not in (bytes, bytearray):
            return
        error.gateway_socket_read = {"raw": bytes(raw[:READ_BYTES]), "state": "partial",
                                     "eof": eof is True, "truncated": len(raw) > READ_BYTES}
    except Exception:
        pass  # Optional diagnostics cannot replace the original read exception.


def conditions(raw):
    """No row text, paths, numeric inode values or kernel pointers are exported."""
    result = {key: 0 for key in COUNTS}
    result.update(first_row=None, reason="none", columns_eight=None, positive_inode=None, inode_unique=None)
    try:
        lines = raw.decode("ascii", "strict").splitlines()
    except UnicodeError:
        result["reason"] = "ascii"
        return result
    if not lines or lines[0].split() != ["Num", "RefCount", "Protocol", "Flags", "Type", "St", "Inode", "Path"]:
        result["reason"] = "header"
        return result
    if len(lines) > 4096:
        result["reason"] = "rows"
        return result
    seen, paths = set(), set()
    for index, line in enumerate(lines[1:], 1):
        row = line.split()
        if len(row) < 8 or row[7] not in {"/run/docker.sock", "/var/run/docker.sock"}:
            continue
        result["target_rows"] += 1
        columns, positive, unique = len(row) == 8, bool(re.fullmatch(r"[1-9][0-9]{0,19}", row[6])), row[6] not in seen
        result["bad_columns"] += not columns
        result["bad_inode"] += not positive
        result["duplicate_inode"] += not unique
        result["duplicate_positive_inode"] += positive and not unique
        seen.add(row[6])
        shape = tuple(row[2:6])
        listener = shape == ("00000000", "00010000", "0001", "01")
        accepted = shape == ("00000000", "00000000", "0001", "03")
        result["listeners"] += listener
        result["pending_zero"] += shape == ("00000000", "00000000", "0001", "02") and row[6] == "0"
        result["unknown_shape"] += not (listener or accepted)
        paths.add(row[7])
        result["alias_rows"] += row[7] == "/var/run/docker.sock"
        reason = "columns" if not columns else "inode" if not positive else "duplicate" if not unique else "shape" if not (listener or accepted) else "none"
        if result["reason"] == "none" and reason != "none":
            result.update(first_row=index, reason=reason, columns_eight=columns, positive_inode=positive, inode_unique=unique)
    if result["reason"] == "none":
        result["reason"] = "listener_count" if result["listeners"] != 1 else "alias" if len(paths) > 1 else "none"
    return result


def reading(raw=None, *, state="not_started", eof=False, truncated=False):
    raw = raw if type(raw) is bytes and len(raw) <= READ_BYTES else None
    return {"raw": raw, "state": state, "eof": eof, "truncated": truncated}


def public_read(value):
    raw = value["raw"]
    return {"state": value["state"], "eof": value["eof"], "truncated": value["truncated"],
            "bytes": len(raw) if raw is not None else 0, "sha256": sha(raw) if raw is not None else None,
            "conditions": conditions(raw) if raw is not None else None}


def _private_write(directory, name, raw, deadline):
    if time.monotonic() >= deadline:
        raise TimeoutError("diagnostic deadline")
    fd = os.open(directory / name, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW | os.O_CLOEXEC, 0o600)
    try:
        position = 0
        while position < len(raw):
            if time.monotonic() >= deadline:
                raise TimeoutError("diagnostic deadline")
            count = os.write(fd, raw[position:position + 4096])
            if count <= 0:
                raise OSError("diagnostic short write")
            position += count
        if time.monotonic() >= deadline:
            raise TimeoutError("diagnostic deadline")
    finally:
        os.close(fd)


def _retain(event, deadline, budget):
    """No fsync/durable or archive-completeness claim; operation is already rejected."""
    if type(deadline) not in (int, float) or not time.monotonic() < deadline:
        return "deadline_exhausted"
    metadata = encoded({"summary": {k: v for k, v in event["summary"].items() if k != "retention"}, "identity": event["identity"],
                        "read_metadata": event["metadata"], "monotonic_ns": time.monotonic_ns()})
    size = len(metadata) + sum(len(x["raw"]) for x in event["reads"] if x["raw"] is not None)
    if len(metadata) > 8192 or budget["events"] >= PRIVATE_EVENTS or budget["bytes"] + size > PRIVATE_BYTES:
        return "capacity_exhausted"
    budget["events"] += 1
    budget["bytes"] += size
    directory = Path(tempfile.mkdtemp(prefix="ai-filter-socket-private-", dir="/tmp"))
    event["private_directory"] = directory
    for name, value in zip(("first.raw", "second.raw"), event["reads"]):
        if value["raw"] is not None:
            _private_write(directory, name, value["raw"], deadline)
    _private_write(directory, "CONTEXT.json", metadata, deadline)
    return "retained_privately"


def capture(error, first, second, metadata, failed_at):
    """First observation stays attached to this error; no cross-attempt overwrite."""
    budget = _SCOPE.get()
    if budget is None:
        return
    try:
        if hasattr(error, "gateway_socket_diagnostic"):
            return
        stage, deadline, identity = _PHASE.get()
        reads = [reading(first, state="complete", eof=True) if first is not None else reading(),
                 reading(second, state="complete", eof=True) if second is not None else reading()]
        if failed_at in {"first_read", "second_read"}:
            index = failed_at == "second_read"
            failed = getattr(error, "gateway_socket_read", None)
            reads[index] = failed if type(failed) is dict and set(failed) == {"raw", "state", "eof", "truncated"} else reading(state="unavailable")
        summary = {"schema": 1, "execution_proof": False, "full_raw_archive_verified": False, "stage": stage,
                   "failed_at": failed_at, "identity_sha256": sha(encoded(identity)),
                   "reads": [public_read(x) for x in reads], "retention": "not_attempted"}
        event = {"summary": summary, "reads": reads, "identity": identity, "metadata": metadata}
        error.gateway_socket_diagnostic = event
        try:
            if type(deadline) not in (int, float) or not time.monotonic() < deadline:
                summary["retention"] = "deadline_exhausted"
            elif threading.current_thread() is not threading.main_thread():
                summary["retention"] = "write_failed"
            else:
                from scripts.production_gateway_switch import _hard_deadline
                with _hard_deadline(deadline - time.monotonic()):
                    summary["retention"] = _retain(event, deadline, budget)
        except TimeoutError:
            summary["retention"] = "deadline_exhausted"
        except Exception:
            summary["retention"] = "deadline_exhausted" if type(deadline) in (int, float) and time.monotonic() >= deadline else "write_failed"
    except Exception:
        pass  # The caller re-raises the original error unconditionally.


def safe_summary(value):
    """Strictly validate public packets; never copy arbitrary fields or error text."""
    try:
        keys = {"schema", "execution_proof", "full_raw_archive_verified", "stage", "failed_at", "identity_sha256", "reads", "retention"}
        if type(value) is not dict or set(value) != keys or value["schema"] != 1 or type(value["schema"]) is not int:
            return None
        if value["execution_proof"] is not False or value["full_raw_archive_verified"] is not False:
            return None
        if value["stage"] not in STAGES or value["retention"] not in RETENTION:
            return None
        if value["failed_at"] not in {"first_read", "second_read", "first_parse", "second_parse", "comparison"}:
            return None
        if type(value["identity_sha256"]) is not str or not re.fullmatch(r"[0-9a-f]{64}", value["identity_sha256"]):
            return None
        if type(value["reads"]) is not list or len(value["reads"]) != 2 or not all(_safe_read(x) for x in value["reads"]):
            return None
        raw = encoded(value)
        return json.loads(raw) if len(raw) <= PUBLIC_BYTES else None
    except Exception:
        return None


def _safe_read(value):
    if type(value) is not dict or set(value) != {"state", "eof", "truncated", "bytes", "sha256", "conditions"}:
        return False
    if value["state"] not in STATES or type(value["eof"]) is not bool or type(value["truncated"]) is not bool:
        return False
    if type(value["bytes"]) is not int or not 0 <= value["bytes"] <= READ_BYTES:
        return False
    if value["sha256"] is None:
        return value["state"] in {"not_started", "unavailable"} and value["bytes"] == 0 and value["conditions"] is None and not value["eof"] and not value["truncated"]
    if type(value["sha256"]) is not str or not re.fullmatch(r"[0-9a-f]{64}", value["sha256"]):
        return False
    if value["state"] not in {"complete", "partial"} or (value["state"] == "complete" and (not value["eof"] or value["truncated"])):
        return False
    item = value["conditions"]
    if type(item) is not dict or set(item) != COUNTS | {"first_row", "reason", "columns_eight", "positive_inode", "inode_unique"}:
        return False
    if not all(type(item[k]) is int and 0 <= item[k] <= 4096 for k in COUNTS) or item["reason"] not in REASONS:
        return False
    return (item["first_row"] is None or type(item["first_row"]) is int and 1 <= item["first_row"] <= 4095) and all(
        item[k] is None or type(item[k]) is bool for k in ("columns_eight", "positive_inode", "inode_unique"))


def error_summary(error):
    try:
        return safe_summary(error.gateway_socket_diagnostic["summary"])
    except Exception:
        return None
