"""Root-only synthetic proof of the real sender bootstrap and legacy capability.

Creates only owned containers on a fresh internal network. The cron command is
an explicit private file fixture, never the system crontab. Root originals stay
private; stdout exports a bounded, hashed derivative of selected safe records.
"""
from __future__ import annotations

import argparse
import base64
from contextlib import nullcontext
from datetime import datetime
import hashlib
import json
import math
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
from durable_handoff_cutover import CutoverError  # noqa: E402
from scripts import production_sender_bootstrap as bootstrap  # noqa: E402
from scripts import production_legacy_takeover as legacy  # noqa: E402
from scripts import production_namespace_native as export  # noqa: E402
from scripts import production_receiver_bootstrap_native as receiver_native  # noqa: E402
from scripts.verify_miniflux_pause_native import (  # noqa: E402
    Harness, source_binding, sha, image_checks, memory_evidence, resource_checks, LABEL,
)
from scripts import sender_host_loopback_pg as local_pg  # noqa: E402

SAFE_FILES = ("SOURCE.json", "SEED131.json", "SEED132.json", "DIRECT131_RESTART.json", "DIRECT131_OLD_LIMIT.json",
              "DIRECT131_OLD_PROBE.json",
              "HOST_LOOPBACK.json", "LOOPBACK_PG_CHECKS.json",
              "LEGACY_STOP.json", "CREATED.json", "STARTED.json",
              "CLOSED.json", "QUERY.json", "PROOF.json", "RESULT.json", "FAILURE.json", "CANDIDATE_FINAL.json",
              "RECEIVER_OLD_STORAGE.json", "RECEIVER_BOOTSTRAP_PROOF.json", "RECEIVER_SQLITE_PRESERVED.json")
MAX_SECONDS = 180
DUPLICATE_INITIALIZE_MESSAGE = "sender gate already initialized or retained control history exists"
OLD_PROCESS = ("import signal,sys,time; signal.signal(signal.SIGTERM,lambda *_:sys.exit(0)); "
               "print('synthetic_TERM_ready',flush=True); time.sleep(600)")
TERM_READY = ("from pathlib import Path; import signal; "
              "rows=dict(line.split(':',1) for line in Path('/proc/1/status').read_text().splitlines()); "
              "print(int(bool(int(rows['SigCgt'].strip(),16)&(1<<(signal.SIGTERM-1)))))")


class HostLoopbackHarness(Harness):
    """Only an explicitly selected owned daemon-host namespace fixture.

    PG keeps its private bridge and exact 127.0.0.1:5432 publication. Only the
    old sender fixture uses host networking; other old roles remain unchanged.
    No conflict recovery, alternate port, alias on host or wider bind exists.
    """
    host_loopback = True

    def __init__(self, *args, private_host_context=None, **kwargs):
        super().__init__(*args, **kwargs)
        self.private_host_context = private_host_context
        if private_host_context is not None:
            from scripts.production_sender_bootstrap_host_native import native_host_identity
            native_host_identity(private_host_context)
            self.started = private_host_context.plan["started"]

    def command(self, *args, **kwargs):
        context = getattr(self, "private_host_context", None)
        if context is None:
            return super().command(*args, **kwargs)
        context.check(final=kwargs.get("final", False))
        end = context.plan["deadline"] if kwargs.get("final", False) else context.plan["active_deadline"]
        from scripts.production_activation_host_native import remaining
        kwargs["timeout"] = min(kwargs.get("timeout", 30), remaining(end))
        try:
            value = super().command(*args, **kwargs)
            context.check(final=kwargs.get("final", False))
            return value
        except BaseException:
            context.commands.uncertain_mutation = True
            raise

    def prepare_network(self):
        context = self.private_host_context
        if context is None:
            return super().prepare_network()
        row = context.prepare_network(self)
        _save(self.output / "NETWORK.json", row)

    def launch(self, name, image_id, extra=(), command=(), *, memory=768 * 1024**2, pids=256):
        if name not in {"pg", "old-sender"}:
            return super().launch(name, image_id, extra, command, memory=memory, pids=pids)
        actual = self.prefix + "-" + name
        self.names[name], self.images[name] = actual, image_id
        self.created.append(name)
        network = "host" if name == "old-sender" else self.network
        topology = ["--network", network]
        if name == "pg":
            topology += ["--network-alias", name, "--publish", "127.0.0.1:5432:5432",
                "--health-cmd", "pg_isready -U synthetic -d synthetic_pause",
                "--health-interval", "1s", "--health-timeout", "1s", "--health-retries", "5"]
        self.command(["run", "-d", "--pull=never", "--platform=linux/amd64", "--name", actual,
            "--label", LABEL + "=" + self.owner, *topology,
            "--memory", str(memory), "--memory-swap", str(memory), "--pids-limit", str(pids),
            "--cpus=1", "--security-opt=no-new-privileges", *extra, image_id, *command])
        row = json.loads(self.command(["inspect", actual]))[0]
        if name == "pg":
            checks = loopback_pg_resources(row, image_id, self.owner, self.network, memory, pids,
                                           diagnostic_root=self.output)
            if getattr(self, "private_host_context", None) is not None:
                self.pg_launch_identity = self._pg_readiness_identity(row)
        else:
            checks = resource_checks(row, image_id, self.owner, "host", memory, pids)
            gate._require(row["NetworkSettings"]["Networks"].keys() == {"host"}, "old sender is not exact host network")
        _save(self.output / ("PROFILE_" + name + ".json"), {"checks": checks, "inspect": self.safe_inspect(row)})
        return row

    def postgres_ready(self):
        if getattr(self, "private_host_context", None) is not None:
            from scripts.verify_production_activation_native import ActivationHarness
            if not ActivationHarness.postgres_ready(self):
                return False
            row = json.loads(self.command(["inspect", self.names["pg"]]))[0]
            health = row["State"].get("Health", {}).get("Status")
            gate._require(health in {"starting", "healthy"}, "real PG health failed or unrecognized")
            return health == "healthy"
        ready = super().postgres_ready()
        row = json.loads(self.command(["inspect", self.names["pg"]]))[0]
        gate._require(row["State"].get("Health", {}).get("Status") == "healthy", "real PG health not ready")
        return ready

    def _pg_readiness_identity(self, row):
        loopback_pg_resources(row, self.images["pg"], self.owner, self.network, 384 * 1024**2, 64)
        state = row["State"]
        gate._require(re.fullmatch(r"[0-9a-f]{64}", row["Id"]) and row["Name"] == "/" + self.names["pg"]
                      and state["Running"] is True and state["Dead"] is False and state["Restarting"] is False
                      and type(state["Pid"]) is int and state["Pid"] > 0
                      and type(row["RestartCount"]) is int and row["RestartCount"] >= 0
                      and type(state["StartedAt"]) is str and 0 < len(state["StartedAt"]) <= 64
                      and not state["StartedAt"].startswith("0001-")
                      and type(row["Created"]) is str and 0 < len(row["Created"]) <= 64,
                      "owned running loopback PostgreSQL identity required")
        return {key: row[key] for key in ("Id", "Name", "Image", "Created", "RestartCount")} | {
            "Pid": state["Pid"], "StartedAt": state["StartedAt"]}

    def _preserve_command_failure(self, observation):
        from scripts.verify_production_activation_native import ActivationHarness
        return ActivationHarness._preserve_command_failure(self, observation)


def loopback_pg_resources(row, image_id, owner, network, memory, pids, *, diagnostic_root=None):
    """An exact alternative topology guard, never normalize an inspect to pass."""
    host = row["HostConfig"]
    ports = {"5432/tcp": [{"HostIp": "127.0.0.1", "HostPort": "5432"}]}
    checks = {"actual_fixed_image": row["Image"] == image_id,
        "owned_label": row["Config"]["Labels"].get(LABEL) == owner,
        "private_bridge": host["NetworkMode"] == network and row["NetworkSettings"]["Networks"].keys() == {network},
        "exact_loopback_port": host.get("PortBindings") == row["NetworkSettings"].get("Ports") == ports,
        "memory": memory == 384 * 1024**2 and host["Memory"] == host["MemorySwap"] == memory,
        "pids": pids == 64 and host["PidsLimit"] == pids, "cpu": host["NanoCpus"] == 1_000_000_000,
        "not_privileged": host["Privileged"] is False,
        "no_new_privileges": any(x in {"no-new-privileges", "no-new-privileges:true"} for x in host.get("SecurityOpt", [])),
        "not_oom": row["State"]["OOMKilled"] is False,
        "real_healthcheck": row["Config"].get("Healthcheck") == {"Test": ["CMD-SHELL", "pg_isready -U synthetic -d synthetic_pause"],
            "Interval": 1_000_000_000, "Timeout": 1_000_000_000, "Retries": 5}}
    if diagnostic_root is not None:
        # Preserve only fixed boolean predicates before the original refusal.
        # A rejected actual inspect must never be copied to a public diagnostic.
        _save(Path(diagnostic_root) / "LOOPBACK_PG_CHECKS.json", {
            "schema": 1, "scope": "loopback_pg_resource_predicates_only", "checks": checks,
            "port_diagnostics": _loopback_port_diagnostics(row, ports),
            "passed": all(checks.values()), "execution_proof": False})
    gate._require(all(checks.values()), "exact loopback PG resource/topology binding differs")
    return checks


def _loopback_port_diagnostics(row, expected):
    """Classify this same inspect without exporting values or adding commands.

    These predicates explain a refusal. They neither normalize the actual
    mappings nor replace the exact resource/topology guard above.
    """
    requested = row["HostConfig"].get("PortBindings")
    runtime = row["NetworkSettings"].get("Ports")
    requested_mapping, runtime_mapping = type(requested) is dict, type(runtime) is dict
    return {
        "requested_expected": requested_mapping and requested == expected,
        "runtime_expected": runtime_mapping and runtime == expected,
        "requested_runtime_equal": requested == runtime,
        "requested_is_null": requested is None,
        "runtime_is_null": runtime is None,
        "requested_empty_mapping": requested_mapping and not requested,
        "runtime_empty_mapping": runtime_mapping and not runtime,
        "runtime_only_pg_null": runtime_mapping and set(runtime) == {"5432/tcp"} and runtime["5432/tcp"] is None,
        "requested_has_extra_ports": requested_mapping and bool(set(requested) - {"5432/tcp"}),
        "runtime_has_extra_ports": runtime_mapping and bool(set(runtime) - {"5432/tcp"}),
        "requested_invalid_type": requested is not None and not requested_mapping,
        "runtime_invalid_type": runtime is not None and not runtime_mapping,
        "pg_running": row["State"].get("Running") is True,
        "pg_created": row["State"].get("Status") == "created",
    }


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


def seed131(harness):
    """Run only the real fixed upstream binary migration; never synthesize 131."""
    reference = _base_image(harness.repo)
    # BuildKit FROM resolution does not prove this Engine reference exists.
    # One explicit preparation stays inside command's original shared deadline.
    harness.command(["pull", "--platform=linux/amd64", reference])
    image = json.loads(harness.command(["image", "inspect", reference]))[0]
    image_checks(image, reference)
    config = image.get("Config")
    gate._require(isinstance(config, dict) and "Entrypoint" in config and config["Entrypoint"] is None
                  and config.get("Cmd") == ["/usr/bin/miniflux"], "fixed upstream executable configuration differs")
    dsn = f"postgres://synthetic:{harness.password}@pg:5432/synthetic_pause?sslmode=disable"
    harness.launch("seed131", image["Id"], ["--read-only", "--cap-drop=ALL", "-e", "DATABASE_URL=" + dsn],
                   ["/usr/bin/miniflux", "-migrate"])
    code = harness.command(["wait", harness.names["seed131"]], timeout=30).strip()
    gate._require(code == "0" and harness.sql("SELECT version FROM schema_version;").strip() == "131",
                  "actual upstream synthetic schema131 required")
    record = {"upstream_image": image["Id"], "upstream_reference": reference,
        "upstream_entrypoint": config["Entrypoint"], "upstream_cmd": config["Cmd"],
        "seed_container_id": json.loads(harness.command(["inspect", harness.names["seed131"]]))[0]["Id"],
        "schema": 131, "marker_rewritten": False, "synthetic_only": True,
        "production_compatibility_proven": False, "historical_raw_coverage": "unknown_before_T0_no_replay"}
    _save(harness.output / "SEED131.json", record)
    return record


def seed132(harness):
    """Actual fixed upstream131 followed by the exact locked132 transaction."""
    original = seed131(harness)
    sql, source_hash = _sql132(harness.repo, harness.source)
    harness.sql("BEGIN; SET LOCAL statement_timeout='3000ms'; " + sql
                + " UPDATE schema_version SET version='132' WHERE version='131'; COMMIT;")
    gate._require(harness.sql("SELECT version FROM schema_version;").strip() == "132", "actual132 transaction failed")
    _save(harness.output / "SEED132.json", {**original,
        "prior_schema": 131, "schema": 132, "marker_rewritten": True, "actual_sql_source_sha256": source_hash,
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


def postgres_before(dsn, *, expected_schema=132, postgres_transport=None):
    gate._require(type(expected_schema) is int and expected_schema in (131, 132), "explicit synthetic predecessor required")
    import psycopg
    deadline = time.monotonic() + 10
    transport = local_pg.HostLoopbackPostgres(postgres_transport) if postgres_transport is not None else None
    if transport is not None:
        transport.check(dsn, deadline=deadline)
    with psycopg.connect(dsn, autocommit=True, connect_timeout=3) as connection:
        connection.execute("SET statement_timeout='3000ms'")
        connection.execute("BEGIN READ ONLY")
        try:
            identity = dict(zip(bootstrap.PG_KEYS, connection.execute(bootstrap.PG_SQL).fetchone()))
            rows = connection.execute("SELECT version FROM schema_version LIMIT 2").fetchall()
            gate._require(rows == [(str(expected_schema),)], "synthetic predecessor must match actual fixed seed")
            if transport is not None:
                with connection.cursor() as cursor:
                    transport.check_connection(cursor, identity)
                transport.check(dsn, deadline=deadline)
            return {**identity, "schema_version": expected_schema}
        finally:
            connection.execute("ROLLBACK")


def prepare_bootstrap(harness, directory, stopped, binding, records):
    operation = gate._hash({"synthetic-bootstrap": harness.owner})
    service, project = "sender_" + operation[:16], "bootstrap_" + harness.owner[:16]
    host_loopback = getattr(harness, "host_loopback", False)
    network = json.loads(harness.command(["network", "inspect", "host" if host_loopback else harness.network]))[0]
    pg = json.loads(harness.command(["inspect", harness.names["pg"]]))[0]
    address = pg["NetworkSettings"]["Networks"][harness.network]["IPAddress"]
    dsn_host = "127.0.0.1" if host_loopback else address
    dsn = f"postgres://synthetic:{harness.password}@{dsn_host}:5432/synthetic_pause?sslmode=disable"
    _save(directory / "dsn.json", {"dsn": dsn})
    _save(directory / "token.json", {"token": harness.token})
    manifest = gate._hash({"synthetic-owner": harness.owner, "image": harness.image_id})
    env = {"DATABASE_URL": dsn, "RUN_MIGRATIONS": "1", "CREATE_ADMIN": "0", "HANDOFF_ENABLED": "1",
        "WEBHOOK_OUTBOX": "1", "HANDOFF_MANIFEST": manifest, "WEBHOOK_OUTBOX_CONTRACT": "synthetic-bootstrap-no-model",
        "HANDOFF_CONTROL_TOKEN": harness.token, "HANDOFF_CONTROL_ADDR": "127.0.0.1:8787",
        "LISTEN_ADDR": "127.0.0.1:8080" if host_loopback else "0.0.0.0:8080", "WORKER_POOL_SIZE": "1", "POLLING_FREQUENCY": "60"}
    spec = {"image": harness.image_id, "environment": {key: "${" + key + ":?}" for key in env}, "user": "65534:65534",
        "mem_limit": 768 * 1024**2, "memswap_limit": 768 * 1024**2, "pids_limit": 256, "cpus": 1,
        "cap_drop": ["ALL"], "security_opt": ["no-new-privileges:true"], "restart": "no", "read_only": True,
        "volumes": [], "ports": [], "networks": ["sender"], "platform": "linux/amd64", "healthcheck": {"disable": True},
        "labels": {"org.opencontainers.image.revision": binding["revision"], "io.ai-filter.handoff.role": "miniflux",
                   "io.ai-filter.sender.bootstrap": operation}}
    if host_loopback:
        spec.pop("networks")
        spec["network_mode"] = "host"
        capability = local_pg.observe(dsn, pg["Id"])
        before = postgres_before(dsn, expected_schema=131, postgres_transport=capability)
    else:
        before = (postgres_before(dsn, expected_schema=131) if getattr(harness, "predecessor_schema", 132) == 131
                  else postgres_before(dsn))
    build_evidence = _save(directory / "build-evidence.json", records, 0o400)
    build = {"schema": "reviewed-miniflux-offline-build-v1", "revision": binding["revision"],
        "upstream_revision": binding["upstream_revision"], "pause_lock_sha256": sha(harness.repo / "ops/miniflux/pause-source-lock.json"),
        "dockerfile_sha256": sha(harness.repo / "ops/miniflux/Dockerfile"), "binary_sha256": binding["binary_sha256"],
        "image_id": harness.image_id, "go_version": "go1.26.4", "build_evidence_sha256": build_evidence["sha256"]}
    now = int(time.time())
    plan = {"schema": bootstrap.SCHEMA, "operation_id": operation, "source_sha256": bootstrap.source_sha256(),
        "revision": binding["revision"], "project": project, "service": service,
        "compose": _save(directory / "compose.json", {"services": {service: spec},
                    "networks": {} if host_loopback else {"sender": {"name": harness.network, "external": True}}}, 0o400),
        "environment": _save(directory / "environment", "".join(key + "=" + value + "\n" for key, value in env.items()).encode(), 0o400),
        "build": _save(directory / "build.json", build, 0o400), "build_evidence": build_evidence, "mount_identities": {},
        "legacy_operation_id": stopped["operation_id"], "legacy_plan_sha256": stopped["plan_sha256"],
        "network": {key: network[key] for key in ("Name", "Id", "Driver", "Scope", "Internal", "Options", "IPAM")},
        "postgres_before": before, "dsn_file": str(directory / "dsn.json"), "token_file": str(directory / "token.json"),
        "control_url": "http://127.0.0.1:8787", "manifest": manifest, "contract": env["WEBHOOK_OUTBOX_CONTRACT"],
        "window": {"not_before": now - 1, "expires_at": now + 120}}
    if host_loopback:
        plan["postgres_transport"] = capability
        _save(harness.output / "HOST_LOOPBACK.json", {"scope": "owned_synthetic_daemon_host_only",
            "capability": capability, "old_sender_network_mode": "host", "candidate_network_mode": "host",
            "pg_network": harness.network, "pg_published": "127.0.0.1:5432", "business_listener": "127.0.0.1:8080",
            "schema_before": before["schema_version"], "production_access": False})
    return plan


def _ready(plan, created):
    identity = bootstrap._postgres(plan, after=True)
    transport = bootstrap.NativeSenderActivationTransport(plan["control_url"], plan["token_file"], container_id=created["container_id"])
    status, value = transport.request("GET", "/activation-registration")
    gate._require(status == 200 and value["database_schema"] == 135, "actual schema135 control not ready")
    return identity


def rejected_original(function, *, expected_cutover_message=None):
    try:
        function()
    except CutoverError as error:
        if (type(error) is CutoverError and expected_cutover_message == DUPLICATE_INITIALIZE_MESSAGE
                and str(error) == expected_cutover_message):
            return True
        raise
    except (ValueError, gate.AdmissionError):
        return True
    raise AssertionError("a repeated original action was accepted")


def closed_http(harness, actual):
    if getattr(harness, "host_loopback", False):
        import http.client
        connection = http.client.HTTPConnection("127.0.0.1", 8080, timeout=3)
        try:
            connection.request("POST", "/login", body=b"username=bad&password=bad",
                               headers={"Content-Type": "application/x-www-form-urlencoded"})
            status = str(connection.getresponse().status)
        finally:
            connection.close()  # No proxy environment, redirects, response body or alternate target.
    else:
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


DIRECT131_BODY = b'{"synthetic":"post-migration-retained-original","entry":{"id":1}}'
OLD131_LOG_BYTES = 16 * 1024
OLD131_SOURCE_REVISION = "51f2e0d8199ea8fa305081f6e175bba64b0ef94b"
DIRECT131_ORIGINAL_SQL = """SELECT json_build_object(
 'schema',(SELECT version FROM schema_version),
 'gate',(SELECT json_build_object('manifest',manifest,'generation',generation,'phase',phase,
                                 'current_operation',current_operation) FROM handoff_state WHERE id=1),
 'retained',(SELECT json_build_object('delivery_id',delivery_id,'body_sha256',body_sha256,
   'body_hex',encode(body,'hex'),'signature',signature,'state',state,'contract',contract)
   FROM webhook_legacy_originals WHERE target_url='http://fixture:8080/direct131-retained'))::text;"""


def direct131_snapshot(harness, operation):
    """Read one owned synthetic original, not all business rows or production data."""
    value = json.loads(harness.sql(DIRECT131_ORIGINAL_SQL))
    digest = hashlib.sha256(DIRECT131_BODY).hexdigest()
    retained = {"delivery_id": digest, "body_sha256": digest, "body_hex": DIRECT131_BODY.hex(),
                "signature": "a" * 64, "state": "held", "contract": "synthetic-bootstrap-no-model"}
    gate._require(set(value) == {"schema", "gate", "retained"} and value["schema"] == "135"
                  and value["gate"] == {"manifest": harness.manifest, "generation": 0,
                      "phase": "closed", "current_operation": operation}
                  and value["retained"] == retained, "direct131 retained original or closed state differs")
    return value


def _old131_log_diagnostic(harness, logs):
    """Export only fixed source literals; retain bounded redacted text privately.

    The literal catalogue describes the reviewed source, not a guarantee that a
    particular image emitted it. Unknown text is never copied into the packet.
    """
    raw = logs.encode()
    bounded = raw[:OLD131_LOG_BYTES].decode(errors="replace")
    redacted = harness.redacted(bounded)
    safe, private, unknown = [], [], []
    for line in redacted.splitlines():
        migration = re.fullmatch(r'(?:time=[0-9T:Z+.-]{20,40} level=INFO )?(?:msg=)?"?Running database migrations"? '
                                r'current_version=(\d{1,6}) latest_version=(\d{1,6})', line)
        stale = re.search(r"the database schema is not up to date: current=v(\d{1,6}) expected=v(\d{1,6})(?:\W|$)", line)
        if migration:
            safe.append({"literal": "Running database migrations", "current_version": int(migration[1]),
                         "latest_version": int(migration[2])})
        elif stale:
            safe.append({"literal": "the database schema is not up to date", "current_version": int(stale[1]),
                         "expected_version": int(stale[2])})
        else:
            unknown.append(hashlib.sha256(line.encode()).hexdigest())
        if re.search(r"(?i)password|passwd|\bpwd\b|token|secret|dsn|credential|authorization|bearer|private.?key|database_url|\bPG[A-Z_]+", line):
            line = "<sensitive line retained only by SHA256 " + hashlib.sha256(line.encode()).hexdigest() + ">"
        private.append(re.sub(r"\b[A-Za-z][A-Za-z0-9+.-]*://[^\s]+", "<redacted-uri>", line))
    private_raw = ("\n".join(private) + "\n").encode()[:OLD131_LOG_BYTES]
    _save(harness.output / "DIRECT131_OLD_LOG_REDACTED.log", private_raw)
    return {"bytes": len(raw), "sha256": hashlib.sha256(raw).hexdigest(),
        "capture_scope": "docker_logs_tail_64", "bounded_complete": len(raw) <= OLD131_LOG_BYTES and len(logs.splitlines()) < 64,
        "reviewed_source_revision": OLD131_SOURCE_REVISION, "source_literals": safe[:8],
        "literal_entries_truncated": len(safe) > 8, "unknown_line_count": len(unknown),
        "unknown_line_sha256": unknown[:8], "private_redacted_file": "DIRECT131_OLD_LOG_REDACTED.log",
        "private_redacted_bytes": len(private_raw), "private_redacted_sha256": hashlib.sha256(private_raw).hexdigest(),
        "raw_log_exported": False}


def _old131_probe(harness, seed):
    """Save actual wait, image lifecycle and safe log observations before refusal."""
    dsn = f"postgres://synthetic:{harness.password}@pg:5432/synthetic_pause?sslmode=disable"
    launched = harness.launch("old131-after135", seed["upstream_image"],
        ["--read-only", "--cap-drop=ALL", "-e", "DATABASE_URL=" + dsn], ["/usr/bin/miniflux", "-migrate"])
    container_id = launched["Id"]
    code = harness.command(["wait", container_id], timeout=15).strip()
    logs = harness.command(["logs", "--tail=64", container_id], timeout=3)
    actual = json.loads(harness.command(["inspect", container_id], timeout=3))[0]
    state = actual.get("State", {})
    started, finished = _old131_timestamp(state.get("StartedAt")), _old131_timestamp(state.get("FinishedAt"))
    checks = {"same_id": actual.get("Id") == container_id, "same_image": actual.get("Image") == seed["upstream_image"],
        "complete_id": bool(re.fullmatch(r"[0-9a-f]{64}", container_id)),
        "complete_image_id": bool(re.fullmatch(r"sha256:[0-9a-f]{64}", seed["upstream_image"])),
        "same_created": actual.get("Created") == launched.get("Created") and _old131_timestamp(actual.get("Created")) is not None,
        "same_started": state.get("StartedAt") == launched.get("State", {}).get("StartedAt"),
        "same_execution": all(actual.get(key) == launched.get(key) for key in ("Config", "HostConfig", "Mounts")),
        "same_owner": actual.get("Config", {}).get("Labels", {}).get(LABEL) == harness.owner,
        "same_name": actual.get("Name") == "/" + harness.names["old131-after135"],
        "fixed_executable": actual.get("Config", {}).get("Entrypoint") is None
            and actual.get("Config", {}).get("Cmd") == ["/usr/bin/miniflux", "-migrate"],
        "read_only": actual.get("HostConfig", {}).get("ReadonlyRootfs") is True,
        "all_capabilities_dropped": actual.get("HostConfig", {}).get("CapDrop") == ["ALL"],
        "zero_mounts": actual.get("Mounts") == [],
        "status_exited": state.get("Status") == "exited", "not_running": state.get("Running") is False,
        "not_paused": state.get("Paused") is False, "not_restarting": state.get("Restarting") is False,
        "not_dead": state.get("Dead") is False, "not_oom": state.get("OOMKilled") is False,
        "no_restart": actual.get("RestartCount") == 0,
        "wait_matches_inspect": code in {"0", "1"} and type(state.get("ExitCode")) is int and str(state["ExitCode"]) == code,
        "finished_at_recorded": started is not None and finished is not None and finished >= started}
    log = _old131_log_diagnostic(harness, logs)
    _save(harness.output / "DIRECT131_OLD_PROBE.json", {"status": "observed_not_accepted", "synthetic_only": True,
        "container_id": container_id, "old_image_id": seed["upstream_image"], "checks": checks,
        "wait_code": code if code in {"0", "1", "137", "143"} else None,
        "wait_code_sha256": hashlib.sha256(code.encode()).hexdigest(),
        "inspect_exit_code": state.get("ExitCode") if type(state.get("ExitCode")) is int else None,
        "inspect_state": {key: state.get(key) if type(state.get(key)) is bool else None
            for key in ("Running", "Paused", "Restarting", "Dead", "OOMKilled")},
        "inspect_status": state.get("Status") if state.get("Status") in {"created", "running", "paused",
            "restarting", "removing", "exited", "dead"} else None,
        "logs": log,
        "old_image_direct_rollback_safe": False, "production_compatibility_proven": False})
    gate._require(all(checks.values()) and log["bounded_complete"], "old fixed image lifecycle or bounded diagnostic unconfirmed")
    resource_checks(actual, seed["upstream_image"], harness.owner, harness.network)
    expected = {"literal": "Running database migrations", "current_version": 135, "latest_version": 131}
    gate._require(code == "0" and log["source_literals"] == [expected] and log["unknown_line_count"] == 0
                  and not log["literal_entries_truncated"], "old fixed image migration no-op not confirmed")
    return log


def _old131_timestamp(value):
    if type(value) is not str or not re.fullmatch(
            r"[0-9]{4}-[0-9]{2}-[0-9]{2}T[0-9]{2}:[0-9]{2}:[0-9]{2}(?:\.[0-9]{1,9})?Z", value):
        return None
    try:
        parsed = datetime.fromisoformat(value[:-1] + "+00:00").timestamp()
    except ValueError:
        return None
    return parsed if parsed > 0 else None


def direct131_restart_and_old_limit(harness, plan, created, cap, evidence):
    """Owned synthetic restart/rollback check; no production or historical raw claim."""
    digest = hashlib.sha256(DIRECT131_BODY).hexdigest()
    harness.manifest = plan["manifest"]
    harness.sql("INSERT INTO webhook_legacy_originals(delivery_id,body_sha256,event_type,user_id,feed_id,entry_id,"
        "target_url,signature,contract,body) VALUES ('" + digest + "','" + digest
        + "','save_entry',1,1,1,'http://fixture:8080/direct131-retained','" + "a" * 64
        + "','synthetic-bootstrap-no-model',decode('" + DIRECT131_BODY.hex() + "','hex'));")
    original = direct131_snapshot(harness, plan["operation_id"])
    before = bootstrap._inspect(created["container_id"])
    harness.command(["stop", "--time=8", created["container_id"]], timeout=10)
    exited = bootstrap._inspect(created["container_id"])
    gate._require(exited["State"]["Running"] is False and exited["State"]["ExitCode"] in (0, 143)
                  and exited["State"].get("OOMKilled") is False, "owned candidate normal TERM unconfirmed")
    harness.command(["start", created["container_id"]])
    harness.wait(lambda: _ready(plan, created), seconds=15, readiness=True)
    after = bootstrap._inspect(created["container_id"])
    gate._require(before["Id"] == after["Id"] == created["container_id"]
                  and before["Image"] == after["Image"] == harness.image_id
                  and before["State"]["StartedAt"] != after["State"]["StartedAt"]
                  and after["State"]["Running"] is True, "actual same-image candidate restart unconfirmed")
    gate._require(direct131_snapshot(harness, plan["operation_id"]) == original,
                  "restart changed retained original")
    denied = closed_http(harness, after)
    # A changed StartedAt must not be silently rebound to the retained release plan.
    rejected_original(lambda: bootstrap.query(cap, plan, evidence))
    _save(harness.output / "DIRECT131_RESTART.json", {"status": "passed", "synthetic_only": True,
        "before_id": before["Id"], "after_id": after["Id"], "image_id": after["Image"],
        "before_started_at": before["State"]["StartedAt"], "after_started_at": after["State"]["StartedAt"],
        "normal_exit_code": exited["State"]["ExitCode"], "business_http_status": denied["status"],
        "closed_and_original_snapshot_sha256": gate._hash(original), "original_body_sha256": digest,
        "stale_release_binding_rejected": True, "historical_raw_coverage": "unknown_before_T0_no_replay",
        "retained_fixture_created_after_migration": True, "power_loss_durability_proven": False})
    seed = json.loads((harness.output / "SEED131.json").read_bytes())
    log = _old131_probe(harness, seed)
    gate._require(direct131_snapshot(harness, plan["operation_id"]) == original,
                  "old fixed image changed migrated data")
    _save(harness.output / "DIRECT131_OLD_LIMIT.json", {"status": "no_op_not_schema_refusal",
        "synthetic_only": True, "old_image_id": seed["upstream_image"], "schema": 135,
        "exit_code": 0, "migration_log_sha256": log["sha256"], "schema_refusal_observed": False,
        "originals_unchanged": True, "old_image_direct_rollback_safe": False,
        "requires_separate_reviewed_database_restore_or_forward_recovery": True,
        "historical_raw_coverage": "unknown_before_T0_no_replay"})
    return {"candidate_restart_closed": True, "retained_original_unchanged": True,
            "old_image_migration_no_op": True, "old_image_schema_refusal": False, "old_image_direct_rollback_safe": False,
            "production_compatibility_proven": False}


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
        rejected_original(lambda: bootstrap.initialize(cap, plan, evidence),
                          expected_cutover_message=DUPLICATE_INITIALIZE_MESSAGE)
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
        direct131 = (direct131_restart_and_old_limit(harness, plan, created, cap, evidence)
                     if plan["postgres_before"]["schema_version"] == 131 else None)
        _deadline(harness)
        proof = {"status": "passed", "synthetic_only": True, "production_access": False, "real_models": 0,
            "container_id": actual["Id"], "image_id": actual["Image"], "actual_mounts": [], "schema_before": plan["postgres_before"]["schema_version"],
            "schema_after": identity["schema_version"], "postgres_identity": identity, "closed_receipt": closed["pg_receipt"],
            "business_http_status": 503, "business_request": denied, "open_actions": 0, "namespace_transport": True,
            "duplicate_create_start_initialize_rejected": True, "direct131_checks": direct131,
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
        errors.append({"stage": role + ":observation", "type": type(error).__name__, **_command_reference(harness, error)})
    if row["State"]["Running"]:
        harness.command(["kill", "--signal", "TERM", row["Id"]], final=True)
        harness.command(["wait", row["Id"]], timeout=8, final=True)
    return errors


def _command_reference(harness, error):
    """Only the actual command failure frame can name a retained original."""
    frame = error.__traceback__
    while frame is not None and frame.tb_next is not None:
        frame = frame.tb_next
    if type(error) is not RuntimeError or frame is None or frame.tb_frame.f_code is not Harness.command.__code__:
        return {}
    local = frame.tb_frame.f_locals
    sequence = harness.sequence
    if (local.get("self") is not harness or type(sequence) is not int or not 1 <= sequence <= 9999
            or local.get("path") != harness.output / f"command-{sequence:04d}.json"
            or "rc" not in local or local["rc"] == 0):
        return {}
    return {"command_sequence": sequence}


def _command_error_class(stderr, rc):
    if rc is None:
        return "result_unknown"
    if len(stderr.encode()) > 65536 or not stderr:
        return "unclassified"
    first = stderr.splitlines()[0]
    if first == "Cannot connect to the Docker daemon at unix:///var/run/docker.sock. Is the docker daemon running?":
        return "daemon_unreachable"
    prefix = "docker: Error response from daemon: "
    if not first.startswith(prefix):
        return "unclassified"
    value = first[len(prefix):]
    if re.fullmatch(r'[^\r\n]{0,2048}exec: "[^"\r\n]{1,256}": executable file not found in \$PATH(?:: unknown)?\.?', value):
        return "container_executable_not_found"
    if re.fullmatch(r"No such image: sha256:[0-9a-f]{64}\.?", value):
        return "image_absent"
    if re.fullmatch(r'Conflict\. The container name "/[a-zA-Z0-9_.-]{1,128}" is already in use by container "[0-9a-f]{64}"\..{0,512}', value):
        return "container_name_conflict"
    return "unclassified"


def _command_summary(fd, sequence, budget):
    name = f"command-{sequence:04d}.json"
    try:
        original, raw = export._export_file(fd, name, budget)
    except Exception as error:
        # No second open/read, chmod, or relaxed original-reader limit. This is
        # only a nofollow path observation after failure, not the rejected FD.
        error._bootstrap_command_metadata = _failed_command_metadata(fd, sequence)
        raise
    def unique(pairs):
        value = {}
        for key, item in pairs:
            gate._require(key not in value, "ambiguous retained command JSON")
            value[key] = item
        return value
    value = json.loads(raw, object_pairs_hook=unique)
    gate._require(isinstance(value, dict) and set(value) == {"argv", "rc", "stdin_sha256", "elapsed_seconds", "utc", "stdout", "stderr"},
                  "exact retained command record required")
    argv, rc, elapsed, stderr = value["argv"], value["rc"], value["elapsed_seconds"], value["stderr"]
    gate._require(isinstance(argv, list) and 4 <= len(argv) <= 128 and all(isinstance(arg, str) for arg in argv)
                  and argv[:3] == ["docker", "--host", "unix:///var/run/docker.sock"], "fixed Docker command prefix required")
    gate._require(rc is None or (type(rc) is int and -255 <= rc <= 255 and rc != 0), "failed or unknown command result required")
    gate._require(type(elapsed) in {int, float} and math.isfinite(elapsed) and elapsed >= 0
                  and isinstance(stderr, str) and isinstance(value["stdout"], str), "bounded command result fields required")
    operations = {"run", "pull", "inspect", "wait", "kill", "logs", "exec", "start", "stop"}
    operation = argv[3] if argv[3] in operations else "unclassified"
    if argv[3:5] in (["image", "inspect"], ["network", "create"], ["network", "inspect"]):
        operation = ".".join(argv[3:5])
    return {"command_sequence": sequence, "operation": operation, "rc": rc, "elapsed_seconds": elapsed,
            "original_record_sha256": original["sha256"], "retained_stderr_sha256": hashlib.sha256(stderr.encode()).hexdigest(),
            "stderr_bytes": len(stderr.encode()), "error_class": _command_error_class(stderr, rc), "execution_proof": False}


def _failed_command_metadata(fd, sequence):
    gate._require(type(sequence) is int and 1 <= sequence <= 9999, "bounded command sequence required")
    name = f"command-{sequence:04d}.json"
    result = {"schema": 1, "command_sequence": sequence, "file_name": name,
              "observation": "nofollow_path_after_export_failure_not_original_fd",
              "content_read_for_diagnostic": False, "original_failure_condition_confirmed": False}
    try:
        info = os.stat(name, dir_fd=fd, follow_symlinks=False)
    except OSError:
        return {**result, "metadata_state": "unavailable"}
    checks = {"regular_file": stat.S_ISREG(info.st_mode), "single_link": info.st_nlink == 1,
              "current_uid": info.st_uid == os.geteuid(), "private_mode_0600": stat.S_IMODE(info.st_mode) == 0o600,
              "original_file_byte_limit": info.st_size <= export.EXPORT_FILE_BYTES}
    return {**result, "metadata_state": "observed", "size": info.st_size, "mode": stat.S_IMODE(info.st_mode),
            "uid": info.st_uid, "nlink": info.st_nlink, "failed_observed_conditions": [k for k, ok in checks.items() if not ok]}


def _partial_export(packet):
    # Failure output is proof-only, even for small already-validated originals:
    # file identity does not validate the privacy of arbitrary JSON fields.
    # No raw_root, argv, message or credential content crosses this boundary.
    files = {}
    for name in ("RESULT.json", "FAILURE.json"):
        if name in packet["files"]:
            record = packet["files"][name]
            files[name] = {key: record[key] for key in ("sha256", "metadata")}
    value = {"complete": False, "originals_modified": False, "execution_proof": False,
             "scope": "already_validated_result_failure_proofs_only", "files": files}
    gate._require(len(gate._encoded(value)) <= 256 * 1024, "bounded partial failure export required")
    return value


def _command_summaries(fd, files, budget):
    result = files["RESULT.json"]["content"]
    failure = files.get("FAILURE.json", {}).get("content")
    rows = [failure, result.get("failure"), *result.get("errors", [])]
    sequences = set()
    for row in rows:
        if isinstance(row, dict) and "command_sequence" in row:
            sequence = row["command_sequence"]
            gate._require(type(sequence) is int and 1 <= sequence <= 9999, "bounded command sequence required")
            sequences.add(sequence)
    gate._require(len(sequences) <= 8, "command diagnostic count bound exceeded")
    summaries = [_command_summary(fd, sequence, budget) for sequence in sorted(sequences)]
    gate._require(len(gate._encoded(summaries)) <= 8192, "command diagnostic output bound exceeded")
    return summaries


def execute(repo, source, image_id, binary, output, build_evidence, *, predecessor_schema=132, host_loopback=False,
            private_host_context=None):
    gate._require(sys.platform == "linux" and os.geteuid() == 0, "native bootstrap requires Linux root")
    gate._require(type(predecessor_schema) is int and predecessor_schema in (131, 132), "explicit synthetic predecessor required")
    gate._require(type(host_loopback) is bool and (not host_loopback or predecessor_schema == 131),
                  "host-loopback native proof requires explicit actual131 predecessor")
    gate._require(private_host_context is None or (host_loopback and local_pg._NATIVE_HOST.get() is private_host_context),
                  "private daemon host requires the exact active live native scope")
    if host_loopback:
        local_pg._host()  # Hard fail before Docker unless this is the real daemon host namespace.
    gate._require(repo == ROOT and output.is_absolute() and not output.exists(), "fixed source and fresh synthetic output required")
    bootstrap.private.safe_path(output.parent)
    gate._require(output.parent.stat().st_uid == 0 and stat.S_IMODE(output.parent.stat().st_mode) == 0o700,
                  "new native originals must stay inside a private root-owned parent")
    binding = source_binding(repo, source, binary)
    records = build_records(build_evidence, image_id, binding["revision"])
    options = {"private_host_context": private_host_context} if private_host_context is not None else {}
    harness = (HostLoopbackHarness if host_loopback else Harness)(repo, source, image_id, binary, output, **options)
    harness.phase_deadline = (private_host_context.plan["active_deadline"] if private_host_context is not None
                              else harness.started + MAX_SECONDS)
    harness.predecessor_schema = predecessor_schema
    _save(output / "SOURCE.json", binding)
    daemon, failure, errors = None, None, []
    try:
        harness.prepare()
        (seed131 if predecessor_schema == 131 else seed132)(harness)
        daemon = subprocess.Popen([sys.executable, "-I", "-B", "-c", "import time; time.sleep(600)"],
                                  stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        exercise(harness, binding, daemon, records)
    except BaseException as error:
        import traceback
        failure = {"type": type(error).__name__, "message_sha256": hashlib.sha256(str(error).encode()).hexdigest(),
            **_command_reference(harness, error),
            "locations": [{"file": Path(row.filename).name, "function": row.name, "line": row.lineno}
                          for row in traceback.extract_tb(error.__traceback__) if Path(row.filename).resolve().is_relative_to(repo)]}
        _save(output / "FAILURE.json", failure)
    finally:
        from scripts import production_native_deadline as native_budget
        with native_budget.final_phase() if private_host_context is not None else nullcontext():
            errors.extend(preserve_candidate(harness))
            if "pg" in harness.names:
                try:
                    harness.command(["exec", harness.names["pg"], "pg_dump", "-U", "synthetic", "-d", "synthetic_pause"],
                                    timeout=15, final=True)
                except BaseException as error:
                    errors.append({"stage": "postgres_dump", "type": type(error).__name__, **_command_reference(harness, error)})
            # Preserve owned synthetic containers; no KILL fallback or deletion.
            for role in reversed(harness.created):
                try:
                    errors.extend(preserve_owned(harness, role))
                except BaseException as error:
                    errors.append({"stage": role, "type": type(error).__name__, **_command_reference(harness, error)})
            if daemon is not None:
                daemon.terminate()
                try:
                    daemon.wait(timeout=native_budget.clamp(3) if private_host_context is not None else 3)
                except subprocess.TimeoutExpired:
                    errors.append({"stage": "owned_daemon", "type": "TERM_unconfirmed"})
    result = {"status": "passed" if failure is None and not errors else "unconfirmed", "source_sha": binding["revision"],
        "failure": failure, "errors": errors, "elapsed_seconds": time.monotonic() - harness.started,
        "production_access": False, "real_models": 0, "system_cron_access": False, "raw_root": str(output), "predecessor_schema": predecessor_schema, "host_loopback": host_loopback,
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
        gate._require("RESULT.json" in packet["files"], "bounded complete result export required")
        packet["command_failures"] = _command_summaries(fd, packet["files"], budget)
        gate._require(export._metadata(os.fstat(fd)) == before, "root originals changed during safe export")
        gate._require(len(gate._encoded(packet)) <= 256 * 1024,
                      "bounded complete result export required")
        return packet
    except Exception as error:
        try:
            error._bootstrap_partial_export = _partial_export(packet)
        except Exception:
            # A diagnostic cannot replace the original refusal or claim any
            # content was exported when its bounded encoding failed.
            error._bootstrap_partial_export = {"complete": False, "originals_modified": False,
                "execution_proof": False, "scope": "partial_export_unavailable", "files": {}}
        raise
    finally:
        os.close(fd)


def main():
    previous_umask = os.umask(0o077)
    try:
        parser = argparse.ArgumentParser(description=__doc__)
        for name in ("repo", "source", "binary", "output", "build-evidence"):
            parser.add_argument("--" + name, required=True, type=Path)
        parser.add_argument("--image-id", required=True)
        parser.add_argument("--predecessor-schema", type=int, choices=(131, 132), default=132,
                            help="explicit real fixed upstream131 or existing exact132 seed (default remains132)")
        parser.add_argument("--host-loopback", action="store_true",
                            help="real daemon host namespace only; exact loopback PG and explicit predecessor131")
        args = parser.parse_args()
        try:
            result = execute(args.repo.resolve(), args.source.resolve(), args.image_id, args.binary.resolve(),
                             args.output.resolve(), args.build_evidence.resolve(), predecessor_schema=args.predecessor_schema, host_loopback=args.host_loopback)
            packet = {"status": result["status"], "safe_evidence": safe_export(args.output.resolve())}
            print(gate._encoded(packet).decode(), flush=True)
            return 0 if packet["status"] == "passed" else 3
        except BaseException as error:
            locations = []
            try:
                from scripts.verify_production_startup_native import public_exception_locations
                locations = public_exception_locations(error, root=ROOT)
            except Exception:
                pass  # Keep the original fallback even if diagnostics are unavailable.
            failure = {"status": "unconfirmed", "error_type": type(error).__name__,
                       "error_sha256": hashlib.sha256(str(error).encode()).hexdigest(),
                       "safe_export_complete": False, "locations": locations}
            if hasattr(error, "_bootstrap_partial_export"):
                failure["safe_evidence"] = error._bootstrap_partial_export
            if hasattr(error, "_bootstrap_command_metadata"):
                failure["command_export_diagnostic"] = error._bootstrap_command_metadata
            print(json.dumps(failure), flush=True)
            return 3
    finally:
        os.umask(previous_umask)


if __name__ == "__main__":
    raise SystemExit(main())
