"""One controlled gateway switch, independent of the application project.

Only fixed Docker operations and GET /health are used. Original gateway and
configuration are retained. An unknown create/start/TERM can only be queried;
query never advances a stage which was not already attempted.
"""

from __future__ import annotations

import hashlib
from contextlib import contextmanager
import fcntl
import http.client
import ipaddress
import json
import os
from pathlib import Path
import re
import selectors
import signal
import stat
import subprocess
import time

from services import admission_fence as gate, production_activation as runtime
from services.production_activation_control import ReceiverActivationSession
from scripts import private_release_contract as private, retained_gateway_configuration as retained_config

NGINX_REF = "nginx:stable-alpine@sha256:0d3b80406a13a767339fbe2f41406d6c7da727ab89cf8fae399e81f780f814d1"
DOCKER = ("/usr/bin/docker", "--host", "unix:///var/run/docker.sock")
MAX_OUTPUT = 4 * 1024**2
CONFIG_TARGET = "/etc/nginx/nginx.conf"
PLAN_KEYS = {
    "schema",
    "operation_id",
    "source_sha256",
    "source_sha",
    "window",
    "old_gateway_id",
    "old_gateway_identity",
    "old_config",
    "new_config",
    "api_alias",
    "network_id",
    "wait_seconds",
    "max_polls",
}


@contextmanager
def _hard_deadline(seconds):
    """Bound blocking process creation and HTTP header reads on the host main thread."""
    gate._require(seconds > 0, "gateway overall deadline expired")
    old_handler, old_timer = signal.getsignal(signal.SIGALRM), signal.getitimer(signal.ITIMER_REAL)
    started = time.monotonic()

    def expired(_signal, _frame):
        raise gate.AdmissionUncertain("gateway blocking operation deadline; outcome unknown")

    signal.signal(signal.SIGALRM, expired)
    signal.setitimer(signal.ITIMER_REAL, min(seconds, old_timer[0]) if old_timer[0] else seconds)
    try:
        yield
    finally:
        signal.setitimer(signal.ITIMER_REAL, 0)
        signal.signal(signal.SIGALRM, old_handler)
        if old_timer[0]:
            signal.setitimer(signal.ITIMER_REAL, max(0.001, old_timer[0] - (time.monotonic() - started)), old_timer[1])


def source_sha256():
    return hashlib.sha256(Path(__file__).read_bytes()).hexdigest()


def gateway_name(operation):
    gate._require(isinstance(operation, str) and gate.HEX64.fullmatch(operation), "exact gateway operation required")
    return "controlled-gateway-" + operation[:12] + "-gateway"


def render_config(operation, api_alias):
    """Frozen before build; actual DNS ownership is independently checked later."""
    gateway_name(operation)
    gate._require(
        isinstance(api_alias, str) and re.fullmatch(r"[a-z][a-z0-9_-]{0,62}", api_alias),
        "fixed candidate API DNS alias required",
    )
    return (
        """worker_processes 1;
worker_shutdown_timeout 10s;
pid /tmp/nginx.pid;
events { worker_connections 1024; }
http {
  include /etc/nginx/mime.types;
  default_type application/octet-stream;
  server_tokens off;
  map_hash_bucket_size 128;
  access_log /dev/stdout;
  error_log /dev/stderr warn;
  resolver 127.0.0.11 valid=10s ipv6=off;
  map $http_x_ai_filter_gateway_probe $controlled_operation { default \"\"; OPERATION OPERATION; }
  map $http_x_ai_filter_gateway_probe $controlled_upstream { default \"\"; OPERATION $upstream_addr; }
  server {
    listen 5000;
    client_max_body_size 1m;
    location / {
      set $api_upstream http://API_ALIAS:5000;
      proxy_pass $api_upstream$request_uri;
      proxy_http_version 1.1;
      proxy_set_header Host $host;
      proxy_set_header X-Real-IP $remote_addr;
      proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;
      proxy_set_header X-Forwarded-Proto $http_x_forwarded_proto;
      proxy_set_header X-AI-Filter-Client-IP $http_x_real_ip;
      proxy_set_header Connection \"\";
      proxy_connect_timeout 3s;
      proxy_read_timeout 120s;
      proxy_send_timeout 120s;
      add_header X-AI-Filter-Gateway-Operation $controlled_operation always;
      add_header X-AI-Filter-Upstream $controlled_upstream always;
    }
  }
}
""".replace("OPERATION", operation).replace("API_ALIAS", api_alias)
    ).encode()


def file_identity(path):
    path = private.safe_path(path)
    fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW)
    try:
        before = os.fstat(fd)
        gate._require(
            stat.S_ISREG(before.st_mode)
            and before.st_uid == os.geteuid()
            and before.st_nlink == 1
            and stat.S_IMODE(before.st_mode) in {0o400, 0o444, 0o600, 0o644}
            and 0 < before.st_size <= 65536,
            "unsafe gateway configuration original",
        )
        raw = os.read(fd, 65537)
        after = os.fstat(fd)
        fields = ("st_dev", "st_ino", "st_size", "st_mtime_ns", "st_ctime_ns")
        gate._require(
            all(getattr(before, k) == getattr(after, k) for k in fields) and len(raw) == before.st_size,
            "gateway configuration changed while reading",
        )
        return {
            "path": str(path),
            "device": before.st_dev,
            "inode": before.st_ino,
            "uid": before.st_uid,
            "mode": stat.S_IMODE(before.st_mode),
            "size": before.st_size,
            "mtime_ns": before.st_mtime_ns,
            "ctime_ns": before.st_ctime_ns,
            "sha256": hashlib.sha256(raw).hexdigest(),
        }, raw
    finally:
        os.close(fd)


def gateway_identity(row):
    """Stable across a natural stop, retaining all configuration/resource hashes."""
    return {
        "id": row["Id"],
        "image_id": row["Image"],
        "name": row["Name"],
        "started_at": row["State"]["StartedAt"],
        "restart_count": row["RestartCount"],
        "config_sha256": gate._hash(row["Config"]),
        "host_sha256": gate._hash(row["HostConfig"]),
        "mounts_sha256": gate._hash(row["Mounts"]),
        "networks": sorted(row["NetworkSettings"]["Networks"]),
    }


class NativeTransport:
    def __init__(self):
        self.deadline = time.monotonic() + 120
        self.last_result = None
        self.last_probe_body = None

    def run(self, args, *, seconds=3):
        remaining = min(seconds, self.deadline - time.monotonic())
        gate._require(remaining > 0, "gateway overall deadline expired")
        with _hard_deadline(remaining):
            return self._run(args, remaining)

    def _run(self, args, remaining):
        process = subprocess.Popen(
            [*DOCKER, *args],
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            env={"PATH": "/usr/bin:/bin", "LC_ALL": "C"},
        )
        output, errors, total, deadline = bytearray(), bytearray(), 0, time.monotonic() + remaining
        self.last_result = None
        try:
            with selectors.DefaultSelector() as poll:
                for stream in (process.stdout, process.stderr):
                    poll.register(stream, selectors.EVENT_READ)
                while poll.get_map():
                    gate._require(time.monotonic() < deadline, "gateway Docker deadline; outcome unknown")
                    for key, _ in poll.select(max(0, deadline - time.monotonic())):
                        data = os.read(key.fileobj.fileno(), 65536)
                        total += len(data)
                        gate._require(total <= MAX_OUTPUT, "gateway Docker output capacity")
                        if not data:
                            poll.unregister(key.fileobj)
                        elif key.fileobj is process.stdout:
                            output.extend(data)
                        else:
                            errors.extend(data)
            gate._require(
                process.wait(timeout=max(0.001, deadline - time.monotonic())) == 0,
                "gateway Docker operation failed; query original only",
            )
            return bytes(output)
        finally:
            self.last_result = {"stdout": bytes(output), "stderr": bytes(errors), "returncode": process.poll()}
            if process.poll() is None:
                process.kill()  # CLI only. Never docker kill --signal KILL.
            process.wait(timeout=1)
            process.stdout.close()
            process.stderr.close()

    def inspect(self, identifier):
        gate._require(gate.HEX64.fullmatch(identifier), "exact container ID required")
        rows = json.loads(self.run(["inspect", identifier]))
        gate._require(len(rows) == 1 and rows[0]["Id"] == identifier, "exact gateway container missing")
        return rows[0]

    def archive(self, identifier, container_path, consumer):
        """Fixed tar GET only, with no CLI/buffered payload prefetch."""
        deadline = min(self.deadline, time.monotonic() + 3)
        with _hard_deadline(deadline - time.monotonic()):
            return retained_config.read_native_archive(identifier, container_path, consumer, deadline=deadline)

    def image(self):
        rows = json.loads(self.run(["image", "inspect", NGINX_REF]))
        gate._require(
            len(rows) == 1 and re.fullmatch(r"sha256:[0-9a-f]{64}", rows[0]["Id"]), "fixed nginx image missing"
        )
        return rows[0]

    def network_members(self, identifier):
        rows = json.loads(self.run(["network", "inspect", identifier]))
        gate._require(
            len(rows) == 1
            and rows[0]["Id"] == identifier
            and rows[0]["Driver"] == "bridge"
            and rows[0]["Scope"] == "local",
            "exact local gateway network required",
        )
        identifiers = sorted((rows[0].get("Containers") or {}).keys())
        gate._require(
            0 < len(identifiers) <= 64 and all(gate.HEX64.fullmatch(i) for i in identifiers),
            "bounded complete gateway network inventory required",
        )
        members = json.loads(self.run(["inspect", *identifiers]))
        gate._require(
            len(members) == len(identifiers) and sorted(r["Id"] for r in members) == identifiers,
            "incomplete network membership inspect",
        )
        return rows[0], members

    def find(self, operation, purpose):
        gate._require(purpose in {"gateway", "config-test"}, "fixed gateway purpose required")
        raw = self.run(
            [
                "ps",
                "-a",
                "--no-trunc",
                "--filter",
                "label=io.ai-filter.gateway.operation=" + operation,
                "--filter",
                "label=io.ai-filter.gateway.purpose=" + purpose,
                "--format",
                "{{.ID}}",
            ]
        )
        ids = raw.decode().splitlines()
        gate._require(
            len(ids) <= 1 and all(gate.HEX64.fullmatch(i) for i in ids), "gateway operation identity ambiguous"
        )
        return self.inspect(ids[0]) if ids else None

    def health(self, host, port, operation):
        deadline = min(self.deadline, time.monotonic() + 3)
        with _hard_deadline(deadline - time.monotonic()):
            return self._health(host, port, operation, deadline)

    def _health(self, host, port, operation, deadline):
        connection = http.client.HTTPConnection(host, port, timeout=max(0.001, deadline - time.monotonic()))
        try:
            connection.request(
                "GET", "/health", headers={"X-AI-Filter-Gateway-Probe": operation, "Connection": "close"}
            )
            response = connection.getresponse()
            body = bytearray()
            while len(body) <= 65536:
                gate._require(time.monotonic() < deadline, "gateway HTTP deadline")
                if connection.sock:
                    connection.sock.settimeout(max(0.001, deadline - time.monotonic()))
                chunk = response.read1(min(4096, 65537 - len(body)))
                if not chunk:
                    break
                body.extend(chunk)
            gate._require(len(body) <= 65536 and time.monotonic() < deadline, "gateway HTTP bound")
            self.last_probe_body = bytes(body)
            return {
                "status": response.status,
                "body_sha256": hashlib.sha256(body).hexdigest(),
                "operation": response.getheader("X-AI-Filter-Gateway-Operation"),
                "upstream": response.getheader("X-AI-Filter-Upstream"),
            }
        finally:
            connection.close()


def _ports(row):
    mapping = row["HostConfig"].get("PortBindings")
    gate._require(
        isinstance(mapping, dict) and set(mapping) == {"5000/tcp"} and 1 <= len(mapping["5000/tcp"]) <= 4,
        "exact gateway port5000 bindings required",
    )
    result = []
    for item in mapping["5000/tcp"]:
        ip = ipaddress.ip_address(item["HostIp"])
        gate._require(
            ip.version == 4
            and (ip.is_private or ip.is_loopback)
            and not ip.is_unspecified
            and not ip.is_multicast
            and str(int(item["HostPort"])) == item["HostPort"]
            and 1 <= int(item["HostPort"]) <= 65535,
            "private explicit gateway host binding required",
        )
        result.append({"HostIp": str(ip), "HostPort": item["HostPort"]})
    gate._require(len({(r["HostIp"], r["HostPort"]) for r in result}) == len(result), "duplicate gateway host port")
    return sorted(result, key=lambda item: (item["HostIp"], item["HostPort"]))


def _execution(row):
    host = row["HostConfig"]
    gate._require(
        type(host["Memory"]) is int
        and 0 < host["Memory"] <= 128 * 1024**2
        and type(host["MemorySwap"]) is int
        and host["Memory"] <= host["MemorySwap"] <= host["Memory"] * 2
        and type(host["PidsLimit"]) is int
        and 0 < host["PidsLimit"] <= 64
        and type(host["NanoCpus"]) is int
        and 0 <= host["NanoCpus"] <= 10**9,
        "fixed gateway resource limits required",
    )
    gate._require(
        not host.get("Privileged")
        and not host.get("CapAdd")
        and host.get("CapDrop") in (None, [], ["ALL"])
        and host.get("SecurityOpt") in (["no-new-privileges:true"], ["no-new-privileges"])
        and not host.get("Devices")
        and not host.get("PidMode")
        and not host.get("UsernsMode")
        and host.get("IpcMode") in ("private", "")
        and not host.get("Tmpfs")
        and not host.get("CpusetCpus")
        and not host.get("CpuShares")
        and not host.get("CpuPeriod")
        and not host.get("CpuQuota")
        and not host.get("CpuRealtimePeriod")
        and not host.get("CpuRealtimeRuntime")
        and not host.get("CpusetMems")
        and not host.get("Ulimits")
        and not host.get("Sysctls")
        and not host.get("CgroupParent")
        and not host.get("Cgroup")
        and not host.get("DeviceRequests")
        and not host.get("DeviceCgroupRules")
        and not host.get("BlkioWeight")
        and not host.get("Dns")
        and not host.get("DnsOptions")
        and not host.get("DnsSearch")
        and not host.get("ExtraHosts")
        and not host.get("Links")
        and not host.get("VolumesFrom")
        and not host.get("OomKillDisable")
        and not host.get("OomScoreAdj")
        and host.get("MemorySwappiness") in (None, -1)
        and not host.get("MemoryReservation"),
        "unsupported old gateway security/resource customization",
    )
    return {
        "memory": host["Memory"],
        "swap": host["MemorySwap"],
        "pids": host["PidsLimit"],
        "nano_cpus": host["NanoCpus"],
        "user": row["Config"].get("User", ""),
        "read_only": host["ReadonlyRootfs"],
        "cap_drop": host.get("CapDrop") or [],
        "security": ["no-new-privileges:true"],
    }


PROBE_TEXT_LIMIT, PROBE_RECORD_LIMIT = 256, 8192


def _probe_observation(port, proof, expected, body):
    proof = proof if isinstance(proof, dict) else {}
    upstream = proof.get("upstream")
    row = {
        "binding": port,
        "status": proof.get("status"),
        "body_sha256": proof.get("body_sha256"),
        "operation_matches": proof.get("operation") == expected["operation"],
        "operation_present": proof.get("operation") not in (None, ""),
        "upstream": None if upstream is None else str(upstream)[:PROBE_TEXT_LIMIT],
        "upstream_expected": expected["upstream"],
        "upstream_matches": upstream == expected["upstream"],
        "failed_checks": sorted(key for key in expected if proof.get(key) != expected[key]),
    }
    if isinstance(body, (bytes, bytearray)):
        row["body_bytes"] = len(body)
        if len(body) <= 64:
            row["body_text"] = bytes(body).decode("utf-8", errors="replace")
    return row


def _save_raw(path, value):
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
    with os.fdopen(fd, "wb") as stream:
        stream.write(value)
        stream.flush()
        os.fsync(stream.fileno())
    gate._sync_directory(path.parent)


class GatewaySwitch:
    def __init__(self, receiver_session, plan, evidence_root, *, transport=None):
        gate._require(
            type(receiver_session) is ReceiverActivationSession, "real receiver activation capability required"
        )
        gate._require(
            isinstance(plan, dict)
            and set(plan) == PLAN_KEYS
            and type(plan["schema"]) is int
            and plan["schema"] == 1
            and plan["source_sha256"] == source_sha256(),
            "exact source-bound gateway plan required",
        )
        self.session, self.plan = receiver_session, gate._parsed(gate._encoded(plan))
        self.root, self.transport = Path(evidence_root), transport or NativeTransport()
        gate._require(type(self.transport) is NativeTransport, "fixed native gateway transport required")
        gate._directory(self.root)
        self.root_identity = private.directory_identity(self.root)
        self.operation, self.plan_sha = plan["operation_id"], gate._hash(plan)
        self.name = gateway_name(self.operation)
        self.project = self.name.removesuffix("-gateway")
        gate._require(
            gate.HEX64.fullmatch(plan["old_gateway_id"])
            and gate.HEX64.fullmatch(plan["network_id"])
            and plan["source_sha"] == receiver_session.permit["source"]["revision"]
            and type(plan["max_polls"]) is int
            and 1 <= plan["max_polls"] <= 16
            and type(plan["wait_seconds"]) is int
            and 1 <= plan["wait_seconds"] <= 30,
            "finite exact gateway identities/budgets required",
        )
        window = plan["window"]
        gate._require(
            set(window) == {"not_before", "expires_at"}
            and all(type(v) is int for v in window.values())
            and receiver_session.permit["not_before"]
            <= window["not_before"]
            < window["expires_at"]
            <= receiver_session.permit["expires_at"],
            "gateway window exceeds activation permission",
        )
        self.config = render_config(self.operation, plan["api_alias"])

    @contextmanager
    def _exclusive(self):
        self.check()
        path = self.root / "control.lock"
        fd = os.open(path, os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW, 0o600)
        try:
            info = os.fstat(fd)
            gate._require(
                stat.S_ISREG(info.st_mode)
                and info.st_nlink == 1
                and info.st_uid == os.geteuid()
                and stat.S_IMODE(info.st_mode) == 0o600
                and info.st_size == 0,
                "unsafe gateway evidence lock",
            )
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            gate._require(
                (path.lstat().st_dev, path.lstat().st_ino) == (info.st_dev, info.st_ino),
                "gateway evidence lock changed",
            )
            yield
        finally:
            os.close(fd)

    def check(self, *, active=False):
        gate._require(
            self.root_identity == private.directory_identity(self.root)
            and gate._hash(self.plan) == self.plan_sha
            and self.plan["source_sha256"] == source_sha256(),
            "gateway evidence directory/plan/source changed",
        )
        self.session._identity(active=active)
        if active:
            gate._require(
                self.plan["window"]["not_before"] <= time.time() < self.plan["window"]["expires_at"],
                "gateway write window expired",
            )
        old = self.plan["old_config"]
        if old.get("kind") == retained_config.KIND:
            result = retained_config.validate(old)
            gate._require(result["container_identity"] == self.plan["old_gateway_identity"]
                          and result["container_identity"]["id"] == self.plan["old_gateway_id"]
                          and result["network_id"] == self.plan["network_id"], "gateway archive proof binding differs")
        else:
            record, _raw = file_identity(old["path"])
            gate._require(record == old, "gateway original config inode or bytes changed")
        record, raw = file_identity(self.plan["new_config"]["path"])
        gate._require(record == self.plan["new_config"], "gateway original config inode or bytes changed")
        gate._require(raw == self.config, "gateway config is not the fixed reviewed routing template")

    def _api(self, *, gateway_id=None):
        owners = self.session._owners()
        ready = self.session._ready()
        confirmation = runtime.read_confirmed(self.session.root / "activation.confirm.json")
        gate._require(
            confirmation["slots"] == ready and confirmation["slots_sha256"] == gate._hash(ready),
            "actual prepared API owners changed",
        )
        ids = {
            value["container_id"] for slot, value in owners.items() if self.session.descriptor["slots"][slot] == "web"
        }
        gate._require(len(ids) == 1, "one exact granted API container required")
        identifier = ids.pop()
        network, members = self.transport.network_members(self.plan["network_id"])
        aliases, gateway_aliases, selected = [], [], None
        for row in members:
            for name, attachment in row["NetworkSettings"]["Networks"].items():
                if attachment.get("NetworkID") != self.plan["network_id"]:
                    continue
                names = set(attachment.get("Aliases") or []) | set(attachment.get("DNSNames") or [])
                if self.plan["api_alias"] in names:
                    aliases.append(row["Id"])
                if self.name in names:
                    gateway_aliases.append(row["Id"])
                if row["Id"] == identifier:
                    selected = (row, name, attachment)
        gate._require(
            aliases == [identifier] and selected is not None, "candidate DNS alias is not uniquely owned by granted API"
        )
        gate._require(
            gateway_aliases == ([] if gateway_id is None else [gateway_id]),
            "new gateway DNS alias is not uniquely owned by the original gateway operation",
        )
        row, name, attachment = selected
        environment = row["Config"].get("Env") or []
        for key, expected in (
            ("AI_FILTER_TRUSTED_PROXY_HOST", self.name),
            ("AI_FILTER_WEB_BASE_URL", "http://" + self.name + ":5000"),
        ):
            gate._require(
                [item for item in environment if item.startswith(key + "=")] == [key + "=" + expected],
                "candidate private environment does not bind the frozen gateway name",
            )
        gate._require(
            row["State"]["Running"]
            and not row["State"].get("Restarting")
            and row["RestartCount"] == 0
            and row["Image"] == self.session.permit["source"]["image_id"]
            and row["Config"]["Labels"].get("com.docker.compose.project") != self.project
            and attachment["EndpointID"]
            and ipaddress.ip_address(attachment["IPAddress"]).version == 4,
            "actual API execution/network endpoint differs",
        )
        return {
            "id": identifier,
            "image_id": row["Image"],
            "started_at": row["State"]["StartedAt"],
            "network_id": network["Id"],
            "network_name": name,
            "endpoint_id": attachment["EndpointID"],
            "address": attachment["IPAddress"],
            "activation_operation": self.session.operation_id,
            "descriptor_sha256": gate._hash(self.session.descriptor),
            "confirmation_sha256": gate._hash(confirmation),
        }

    def _old(self):
        row = self.transport.inspect(self.plan["old_gateway_id"])
        gate._require(
            gateway_identity(row) == self.plan["old_gateway_identity"], "retained old gateway identity changed"
        )
        mounts = row["Mounts"]
        if self.plan["old_config"].get("kind") == retained_config.KIND:
            retained_config.verify(self.plan["old_config"], row, inspect=self.transport.inspect,
                                   archive=self.transport.archive)
        else:
            gate._require(
                len(mounts) == 1
                and mounts[0]["Type"] == "bind"
                and mounts[0]["Destination"] == CONFIG_TARGET
                and mounts[0]["Source"] == self.plan["old_config"]["path"]
                and not mounts[0]["RW"],
                "old gateway configuration is not the retained bound mount",
            )
        gate._require(
            not row["State"].get("Paused")
            and not row["State"].get("Restarting")
            and not row["State"].get("Dead")
            and not row["State"].get("OOMKilled"),
            "old gateway state uncertain",
        )
        return row

    def _argv(self, execution, image, api, purpose):
        test = purpose == "config-test"
        name = self.name + ("-config-test" if test else "")
        args = [
            "create",
            "--pull",
            "never",
            "--name",
            name,
            "--restart",
            "no",
            "--no-healthcheck",
            "--label",
            "com.docker.compose.project=" + self.project,
            "--label",
            "com.docker.compose.service=" + purpose,
            "--label",
            "io.ai-filter.gateway.operation=" + self.operation,
            "--label",
            "io.ai-filter.gateway.plan=" + self.plan_sha,
            "--label",
            "io.ai-filter.gateway.purpose=" + purpose,
            "--label",
            "com.centurylinklabs.watchtower.enable=false",
            "--memory",
            str(execution["memory"]),
            "--memory-swap",
            str(execution["swap"]),
            "--pids-limit",
            str(execution["pids"]),
            "--security-opt",
            "no-new-privileges:true",
            "--network",
            "none" if test else api["network_id"],
            "--mount",
            "type=bind,src=" + self.plan["new_config"]["path"] + ",dst=" + CONFIG_TARGET + ",readonly",
        ]
        if execution["user"]:
            args += ["--user", execution["user"]]
        if execution["read_only"]:
            args += ["--read-only"]
        if execution["cap_drop"]:
            args += ["--cap-drop", "ALL"]
        if execution["nano_cpus"]:
            args += ["--cpus", str(execution["nano_cpus"] / 10**9)]
        if not test:
            args += ["--network-alias", self.name]
            for port in self.intent["ports"]:
                args += ["--publish", port["HostIp"] + ":" + port["HostPort"] + ":5000/tcp"]
        return [
            *args,
            "--entrypoint",
            "nginx",
            image,
            *(["-t", "-c", CONFIG_TARGET] if test else ["-g", "daemon off;"]),
        ]

    def _create(self, purpose):
        self.check(active=True)
        args = self._argv(self.intent["execution"], self.intent["image_id"], self.intent["api"], purpose)
        gate._write(
            self.root / (purpose + "-create.intent.json"),
            {"operation_id": self.operation, "argv_sha256": gate._hash(args)},
        )
        identifier = self._command(args, purpose + "-create").decode().strip()
        gate._require(gate.HEX64.fullmatch(identifier), "gateway create acknowledgment unknown")
        row = self.transport.inspect(identifier)
        self._created(row, purpose)
        gate._write(self.root / (purpose + "-created.json"), {"id": identifier, "operation_id": self.operation})
        return identifier

    def _created(self, row, purpose):
        labels = row["Config"]["Labels"]
        test = purpose == "config-test"
        gate._require(
            row["Name"] == "/" + self.name + ("-config-test" if test else "")
            and row["Image"] == self.intent["image_id"]
            and row["RestartCount"] == 0
            and labels.get("io.ai-filter.gateway.operation") == self.operation
            and labels.get("io.ai-filter.gateway.plan") == self.plan_sha
            and labels.get("io.ai-filter.gateway.purpose") == purpose
            and labels.get("com.docker.compose.project") == self.project
            and _execution(row) == self.intent["execution"]
            and row["Config"]["Entrypoint"] == ["nginx"]
            and row["Config"]["Cmd"] == (["-t", "-c", CONFIG_TARGET] if test else ["-g", "daemon off;"])
            and row["HostConfig"]["RestartPolicy"]["Name"] == "no",
            "new gateway identity/execution differs",
        )
        mounts = row["Mounts"]
        gate._require(
            len(mounts) == 1
            and mounts[0]["Type"] == "bind"
            and not mounts[0]["RW"]
            and mounts[0]["Source"] == self.plan["new_config"]["path"]
            and mounts[0]["Destination"] == CONFIG_TARGET,
            "new gateway configuration mount differs",
        )
        if test:
            gate._require(
                row["HostConfig"]["NetworkMode"] == "none" and not row["HostConfig"].get("PortBindings"),
                "configuration test must not occupy old ports",
            )
        else:
            gate._require(
                _ports(row) == self.intent["ports"]
                and set(row["NetworkSettings"]["Networks"]) == {self.intent["api"]["network_name"]},
                "gateway actual ports/network differ",
            )
            attachment = row["NetworkSettings"]["Networks"][self.intent["api"]["network_name"]]
            gate._require(
                row["HostConfig"]["NetworkMode"] == self.plan["network_id"]
                and attachment.get("NetworkID") in ("", self.plan["network_id"])
                and self.name in (attachment.get("Aliases") or []),
                "gateway network binding differs",
            )

    def _command(self, args, stage):
        """Retain only fixed mutation diagnostics; never persist inspect Env."""
        self.transport.last_result = None
        try:
            return self.transport.run(args)
        finally:
            result = self.transport.last_result
            if result is not None:
                for stream in ("stdout", "stderr"):
                    _save_raw(self.root / (stage + "." + stream), result[stream])
                gate._write(
                    self.root / (stage + ".command.json"),
                    {
                        "argv_sha256": gate._hash(args),
                        "returncode": result["returncode"],
                        "stdout_sha256": hashlib.sha256(result["stdout"]).hexdigest(),
                        "stderr_sha256": hashlib.sha256(result["stderr"]).hexdigest(),
                    },
                )

    def _record_probe(self, observed):
        """Bounded diagnostic of a failed probe; exported (capped, redacted) as gateway_command_stderr."""
        raw = (json.dumps({"schema": 1, "probes": observed[-8:]}, sort_keys=True) + "\n").encode()[:PROBE_RECORD_LIMIT]
        target = self.root / "route-probe.stderr"
        temporary = self.root / ".route-probe.stderr.tmp"
        temporary.unlink(missing_ok=True)
        _save_raw(temporary, raw)
        os.replace(temporary, target)
        gate._sync_directory(self.root)

    def _start(self, identifier, purpose):
        self.check(active=True)
        gate._write(self.root / (purpose + "-start.intent.json"), {"id": identifier, "operation_id": self.operation})
        args = ["start", "--attach", identifier] if purpose == "config-test" else ["start", identifier]
        self._command(args, purpose + "-start")

    def _poll(self, action, accepted):
        deadline = time.monotonic() + self.plan["wait_seconds"]
        for attempt in range(self.plan["max_polls"]):
            gate._require(time.monotonic() < deadline, "gateway observation deadline")
            value = action()
            gate._require(time.monotonic() < deadline, "gateway observation exceeded deadline")
            if accepted(value):
                return value
            if attempt + 1 < self.plan["max_polls"]:
                time.sleep(min(1, max(0, deadline - time.monotonic())))
        raise gate.AdmissionUncertain("gateway observation budget exhausted")

    def _stopped(self):
        row = self._old()
        return (
            not row["State"]["Running"] and row["State"]["Status"] == "exited" and row["State"]["ExitCode"] in {0, 143}
        )

    def _route(self, row):
        self._created(row, "gateway")
        gate._require(
            row["State"]["Running"]
            and row["State"]["Status"] == "running"
            and not row["State"].get("Restarting")
            and not row["State"].get("OOMKilled")
            and self._stopped()
            and self._api(gateway_id=row["Id"]) == self.intent["api"],
            "gateway or actual API changed",
        )
        attachment = row["NetworkSettings"]["Networks"][self.intent["api"]["network_name"]]
        gate._require(
            attachment["NetworkID"] == self.plan["network_id"]
            and attachment["EndpointID"]
            and ipaddress.ip_address(attachment["IPAddress"]).version == 4,
            "running gateway endpoint is not bound to the exact network",
        )
        proofs, observed = [], []
        expected = {
            "status": 200,
            "body_sha256": hashlib.sha256(b"OK").hexdigest(),
            "operation": self.operation,
            "upstream": self.intent["api"]["address"] + ":5000",
        }
        for port in self.intent["ports"]:
            self.transport.last_probe_body = None
            try:
                proof = self.transport.health(port["HostIp"], int(port["HostPort"]), self.operation)
            except Exception as error:
                observed.append({"binding": port, "error_type": type(error).__name__,
                                 "error": str(error)[:PROBE_TEXT_LIMIT]})
                self._record_probe(observed)
                raise
            row_observed = _probe_observation(port, proof, expected, getattr(self.transport, "last_probe_body", None))
            observed.append(row_observed)
            if proof != expected:
                self._record_probe(observed)
            gate._require(proof == expected, "actual gateway HTTP did not reach the exact granted API endpoint")
            proofs.append({"binding": port, **proof})
        self.check()
        after = self.transport.inspect(row["Id"])
        self._created(after, "gateway")
        gate._require(
            self._api(gateway_id=row["Id"]) == self.intent["api"]
            and after["State"] == row["State"]
            and after["NetworkSettings"] == row["NetworkSettings"],
            "endpoint changed during route verification",
        )
        return {
            "schema": 1,
            "operation_id": self.operation,
            "plan_sha256": self.plan_sha,
            "status": "gateway_switched",
            "gateway_id": row["Id"],
            "gateway_started_at": row["State"]["StartedAt"],
            "image_id": row["Image"],
            "api": self.intent["api"],
            "http": proofs,
            "old_gateway_retained": self.plan["old_gateway_id"],
            "old_config": self.plan["old_config"],
            "new_config_sha256": self.plan["new_config"]["sha256"],
            "durable": True,
            "receiver_opened_by_adapter": False,
            "sender_resumed_by_adapter": False,
            "automatic_rollback": False,
        }

    def switch(self):
        with self._exclusive():
            return self._switch()

    def _switch(self):
        self.check(active=True)
        gate._require(
            not self.session.opened and all(p.name == "control.lock" for p in self.root.iterdir()),
            "gateway attempted or receiver already open; query original only",
        )
        self.session.check_receiver_resume(active=True)
        old, image, api = self._old(), self.transport.image(), self._api()
        gate._require(
            old["Image"] == image["Id"]
            and self.project != old["Config"]["Labels"].get("com.docker.compose.project")
            and set(old["NetworkSettings"]["Networks"]) == {api["network_name"]}
            and old["NetworkSettings"]["Networks"][api["network_name"]]["NetworkID"] == self.plan["network_id"],
            "old gateway fixed image/network/project differs",
        )
        gate._require(
            self.transport.find(self.operation, "gateway") is None
            and self.transport.find(self.operation, "config-test") is None,
            "gateway operation already has containers",
        )
        self.intent = {
            "schema": 1,
            "plan": self.plan,
            "api": api,
            "image_id": image["Id"],
            "execution": _execution(old),
            "ports": _ports(old),
            "old_was_running": old["State"]["Running"],
        }
        if self.plan["old_config"].get("kind") == retained_config.KIND:
            original = retained_config.validate(self.plan["old_config"])["files"][CONFIG_TARGET]
            gate._write(self.root / "old-configuration-proof.json", self.plan["old_config"])
        else:
            original = file_identity(self.plan["old_config"]["path"])[1]
        _save_raw(self.root / "old-nginx.conf", original)
        _save_raw(self.root / "new-nginx.conf", self.config)
        gate._write(self.root / "intent.json", self.intent)
        test = self._create("config-test")
        self._start(test, "config-test")
        tested = self.transport.inspect(test)
        self._created(tested, "config-test")
        gate._require(
            not tested["State"]["Running"]
            and tested["State"]["Status"] == "exited"
            and tested["State"]["ExitCode"] == 0
            and not tested["State"].get("OOMKilled"),
            "actual nginx configuration test failed",
        )
        gate._write(
            self.root / "config-tested.json",
            {"id": test, "config_sha256": self.plan["new_config"]["sha256"], "exit_code": 0},
        )
        if old["State"]["Running"]:
            self.check(active=True)
            self._old()
            gate._write(self.root / "old-term.intent.json", {"id": self.plan["old_gateway_id"], "signal": "TERM"})
            self._command(["kill", "--signal", "TERM", self.plan["old_gateway_id"]], "old-term")
        self._poll(self._stopped, bool)
        gate._write(
            self.root / "old-stopped.json",
            {"id": self.plan["old_gateway_id"], "already_stopped": not self.intent["old_was_running"]},
        )
        identifier = self._create("gateway")
        self._start(identifier, "gateway")
        row = self._poll(lambda: self.transport.inspect(identifier), lambda value: value["State"]["Running"])
        result = self._route(row)
        gate._write(self.root / "result.json", result)
        return runtime.read_confirmed(self.root / "result.json")

    def query(self):
        with self._exclusive():
            return self._query()

    @contextmanager
    def retained_verification(self, receiver_session):
        """Hold real gateway custody throughout independent application verify."""
        gate._require(receiver_session is self.session, "same live gateway receiver capability required")
        with self._exclusive():
            result = self._query()
            gate._require(result["status"] == "gateway_switched", "actual gateway cutover is not confirmed")
            receipt = {key: value for key, value in result.items() if key != "query_only"}
            expected = gate._hash(receipt)
            # A caller may retain a snapshot, never the mutable internal proof.
            provided = gate._parsed(gate._encoded(receipt))
            try:
                yield provided
            finally:
                self.check()
                self._predecessors()
                after = self._route(self.transport.inspect(receipt["gateway_id"]))
                gate._require(gate._hash(provided) == expected and gate._hash(after) == expected,
                              "gateway custody changed during independent verification")

    def _query(self):
        self.check()
        self.intent = runtime.read_confirmed(self.root / "intent.json")
        attempts = [self.root / f"query-{i:02d}.json" for i in range(32)]
        unused = next(
            (p for p in attempts if not gate._present(p) and not gate._present(p.with_suffix(".json.pending"))), None
        )
        gate._require(unused is not None, "gateway query budget exhausted")
        gate._write(unused, {"operation_id": self.operation, "reserved": True})
        old = self._old()
        gate._require(
            set(self.intent) == {"schema", "plan", "api", "image_id", "execution", "ports", "old_was_running"}
            and self.intent["schema"] == 1
            and self.intent["plan"] == self.plan
            and type(self.intent["old_was_running"]) is bool
            and self.intent["image_id"] == old["Image"] == self.transport.image()["Id"]
            and self.intent["execution"] == _execution(old)
            and self.intent["ports"] == _ports(old),
            "query original gateway intent only",
        )
        if not gate._present(self.root / "gateway-start.intent.json"):
            # Even a recovered create/config-test/TERM does not authorize the
            # next not-yet-attempted mutation. Retain all partial objects.
            found = {}
            for purpose in ("config-test", "gateway"):
                row = self.transport.find(self.operation, purpose)
                if row is not None:
                    self._created(row, purpose)
                    found[purpose] = {"id": row["Id"], "status": row["State"]["Status"]}
            return {
                "status": "gateway_stage_unconfirmed",
                "operation_id": self.operation,
                "observed": found,
                "old_stopped": self._stopped(),
                "query_only": True,
                "mutation_replayed": False,
            }
        start = runtime.read_confirmed(self.root / "gateway-start.intent.json")
        self._predecessors()
        row = self.transport.inspect(start["id"])
        gate._require(start["operation_id"] == self.operation, "original gateway start differs")
        result = self._route(row)
        path = self.root / "result.json"
        if gate._present(path):
            gate._require(runtime.read_confirmed(path) == result, "original gateway outcome changed")
        else:
            gate._write(path, result)
        return {**runtime.read_confirmed(path), "query_only": True}

    def _predecessors(self):
        """A start receipt cannot stand in for all earlier attempted stages."""
        for purpose in ("config-test", "gateway"):
            args = self._argv(self.intent["execution"], self.intent["image_id"], self.intent["api"], purpose)
            gate._require(
                runtime.read_confirmed(self.root / (purpose + "-create.intent.json"))
                == {"operation_id": self.operation, "argv_sha256": gate._hash(args)},
                "create predecessor changed",
            )
            created = runtime.read_confirmed(self.root / (purpose + "-created.json"))
            started = runtime.read_confirmed(self.root / (purpose + "-start.intent.json"))
            gate._require(
                created == started
                and set(created) == {"id", "operation_id"}
                and created["operation_id"] == self.operation,
                "original create/start identity differs",
            )
            row = self.transport.inspect(created["id"])
            self._created(row, purpose)
            if purpose == "config-test":
                gate._require(
                    not row["State"]["Running"]
                    and row["State"]["Status"] == "exited"
                    and row["State"]["ExitCode"] == 0
                    and not row["State"].get("OOMKilled")
                    and runtime.read_confirmed(self.root / "config-tested.json")
                    == {"id": created["id"], "config_sha256": self.plan["new_config"]["sha256"], "exit_code": 0},
                    "actual original config test not confirmed",
                )
        gate._require(
            runtime.read_confirmed(self.root / "old-stopped.json")
            == {"id": self.plan["old_gateway_id"], "already_stopped": not self.intent["old_was_running"]},
            "old stopped predecessor changed",
        )
        if self.intent["old_was_running"]:
            gate._require(
                runtime.read_confirmed(self.root / "old-term.intent.json")
                == {"id": self.plan["old_gateway_id"], "signal": "TERM"},
                "original TERM predecessor changed",
            )
