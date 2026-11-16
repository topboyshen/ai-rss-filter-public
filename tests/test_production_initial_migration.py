"""Pristine CLOSED authority with real synthetic SQLite and file locks."""
from copy import deepcopy
import time

import pytest

from test_production_migration import prepared, set_compatibility  # noqa: F401
from test_production_container_lifecycle import container
from scripts import private_release_contract as private
from services import admission_fence as gate, production_initial_fence as initial, production_migration as migration


@pytest.fixture
def initial_case(prepared, monkeypatch):  # noqa: F811
    p = prepared
    permit = deepcopy(p["permit"])
    root = p["directory"] / "first-fence"
    binding = permit["freeze"]["binding"]
    now = int(time.time())
    gate.provision(root, binding, {"not_before": now - 1, "expires_at": now + 600,
                                   "max_admissions": 10, "max_bytes": 10000})
    result = initial.confirm(root, binding, operation_id="6" * 64)
    permit["freeze"] = {"root": str(root), "binding": binding, "kind": "initial_closed", "generation": 0,
        "operation_id": "6" * 64, "manifest_sha256": result["manifest_sha256"], "receipt_sha256": gate._hash(result)}
    for name in ("controller", "activity"):
        permit["locks"][name] = migration.file_identity(root / "locks" / (name + ".lock"))
    set_compatibility(permit)
    rows = [container("1" * 64, "previous-api", "web", binding["image_id"]),
            container("2" * 64, "previous-worker", "worker", binding["image_id"])]
    contract = {"app_services": ["api", "worker"], "review": {
        "existing_topology": private.runtime_topology(rows),
        "service_mapping": {"api": ["previous-api"], "worker": ["previous-worker"]}}}
    monkeypatch.setattr(private, "load", lambda *a, **kw: (contract, {"services": {}}, "e" * 64))
    monkeypatch.setattr(private, "inspect_project", lambda *a: deepcopy(rows))
    return {**p, "permit": permit, "fence_root": root, "rows": rows}


def test_pristine_authority_permits_real_closed_migration(initial_case):
    p = initial_case
    before = (p["fence_root"] / "startup/initial_closed.json").read_bytes()
    with migration.locked_session(p["root"], p["permit"]) as session:
        session.authorize()
        result = session.migrate()
        assert result["status"] == "migration_completed" and not result["business_open"]
    assert (p["fence_root"] / "startup/initial_closed.json").read_bytes() == before
    assert gate._read(p["fence_root"] / "state.json")["generation"] == 0


@pytest.mark.parametrize("field,value", [("Running", True), ("Paused", True), ("Restarting", True), ("Dead", True)])
def test_initial_gate_does_not_claim_previous_producers_stopped(initial_case, field, value):
    p = initial_case
    p["rows"][0]["State"][field] = value
    with pytest.raises(gate.AdmissionError, match="not safely stopped"):
        with migration.locked_session(p["root"], p["permit"]):
            pytest.fail("unsafe prior application accepted")
    assert not (p["root"] / "permit.json").exists()


def test_final_visible_sync_failure_cannot_authorize_migration(initial_case, monkeypatch):
    p = initial_case
    path = p["fence_root"] / "startup"
    original = {item.name: item.read_bytes() for item in path.iterdir()}
    before = migration.database_fingerprint(p["anchor"])
    sync = gate._sync_directory
    def fail(directory):
        if directory == path:
            raise OSError("synthetic final directory sync failure")
        return sync(directory)
    with monkeypatch.context() as patch:
        patch.setattr(gate, "_sync_directory", fail)
        with pytest.raises(OSError, match="directory sync"):
            with migration.locked_session(p["root"], p["permit"]):
                pytest.fail("unconfirmed initial authority accepted")
    assert migration.database_fingerprint(p["anchor"]) == before
    assert not (p["root"] / "permit.json").exists()
    assert {item.name: item.read_bytes() for item in path.iterdir()} == original
    # Same operation, no replacement receipt or opening: explicit host confirm.
    with migration.locked_session(p["root"], p["permit"]) as session:
        session.authorize()
    assert {item.name: item.read_bytes() for item in path.iterdir()} == original
