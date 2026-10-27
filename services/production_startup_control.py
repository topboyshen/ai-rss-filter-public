"""Host-only controller for finite closed runtime grants and exact queries."""
from __future__ import annotations

import fcntl
import json
import os
from pathlib import Path
import time

from services import admission_fence as gate, controlled_startup as isolation
from services import production_migration as migration, production_startup as runtime


def _session(session, *, active=True):
    gate._require(type(session) is migration.LockedMigration, "real migration lock capability required")
    session.check(active=active)
    session.recheck_inventory(active=active)
    result = session.query_migration(session.permit["operation_id"])
    gate._require(result["status"] == "migration_completed" and result["durable"], "completed migration required")
    return result


def authorize(session, root, *, operation_id, paths, web_workers, threads=16):
    """Seal a narrow descriptor; its mount must be readonly in the private plan."""
    result = _session(session)
    root = Path(root)
    gate._directory(root)
    gate._require(gate.HEX64.fullmatch(operation_id) and operation_id != session.permit["operation_id"]
                  and type(web_workers) is int and 1 <= web_workers <= 4
                  and type(threads) is int and 4 <= threads <= 64, "invalid finite startup operation/topology")
    gate._require(not gate._bounded_paths(root, 32), "startup descriptor already attempted")
    gate._require(set(paths) == {"database", "config", "freeze", "state"}, "exact runtime path mapping required")
    for path in paths.values():
        value = Path(path)
        gate._require(value.is_absolute() and str(value) == os.path.normpath(value), "invalid runtime path mapping")
    permit = session.permit
    conn = migration._connection(session.path, readonly=True)
    try:
        rows = conn.execute(f"SELECT operation_id,step_number,record FROM {migration.JOURNAL} ORDER BY step_number").fetchall()
    finally:
        conn.close()
    identity = {key: permit["database"]["file"][key] for key in ("device", "inode", "uid")}
    actual_db = migration.database_identity(session.path)
    gate._require(actual_db["wal"] is not None and actual_db["shm"] is not None, "startup requires retained WAL and SHM")
    sidecars = {name: {key: actual_db[name][key] for key in ("device", "inode", "uid", "mode")} for name in ("wal", "shm")}
    descriptor = {"schema": runtime.SCHEMA, "operation_id": operation_id,
        "runtime_sources_sha256": permit["runtime_sources_sha256"], "source": permit["source"],
        "config": {"path": paths["config"], "sha256": permit["config"]["sha256"],
                   "effective_sha256": permit["config"]["effective_sha256"]},
        "database": {"path": paths["database"], "identity": identity, "sidecars": sidecars},
        "migration": {"operation_id": result["operation_id"], "permit_sha256": result["permit_sha256"],
                      "journal_sha256": gate._hash([list(row) for row in rows]),
                      "schema_sha256": result["postcondition"]["schema_sha256"]},
        "freeze": {**permit["freeze"], "root": paths["freeze"]}, "state_path": paths["state"],
        "clock": {"boot_id": gate._boot_identity(), "wall": time.time(), "elapsed": gate._elapsed(),
                  "expires_at": permit["expires_at"]},
        "slots": {**{f"web-{i}": "web" for i in range(1, web_workers + 1)}, "worker-1": "worker"}, "threads": threads}
    gate._write(root / "descriptor.json", descriptor)
    return descriptor



def _descriptor(session, root, *, active=True):
    descriptor = gate._read(root / "descriptor.json")
    permit = session.permit
    gate._require(descriptor["source"] == permit["source"]
                  and descriptor["runtime_sources_sha256"] == permit["runtime_sources_sha256"]
                  and descriptor["migration"]["operation_id"] == permit["operation_id"]
                  and descriptor["migration"]["permit_sha256"] == gate._hash(permit)
                  and descriptor["migration"]["schema_sha256"] == permit["target_schema_sha256"]
                  and descriptor["clock"]["expires_at"] == permit["expires_at"], "startup descriptor migration binding differs")
    runtime._clock(descriptor["clock"], active=active)
    return descriptor


def _container(container_id):
    from scripts import private_release_contract as private
    gate._require(isinstance(container_id, str) and gate.HEX64.fullmatch(container_id), "exact container ID required")
    rows = json.loads(private.run(["docker", "inspect", container_id]))
    gate._require(isinstance(rows, list) and len(rows) == 1 and rows[0]["Id"] == container_id, "actual container differs")
    return rows[0]


def _mount(record, source, target, *, writable):
    rows = [row for row in record["Mounts"] if row["Destination"] == target]
    gate._require(len(rows) == 1 and rows[0]["Type"] == "bind" and rows[0]["Source"] == str(source)
                  and rows[0]["RW"] is writable, "startup mount differs")



def _protect_readonly_inputs(record, root, state, config):
    root, state, config = (Path(value).resolve() for value in (root, state, config))
    gate._require(not root.is_relative_to(state) and not state.is_relative_to(root), "startup grant/state storage overlaps")
    for mount in record["Mounts"]:
        if mount["Type"] != "bind" or not mount["RW"]:
            continue
        source = Path(mount["Source"]).resolve()
        gate._require(not root.is_relative_to(source) and not config.is_relative_to(source),
                      "readonly startup/config input exposed by writable alias")


def verify_container(session, root, state, descriptor, container_id, role, *, running):
    from scripts import private_release_contract as private
    record = _container(container_id)
    _protect_readonly_inputs(record, root, state, Path(session.permit["config"]["path"]))
    gate._require(record["Image"] == descriptor["source"]["image_id"]
                  and record["State"]["Running"] is running and record["RestartCount"] == 0
                  and record["Config"]["Hostname"] == container_id[:12], "startup container/image/state differs")
    contract, manifest, _digest = private.load(session.permit["config"]["private_contract_path"], private.Path(__file__).resolve().parents[1])
    env = private.environment_values(private.private_file(Path(session.permit["config"]["private_contract_path"]).parent / "environment", sealed=True)[0])
    matches = [service for service in manifest["services"].values()
               if service.get("environment", {}).get("AI_FILTER_ROLE") == role]
    gate._require(len(matches) == 1, "startup role must map to exactly one reviewed service")
    image = json.loads(private.run(["docker", "image", "inspect", record["Image"]]))[0]
    private.verify_execution(matches[0], record, image, env, expected_running=running)
    _topology(contract, manifest, matches[0], record)
    values = private.runtime_environment(record["Config"].get("Env", []))
    gate._require(values.get(runtime.ENV_OPERATION) == descriptor["operation_id"]
                  and isolation.ENV_OPERATION not in values and values.get("AI_FILTER_ADMISSION_MODE") == "enforced"
                  and values.get("AI_FILTER_ADMISSION_ROOT") == descriptor["freeze"]["root"]
                  and gate._parsed(values.get("AI_FILTER_ADMISSION_BINDING", "null")) == descriptor["freeze"]["binding"]
                  and values.get("AI_FILTER_DB_PATH") == descriptor["database"]["path"]
                  and values.get("AI_FILTER_CONFIG_PATH") == descriptor["config"]["path"],
                  "startup runtime operation environment differs")
    _mount(record, root, values.get(runtime.ENV_ROOT), writable=False)
    _mount(record, state, descriptor["state_path"], writable=True)
    _mount(record, session.path.parent, str(Path(descriptor["database"]["path"]).parent), writable=True)
    _mount(record, session.permit["freeze"]["root"], descriptor["freeze"]["root"], writable=True)
    _mount(record, session.permit["config"]["path"], descriptor["config"]["path"], writable=False)
    return record



def _topology(contract, manifest, service, record):
    from scripts import private_release_contract as private
    names = [name for name, value in manifest["services"].items() if value == service]
    labels = record["Config"]["Labels"]
    gate._require(len(names) == 1 and labels.get("com.docker.compose.service") == names[0]
                  and labels.get("com.docker.compose.project") == contract["review"]["project"]
                  and labels.get("com.docker.compose.project.working_dir") == contract["review"]["project_directory"],
                  "startup project/service identity differs")
    mounts = [{"type": value["type"], "source": value["source"] if value["type"] == "bind" else manifest["volumes"][value["source"]]["name"],
               "target": value["target"], "read_only": value.get("read_only", False)} for value in service.get("volumes", [])]
    mounts += [{"type": "bind", "source": manifest["secrets"][name]["file"], "target": "/run/secrets/" + name,
                "read_only": True} for name in service.get("secrets", [])]
    actual = private.runtime_topology([record])[names[0]][0]
    networks = sorted(manifest["networks"][key]["name"] for key in service.get("networks", []))
    if service.get("network_mode") == "none":
        networks = ["none"]
    ports = sorted([{"target": f"{p['target']}/{p['protocol']}", "host_ip": p["host_ip"], "published": str(p["published"])}
                    for p in service.get("ports", [])], key=private.encoded)
    gate._require(actual["mounts"] == sorted(mounts, key=lambda item: item["target"])
                  and actual["networks"] == networks and actual["ports"] == ports, "startup actual topology differs")


def grant(session, root, state, *, slot, container_id, attempt_operation_id):
    _session(session)
    root, state = Path(root), Path(state)
    gate._directory(root)
    gate._directory(state)
    descriptor = _descriptor(session, root)
    gate._require(slot in descriptor["slots"] and gate.HEX64.fullmatch(attempt_operation_id), "exact initial slot operation required")
    record = verify_container(session, root, state, descriptor, container_id, descriptor["slots"][slot], running=False)
    gate._require(record["State"].get("Status") == "created", "initial grant requires never-started container")
    return _write_grant(root, state, descriptor, slot, 0, container_id, attempt_operation_id, None)


def _write_grant(root, state, descriptor, slot, attempt, container, operation, previous):
    path = root / f"{slot}.{attempt}.grant.json"
    gate._require(not gate._present(path) and not gate._present(path.with_name(path.name + ".pending")), "grant already attempted")
    lock_path = state / f"{slot}.{attempt}.lock"
    fd = os.open(lock_path, os.O_CREAT | os.O_EXCL | os.O_WRONLY | os.O_NOFOLLOW, 0o600)
    try:
        os.fsync(fd)
        info = os.fstat(fd)
    finally:
        os.close(fd)
    gate._sync_directory(state)
    value = {"schema": runtime.SCHEMA, "operation_id": descriptor["operation_id"], "descriptor_sha256": gate._hash(descriptor),
             "slot": slot, "attempt": attempt, "role": descriptor["slots"][slot], "container_id": container,
             "image_id": descriptor["source"]["image_id"], "attempt_operation_id": operation,
             "previous_receipt_sha256": previous, "process_nonce": gate.secrets.token_hex(16),
             "slot_lock": {"device": info.st_dev, "inode": info.st_ino, "uid": info.st_uid}}
    gate._write(path, value)
    return value


def _receipt(state, descriptor, slot, attempt):
    gate._require(slot in descriptor["slots"] and type(attempt) is int and attempt in (0, 1), "invalid query slot")
    intent, final = (state / f"{slot}.{attempt}{suffix}" for suffix in (".intent.json", ".json"))
    value = isolation._read_complete(final)
    gate._require(isolation._read_complete(intent) == value and value["slot"] == slot and value["attempt"] == attempt
                  and value["descriptor_sha256"] == gate._hash(descriptor), "startup receipt differs")
    fd = os.open(intent, os.O_RDONLY | os.O_NOFOLLOW)
    try:
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            live = False
        except BlockingIOError:
            live = True
    finally:
        os.close(fd)
    return value, live


def _actual_process(container_id, owner):
    """Fixed, bounded query; no private env/config is returned by the process."""
    from scripts import private_release_contract as private
    code = ("import json,os; from services.production_startup import process_identity; "
            f"p={owner['pid']!r}; "
            "print(json.dumps(process_identity(p) if os.path.exists('/proc/'+str(p)) else None))")
    return json.loads(private.run(["docker", "exec", container_id, "python", "-I", "-c",
                                  "import sys;sys.path.insert(0,'/app');" + code]))



def _unique_owner(state, descriptor, receipt):
    identities = []
    for slot in descriptor["slots"]:
        for attempt in (0, 1):
            path = state / f"{slot}.{attempt}.json"
            if not gate._present(path):
                continue
            other = isolation._read_complete(path)
            owner = other["owner"]
            identities.append((other["container_id"], owner["pid"], owner["start_ticks"], owner["pid_namespace"], owner["boot_id"]))
    gate._require(len(identities) == len(set(identities)) and receipt["owner"]["pid"] > 0, "one actual owner cannot fill multiple slots")


def query(session, root, state, *, slot, attempt, operation_id):
    _session(session, active=False)
    root, state = Path(root), Path(state)
    descriptor = _descriptor(session, root, active=False)
    receipt, live = _receipt(state, descriptor, slot, attempt)
    _unique_owner(state, descriptor, receipt)
    grant_value = gate._read(root / f"{slot}.{attempt}.grant.json")
    gate._require(receipt["attempt_operation_id"] == operation_id and all(receipt[k] == v for k, v in grant_value.items()),
                  "query original slot operation only")
    record = _container(receipt["container_id"])
    owner = master = None
    if record["State"]["Running"]:
        record = verify_container(session, root, state, descriptor, receipt["container_id"], receipt["role"], running=True)
        owner = _actual_process(receipt["container_id"], receipt["owner"])
        master = _actual_process(receipt["container_id"], receipt["master"])
    matches = owner == receipt["owner"] and master == receipt["master"]
    matches = matches and ((receipt["role"] == "worker" and owner == master and owner["pid"] == 1)
                           or (receipt["role"] == "web" and owner["ppid"] == master["pid"] == 1 and owner["pid"] != 1))
    return {"status": "closed_initialized" if live and matches else "startup_uncertain",
            "receipt_sha256": gate._hash(receipt), "container_id": record["Id"], "started_at": record["State"]["StartedAt"],
            "restart_count": record["RestartCount"], "owner_lease_live": live, "actual_owner_matches": matches,
            "business_open": False, "switch_authorized": False, "health_verified": False}


def replace(session, root, state, *, slot, operation_id, previous_receipt_sha256, container_id):
    """One replacement after independently proving the original owner is dead."""
    _session(session)
    root, state = Path(root), Path(state)
    descriptor = _descriptor(session, root)
    receipt, live = _receipt(state, descriptor, slot, 0)
    gate._require(not live and gate._hash(receipt) == previous_receipt_sha256 and gate.HEX64.fullmatch(operation_id)
                  and operation_id != receipt["attempt_operation_id"], "replacement original outcome unresolved")
    original = _container(receipt["container_id"])
    gate._require(original["RestartCount"] == 0 and original["Image"] == descriptor["source"]["image_id"], "old container identity changed")
    if original["State"]["Running"]:
        actual = _actual_process(receipt["container_id"], receipt["owner"])
        gate._require(actual is None or actual != receipt["owner"], "old startup owner remains alive")
    else:
        gate._require(original["State"]["Status"] == "exited" and not original["State"].get("Restarting"), "old container outcome unknown")
    same = container_id == receipt["container_id"]
    gate._require(receipt["role"] == "web" or not same, "worker replacement requires a new container")
    record = verify_container(session, root, state, descriptor, container_id, receipt["role"], running=same)
    gate._require(same or record["State"]["Status"] == "created", "replacement container already attempted")
    return _write_grant(root, state, descriptor, slot, 1, container_id, operation_id, previous_receipt_sha256)
