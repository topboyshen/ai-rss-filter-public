"""Exact-operation host driver for durable-original transfer, never deployment."""
from __future__ import annotations

import hashlib
from pathlib import Path
import time

from durable_handoff_cutover import durable_write, encoded, private_directory, read_private, require
from handoff_host_cron import locked, present
from production_receiver_driver import ReceiverDriver
from services import admission_fence as gate
from services.receiver_drain_control import PROTOCOL
from services.receiver_drain_transfer import sources as transfer_sources

MAX_QUERIES = 32


def sources():
    return {"driver": hashlib.sha256(Path(__file__).read_bytes()).hexdigest(), **transfer_sources()}


class ReceiverDrainDriver:
    def __init__(self, receiver, request, evidence_root):
        require(isinstance(receiver, ReceiverDriver), "actual instrumented receiver driver required")
        self.receiver, self.request, self.root = receiver, gate._parsed(encoded(request)), Path(evidence_root)
        self.request_sha256 = gate._hash(self.request)
        self.operation_id = request["operation_id"]
        require(isinstance(self.operation_id, str) and gate.HEX64.fullmatch(self.operation_id), "exact transfer operation required")
        require(request["receiver_binding"] == receiver.plan["registration"]["binding"]
                and request["receiver_manifest_sha256"] == receiver.plan["registration"]["manifest_sha256"]
                and request["spool"] == receiver.plan["container_spool_root"], "transfer receiver differs from actual host plan")
        # Originals must already be mounted through the inspected fence bind.
        root = Path(receiver.plan["container_fence_root"])
        require(Path(request["sender_bundle"]).is_relative_to(root)
                and Path(request["sender_bundle"]) != root, "sender bundle must be under the bound receiver fence mount")
        private_directory(self.root, create=True)
        from services.receiver_drain_storage import create_lock
        create_lock(self.root / ".lock")
        value = {"schema": 1, "request": self.request, "request_sha256": self.request_sha256,
                 "receiver_plan_sha256": receiver.plan_sha256, "source_sha256": sources()}
        path = self.root / "plan.json"
        if present(path):
            require(read_private(path) == value, "durable transfer plan changed")
        durable_write(path, value)
        self.plan = value

    def bind(self):
        require(self.request_sha256 == gate._hash(self.request) and self.plan["source_sha256"] == sources(), "host transfer source/request changed")
        self.receiver.bind()
        value = self.receiver.transport.request("GET", "/drain/registration")
        expected = {"schema": 1, "protocol": PROTOCOL, "receiver": self.receiver.plan["registration"],
                    "source_sha256": self.request["source_sha256"], "body_limit": self.request["body_limit"],
                    "config_sha256": self.request["config_sha256"], "actions": ["transfer", "query"],
                    "switch_authorized": False, "queue_checked": False}
        require(value == expected, "actual drain capability differs")
        return value

    def _accept(self, response, registration):
        require(isinstance(response, dict) and set(response) == {"registration", "receipt"}
                and response["registration"] == registration, "transfer response identity differs")
        value = response["receipt"]
        require(isinstance(value, dict) and set(value) == {"schema", "operation_id", "plan_sha256", "sender_manifest_sha256",
                "confirmed", "total", "state", "error", "durable", "queue_checked", "switch_authorized", "legacy_originals_proven"},
                "exact transfer receipt required")
        require(value["schema"] == 1 and value["operation_id"] == self.operation_id
                and value["sender_manifest_sha256"] == self.request["sender_manifest_sha256"]
                and isinstance(value["plan_sha256"], str) and gate.HEX64.fullmatch(value["plan_sha256"])
                and all(type(value[k]) is int for k in ("confirmed", "total"))
                and 0 <= value["confirmed"] <= value["total"] <= 4096
                and value["state"] in {"pending", "complete"} and type(value["durable"]) is bool
                and value["error"] in {None, "original_transfer_unconfirmed"}
                and value["queue_checked"] is value["switch_authorized"] is value["legacy_originals_proven"] is False,
                "invalid transfer receipt")
        complete = value["confirmed"] == value["total"]
        require(value["durable"] is complete and (value["state"] == "complete") is complete
                and (not complete or value["error"] is None), "incomplete durable transfer")
        self.bind()  # Identity changing during POST/GET cannot confirm a receipt.
        return value

    def prepare(self):
        with locked(self.root / ".lock", True):
            self.receiver._window()
            registration = self.bind()
            path = self.root / "intent.json"
            require(not present(path) and not present(path.with_name(path.name + ".pending")), "transfer attempted; query original operation only")
            durable_write(path, {"operation_id": self.operation_id, "request_sha256": self.request_sha256,
                                 "source_sha256": sources(), "created_at": time.time()})
            result = self._accept(self.receiver.transport.request("POST", "/drain/transfer", self.request), registration)
            durable_write(self.root / "response.json", result)
            return result

    def query(self):
        with locked(self.root / ".lock", True):
            intent = read_private(self.root / "intent.json")
            require(intent["operation_id"] == self.operation_id and intent["request_sha256"] == self.request_sha256
                    and intent["source_sha256"] == sources(), "unknown original transfer operation")
            registration = self.bind()
            paths = list(self.root.glob("query-*.json*"))
            require(len(paths) < MAX_QUERIES, "transfer query budget exhausted")
            path = self.root / f"query-{len(paths):02}.json"
            durable_write(path, {"operation_id": self.operation_id, "request_sha256": self.request_sha256})
            result = self._accept(self.receiver.transport.request("GET", "/drain/operations/" + self.operation_id), registration)
            durable_write(self.root / f"response-{len(paths):02}.json", result)
            return result
