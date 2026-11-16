"""Bounded host driver for an already instrumented receiver, never a cutover.

Only fixed Docker inspect and authenticated loopback stop/query operations are
available. All local intents precede network writes; uncertain results permit
only exact-operation queries. Sender, cron, old originals, migration, start,
reopen and rollback are deliberately outside this receiver-only capability.
"""
from __future__ import annotations

import hashlib
import os
from pathlib import Path
import selectors
import socket
import subprocess
import time
from urllib.parse import urlsplit

from services import admission_fence as gate
from services.receiver_handoff_control import PROTOCOL, source_sha256 as control_source
from durable_handoff_cutover import durable_write, encoded, private_directory, read_private, require
from handoff_host_cron import locked, present
from handoff_runtime_adapter import _component, inspect_binding
from observe_handoff_capacity import sample_receiver, spool_identity, receipt as receipt_format

MAX_INSPECT_BYTES = 4 * 1024 * 1024
MAX_QUERIES = 32
MAX_SAMPLES = 32


def sha(value):
    return hashlib.sha256(encoded(value)).hexdigest()


def sources():
    return {"driver": hashlib.sha256(Path(__file__).read_bytes()).hexdigest(),
            "control": control_source(), "fence": gate.source_sha256(),
            **{name: hashlib.sha256((Path(__file__).parent / (name + ".py")).read_bytes()).hexdigest()
               for name in ("durable_handoff_cutover", "handoff_host_cron", "handoff_runtime_adapter", "observe_handoff_capacity")},
            "receipts": hashlib.sha256(Path(receipt_format.__file__).read_bytes()).hexdigest()}


def _capture(argv, *, seconds=3, max_bytes=MAX_INSPECT_BYTES):
    """Bound time AND streaming bytes; only the local inspect client is killed."""
    process = subprocess.Popen(argv, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                               shell=False, env={"PATH": "/usr/bin:/bin", "LC_ALL": "C"})
    deadline, output, total = time.monotonic() + seconds, bytearray(), 0
    try:
        with selectors.DefaultSelector() as selector:
            for stream in (process.stdout, process.stderr):
                os.set_blocking(stream.fileno(), False)
                selector.register(stream, selectors.EVENT_READ)
            while selector.get_map():
                remaining = deadline - time.monotonic()
                require(remaining > 0, "inspect deadline exceeded")
                for key, _ in selector.select(remaining):
                    chunk = os.read(key.fd, min(65536, max_bytes + 1 - total))
                    if not chunk:
                        selector.unregister(key.fileobj)
                        continue
                    total += len(chunk)
                    require(total <= max_bytes, "inspect output capacity exceeded")
                    if key.fileobj is process.stdout:
                        output.extend(chunk)
            require(process.wait(timeout=max(0.001, deadline - time.monotonic())) == 0, "inspect failed")
        return bytes(output)
    finally:
        if process.poll() is None:
            process.kill()
        process.wait(timeout=1)
        process.stdout.close()
        process.stderr.close()


class NativeTransport:
    def __init__(self, url, token_file):
        value = urlsplit(url)
        require(value.scheme == "http" and value.hostname == "127.0.0.1" and value.port
                and value.path in ("", "/") and not value.query and not value.fragment
                and not value.username and not value.password, "authenticated loopback endpoint required")
        self.port = value.port
        self.token_file = Path(token_file)
        require(self.token_file.is_absolute(), "absolute private token file required")
        self.token_sha256 = sha(self._token())

    def _token(self):
        value = read_private(self.token_file)
        require(isinstance(value, dict) and set(value) == {"token"}
                and isinstance(value["token"], str) and gate.HEX64.fullmatch(value["token"]), "invalid private token")
        return value

    def inspect(self, ids):
        require(isinstance(ids, tuple) and 1 <= len(ids) <= 8
                and all(isinstance(v, str) and gate.HEX64.fullmatch(v) for v in ids), "full component ids required")
        return gate._parsed(_capture(["/usr/bin/docker", "--host", "unix:///var/run/docker.sock", "inspect", *ids]))

    def request(self, method, path, value=None):
        require((method == "GET" and (path in {"/registration", "/snapshot", "/drain/registration"}
                or (path.startswith("/operations/") and gate.HEX64.fullmatch(path[12:]))
                or (path.startswith("/drain/operations/") and gate.HEX64.fullmatch(path[18:]))))
                or (method == "POST" and path in {"/stop-new", "/freeze", "/drain/transfer"}), "unsupported control request")
        token = self._token()
        require(sha(token) == self.token_sha256, "control credential changed")
        body = b"" if value is None else encoded(value)
        require(len(body) <= 8192, "control request capacity exceeded")
        raw = (f"{method} {path} HTTP/1.1\r\nHost: 127.0.0.1:{self.port}\r\n"
               f"Authorization: Bearer {token['token']}\r\nContent-Type: application/json\r\n"
               f"Content-Length: {len(body)}\r\nConnection: close\r\n\r\n").encode() + body
        deadline = time.monotonic() + 2
        with socket.create_connection(("127.0.0.1", self.port), timeout=2) as connection:
            connection.settimeout(max(0.001, deadline - time.monotonic()))
            connection.sendall(raw)
            data = bytearray()
            while b"\r\n\r\n" not in data:
                _receive(connection, data, deadline, 8192)
            headers, payload = bytes(data).split(b"\r\n\r\n", 1)
            lines = headers.decode("ascii").split("\r\n")
            require(lines[0].split(" ", 2)[:2] in (["HTTP/1.0", "200"], ["HTTP/1.1", "200"]),
                    "control result unconfirmed; query exact operation")
            fields = {}
            for line in lines[1:]:
                key, separator, item = line.partition(":")
                key = key.lower()
                require(separator and key not in fields, "duplicate/malformed response header")
                fields[key] = item.strip()
            length = fields.get("content-length", "")
            require("transfer-encoding" not in fields and length.isascii() and length.isdecimal()
                    and 0 < int(length) <= 65536 and fields.get("content-type") == "application/json",
                    "bounded complete control response required")
            data = bytearray(payload)
            require(len(data) <= int(length), "control response length differs")
            while len(data) < int(length):
                _receive(connection, data, deadline, int(length))
            return gate._parsed(bytes(data))


def _receive(connection, data, deadline, limit):
    remaining = deadline - time.monotonic()
    require(remaining > 0 and len(data) < limit, "control response deadline/capacity")
    connection.settimeout(remaining)
    chunk = connection.recv(min(4096, limit - len(data)))
    require(chunk, "control response incomplete")
    data.extend(chunk)


def validate_plan(plan):
    require(isinstance(plan, dict) and set(plan) == {"schema", "scope", "sources", "components", "registration",
            "host_fence_root", "container_fence_root", "spool", "container_spool_root", "spool_identity",
            "not_before", "expires_at", "owner", "capacity"}
            and type(plan["schema"]) is int and plan["schema"] == 1
            and plan["scope"] == "instrumented_receiver_only" and plan["sources"] == sources(),
            "exact source-bound receiver-only plan required")
    require(type(plan["not_before"]) is int and type(plan["expires_at"]) is int
            and 0 < plan["expires_at"] - plan["not_before"] <= 7200
            and isinstance(plan["owner"], str) and 0 < len(plan["owner"]) <= 128, "finite window/operator required")
    registration = plan["registration"]
    require(isinstance(registration, dict) and set(registration) == {"schema", "protocol", "source_sha256",
            "manifest_sha256", "binding", "fence_root", "filesystem", "actions", "switch_authorized",
            "sender_controlled", "legacy_originals_proven"}
            and registration["protocol"] == PROTOCOL and type(registration["schema"]) is int
            and registration["schema"] == 1 and registration["source_sha256"] == control_source()
            and gate.HEX64.fullmatch(str(registration["manifest_sha256"]))
            and registration["actions"] == ["stop_new", "freeze", "query"]
            and registration["switch_authorized"] is False and registration["sender_controlled"] is False
            and registration["legacy_originals_proven"] is False, "old or mismatched receiver control")
    gate._binding(registration["binding"])
    for field in ("host_fence_root", "spool"):
        path = Path(plan[field])
        require(path.is_absolute() and path.resolve() == path, "canonical bound host path required")
    require(registration["fence_root"] == plan["host_fence_root"]
            and all(Path(plan[key]).is_absolute() for key in ("container_fence_root", "container_spool_root")),
            "bound host/container data paths required")
    require(isinstance(plan["components"], dict) and 2 <= len(plan["components"]) <= 8, "all receiver web/worker components required")
    roles = set()
    for name, component in plan["components"].items():
        _component(name, component)
        roles.add(component["role"])
        require(component["role"] in {"web", "worker"} and component["image"] == registration["binding"]["image_id"]
                and component["revision"] == registration["binding"]["revision"], "receiver component contract differs")
        _bound_mount(component["mounts"], plan["host_fence_root"], plan["container_fence_root"])
        _bound_mount(component["mounts"], plan["spool"], plan["container_spool_root"])
    require(roles == {"web", "worker"}, "both receiver roles must be bound")
    policy = plan["capacity"]
    require(isinstance(policy, dict) and set(policy) == {"stop_records", "stop_bytes", "minimum_free_bytes"}
            and all(type(v) is int for v in policy.values()) and 1 <= policy["stop_records"] < 4096
            and 1 <= policy["stop_bytes"] <= 256 * 1024**2 - receipt_format.MAX_ALIAS_RESERVATION
            and policy["minimum_free_bytes"] >= receipt_format.MAX_ALIAS_RESERVATION,
            "reservation-aware finite receipt stop policy required")
    return plan


def _bound_mount(mounts, host, container):
    """Account for a data-root bind and reject a shadowing nested mount."""
    matches = [(len(Path(mount["Destination"]).parts), mount) for mount in mounts
               if Path(container).is_relative_to(Path(mount["Destination"]))]
    require(matches, "bound receiver path is not mounted")
    depth = max(item[0] for item in matches)
    selected = [item for level, item in matches if level == depth]
    require(len(selected) == 1, "ambiguous receiver mount destination")
    mount = selected[0]
    resolved = Path(mount["Source"]) / Path(container).relative_to(mount["Destination"])
    require(mount["Type"] == "bind" and mount["RW"] is True and resolved == Path(host),
            "actual fence/spool bind mapping differs")


class ReceiverDriver:
    def __init__(self, plan, transport, evidence_root):
        self.plan = validate_plan(gate._parsed(encoded(plan)))
        self.transport, self.root = transport, Path(evidence_root)
        private_directory(self.root, create=True)
        self.plan_sha256 = sha(self.plan)
        self.deadline = time.monotonic() + max(0, plan["expires_at"] - time.time())
        path = self.root / "controller.lock"
        if not present(path):
            fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
            try:
                os.fsync(fd)
            finally:
                os.close(fd)

    def _window(self):
        require(self.plan["not_before"] <= time.time() < self.plan["expires_at"]
                and time.monotonic() < self.deadline, "receiver operation window expired")

    def bind(self):
        rows = self.transport.inspect(tuple(item["id"] for item in self.plan["components"].values()))
        observed = inspect_binding(rows, self.plan["components"])
        for row in rows:
            env = dict(item.split("=", 1) for item in row["Config"]["Env"])
            require(env.get("AI_FILTER_ADMISSION_MODE") == "enforced"
                    and env.get("AI_FILTER_ADMISSION_ROOT") == self.plan["container_fence_root"]
                    and env.get("WEBHOOK_SPOOL_DIR") == self.plan["container_spool_root"]
                    and gate._parsed(env.get("AI_FILTER_ADMISSION_BINDING", "null")) == self.plan["registration"]["binding"],
                    "old/uninstrumented or different actual receiver fence")
        registration = self.transport.request("GET", "/registration")
        require(registration == self.plan["registration"], "actual receiver registration differs")
        require(spool_identity(Path(self.plan["host_fence_root"])) == registration["filesystem"], "host fence inode changed")
        manifest = gate._read(Path(self.plan["host_fence_root"]) / "manifest.json")
        require(gate._hash(manifest) == registration["manifest_sha256"]
                and manifest["binding"] == registration["binding"]
                and self.plan["not_before"] >= manifest["policy"]["not_before"]
                and self.plan["expires_at"] <= manifest["policy"]["expires_at"], "controller window/fence policy differs")
        return {"identity": observed, "registration": registration, "switch_authorized": False}

    def _snapshot(self):
        value = self.transport.request("GET", "/snapshot")
        require(set(value) == {"registration", "fence"} and value["registration"] == self.plan["registration"], "snapshot binding differs")
        fence = value["fence"]
        require(fence["binding"] == self.plan["registration"]["binding"]
                and fence["manifest_sha256"] == self.plan["registration"]["manifest_sha256"]
                and type(fence["generation"]) is int and 0 <= fence["generation"] < 2**63 - 1, "snapshot fence differs")
        return fence

    def _receipt(self, response, intent):
        require(isinstance(response, dict) and set(response) == {"registration", "receipt"}
                and response["registration"] == self.plan["registration"], "receipt registration differs")
        value = response["receipt"]
        request = {"action": intent["action"], "operation_id": intent["operation_id"],
                   "expected_generation": intent["generation"], "reason": intent["reason"],
                   "manifest_sha256": self.plan["registration"]["manifest_sha256"]}
        expected = {"schema": 1, **request, "request_sha256": gate._hash(request),
                    "generation": intent["generation"] + 1,
                    "phase": "draining" if intent["action"] == "stop_new" else "frozen",
                    "durable": True, "queue_checked": False, "switch_authorized": False}
        require(isinstance(value, dict) and set(value) == set(expected) | {"activity_drained"}
                and type(value["activity_drained"]) is bool
                and all(type(value[k]) is type(v) and value[k] == v for k, v in expected.items())
                and (intent["action"] != "freeze" or value["activity_drained"]), "wrong or unconfirmed operation receipt")
        return value

    def operate(self, action, operation_id, reason):
        with locked(self.root / "controller.lock", True):
            self._window()
            require(action in {"stop_new", "freeze"} and isinstance(operation_id, str) and gate.HEX64.fullmatch(operation_id)
                    and isinstance(reason, str) and 0 < len(reason) <= 256, "exact stop/freeze request required")
            intents = self._intents()
            require(not any(item["action"] == action for item in intents)
                    and all(present(self.root / (item["operation_id"] + ".receipt.json")) for item in intents),
                    "action attempted or uncertain operation remains; query exact original")
            self.bind()
            snapshot = self._snapshot()
            require(snapshot["phase"] == ("open" if action == "stop_new" else "draining"), "wrong receiver phase")
            require(action != "freeze" or (len(intents) == 1 and intents[0]["action"] == "stop_new"),
                    "this controller must first confirm stop_new")
            intent = {"schema": 1, "plan_sha256": self.plan_sha256, "action": action,
                      "operation_id": operation_id, "reason": reason, "generation": snapshot["generation"]}
            durable_write(self.root / (operation_id + ".intent.json"), intent)
            self._window()
            response = self.transport.request("POST", "/" + action.replace("_", "-"),
                {key: intent[key] for key in ("operation_id", "generation", "reason")})
            value = self._receipt(response, intent)
            queried = self._receipt(self.transport.request("GET", "/operations/" + operation_id), intent)
            require(queried == value, "independent operation query differs")
            self.bind()
            durable_write(self.root / (operation_id + ".receipt.json"), value)
            return value

    def _intents(self, *, query=False):
        values = []
        with os.scandir(self.root) as entries:
            for count, entry in enumerate(entries):
                require(count < 2 * MAX_QUERIES + 2 * MAX_SAMPLES + 16, "controller evidence capacity exceeded")
                require(not entry.name.endswith(".pending") or (query and entry.name.endswith(".receipt.json.pending")),
                        "partial evidence remains; preserve and review")
                if entry.name.endswith(".intent.json"):
                    value = read_private(Path(entry.path))
                    require(set(value) == {"schema", "plan_sha256", "action", "operation_id", "reason", "generation"}
                            and value["plan_sha256"] == self.plan_sha256 and value["action"] in {"stop_new", "freeze"}
                            and type(value["schema"]) is int and value["schema"] == 1
                            and isinstance(value["operation_id"], str) and gate.HEX64.fullmatch(value["operation_id"])
                            and type(value["generation"]) is int and 0 <= value["generation"] < 2**63 - 1
                            and isinstance(value["reason"], str) and 0 < len(value["reason"]) <= 256
                            and entry.name == value["operation_id"] + ".intent.json", "saved intent differs")
                    values.append(value)
        require(len(values) <= 2, "controller permits one stop and one freeze")
        return values

    def query(self, operation_id):
        # Confirmation remains available after the mutation window expires.
        # Each query has independent transport bounds and retained finite slots.
        with locked(self.root / "controller.lock", True):
            intents = self._intents(query=True)
            matches = [item for item in intents if item["operation_id"] == operation_id]
            require(len(matches) == 1, "exact existing operation required")
            prefix = self.root / (operation_id + ".query-")
            for number in range(MAX_QUERIES):
                attempt = Path(str(prefix) + str(number) + ".json")
                if not present(attempt):
                    break
            else:
                require(False, "query evidence capacity exhausted; operator review required")
            durable_write(attempt, {"operation_id": operation_id, "plan_sha256": self.plan_sha256})
            self.bind()
            value = self._receipt(self.transport.request("GET", "/operations/" + operation_id), matches[0])
            self.bind()
            path = self.root / (operation_id + ".receipt.json")
            require(not present(path) or read_private(path) == value, "original receipt conflicts")
            durable_write(path, value)
            return value

    def capacity_tick(self, operation_id):
        """One reservation-aware sample; any observation failure stops intake.

        This never claims to stop sender production. A stopped receiver can
        reject new delivery; sender original preservation remains a prerequisite.
        """
        with locked(self.root / "controller.lock", True):
            require(not self._intents(), "a stop was already attempted; query exact original")
            reason = "receipt_observation_count_limit"
            for number in range(MAX_SAMPLES):
                path = self.root / ("sample-" + str(number) + ".json")
                if not present(path):
                    break
            else:
                path = None
            if path is not None:
                # Reserve the attempt durably even when sampling itself fails.
                # Restart cannot reset a trial's observation budget.
                durable_write(path, {"plan_sha256": self.plan_sha256, "attempt": number})
                reason = "receipt_observation_failed"
                try:
                    self.bind()
                    sample = sample_receiver(self.plan["spool"], self.plan["spool_identity"], seconds=1)
                    durable_write(path.with_suffix(".result.json"), sample)
                    policy = self.plan["capacity"]
                    if sample["records"] >= policy["stop_records"]:
                        reason = "receipt_record_limit"
                    elif sample["reserved_bytes"] >= policy["stop_bytes"]:
                        reason = "receipt_reservation_limit"
                    elif sample["physical_free_bytes"] < policy["minimum_free_bytes"]:
                        reason = "receipt_physical_space_limit"
                    else:
                        self._window()
                        return {"status": "observed", "sample": sample, "switch_authorized": False}
                except Exception:
                    pass
        # Window expiry is not permission to mutate indefinitely. The fence's
        # own wall+boot clock policy independently refuses new intake at expiry.
        return {"status": "receiver_intake_stopped", "receipt": self.operate("stop_new", operation_id, reason),
                "sender_stopped": False, "switch_authorized": False}


if __name__ == "__main__":
    raise SystemExit("Receiver control library only: no first-takeover, deploy, reopen or rollback CLI")
