"""Reviewable, isolation-only durable handoff cutover protocol controller.

This executable has no Docker, SSH, shell, process-kill, production or rollback
adapter. It verifies a loopback fixture protocol; production binding is absent.
"""
from __future__ import annotations

import argparse
import fcntl
import hashlib
import http.client
import json
import os
from pathlib import Path
import re
import stat
import socket
import threading
import time
from urllib.parse import urlsplit

ENTRANCES = ("miniflux_scheduler", "miniflux_manual", "legacy_async_sender", "webhook",
             "webhook_bypass", "natural_cron", "manual_refresh", "filter", "filter_multi",
             "briefing_llm", "feedback", "internal_queue", "cron_maintenance", "outside_compose")
ZERO_COUNTERS = ("pending", "running", "old_contract_unfinished", "legacy_inflight", "legacy_unpreserved",
                 "queue_pending", "queue_inflight", "journal_pending", "journal_running",
                 "journal_commit_pending", "archive_pending", "terminal_pending", "active_requests",
                 "old_route_inflight", "old_route_unknown", "rejected_unresolved", "missing_originals",
                 "buffer_inflight", "buffer_uncertain", "unknown_producers")
PHASES = ("prepared", "paused", "drained", "receiver_switched", "sender_switched", "verified", "resumed")
COMMANDS = {"prepared": "pause", "drained": "switch_receiver", "receiver_switched": "switch_sender",
            "verified": "resume"}
MAX_BYTES = 1024 * 1024


class CutoverError(Exception):
    """A required boundary is unconfirmed; nothing is rolled back or retried."""


def encoded(value):
    return json.dumps(value, sort_keys=True, separators=(",", ":"), allow_nan=False).encode()


def digest(value):
    return hashlib.sha256(encoded(value)).hexdigest()


def require(value, reason):
    if not value:
        raise CutoverError(reason)


def parsed(raw):
    def pairs(items):
        result = {}
        for key, value in items:
            require(key not in result, "duplicate JSON field")
            result[key] = value
        return result
    return json.loads(raw, object_pairs_hook=pairs)


def private_directory(path, create=False):
    if create:
        path.mkdir(mode=0o700, parents=True, exist_ok=True)
    info = path.lstat()
    require(stat.S_ISDIR(info.st_mode) and info.st_uid == os.geteuid()
            and stat.S_IMODE(info.st_mode) == 0o700, "unsafe private directory")
    if create:
        sync_directory(path)
        sync_directory(path.parent)


def read_private(path):
    fd = os.open(path, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0))
    with os.fdopen(fd, "rb") as stream:
        before = os.fstat(stream.fileno())
        require(stat.S_ISREG(before.st_mode) and before.st_nlink == 1 and before.st_uid == os.geteuid()
                and stat.S_IMODE(before.st_mode) == 0o600 and before.st_size <= MAX_BYTES, "unsafe private file")
        raw = stream.read(before.st_size + 1)
        after = os.fstat(stream.fileno())
        require(len(raw) == before.st_size and before.st_mtime_ns == after.st_mtime_ns
                and before.st_size == after.st_size, "private file changed while reading")
    return parsed(raw)


def sync_directory(path):
    fd = os.open(path, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def durable_write(path, value):
    """Strict file+rename+directory barrier; pending bytes survive failures."""
    private_directory(path.parent)
    raw = encoded(value)
    require(len(raw) <= MAX_BYTES, "record size budget exceeded")
    temporary = path.with_name(path.name + ".pending")
    if temporary.exists():
        require(read_private(temporary) == value, "unresolved partial/conflicting write")
        fd = os.open(temporary, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0))
        try:
            os.fsync(fd)
        finally:
            os.close(fd)
    else:
        fd = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0), 0o600)
        with os.fdopen(fd, "wb") as stream:
            stream.write(raw)
            stream.flush()
            os.fsync(stream.fileno())
    os.replace(temporary, path)
    sync_directory(path.parent)


class Journal:
    """Immutable numbered records, compare-and-swap under one process flock."""

    def __init__(self, root):
        self.root = Path(root)
        private_directory(self.root, create=True)
        self.records = self.root / "records"
        private_directory(self.records, create=True)
        self.lock = os.open(self.root / "controller.lock", os.O_CREAT | os.O_RDWR | getattr(os, "O_NOFOLLOW", 0), 0o600)
        try:
            info = os.fstat(self.lock)
            require(stat.S_ISREG(info.st_mode) and info.st_nlink == 1 and info.st_uid == os.geteuid()
                    and stat.S_IMODE(info.st_mode) == 0o600, "unsafe controller lock")
            fcntl.flock(self.lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except (BlockingIOError, CutoverError) as error:
            os.close(self.lock)
            raise CutoverError("another controller owns this run") from error

    def close(self):
        os.close(self.lock)

    def latest(self):
        final = sorted(self.records.glob("*.json"))
        pending = sorted(self.records.glob("*.pending"))
        require(len(final) <= 64 and len(pending) <= 1, "journal record budget/shape invalid")
        previous = None
        for index, path in enumerate(final):
            value = read_private(path)
            require(path.name == f"{index:04d}.json" and value.get("sequence") == index
                    and value.get("previous") == (digest(previous) if previous is not None else None), "journal CAS chain mismatch")
            fd = os.open(path, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0))
            try:
                os.fsync(fd)
            finally:
                os.close(fd)
            previous = value
        sync_directory(self.records)
        if pending:
            value = read_private(pending[0])
            expected = self.records / f"{len(final):04d}.json"
            require(pending[0].name == expected.name + ".pending" and value.get("sequence") == len(final)
                    and value.get("previous") == (digest(previous) if previous is not None else None), "pending CAS identity mismatch")
            durable_write(expected, value)
            previous = value
        return previous

    def append(self, prior, state):
        actual = self.latest()
        require(actual == prior, "stale controller CAS")
        sequence = 0 if prior is None else prior["sequence"] + 1
        require(sequence < 64, "controller journal exhausted")
        value = {**state, "sequence": sequence, "previous": digest(prior) if prior is not None else None}
        durable_write(self.records / f"{sequence:04d}.json", value)
        return value


def validate_manifest(value):
    require(isinstance(value, dict) and type(value.get("schema")) is int
            and value.get("schema") == 1 and value.get("mode") == "isolation"
            and value.get("production_adapter_bound") is False, "production execution is not implemented")
    require(re.fullmatch(r"[0-9a-f]{32}", value.get("run_id", "")), "bad run identity")
    require(re.fullmatch(r"[0-9a-f]{64}", value.get("token", "")), "bad isolation authentication token")
    require(value.get("entrances") == list(ENTRANCES), "all-entrypoint inventory differs")
    endpoint = urlsplit(value.get("endpoint", ""))
    require(endpoint.scheme == "http" and endpoint.hostname == "127.0.0.1" and endpoint.port
            and not endpoint.username and not endpoint.password and endpoint.path in ("", "/")
            and not endpoint.query and not endpoint.fragment, "only loopback isolation endpoint permitted")
    require(value.get("adapter_identity") == "synthetic-cutover-v1" and re.fullmatch(r"[0-9a-f]{64}", value.get("adapter_sha256", "")),
            "adapter binding absent")
    require(value.get("targets", {}).keys() == {"receiver", "sender"}, "two new component targets required")
    for role, target in value["targets"].items():
        require(target.startswith("synthetic-new-" + role + "@sha256:") and re.fullmatch(r"[0-9a-f]{64}", target.rsplit(":", 1)[-1]),
                "only explicitly synthetic new candidate identities permitted")
    limits = value.get("budget", {})
    require(set(limits) == {"total_seconds", "drain_seconds", "request_seconds", "max_polls"}, "budget keys differ")
    for key, low, high in (("total_seconds", 1, 60), ("drain_seconds", 0.1, 30), ("request_seconds", 0.05, 2), ("max_polls", 1, 100)):
        require(type(limits.get(key)) in (int, float) and low <= limits[key] <= high, "invalid hard budget: " + key)
    require(type(limits["max_polls"]) is int and limits["drain_seconds"] <= limits["total_seconds"], "inconsistent budget")
    return endpoint


class Controller:
    def __init__(self, manifest, journal_root):
        self.manifest = manifest
        self.endpoint = validate_manifest(manifest)
        self.journal = Journal(journal_root)
        self.started = time.monotonic()
        self.last_request = None
        try:
            self.state = self.journal.latest()
            if self.state is None:
                self.state = self.journal.append(None, {"manifest_sha256": digest(manifest), "run_id": manifest["run_id"],
                    "phase": "prepared", "intent": None, "stopped": None, "expires_at": time.time() + manifest["budget"]["total_seconds"]})
            require(self.state["manifest_sha256"] == digest(manifest) and self.state["run_id"] == manifest["run_id"], "manifest changed across restart")
        except Exception:
            self.journal.close()
            raise

    def record(self, **changes):
        value = {key: val for key, val in self.state.items() if key not in ("sequence", "previous")}
        self.state = self.journal.append(self.state, {**value, **changes})

    def request(self, path, payload=None):
        started = time.monotonic()
        self.last_request = {"method": "POST" if payload is not None else "GET", "path": path,
            "action": payload.get("action") if payload is not None else None,
            "phase": self.state["phase"], "stage": "budget", "outcome": "not_started",
            "request_seconds": self.manifest["budget"]["request_seconds"]}
        remaining = min(self.state["expires_at"] - time.time(), self.manifest["budget"]["total_seconds"] - (time.monotonic() - self.started))
        self.last_request["remaining_total_seconds"] = remaining
        require(remaining > 0, "frozen total deadline expired")
        timeout = min(remaining, self.manifest["budget"]["request_seconds"])
        self.last_request.update(timeout_seconds=timeout, outcome="in_progress", stage="connect")
        connection = http.client.HTTPConnection("127.0.0.1", self.endpoint.port, timeout=timeout)
        timer = None
        wire = None
        try:
            deadline = time.monotonic() + timeout
            connection.connect()
            wire = connection.sock.dup()
            def abort():
                try:
                    wire.shutdown(socket.SHUT_RDWR)
                except OSError:
                    pass
            timer = threading.Timer(max(0, deadline - time.monotonic()), abort)
            timer.daemon = True
            timer.start()
            self.last_request["stage"] = "send"
            connection.request("POST" if payload is not None else "GET", path,
                body=encoded(payload) if payload is not None else None,
                headers={"Content-Type": "application/json", "X-Isolation-Token": self.manifest["token"]})
            self.last_request["stage"] = "response_headers"
            response = connection.getresponse()
            self.last_request.update(stage="response_body", http_status=response.status)
            raw = bytearray()
            while len(raw) <= MAX_BYTES:
                left = deadline - time.monotonic()
                require(left > 0, "absolute HTTP deadline expired")
                part = response.read1(min(65536, MAX_BYTES + 1 - len(raw)))
                if not part:
                    break
                raw.extend(part)
            self.last_request.update(stage="validate", response_bytes=len(raw))
            require(time.monotonic() <= deadline and response.status == 200 and len(raw) <= MAX_BYTES,
                    "adapter non-success/bounded response failure")
            value = parsed(raw)
            require(value.get("run_id") == self.manifest["run_id"] and value.get("mode") == "isolation"
                    and value.get("adapter_identity") == self.manifest["adapter_identity"]
                    and value.get("adapter_sha256") == self.manifest["adapter_sha256"], "adapter identity changed")
            self.last_request.update(stage="complete", outcome="returned")
            return value
        except Exception as error:
            self.last_request.update(outcome="raised", error_type=type(error).__name__)
            raise
        finally:
            self.last_request["elapsed_seconds"] = time.monotonic() - started
            if timer is not None:
                timer.cancel()
            connection.close()
            if wire is not None:
                wire.close()

    def inspect(self):
        value = self.request("/control/snapshot")
        require(value.get("entrances", {}).keys() == set(ENTRANCES), "entrypoint coverage missing")
        require(value.get("legacy_unpreserved") == 0, "old asynchronous send lacks exact durable preservation")
        require(value.get("unknown_producers") == 0, "unbound producer remains")
        return value

    def zero_gate(self, value):
        require(all(mode == "buffering" for mode in value["entrances"].values()), "not all producers are fenced")
        for key in ZERO_COUNTERS:
            require(type(value.get(key)) is int and value[key] == 0, "zero gate not met: " + key)

    def apply(self, action, snapshot):
        intent = self.state.get("intent")
        if intent is None:
            intent = {"operation_id": digest({"run_id": self.manifest["run_id"], "action": action}),
                      "action": action, "expected_generation": snapshot["generation"], "manifest_sha256": digest(self.manifest),
                      "targets": self.manifest["targets"]}
            self.record(intent=intent)
            receipt = self.request("/control/apply", intent)
        else:
            require(intent["action"] == action, "unresolved operation belongs to a different phase")
            receipt = self.request("/control/operation/" + intent["operation_id"])
        require(receipt.get("confirmed") is True and receipt.get("request") == intent,
                "operation not durably confirmed; do not reissue")
        require(type(receipt.get("generation")) is int and receipt["generation"] == intent["expected_generation"] + 1,
                "operation generation does not match CAS")
        self.record(phase=PHASES[PHASES.index(self.state["phase"]) + 1], intent=None,
                    last_receipt=receipt, stopped=None)

    def drain(self):
        deadline = time.monotonic() + self.manifest["budget"]["drain_seconds"]
        for _ in range(self.manifest["budget"]["max_polls"]):
            value = self.inspect()
            require(all(mode == "buffering" for mode in value["entrances"].values()), "fence reopened during drain")
            if all(type(value.get(key)) is int and value[key] == 0 for key in ZERO_COUNTERS):
                self.record(phase="drained", drain_evidence=value)
                return
            require(time.monotonic() < deadline, "drain deadline expired; ingress remains buffered")
            time.sleep(min(0.02, max(0, deadline - time.monotonic())))
        raise CutoverError("drain poll budget exhausted")

    def run(self, resume=False):
        try:
            require(not self.state.get("stopped") or resume, "stopped run requires explicit isolated resume")
            while self.state["phase"] != "resumed":
                phase = self.state["phase"]
                if phase == "paused":
                    self.drain()
                    continue
                if self.state.get("intent"):
                    # An uncertain resume may already have reopened the fence.
                    # Resolve its exact receipt; never reissue the mutation or
                    # require a pre-operation state after it has committed.
                    self.apply(COMMANDS[phase], {})
                    continue
                value = self.inspect()
                if phase in ("drained", "receiver_switched", "sender_switched", "verified"):
                    self.zero_gate(value)
                if phase == "sender_switched":
                    require(value.get("components") == self.manifest["targets"] and value.get("schema") == 132,
                            "new components/schema did not verify")
                    self.record(phase="verified", verified=value)
                else:
                    self.apply(COMMANDS[phase], value)
            final = self.inspect()
            require(all(mode == "open" for mode in final["entrances"].values()) and final.get("buffered") == 0
                    and final.get("components") == self.manifest["targets"] and final.get("schema") == 132,
                    "restored ingress or buffer transfer is unconfirmed")
            return {"status": "passed_isolation_protocol_only", "phase": self.state["phase"], "final": final,
                    "production_adapter_bound": False, "automatic_rollback": False}
        except (CutoverError, OSError, ValueError, http.client.HTTPException) as error:
            self.record(stopped={"type": type(error).__name__, "reason": str(error), "at": time.time(),
                                 "last_request": self.last_request})
            return {"status": "stopped", "phase": self.state["phase"], "reason": str(error),
                    "error_type": type(error).__name__, "last_request": self.last_request,
                    "production_adapter_bound": False, "automatic_rollback": False}
        finally:
            self.journal.close()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--manifest", type=Path, required=True)
    parser.add_argument("--journal", type=Path, required=True)
    parser.add_argument("--resume", action="store_true")
    args = parser.parse_args()
    try:
        result = Controller(read_private(args.manifest), args.journal).run(args.resume)
    except (CutoverError, OSError, ValueError) as error:
        result = {"status": "stopped", "reason": str(error), "production_adapter_bound": False}
    print(json.dumps(result, sort_keys=True))
    return 0 if result["status"] == "passed_isolation_protocol_only" else 2


if __name__ == "__main__":
    raise SystemExit(main())
