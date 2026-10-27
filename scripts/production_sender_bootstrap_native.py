"""Root-only synthetic proof of the real sender bootstrap and legacy capability.

Creates only owned containers on a fresh internal network. The cron command is
an explicit private file fixture, never the system crontab. Root originals stay
private; stdout exports a bounded, hashed derivative of selected safe records.
"""
from __future__ import annotations

import argparse
import base64
import hashlib
import json
import os
from pathlib import Path
import re
import stat
import subprocess
import sys
import time

ROOT = Path(__file__).resolve().parents[1]
for directory in (ROOT, ROOT / "scripts"):
    if str(directory) not in sys.path:
        sys.path.insert(0, str(directory))

from services import admission_fence as gate  # noqa: E402
from scripts import production_sender_bootstrap as bootstrap  # noqa: E402
from scripts import production_legacy_takeover as legacy  # noqa: E402
from scripts import production_namespace_native as export  # noqa: E402
from scripts import production_receiver_bootstrap_native as receiver_native  # noqa: E402
from scripts.verify_miniflux_pause_native import Harness, source_binding, sha, image_checks, memory_evidence  # noqa: E402

SAFE_FILES = ("SOURCE.json", "SEED132.json", "LEGACY_STOP.json", "CREATED.json", "STARTED.json",
              "CLOSED.json", "QUERY.json", "PROOF.json", "RESULT.json", "FAILURE.json", "CANDIDATE_FINAL.json",
              "RECEIVER_OLD_STORAGE.json", "RECEIVER_BOOTSTRAP_PROOF.json", "RECEIVER_SQLITE_PRESERVED.json")
MAX_SECONDS = 180
OLD_PROCESS = ("import signal,sys,time; signal.signal(signal.SIGTERM,lambda *_:sys.exit(0)); "
               "print('synthetic_TERM_ready',flush=True); time.sleep(600)")
TERM_READY = ("from pathlib import Path; import signal; "
              "rows=dict(line.split(':',1) for line in Path('/proc/1/status').read_text().splitlines()); "
              "print(int(bool(int(rows['SigCgt'].strip(),16)&(1<<(signal.SIGTERM-1)))))")


def build_records(directory, image_id, revision):
    directory = bootstrap.private.safe_path(directory)
    gate._require(directory.is_dir() and directory.stat().st_uid == os.geteuid()
                  and stat.S_IMODE(directory.stat().st_mode) == 0o700, "private root-owned actual build evidence required")
    files, total = {}, 0
    for name in ("BUILD.log", "IMAGE_BUILD.log", "rawimage.json"):
        path = directory / name
        fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
        try:
            before = os.fstat(fd)
            gate._require(stat.S_ISREG(before.st_mode) and before.st_uid == os.geteuid() and before.st_nlink == 1
                          and stat.S_IMODE(before.st_mode) == 0o400 and 0 < before.st_size <= 1024**2,
                          "bounded sealed original build file required")
            total += before.st_size
            gate._require(total <= 2 * 1024**2, "actual build evidence total bound")
            raw = bytearray()
            while len(raw) <= before.st_size:
                block = os.read(fd, min(65536, before.st_size + 1 - len(raw)))
                if not block:
                    break
                raw.extend(block)
            gate._require(len(raw) == before.st_size and export._metadata(before) == export._metadata(os.fstat(fd))
                          and export._metadata(before) == export._metadata(path.lstat()), "actual build evidence changed")
        finally:
            os.close(fd)
        files[name] = {"sha256": hashlib.sha256(raw).hexdigest(), "bytes": len(raw),
                       "raw_base64": base64.b64encode(raw).decode()}
        if name == "BUILD.log":
            gate._require(b"go version go1.26.4 " in raw, "actual fixed compiler version absent from build log")
        if name == "rawimage.json":
            rows = json.loads(raw)
            gate._require(isinstance(rows, list) and len(rows) == 1 and rows[0]["Id"] == image_id
                          and rows[0]["Config"]["Labels"].get("org.opencontainers.image.revision") == revision,
                          "actual image build source/ID evidence differs")
    return {"schema": "synthetic-native-actual-build-records-v1", "revision": revision, "image_id": image_id, "files": files}


def _save(path, value, mode=0o600):
    raw = value if isinstance(value, bytes) else gate._encoded(value)
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, mode)
    with os.fdopen(fd, "wb") as stream:
        stream.write(raw)
        stream.flush()
        os.fsync(stream.fileno())
    gate._sync_directory(path.parent)
    return {"path": str(path), "sha256": hashlib.sha256(raw).hexdigest()}


def _record(path, value, mode=0o600):
    _save(path, value, mode)
    return legacy._record(path, executable=bool(mode & 0o111))[0]


def _deadline(harness):
    gate._require(time.monotonic() - harness.started < MAX_SECONDS, "bootstrap native total deadline")


def _sql132(repo, source):
    name = "internal/database/webhook_outbox.go"
    raw = (source / name).read_bytes()
    lock = json.loads((repo / "ops/miniflux/pause-source-lock.json").read_text())
    gate._require(hashlib.sha256(raw).hexdigest() == lock["patched_files_sha256"][name], "actual132 source hash differs")
    matches = re.findall(r"tx\.Exec\(`(.*?)`\)", raw.decode(), re.S)
    gate._require(len(matches) == 1, "one actual locked132 SQL required")
    return matches[0], hashlib.sha256(raw).hexdigest()


def _base_image(repo):
    matches = re.findall(r"^FROM (miniflux/miniflux@sha256:[0-9a-f]{64})$",
                         (repo / "ops/miniflux/Dockerfile").read_text(), re.M)
    gate._require(len(matches) == 1, "fixed upstream seed image required")
    return matches[0]


def seed132(harness):
    """Actual fixed upstream131 followed by the exact locked132 transaction."""
    reference = _base_image(harness.repo)
    image = json.loads(harness.command(["image", "inspect", reference]))[0]
    image_checks(image, reference)
    dsn = f"postgres://synthetic:{harness.password}@pg:5432/synthetic_pause?sslmode=disable"
    harness.launch("seed131", image["Id"], ["--read-only", "--cap-drop=ALL", "-e", "DATABASE_URL=" + dsn], ["-migrate"])
    code = harness.command(["wait", harness.names["seed131"]], timeout=30).strip()
    gate._require(code == "0" and harness.sql("SELECT version FROM schema_version;").strip() == "131",
                  "actual upstream synthetic schema131 required")
    sql, source_hash = _sql132(harness.repo, harness.source)
    harness.sql("BEGIN; SET LOCAL statement_timeout='3000ms'; " + sql
                + " UPDATE schema_version SET version='132' WHERE version='131'; COMMIT;")
    gate._require(harness.sql("SELECT version FROM schema_version;").strip() == "132", "actual132 transaction failed")
    _save(harness.output / "SEED132.json", {"upstream_image": image["Id"], "upstream_reference": reference,
        "seed_container_id": json.loads(harness.command(["inspect", harness.names["seed131"]]))[0]["Id"],
        "prior_schema": 131, "schema": 132, "actual_sql_source_sha256": source_hash,
        "sql_sha256": hashlib.sha256(sql.encode()).hexdigest(), "marker_and_sql_same_transaction": True,
        "synthetic_only": True, "production_compatibility_proven": False})


def cron_command(path):
    """A sealed file command with crontab argv, never an installed system writer."""
    return ("#!/usr/bin/python3\nimport os,sys\nfrom pathlib import Path\n"
            + "p=Path(" + repr(str(path)) + ")\n"
            + "assert sys.argv[1:3]==['-u','root'] and len(sys.argv)==4\n"
            + "if sys.argv[3]=='-l': sys.stdout.buffer.write(p.read_bytes())\n"
            + "else:\n data=Path(sys.argv[3]).read_bytes()\n assert len(data)<=262144\n"
            + " with p.open('wb') as f: f.write(data); f.flush(); os.fsync(f.fileno())\n"
            + " fd=os.open(p.parent,os.O_RDONLY|os.O_DIRECTORY); os.fsync(fd); os.close(fd)\n").encode()


def prepare_legacy(harness, directory, daemon, receiver_data):
    operation, now = gate._hash({"synthetic-legacy": harness.owner}), int(time.time())
    route = _record(directory / "gateway.conf", b"# owned synthetic original gateway\n")
    rows = {}
    for role in legacy.ROLES:
        extra = ["--read-only", "--cap-drop=ALL", "--user", "0:0"]
        if role == "gateway":
            extra += ["--mount", "type=bind,src=" + route["path"] + ",dst=/etc/nginx/conf.d/default.conf,readonly"]
        if role in {"api", "worker"}:
            extra += ["--mount", "type=bind,src=" + str(receiver_data) + ",dst=/app/data"]
        harness.launch("old-" + role, harness.python_id, extra, ["python", "-I", "-B", "-c", OLD_PROCESS])
        harness.wait(lambda: harness.command(["exec", harness.names["old-" + role], "python", "-I", "-c", TERM_READY]).strip() == "1",
                     seconds=3, readiness=True)
        rows[role] = json.loads(harness.command(["inspect", harness.names["old-" + role]]))[0]
    refresh = _record(directory / "refresh", b"#!/bin/sh\nexit 0\n", 0o700)
    before = ("# synthetic file-only cron\n* * * * * " + refresh["path"] + "\n").encode()
    lock = directory / "cron.lock"
    _save(lock, b"")
    binary = _record(directory / "crontab-fixture", cron_command(directory / "cron.current"), 0o700)
    _save(directory / "cron.current", before)
    flock = legacy._record(Path("/usr/bin/flock"), executable=True)[0]
    writer = _record(directory / "writer", legacy.writer_script(str(lock), binary["path"], flock["path"]), 0o700)
    line_sha = hashlib.sha256(before.splitlines(keepends=True)[1]).hexdigest()
    after = before.splitlines(keepends=True)[0] + ("# controlled-legacy-stop " + operation + " " + line_sha + "\n").encode()
    plan = {"schema": 1, "operation_id": operation, "source_sha256": legacy.source_sha256(),
        "host_boot_id": legacy.NativeTransport().host_boot(), "approved_by": "owned-synthetic-file-cron-only",
        "not_before": now - 1, "expires_at": now + 300,
        "docker": legacy._record(Path("/usr/bin/docker"), executable=True)[0], "wait_seconds": 5, "max_polls": 20,
        "containers": {role: legacy.container_identity(row) for role, row in rows.items()},
        "gateway": {"original": route, "container_id": rows["gateway"]["Id"], "config_target": "/etc/nginx/conf.d/default.conf",
                    "candidate_service": "synthetic-api", "candidate_image_id": harness.python_id,
                    "private_contract_sha256": gate._hash({"synthetic": harness.owner})},
        "cron": {"lock": legacy._lock_identity(lock), "binary": binary, "flock": flock, "writer": writer,
                 "before": _record(directory / "cron.original", before), "after_sha256": hashlib.sha256(after).hexdigest(),
                 "entries": [{"line": 1, "sha256": line_sha, "executable": refresh}], "daemon": legacy._process(daemon.pid)}}
    inventory = {"schema": 1, "scope": "registered-root-cron-writers", "host_boot_id": plan["host_boot_id"],
        "lock": plan["cron"]["lock"], "writer": writer, "adapter_source_sha256": plan["source_sha256"],
        "registered_writers": [writer["path"], str(Path(legacy.__file__).resolve())],
        "external_root_mutation": "forbidden-during-reviewed-window", "expires_at": plan["expires_at"],
        "reviewed_by": "owned-synthetic-file-cron-only"}
    plan["cron"]["writer_inventory"] = _record(directory / "writers.json", inventory)
    path = directory / "legacy-plan.json"
    _save(path, plan)
    return path


def postgres_before(dsn):
    import psycopg
    with psycopg.connect(dsn, autocommit=True, connect_timeout=3) as connection:
        connection.execute("SET statement_timeout='3000ms'")
        connection.execute("BEGIN READ ONLY")
        try:
            identity = dict(zip(bootstrap.PG_KEYS, connection.execute(bootstrap.PG_SQL).fetchone()))
            rows = connection.execute("SELECT version FROM schema_version LIMIT 2").fetchall()
            gate._require(rows == [("132",)], "synthetic predecessor must be actual132")
            return {**identity, "schema_version": 132}
        finally:
            connection.execute("ROLLBACK")


def prepare_bootstrap(harness, directory, stopped, binding, records):
    operation = gate._hash({"synthetic-bootstrap": harness.owner})
    service, project = "sender_" + operation[:16], "bootstrap_" + harness.owner[:16]
    network = json.loads(harness.command(["network", "inspect", harness.network]))[0]
    pg = json.loads(harness.command(["inspect", harness.names["pg"]]))[0]
    address = pg["NetworkSettings"]["Networks"][harness.network]["IPAddress"]
    dsn = f"postgres://synthetic:{harness.password}@{address}:5432/synthetic_pause?sslmode=disable"
    _save(directory / "dsn.json", {"dsn": dsn})
    _save(directory / "token.json", {"token": harness.token})
    manifest = gate._hash({"synthetic-owner": harness.owner, "image": harness.image_id})
    env = {"DATABASE_URL": dsn, "RUN_MIGRATIONS": "1", "CREATE_ADMIN": "0", "HANDOFF_ENABLED": "1",
        "WEBHOOK_OUTBOX": "1", "HANDOFF_MANIFEST": manifest, "WEBHOOK_OUTBOX_CONTRACT": "synthetic-bootstrap-no-model",
        "HANDOFF_CONTROL_TOKEN": harness.token, "HANDOFF_CONTROL_ADDR": "127.0.0.1:8787",
        "LISTEN_ADDR": "0.0.0.0:8080", "WORKER_POOL_SIZE": "1", "POLLING_FREQUENCY": "60"}
    spec = {"image": harness.image_id, "environment": {key: "${" + key + "}" for key in env}, "user": "65534:65534",
        "mem_limit": 768 * 1024**2, "memswap_limit": 768 * 1024**2, "pids_limit": 256, "cpus": 1,
        "cap_drop": ["ALL"], "security_opt": ["no-new-privileges:true"], "restart": "no", "read_only": True,
        "volumes": [], "ports": [], "networks": ["sender"], "platform": "linux/amd64", "healthcheck": {"disable": True},
        "labels": {"org.opencontainers.image.revision": binding["revision"], "io.ai-filter.handoff.role": "miniflux",
                   "io.ai-filter.sender.bootstrap": operation}}
    build_evidence = _save(directory / "build-evidence.json", records, 0o400)
    build = {"schema": "reviewed-miniflux-offline-build-v1", "revision": binding["revision"],
        "upstream_revision": binding["upstream_revision"], "pause_lock_sha256": sha(harness.repo / "ops/miniflux/pause-source-lock.json"),
        "dockerfile_sha256": sha(harness.repo / "ops/miniflux/Dockerfile"), "binary_sha256": binding["binary_sha256"],
        "image_id": harness.image_id, "go_version": "go1.26.4", "build_evidence_sha256": build_evidence["sha256"]}
    now = int(time.time())
    return {"schema": bootstrap.SCHEMA, "operation_id": operation, "source_sha256": bootstrap.source_sha256(),
        "revision": binding["revision"], "project": project, "service": service,
        "compose": _save(directory / "compose.json", {"services": {service: spec},
                    "networks": {"sender": {"name": harness.network, "external": True}}}, 0o400),
        "environment": _save(directory / "environment", "".join(key + "=" + value + "\n" for key, value in env.items()).encode(), 0o400),
        "build": _save(directory / "build.json", build, 0o400), "build_evidence": build_evidence, "mount_identities": {},
        "legacy_operation_id": stopped["operation_id"], "legacy_plan_sha256": stopped["plan_sha256"],
        "network": {key: network[key] for key in ("Name", "Id", "Driver", "Scope", "Internal", "Options", "IPAM")},
        "postgres_before": postgres_before(dsn), "dsn_file": str(directory / "dsn.json"), "token_file": str(directory / "token.json"),
        "control_url": "http://127.0.0.1:8787", "manifest": manifest, "contract": env["WEBHOOK_OUTBOX_CONTRACT"],
        "window": {"not_before": now - 1, "expires_at": now + 120}}


def _ready(plan, created):
    identity = bootstrap._postgres(plan, after=True)
    transport = bootstrap.NativeSenderActivationTransport(plan["control_url"], plan["token_file"], container_id=created["container_id"])
    status, value = transport.request("GET", "/activation-registration")
    gate._require(status == 200 and value["database_schema"] == 135, "actual schema135 control not ready")
    return identity


def rejected_original(function):
    try:
        function()
    except (ValueError, gate.AdmissionError):
        return True
    raise AssertionError("a repeated original action was accepted")


def closed_http(harness, actual):
    address = actual["NetworkSettings"]["Networks"][harness.network]["IPAddress"]
    command = ("import urllib.request,urllib.error;\ntry: urllib.request.urlopen(urllib.request.Request('http://"
               + address + ":8080/login',data=b'username=bad&password=bad',method='POST'),timeout=3)\n"
               + "except urllib.error.HTTPError as e: print(e.code)")
    status = harness.command(["exec", harness.names["fixture"], "python", "-I", "-c", command])
    gate._require(status.strip() == "503", "login business request did not remain closed")
    gate._require(harness.sql("SELECT count(*) FROM handoff_leases;").strip() == "0",
                  "closed login unexpectedly retained a business admission lease")
    return {"method": "POST", "path": "/login", "status": 503, "business_leases_after": 0,
            "credentials": "deliberately_invalid_synthetic_no_user_created"}


def exercise(harness, binding, daemon, records):
    with receiver_native.prepared(harness.output) as receiver:
        with receiver_native.receiver.retained_release_lock(receiver["release_lock"]) as release:
            return exercise_locked(harness, binding, daemon, records, receiver, release)


def exercise_locked(harness, binding, daemon, records, receiver, release):
    root = harness.output
    directory, evidence = root / "inputs", root / "bootstrap"
    directory.mkdir(mode=0o700)
    evidence.mkdir(mode=0o700)
    old = root / "legacy"
    old.mkdir(mode=0o700)
    path = prepare_legacy(harness, directory, daemon, receiver["data"])
    with legacy.locked_takeover(path, old) as cap:
        stopped = cap.stop()
        _save(root / "LEGACY_STOP.json", stopped)
        plan = prepare_bootstrap(harness, directory, stopped, binding, records)
        _save(directory / "bootstrap-plan.json", plan, 0o400)
        _deadline(harness)
        created = bootstrap.create(cap, plan, evidence)
        _save(root / "CREATED.json", created)
        rejected_original(lambda: bootstrap.create(cap, plan, evidence))
        started = bootstrap.start(cap, plan, evidence)
        _save(root / "STARTED.json", started)
        rejected_original(lambda: bootstrap.start(cap, plan, evidence))
        identity = harness.wait(lambda: _ready(plan, created), seconds=15, readiness=True)
        closed = bootstrap.initialize(cap, plan, evidence)
        _save(root / "CLOSED.json", closed)
        rejected_original(lambda: bootstrap.initialize(cap, plan, evidence))
        queried = bootstrap.query(cap, plan, evidence)
        _save(root / "QUERY.json", queried)
        gate._require(closed["pg_receipt"] == queried["pg_receipt"] and closed["pg_receipt"]["state"] == "closed"
                      and closed["pg_receipt"]["generation"] == 0, "exact first CLOSED query differs")
        actual = bootstrap._inspect(created["container_id"])
        gate._require(actual["Mounts"] == [], "external PG sender unexpectedly gained a dummy mount")
        denied = closed_http(harness, actual)
        _deadline(harness)
        driver = bootstrap._driver(cap, plan, evidence, recovery=True)
        receiver_proof = receiver_native.exercise(root, receiver, cap, release, driver, binding["revision"])
        _deadline(harness)
        proof = {"status": "passed", "synthetic_only": True, "production_access": False, "real_models": 0,
            "container_id": actual["Id"], "image_id": actual["Image"], "actual_mounts": [], "schema_before": 132,
            "schema_after": identity["schema_version"], "postgres_identity": identity, "closed_receipt": closed["pg_receipt"],
            "business_http_status": 503, "business_request": denied, "open_actions": 0, "namespace_transport": True,
            "duplicate_create_start_initialize_rejected": True,
            "receiver_metadata_bootstrap": {"operation_id": receiver_proof["result"]["operation_id"],
                                            "proof_sha256": gate._hash(receiver_proof)},
            "old_retained": cap.check_stopped(), "cron_scope": "synthetic_file_command_not_system_cron",
            "historical_raw_coverage": "unknown_before_T0_no_replay", "binary": created["binary"],
            "build_original_sha256": {name: value["sha256"] for name, value in records["files"].items()},
            "memory": actual["HostConfig"]["Memory"], "memory_swap": actual["HostConfig"]["MemorySwap"],
            "pids": actual["HostConfig"]["PidsLimit"], "nano_cpus": actual["HostConfig"]["NanoCpus"]}
        _save(root / "PROOF.json", proof)
        return proof


def preserve_candidate(harness):
    """Only the actual retained candidate ID, TERM only, no remove or retry."""
    path = harness.output / "bootstrap/created.json"
    if not path.exists():
        return []
    errors = []
    try:
        created = gate._read(path)
        row = bootstrap._inspect(created["container_id"])
        plan = json.loads(bootstrap.private.private_file(harness.output / "inputs/bootstrap-plan.json", sealed=True)[0])
        gate._require(row["Config"]["Labels"].get("io.ai-filter.sender.bootstrap") == plan["operation_id"], "candidate ownership changed")
        try:
            harness.command(["logs", row["Id"]], timeout=3, final=True)
        except BaseException as error:
            errors.append({"stage": "candidate_logs", "type": type(error).__name__})
        if row["State"]["Running"]:
            values = {}
            try:
                values = {name: bootstrap._docker(["exec", row["Id"], "cat", "/sys/fs/cgroup/" + name]).decode()
                          for name in ("memory.peak", "memory.events")}
                checked = memory_evidence(values["memory.peak"], values["memory.events"])
                gate._require(not any(checked["events"].get(key, 0) for key in ("oom", "oom_kill", "oom_group_kill")),
                              "candidate cgroup OOM observed")
            except BaseException as error:
                errors.append({"stage": "candidate_cgroup", "type": type(error).__name__})
            bootstrap._docker(["kill", "--signal", "TERM", row["Id"]])
            gate._require(bootstrap._docker(["wait", row["Id"]], seconds=8).strip() in {b"0", b"143"}, "candidate TERM unconfirmed")
        else:
            values = {}
        _save(harness.output / "CANDIDATE_FINAL.json", {"inspect": harness.safe_inspect(bootstrap._inspect(row["Id"])), "cgroup": values})
    except BaseException as error:
        errors.append({"stage": "candidate_preserve", "type": type(error).__name__, "sha256": hashlib.sha256(str(error).encode()).hexdigest()})
    return errors


def preserve_owned(harness, role):
    errors = []
    row = json.loads(harness.command(["inspect", harness.names[role]], final=True))[0]
    gate._require(row["Config"]["Labels"].get("ai-filter.synthetic-miniflux-pause") == harness.owner,
                  "retained synthetic ownership changed")
    _save(harness.output / ("RETAINED_" + role + ".json"), harness.safe_inspect(row))
    try:
        harness.command(["logs", row["Id"]], timeout=3, final=True)
        if row["State"]["Running"]:
            harness.sample_memory(role, "final")
    except BaseException as error:
        errors.append({"stage": role + ":observation", "type": type(error).__name__})
    if row["State"]["Running"]:
        harness.command(["kill", "--signal", "TERM", row["Id"]], final=True)
        harness.command(["wait", row["Id"]], timeout=8, final=True)
    return errors


def execute(repo, source, image_id, binary, output, build_evidence):
    gate._require(sys.platform == "linux" and os.geteuid() == 0, "native bootstrap requires Linux root")
    gate._require(repo == ROOT and output.is_absolute() and not output.exists(), "fixed source and fresh synthetic output required")
    bootstrap.private.safe_path(output.parent)
    gate._require(output.parent.stat().st_uid == 0 and stat.S_IMODE(output.parent.stat().st_mode) == 0o700,
                  "new native originals must stay inside a private root-owned parent")
    binding = source_binding(repo, source, binary)
    records = build_records(build_evidence, image_id, binding["revision"])
    harness = Harness(repo, source, image_id, binary, output)
    harness.phase_deadline = harness.started + MAX_SECONDS
    _save(output / "SOURCE.json", binding)
    daemon, failure, errors = None, None, []
    try:
        harness.prepare()
        seed132(harness)
        daemon = subprocess.Popen([sys.executable, "-I", "-B", "-c", "import time; time.sleep(600)"],
                                  stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        exercise(harness, binding, daemon, records)
    except BaseException as error:
        import traceback
        failure = {"type": type(error).__name__, "message_sha256": hashlib.sha256(str(error).encode()).hexdigest(),
            "locations": [{"file": Path(row.filename).name, "function": row.name, "line": row.lineno}
                          for row in traceback.extract_tb(error.__traceback__) if Path(row.filename).resolve().is_relative_to(repo)]}
        _save(output / "FAILURE.json", failure)
    finally:
        errors.extend(preserve_candidate(harness))
        if "pg" in harness.names:
            try:
                harness.command(["exec", harness.names["pg"], "pg_dump", "-U", "synthetic", "-d", "synthetic_pause"],
                                timeout=15, final=True)
            except BaseException as error:
                errors.append({"stage": "postgres_dump", "type": type(error).__name__})
        # Preserve owned synthetic containers; no KILL fallback or deletion.
        for role in reversed(harness.created):
            try:
                errors.extend(preserve_owned(harness, role))
            except BaseException as error:
                errors.append({"stage": role, "type": type(error).__name__})
        if daemon is not None:
            daemon.terminate()
            try:
                daemon.wait(timeout=3)
            except subprocess.TimeoutExpired:
                errors.append({"stage": "owned_daemon", "type": "TERM_unconfirmed"})
    result = {"status": "passed" if failure is None and not errors else "unconfirmed", "source_sha": binding["revision"],
        "failure": failure, "errors": errors, "elapsed_seconds": time.monotonic() - harness.started,
        "production_access": False, "real_models": 0, "system_cron_access": False, "raw_root": str(output),
        "limits": {"application_memory": 768 * 1024**2, "application_pids": 256,
                   "postgres_memory": 384 * 1024**2, "postgres_pids": 64, "operation_seconds": MAX_SECONDS}}
    _save(output / "RESULT.json", result)
    return result


def safe_export(root):
    """Reuse strict namespace nofollow/inode/owner readers for safe records only."""
    fd, budget, packet = export._evidence_root_fd(root), [0], {"files": {}, "complete": True, "originals_modified": False}
    try:
        before = export._metadata(os.fstat(fd))
        for name in SAFE_FILES:
            try:
                record, raw = export._export_file(fd, name, budget)
            except FileNotFoundError:
                continue
            record["content"] = json.loads(raw)
            packet["files"][name] = record
        gate._require(export._metadata(os.fstat(fd)) == before, "root originals changed during safe export")
        gate._require("RESULT.json" in packet["files"] and len(gate._encoded(packet)) <= 256 * 1024,
                      "bounded complete result export required")
        return packet
    finally:
        os.close(fd)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    for name in ("repo", "source", "binary", "output", "build-evidence"):
        parser.add_argument("--" + name, required=True, type=Path)
    parser.add_argument("--image-id", required=True)
    args = parser.parse_args()
    try:
        result = execute(args.repo.resolve(), args.source.resolve(), args.image_id, args.binary.resolve(),
                         args.output.resolve(), args.build_evidence.resolve())
        packet = {"status": result["status"], "safe_evidence": safe_export(args.output.resolve())}
        print(gate._encoded(packet).decode(), flush=True)
        return 0 if packet["status"] == "passed" else 3
    except BaseException as error:
        print(json.dumps({"status": "unconfirmed", "error_type": type(error).__name__,
                          "error_sha256": hashlib.sha256(str(error).encode()).hexdigest(), "safe_export_complete": False}), flush=True)
        return 3


if __name__ == "__main__":
    raise SystemExit(main())
