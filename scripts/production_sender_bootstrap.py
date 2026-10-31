#!/usr/bin/env python3
"""One retained candidate sender, initially refusing business until CLOSED135.

Consumes reviewed offline build evidence; never clones/builds Go, stops legacy,
recreates/removes containers, resets PG history, or sends any OPEN operation.
"""
from __future__ import annotations

import hashlib
import io
import json
from pathlib import Path
import re
import stat
import sys
import tarfile
import time
from urllib.parse import urlsplit

ROOT = Path(__file__).resolve().parents[1]
for directory in (ROOT, ROOT / "scripts"):
    if str(directory) not in sys.path:
        sys.path.insert(0, str(directory))

from services import admission_fence as gate  # noqa: E402
from services import production_activation as durable  # noqa: E402
from scripts import private_release_contract as private  # noqa: E402
from scripts.production_legacy_takeover import LegacyTakeover  # noqa: E402
from production_sender_driver import NativeSenderActivationTransport, SenderActivationDriver, PostgresReader, sources  # noqa: E402
from production_receiver_driver import _capture  # noqa: E402
from sender_drain_inventory import IDENTITY_SQL, IDENTITY_KEYS  # noqa: E402
from miniflux_handoff_adapter import source_sha256 as bridge_source  # noqa: E402

SCHEMA = "production-sender-bootstrap-v1"
MAX_BINARY = 64 * 1024**2
PLAN_KEYS = {"schema", "operation_id", "source_sha256", "revision", "project", "service", "compose", "environment", "build",
             "build_evidence", "mount_identities", "legacy_operation_id", "legacy_plan_sha256", "network", "postgres_before", "dsn_file", "token_file",
             "control_url", "manifest", "contract", "window"}
SERVICE_KEYS = {"image", "environment", "user", "mem_limit", "memswap_limit", "pids_limit", "cpus", "cap_drop", "security_opt",
                "restart", "read_only", "volumes", "ports", "networks", "platform", "labels", "healthcheck"}
PG_KEYS = ("database", "schema", "system_identifier", "postmaster_started_at", "current_user", "session_user")
PG_SQL = """SELECT current_database(),current_schema(),(SELECT system_identifier::text FROM pg_control_system()),
 pg_postmaster_start_time()::text,current_user,session_user"""


def source_sha256():
    names = ("scripts/production_sender_bootstrap.py", "scripts/production_legacy_takeover.py", "scripts/private_release_contract.py",
             "scripts/production_sender_driver.py", "scripts/sender_namespace_http.py", "scripts/sender_drain_inventory.py",
             "scripts/handoff_runtime_adapter.py", "scripts/miniflux_handoff_adapter.py", "ops/miniflux/Dockerfile",
             "ops/miniflux/pause-source-lock.json")
    return gate._hash({name: hashlib.sha256((ROOT / name).read_bytes()).hexdigest() for name in names})


def _file(record):
    gate._require(isinstance(record, dict) and set(record) == {"path", "sha256"}, "sealed input path/hash required")
    raw, _identity = private.private_file(Path(record["path"]), sealed=True)
    gate._require(hashlib.sha256(raw).hexdigest() == record["sha256"], "sealed bootstrap input changed")
    return raw


def _docker(args, *, seconds=3, maximum=2 * 1024**2):
    return _capture(["/usr/bin/docker", "--host", "unix:///var/run/docker.sock", *args], seconds=seconds, max_bytes=maximum)


def _inspect(identifier):
    gate._require(isinstance(identifier, str) and gate.HEX64.fullmatch(identifier), "exact sender container required")
    rows = gate._parsed(_docker(["inspect", identifier]))
    gate._require(isinstance(rows, list) and len(rows) == 1 and rows[0]["Id"] == identifier, "sender actual inspect differs")
    return rows[0]


def _image(reference):
    rows = gate._parsed(_docker(["image", "inspect", reference]))
    gate._require(len(rows) == 1 and rows[0]["Id"] == reference and rows[0]["Architecture"] == "amd64"
                  and rows[0]["Os"] == "linux", "actual immutable amd64 sender image required")
    return rows[0]


def _network(plan):
    rows = gate._parsed(_docker(["network", "inspect", plan["network"]["Name"]]))
    keys = {"Name", "Id", "Driver", "Scope", "Internal", "Options", "IPAM"}
    gate._require(set(plan["network"]) == keys and len(rows) == 1
                  and {key: rows[0][key] for key in keys} == plan["network"], "existing sender network differs")


def _postgres(plan, *, after=False):
    import psycopg
    secret = gate._read(Path(plan["dsn_file"]))
    gate._require(set(secret) == {"dsn"} and isinstance(secret["dsn"], str) and 0 < len(secret["dsn"]) <= 8192, "private sender DSN required")
    deadline = time.monotonic() + 10
    with psycopg.connect(secret["dsn"], connect_timeout=3, autocommit=True) as connection:
        try:
            connection.execute("BEGIN READ ONLY")
            connection.execute("SET LOCAL statement_timeout='3000ms'")
            connection.execute("SET LOCAL lock_timeout='1000ms'")
            base = dict(zip(PG_KEYS, connection.execute(PG_SQL).fetchone()))
            versions = connection.execute("SELECT version FROM schema_version LIMIT 2").fetchall()
            gate._require(len(versions) == 1 and versions[0][0] in {"132", "133", "134", "135"},
                          "sender predecessor schema lacks this bootstrap's verified upgrade coverage")
            actual = {**base, "schema_version": int(versions[0][0])}
            wanted = plan["postgres_before"]
            gate._require(set(wanted) == set(PG_KEYS) | {"schema_version"}
                          and all(actual[key] == wanted[key] for key in PG_KEYS), "sender PG host/database/session changed")
            if after:
                gate._require(actual["schema_version"] == 135, "actual sender migration135 not complete")
                actual = dict(zip(IDENTITY_KEYS, connection.execute(IDENTITY_SQL).fetchone()))
            else:
                gate._require(actual == wanted, "original sender PG schema changed")
                for table in ("handoff_state", "handoff_operations", "handoff_leases"):
                    exists = connection.execute("SELECT to_regclass(%s)::text", (table,)).fetchone()[0]
                    if exists is not None:
                        gate._require(not connection.execute("SELECT 1 FROM " + table + " LIMIT 1").fetchall(),
                                      "existing control history cannot be initialized/reset")
            gate._require(time.monotonic() < deadline, "sender PG metadata deadline")
            return actual
        finally:
            connection.execute("ROLLBACK")


def _manifest(plan, raw, environment):
    manifest = gate._parsed(raw)
    gate._require(set(manifest) == {"services", "networks"} and set(manifest["services"]) == {plan["service"]}
                  and manifest["networks"] == {"sender": {"name": plan["network"]["Name"], "external": True}},
                  "one new sender service on one existing network required")
    service = manifest["services"][plan["service"]]
    gate._require(set(service) == SERVICE_KEYS and service["networks"] == ["sender"] and service["platform"] == "linux/amd64"
                  and service["restart"] == "no" and service["read_only"] is True and service["user"] == "65534:65534"
                  and service["cap_drop"] == ["ALL"] and service["security_opt"] == ["no-new-privileges:true"]
                  and service["healthcheck"] == {"disable": True}, "exact closed sender execution scope required")
    gate._require(type(service["pids_limit"]) is int and 1 <= service["pids_limit"] <= 256
                  and 1 <= private.memory_bytes(service["mem_limit"]) <= 768 * 1024**2
                  and private.memory_bytes(service["memswap_limit"]) == private.memory_bytes(service["mem_limit"])
                  and type(service["cpus"]) in (int, float) and 0 < service["cpus"] <= 1, "sender hard resource ceiling")
    gate._require(service["labels"] == {"org.opencontainers.image.revision": plan["revision"], "io.ai-filter.handoff.role": "miniflux",
                  "io.ai-filter.sender.bootstrap": plan["operation_id"]}, "sender source/role/operation labels required")
    for mount in service["volumes"]:
        gate._require(set(mount) == {"type", "source", "target", "read_only"} and mount["type"] == "bind"
                      and type(mount["read_only"]) is bool and mount["target"].startswith("/")
                      and ".." not in Path(mount["target"]).parts
                      and not Path("/usr/bin/miniflux").is_relative_to(Path(mount["target"])), "exact sender bind mount required")
        private.safe_path(mount["source"])
    gate._require(len({m["target"] for m in service["volumes"]}) == len(service["volumes"]) <= 8
                  and isinstance(service["ports"], list) and len(service["ports"]) <= 4, "sender topology bound")
    _mount_identities(plan, service)
    image = _image(service["image"])
    gate._require(private.command_vector(image["Config"].get("Entrypoint")) == ["/usr/bin/miniflux"]
                  and not private.command_vector(image["Config"].get("Cmd")), "locked sender binary entrypoint required")
    env = private.role_environment(service, image["Config"], environment)
    endpoint = urlsplit(plan["control_url"])
    gate._require(endpoint.scheme == "http" and endpoint.hostname == "127.0.0.1" and endpoint.port
                  and endpoint.path in ("", "/") and not endpoint.query and not endpoint.fragment
                  and not endpoint.username and not endpoint.password, "fixed private sender control URL required")
    token = gate._read(Path(plan["token_file"]))
    gate._require(set(token) == {"token"} and gate.HEX64.fullmatch(token["token"]), "private sender control token required")
    dsn = gate._read(Path(plan["dsn_file"]))
    gate._require(set(dsn) == {"dsn"} and env.get("DATABASE_URL") == dsn["dsn"],
                  "sender and independent metadata reader must use the same sealed DSN")
    gate._require(not env.get("DATABASE_URL_FILE") and env.get("CREATE_ADMIN", "0") == "0",
                  "bootstrap cannot override PG identity through files or create admin business rows")
    wanted = {"HANDOFF_ENABLED": "1", "WEBHOOK_OUTBOX": "1", "RUN_MIGRATIONS": "1", "HANDOFF_MANIFEST": plan["manifest"],
              "WEBHOOK_OUTBOX_CONTRACT": plan["contract"], "HANDOFF_CONTROL_TOKEN": token["token"],
              "HANDOFF_CONTROL_ADDR": "127.0.0.1:" + str(endpoint.port)}
    gate._require(all(env.get(key) == value for key, value in wanted.items()), "first CLOSED protections/environment differ")
    return manifest, service, image


def _build(plan, image):
    value = gate._parsed(_file(plan["build"]))
    lock = json.loads((ROOT / "ops/miniflux/pause-source-lock.json").read_text())
    keys = {"schema", "revision", "upstream_revision", "pause_lock_sha256", "dockerfile_sha256", "binary_sha256", "image_id",
            "go_version", "build_evidence_sha256"}
    gate._require(set(value) == keys and value["schema"] == "reviewed-miniflux-offline-build-v1"
                  and value["revision"] == plan["revision"] and value["upstream_revision"] == lock["revision"]
                  and value["pause_lock_sha256"] == hashlib.sha256((ROOT / "ops/miniflux/pause-source-lock.json").read_bytes()).hexdigest()
                  and value["dockerfile_sha256"] == hashlib.sha256((ROOT / "ops/miniflux/Dockerfile").read_bytes()).hexdigest()
                  and value["image_id"] == image["Id"] and value["go_version"] == "go1.26.4"
                  and all(isinstance(value[key], str) and gate.HEX64.fullmatch(value[key])
                          for key in ("binary_sha256", "build_evidence_sha256"))
                  and image["Config"].get("Labels", {}).get("org.opencontainers.image.revision") == plan["revision"],
                  "reviewed offline build/source/image evidence required")
    gate._require(hashlib.sha256(_file(plan["build_evidence"])).hexdigest() == value["build_evidence_sha256"],
                  "actual reviewed offline build evidence missing or changed")
    return value


def _load(legacy, plan, root, *, active=True):
    gate._require(type(legacy) is LegacyTakeover, "real retained legacy takeover capability required")
    legacy.check(active=active)
    stopped = legacy.check_stopped()
    gate._require(set(plan) == PLAN_KEYS and plan["schema"] == SCHEMA and plan["source_sha256"] == source_sha256()
                  and gate.HEX64.fullmatch(plan["operation_id"]) and re.fullmatch(r"[0-9a-f]{40}", plan["revision"])
                  and plan["legacy_operation_id"] == stopped["operation_id"]
                  and plan["legacy_plan_sha256"] == stopped["plan_sha256"], "exact bootstrap source/legacy binding required")
    gate._require(private.source_identity(ROOT)["source_sha"] == plan["revision"], "formal clean bootstrap source required")
    gate._require(re.fullmatch(r"[a-z][a-z0-9_-]{0,62}", plan["project"])
                  and plan["service"] == "sender_" + plan["operation_id"][:16], "dedicated new sender name required")
    window = plan["window"]
    gate._require(set(window) == {"not_before", "expires_at"} and all(type(v) is int for v in window.values())
                  and 0 < window["expires_at"] - window["not_before"] <= 600, "finite bootstrap window required")
    if active:
        gate._require(window["not_before"] <= time.time() < window["expires_at"], "bootstrap window expired")
    gate._directory(Path(root))
    values = private.environment_values(_file(plan["environment"]))
    manifest, service, image = _manifest(plan, _file(plan["compose"]), values)
    build = _build(plan, image)
    _same_topology(legacy, plan, service)
    _network(plan)
    return manifest, service, image, values, build


def _compose(plan):
    return ["compose", "--project-name", plan["project"], "--project-directory", str(Path(plan["compose"]["path"]).parent),
            "--env-file", plan["environment"]["path"], "-f", plan["compose"]["path"]]


def _candidate(plan):
    raw = _docker(["ps", "-a", "--filter", "label=com.docker.compose.project=" + plan["project"], "--format", "{{.ID}}", "--no-trunc"])
    identifiers = raw.decode().split()
    gate._require(len(identifiers) == 1 and gate.HEX64.fullmatch(identifiers[0]), "exact one new sender candidate required")
    return _inspect(identifiers[0])


def _verify(plan, inputs, row, *, running):
    _manifest_value, service, image, values, _build_value = inputs
    private.verify_execution(service, row, image, values, expected_running=running)
    labels = row["Config"]["Labels"]
    gate._require(labels.get("com.docker.compose.project") == plan["project"]
                  and labels.get("com.docker.compose.service") == plan["service"]
                  and labels.get("com.docker.compose.project.working_dir") == str(Path(plan["compose"]["path"]).parent)
                  and all(labels.get(key) == value for key, value in service["labels"].items())
                  and row["RestartCount"] == 0 and not row["State"].get("Paused") and not row["State"].get("Restarting")
                  and not row["State"].get("OOMKilled") and not row["State"].get("Dead")
                  and row["HostConfig"].get("ReadonlyRootfs") is True
                  and row["HostConfig"].get("RestartPolicy", {}).get("Name") == "no", "sender actual runtime identity differs")
    mounts = [{"type": item["Type"], "source": item["Source"], "target": item["Destination"], "read_only": not item["RW"]}
              for item in row["Mounts"]]
    gate._require(sorted(mounts, key=lambda x: x["target"]) == sorted(service["volumes"], key=lambda x: x["target"]),
                  "sender actual mounts differ")
    ports = sorted((target, p["HostIp"], p["HostPort"]) for target, rows in row["HostConfig"].get("PortBindings", {}).items() for p in rows or [])
    wanted = sorted((str(p["target"]) + "/" + p["protocol"], p["host_ip"], str(p["published"])) for p in service["ports"])
    gate._require(ports == wanted and row["HostConfig"]["NetworkMode"] == plan["network"]["Name"], "sender network/port drift")
    networks = row["NetworkSettings"]["Networks"]
    gate._require(set(networks) <= {plan["network"]["Name"]}, "unreviewed sender network")
    if running:
        gate._require(set(networks) == {plan["network"]["Name"]}
                      and networks[plan["network"]["Name"]]["NetworkID"] == plan["network"]["Id"], "actual sender network ID differs")


def _binary(identifier, expected):
    raw = _docker(["cp", identifier + ":/usr/bin/miniflux", "-"], seconds=10, maximum=MAX_BINARY + 1024**2)
    with tarfile.open(fileobj=io.BytesIO(raw), mode="r:") as archive:
        members = archive.getmembers()
        gate._require(len(members) == 1 and members[0].name == "miniflux" and members[0].isfile()
                      and 0 < members[0].size <= MAX_BINARY, "exact regular sender binary archive required")
        digest, count = hashlib.sha256(), 0
        with archive.extractfile(members[0]) as stream:
            while chunk := stream.read(1024**2):
                count += len(chunk)
                gate._require(count <= MAX_BINARY, "sender binary size bound")
                digest.update(chunk)
        gate._require(count == members[0].size and digest.hexdigest() == expected, "actual created image binary differs from reviewed build")
    return {"sha256": digest.hexdigest(), "bytes": count, "container_id": identifier}


def create(legacy, plan, evidence_root):
    root = Path(evidence_root)
    inputs = _load(legacy, plan, root)
    gate._require(not any(gate._present(root / name) for name in ("create.intent.json", "create.intent.json.pending", "created.json")),
                  "sender create attempted; inspect original only")
    existing = _docker(["ps", "-a", "--filter", "label=com.docker.compose.project=" + plan["project"], "--format", "{{.ID}}"])
    gate._require(not existing.strip(), "new sender project already has containers")
    before = _postgres(plan)
    intent = {"plan_sha256": gate._hash(plan), "postgres_before": before, "legacy": legacy.check_stopped()}
    gate._write(root / "create.intent.json", intent)
    _docker([*_compose(plan), "create", "--no-recreate", "--no-build", "--pull", "never", plan["service"]], seconds=30)
    row = _candidate(plan)
    _verify(plan, inputs, row, running=False)
    binary = _binary(row["Id"], inputs[-1]["binary_sha256"])
    legacy.check_stopped()
    value = {"plan_sha256": gate._hash(plan), "container_id": row["Id"], "binary": binary,
             "status": "created_unstarted", "business_open": False}
    gate._write(root / "created.json", value)
    return value


def start(legacy, plan, evidence_root):
    root = Path(evidence_root)
    inputs = _load(legacy, plan, root)
    created = durable.read_confirmed(root / "created.json")
    gate._require(created["plan_sha256"] == gate._hash(plan), "original created sender differs")
    row = _inspect(created["container_id"])
    _verify(plan, inputs, row, running=False)
    gate._require(not gate._present(root / "start.intent.json") and not gate._present(root / "start.intent.json.pending"),
                  "sender start attempted; query exact candidate only")
    _postgres(plan)
    gate._write(root / "start.intent.json", created)
    _docker(["start", created["container_id"]], seconds=10)
    _verify(plan, inputs, _inspect(created["container_id"]), running=True)
    legacy.check_stopped()
    result = {**created, "status": "started_requires_actual135_and_closed_initialization"}
    gate._write(root / "started.json", result)
    return result


def _driver(legacy, plan, root, *, recovery=False):
    inputs = _load(legacy, plan, root, active=not recovery)
    created = durable.read_confirmed(root / "created.json")
    gate._require(created["plan_sha256"] == gate._hash(plan)
                  and durable.read_confirmed(root / "start.intent.json") == created, "original sender start differs")
    row = _inspect(created["container_id"])
    _verify(plan, inputs, row, running=True)
    identity = _postgres(plan, after=True)
    path = root / "driver-plan.json"
    binding = {"schema": 1, "scope": "instrumented_sender_only", "run_id": plan["operation_id"][:32], "control_id": "miniflux",
               "component_name": row["Name"].lstrip("/"), "component": _component(row), "manifest": plan["manifest"],
               "application_source_sha256": hashlib.sha256((ROOT / "ops/miniflux/pause-source-lock.json").read_bytes()).hexdigest(),
               "source_sha256": bridge_source(), "generation": 0, "initial_operation_id": plan["operation_id"],
               "entrances": ["miniflux_scheduler", "miniflux_manual", "legacy_async_sender"]}
    driver_plan = {"schema": 1, "scope": "instrumented_sender_only", "binding": binding,
                   "postgres_identity": identity, "window": plan["window"], "sources": sources()}
    if gate._present(path):
        gate._require(durable.read_confirmed(path) == driver_plan, "retained sender driver identity changed")
    else:
        gate._require(not recovery, "no retained sender binding; no bootstrap mutation in query")
        gate._write(path, driver_plan)
    transport = NativeSenderActivationTransport(plan["control_url"], plan["token_file"], container_id=row["Id"])
    return SenderActivationDriver(driver_plan, transport, PostgresReader(plan["dsn_file"], identity), root / "driver")


def initialize(legacy, plan, evidence_root):
    root = Path(evidence_root)
    driver = _driver(legacy, plan, root)
    receipt = driver.initialize_closed(plan["operation_id"])
    legacy.check_stopped()
    result = {"status": "sender_initialized_closed", "plan_sha256": gate._hash(plan), "pg_receipt": receipt,
              "driver_plan_sha256": driver.plan_sha256, "business_open": False, "historical_raw_coverage": "unknown_before_T0_no_replay"}
    gate._write(root / "closed.json", result)
    return result


def query(legacy, plan, evidence_root):
    root = Path(evidence_root)
    _load(legacy, plan, root, active=False)
    if not gate._present(root / "created.json"):
        return {"status": "create_uncertain_no_start", "mutation_replayed": False}
    if not gate._present(root / "start.intent.json"):
        return {"status": "created_unstarted", "mutation_replayed": False}
    if not gate._present(root / "driver/control/initialize_closed.intent.json"):
        row = _inspect(durable.read_confirmed(root / "created.json")["container_id"])
        return {"status": "initialization_not_confirmed", "running": row["State"]["Running"], "mutation_replayed": False}
    driver = _driver(legacy, plan, root, recovery=True)
    receipt = driver.query_initialize_closed(plan["operation_id"])
    legacy.check_stopped()
    return {"status": "sender_initialized_closed", "pg_receipt": receipt, "query_only": True, "business_open": False}


def _same_topology(legacy, plan, service):
    gate._require(legacy.plan["docker"]["path"] == "/usr/bin/docker", "bound native Docker binary required")
    old = _inspect(legacy.plan["containers"]["sender"]["id"])
    mounts = [{"type": item["Type"], "source": item["Source"], "target": item["Destination"], "read_only": not item["RW"]}
              for item in old["Mounts"]]
    gate._require(sorted(mounts, key=lambda x: x["target"]) == sorted(service["volumes"], key=lambda x: x["target"]),
                  "candidate sender cannot replace legacy business mounts")
    ports = sorted((target, p["HostIp"], p["HostPort"]) for target, rows in old["HostConfig"].get("PortBindings", {}).items() for p in rows or [])
    wanted = sorted((str(p["target"]) + "/" + p["protocol"], p["host_ip"], str(p["published"])) for p in service["ports"])
    gate._require(ports == wanted and set(old["NetworkSettings"]["Networks"]) == {plan["network"]["Name"]},
                  "candidate sender must retain original network/ports")


def _component(row):
    value = {"id": row["Id"], "image": row["Image"], "revision": row["Config"]["Labels"]["org.opencontainers.image.revision"],
            "started_at": row["State"]["StartedAt"], "restart_count": row["RestartCount"], "role": "miniflux",
            "user": row["Config"]["User"], "memory": row["HostConfig"]["Memory"], "pids": row["HostConfig"]["PidsLimit"],
            "mounts": [{key: item[key] for key in ("Type", "Source", "Destination", "RW")} for item in row["Mounts"]]}
    if row["Mounts"] == []:
        from services.sender_drain_contract import external_postgres_identity
        values = private.runtime_environment(row["Config"]["Env"])
        value["external_postgres"] = external_postgres_identity(values.get("DATABASE_URL"), row["HostConfig"].get("ReadonlyRootfs"))
    return value


def main(argv=None):
    import argparse
    from scripts.production_legacy_takeover import locked_takeover
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("action", choices=("create", "start", "initialize", "query"))
    parser.add_argument("--plan", required=True, type=Path)
    parser.add_argument("--legacy-plan", required=True, type=Path)
    parser.add_argument("--legacy-evidence", required=True, type=Path)
    parser.add_argument("--evidence", required=True, type=Path)
    args = parser.parse_args(argv)
    raw, _identity = private.private_file(args.plan, sealed=True)
    plan = gate._parsed(raw)
    with locked_takeover(args.legacy_plan, args.legacy_evidence) as legacy:
        result = {"create": create, "start": start, "initialize": initialize, "query": query}[args.action](legacy, plan, args.evidence)
    print(gate._encoded({"status": result["status"], "operation_id": plan["operation_id"]}).decode())
    return 0


def _mount_identities(plan, service):
    expected = plan["mount_identities"]
    gate._require(isinstance(expected, dict) and set(expected) == {m["source"] for m in service["volumes"]},
                  "exact existing sender mount identities required")
    for path, record in expected.items():
        info = private.safe_path(path).lstat()
        kind = "file" if stat.S_ISREG(info.st_mode) else "directory" if stat.S_ISDIR(info.st_mode) else "other"
        actual = {"device": info.st_dev, "inode": info.st_ino, "uid": info.st_uid, "mode": stat.S_IMODE(info.st_mode), "kind": kind}
        gate._require(kind != "other" and record == actual, "sender existing bind inode/owner/mode changed")


if __name__ == "__main__":
    raise SystemExit(main())
