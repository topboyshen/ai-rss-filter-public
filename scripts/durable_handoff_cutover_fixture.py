"""Authenticated loopback-only synthetic adapter for the cutover protocol.

Not an AI-Filter/Miniflux implementation or a production switch adapter. All
accepted bytes, operation receipts and phase snapshots live in a private root.
"""
from __future__ import annotations

import argparse
import base64
import fcntl
import hashlib
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import os
from pathlib import Path
import secrets
import stat
import threading
import time

from durable_handoff_cutover import (CutoverError, ENTRANCES, MAX_BYTES, ZERO_COUNTERS,
                                     durable_write, encoded, private_directory,
                                     read_private, require, sync_directory)

KINDS = ("pending", "running", "continuations", "legacy", "buffer", "receipts", "operations", "retained", "blockers")


class Fixture:
    def __init__(self, root, run_id, token, faults=None):
        self.root, self.run_id, self.token = Path(root), run_id, token
        self.faults = faults or {}
        private_directory(self.root, create=True)
        for kind in KINDS:
            private_directory(self.root / kind, create=True)
        self.identity = {"mode": "isolation", "run_id": run_id, "adapter_identity": "synthetic-cutover-v1",
                         "adapter_sha256": hashlib.sha256(Path(__file__).read_bytes()).hexdigest()}
        self.lock = threading.Lock()
        if not (self.root / "state.json").exists():
            durable_write(self.root / "state.json", {"run_id": run_id, "generation": 0, "schema": 131,
                "entrances": {name: "open" for name in ENTRANCES},
                "components": {"receiver": "synthetic-old-receiver", "sender": "synthetic-old-sender"},
                "switch_counts": {"receiver": 0, "sender": 0}})
        require(read_private(self.root / "state.json")["run_id"] == run_id, "wrong fixture root/run")

    def save(self, state):
        durable_write(self.root / "state.json", state)

    def snapshot(self):
        state = read_private(self.root / "state.json")
        values = {kind: [read_private(path) for path in (self.root / kind).glob("*.json")]
                  for kind in KINDS if kind != "operations"}
        for row in values["legacy"]:
            if row.get("body_b64"):
                raw = base64.b64decode(row["body_b64"], validate=True)
                require(raw and hashlib.sha256(raw).hexdigest() == row.get("delivery_id"),
                        "legacy exact-byte identity is unconfirmed")
        counters = dict.fromkeys(ZERO_COUNTERS, 0)
        for row in values["blockers"]:
            require(row.get("counter") in ZERO_COUNTERS and type(row.get("count")) is int and row["count"] > 0,
                    "unrecognized nonzero fixture gate record")
            counters[row["counter"]] += row["count"]
        return {**self.identity, **state, **counters,
                "queue_pending": len(values["pending"]) + counters["queue_pending"],
                "queue_inflight": len(values["running"]) + counters["queue_inflight"],
                "journal_pending": len(values["continuations"]) + counters["journal_pending"],
                "pending": len(values["pending"]) + counters["pending"],
                "running": len(values["running"]) + counters["running"],
                "old_contract_unfinished": len(values["continuations"]) + counters["old_contract_unfinished"],
                "buffered": len(values["buffer"]),
                "legacy_inflight": sum(row.get("status") != "preserved" for row in values["legacy"]) + counters["legacy_inflight"],
                "legacy_unpreserved": sum(not row.get("body_b64") for row in values["legacy"]) + counters["legacy_unpreserved"],
                "unknown_producers": int(self.faults.get("unknown_producers", 0)) + counters["unknown_producers"],
                "buffer_delivery_ids": sorted(row["delivery_id"] for row in values["buffer"]),
                "receipt_delivery_ids": sorted(row["delivery_id"] for row in values["receipts"]),
                "retained_delivery_ids": sorted(row["delivery_id"] for row in values["retained"])}

    def store_body(self, directory, raw, entrance):
        require(0 < len(raw) <= 65536 and entrance in ENTRANCES, "fixture body/entrypoint outside bound")
        delivery = hashlib.sha256(raw).hexdigest()
        path = self.root / directory / (delivery + ".json")
        record = {"delivery_id": delivery, "body_sha256": delivery, "body_b64": base64.b64encode(raw).decode(),
                  "entrance": entrance, "status": "preserved"}
        if path.exists():
            require(read_private(path) == record, "same identity different fixture record")
        else:
            files = list((self.root / directory).glob("*.json"))
            require(len(files) < 16 and sum(p.stat().st_size for p in files) + len(encoded(record)) <= MAX_BYTES,
                    "fixture buffer capacity exhausted")
        durable_write(path, record)
        return record

    def accept(self, entrance, raw):
        state = read_private(self.root / "state.json")
        target = "buffer" if state["entrances"][entrance] == "buffering" else "pending"
        if self.faults.get("accept_sync_failure"):
            raise OSError("synthetic strict barrier failure")
        record = self.store_body(target, raw, entrance)
        return {**self.identity, "schema": 1, "durable": True, "delivery_id": record["delivery_id"],
                "body_sha256": record["delivery_id"], "stored_in": target}

    def retain(self, source):
        """Move synthetic completed work only after preserving its original bytes."""
        require(source.parent.name in ("pending", "running", "continuations", "buffer"), "wrong fixture transfer")
        record = read_private(source)
        durable_write(self.root / "retained" / source.name, record)
        source.unlink()
        sync_directory(source.parent)

    def zero_gate(self, state):
        value = self.snapshot()
        require(all(mode == "buffering" for mode in state["entrances"].values()), "not fully fenced")
        require(all(type(value[key]) is int and value[key] == 0 for key in ZERO_COUNTERS),
                "atomic switch zero gate failed")

    def confirmed_operation(self, operation):
        path = self.root / "operations" / (operation + ".json")
        receipt = read_private(path)
        # A visible rename may be from a previous failed directory barrier.
        # A recovery query cannot promote visibility into durable confirmation.
        durable_write(path, receipt)
        return receipt

    def apply(self, request):
        operation = request.get("operation_id", "")
        require(len(operation) == 64 and all(c in "0123456789abcdef" for c in operation), "bad operation identity")
        path = self.root / "operations" / (operation + ".json")
        if path.exists():
            value = read_private(path)
            require(value["request"] == request, "operation replay identity conflict")
            durable_write(path, value)
            return value
        state = read_private(self.root / "state.json")
        require(state["generation"] == request["expected_generation"], "adapter CAS conflict")
        action = request["action"]
        require(action in {"pause", "switch_receiver", "switch_sender", "resume"}, "unsupported action/rollback forbidden")
        if action == "pause":
            state["entrances"] = {name: "buffering" for name in ENTRANCES}
        elif action.startswith("switch_"):
            self.zero_gate(state)
            role = action.removeprefix("switch_")
            require(state["switch_counts"][role] == 0, "component already switched")
            if role == "sender":
                require(state["components"]["receiver"] == request["targets"]["receiver"], "receiver not ready before sender")
                state["schema"] = 132
            state["components"][role] = request["targets"][role]
            state["switch_counts"][role] += 1
        else:
            self.zero_gate(state)
            require(state["components"] == request["targets"] and state["schema"] == 132, "new components not verified")
            for source in sorted((self.root / "buffer").glob("*.json")):
                raw_record = read_private(source)
                durable_write(self.root / "receipts" / source.name, {**raw_record, "schema": 1, "durable": True})
                self.retain(source)
            state["entrances"] = {name: "open" for name in ENTRANCES}
        state["generation"] += 1
        # One durable envelope commits state and operation together. No ACK until
        # both derived files are synced; recovery adopts the exact envelope.
        receipt = {**self.identity, "confirmed": True, "request": request, "generation": state["generation"]}
        durable_write(self.root / "transition.json", {"state": state, "receipt": receipt})
        self.save(state)
        durable_write(path, receipt)
        if self.faults.get("exit_after") == action:
            os._exit(73)
        if self.faults.get("delay_after") == action:
            time.sleep(0.4)
        return receipt

    def recover(self):
        path = self.root / "transition.json"
        pending = path.with_name(path.name + ".pending")
        if pending.exists():
            value = read_private(pending)
            durable_write(path, value)
        if path.exists():
            value = read_private(path)
            state = read_private(self.root / "state.json")
            require(value["state"]["generation"] >= state["generation"], "transition behind adapter state")
            self.save(value["state"])
            receipt = value["receipt"]
            durable_write(self.root / "operations" / (receipt["request"]["operation_id"] + ".json"), receipt)


class Handler(BaseHTTPRequestHandler):
    def log_message(self, *_args):
        pass

    def dispatch(self):
        fixture = self.server.fixture
        require(secrets.compare_digest(self.headers.get("X-Isolation-Token", ""), fixture.token), "fixture authentication failed")
        size = int(self.headers.get("Content-Length", 0))
        require(0 <= size <= 65536, "fixture request too large")
        raw = self.rfile.read(size)
        require(len(raw) == size, "short fixture request")
        require((self.command == "GET") == (self.path == "/control/snapshot" or self.path.startswith("/control/operation/")),
                "wrong protocol HTTP method")
        with fixture.lock:
            if self.path == "/control/snapshot":
                return 200, fixture.snapshot()
            if self.path == "/control/apply":
                return 200, fixture.apply(json.loads(raw))
            if self.path.startswith("/control/operation/"):
                operation = self.path.rsplit("/", 1)[-1]
                require(len(operation) == 64 and all(c in "0123456789abcdef" for c in operation), "bad operation query")
                return 200, fixture.confirmed_operation(operation)
            if self.path.startswith("/entry/"):
                entrance = self.path.rsplit("/", 1)[-1]
                require(entrance in ENTRANCES, "unknown entrypoint")
                return 202, fixture.accept(entrance, raw)
            if self.path == "/fixture/complete":
                request = json.loads(raw)
                require(request["kind"] in {"pending", "running", "continuations"}, "invalid completion kind")
                name = request["delivery_id"]
                require(len(name) == 64 and all(c in "0123456789abcdef" for c in name), "bad completion identity")
                fixture.retain(fixture.root / request["kind"] / (name + ".json"))
                return 200, {**fixture.identity, "retained": True}
            if self.path == "/shutdown":
                threading.Thread(target=self.server.shutdown, daemon=True).start()
                return 200, {**fixture.identity, "stopping_owned_fixture": True}
        raise CutoverError("unsupported fixture endpoint")

    def do_GET(self):
        try:
            status, value = self.dispatch()
        except (CutoverError, OSError, ValueError, KeyError) as error:
            status, value = 503, {**self.server.fixture.identity, "error": type(error).__name__}
        raw = encoded(value)
        try:
            if self.server.fixture.faults.get("ordinary_202") and self.path == "/control/apply":
                status, raw = 202, encoded({"accepted": True})
            self.send_response(status)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(raw)))
            self.end_headers()
            if self.server.fixture.faults.get("slow_drip") and self.path == "/control/snapshot":
                for byte in raw:
                    self.wfile.write(bytes([byte]))
                    self.wfile.flush()
                    time.sleep(0.02)
            else:
                self.wfile.write(raw)
        except (BrokenPipeError, ConnectionResetError):
            pass

    do_POST = do_GET


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, required=True)
    parser.add_argument("--config", type=Path, required=True)
    parser.add_argument("--port", type=int, default=0)
    args = parser.parse_args()
    config = read_private(args.config)
    require(config.get("mode") == "isolation", "no production fixture mode")
    private_directory(args.root, create=True)
    lock = os.open(args.root / "adapter.lock", os.O_CREAT | os.O_RDWR | getattr(os, "O_NOFOLLOW", 0), 0o600)
    info = os.fstat(lock)
    require(stat.S_ISREG(info.st_mode) and info.st_nlink == 1 and info.st_uid == os.geteuid()
            and stat.S_IMODE(info.st_mode) == 0o600, "unsafe adapter lock")
    fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
    fixture = Fixture(args.root, config["run_id"], config["token"], config.get("faults"))
    fixture.recover()
    server = ThreadingHTTPServer(("127.0.0.1", args.port), Handler)
    server.fixture = fixture
    print(json.dumps({**fixture.identity, "port": server.server_port}), flush=True)
    server.serve_forever(poll_interval=0.02)
    server.server_close()
    os.close(lock)


if __name__ == "__main__":
    main()
