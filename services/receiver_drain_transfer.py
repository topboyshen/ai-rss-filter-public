"""Bounded, exact-original durable transfer into an already draining receiver.

Preparing is a WRITE: originals enter the real queue and ReceiptStore. Only
after every original is durable may the ordinary authenticated webhook repeat
those same deliveries. No caller flag permits new intake, migration or reopen.
"""
from __future__ import annotations

from contextlib import contextmanager
import hashlib
import hmac
import os
from pathlib import Path
import time
from urllib.parse import urlsplit

from services import admission_fence as gate
from services import receiver_drain_storage as disk
from services import sender_drain_contract as originals

MAX_BODY_BYTES = 1024 * 1024
MAX_ITEMS = 4096
MAX_FROZEN_BYTES = 32 * 1024**2
MAX_BATCHES = 256
BATCH_SIZE = 16
MAX_REPLAYS = 4096
SOURCE_FILES = ("services/receiver_drain_transfer.py", "services/receiver_drain_storage.py",
                "services/receiver_drain_control.py",
                "services/sender_drain_contract.py", "services/admission_fence.py",
                "services/admission_http.py", "services/webhook_receipts.py", "routes/webhook_routes.py",
                "routes/security.py", "utils/helpers.py", "core/user_feedback.py", "db/database.py", "app.py")
REQUEST_KEYS = {"schema", "operation_id", "stop_operation_id", "generation", "receiver_binding",
                "receiver_manifest_sha256", "source_sha256", "spool", "spool_identity",
                "sender_bundle", "sender_manifest_sha256", "sender_binding", "postgres_identity",
                "endpoint", "contract", "config_sha256", "body_limit", "max_batches", "max_replays"}


class CompletionUnconfirmed(gate.AdmissionUncertain):
    """Business transfer finished; only its completion evidence needs query."""


def sources():
    root = Path(__file__).resolve().parents[1]
    return {name: hashlib.sha256((root / name).read_bytes()).hexdigest() for name in SOURCE_FILES}


def _webhook():
    from routes import webhook_routes
    return webhook_routes


def config_sha256():
    return gate._hash(_webhook()._cfg())


def body_limit():
    from app import app
    value = app.config.get("MAX_CONTENT_LENGTH")
    gate._require(type(value) is int and value > 0, "actual application body limit absent")
    return min(value, MAX_BODY_BYTES)


def _endpoint(value):
    gate._require(isinstance(value, str) and len(value) <= 2048, "exact webhook endpoint required")
    url = urlsplit(value)
    gate._require(url.scheme in {"http", "https"} and url.hostname and url.path == "/webhook"
                  and not url.query and not url.fragment and not url.username and not url.password
                  and not any(ord(c) <= 32 or ord(c) == 127 for c in value), "unsupported drain endpoint")


def _signature(raw, supplied):
    secret = os.environ.get("MINIFLUX_WEBHOOK_SECRET", "").strip().encode()
    gate._require(secret and isinstance(supplied, str) and gate.HEX64.fullmatch(supplied)
                  and hmac.compare_digest(hmac.new(secret, raw, hashlib.sha256).hexdigest(), supplied),
                  "original signature no longer verifies; never re-sign")


def _request(value, fence):
    gate._require(isinstance(value, dict) and set(value) == REQUEST_KEYS
                  and type(value["schema"]) is int and value["schema"] == 1, "exact transfer request required")
    for key in ("operation_id", "stop_operation_id", "receiver_manifest_sha256", "sender_manifest_sha256", "config_sha256"):
        gate._require(isinstance(value[key], str) and gate.HEX64.fullmatch(value[key]), "invalid transfer identity")
    gate._require(value["operation_id"] != value["stop_operation_id"]
                  and value["receiver_binding"] == fence.binding
                  and value["receiver_manifest_sha256"] == gate._hash(fence._manifest())
                  and value["source_sha256"] == sources()
                  and value["config_sha256"] == config_sha256()
                  and type(value["body_limit"]) is int and value["body_limit"] == body_limit(),
                  "transfer receiver/source/config drift")
    gate._require(type(value["generation"]) is int and value["generation"] > 0
                  and type(value["max_batches"]) is int and 1 <= value["max_batches"] <= MAX_BATCHES
                  and type(value["max_replays"]) is int and 1 <= value["max_replays"] <= MAX_REPLAYS,
                  "finite transfer operation bounds required")
    gate._require(value["spool"] == str(_webhook()._webhook_spool_dir)
                  and disk.identity(value["spool"]) == value["spool_identity"], "actual transfer spool differs")
    disk.directory(value["sender_bundle"])
    originals.validate_binding(value["sender_binding"])
    originals.validate_identity(value["postgres_identity"])
    _endpoint(value["endpoint"])
    gate._require(isinstance(value["contract"], str) and 0 < len(value["contract"]) <= 256,
                  "exact sender delivery contract required")
    gate._require(_webhook()._delivery_strategy() == fence.binding["strategy"]
                  and os.environ.get("AI_FILTER_REVISION") == fence.binding["revision"], "receiver acceptance identity differs")


def _producer_boundary(fence, request):
    """Read actual state and retained leases, never trust a closed=true input."""
    with fence._metadata():
        manifest = fence._manifest()
        state = gate._state(gate._read(fence.root / "state.json"))
        leases, finishes = fence._leases()
        _, pending = fence._operation_inventory()
        gate._require(gate._hash(manifest) == request["receiver_manifest_sha256"]
                      and state["phase"] == "draining" and state["generation"] == request["generation"]
                      and not state["pending_operation"] and not pending, "receiver is not exactly draining")
        gate._require(not any(row["kind"] == "intake" and key not in finishes for key, row in leases.items())
                      and not fence._orphaned(leases, finishes)
                      and not any(row["outcome"] != "finished" for row in finishes.values()),
                      "receiver producers or uncertain activities remain")


def _window(intent, manifest):
    clock = gate._opening_clock(intent["clock"])
    window = manifest["window"]
    now, elapsed = time.time(), gate._elapsed() - clock["elapsed"]
    gate._require(window["not_before"] <= clock["wall"] <= now < window["expires_at"]
                  and 0 <= elapsed < window["expires_at"] - clock["wall"], "fixed transfer window expired or clock changed")


def _original_item(bundle, row, request):
    raw = originals.read_original(bundle, row)
    gate._require(len(raw) <= request["body_limit"] and row["target_url"] == request["endpoint"]
                  and row["contract"] == request["contract"], "original body/endpoint/contract outside receiver boundary")
    _signature(raw, row["signature"])
    payload = gate._parsed(raw)
    gate._require(isinstance(payload, dict) and isinstance(payload.get("feed"), dict)
                  and isinstance(payload.get("entries"), list) and len(payload["entries"]) == 1
                  and isinstance(payload["entries"][0], dict)
                  and payload["feed"].get("id") == row["feed_id"]
                  and payload["feed"].get("user_id") == row["user_id"]
                  and payload["entries"][0].get("id") == row["entry_id"], "sender original business identity differs")
    task = _webhook().normalize_webhook_delivery(raw)
    return {"schema": 1, "original": row, "task": task, "task_sha256": _webhook()._task_digest(task)}


class DrainTransfer:
    """One immutable bounded transfer per provisioned receiver fence."""
    def __init__(self, fence):
        gate._require(fence is not None, "enforced receiver fence required")
        self.fence = fence
        self.root = fence.root / "drain-transfer"

    @contextmanager
    def _locked(self, *, create=False):
        disk.directory(self.root, create=create)
        if create:
            disk.create_lock(self.root / ".lock")
        with gate._locked(self.root / ".lock", True, 1):
            yield

    def _intent(self, operation_id):
        gate._require(isinstance(operation_id, str) and gate.HEX64.fullmatch(operation_id), "exact transfer operation required")
        path = self.root / "intent.json"
        pending = path.with_name(path.name + ".pending")
        value = disk.read(pending if gate._present(pending) else path)
        gate._require(value["request"]["operation_id"] == operation_id
                      and value["request_sha256"] == gate._hash(value["request"]), "transfer operation identity differs")
        _request(value["request"], self.fence)
        disk.once(path, value)
        return value

    def prepare(self, request):
        _request(request, self.fence)
        gate._require(not gate._present(self.root / "intent.json")
                      and not gate._present(self.root / "intent.json.pending"), "already attempted; query exact transfer only")
        stop = self.fence.query_operation(request["stop_operation_id"], wait_seconds=0)
        gate._require(stop["action"] == "stop_new" and stop["generation"] == request["generation"]
                      and stop["phase"] == "draining" and stop["durable"] is True, "exact receiver stop receipt required")
        _producer_boundary(self.fence, request)
        manifest, sha = originals.load_bundle(request["sender_bundle"])
        self._bundle_matches(request, manifest, sha)
        # Validate EVERY original before the first queue/receipt write. A large,
        # expired, re-signed or mismatched tail must not cause partial transfer.
        items = self._freeze_items(request, manifest)
        with self._locked(create=True):
            gate._require(not gate._present(self.root / "intent.json")
                          and not gate._present(self.root / "intent.json.pending"), "already attempted; query exact transfer only")
            intent = {"request": request, "request_sha256": gate._hash(request),
                      "clock": {"boot_id": gate._boot_identity(), "elapsed": gate._elapsed(), "wall": time.time()},
                      "bundle_identity": disk.identity(request["sender_bundle"]), "stop_receipt": stop}
            disk.once(self.root / "intent.json", intent)
            self._seal_items(intent, manifest, items)
        return self.query(request["operation_id"])

    @staticmethod
    def _bundle_matches(request, manifest, sha):
        gate._require(sha == request["sender_manifest_sha256"]
                      and manifest["sender_binding"] == request["sender_binding"]
                      and manifest["postgres_identity"] == request["postgres_identity"]
                      and manifest["control_receipt"]["uncertain_leases"] == 0,
                      "sender manifest/PG/control identity differs")
        gate._require(len(manifest["entries"]) <= MAX_ITEMS, "transfer exceeds retained receiver record limit")

    @staticmethod
    def _freeze_items(request, manifest):
        result, total = [], 0
        for row in manifest["entries"]:
            item = _original_item(Path(request["sender_bundle"]), row, request)
            total += len(gate._encoded(item))
            gate._require(total <= MAX_FROZEN_BYTES, "frozen normalization evidence capacity exceeded")
            result.append(item)
        return result

    def _seal_items(self, intent, manifest, items):
        directory = disk.directory(self.root / "items", create=True)
        index = {}
        for item in items:
            delivery = item["original"]["delivery_id"]
            disk.once(directory / (delivery + ".json"), item)
            index[delivery] = gate._hash(item)
        plan = {"schema": 1, "intent_sha256": gate._hash(intent), "items": index,
                "window": manifest["window"], "sender_stop_sha256": gate._hash(manifest["control_receipt"])}
        disk.once(self.root / "plan.json", plan)

    def _plan(self, intent):
        request = intent["request"]
        if not gate._present(self.root / "plan.json"):
            manifest, sha = originals.load_bundle(request["sender_bundle"])
            self._bundle_matches(request, manifest, sha)
            gate._require(disk.identity(request["sender_bundle"]) == intent["bundle_identity"], "sender bundle inode changed")
            self._seal_items(intent, manifest, self._freeze_items(request, manifest))
        plan = disk.confirm(self.root / "plan.json")
        gate._require(plan["intent_sha256"] == gate._hash(intent), "transfer plan identity differs")
        return plan

    def _item(self, plan, delivery):
        item = disk.confirm(self.root / "items" / (delivery + ".json"))
        gate._require(gate._hash(item) == plan["items"][delivery]
                      and item["original"]["delivery_id"] == delivery
                      and _webhook()._task_digest(item["task"]) == item["task_sha256"], "frozen original/task changed")
        return item

    def _receipt(self, item, directory):
        webhook = _webhook()
        store = webhook._receipt_store(directory)
        record = store.load(item["original"]["delivery_id"])
        if record is None:
            return None
        gate._require(record["task_digest"] == item["task_sha256"]
                      and record["accepted_strategy"] == self.fence.binding["strategy"]
                      and record["accepted_revision"] == self.fence.binding["revision"], "original accepted under another contract")
        result = webhook._public_delivery(directory, record)
        gate._require(result["state"] != "terminal", "terminal original remains held")
        if result["state"] == "accepted":
            paths = [directory / (record["task_digest"] + ".json"), directory / "inflight" / (record["task_digest"] + ".json")]
            present = [path for path in paths if gate._present(path)]
            gate._require(len(present) == 1, "accepted receipt lacks unique durable queue")
            envelope = webhook._load_spooled_task(present[0])
            gate._require(envelope is not None and webhook._delivery_receipt(directory, envelope) is not None,
                          "accepted queue has no original receipt chain")
            webhook._confirm_spool_file(present[0])
        return result

    def _completed(self, plan, intent):
        done = set()
        for path in gate._bounded_paths(self.root / "confirmed", MAX_ITEMS * 2):
            delivery = path.name.split(".", 1)[0]
            gate._require(delivery in plan["items"] and path.name in {delivery + ".json", delivery + ".json.pending"},
                          "unknown transfer confirmation")
            if path.suffix != ".json":
                continue
            value = disk.confirm(path)
            item = self._item(plan, delivery)
            expected = {"schema": 1, "operation_id": intent["request"]["operation_id"], "delivery_id": delivery,
                        "sender_manifest_sha256": intent["request"]["sender_manifest_sha256"],
                        "sender_stop_sha256": plan["sender_stop_sha256"], "original_sha256": gate._hash(item["original"]),
                        "task_sha256": item["task_sha256"], "accepted_strategy": self.fence.binding["strategy"],
                        "accepted_revision": self.fence.binding["revision"]}
            gate._require(set(value) == set(expected) | {"primary_delivery_id"}
                          and all(type(value[k]) is type(v) and value[k] == v for k, v in expected.items())
                          and isinstance(value["primary_delivery_id"], str) and gate.HEX64.fullmatch(value["primary_delivery_id"]),
                          "transfer confirmation identity differs")
            done.add(delivery)
        return done

    def _transfer_one(self, intent, plan, delivery):
        item = self._item(plan, delivery)
        request, webhook = intent["request"], _webhook()
        raw = originals.read_original(Path(request["sender_bundle"]), item["original"])
        _signature(raw, item["original"]["signature"])
        # No normalization here: recovery uses the exact task sealed BEFORE
        # the first transfer, even after a queue-write or response was lost.
        with webhook.locked_transfer_spool() as directory:
            receipt = self._receipt(item, directory)
            if receipt is None:
                webhook.persist_delivery_locked(directory, item["task"], raw)
                receipt = self._receipt(item, directory)
            gate._require(receipt is not None, "durable transfer receipt absent")
            proof = {"schema": 1, "operation_id": request["operation_id"], "delivery_id": delivery,
                     "sender_manifest_sha256": request["sender_manifest_sha256"],
                     "sender_stop_sha256": plan["sender_stop_sha256"], "original_sha256": gate._hash(item["original"]),
                     "task_sha256": item["task_sha256"], "accepted_strategy": receipt["accepted_strategy"],
                     "accepted_revision": receipt["accepted_revision"], "primary_delivery_id": receipt["primary_delivery_id"]}
            disk.once(self.root / "confirmed" / (delivery + ".json"), proof)
        webhook._webhook_dispatcher_wake.set()

    def query(self, operation_id):
        with self._locked():
            intent = self._intent(operation_id)
            plan = self._plan(intent)
            complete = self.root / "complete.json"
            if gate._present(complete):
                value = disk.confirm(complete)
                done = self._completed(plan, intent)
                gate._require(len(done) == len(plan["items"])
                              and value == self._result(intent, plan, len(done)), "transfer completion differs")
                return value
            _producer_boundary(self.fence, intent["request"])
            disk.directory(self.root / "confirmed", create=True)
            # A lost completion journal ACK must not turn already durable
            # originals into a new transfer. This branch may only re-confirm
            # existing receipts/queues and sync the same completion evidence.
            if len(self._completed(plan, intent)) == len(plan["items"]):
                return self._confirm_existing(intent, plan)
            _window(intent, plan)
            batches = disk.directory(self.root / "batches", create=True)
            paths = gate._bounded_paths(batches, MAX_BATCHES * 2)
            # Every attempted batch consumes a permanent slot, including a
            # crash or pending journal write. No retry resets this budget.
            number = len({path.name.split(".")[0] for path in paths})
            gate._require(number < intent["request"]["max_batches"], "transfer batch budget exhausted; retain originals")
            disk.once(batches / f"{number:04}.json", {"operation_id": operation_id, "plan_sha256": gate._hash(plan)})
            lease = self.fence.reserve_lease("durable_original_transfer", kind="drain")
            try:
                with lease.activate():
                    result = self._advance(intent, plan)
            except CompletionUnconfirmed:
                lease.finish(True)
                raise
            except BaseException:
                lease.finish(False)
                raise
            else:
                lease.finish(True)
                return result

    def _confirm_existing(self, intent, plan):
        lease = self.fence.reserve_lease("confirm_original_transfer", kind="drain")
        try:
            with lease.activate(), _webhook().locked_transfer_spool() as directory:
                for delivery in sorted(plan["items"]):
                    item = self._item(plan, delivery)
                    receipt = self._receipt(item, directory)
                    proof = disk.confirm(self.root / "confirmed" / (delivery + ".json"))
                    gate._require(receipt is not None and receipt["primary_delivery_id"] == proof["primary_delivery_id"],
                                  "existing transfer confirmation lacks actual receipt chain")
                result = self._result(intent, plan, len(plan["items"]))
                self._save_complete(result)
        except CompletionUnconfirmed:
            lease.finish(True)
            raise
        except BaseException:
            lease.finish(False)
            raise
        else:
            lease.finish(True)
            return result

    def _save_complete(self, result):
        try:
            disk.once(self.root / "complete.json", result)
        except Exception as error:
            raise CompletionUnconfirmed("completion evidence unconfirmed; query original operation") from error

    def _advance(self, intent, plan):
        done = self._completed(plan, intent)
        pending = sorted(set(plan["items"]) - done)
        error = None
        for delivery in pending[:BATCH_SIZE]:
            try:
                _window(intent, plan)
                self._transfer_one(intent, plan, delivery)
                done.add(delivery)
            except Exception:
                # Retain original, queue, receipt, pending and intent. A
                # bounded query of this same operation may reconcile them.
                error = "original_transfer_unconfirmed"
                break
        result = self._result(intent, plan, len(done), error)
        if result["state"] == "complete":
            self._save_complete(result)
        return result

    @staticmethod
    def _result(intent, plan, count, error=None):
        request = intent["request"]
        return {"schema": 1, "operation_id": request["operation_id"], "plan_sha256": gate._hash(plan),
                  "sender_manifest_sha256": request["sender_manifest_sha256"], "confirmed": count,
                  "total": len(plan["items"]), "state": "complete" if count == len(plan["items"]) else "pending",
                  "error": error, "durable": count == len(plan["items"]), "queue_checked": False,
                  "switch_authorized": False, "legacy_originals_proven": False}

    def admit_duplicate(self, raw, endpoint, signature):
        """Return the sealed task only after a real confirmed transfer exists."""
        with self._locked():
            intent = disk.confirm(self.root / "intent.json")
            request = intent["request"]
            gate._require(intent["request_sha256"] == gate._hash(request), "transfer intent differs")
            _request(request, self.fence)
            _producer_boundary(self.fence, request)
            plan = disk.confirm(self.root / "plan.json")
            complete = disk.confirm(self.root / "complete.json")
            gate._require(plan["intent_sha256"] == gate._hash(intent)
                          and complete == self._result(intent, plan, len(plan["items"])), "original transfer incomplete")
            _window(intent, plan)
            delivery = hashlib.sha256(raw).hexdigest()
            gate._require(len(raw) <= request["body_limit"] and delivery in plan["items"]
                          and endpoint == request["endpoint"], "new or wrong-target intake forbidden")
            item = self._item(plan, delivery)
            gate._require(signature == item["original"]["signature"] and len(raw) == item["original"]["body_size"],
                          "original signature or bytes differ")
            _signature(raw, signature)
            attempts = disk.directory(self.root / "replays", create=True)
            count = len({p.name.split(".")[0] for p in gate._bounded_paths(attempts, MAX_REPLAYS * 2)})
            gate._require(count < request["max_replays"], "duplicate delivery budget exhausted")
            disk.once(attempts / f"{count:04}.json", {"delivery_id": delivery, "plan_sha256": gate._hash(plan)})
            return item["task"]


def admitted_webhook_task(raw, endpoint, signature):
    fence = gate._configured()
    gate._require(fence is not None, "drain transfer requires enforced admission")
    return DrainTransfer(fence).admit_duplicate(raw, endpoint, signature)


def confirm_admitted_webhook(raw):
    """Recheck under the HTTP drain lease and actual spool lock before handler."""
    fence = gate._configured()
    gate._require(fence is not None and gate.current_kind() == "drain", "active drain lease required")
    transfer = DrainTransfer(fence)
    plan = disk.confirm(transfer.root / "plan.json")
    item = transfer._item(plan, hashlib.sha256(raw).hexdigest())
    with _webhook().locked_transfer_spool() as directory:
        gate._require(transfer._receipt(item, directory) is not None, "transferred original receipt missing")
