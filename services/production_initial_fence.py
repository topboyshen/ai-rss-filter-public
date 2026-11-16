"""Confirm a pristine initial CLOSED fence, without ever opening admission.

This proves only this new fence's state. It does not prove that historical
producers were fenced, that old originals exist, or that a release may start.
"""
from __future__ import annotations

from pathlib import Path

from services import admission_fence as gate, controlled_startup as isolation


def _paths(root):
    directory = Path(root) / "startup"
    gate._directory(directory)
    return directory / "initial_closed.intent.json", directory / "initial_closed.json"


def _pristine(fence):
    state = gate._state(gate._read(fence.root / "state.json"))
    intents, pending = fence._operation_inventory()
    leases, finishes = fence._leases()
    gate._require(state == {"schema": 1, "generation": 0, "phase": "frozen", "pending_operation": "", "opening_clock": None}
                  and not intents and not pending and not leases and not finishes,
                  "initial confirmation requires never-opened pristine closed admission")
    return state


def confirm(root, binding, *, operation_id):
    """One confirmation intent; failure only permits exact-operation query."""
    fence = gate.Fence(root, binding)
    gate._require(isinstance(operation_id, str) and gate.HEX64.fullmatch(operation_id), "invalid initial operation")
    intent, final = _paths(root)
    with gate._locked(fence.root / "locks/controller.lock", True, 1):
        with gate._locked(fence.root / "locks/activity.lock", True, 1):
            with fence._metadata():
                state = _pristine(fence)
                manifest = fence._manifest()
                gate._require(not any(gate._present(path) or gate._present(path.with_name(path.name + ".pending"))
                                      for path in (intent, final)), "initial confirmation already attempted; query only")
                receipt = {"schema": 1, "kind": "initial_closed", "operation_id": operation_id,
                    "manifest_sha256": gate._hash(manifest), "binding": binding, "generation": 0,
                    "phase": "frozen", "state_sha256": gate._hash(state), "durable": True,
                    "historical_producers_verified": False, "business_open": False}
                gate._write(intent, receipt)
                gate._write(final, receipt)
                return receipt


def read_receipt(root, record):
    """Read-only identity validation, suitable for the actual runtime image."""
    intent, final = _paths(root)
    gate._require(all(gate._present(path) and not gate._present(path.with_name(path.name + ".pending"))
                      for path in (intent, final)), "initial receipt is unconfirmed; exact query required")
    receipt = gate._read(final)
    original = gate._read(intent)
    manifest = gate._read(Path(root) / "manifest.json")
    gate._require(receipt == original and receipt["schema"] == 1 and receipt["kind"] == "initial_closed"
                  and receipt["operation_id"] == record["operation_id"]
                  and receipt["binding"] == record["binding"] == manifest["binding"]
                  and receipt["manifest_sha256"] == record["manifest_sha256"] == gate._hash(manifest)
                  and receipt["generation"] == record["generation"] == 0 and receipt["phase"] == "frozen"
                  and receipt["durable"] is True and receipt["business_open"] is False
                  and receipt["historical_producers_verified"] is False
                  and gate._hash(receipt) == record["receipt_sha256"], "initial closed receipt differs")
    return receipt


def query(root, binding, *, operation_id):
    """Confirm only the original durable intent while the fence is pristine."""
    fence = gate.Fence(root, binding)
    intent, final = _paths(root)
    with gate._locked(fence.root / "locks/controller.lock", True, 1):
        with gate._locked(fence.root / "locks/activity.lock", True, 1):
            with fence._metadata():
                state = _pristine(fence)
                original = isolation._read_complete(intent)
                gate._require(original["operation_id"] == operation_id and original["binding"] == binding
                              and original["state_sha256"] == gate._hash(state)
                              and original["manifest_sha256"] == gate._hash(fence._manifest()),
                              "initial closed operation changed")
                gate._write(intent, original)
                if gate._present(final):
                    gate._require(isolation._read_complete(final) == original, "initial receipt conflicts")
                gate._write(final, original)
                return read_receipt(root, {**original, "receipt_sha256": gate._hash(original)})
