"""Native AMD64 actual Miniflux pause rehearsal; no production/AI-Filter access.

Only newly labelled containers on a new internal network are used. The fixed
sender image and synthetic PG/HTTP evidence are retained, including failures.
"""
from __future__ import annotations

import argparse
import base64
from datetime import datetime, timezone
import hashlib
import hmac
import json
import math
import os
from pathlib import Path
import platform
import re
import secrets
import stat
import subprocess
import time
import uuid

from verify_handoff_operations_native import IMAGE as PYTHON_IMAGE, native_host

DOCKER = ["docker", "--host", "unix:///var/run/docker.sock"]
PG_IMAGE = "postgres@sha256:721873c34ceb9f8d8fc265984940dc982404c105f19ad51be9fdc5970a6080ea"
LABEL = "ai-filter.synthetic-miniflux-pause"
MEMORY = 768 * 1024 * 1024
PIDS = 256
MAX_SECONDS = 600
COMMAND_EVIDENCE_BYTES = 8 * 1024**2  # Existing selected-original file bound, not the control-record bound.
CLIENT = """
import json,sys,urllib.request,urllib.error
p=json.load(sys.stdin);headers={'Content-Type':'application/json'}
if p.get('auth'): headers['Authorization']=p['auth']
body=json.dumps(p['value']).encode() if p.get('value') is not None else None
r=urllib.request.Request(p['url'],data=body,headers=headers,method=p['method'])
try:
 with urllib.request.urlopen(r,timeout=12) as s: value={'status':s.status,'body':s.read(1048576).decode()}
except urllib.error.HTTPError as s: value={'status':s.code,'body':s.read(1048576).decode(),'category':s.headers.get('X-Handoff-Error-Category')}
print(json.dumps(value))
"""


ERROR_CATEGORIES = frozenset({"unconfirmed", "deadline", "uncertain", "conflict", "closed", "no_rows", "lock", "db", "other"})
OBSERVED_DOCKER_OPERATIONS = frozenset({"inspect", "run", "exec", "logs", "ps", "image", "network", "volume",
    "start", "stop", "kill", "rm", "rmi", "build", "images", "info", "load", "save", "create", "wait"})


def _command_observation(args, sequence, timeout, remaining):
    operation = args[0] if type(args) in (list, tuple) and args and type(args[0]) is str else None

    def seconds(value):
        return value if type(value) in (int, float) and math.isfinite(value) and -600 <= value <= 600 else None

    return {"schema": 1, "sequence": sequence,
            "operation": "docker_" + operation if operation in OBSERVED_DOCKER_OPERATIONS else "unknown",
            "timeout_seconds": seconds(timeout), "harness_remaining_seconds": seconds(remaining),
            "elapsed_seconds": None, "returncode": None, "outcome": "unconfirmed"}


def error_category(value):
    """CI diagnostic only: a fixed Go enum header, never free-form error text."""
    if value is None:
        return None
    return value if value in ERROR_CATEGORIES else "invalid"


def save(path, value, *, retain_proof=False):
    with path.open("x+" if retain_proof else "x", encoding="utf-8") as stream:
        json.dump(value, stream, ensure_ascii=False, indent=2, sort_keys=True, allow_nan=False)
        stream.write("\n")
        if retain_proof:
            stream.flush()
            return command_original_proof(path, sync=True, fd=stream.fileno())


def command_original_proof(path, *, sync=False, fd=None):
    """A diagnostic original is not a small admission record; verify its exact FD."""
    from scripts import production_host_ingress_native as host
    from services import admission_fence as gate
    owned = fd is None
    fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW) if owned else fd
    try:
        before = os.fstat(fd)
        gate._require(stat.S_ISREG(before.st_mode) and before.st_nlink == 1
                      and before.st_uid == os.geteuid() and stat.S_IMODE(before.st_mode) == 0o600,
                      "unsafe command original")
        proof = host.file_proof(path, limit=COMMAND_EVIDENCE_BYTES, fd=fd)
        if sync:
            os.fsync(fd)
            gate._sync_directory(path.parent)
        gate._require(host.identity(os.fstat(fd)) == proof["identity"] == host.identity(path.lstat()),
                      "command original changed during confirmation")
        return proof
    finally:
        if owned:
            os.close(fd)


def retain_command_result(path, sequence, returncode, original):
    """Publish small execution metadata only after the unchanged raw evidence is durable."""
    from services import admission_fence as gate
    from scripts import production_host_ingress_native as host
    gate._require(type(sequence) is int and sequence > 0 and (returncode is None or type(returncode) is int),
                  "invalid command result identity")
    receipt = path.with_suffix(".result.json")
    gate._require(not gate._present(receipt) and not gate._present(receipt.with_suffix(".json.pending")),
                  "command result evidence already exists")
    gate._require(gate._encoded(command_original_proof(path)) == gate._encoded(original), "command original changed after capture")
    result = {"schema": 1, "sequence": sequence, "returncode": returncode, "original": original}
    gate._require(len(host.encoded(result)) <= gate.MAX_RECORD_BYTES, "command result record byte limit")
    host.durable(receipt, result)  # Exclusive creation; never replace a retained receipt.
    return result


def read_command_result(path, sequence, expected):
    """The 16 KiB admission reader is used only for the small original-bound receipt."""
    from services import admission_fence as gate
    value = gate._read(path.with_suffix(".result.json"))
    gate._require(set(value) == {"schema", "sequence", "returncode", "original"}
                  and type(value["schema"]) is int and value["schema"] == 1
                  and type(value["sequence"]) is int and value["sequence"] == sequence
                  and (value["returncode"] is None or type(value["returncode"]) is int),
                  "command result identity changed")
    gate._require(gate._encoded(value) == gate._encoded(expected), "command result differs from actual execution")
    gate._require(gate._encoded(command_original_proof(path)) == gate._encoded(value["original"]), "command original proof changed")
    return value


def sha(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def canonical_repo_digest(reference):
    if not isinstance(reference, str) or reference.count("@") != 1:
        raise ValueError("full repository digest required")
    repository, digest = reference.split("@")
    if not re.fullmatch(r"sha256:[0-9a-f]{64}", digest):
        raise ValueError("full sha256 digest required")
    if ":" in repository.rsplit("/", 1)[-1]:
        repository, tag = repository.rsplit(":", 1)
        if not re.fullmatch(r"[A-Za-z0-9_][A-Za-z0-9_.-]{0,127}", tag):
            raise ValueError("invalid image tag")
    components = repository.split("/")
    if len(components) > 1 and ("." in components[0] or ":" in components[0] or components[0] == "localhost"):
        registry, components = components[0], components[1:]
        if not re.fullmatch(r"[a-z0-9]+(?:[.-][a-z0-9]+)*(?::[0-9]{1,5})?", registry):
            raise ValueError("invalid registry")
    else:
        registry = "docker.io"
    if not components or any(not re.fullmatch(r"[a-z0-9]+(?:(?:[._]|__|-+)[a-z0-9]+)*", part)
                             for part in components):
        raise ValueError("invalid repository")
    if registry == "index.docker.io":
        registry = "docker.io"
    if registry == "docker.io" and len(components) == 1:
        components.insert(0, "library")
    return registry + "/" + "/".join(components) + "@" + digest


def image_checks(row, reference):
    if row.get("Architecture") != "amd64" or row.get("Os") != "linux":
        raise ValueError("actual fixed image platform mismatch")
    image_id = row.get("Id")
    if not isinstance(image_id, str) or not re.fullmatch(r"sha256:[0-9a-f]{64}", image_id):
        raise ValueError("actual full image ID required")
    if re.fullmatch(r"sha256:[0-9a-f]{64}", reference):
        if image_id != reference:
            raise ValueError("actual fixed sender image ID mismatch")
        return
    expected = canonical_repo_digest(reference)
    actual = row.get("RepoDigests")
    if not isinstance(actual, list) or expected not in [canonical_repo_digest(value) for value in actual]:
        raise ValueError("fixed dependency repository digest mismatch")


def resource_checks(row, image_id, owner, network, memory=MEMORY, pids=PIDS):
    host = row["HostConfig"]
    checks = {"actual_fixed_image": row["Image"] == image_id,
              "owned_label": row["Config"]["Labels"].get(LABEL) == owner,
              "internal_network": host["NetworkMode"] == network,
              "no_host_ports": not host.get("PortBindings"),
              "memory": host["Memory"] == memory, "memory_swap": host["MemorySwap"] == memory,
              "pids": host["PidsLimit"] == pids, "cpu": host["NanoCpus"] == 1_000_000_000,
              "not_privileged": host["Privileged"] is False,
              "no_new_privileges": any(x in {"no-new-privileges", "no-new-privileges:true"}
                  for x in host.get("SecurityOpt", [])), "not_oom": row["State"]["OOMKilled"] is False}
    if not all(checks.values()):
        raise ValueError("actual resource binding failed: " + ",".join(k for k, v in checks.items() if not v))
    return checks


def memory_evidence(peak, events):
    if not isinstance(peak, str) or not re.fullmatch(r"[0-9]+\s*", peak):
        raise ValueError("actual cgroup memory peak unavailable")
    counts = {}
    for line in events.splitlines():
        parts = line.split()
        if len(parts) != 2 or parts[0] in counts or not re.fullmatch(r"[0-9]+", parts[1]):
            raise ValueError("actual cgroup event evidence malformed")
        counts[parts[0]] = int(parts[1])
    if not {"oom", "oom_kill"} <= counts.keys():
        raise ValueError("actual cgroup OOM counters unavailable; not zero")
    return {"peak_bytes": int(peak.strip()), "events": counts}


class Harness:
    def __init__(self, repo, source, image_id, binary, output):
        if platform.system() != "Linux" or platform.machine() != "x86_64":
            raise ValueError("native Linux AMD64 host required before Docker access")
        if not re.fullmatch(r"sha256:[0-9a-f]{64}", image_id):
            raise ValueError("actual full sender image ID required")
        self.repo, self.source, self.image_id, self.binary = repo, source, image_id, binary
        self.output = output
        output.mkdir(mode=0o700, parents=True, exist_ok=False)
        self.owner = uuid.uuid4().hex
        self.prefix = "miniflux-pause-" + self.owner[:12]
        self.network = self.prefix + "-network"
        self.names, self.images, self.created = {}, {}, []
        self.sequence, self.started = 0, time.monotonic()
        self.last_error_category = None
        self.phase_deadline = None
        self.password, self.token = secrets.token_hex(24), secrets.token_hex(32)
        self.auth = "Basic " + base64.b64encode(("synthetic:" + self.password).encode()).decode()
        self.env = {k: v for k, v in os.environ.items() if k not in {"DOCKER_HOST", "DOCKER_CONTEXT", "DOCKER_TLS_VERIFY", "DOCKER_CERT_PATH"}}

    def redacted(self, value):
        for secret in (self.password, self.token, self.auth):
            value = value.replace(secret, "<synthetic-secret>")
        return value

    def command(self, args, *, stdin=None, timeout=30, final=False, allowed_failure=False, command_result=None,
                command_observation=None):
        if command_result is not None and (type(command_result) is not dict or command_result):
            raise ValueError("command result must be a fresh output mapping")
        if command_observation is not None and (type(command_observation) is not dict or command_observation):
            raise ValueError("command observation must be a fresh output mapping")
        self.sequence += 1
        path = self.output / f"command-{self.sequence:04d}.json"
        remaining = MAX_SECONDS - (time.monotonic() - self.started)
        if self.phase_deadline is not None and not final:
            remaining = min(remaining, self.phase_deadline - time.monotonic())
        if remaining <= 0 and not final:
            raise TimeoutError("bounded isolated rehearsal elapsed")
        start = time.monotonic()
        effective_timeout = timeout if final else min(timeout, remaining)
        if command_observation is not None:
            command_observation.update(_command_observation(args, self.sequence, effective_timeout, remaining))
        try:
            result = subprocess.run(DOCKER + list(map(str, args)), input=stdin, text=True,
                                    capture_output=True, timeout=effective_timeout,
                                    check=False, env=self.env)
            rc, stdout, stderr = result.returncode, result.stdout, result.stderr
            if command_observation is not None:
                command_observation.update(returncode=rc, outcome="returned")
        except subprocess.TimeoutExpired as error:
            rc = None
            stdout = error.stdout or ""
            stderr = error.stderr or ""
            stdout = stdout.decode(errors="replace") if isinstance(stdout, bytes) else stdout
            stderr = stderr.decode(errors="replace") if isinstance(stderr, bytes) else stderr
            if command_observation is not None:
                command_observation["outcome"] = "timeout"
        finally:
            if command_observation is not None:
                elapsed = time.monotonic() - start
                command_observation["elapsed_seconds"] = elapsed if math.isfinite(elapsed) and 0 <= elapsed <= 600 else None
        original = save(path, {"argv": [self.redacted(str(v)) for v in DOCKER + list(args)], "rc": rc,
                    "stdin_sha256": hashlib.sha256((stdin or "").encode()).hexdigest(),
                    "elapsed_seconds": time.monotonic() - start, "utc": datetime.now(timezone.utc).isoformat(),
                    "stdout": self.redacted(stdout), "stderr": self.redacted(stderr)}, retain_proof=command_result is not None)
        if command_result is not None:
            command_result.update(retain_command_result(path, self.sequence, rc, original))
        if rc != 0 and not allowed_failure:
            raise RuntimeError(f"isolated command {self.sequence} failed rc={rc}")
        return stdout + stderr if args and args[0] == "logs" else stdout

    def launch(self, name, image_id, extra=(), command=(), *, memory=MEMORY, pids=PIDS):
        actual = self.prefix + "-" + name
        self.names[name], self.images[name] = actual, image_id
        self.created.append(name)
        self.command(["run", "-d", "--pull=never", "--platform=linux/amd64", "--name", actual,
                      "--label", LABEL + "=" + self.owner, "--network", self.network, "--network-alias", name,
                      "--memory", str(memory), "--memory-swap", str(memory), "--pids-limit", str(pids),
                      "--cpus=1", "--security-opt=no-new-privileges", *extra, image_id, *command])
        row = json.loads(self.command(["inspect", actual]))[0]
        checks = resource_checks(row, image_id, self.owner, self.network, memory, pids)
        save(self.output / ("PROFILE_" + name + ".json"), {"checks": checks, "inspect": self.safe_inspect(row)})
        return row

    def safe_inspect(self, row):
        return {key: row.get(key) for key in ("Id", "Name", "Image", "Created", "State", "HostConfig", "Mounts", "RestartCount")}

    def wait(self, function, seconds=30, readiness=False):
        end = time.monotonic() + seconds
        prior = self.phase_deadline
        self.phase_deadline = min(prior, end) if prior is not None else end
        try:
            while time.monotonic() < self.phase_deadline:
                try:
                    value = function()
                    if time.monotonic() >= self.phase_deadline:
                        raise TimeoutError("actual handshake exceeded phase budget")
                    if value:
                        return value
                except (RuntimeError, ValueError, json.JSONDecodeError):
                    if not readiness:
                        raise
                time.sleep(min(0.25, max(0, self.phase_deadline - time.monotonic())))
            raise TimeoutError("bounded actual state/HTTP handshake did not arrive")
        finally:
            self.phase_deadline = prior

    def http(self, role, path, method="GET", value=None, *, expected=200):
        auth = "Bearer " + self.token if role == "fixture" else self.auth
        payload = {"url": f"http://{role}:8080{path}", "method": method, "value": value, "auth": auth}
        raw = self.command(["exec", "-i", self.names["fixture"], "python", "-B", "-c", CLIENT],
                           stdin=json.dumps(payload), timeout=15)
        result = json.loads(raw)
        if result["status"] != expected:
            raise ValueError(f"actual isolated HTTP {role}{path}: {result['status']} != {expected}")
        if not result["body"]:
            return None
        try:
            return json.loads(result["body"])
        except json.JSONDecodeError:
            return result["body"]

    def sql(self, statement, database="synthetic_pause"):
        return self.command(["exec", self.names["pg"], "psql", "-X", "-A", "-t", "-v", "ON_ERROR_STOP=1",
                             "-U", "synthetic", "-d", database, "-c", statement])

    def events(self):
        return self.http("fixture", "/events")["events"]

    def control(self, items, *, hold=False, release=False):
        return self.http("fixture", "/control", "POST", {"items": items, "hold": hold, "release": release})

    def private_exchange(self, path, method="GET", value=None):
        # The real control listener is loopback-only inside the sender. A fresh
        # fixed Python client joins that exact container network namespace; no
        # control port is published and no address override broadens the hook.
        target = self.names["sender"]
        sender = json.loads(self.command(["inspect", target]))[0]
        resource_checks(sender, self.image_id, self.owner, self.network)
        name = self.prefix + "-control-" + str(self.sequence)
        payload = {"url": "http://127.0.0.1:8787" + path, "method": method,
                   "value": value, "auth": "Bearer " + self.token}
        raw = self.command(["run", "-i", "--pull=never", "--platform=linux/amd64", "--name", name,
                            "--label", LABEL + "=" + self.owner, "--network", "container:" + sender["Id"],
                            "--read-only", "--cap-drop=ALL", "--security-opt=no-new-privileges",
                            "--memory", str(MEMORY), "--memory-swap", str(MEMORY), "--pids-limit", str(PIDS),
                            "--cpus=1", self.python_id, "python", "-B", "-c", CLIENT],
                           stdin=json.dumps(payload), timeout=18)
        row = json.loads(self.command(["inspect", name]))[0]
        resource_checks(row, self.python_id, self.owner, "container:" + sender["Id"])
        result = json.loads(raw)
        self.last_error_category = error_category(result.get("category"))
        return result["status"], json.loads(result["body"]) if result["body"] else None

    def private_control(self, path, method="GET", value=None, *, expected=200):
        status, result = self.private_exchange(path, method, value)
        if status not in (expected if isinstance(expected, tuple) else (expected,)):
            raise ValueError(f"private control {path}: {status} != {expected}")
        return result

    def postgres_ready(self):
        return self.command(["exec", self.names["pg"], "pg_isready", "-U", "synthetic", "-d", "synthetic_pause"])

    def prepare(self):
        info = json.loads(self.command(["info", "--format", "{{json .}}"]))
        native_host(info)
        save(self.output / "HOST.json", {"native_amd64": True, "machine": platform.machine(),
                                         "docker_architecture": info["Architecture"]})
        self.prepare_network()
        images = {}
        for name, reference in (("sender", self.image_id), ("python", PYTHON_IMAGE), ("postgres", PG_IMAGE)):
            row = json.loads(self.command(["image", "inspect", reference]))[0]
            image_checks(row, reference)
            images[name] = {key: row.get(key) for key in ("Id", "Architecture", "Os", "RepoDigests")}
        self.python_id, pg_id = images["python"]["Id"], images["postgres"]["Id"]
        save(self.output / "IMAGES.json", images)
        volume = self.prefix + "-pg"
        self.command(["volume", "create", "--label", LABEL + "=" + self.owner, volume])
        self.launch("pg", pg_id, ["--mount", "type=volume,src=" + volume + ",dst=/var/lib/postgresql/data",
                    "-e", "POSTGRES_USER=synthetic", "-e", "POSTGRES_DB=synthetic_pause",
                    "-e", "POSTGRES_PASSWORD=" + self.password], memory=384 * 1024 * 1024, pids=64)
        self.wait(self.postgres_ready, readiness=True)
        evidence = self.output / "fixture"
        evidence.mkdir(mode=0o700)
        self.launch("fixture", self.python_id, ["--read-only", "--cap-drop=ALL", "--user", f"{os.getuid()}:{os.getgid()}",
                    "--mount", "type=bind,src=" + str(evidence) + ",dst=/evidence",
                    "--mount", "type=bind,src=" + str(self.repo / "scripts/miniflux_pause_fixture.py") + ",dst=/fixture.py,readonly",
                    "-e", "MINIFLUX_PAUSE_FIXTURE_TOKEN=" + self.token], ["python", "-B", "/fixture.py"])
        self.wait(lambda: self.http("fixture", "/health"), readiness=True)

    def prepare_network(self):
        self.command(["network", "create", "--internal", "--label", LABEL + "=" + self.owner, self.network])
        network = json.loads(self.command(["network", "inspect", self.network]))[0]
        if network["Internal"] is not True:
            raise ValueError("isolation network is not internal")
        save(self.output / "NETWORK.json", network)

    def launch_sender(self):
        lock = json.loads((self.repo / "ops/miniflux/pause-source-lock.json").read_text())
        for name, digest in lock["patched_files_sha256"].items():
            if sha(self.source / name) != digest:
                raise ValueError("actual patched source differs: " + name)
        manifest = hashlib.sha256(json.dumps({"image": self.image_id, "binary": sha(self.binary),
                                              "source_lock": sha(self.repo / "ops/miniflux/pause-source-lock.json")}, sort_keys=True).encode()).hexdigest()
        self.manifest = manifest
        self.launch("sender", self.image_id, ["--read-only", "--cap-drop=ALL", "--tmpfs", "/tmp:rw,nosuid,nodev,size=16777216",
                    "--label", "io.ai-filter.handoff.role=" + getattr(self, "sender_role", "miniflux-synthetic"),
                    "--mount", "type=bind,src=" + str(self.repo / "ops/miniflux/pause-source-lock.json") + ",dst=/handoff-source-lock.json,readonly",
                    "-e", "DATABASE_URL=postgres://synthetic:" + self.password + "@pg:5432/synthetic_pause?sslmode=disable",
                    "-e", "RUN_MIGRATIONS=1", "-e", "CREATE_ADMIN=1", "-e", "ADMIN_USERNAME=synthetic",
                    "-e", "ADMIN_PASSWORD=" + self.password, "-e", "LISTEN_ADDR=0.0.0.0:8080",
                    "-e", "LOG_LEVEL=debug", "-e", "POLLING_FREQUENCY=1", "-e", "WORKER_POOL_SIZE=1",
                    "-e", "INTEGRATION_ALLOW_PRIVATE_NETWORKS=1", "-e", "FETCHER_ALLOW_PRIVATE_NETWORKS=1", "-e", "WEBHOOK_OUTBOX=1",
                    "-e", "WEBHOOK_OUTBOX_CONTRACT=" + getattr(self, "sender_contract", "synthetic-no-model-contract"), "-e", "HANDOFF_ENABLED=1",
                    "-e", "HANDOFF_MANIFEST=" + manifest, "-e", "HANDOFF_CONTROL_ADDR=127.0.0.1:8787",
                    "-e", "HANDOFF_CONTROL_TOKEN=" + self.token])
        copied = self.output / "actual-miniflux"
        self.command(["cp", self.names["sender"] + ":/usr/bin/miniflux", str(copied)])
        if sha(copied) != sha(self.binary):
            raise ValueError("actual running image binary differs from compiled fixed source")
        save(self.output / "SENDER_BINDING.json", {"image_id": self.image_id, "binary_sha256": sha(copied),
             "lock_sha256": sha(self.repo / "ops/miniflux/pause-source-lock.json"), "manifest": manifest,
             "patched_files_sha256": lock["patched_files_sha256"], "production_access": False, "models": 0})
        self.wait(lambda: self.http("sender", "/healthcheck"), readiness=True)

    def case(self, name, value):
        save(self.output / ("CASE_" + name + ".json"), {"status": "passed", "observed_utc": datetime.now(timezone.utc).isoformat(), **value})

    def gate_snapshot(self):
        sql = """SELECT json_build_object(
          'state',(SELECT COALESCE(json_agg(x),'[]') FROM handoff_state x),
          'operations',(SELECT COALESCE(json_agg(x),'[]') FROM handoff_operations x),
          'leases',(SELECT COALESCE(json_agg(x),'[]') FROM handoff_leases x));"""
        return json.loads(self.sql(sql))

    def outbox(self):
        return json.loads(self.sql("""SELECT COALESCE(json_agg(x),'[]') FROM
          (SELECT delivery_id,body_sha256,entry_id,state,attempts,contract,
           replace(encode(body,'base64'),E'\\n','') AS body_b64,encode(sha256(body),'hex') AS actual_body_sha256
           FROM webhook_outbox ORDER BY entry_id) x;"""))

    def proved_row(self, key):
        rows = [row for row in self.outbox() if json.loads(base64.b64decode(row["body_b64"]))["entries"][0]["url"].endswith("/" + key)]
        if len(rows) > 1:
            raise ValueError("multiple rows for one synthetic article")
        if not rows or rows[0]["state"] != "acked":
            return None
        row = rows[0]
        if row["delivery_id"] != row["actual_body_sha256"] or row["body_sha256"] != row["delivery_id"]:
            raise ValueError("outbox exact body identity mismatch")
        return row

    def checked_receipt(self, value, operation, state, *, action=None, confirmed=True):
        if not isinstance(value, dict) or not isinstance(value.get("result"), dict):
            raise ValueError("missing exact durable operation result")
        result = value["result"]
        action = action or ("pause" if state in {"closed", "closing"} else "resume")
        if (type(result.get("schema")) is not int or result["schema"] != 1
                or result.get("manifest") != self.manifest or result.get("operation_id") != operation
                or result.get("state") != state or result.get("durable") is not True
                or value.get("confirmed") is not confirmed or result.get("confirmed") is not confirmed
                or result.get("action") != action
                or type(result.get("generation")) is not int or type(result.get("previous_generation")) is not int
                or result["previous_generation"] < -1 or result["generation"] != result["previous_generation"] + 1
                or type(result.get("active_leases")) is not int or result["active_leases"] < 0
                or type(result.get("uncertain_leases")) is not int or result["uncertain_leases"] < 0):
            raise ValueError("receipt identity/state mismatch")
        if state == "closed" and (type(result.get("active_leases")) is not int or result["active_leases"] != 0
                or type(result.get("uncertain_leases")) is not int or result["uncertain_leases"] != 0):
            raise ValueError("closed does not prove actual empty lease registry")
        return result

    def query_closed(self, operation):
        value = self.private_control("/operations/" + operation, expected=(200, 409))
        if value.get("confirmed") is False:
            self.checked_receipt(value, operation, "closing", confirmed=False)
            return None
        result = self.checked_receipt(value, operation, "closed")
        snapshot = self.gate_snapshot()
        if len(snapshot["state"]) != 1 or snapshot["state"][0]["phase"] != "closed":
            raise ValueError("HTTP and actual PostgreSQL gate state differ")
        if any(row["status"] != "finished" for row in snapshot["leases"]):
            raise ValueError("active or uncertain actual lease remains")
        return {"receipt": result, "postgres": snapshot}

    def refresh(self, expected=204):
        return self.http("sender", f"/v1/feeds/{self.feed_id}/refresh", "PUT", expected=expected)

    def setup_cases(self):
        self.http("sender", "/v1/categories", expected=503)
        registration = self.private_control("/registration")
        if registration != {"schema": 1, "manifest": self.manifest, "protocol": "miniflux-pg-handoff-v1", "business_open_not_assumed": True}:
            raise ValueError("actual private registration differs")
        operation = "1" * 64
        initialized = self.checked_receipt(self.private_control("/initialize", "POST", {"operation_id": operation}), operation, "open", action="initialize")
        category = self.http("sender", "/v1/categories")[0]["id"]
        self.feed_id = self.http("sender", "/v1/feeds", "POST", {"feed_url": "http://fixture:8080/feed", "category_id": category}, expected=201)["feed_id"]
        self.sql("UPDATE integrations SET webhook_enabled=true,webhook_url='http://fixture:8080/hook',"
                 "webhook_secret='synthetic-webhook-secret' WHERE user_id=1;")
        self.items = ["seed", "manual"]
        self.control(self.items)
        self.refresh()
        manual = self.wait(lambda: self.proved_row("manual"))
        self.entry_id = manual["entry_id"]
        self.case("INITIAL_CLOSED_EXPLICIT_OPEN_AND_MANUAL", {"registration": registration, "initialize": initialized,
                  "outbox": manual, "postgres": self.gate_snapshot(), "ordinary_202_not_used": True})

    def scheduler_case(self):
        self.items.append("scheduler")
        self.control(self.items)
        self.sql("UPDATE feeds SET next_check_at=clock_timestamp()-interval '1 second' WHERE id=" + str(self.feed_id) + ";")
        row = self.wait(lambda: self.proved_row("scheduler"), seconds=75)
        snapshot = self.gate_snapshot()
        sources = [v["source"] for v in snapshot["leases"] if v["status"] == "finished"]
        if "scheduler" not in sources or "queued_refresh" not in sources:
            raise ValueError("actual timer and queued-worker lease chain absent")
        self.case("REAL_SCHEDULER", {"outbox": row, "postgres": snapshot, "fixture_events": self.events(),
                                     "timer_frequency_seconds": 60, "manual_refresh_used_for_this_item": False})

    def legacy_async_case(self):
        self.sql("UPDATE integrations SET readeck_enabled=true,readeck_only_url=true,"
                 "readeck_url='http://fixture:8080',readeck_api_key='synthetic-only' WHERE user_id=1;")
        self.control(self.items, hold=True)
        self.http("sender", f"/v1/entries/{self.entry_id}/save", "POST", expected=202)
        received = self.wait(lambda: next((r for r in self.events() if r["kind"] == "legacy_received" and r["held"]), None), seconds=4)
        operation = "2" * 64
        pending = self.private_control("/pause", "POST", {"operation_id": operation}, expected=409)
        self.checked_receipt(pending, operation, "closing", confirmed=False)
        snapshot = self.gate_snapshot()
        if not any(r["source"] == "manual_save" and r["status"] == "active" for r in snapshot["leases"]):
            raise ValueError("legacy asynchronous activity not registered")
        self.refresh(expected=503)
        self.control(self.items, release=True)
        result = self.wait(lambda: self.query_closed(operation), seconds=15)
        originals = json.loads(self.sql("""SELECT COALESCE(json_agg(x),'[]') FROM
          (SELECT delivery_id,body_sha256,event_type,user_id,feed_id,entry_id,target_url,signature,contract,state,reason,
           replace(encode(body,'base64'),E'\\n','') AS body_b64,encode(sha256(body),'hex') AS actual_body_sha256
           FROM webhook_legacy_originals) x;"""))
        if len(originals) != 1 or originals[0]["state"] != "held" or originals[0]["event_type"] != "save_entry":
            raise ValueError("unsupported legacy webhook original not retained held")
        original = originals[0]
        if original["delivery_id"] != original["actual_body_sha256"] or original["body_sha256"] != original["delivery_id"]:
            raise ValueError("legacy exact-body identity mismatch")
        raw = base64.b64decode(original["body_b64"])
        payload = json.loads(raw)
        if (original["signature"] != hmac.new(b"synthetic-webhook-secret", raw, hashlib.sha256).hexdigest()
                or payload["event_type"] != "save_entry" or payload["entry"]["id"] != self.entry_id
                or original["entry_id"] != self.entry_id or not payload["entry"]["url"].endswith("/manual")):
            raise ValueError("legacy payload/HMAC/entry identity mismatch")
        if any(r["kind"] == "webhook_received" and r["event"] == "save_entry" for r in self.events()):
            raise ValueError("unsupported save_entry was sent as if receiver supported it")
        self.case("LEGACY_ASYNC_AND_HELD_ORIGINAL", {"inflight_handshake": received, "pending": pending,
                  "inflight_postgres": snapshot, "closed": result, "held_original": original})
        return operation

    def restart_closed_case(self, closed):
        before = json.loads(self.command(["inspect", self.names["sender"]]))[0]
        self.sample_memory("sender", "before_restart")
        self.command(["stop", "--time=15", self.names["sender"]], timeout=20)
        self.command(["start", self.names["sender"]])
        self.wait(lambda: self.http("sender", "/healthcheck"), readiness=True)
        self.refresh(expected=503)
        result = self.wait(lambda: self.query_closed(closed))
        after = json.loads(self.command(["inspect", self.names["sender"]]))[0]
        resource_checks(after, self.image_id, self.owner, self.network)
        if before["Id"] != after["Id"] or before["State"]["StartedAt"] == after["State"]["StartedAt"]:
            raise ValueError("actual same-image process restart not observed")
        self.case("SENDER_RESTART_STAYS_CLOSED", {"before": self.safe_inspect(before), "after": self.safe_inspect(after), "closed": result})

    def resume(self, prior, operation):
        bad = self.private_control("/resume", "POST", {"closed_operation_id": "f" * 64, "operation_id": "e" * 64}, expected=409)
        if bad.get("confirmed") is not False:
            raise ValueError("foreign closed operation accepted")
        value = self.private_control("/resume", "POST", {"closed_operation_id": prior, "operation_id": operation})
        return self.checked_receipt(value, operation, "open")

    def outbox_inflight_case(self, prior):
        resumed = self.resume(prior, "3" * 64)
        self.sql("UPDATE integrations SET readeck_enabled=false WHERE user_id=1;")
        self.items.append("outbox_inflight")
        self.control(self.items, hold=True)
        self.refresh()
        received = self.wait(lambda: next((r for r in self.events() if r["kind"] == "webhook_received" and r["held"]), None), seconds=4)
        operation = "4" * 64
        pending = self.private_control("/pause", "POST", {"operation_id": operation}, expected=409)
        self.checked_receipt(pending, operation, "closing", confirmed=False)
        snapshot = self.gate_snapshot()
        self.control(self.items, release=True)
        result = self.wait(lambda: self.query_closed(operation), seconds=15)
        row = self.wait(lambda: self.proved_row("outbox_inflight"))
        self.case("OUTBOX_INFLIGHT_NO_LOSS", {"resume": resumed, "handshake": received, "pending": pending,
                  "inflight_postgres": snapshot, "closed": result, "acked_outbox": row})
        return operation

    def checked_draining(self, value, operation, *, confirmed=True):
        result = self.checked_receipt(value, operation, "draining", action="stop_new", confirmed=confirmed)
        for field in ("active_producer_leases", "uncertain_producer_leases"):
            if type(result.get(field)) is not int or result[field] < 0 or (confirmed and result[field] != 0):
                raise ValueError("stop-new lacks exact producer completion counts")
        return result

    def stop_new_drain_case(self, prior):
        # Operation identities are retained across every native scenario. Keep
        # this added scenario separate from the existing joint-host 5/6 IDs.
        resumed = self.resume(prior, hashlib.sha256(b"native:stop-new-drain:resume").hexdigest())
        self.items.extend(["drain_first", "drain_queued"])
        self.control(self.items, hold=True)
        self.refresh()
        received = self.wait(lambda: next((r for r in self.events()
            if r["kind"] == "webhook_received" and r["held"]
            and any(json.loads(base64.b64decode(r["body_b64"]))["entries"][0]["url"].endswith("/" + key)
                    for key in ("drain_first", "drain_queued"))), None), seconds=4)
        backlog = [r for r in self.outbox() if any(
            json.loads(base64.b64decode(r["body_b64"]))["entries"][0]["url"].endswith("/" + key)
            for key in ("drain_first", "drain_queued"))]
        if len(backlog) != 2 or not any(r["attempts"] == 0 and r["state"] == "pending" for r in backlog):
            raise ValueError("no actual unclaimed durable backlog before stop-new")
        operation = hashlib.sha256(b"native:stop-new-drain:stop-new").hexdigest()
        pending = self.private_control("/stop-new", "POST", {"operation_id": operation}, expected=(200, 409))
        self.checked_draining(pending, operation, confirmed=pending.get("confirmed") is True)
        self.refresh(expected=503)
        self.http("sender", f"/v1/entries/{self.entry_id}/save", "POST", expected=503)
        self.control(self.items, release=True)
        rows = [self.wait(lambda key=key: self.proved_row(key)) for key in ("drain_first", "drain_queued")]
        if {r["body_sha256"] for r in rows} != {r["body_sha256"] for r in backlog}:
            raise ValueError("drain changed original durable body identity")
        result = self.checked_draining(self.private_control("/operations/" + operation), operation)
        before = self.gate_snapshot()
        if before["state"][0]["phase"] != "draining":
            raise ValueError("stop-new did not retain durable draining phase")
        # A real scheduler timer must reach the closed producer boundary.
        baseline = self.command(["logs", self.names["sender"]]).count("Handoff scheduler admission refused")
        feeds_before = len([r for r in self.events() if r["kind"] == "feed"])
        self.sql("UPDATE feeds SET next_check_at=clock_timestamp()-interval '1 second' WHERE id=" + str(self.feed_id) + ";")
        logs = self.wait(lambda: r if (r := self.command(["logs", self.names["sender"]])).count("Handoff scheduler admission refused") > baseline else None, seconds=75)
        if len([r for r in self.events() if r["kind"] == "feed"]) != feeds_before:
            raise ValueError("draining scheduler produced another feed request")
        self.sample_memory("sender", "before_draining_restart")
        self.command(["stop", "--time=15", self.names["sender"]], timeout=20)
        self.command(["start", self.names["sender"]])
        self.wait(lambda: self.http("sender", "/healthcheck"), readiness=True)
        self.refresh(expected=503)
        restarted = self.checked_draining(self.private_control("/operations/" + operation), operation)
        freeze = hashlib.sha256(b"native:stop-new-drain:freeze").hexdigest()
        self.private_control("/pause", "POST", {"operation_id": freeze}, expected=(200, 409))
        closed = self.wait(lambda: self.query_closed(freeze))
        self.case("STOP_NEW_DRAINS_BACKLOG_AND_RESTART", {"resume": resumed, "inflight_handshake": received,
            "backlog_before_stop_new": backlog, "stop_new_response": pending, "confirmed_stop_new": result,
            "acked_originals": rows, "draining_postgres": before, "restart_draining": restarted,
            "scheduler_refusal_observed": logs.count("Handoff scheduler admission refused") > baseline,
            "full_pause": closed, "stop_new_is_queue_empty_proof": False})
        return freeze

    def staged_host_bridge_case(self):
        from miniflux_handoff_adapter import MinifluxDrainControl, source_sha256 as bridge_source
        from miniflux_host_joint_native import component

        prior = self.gate_snapshot()["state"]
        if len(prior) != 1 or prior[0]["phase"] != "closed":
            raise ValueError("staged host bridge requires the actual prior closed operation")
        resume = hashlib.sha256(b"native:staged-host:resume").hexdigest()
        resumed = self.resume(prior[0]["current_operation"], resume)
        def inspect():
            return json.loads(self.command(["inspect", self.names["sender"]]))
        row = inspect()[0]
        binding = {"schema": 1, "scope": "instrumented_sender_only", "run_id": self.owner,
                   "control_id": "miniflux", "component_name": row["Name"].lstrip("/"), "component": component(row),
                   "manifest": self.manifest, "application_source_sha256": sha(self.repo / "ops/miniflux/pause-source-lock.json"),
                   "source_sha256": bridge_source(), "generation": resumed["generation"],
                   "initial_operation_id": resume, "entrances": ["miniflux_scheduler", "miniflux_manual", "legacy_async_sender"]}
        harness = self
        class Client:
            def request(self, method, path, payload):
                return harness.private_exchange(path, method, payload)
        control = MinifluxDrainControl(binding, Client(), inspect, self.output / "staged-host-bridge")
        operation = hashlib.sha256(b"native:staged-host:stop-new").hexdigest()
        stop = control.stop_new(operation, resumed["generation"], "actual synthetic host producer stop")
        queried_stop = control.query_stop_new(operation)
        self.refresh(expected=503)
        stopped_snapshot = self.gate_snapshot()
        freeze = hashlib.sha256(b"native:staged-host:freeze").hexdigest()
        paused = control.freeze(freeze, operation, "actual synthetic host full pause")
        queried_pause = control.query_freeze(freeze)
        closed = self.query_closed(freeze)
        self.case("ACTUAL_STAGED_HOST_BRIDGE", {"binding": binding, "producer_stop": stop,
            "queried_stop": queried_stop, "draining_postgres": stopped_snapshot,
            "pause": paused, "queried_pause": queried_pause, "closed": closed,
            "queue_checked": False, "legacy_originals_proven": False, "switch_authorized": False,
            "scope": "actual host bridge phases only; independent inventory and receiver transfer tested separately"})

    def postgres_restart_case(self, closed):
        before = self.gate_snapshot()
        self.sample_memory("pg", "before_restart")
        self.command(["stop", "--time=15", self.names["pg"]], timeout=20)
        # BasicAuth cannot verify credentials while PostgreSQL is unavailable.
        # Its exact 401 is not stop proof; verify the retained closed operation
        # and authenticated 503 only after the same database has restarted.
        self.http("sender", "/v1/categories", expected=401)
        unavailable = self.private_control("/operations/" + closed, expected=409)
        if unavailable.get("confirmed") is not False:
            raise ValueError("database unavailable returned confirmed stop")
        self.command(["start", self.names["pg"]])
        self.wait(self.postgres_ready, readiness=True)
        after = self.wait(lambda: self.query_closed(closed))
        self.refresh(expected=503)
        self.case("POSTGRES_RESTART_CLOSED_DURABLE", {"before": before, "unavailable": unavailable, "after": after,
                  "restart_kind": "graceful_owned_synthetic_postgres_not_power_loss"})

    def closed_scheduler_case(self):
        baseline = self.command(["logs", self.names["sender"]]).count("Handoff scheduler admission refused")
        before = len([r for r in self.events() if r["kind"] == "feed"])
        self.sql("UPDATE feeds SET next_check_at=clock_timestamp()-interval '1 second' WHERE id=" + str(self.feed_id) + ";")
        logs = self.wait(lambda: r if (r := self.command(["logs", self.names["sender"]])).count("Handoff scheduler admission refused") > baseline else None, seconds=75)
        after = len([r for r in self.events() if r["kind"] == "feed"])
        if after != before:
            raise ValueError("closed scheduler still dispatched a feed request")
        self.case("ACTUAL_SCHEDULER_REFUSED", {"refused_before": baseline, "refused_after": logs.count("Handoff scheduler admission refused"),
                  "feed_requests_before": before, "feed_requests_after": after, "postgres": self.gate_snapshot()})

    def sample_memory(self, name, phase):
        peak = self.command(["exec", self.names[name], "cat", "/sys/fs/cgroup/memory.peak"], final=True)
        events = self.command(["exec", self.names[name], "cat", "/sys/fs/cgroup/memory.events"], final=True)
        evidence = memory_evidence(peak, events)
        counts = evidence["events"]
        save(self.output / ("MEMORY_" + name + "_" + phase + ".json"), {**evidence,
             "phase": phase, "exact_continuous_sampling_claimed": False, "observed_utc": datetime.now(timezone.utc).isoformat()})
        if counts["oom"] != 0 or counts["oom_kill"] != 0 or counts.get("oom_group_kill", 0) != 0:
            raise ValueError("actual cgroup OOM observed")

    def preserve(self):
        # Only exact run-owned containers; no delete/prune and no unrelated stop.
        errors = []
        for name in reversed(self.created):
            try:
                raw = self.command(["inspect", self.names[name]], final=True)
                row = json.loads(raw)[0]
                if row["Config"]["Labels"].get(LABEL) != self.owner:
                    raise ValueError("owned identity changed; cannot stop")
                save(self.output / ("FINAL_" + name + ".json"), self.safe_inspect(row))
            except Exception as error:
                save(self.output / ("PRESERVE_ERROR_" + name + ".json"), {"error": str(error)})
                errors.append(name + ":inspect")
                continue
            try:
                if row["State"]["Running"]:
                    self.sample_memory(name, "final")
                self.command(["logs", self.names[name]], final=True)
            except Exception as error:
                save(self.output / ("PRESERVE_OBSERVATION_ERROR_" + name + ".json"), {"error": str(error)})
                errors.append(name + ":observation")
            finally:
                if row["State"]["Running"]:
                    try:
                        self.command(["stop", "--time=15", self.names[name]], timeout=20, final=True)
                    except Exception as error:
                        save(self.output / ("PRESERVE_STOP_ERROR_" + name + ".json"), {"error": str(error)})
                        errors.append(name + ":stop_unconfirmed")
        return errors


def source_binding(repo, source, binary):
    revision = subprocess.check_output(["git", "-C", str(repo), "rev-parse", "HEAD"], text=True, timeout=10).strip()
    if subprocess.check_output(["git", "-C", str(repo), "status", "--porcelain"], timeout=10).strip():
        raise ValueError("CI harness repository must be clean")
    lock_path = repo / "ops/miniflux/pause-source-lock.json"
    lock = json.loads(lock_path.read_text())
    upstream = subprocess.check_output(["git", "-C", str(source), "rev-parse", "HEAD"], text=True, timeout=10).strip()
    if upstream != lock["revision"]:
        raise ValueError("actual upstream revision differs")
    for name, digest in lock["patched_files_sha256"].items():
        if sha(source / name) != digest:
            raise ValueError("actual patched source differs")
    names = ("scripts/verify_miniflux_pause_native.py", "scripts/miniflux_pause_fixture.py",
             "scripts/verify_handoff_operations_native.py",
             "scripts/miniflux_dump_selection.py",
             "scripts/miniflux_host_joint_native.py", "scripts/miniflux_handoff_adapter.py",
             "scripts/handoff_host_cron.py", "scripts/handoff_runtime_adapter.py", "scripts/durable_handoff_cutover.py",
             "scripts/apply_miniflux_pause.py", "ops/miniflux/pause-source-lock.json",
             "ops/miniflux/source-lock.json", "ops/miniflux/Dockerfile", ".github/workflows/miniflux-pause.yml")
    return {"revision": revision, "upstream_revision": upstream, "binary_sha256": sha(binary),
            "source_sha256": {name: sha(repo / name) for name in names}, "patch_sha256": lock["patch_sha256"],
            "production_access": False, "models": 0, "existing_126_modified": False}


def run(repo, source, image_id, binary, output):
    binding = source_binding(repo, source, binary)
    harness = Harness(repo, source, image_id, binary, output)
    save(output / "SOURCE.json", binding)
    error = None
    try:
        harness.prepare()
        harness.launch_sender()
        harness.setup_cases()
        harness.scheduler_case()
        closed = harness.legacy_async_case()
        harness.restart_closed_case(closed)
        closed = harness.outbox_inflight_case(closed)
        closed = harness.stop_new_drain_case(closed)
        harness.postgres_restart_case(closed)
        harness.closed_scheduler_case()
        from miniflux_host_joint_native import run as joint_run
        joint_run(harness, closed)
        harness.staged_host_bridge_case()
    except Exception as failure:
        error = {"type": type(failure).__name__, "message": harness.redacted(str(failure))}
        save(output / "FAILURE.json", error)
    finally:
        evidence_errors = []
        if "pg" in harness.names:
            try:
                harness.command(["exec", harness.names["pg"], "pg_dump", "-U", "synthetic", "-d", "synthetic_pause"], timeout=30, final=True)
            except Exception as failure:
                evidence_errors.append("synthetic_pg_dump:" + str(failure))
        evidence_errors.extend(harness.preserve())
        if error is None and evidence_errors:
            error = {"type": "EvidencePreservationIncomplete", "message": str(evidence_errors)}
    cases = sorted(path.name for path in output.glob("CASE_*.json"))
    result = {"status": "passed" if error is None and len(cases) == 10 else "failed_or_evidence_insufficient",
              "cases": cases, "error": error, "evidence_errors": evidence_errors, "elapsed_seconds": time.monotonic() - harness.started,
              "native_amd64": True, "resource_limits": {"sender_memory_bytes": MEMORY, "sender_pids": PIDS},
              "retained_network": harness.network, "retained_containers": list(harness.names.values()),
              "fixture_ack_is_actual_receiver_proof": False, "power_loss_proven": False,
              "production_access": False, "models": 0, "existing_126_modified": False, "deployment": False}
    save(output / "RESULT.json", result)
    return result


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--repo", type=Path, default=Path(__file__).resolve().parents[1])
    parser.add_argument("--source", type=Path, required=True)
    parser.add_argument("--sender-image", required=True)
    parser.add_argument("--binary", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--run", action="store_true")
    args = parser.parse_args()
    if not args.run:
        print(json.dumps({"status": "plan_only", "source": str(args.source), "sender_image": args.sender_image,
                          "native_amd64_required": True, "production_access": False, "models": 0}))
        return 0
    result = run(args.repo.resolve(), args.source.resolve(), args.sender_image, args.binary.resolve(), args.output.resolve())
    print(json.dumps(result))
    return 0 if result["status"] == "passed" else 2


if __name__ == "__main__":
    raise SystemExit(main())
