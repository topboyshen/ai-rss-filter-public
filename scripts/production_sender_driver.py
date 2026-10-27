"""Bounded instrumented sender phases; no deployment, reopening or old-history claim.

Stop-new is distinct from full pause. Actual independent PostgreSQL inventory
must clear before the normal freeze path; an explicit abort freezes without
claiming a drained queue. Unknown writes only permit exact-operation queries.
"""
from __future__ import annotations

import hashlib
import http.client
from pathlib import Path
import time
from urllib.parse import urlsplit

from durable_handoff_cutover import durable_write, encoded, parsed, private_directory, read_private, require
from handoff_host_cron import locked, present
from handoff_runtime_adapter import inspect_binding
from miniflux_handoff_adapter import MinifluxDrainControl, HEX64, checked_binding, source_sha256 as bridge_source
from production_receiver_driver import _capture
from sender_drain_inventory import PostgresReader
from services import sender_drain_contract as contract

MAX_SAMPLES = 32


def sources():
    names = ("production_sender_driver.py", "sender_drain_inventory.py", "miniflux_handoff_adapter.py",
             "handoff_runtime_adapter.py", "handoff_host_cron.py", "durable_handoff_cutover.py", "observe_handoff_capacity.py",
             "production_receiver_driver.py")
    return {**{name: hashlib.sha256((Path(__file__).parent/name).read_bytes()).hexdigest() for name in names},
            "sender_drain_contract": contract.source_sha256()}


def validate_plan(plan):
    require(isinstance(plan, dict) and set(plan) == {"schema", "scope", "binding", "postgres_identity", "window", "sources"}
            and type(plan["schema"]) is int and plan["schema"] == 1 and plan["scope"] == "instrumented_sender_only"
            and plan["sources"] == sources(), "exact source-bound sender plan required")
    binding = checked_binding(plan["binding"], scope="instrumented_sender_only")
    require(binding["source_sha256"] == bridge_source(), "sender bridge source changed")
    lock = Path(__file__).resolve().parents[1]/"ops/miniflux/pause-source-lock.json"
    require(binding["application_source_sha256"] == hashlib.sha256(lock.read_bytes()).hexdigest(), "sender patch lock differs")
    contract.validate_identity(plan["postgres_identity"])
    window = plan["window"]
    require(isinstance(window, dict) and set(window) == {"not_before", "expires_at"}
            and all(type(v) is int for v in window.values()) and 0 < window["expires_at"] - window["not_before"] <= 7200,
            "finite sender control window required")
    return plan


class NativeSenderTransport:
    """Only fixed Docker inspect and authenticated loopback private gate calls."""
    def __init__(self, url, token_file):
        value = urlsplit(url)
        require(value.scheme == "http" and value.hostname == "127.0.0.1" and value.port and value.path in ("", "/")
                and not value.query and not value.fragment and not value.username and not value.password,
                "sender private loopback endpoint required")
        self.port, self.token_file = value.port, Path(token_file)
        require(self.token_file.is_absolute(), "absolute private sender token required")
        self.token_sha256 = contract.digest(self._token())

    def _token(self):
        value = read_private(self.token_file)
        require(isinstance(value, dict) and set(value) == {"token"} and isinstance(value["token"], str)
                and HEX64.fullmatch(value["token"]), "invalid sender control token")
        return value

    def inspect(self, component_id):
        require(isinstance(component_id, str) and HEX64.fullmatch(component_id), "exact sender container ID required")
        return parsed(_capture(["/usr/bin/docker", "--host", "unix:///var/run/docker.sock", "inspect", component_id]))

    def request(self, method, path, payload=None):
        require((method == "GET" and (path == "/registration" or (path.startswith("/operations/") and HEX64.fullmatch(path[12:]))))
                or (method == "POST" and path in {"/stop-new", "/pause"}), "sender control operation not allowed")
        token = self._token()
        require(contract.digest(token) == self.token_sha256, "sender credential changed")
        body = None if payload is None else encoded(payload)
        require(body is None or len(body) <= 8192, "sender request byte budget")
        started = time.monotonic()
        connection = http.client.HTTPConnection("127.0.0.1", self.port, timeout=2)
        try:
            connection.request(method, path, body=body, headers={"Authorization":"Bearer " + token["token"], "Content-Type":"application/json"})
            response = connection.getresponse()
            raw = bytearray()
            while len(raw) <= 65536:
                require(time.monotonic()-started < 2, "sender control deadline; outcome unknown")
                if connection.sock:
                    connection.sock.settimeout(max(0.001, 2-(time.monotonic()-started)))
                chunk = response.read1(min(4096, 65537-len(raw)))
                if not chunk:
                    break
                raw.extend(chunk)
            require(len(raw) <= 65536 and response.status in (200,409) and time.monotonic()-started < 2,
                    "sender response unconfirmed; exact query only")
            return response.status, parsed(bytes(raw))
        finally:
            connection.close()


class SenderDriver:
    def __init__(self, plan, transport, postgres, evidence_root):
        self.plan = validate_plan(parsed(encoded(plan)))
        require(isinstance(postgres, PostgresReader), "fixed sender PostgreSQL reader required")
        require(postgres.expected == self.plan["postgres_identity"], "sender reader targets different database")
        self.transport, self.postgres, self.root = transport, postgres, Path(evidence_root)
        private_directory(self.root, create=True)
        self.deadline = time.monotonic() + max(0, self.plan["window"]["expires_at"]-time.time())
        self.control = MinifluxDrainControl(self.plan["binding"], transport, self._inspect, self.root/"control")
        self.plan_sha256 = contract.digest(self.plan)
        path = self.root/"plan.json"
        if present(path):
            require(read_private(path) == self.plan, "sender plan changed across recovery")
        durable_write(path, self.plan)

    def _inspect(self):
        return self.transport.inspect(self.plan["binding"]["component"]["id"])

    def _window(self):
        require(self.plan["window"]["not_before"] <= time.time() < self.plan["window"]["expires_at"]
                and time.monotonic() < self.deadline, "sender operation window expired; preserve originals")

    def bind(self):
        validate_plan(self.plan)
        require(contract.digest(self.plan) == self.plan_sha256, "sender plan mutated")
        binding = self.plan["binding"]
        rows = self._inspect()
        inspect_binding(rows, {binding["component_name"]:binding["component"]})
        for row in rows:
            env = dict(item.split("=",1) for item in row["Config"]["Env"])
            require(env.get("HANDOFF_ENABLED") == "1" and env.get("HANDOFF_MANIFEST") == binding["manifest"],
                    "actual sender is not the bound instrumented instance")
        self.control.registration()
        sample = self.postgres.snapshot()
        require(sample["database_identity"] == self.plan["postgres_identity"]
                and sample["gate"]["manifest"] == binding["manifest"], "actual sender PG gate/identity differs")
        return sample

    def stop_new(self, operation, reason):
        self._window()
        self.bind()
        value = self.control.stop_new(operation, self.plan["binding"]["generation"], reason)
        self.bind()
        return value

    def query_stop_new(self, operation):
        # Recovery is separately bounded by original intent, 32 retained query
        # slots and per-I/O deadlines. It never grants another POST/window.
        self.bind()
        value = self.control.query_stop_new(operation)
        self.bind()
        return value

    def seal_originals(self, stop_operation):
        self._window()
        receipt = self.query_stop_new(stop_operation)
        with locked(self.control.root/"control.lock", True):
            self._window()
            self.bind()
            bundle = self.root/"originals"
            value = self.postgres.freeze(self.plan["binding"], receipt, self.plan["window"], bundle)
            self.bind()
            checked, digest = contract.load_bundle(bundle)
            require(checked == value, "sealed original bundle differs")
            return {"manifest_sha256":digest,"bundle":str(bundle),"queue_checked":False,"switch_authorized":False}

    def _sample(self, operation, phase, *, recovery=False):
        with locked(self.control.root/"control.lock", True):
            return self._sample_locked(operation, phase, recovery=recovery)

    def _sample_locked(self, operation, phase, *, recovery=False):
        if not recovery:
            self._window()
        paths = list(self.root.glob("inventory-*.json*"))
        require(len(paths) < MAX_SAMPLES, "sender inventory evidence budget exhausted")
        sample = self.bind()
        gate = sample["gate"]
        require(gate["phase"] == phase and gate["current_operation"] == operation, "sender inventory stage differs")
        leases = sample["retained_leases"]
        require(leases["active_producers"] == leases["uncertain_producers"] == 0, "sender producers remain")
        blockers = []
        for state in ("pending","inflight","held"):
            if sample["states"][state]:
                blockers.append("outbox_"+state)
        if sample["legacy"]["rows"]:
            blockers.append("legacy_held")
        if sample["expired_unacknowledged"]:
            blockers.append("expired_unacknowledged")
        if phase == "closed" and (leases["active"] or leases["uncertain"]):
            blockers.append("active_or_uncertain_lease")
        evidence = {"schema":1,"operation_id":operation,"phase":phase,"plan_sha256":self.plan_sha256,
                    "sample":sample,"blockers":blockers,"switch_authorized":False}
        durable_write(self.root/f"inventory-{len(paths):02d}.json", evidence)
        return evidence

    def inventory(self, stop_operation):
        return self._sample(stop_operation, "draining")

    def freeze(self, operation, stop_operation, reason):
        inventory = self.inventory(stop_operation)
        require(not inventory["blockers"], "sender not independently drained; preserve originals")
        value = self.control.freeze(operation, stop_operation, reason)
        final = self._sample(operation, "closed")
        require(not final["blockers"], "sender closed but final inventory blocked")
        return {"receipt":value,"inventory":final,"switch_authorized":False}

    def query_freeze(self, operation):
        # Exact recovery may finish the retained receipt after the write window.
        self.bind()
        value = self.control.query_freeze(operation)
        final = self._sample(operation, "closed", recovery=True)
        require(not final["blockers"], "sender closed but final inventory blocked")
        return {"receipt":value,"inventory":final,"switch_authorized":False}

    def abort_window(self, operation, stop_operation, reason):
        """Freeze late consumers without calling retained work drained or deleting it."""
        self._window()
        self.bind()
        value = self.control.freeze(operation, stop_operation, reason)
        return {"receipt":value,"queue_checked":False,"window_aborted":True,"switch_authorized":False}
