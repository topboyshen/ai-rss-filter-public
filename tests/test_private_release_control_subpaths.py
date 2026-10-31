"""Legacy single-data-bind adaptation; real private files, synthetic inspect."""
from copy import deepcopy
import json
import os
from pathlib import Path
import subprocess
import sys
from types import SimpleNamespace

import pytest

from scripts import private_control_subpaths as subpaths, private_release_contract as private
from services import admission_fence as gate, production_migration as migration, production_startup_control as startup
from test_private_release_contract import fixture, fake_docker, inspect_records, reseal_inputs, seal  # noqa: F401


@pytest.fixture
def legacy(fixture):  # noqa: F811
    p = fixture
    manifest, review = p["manifest_data"], p["review_data"]
    data = Path(manifest["services"]["ai-filter-api"]["volumes"][0]["source"])
    controls = data / "control"
    controls.mkdir(mode=0o700)
    for kind in ("state", "freeze"):
        (controls / kind).mkdir(mode=0o700)
    grants = data.parent / "grants"
    grants.mkdir(mode=0o700)
    operation, binding = "a" * 64, {"synthetic": True}
    manifest["services"]["ai-filter-worker"] = deepcopy(manifest["services"]["ai-filter-api"])
    mapping = {kind: subpaths.capture(controls / kind, "/app/data/control/" + kind, data) for kind in ("state", "freeze")}
    for name, role in (("ai-filter-api", "web"), ("ai-filter-worker", "worker")):
        service = manifest["services"][name]
        service["environment"].update({"AI_FILTER_ROLE": role, "AI_FILTER_ADMISSION_MODE": "enforced",
            "AI_FILTER_ADMISSION_ROOT": mapping["freeze"]["target"], "AI_FILTER_ADMISSION_BINDING": json.dumps(binding),
            "AI_FILTER_DB_PATH": "/app/data/fixture.db", "AI_FILTER_PRODUCTION_STARTUP_OPERATION": operation,
            "AI_FILTER_PRODUCTION_STARTUP_ROOT": "/app/data/startup_contract"})
        service["volumes"].append({"type": "bind", "source": str(grants), "target": "/app/data/startup_contract",
                                   "read_only": True, "bind": {"create_host_path": False}})
    review["service_mapping"]["ai-filter-worker"] = ["ai-filter-worker"]
    review["control_subpaths"] = {name: deepcopy(mapping) for name in ("ai-filter-api", "ai-filter-worker")}
    review["topology"] = {name: {"mounts": s.get("volumes", []), "networks": s.get("networks", []), "ports": s.get("ports", [])}
                          for name, s in manifest["services"].items()}
    # Old applications really have only the retained data RW bind. No control
    # submounts, grants, control environment or new topology is fabricated.
    old = inspect_records(p, p["output"])
    for row in old:
        row["Mounts"] = [m for m in row["Mounts"] if m["Destination"] != "/app/data/startup_contract"]
        row["Config"]["Env"] = []
    review["existing_topology"] = private.runtime_topology(old)
    reseal_inputs(p)
    return {**p, "data": data, "controls": controls, "grants": grants, "mapping": mapping,
            "operation": operation, "binding": binding, "old": old}


def test_single_legacy_bind_seals_preflights_and_verifies_actual_startup(legacy, monkeypatch):
    p = legacy
    path = seal(p)
    contract, manifest, _ = private.load(path, p["root"], require_fresh=True)
    source = private.source_identity(p["root"])
    monkeypatch.setattr(private, "source_identity", lambda _root: source)
    calls = fake_docker(monkeypatch, contract, p["old"])
    private.preflight(contract, manifest)
    assert all(call[0][:2] != ["docker", "create"] for call in calls)
    candidate = inspect_records(p, path.parent)
    for row in candidate:
        row["Config"]["Hostname"] = row["Id"][:12]
        row["RestartCount"] = 0
    fake_docker(monkeypatch, contract, candidate)
    monkeypatch.setattr(startup, "_container", lambda identifier: next(r for r in candidate if r["Id"] == identifier))
    original_load = private.load
    monkeypatch.setattr(private, "load", lambda *a, **kw: original_load(path, p["root"], **kw))
    session = SimpleNamespace(path=p["data"] / "fixture.db", permit={
        "config": {"path": str(path.parent / "runtime.yaml"), "private_contract_path": str(path)},
        "freeze": {"root": p["mapping"]["freeze"]["source"], "binding": p["binding"]},
        "source": {"image_id": candidate[0]["Image"]}, "parent_operation_id": p["operation"]})
    descriptor = {"source": {"image_id": candidate[0]["Image"]}, "operation_id": p["operation"],
        "state_path": p["mapping"]["state"]["target"], "database": {"path": "/app/data/fixture.db"},
        "config": {"path": "/app/config.yaml"},
        "freeze": {"root": p["mapping"]["freeze"]["target"], "binding": p["binding"]}}
    for role, index in (("web", 0), ("worker", 2)):
        assert startup.verify_container(session, p["grants"], Path(p["mapping"]["state"]["source"]),
            descriptor, candidate[index]["Id"], role, running=True) == candidate[index]
    migration._initial_candidates(session.permit, contract, manifest, [candidate[0], candidate[2]])
    assert private.expected_topology(manifest)[0] == {
        ("bind", str(p["data"]), "/app/data"), ("volume", "existing-project_redis-data", "/data")}
    assert not list(p["data"].rglob("*receipt*"))


def test_isolated_cli_can_load_the_sibling_helper(legacy):
    path = seal(legacy)
    result = subprocess.run([sys.executable, "-I", private.__file__, "check", "--root", str(legacy["root"]),
                             "--contract", str(path)], capture_output=True, timeout=15)
    assert result.returncode == 0, result.stderr.decode()


@pytest.mark.parametrize("kind", ["state", "freeze"])
@pytest.mark.parametrize("drift", ["inode", "parent_inode", "symlink", "mode", "missing", "uid", "gid", "device"])
def test_sealed_subdirectory_identity_drift_fails_before_docker(legacy, monkeypatch, kind, drift):
    path = seal(legacy)
    value = legacy["mapping"][kind]
    target = Path(value["source"])
    if drift in {"inode", "symlink", "missing"}:
        target.rename(target.with_name(kind + "-retained"))
        if drift == "inode":
            target.mkdir(mode=0o700)
        elif drift == "symlink":
            target.symlink_to(target.with_name(kind + "-retained"), target_is_directory=True)
    elif drift == "parent_inode":
        parent = target.parent
        parent.rename(parent.with_name("control-retained"))
        parent.mkdir(mode=0o700)
        for name in ("state", "freeze"):
            (parent / name).mkdir(mode=0o700)
    elif drift == "mode":
        target.chmod(0o755)
    else:
        original = os.fstat
        identity = target.stat().st_ino
        def changed(fd):
            info = original(fd)
            if info.st_ino != identity:
                return info
            values = {name: getattr(info, name) for name in ("st_dev", "st_ino", "st_mode", "st_uid", "st_gid")}
            values[{"uid": "st_uid", "gid": "st_gid", "device": "st_dev"}[drift]] += 1
            return SimpleNamespace(**values)
        monkeypatch.setattr(os, "fstat", changed)
    monkeypatch.setattr(private, "run", lambda *a, **kw: pytest.fail("invalid paths must fail before source/Docker commands"))
    # Source identity precedes path validation in load; preserve only that read.
    monkeypatch.setattr(private, "source_identity", lambda root: {k: legacy["review_data"][k] for k in
        ("source_sha", "source_tree", "example_config_sha256", "source_manifest_sha256")})
    with pytest.raises(private.ContractError, match="control subpath"):
        private.load(path, legacy["root"])


@pytest.mark.parametrize("mutation", ["sibling", "escape", "alias", "old_anchor", "new_anchor", "shadow_ro", "shadow_child", "role", "missing_role", "boolean_identity"])
def test_invalid_review_cannot_be_sealed(legacy, mutation):
    p = legacy
    values = p["review_data"]["control_subpaths"]
    value = values["ai-filter-api"]["state"]
    if mutation == "sibling":
        value["source"] = str(p["data"].parent / "other")
    elif mutation == "escape":
        value["target"] = "/app/data/../outside"
    elif mutation == "alias":
        values["ai-filter-api"]["state"] = deepcopy(values["ai-filter-api"]["freeze"])
    elif mutation == "old_anchor":
        old = p["review_data"]["existing_topology"]["ai-filter-api"][0]["mounts"]
        next(m for m in old if m["target"] == "/app/data")["source"] = str(p["data"].parent)
    elif mutation == "new_anchor":
        p["manifest_data"]["services"]["ai-filter-api"]["volumes"][0]["source"] = str(p["data"].parent)
    elif mutation.startswith("shadow"):
        target = value["target"] if mutation == "shadow_ro" else value["target"] + "/hidden"
        p["manifest_data"]["services"]["ai-filter-api"]["volumes"].append({"type": "bind", "source": str(p["grants"]),
            "target": target, "read_only": True, "bind": {"create_host_path": False}})
    elif mutation == "role":
        p["manifest_data"]["services"]["ai-filter-worker"]["environment"]["AI_FILTER_ROLE"] = "web"
    elif mutation == "boolean_identity":
        value["directories"][-1]["uid"] = False
    else:
        values.pop("ai-filter-worker")
    reseal_inputs(p)
    with pytest.raises(private.ContractError):
        seal(p)
    assert not p["output"].exists()


@pytest.mark.parametrize("change", ["source", "readonly", "shadow", "target", "no_review"])
def test_runtime_subpath_verifier_never_accepts_an_unreviewed_mount(legacy, change):
    p = legacy
    value = p["mapping"]["freeze"]
    rows = {"Mounts": [{"Type": "bind", "Source": str(p["data"]), "Destination": "/app/data", "RW": True}]}
    mapping, target = p["mapping"], value["target"]
    if change == "source":
        rows["Mounts"][0]["Source"] = str(p["data"].parent)
    elif change == "readonly":
        rows["Mounts"][0]["RW"] = False
    elif change == "shadow":
        rows["Mounts"].append({"Type": "bind", "Source": str(p["grants"]), "Destination": target, "RW": True})
    elif change == "target":
        target = "/app/data/wrong"
    else:
        mapping = None
    with pytest.raises(gate.AdmissionError):
        startup._control_mount(rows, mapping, "freeze", value["source"], target)


@pytest.mark.parametrize("protected", ["grants", "config"])
def test_retained_data_bind_cannot_expose_readonly_input_alias(legacy, protected):
    p = legacy
    grants = p["data"] / "grants" if protected == "grants" else p["grants"]
    config = p["data"] / "runtime.yaml" if protected == "config" else p["config"]
    record = {"Mounts": [{"Type": "bind", "Source": str(p["data"]), "Destination": "/app/data", "RW": True}]}
    with pytest.raises(gate.AdmissionError, match="writable alias"):
        startup._protect_readonly_inputs(record, grants, Path(p["mapping"]["state"]["source"]), config)
