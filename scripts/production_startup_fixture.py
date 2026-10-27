"""Synthetic-only disk/manifest inputs for the native closed startup proof."""
from __future__ import annotations

from copy import deepcopy
import hashlib
import json
import os
from pathlib import Path
import time

from scripts import private_release_contract as private
from scripts.verify_private_release_native import save
from services import admission_fence as gate, controlled_startup as isolation, production_migration as migration
from db import database, startup_migration_steps as steps


def prepare(directory, source, workers, raw_dsn, miniflux_source):
    from scripts.production_startup_pg_fixture import create_fixture, close_fixture
    from core import config
    directory.mkdir(mode=0o700)
    paths = {name: directory / name for name in ("project", "data", "fence", "spool", "journal", "grants", "slots")}
    for name, path in paths.items():
        if name != "fence":
            path.mkdir(mode=0o700)
    api_key = "synthetic-never-a-production-secret-" + gate.secrets.token_hex(16)
    os.environ["AI_FILTER_API_KEY"] = api_key
    cfg = directory / "runtime.yaml"
    save(cfg, b"threshold:\n  pass: 7\nscoring_strategy_version: synthetic-startup\nfeeds: []\n")
    config.CONFIG_PATH = str(cfg)
    config.config.reload()
    binding = {"revision": "a" * 40, "image_id": "sha256:" + "b" * 64, "strategy": "synthetic-previous-not-executed",
               "source_sha256": gate.source_sha256(), "deployment_nonce": gate.secrets.token_hex(16)}
    now = int(time.time())
    gate.provision(paths["fence"], binding, {"not_before": now - 1, "expires_at": now + 1800,
                                          "max_admissions": 20, "max_bytes": 100000})
    fence = gate.Fence(paths["fence"], binding)
    fence.open_trial("1" * 64, 0, "synthetic initial")
    fence.stop_new("2" * 64, 1, "synthetic stopped")
    frozen = fence.freeze("3" * 64, 2, "synthetic frozen")
    anchor, target = seed_databases(paths["data"])
    db = paths["data"] / "fixture.db"
    for path in (paths["spool"] / "inflight", paths["spool"] / "dead-letter"):
        path.mkdir(mode=0o700)
    for path in (paths["spool"] / ".lock", paths["journal"] / ".lock", directory / "release.lock", directory / "refresh.lock"):
        save(path, b"")
    from scripts import independent_drain_inventory as inventory
    store = inventory.capacity.receipt.ReceiptStore(paths["spool"])
    accepted, _ = store.accept(b"synthetic original retained", {"entries": []}, strategy="synthetic", revision="synthetic", accepted_at=100.0)
    store.update_status(accepted["delivery_id"], status="completed", completion={"synthetic": True})
    pg, pg_fixture = create_fixture(raw_dsn, miniflux_source)
    closed = close_fixture(pg, pg_fixture)
    dsn = directory / "postgres.json"
    save(dsn, {"dsn": pg_fixture["dsn"]})
    operation = gate.secrets.token_hex(32)
    project = "startup-native-" + gate.secrets.token_hex(6)
    manifest = manifest_input(paths, project, operation, workers, binding)
    manifest_path = directory / "manifest.json"
    save(manifest_path, manifest)
    environment = directory / "environment"
    save(environment, ("AI_FILTER_API_KEY=" + api_key + "\n").encode())
    return {"directory": directory, "paths": paths, "source": source, "workers": workers, "config": cfg,
            "anchor": anchor, "target": target, "database": db, "binding": binding, "fence": fence, "frozen": frozen,
            "postgres": pg, "postgres_fixture": pg_fixture, "closed_operation": closed, "dsn": dsn,
            "operation": operation, "project": project, "manifest": manifest, "manifest_path": manifest_path,
            "environment": environment}


def seed_databases(data):
    from scripts import independent_drain_inventory as inventory
    original = database.DB_PATH
    old_umask = os.umask(0o077)
    try:
        for name in ("target.db", "fixture.db"):
            database.DB_PATH = str(data / name)
            database.init_db()
            conn = migration._connection(data / name)
            conn.executemany("INSERT OR REPLACE INTO app_meta(k,v) VALUES(?,?)", zip(inventory.META_KEYS, ("5", "5", "100")))
            if name == "target.db":
                for step in steps.STEP_IDS:
                    conn.execute("BEGIN IMMEDIATE")
                    steps.apply_step(conn, step, now=1)
                    conn.execute("COMMIT")
                target = migration.schema_fingerprint(conn)
                conn.close()
            else:
                anchor = conn
    finally:
        database.DB_PATH = original
        os.umask(old_umask)
    return anchor, target


def manifest_input(paths, project, operation, workers, binding):
    mounts = [{"type": "bind", "source": str(paths[name]), "target": target,
               "read_only": readonly, "bind": {"create_host_path": False}} for name, target, readonly in (
        ("data", "/app/data/business", False), ("fence", "/app/data/admission_fence", False),
        ("grants", "/app/data/startup_contract", True), ("slots", "/app/data/startup_slots", False))]
    mounts.append({"type": "bind", "source": private.RUNTIME, "target": "/app/config.yaml", "read_only": True})
    environment = {"AI_FILTER_CONFIG_PATH": "/app/config.yaml", "AI_FILTER_API_KEY": "${AI_FILTER_API_KEY:?}",
        "AI_FILTER_DB_PATH": "/app/data/business/fixture.db", "AI_FILTER_ADMISSION_MODE": "enforced",
        "AI_FILTER_ADMISSION_ROOT": "/app/data/admission_fence", "AI_FILTER_ADMISSION_BINDING": json.dumps(binding, sort_keys=True),
        "AI_FILTER_PRODUCTION_STARTUP_OPERATION": operation, "AI_FILTER_PRODUCTION_STARTUP_ROOT": "/app/data/startup_contract"}
    common = {"image": private.IMAGE, "mem_limit": "768m", "memswap_limit": "768m", "pids_limit": 256, "user": f"{os.geteuid()}:{os.getegid()}",
              "cap_drop": ["ALL"], "security_opt": ["no-new-privileges:true"], "network_mode": "none", "restart": "no",
              "volumes": mounts, "healthcheck": {"disable": True}}
    return {"name": project, "services": {
        "ai-filter-api": {**deepcopy(common), "command": ["/app/start.sh"],
                          "environment": {**environment, "AI_FILTER_ROLE": "web", "WEB_CONCURRENCY": str(workers), "GUNICORN_THREADS": "4"}},
        "ai-filter-worker": {**deepcopy(common), "command": ["python", "worker_main.py"],
                             "environment": {**environment, "AI_FILTER_ROLE": "worker"}}}}


def seal_inputs(root, fixture):
    """The previous synthetic containers are only CREATED, never application proof."""
    p = fixture
    manifest = p["manifest"]
    previous = deepcopy(manifest)
    base = next(line.split()[1] for line in (root / "Dockerfile").read_text().splitlines() if line.startswith("FROM "))
    for service in previous["services"].values():
        service["image"] = base
        service["environment"]["AI_FILTER_API_KEY"] = os.environ["AI_FILTER_API_KEY"]
        for mount in service["volumes"]:
            if mount["source"] == private.RUNTIME:
                mount["source"] = str(p["config"])
    old = p["directory"] / "previous.json"
    save(old, previous)
    private.run(["docker", "compose", "--project-name", p["project"], "--project-directory", str(p["paths"]["project"]),
                 "-f", str(old), "create", "--no-build", "--pull", "never"])
    records = private.inspect_project({"review": {"project": p["project"], "project_directory": str(p["paths"]["project"])}})
    private.require(all(not r["State"]["Running"] for r in records), "synthetic previous fixture unexpectedly running")
    topology = {name: {"mounts": service["volumes"], "networks": [], "ports": []} for name, service in manifest["services"].items()}
    review = {**p["source"], "approved": True, "approved_by": "synthetic-startup-workflow",
        "approved_at": int(time.time()) - 1, "expires_at": int(time.time()) + 1800,
        "runtime_config_sha256": private.sha(p["config"].read_bytes()), "input_manifest_sha256": private.sha(p["manifest_path"].read_bytes()),
        "environment_sha256": private.sha(p["environment"].read_bytes()), "strategy": "synthetic-startup",
        "project": p["project"], "project_directory": str(p["paths"]["project"]), "health_services": ["ai-filter-api"],
        "existing_topology": private.runtime_topology(records), "service_mapping": {name: [name] for name in manifest["services"]},
        "topology": topology}
    review_path = p["directory"] / "review.json"
    save(review_path, review)
    sealed = private.seal(root, p["config"], p["manifest_path"], p["environment"], review_path, p["directory"] / "sealed")
    return sealed


def migration_permit(p, sealed, digest):
    from core.config import config as holder
    from scripts import independent_drain_inventory as inventory
    from scripts.sender_drain_inventory import PostgresReader
    source = p["source"]
    contract, _manifest, contract_sha = private.load(sealed, Path(__file__).resolve().parents[1], require_fresh=True)
    paths, db = p["paths"], p["database"]
    sender = {"dsn_file": str(p["dsn"]), "dsn_sha256": hashlib.sha256(p["dsn"].read_bytes()).hexdigest(),
              "expected_identity": p["postgres_fixture"]["expected_identity"], "closed_operation_id": p["closed_operation"],
              "manifest": p["postgres_fixture"]["manifest"]}
    spec = {"spool": str(paths["spool"]), "journal": str(paths["journal"]), "database": str(db), "identities": {
        "spool": inventory.directory_identity(paths["spool"]), "journal": inventory.directory_identity(paths["journal"]),
        "database": inventory._identity(db.lstat())}}
    reader = PostgresReader(p["dsn"], sender["expected_identity"])
    # A preflight sample is only a reviewed fingerprint. locked_session takes
    # the real full lock set and independently samples again before action.
    with reader.locked(p["closed_operation"], sender["manifest"]) as frozen:
        with inventory.locked_inventory(spec, postgres=lambda: frozen.snapshot) as sample:
            inventory_sha = migration.inventory_fingerprint(sample)
    lock_paths = {"release": p["directory"] / "release.lock", "refresh": p["directory"] / "refresh.lock",
                  "controller": paths["fence"] / "locks/controller.lock", "activity": paths["fence"] / "locks/activity.lock",
                  "spool": paths["spool"] / ".lock", "journal": paths["journal"] / ".lock"}
    permit = {"schema": migration.SCHEMA, "operation_id": gate.secrets.token_hex(32), "parent_operation_id": p["operation"],
        "approved_by": "synthetic-startup-workflow", "not_before": int(time.time()) - 1, "expires_at": int(time.time()) + 900,
        "runtime_sources_sha256": isolation.runtime_sources_sha256(), "migration_code_sha256": migration.migration_code_sha256(),
        "source": {"revision": source["source_sha"], "tree": source["source_tree"], "image_id": digest,
                   "private_contract_sha256": contract_sha, "manifest_sha256": contract["files"]["compose.json"]},
        "config": {"path": str(sealed.parent / "runtime.yaml"), "sha256": contract["files"]["runtime.yaml"],
                   "effective_sha256": gate._hash(holder.cfg), "private_contract_path": str(sealed)},
        "database": migration.database_identity(db), "freeze": {"root": str(paths["fence"]), "binding": p["binding"],
            "manifest_sha256": gate._hash(p["fence"]._manifest()), "operation_id": "3" * 64, "generation": 3,
            "receipt_sha256": gate._hash(p["frozen"])}, "locks": {key: migration.file_identity(path) for key, path in lock_paths.items()},
        "before": migration.database_fingerprint(p["anchor"]), "target_schema_sha256": p["target"], "step_ids": list(steps.STEP_IDS),
        "sender": sender, "sender_contract_sha256": gate._hash(sender), "inventory": spec, "inventory_sha256": inventory_sha,
        "minimum_free_bytes": 64 * 1024**2, "max_database_bytes": 4 * 1024**3}
    permit["compatibility"] = {"old_binding": p["binding"], "candidate_source": permit["source"],
        "from_schema_sha256": permit["before"]["schema_sha256"], "to_schema_sha256": permit["target_schema_sha256"],
        "sqlite_policy": "fixed-current-event-core-forward-only", "old_image_execution": "not-proven-no-fallback"}
    permit["compatibility_sha256"] = gate._hash(permit["compatibility"])
    return permit
