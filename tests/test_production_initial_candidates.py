"""Initial migration candidate binding uses real validators, synthetic inspect."""
from __future__ import annotations

from copy import deepcopy
import json

import pytest

from scripts import private_release_contract as private, production_activation_fixture as fixture
from services import admission_fence as gate, production_migration as migration


@pytest.fixture
def candidate(tmp_path, monkeypatch):
    paths = {name: tmp_path / name for name in ("data", "fence", "spool", "journal", "grants", "slots")}
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
    contract = {"review": {"project": "synthetic", "project_directory": str(tmp_path / "project")}}
    commands = []
    def inspect(command):
        commands.append(command)
        assert command == ["docker", "image", "inspect", image["Id"]]
        return json.dumps([image]).encode()
    monkeypatch.setattr(private, "run", inspect)
    return {"permit": permit, "contract": contract, "manifest": manifest, "row": row, "commands": commands}


def check(p):
    return migration._initial_candidates(p["permit"], p["contract"], p["manifest"], [p["row"]])


def test_exact_candidate_is_accepted_without_mutating_inspect_or_manifest(candidate):
    p = candidate
    before = deepcopy({key: value for key, value in p.items() if key != "commands"})
    assert check(p) is None
    assert before == {key: value for key, value in p.items() if key != "commands"}
    assert len(p["commands"]) == 1


@pytest.mark.parametrize("destination,field,value", [
    ("/app/data/business", "Source", "/unreviewed/database"),
    ("/app/config.yaml", "Source", "/unreviewed/runtime.yaml"),
    ("/app/config.yaml", "RW", True),
    ("/app/data/startup_contract", "Source", "/unreviewed/grants"),
    ("/app/data/startup_contract", "RW", True),
    ("/app/data/admission_fence", "Source", "/unreviewed/fence"),
])
def test_single_actual_mount_drift_is_rejected(candidate, destination, field, value):
    next(mount for mount in candidate["row"]["Mounts"] if mount["Destination"] == destination)[field] = value
    with pytest.raises(gate.AdmissionError):
        check(candidate)


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
