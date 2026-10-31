"""Isolation-only, source/image-bound bridge to the real Miniflux PG gate.

Client.request(method, path, payload) returns (HTTP status, decoded JSON).
The client must use the fixed instance's authenticated private control, not its
public HTTP API. No initialization, automatic resume, deployment or SSH exists.
"""
from __future__ import annotations

import hashlib
import os
from pathlib import Path
import re

from durable_handoff_cutover import CutoverError, durable_write, encoded, private_directory, read_private, require
from handoff_host_cron import locked, present
from handoff_runtime_adapter import _component, inspect_binding

HEX64 = re.compile(r"[0-9a-f]{64}\Z")
ENTRANCES = ("miniflux_scheduler", "miniflux_manual", "legacy_async_sender")


class PausePending(CutoverError):
    """Only a precise durable PG closing receipt; never identity/I/O failure."""


def sha(value):
    return hashlib.sha256(encoded(value)).hexdigest()


def source_sha256():
    directory = Path(__file__).parent
    names = ("miniflux_handoff_adapter.py", "handoff_runtime_adapter.py",
             "handoff_host_cron.py", "durable_handoff_cutover.py")
    return sha({name: hashlib.sha256((directory / name).read_bytes()).hexdigest() for name in names})


def checked_binding(value, *, scope="isolation"):
    keys = {"schema", "scope", "run_id", "control_id", "component_name", "component", "manifest",
            "application_source_sha256", "source_sha256", "generation", "initial_operation_id", "entrances"}
    require(isinstance(value, dict) and set(value) == keys and type(value["schema"]) is int
            and value["schema"] == 1 and value["scope"] == scope, "exact Miniflux binding scope required")
    require(re.fullmatch(r"[0-9a-f]{32}", value["run_id"])
            and re.fullmatch(r"[a-z][a-z0-9_-]{0,63}", value["control_id"]), "invalid bridge identity")
    for name in ("manifest", "application_source_sha256", "initial_operation_id"):
        require(isinstance(value[name], str) and HEX64.fullmatch(value[name]), "complete frozen identity required")
    require(value["source_sha256"] == source_sha256() and type(value["generation"]) is int
            and 0 <= value["generation"] < 2**63 - 2, "source or generation differs")
    require(value["entrances"] == list(ENTRANCES), "Miniflux bridge only covers its three named producer groups")
    require(not ({"external_postgres", "host_loopback_postgres"} & value["component"].keys())
            or value["control_id"] == "miniflux", "PostgreSQL policy requires miniflux control")
    _component(value["component_name"], value["component"], allow_external_postgres=scope == "instrumented_sender_only",
               allow_host_loopback_postgres=scope == "instrumented_sender_only")
    return value


def _pg_receipt(raw, manifest, operation, generation, action, state, *, allow_work=False):
    keys = {"schema", "manifest", "operation_id", "action", "previous_generation", "generation",
            "state", "durable", "confirmed", "active_leases", "uncertain_leases"}
    require(isinstance(raw, dict) and set(raw) == keys, "exact PostgreSQL receipt required")
    expected = {"schema": 1, "manifest": manifest, "operation_id": operation, "action": action,
                "previous_generation": generation - 1, "generation": generation, "state": state,
                "durable": True, "confirmed": True, "active_leases": 0, "uncertain_leases": 0}
    if allow_work:
        for key in ("active_leases", "uncertain_leases"):
            require(type(raw[key]) is int and 0 <= raw[key] <= 10000, "invalid real work counter")
            expected[key] = raw[key]
    require(all(type(raw[key]) is type(value) and raw[key] == value for key, value in expected.items()),
            "PG operation/manifest/generation/inflight result unconfirmed")
    return raw


def _pause_pending(status, raw, manifest, operation, generation):
    if status != 409 or set(raw) != {"result", "confirmed"} or raw["confirmed"] is not False:
        return False
    value = raw["result"]
    expected = {"schema": 1, "manifest": manifest, "operation_id": operation, "action": "pause",
                "previous_generation": generation - 1, "generation": generation, "state": "closing",
                "durable": True, "confirmed": False, "active_leases": 0, "uncertain_leases": 0}
    if not isinstance(value, dict) or set(value) != set(expected):
        return False
    for key in ("active_leases", "uncertain_leases"):
        if type(value[key]) is not int or not 0 <= value[key] <= 10000:
            return False
        expected[key] = value[key]
    return all(type(value[k]) is type(v) and value[k] == v for k, v in expected.items())


class MinifluxPauseControl:
    """Finite stop/query bridge. Unknown original work stays unconfirmed.

    inspect_current() must return actual Docker inspect rows for this exact
    component each time. Injected rows in unit tests are explicitly not Docker
    evidence. The fixed native harness uses fresh inspect on every call.
    """
    _scope = "isolation"

    def __init__(self, binding, client, inspect_current, evidence_root):
        self.binding = checked_binding(binding, scope=self._scope)
        self.client, self.inspect_current = client, inspect_current
        self.root = Path(evidence_root)
        private_directory(self.root, create=True)
        self.binding_sha256 = sha(binding)
        lock = self.root / "control.lock"
        if not present(lock):
            fd = os.open(lock, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
            try:
                os.fsync(fd)
            finally:
                os.close(fd)

    def _identity(self):
        checked_binding(self.binding, scope=self._scope)
        require(sha(self.binding) == self.binding_sha256, "frozen binding mutated")
        inspect_binding(self.inspect_current(), {self.binding["component_name"]: self.binding["component"]},
                        allow_external_postgres=self._scope == "instrumented_sender_only",
                        allow_host_loopback_postgres=self._scope == "instrumented_sender_only")

    def _exchange(self, method, path, payload=None):
        self._identity()
        status, raw = self.client.request(method, path, payload)
        require(type(status) is int and isinstance(raw, dict) and len(encoded(raw)) <= 65536,
                "bounded actual private control response required")
        self._identity()
        return status, raw

    def registration(self):
        status, raw = self._exchange("GET", "/registration")
        expected = {"schema": 1, "manifest": self.binding["manifest"],
                    "protocol": "miniflux-pg-handoff-v1", "business_open_not_assumed": True}
        require(status == 200 and set(raw) == set(expected)
                and all(type(raw[k]) is type(v) and raw[k] == v for k, v in expected.items()),
                "old/unpatched/wrong-manifest private control")
        return {"binding_sha256": self.binding_sha256, "source_sha256": self.binding["source_sha256"],
                "entrances": self.binding["entrances"]}

    def _intent(self, operation, generation, reason):
        require(isinstance(operation, str) and HEX64.fullmatch(operation)
                and type(generation) is int and generation == self.binding["generation"]
                and isinstance(reason, str) and 0 < len(reason) <= 256, "exact bound stop request required")
        return {"schema": 1, "action": "pause", "operation_id": operation,
                "binding_sha256": self.binding_sha256, "generation": generation, "reason": reason}

    def _save_exchange(self, name, status, raw):
        path = self.root / (name + ".json")
        value = {"status": status, "response": raw}
        require(not present(path) and not present(Path(str(path) + ".pending")), "exchange already recorded")
        durable_write(path, value)

    def _confirmed(self, status, raw, operation, generation, action, state, *, allow_work=False):
        if action == "pause" and state == "closed" and _pause_pending(status, raw, self.binding["manifest"], operation, generation):
            raise PausePending("precise durable PostgreSQL closing operation is still pending")
        require(status == 200 and set(raw) == {"result", "confirmed"} and raw["confirmed"] is True,
                "private control not confirmed; original response retained")
        return _pg_receipt(raw["result"], self.binding["manifest"], operation, generation, action, state, allow_work=allow_work)

    def _initial_open(self):
        status, raw = self._exchange("GET", "/operations/" + self.binding["initial_operation_id"])
        require(isinstance(raw.get("result"), dict), "initial operation missing")
        action = raw["result"].get("action")
        require(action in ("initialize", "resume"), "initial open is not an explicit approved operation")
        return self._confirmed(status, raw, self.binding["initial_operation_id"],
                               self.binding["generation"], action, "open", allow_work=True)

    def stop(self, operation, generation, reason):
        with locked(self.root / "control.lock", True):
            intent = self._intent(operation, generation, reason)
            require(not present(self.root / "pause.intent.json")
                    and not present(self.root / "pause.intent.json.pending"), "pause attempted; query only")
            self.registration()
            initial = self._initial_open()
            durable_write(self.root / "initial-open.json", initial)
            durable_write(self.root / "pause.intent.json", intent)
            status, raw = self._exchange("POST", "/pause", {"operation_id": operation})
            self._save_exchange("pause-response", status, raw)
            receipt = self._confirmed(status, raw, operation, generation + 1, "pause", "closed")
            return self._normalized(intent, receipt)

    def _load_intent(self, operation):
        require(not present(self.root / "pause.intent.json.pending"), "local intent sync uncertain; preserve")
        intent = read_private(self.root / "pause.intent.json")
        require(intent == self._intent(operation, self.binding["generation"], intent["reason"]), "stop intent conflict")
        durable_write(self.root / "pause.intent.json", intent)
        return intent

    def _normalized(self, intent, receipt):
        value = {"schema": 1, "control_id": self.binding["control_id"], "operation_id": intent["operation_id"],
                 **self.registration(), "previous_generation": intent["generation"],
                 "generation": intent["generation"] + 1, "reason": intent["reason"],
                 "state": "closed", "durable": True, "inflight": 0, "unknown": 0}
        path = self.root / "pause.confirmed.json"
        evidence = {"pg_receipt": receipt, "host_receipt": value}
        if present(path):
            require(read_private(path) == evidence, "confirmed original conflicts")
        durable_write(path, evidence)
        return value

    def query(self, operation):
        with locked(self.root / "control.lock", True):
            intent = self._load_intent(operation)
            self.registration()
            # Preserve bounded query attempts separately; never replace the
            # earlier unconfirmed pause or its original raw response.
            with _query_slot(self.root) as name:
                status, raw = self._exchange("GET", "/operations/" + operation)
                self._save_exchange(name, status, raw)
            receipt = self._confirmed(status, raw, operation, intent["generation"] + 1, "pause", "closed")
            return self._normalized(intent, receipt)

    def resume(self, approval):
        """Explicit separately approved isolation resume only; never called by stop/query."""
        with locked(self.root / "control.lock", True):
            value = self._approval(approval)
            require(not present(self.root / "resume.intent.json")
                    and not present(self.root / "resume.intent.json.pending"), "resume attempted; query only")
            original = self._load_intent(value["closed_operation_id"])
            require(original["generation"] + 1 == value["closed_generation"], "closed generation differs")
            status, raw = self._exchange("GET", "/operations/" + value["closed_operation_id"])
            self._confirmed(status, raw, value["closed_operation_id"], value["closed_generation"], "pause", "closed")
            durable_write(self.root / "resume.intent.json", value)
            status, raw = self._exchange("POST", "/resume", {"operation_id": value["operation_id"],
                                                            "closed_operation_id": value["closed_operation_id"]})
            self._save_exchange("resume-response", status, raw)
            return self._resume_confirmed(value, status, raw)

    def _approval(self, value):
        keys = {"schema", "scope", "binding_sha256", "closed_operation_id", "closed_generation",
                "operation_id", "separately_approved", "approval_reference"}
        require(isinstance(value, dict) and set(value) == keys and type(value["schema"]) is int and value["schema"] == 1
                and value["scope"] == "isolation" and value["binding_sha256"] == self.binding_sha256
                and value["separately_approved"] is True and type(value["closed_generation"]) is int
                and value["closed_generation"] == self.binding["generation"] + 1
                and isinstance(value["approval_reference"], str) and 0 < len(value["approval_reference"]) <= 256,
                "separate exact isolation resume approval required")
        require(all(isinstance(value[k], str) and HEX64.fullmatch(value[k]) for k in ("closed_operation_id", "operation_id"))
                and value["closed_operation_id"] != value["operation_id"], "exact distinct resume operation required")
        return value

    def _resume_confirmed(self, approval, status, raw):
        receipt = self._confirmed(status, raw, approval["operation_id"], approval["closed_generation"] + 1, "resume", "open")
        path = self.root / "resume.confirmed.json"
        if present(path):
            require(read_private(path) == receipt, "resume original conflicts")
        durable_write(path, receipt)
        return {"receipt": receipt, "scope": "isolation", "switch_authorized": False}

    def query_resume(self, approval):
        with locked(self.root / "control.lock", True):
            value = self._approval(approval)
            require(not present(self.root / "resume.intent.json.pending")
                    and read_private(self.root / "resume.intent.json") == value, "resume intent absent/conflicting")
            durable_write(self.root / "resume.intent.json", value)
            with _query_slot(self.root) as name:
                status, raw = self._exchange("GET", "/operations/" + value["operation_id"])
                self._save_exchange(name, status, raw)
            return self._resume_confirmed(value, status, raw)


def _query_slot(root):
    from contextlib import contextmanager

    @contextmanager
    def slot():
        # One bridge is one stop plus an optional explicitly approved resume.
        # Query capacity is retained, finite and never garbage-collected.
        slots = set()
        with os.scandir(root) as paths:
            for total, path in enumerate(paths):
                require(total < 96, "bridge file capacity exceeded")
                if path.name.startswith(("query-", "attempt-")):
                    match = re.fullmatch(r"(?:query|attempt)-(\d{2})\.json(?:\.pending)?", path.name)
                    require(match is not None, "unrecognized retained query evidence")
                    slots.add(int(match.group(1)))
        count = len(slots)
        require(slots == set(range(count)) and count < 32, "query evidence budget exhausted or conflicting")
        name = f"query-{count:02d}"
        # Charge before any request, including timeout/unknown outcomes. Old
        # query files and new reservations share the SAME32-slot namespace.
        durable_write(root / f"attempt-{count:02d}.json", {"schema":1,"slot":count,"status":"reserved"})
        yield name
    return slot()


class StopNewPending(CutoverError):
    """Exact durable producer stop exists; existing producers remain active/unknown."""


def checked_stop_new(raw, manifest, operation, generation, *, confirmed=True):
    keys = {"schema", "manifest", "operation_id", "action", "previous_generation", "generation",
            "state", "durable", "confirmed", "active_leases", "uncertain_leases",
            "active_producer_leases", "uncertain_producer_leases"}
    require(isinstance(raw, dict) and set(raw) == keys, "exact stop-new PostgreSQL receipt required")
    expected = {"schema": 1, "manifest": manifest, "operation_id": operation, "action": "stop_new",
                "previous_generation": generation - 1, "generation": generation, "state": "draining",
                "durable": True, "confirmed": confirmed}
    require(isinstance(operation, str) and HEX64.fullmatch(operation)
            and all(type(raw[k]) is type(v) and raw[k] == v for k, v in expected.items()), "stop-new identity/state differs")
    for key in ("active_leases", "uncertain_leases", "active_producer_leases", "uncertain_producer_leases"):
        require(type(raw[key]) is int and 0 <= raw[key] <= 10000, "invalid producer/consumer count")
    require(raw["active_producer_leases"] <= raw["active_leases"]
            and raw["uncertain_producer_leases"] <= raw["uncertain_leases"], "producer counts exceed total work")
    if confirmed:
        require(raw["active_producer_leases"] == raw["uncertain_producer_leases"] == 0, "producer completion unconfirmed")
    return raw


class MinifluxDrainControl(MinifluxPauseControl):
    """Instrumented sender stop-new, exact query, then full pause; never reopen.

    The host driver must bind PG and independently inventory originals. A
    confirmed producer stop explicitly allows durable consumers and says
    nothing about old unregistered work, receiver completion or switching.
    """
    _scope = "instrumented_sender_only"

    def stop(self, *args, **kwargs):
        raise CutoverError("staged sender requires stop_new then freeze")

    def resume(self, *args, **kwargs):
        raise CutoverError("staged sender has no reopen capability")

    def query_resume(self, *args, **kwargs):
        raise CutoverError("staged sender has no reopen capability")

    def _stage_intent(self, action, operation, generation, reason, prior):
        require(action in {"stop_new", "pause"} and isinstance(operation, str) and HEX64.fullmatch(operation)
                and operation != prior and isinstance(prior, str) and HEX64.fullmatch(prior)
                and type(generation) is int and generation == self.binding["generation"] + (action == "pause")
                and isinstance(reason, str) and 0 < len(reason) <= 256, "exact staged operation required")
        return {"schema": 1, "action": action, "operation_id": operation, "prior_operation_id": prior,
                "binding_sha256": self.binding_sha256, "generation": generation, "reason": reason}

    def _read_stage(self, action, operation):
        path = self.root / (action + ".intent.json")
        require(not present(Path(str(path) + ".pending")), "staged intent sync uncertain; preserve")
        intent = read_private(path)
        expected = self._stage_intent(action, operation, intent["generation"], intent["reason"], intent["prior_operation_id"])
        require(intent == expected, "staged intent differs")
        durable_write(path, intent)
        return intent

    def _stage_response(self, intent, status, raw):
        action, operation, generation = intent["action"], intent["operation_id"], intent["generation"] + 1
        if action == "stop_new":
            require(status in (200, 409) and set(raw) == {"result", "confirmed"}
                    and type(raw["confirmed"]) is bool and (status == 200) == raw["confirmed"], "stop-new result unknown")
            receipt = checked_stop_new(raw["result"], self.binding["manifest"], operation, generation, confirmed=raw["confirmed"])
            if not raw["confirmed"]:
                raise StopNewPending("registered producers remain; preserve original and query exact operation")
        else:
            receipt = self._confirmed(status, raw, operation, generation, "pause", "closed")
        path = self.root / (action + ".confirmed.json")
        evidence = {"intent_sha256": sha(intent), "pg_receipt": receipt, "queue_checked": False,
                    "legacy_originals_proven": False, "switch_authorized": False}
        if present(path):
            # Consumer counters can decrease after a confirmed producer stop.
            # Keep the first exact original, but compare immutable identity.
            old = read_private(path)
            expected = {k: v for k, v in old["pg_receipt"].items() if k not in {"active_leases", "uncertain_leases"}}
            require({k: v for k, v in receipt.items() if k in expected} == expected
                    and old["intent_sha256"] == evidence["intent_sha256"], "confirmed stage identity changed")
        else:
            durable_write(path, evidence)
        return receipt

    def stop_new(self, operation, generation, reason):
        with locked(self.root / "control.lock", True):
            intent = self._stage_intent("stop_new", operation, generation, reason, self.binding["initial_operation_id"])
            path = self.root / "stop_new.intent.json"
            require(not present(path) and not present(Path(str(path) + ".pending")), "stop-new attempted; exact query only")
            self.registration()
            durable_write(self.root / "initial-open.json", self._initial_open())
            durable_write(path, intent)
            status, raw = self._exchange("POST", "/stop-new", {"operation_id": operation})
            self._save_exchange("stop_new-response", status, raw)
            return self._stage_response(intent, status, raw)

    def query_stop_new(self, operation):
        return self._query_stage("stop_new", operation)

    def _query_stage(self, action, operation):
        with locked(self.root / "control.lock", True):
            intent = self._read_stage(action, operation)
            self.registration()
            with _query_slot(self.root) as name:
                status, raw = self._exchange("GET", "/operations/" + operation)
                self._save_exchange(name, status, raw)
            return self._stage_response(intent, status, raw)

    def freeze(self, operation, stop_operation, reason):
        with locked(self.root / "control.lock", True):
            prior = self._read_stage("stop_new", stop_operation)
            intent = self._stage_intent("pause", operation, prior["generation"] + 1, reason, stop_operation)
            path = self.root / "pause.intent.json"
            require(not present(path) and not present(Path(str(path) + ".pending")), "full pause attempted; exact query only")
            self.registration()
            with _query_slot(self.root) as name:
                status, raw = self._exchange("GET", "/operations/" + stop_operation)
                self._save_exchange(name, status, raw)
            self._stage_response(prior, status, raw)
            durable_write(path, intent)
            status, raw = self._exchange("POST", "/pause", {"operation_id": operation})
            self._save_exchange("pause-response", status, raw)
            return self._stage_response(intent, status, raw)

    def query_freeze(self, operation):
        return self._query_stage("pause", operation)


class MinifluxActivationControl(MinifluxPauseControl):
    """Explicit135 first CLOSED and receiver-capability-bound production CAS.

    No queue/history claim follows from initialize_closed. The Go endpoint
    validates its own CAS only; actual receiver checks belong to the retained
    ReceiverActivationSession capability, before and after the one POST.
    """
    _scope = "instrumented_sender_only"

    def __init__(self, binding, client, inspect_current, evidence_root, window):
        import time
        super().__init__(binding, client, inspect_current, evidence_root)
        self.window = self._checked_window(window)
        self.deadline = time.monotonic() + max(0, window["expires_at"] - time.time())
        path = self.root / "activation-binding.json"
        value = {"binding_sha256": self.binding_sha256, "window": self.window}
        if present(path):
            require(read_private(path) == value, "activation binding/window changed across recovery")
        durable_write(path, value)

    @staticmethod
    def _checked_window(value):
        require(isinstance(value, dict) and set(value) == {"not_before", "expires_at"}
                and all(type(v) is int for v in value.values())
                and 0 < value["expires_at"] - value["not_before"] <= 7200, "finite activation window required")
        return dict(value)

    def _window(self):
        import time
        require(self.window["not_before"] <= time.time() < self.window["expires_at"]
                and time.monotonic() < self.deadline, "sender activation window expired; query original operation only")

    def registration(self):
        status, raw = self._exchange("GET", "/activation-registration")
        expected = {"schema": 1, "manifest": self.binding["manifest"], "protocol": "miniflux-pg-activation-v1",
                    "database_schema": 135, "capabilities": ["initialize_closed", "production_resume"],
                    "business_open_not_assumed": True}
        require(status == 200 and set(raw) == set(expected)
                and all(type(raw[k]) is type(v) and raw[k] == v for k, v in expected.items()),
                "exact schema135 activation capability required")
        return expected

    def stop(self, *args, **kwargs):
        raise CutoverError("use staged sender stop/drain control")

    def query(self, *args, **kwargs):
        raise CutoverError("use exact named activation operation query")

    def resume(self, *args, **kwargs):
        raise CutoverError("production resume requires real receiver activation capability")

    def query_resume(self, *args, **kwargs):
        raise CutoverError("use exact production activation query")

    def _closed_intent(self, operation):
        require(self.binding["generation"] == 0 and operation == self.binding["initial_operation_id"],
                "first closed operation must match generation0 binding")
        return {"schema": 1, "action": "initialize_closed", "operation_id": operation,
                "binding_sha256": self.binding_sha256, "generation": 0, "window": self.window}

    def _persist_result(self, name, value):
        path = self.root / (name + ".confirmed.json")
        if present(path):
            require(read_private(path) == value, "activation confirmed original differs")
        durable_write(path, value)
        return value

    def initialize_closed(self, operation):
        with locked(self.root / "control.lock", True):
            self._window()
            intent = self._closed_intent(operation)
            require(not present(self.root / "initialize_closed.intent.json")
                    and not present(self.root / "initialize_closed.intent.json.pending"), "initial close attempted; exact query only")
            self.registration()
            self._window()
            durable_write(self.root / "initialize_closed.intent.json", intent)
            status, raw = self._exchange("POST", "/initialize-closed", {"operation_id": operation})
            self._save_exchange("initialize_closed-response", status, raw)
            receipt = self._confirmed(status, raw, operation, 0, "initialize_closed", "closed")
            return self._persist_result("initialize_closed", receipt)

    def recorded_initialize_closed(self, operation):
        """Already-confirmed closed initialization from disk only; never contacts Go."""
        with locked(self.root / "control.lock", True):
            require(not present(self.root / "initialize_closed.intent.json.pending")
                    and read_private(self.root / "initialize_closed.intent.json") == self._closed_intent(operation),
                    "original closed initialization intent missing/conflicting")
            path = self.root / "initialize_closed.confirmed.json"
            require(present(path), "confirmed closed initialization receipt missing")
            return _pg_receipt(read_private(path), self.binding["manifest"], operation, 0, "initialize_closed", "closed")

    def query_initialize_closed(self, operation):
        with locked(self.root / "control.lock", True):
            require(not present(self.root / "initialize_closed.intent.json.pending")
                    and read_private(self.root / "initialize_closed.intent.json") == self._closed_intent(operation),
                    "original closed initialization intent missing/conflicting")
            self.registration()
            with _query_slot(self.root) as name:
                status, raw = self._exchange("GET", "/operations/" + operation)
                self._save_exchange(name, status, raw)
            receipt = self._confirmed(status, raw, operation, 0, "initialize_closed", "closed")
            return self._persist_result("initialize_closed", receipt)

    def _receiver_proof(self, receiver_activation_proof, *, active=True):
        from services.production_activation_control import ReceiverActivationSession
        require(type(receiver_activation_proof) is ReceiverActivationSession,
                "actual ReceiverActivationSession capability required; no caller readiness flag")
        value = receiver_activation_proof.check_sender_resume(active=active)
        keys = {"operation_id", "descriptor_sha256", "manifest_sha256", "binding_sha256", "generation", "slots_sha256", "window"}
        require(isinstance(value, dict) and set(value) == keys
                and all(isinstance(value[k], str) and HEX64.fullmatch(value[k]) for k in keys - {"generation", "window"})
                and type(value["generation"]) is int and 0 <= value["generation"] < 2**63 - 2,
                "exact actual receiver activation proof required")
        self._checked_window(value["window"])
        # Detach immutable evidence from the live capability's in-memory object.
        from durable_handoff_cutover import parsed
        return parsed(encoded(value))

    def _production_intent(self, closed_operation, generation, proof):
        require(isinstance(closed_operation, str) and HEX64.fullmatch(closed_operation)
                and type(generation) is int and 0 <= generation < 2**63 - 2, "exact closed CAS identity required")
        identity = {"domain": "miniflux-production-resume-v1", "binding_sha256": self.binding_sha256,
                    "closed_operation_id": closed_operation, "closed_generation": generation,
                    "receiver_activation_proof": proof}
        return {"schema": 1, "action": "production_resume", **identity,
                "operation_id": sha(identity), "receiver_activation_proof_sha256": sha(proof), "window": self.window}

    def production_resume(self, closed_operation, generation, receiver_activation_proof):
        with locked(self.root / "control.lock", True):
            self._window()
            proof = self._receiver_proof(receiver_activation_proof)
            intent = self._production_intent(closed_operation, generation, proof)
            require(not present(self.root / "production_resume.intent.json")
                    and not present(self.root / "production_resume.intent.json.pending"), "production resume attempted; exact query only")
            self.registration()
            status, raw = self._exchange("GET", "/operations/" + closed_operation)
            action = raw.get("result", {}).get("action")
            require(action == "pause" or (action == "initialize_closed" and generation == 0), "exact supported closed action required")
            receipt = self._confirmed(status, raw, closed_operation, generation, action, "closed")
            durable_write(self.root / "production-closed-original.json", receipt)
            require(self._receiver_proof(receiver_activation_proof) == proof, "receiver proof changed before resume")
            self._window()
            durable_write(self.root / "production_resume.intent.json", intent)
            status, raw = self._exchange("POST", "/production-resume", {
                "operation_id": intent["operation_id"], "closed_operation_id": closed_operation})
            self._save_exchange("production_resume-response", status, raw)
            return self._production_confirmed(intent, status, raw, receiver_activation_proof, recovery=False)

    def _production_confirmed(self, intent, status, raw, capability, *, recovery):
        receipt = self._confirmed(status, raw, intent["operation_id"], intent["closed_generation"] + 1,
                                  "resume", "open", allow_work=True)
        # A failed post-check retains the actual response and original intent;
        # it never retries the write or reports a cross-service confirmation.
        require(self._receiver_proof(capability, active=not recovery) == intent["receiver_activation_proof"],
                "receiver changed after sender operation; preserve outcome and stop")
        value = {"pg_receipt": receipt, "operation_id": intent["operation_id"],
                 "receiver_activation_proof_sha256": intent["receiver_activation_proof_sha256"],
                 "historical_coverage": "unknown_before_T0_no_replay", "switch_authorized": False}
        # Work can start after OPEN; preserve each raw query's real counters.
        path = self.root / "production_resume.confirmed.json"
        if present(path):
            original = read_private(path)
            def stable(v):
                return {k: x for k, x in v.items() if k not in {"active_leases", "uncertain_leases"}}
            require({k:v for k,v in original.items() if k != "pg_receipt"} == {k:v for k,v in value.items() if k != "pg_receipt"}
                    and stable(original["pg_receipt"]) == stable(receipt), "production confirmed original conflicts")
            return value
        return self._persist_result("production_resume", value)

    def query_production_resume(self, operation, receiver_activation_proof):
        with locked(self.root / "control.lock", True):
            require(not present(self.root / "production_resume.intent.json.pending"), "resume intent sync uncertain")
            intent = read_private(self.root / "production_resume.intent.json")
            require(intent == self._production_intent(intent["closed_operation_id"], intent["closed_generation"],
                                                       intent["receiver_activation_proof"])
                    and operation == intent["operation_id"], "exact original production resume intent required")
            self.registration()
            with _query_slot(self.root) as name:
                status, raw = self._exchange("GET", "/operations/" + operation)
                self._save_exchange(name, status, raw)
            return self._production_confirmed(intent, status, raw, receiver_activation_proof, recovery=True)
