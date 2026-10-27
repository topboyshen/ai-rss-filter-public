"""Owned synthetic inputs for first-CLOSED initialization and activation.

The sole feed is served inside the fixture network. Its explicit Synthetic
blacklist exercises real deterministic consumption without an LLM backend.
No historical originals, old binary behavior, or model quality are inferred.
"""
from __future__ import annotations

from copy import deepcopy
import os
import time

from scripts import private_release_contract as private, production_startup_fixture as startup
from scripts.verify_private_release_native import save
from services import admission_fence as gate, webhook_receipts


def initialize_synthetic_empty_receipts(spool):
    """Prepare only this new synthetic spool; never repair existing inventory."""
    webhook_receipts._private_directory(spool)
    with gate._locked(spool / ".lock", True, 1):
        gate._require({path.name for path in spool.iterdir()} == {".lock", "inflight", "dead-letter"},
                      "synthetic empty spool must be pristine")
        for name in ("inflight", "dead-letter"):
            webhook_receipts._private_directory(spool / name)
            gate._require(not list((spool / name).iterdir()), "synthetic spool contains retained work")
        store = webhook_receipts.ReceiptStore(spool)
        gate._require(store._directories(create=True), "synthetic receipt initialization failed")
        for path in (store.tasks, store.directory, spool, spool.parent):
            webhook_receipts._fsync_directory(path)
        gate._require(not list(store.tasks.iterdir())
                      and {path.name for path in store.directory.iterdir()} == {"tasks"},
                      "synthetic preparation created receipt originals")


def prepare_disk(directory, source, network, previous_image_id, *, workers=2):
    from core import config
    from services.production_initial_fence import confirm

    directory.mkdir(mode=0o700)
    paths = {name: directory / name for name in
             ("project", "data", "fence", "spool", "journal", "grants", "slots", "lifecycle")}
    # The previous synthetic app has the actual legacy *shape*: one data bind.
    # New control directories are descendants, never preinstalled extra mounts.
    for name, relative in {"fence": "admission_fence", "slots": "startup_slots",
                           "spool": "webhook_spool", "journal": "admission_journal"}.items():
        paths[name] = paths["data"] / relative
    for name, path in paths.items():
        if name != "fence":
            path.mkdir(mode=0o700)
    api_key = "synthetic-never-a-production-secret-" + gate.secrets.token_hex(16)
    os.environ["AI_FILTER_API_KEY"] = api_key
    cfg = directory / "runtime.yaml"
    save(cfg, b"threshold:\n  pass: 7\nscoring_strategy_version: synthetic-activation\n"
              b"blacklist: [Synthetic]\nllm: []\ngenerate_summary: false\n"
              b"feeds:\n  - url: http://fixture:8080/feed\n    name: Synthetic activation feed\n")
    config.CONFIG_PATH = str(cfg)
    config.config.reload()
    gate._require(isinstance(previous_image_id, str) and previous_image_id.startswith("sha256:")
                  and gate.HEX64.fullmatch(previous_image_id[7:]), "exact inspected previous image required")
    binding = {"revision": source["source_sha"], "image_id": previous_image_id,
               "strategy": config.config.cfg["scoring_strategy_version"],
               "source_sha256": gate.source_sha256(), "deployment_nonce": gate.secrets.token_hex(16)}
    now = int(time.time())
    gate.provision(paths["fence"], binding, {"not_before": now - 1, "expires_at": now + 900,
                                          "max_admissions": 128, "max_bytes": 8 * 1024**2})
    initial_operation = gate.secrets.token_hex(32)
    frozen = confirm(paths["fence"], binding, operation_id=initial_operation)
    anchor, target = startup.seed_databases(paths["data"])
    for path in (paths["spool"] / "inflight", paths["spool"] / "dead-letter"):
        path.mkdir(mode=0o700)
    for path in (paths["spool"] / ".lock", paths["journal"] / ".lock",
                 directory / "release.lock", directory / "refresh.lock"):
        save(path, b"")
    initialize_synthetic_empty_receipts(paths["spool"])
    gate._write(directory / "SYNTHETIC_EMPTY_RECEIPT_STORE.json", {
        "synthetic_only": True, "accepted_originals": 0, "business_open": False,
        "production_missing_metadata_authorized": False})
    operation = gate.secrets.token_hex(32)
    project = "activation-native-" + gate.secrets.token_hex(6)
    manifest = manifest_input(paths, project, operation, workers, binding, network)
    manifest_path = directory / "manifest.json"
    save(manifest_path, manifest)
    environment = directory / "environment"
    save(environment, ("AI_FILTER_API_KEY=" + api_key + "\n"
                       "MINIFLUX_WEBHOOK_SECRET=synthetic-webhook-secret\n").encode())
    return {"directory": directory, "paths": paths, "source": source, "binding": binding,
            "fence": gate.Fence(paths["fence"], binding), "frozen": frozen,
            "initial_operation": initial_operation, "anchor": anchor, "target": target,
            "database": paths["data"] / "fixture.db", "operation": operation, "project": project,
            "manifest": manifest, "manifest_path": manifest_path, "config": cfg,
            "environment": environment, "strategy": config.config.cfg["scoring_strategy_version"]}


def manifest_input(paths, project, operation, workers, binding, network):
    manifest = startup.manifest_input(paths, project, operation, workers, binding)
    manifest["networks"] = {"isolated": {"external": True, "name": network}}
    for service in manifest["services"].values():
        service.pop("network_mode")
        service["networks"] = ["isolated"]
        service["environment"].update({"REDIS_URL": "redis://redis:6379/0",
            "AI_FILTER_DB_PATH": "/app/data/fixture.db",
            "WEBHOOK_SPOOL_DIR": "/app/data/webhook_spool", "AI_FILTER_DASHBOARD_SNAPSHOTS": "0",
            "MINIFLUX_WEBHOOK_SECRET": "${MINIFLUX_WEBHOOK_SECRET:?}"})
        service["volumes"] = [mount for mount in service["volumes"] if mount.get("read_only", False)] + [
            {"type": "bind", "source": str(paths["data"]), "target": "/app/data",
             "read_only": False, "bind": {"create_host_path": False}}]
    return manifest


def previous_manifest(p, base):
    """No startup/control mounts or protocol env are installed on the old app."""
    previous = deepcopy(p["manifest"])
    names = {name: "previous-" + name for name in previous["services"]}
    previous["services"] = {names[name]: value for name, value in previous["services"].items()}
    env = private.environment_values(p["environment"].read_bytes())
    for service in previous["services"].values():
        service["image"] = base
        for key in ("AI_FILTER_API_KEY", "MINIFLUX_WEBHOOK_SECRET"):
            service["environment"][key] = env[key]
        service["environment"] = {key: value for key, value in service["environment"].items()
                                  if not key.startswith(("AI_FILTER_PRODUCTION_STARTUP_", "AI_FILTER_ADMISSION_"))}
        service["volumes"] = [mount for mount in service["volumes"] if mount["source"] != str(p["paths"]["grants"])]
        for mount in service["volumes"]:
            if mount["source"] == private.RUNTIME:
                mount["source"] = str(p["config"])
    return previous, names


def seal_inputs(root, p):
    """Create stopped previous names; lifecycle must retain every exact ID."""
    base = next(line.split()[1] for line in (root / "Dockerfile").read_text().splitlines() if line.startswith("FROM "))
    previous, names = previous_manifest(p, base)
    old = p["directory"] / "previous.json"
    save(old, previous)
    private.run(["docker", "compose", "--project-name", p["project"], "--project-directory", str(p["paths"]["project"]),
                 "-f", str(old), "create", "--no-recreate", "--no-build", "--pull", "never"])
    records = private.inspect_project({"review": {"project": p["project"], "project_directory": str(p["paths"]["project"])}})
    private.require(all(row["State"]["Status"] == "created" and not row["State"]["Running"] for row in records),
                    "synthetic previous containers are not stopped")
    topology = {name: {"mounts": service["volumes"], "networks": service["networks"], "ports": []}
                for name, service in p["manifest"]["services"].items()}
    review = {**p["source"], "approved": True, "approved_by": "synthetic-activation-workflow",
        "approved_at": int(time.time()) - 1, "expires_at": int(time.time()) + 900,
        "runtime_config_sha256": private.sha(p["config"].read_bytes()),
        "input_manifest_sha256": private.sha(p["manifest_path"].read_bytes()),
        "environment_sha256": private.sha(p["environment"].read_bytes()), "strategy": p["strategy"],
        "project": p["project"], "project_directory": str(p["paths"]["project"]), "health_services": ["ai-filter-api"],
        "existing_topology": private.runtime_topology(records),
        "service_mapping": {name: [names[name]] for name in names}, "topology": topology}
    from scripts.private_control_subpaths import capture
    control = {kind: capture(p["paths"][path], target, p["paths"]["data"]) for kind, path, target in (
        ("state", "slots", "/app/data/startup_slots"), ("freeze", "fence", "/app/data/admission_fence"))}
    review["control_subpaths"] = {name: deepcopy(control) for name in names}
    review_path = p["directory"] / "review.json"
    save(review_path, review)
    p["previous_containers"] = {row["Id"]: row for row in records}
    return private.seal(root, p["config"], p["manifest_path"], p["environment"], review_path, p["directory"] / "sealed")


def migration_permit(p, sealed, digest):
    """Use the actual sender snapshot/identity attached after Go closed-init."""
    permit = startup.migration_permit(p, sealed, digest)
    permit["freeze"].update({"kind": "initial_closed", "operation_id": p["initial_operation"], "generation": 0,
                             "receipt_sha256": gate._hash(p["frozen"])})
    return permit
