"""Initial migration candidate binding uses real validators, synthetic inspect."""
from __future__ import annotations

from copy import deepcopy
import json

import pytest

from scripts import private_release_contract as private, production_activation_fixture as fixture
from scripts.private_control_subpaths import capture
from services import admission_fence as gate, production_migration as migration, production_startup_control as startup


@pytest.fixture
def candidate(tmp_path, monkeypatch):
    tmp_path = tmp_path.resolve()
    paths = {name: tmp_path / name for name in ("data", "fence", "spool", "journal", "grants", "slots")}
    for name, relative in {"fence": "admission_fence", "slots": "startup_slots",
                           "spool": "webhook_spool", "journal": "admission_journal"}.items():
        paths[name] = paths["data"] / relative
    for path in paths.values():
        path.mkdir(mode=0o700)
    binding = {"revision": "a" * 40, "image_id": "sha256:" + "b" * 64, "strategy": "synthetic",
               "source_sha256": "c" * 64, "deployment_nonce": "d" * 32}
    manifest = fixture.manifest_input(paths, "synthetic", "e" * 64, 2, binding, "internal")
    service = manifest["services"]["ai-filter-api"]
    for mount in service["volumes"]:
        if mount["source"] == private.RUNTIME:
            mount["source"] = str(tmp_path / "runtime.yaml")
    image = {"Id": "sha256:" + "f" * 64, "Config": {"Env": ["PATH=/usr/bin"],
        "WorkingDir": "/app", "Cmd": ["python3"], "Entrypoint": None}}
    values = {"AI_FILTER_API_KEY": "synthetic-only", "MINIFLUX_WEBHOOK_SECRET": "synthetic-only"}
    environment = tmp_path / "environment"
    environment.write_text("AI_FILTER_API_KEY=synthetic-only\nMINIFLUX_WEBHOOK_SECRET=synthetic-only\n")
    environment.chmod(0o400)
    row = {"Id": "1" * 64, "Image": image["Id"], "RestartCount": 0,
        "State": {"Running": True, "Status": "running"},
        "Config": {"Labels": {"com.docker.compose.service": "ai-filter-api",
            "com.docker.compose.project": "synthetic", "com.docker.compose.project.working_dir": str(tmp_path / "project")},
            "User": service["user"], "WorkingDir": "/app", "Cmd": service["command"], "Entrypoint": None,
            "Healthcheck": {"Test": ["NONE"]},
            "Env": [key + "=" + value for key, value in private.role_environment(service, image["Config"], values).items()]},
        "HostConfig": {"Memory": 805306368, "MemorySwap": 805306368, "MemoryReservation": 0,
            "PidsLimit": 256, "NanoCpus": 1000000000, "Privileged": False, "CapAdd": None, "CapDrop": ["ALL"],
            "SecurityOpt": ["no-new-privileges:true"], "GroupAdd": [], "PortBindings": {}},
        "Mounts": [{"Type": mount["type"], "Source": mount["source"], "Destination": mount["target"],
                    "RW": not mount.get("read_only", False)} for mount in service["volumes"]],
        "NetworkSettings": {"Networks": {"internal": {}}}}
    permit = {"source": {"image_id": image["Id"]}, "config": {"private_contract_path": str(tmp_path / "contract.json")},
        "parent_operation_id": "e" * 64, "freeze": {"root": str(paths["fence"]), "binding": binding}}
    control = {kind: capture(paths[name], target, paths["data"]) for kind, name, target in (
        ("state", "slots", "/app/data/startup_slots"), ("freeze", "fence", "/app/data/admission_fence"))}
    contract = {"review": {"project": "synthetic", "project_directory": str(tmp_path / "project"),
        "control_subpaths": {name: deepcopy(control) for name in manifest["services"]}}}
    commands = []
    def inspect(command):
        commands.append(command)
        assert command == ["docker", "image", "inspect", image["Id"]]
        return json.dumps([image]).encode()
    monkeypatch.setattr(private, "run", inspect)
    return {"permit": permit, "contract": contract, "manifest": manifest, "row": row, "commands": commands, "paths": paths}


def check(p):
    return migration._initial_candidates(p["permit"], p["contract"], p["manifest"], [p["row"]])


def test_exact_candidate_is_accepted_without_mutating_inspect_or_manifest(candidate):
    p = candidate
    before = deepcopy({key: value for key, value in p.items() if key != "commands"})
    assert check(p) is None
    assert {mount["Destination"] for mount in p["row"]["Mounts"]} == {
        "/app/data", "/app/config.yaml", "/app/data/startup_contract"}
    assert before == {key: value for key, value in p.items() if key != "commands"}
    assert len(p["commands"]) == 1


@pytest.mark.parametrize("destination,field,value", [
    ("/app/data", "Source", "/unreviewed/database-and-control-parent"),
    ("/app/config.yaml", "Source", "/unreviewed/runtime.yaml"),
    ("/app/config.yaml", "RW", True),
    ("/app/data/startup_contract", "Source", "/unreviewed/grants"),
    ("/app/data/startup_contract", "RW", True),
    ("/app/data", "RW", False),
])
def test_single_actual_mount_drift_is_rejected(candidate, destination, field, value):
    next(mount for mount in candidate["row"]["Mounts"] if mount["Destination"] == destination)[field] = value
    with pytest.raises(gate.AdmissionError):
        check(candidate)


def test_parent_data_bind_requires_explicit_reviewed_control_subpaths(candidate):
    del candidate["contract"]["review"]["control_subpaths"]
    with pytest.raises(gate.AdmissionError, match="startup mount differs"):
        check(candidate)


def test_reviewed_control_subpath_inode_change_is_rejected(candidate):
    fence = candidate["paths"]["fence"]
    fence.rename(fence.with_name("retained-original-fence"))
    fence.mkdir(mode=0o700)
    with pytest.raises(gate.AdmissionError, match="reviewed control subpath differs"):
        check(candidate)


@pytest.mark.parametrize("kind", ["state", "freeze"])
@pytest.mark.parametrize("drift", [None, "Source", "RW", "Type", "duplicate", "missing"])
def test_direct_control_bind_still_requires_exact_single_writable_source(tmp_path, kind, drift):
    source, target = tmp_path / kind, "/app/data/" + kind
    mount = {"Type": "bind", "Source": str(source), "Destination": target, "RW": True}
    rows = [mount]
    if drift == "duplicate":
        rows.append(deepcopy(mount))
    elif drift == "missing":
        rows = []
    elif drift:
        mount[drift] = {"Source": str(tmp_path / "unreviewed"), "RW": False, "Type": "volume"}[drift]
    if drift is None:
        assert startup._control_mount({"Mounts": rows}, None, kind, source, target) is None
    else:
        with pytest.raises(gate.AdmissionError, match="startup mount differs"):
            startup._control_mount({"Mounts": rows}, None, kind, source, target)


@pytest.mark.parametrize("drift", ["network", "port", "image", "restart", "operation", "fence_binding"])
def test_actual_identity_and_entrance_drift_is_rejected(candidate, drift):
    p, row = candidate, candidate["row"]
    if drift == "network":
        row["NetworkSettings"]["Networks"] = {"external": {}}
    elif drift == "port":
        row["HostConfig"]["PortBindings"] = {"5000/tcp": [{"HostIp": "0.0.0.0", "HostPort": "5000"}]}
    elif drift == "image":
        row["Image"] = "sha256:" + "0" * 64
    elif drift == "restart":
        row["RestartCount"] = 1
    else:
        key = "AI_FILTER_PRODUCTION_STARTUP_OPERATION" if drift == "operation" else "AI_FILTER_ADMISSION_BINDING"
        replacement = "0" * 64 if drift == "operation" else json.dumps({**p["permit"]["freeze"]["binding"], "revision": "0" * 40})
        row["Config"]["Env"] = [key + "=" + replacement if item.startswith(key + "=") else item for item in row["Config"]["Env"]]
    with pytest.raises((gate.AdmissionError, private.ContractError)):
        check(p)
