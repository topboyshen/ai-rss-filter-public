"""Synthetic private originals only; format checks are not live PG provenance."""

import copy
import hashlib
import time

import pytest

from services import sender_drain_contract as contract


def synthetic_manifest(root):
    body = b'{"event_type":"new_entries","entries":[{"id":1}]}'
    sha = hashlib.sha256(body).hexdigest()
    component = {
        "id": "1" * 64,
        "image": "sha256:" + "2" * 64,
        "revision": "3" * 40,
        "started_at": "2026-10-27T00:00:00Z",
        "restart_count": 0,
        "role": "miniflux",
        "user": "0",
        "memory": 768 * 1024**2,
        "pids": 256,
        "mounts": [
            {"Type": "bind", "Source": str(root), "Destination": "/state", "RW": True}
        ],
    }
    binding = {
        "schema": 1,
        "scope": "instrumented_sender_only",
        "run_id": "4" * 32,
        "control_id": "miniflux",
        "component_name": "sender",
        "component": component,
        "manifest": "5" * 64,
        "application_source_sha256": "6" * 64,
        "source_sha256": "7" * 64,
        "generation": 2,
        "initial_operation_id": "8" * 64,
        "entrances": ["miniflux_scheduler", "miniflux_manual", "legacy_async_sender"],
    }
    identity = {
        key: (
            134
            if key == "schema_version"
            else 100
            if key.endswith("_oid")
            else "synthetic"
        )
        for key in contract.IDENTITY_KEYS
    }
    receipt = {
        "schema": 1,
        "manifest": binding["manifest"],
        "operation_id": "9" * 64,
        "action": "stop_new",
        "previous_generation": 2,
        "generation": 3,
        "state": "draining",
        "durable": True,
        "confirmed": True,
        "active_leases": 2,
        "uncertain_leases": 0,
        "active_producer_leases": 0,
        "uncertain_producer_leases": 0,
    }
    summary = {
        "coverage": "outbox_and_legacy_v1",
        "rows": 1,
        "charged_bytes": 2048,
        "outbox_rows": 1,
        "outbox_charged_bytes": 2048,
        "states": {"pending": 1, "inflight": 0, "held": 0, "acked": 0},
        "max_attempts": 0,
        "expired_unacknowledged": 0,
        "acked_at_least_7_days": 0,
        "cleanup_credit": 0,
        "legacy": {
            "rows": 0,
            "charged_bytes": 0,
            "states": {"held": 0},
            "older_than_7_days": 0,
            "cleanup_credit": 0,
            "forwarding_allowed": False,
        },
        "database_identity": identity,
        "read_only_transaction": True,
    }
    row = {
        "delivery_id": sha,
        "body_sha256": sha,
        "body_file": sha + ".body",
        "body_size": len(body),
        "target_url": "http://receiver.invalid/webhook",
        "signature": "a" * 64,
        "contract": "synthetic-v1",
        "user_id": 1,
        "feed_id": 1,
        "entry_id": 1,
        "state": "pending",
        "attempts": 0,
    }
    now = int(time.time())
    value = {
        "schema": 1,
        "kind": "miniflux-durable-drain-v1",
        "sender_binding": binding,
        "postgres_identity": identity,
        "control_receipt": receipt,
        "window": {"not_before": now - 1, "expires_at": now + 300},
        "source_sha256": contract.source_sha256(),
        "inventory_summary": summary,
        "retained_lease_budget": {"records": 2, "remaining": 9998, "limit": 10000},
        "entries": [row],
        "legacy_original_coverage": "registered_paths_since_instrumentation_only",
        "queue_checked": False,
        "switch_authorized": False,
    }
    return value, body


def save_bundle(root, value, body):
    root.mkdir(mode=0o700)
    for name, raw in (
        ("manifest.json", contract.encoded(value)),
        (value["entries"][0]["body_file"], body),
    ):
        path = root / name
        path.write_bytes(raw)
        path.chmod(0o600)


def test_exact_original_roundtrip_and_consumers_are_not_queue_empty(tmp_path):
    value, body = synthetic_manifest(tmp_path)
    root = tmp_path / "bundle"
    save_bundle(root, value, body)
    loaded, sha = contract.load_bundle(root)
    assert (
        loaded == value and sha == hashlib.sha256(contract.encoded(value)).hexdigest()
    )
    assert contract.read_original(root, value["entries"][0]) == body
    assert loaded["control_receipt"]["active_leases"] == 2
    assert loaded["queue_checked"] is loaded["switch_authorized"] is False
    assert loaded["inventory_summary"]["cleanup_credit"] == 0


@pytest.mark.parametrize("credit", [1, -1, False, 0.0, "0", None])
def test_outbox_cleanup_credit_requires_exact_integer_zero(tmp_path, credit):
    value, _ = synthetic_manifest(tmp_path)
    value["inventory_summary"]["cleanup_credit"] = credit
    with pytest.raises(contract.DrainContractError, match="cannot grant cleanup credit"):
        contract.validate_manifest(value)


def test_outbox_cleanup_credit_cannot_be_omitted(tmp_path):
    value, _ = synthetic_manifest(tmp_path)
    del value["inventory_summary"]["cleanup_credit"]
    with pytest.raises(contract.DrainContractError, match="cannot grant cleanup credit"):
        contract.validate_manifest(value)


@pytest.mark.parametrize(
    "change",
    [
        lambda v: v["control_receipt"].update(active_producer_leases=1),
        lambda v: v["control_receipt"].update(active_producer_leases=False),
        lambda v: v["control_receipt"].update(state="closed"),
        lambda v: v["control_receipt"].update(generation=4),
        lambda v: v["control_receipt"].update(manifest="b" * 64),
        lambda v: v["postgres_identity"].update(schema_version=133),
        lambda v: v["entries"][0].update(state="held"),
        lambda v: v["entries"][0].update(attempts=20),
        lambda v: v["entries"][0].update(body_file="../outside"),
        lambda v: v["entries"].append(copy.deepcopy(v["entries"][0])),
        lambda v: v.update(legacy_original_coverage=True),
        lambda v: v.update(switch_authorized=True),
        lambda v: v["retained_lease_budget"].update(records=9999, remaining=1),
        lambda v: v["window"].update(not_before=1, expires_at=100),
        lambda v: v["inventory_summary"]["legacy"].update(rows=1, states={"held": 1}),
    ],
)
def test_unproven_changed_or_exhausted_manifest_rejected(tmp_path, change):
    value, _ = synthetic_manifest(tmp_path)
    change(value)
    with pytest.raises((ValueError, KeyError)):
        contract.validate_manifest(value)


def test_changed_missing_pending_and_symlink_originals_fail(tmp_path):
    value, body = synthetic_manifest(tmp_path)
    root = tmp_path / "bundle"
    save_bundle(root, value, body)
    path = root / value["entries"][0]["body_file"]
    path.write_bytes(body + b" ")
    with pytest.raises(ValueError):
        contract.load_bundle(root)
    path.write_bytes(body)
    (root / "incomplete.pending").write_bytes(b"partial")
    with pytest.raises(ValueError):
        contract.load_bundle(root)
    (root / "incomplete.pending").unlink()
    path.unlink()
    path.symlink_to(tmp_path / "outside")
    with pytest.raises(OSError):
        contract.load_bundle(root)


def test_duplicate_json_keys_and_unsafe_modes_fail(tmp_path):
    value, body = synthetic_manifest(tmp_path)
    root = tmp_path / "bundle"
    save_bundle(root, value, body)
    path = root / "manifest.json"
    path.write_bytes(b'{"schema":1,"schema":1}')
    with pytest.raises(ValueError):
        contract.load_bundle(root)
    path.write_bytes(contract.encoded(value))
    path.chmod(0o644)
    with pytest.raises(ValueError):
        contract.load_bundle(root)
    assert not list(root.glob("*.pending"))
