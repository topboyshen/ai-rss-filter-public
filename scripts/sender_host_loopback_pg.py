"""Exact host-published PostgreSQL capability, separate from external hosts.

No DNS, network changes, migrations, retries or sender control requests. A
sealed observation is not permission: each connection rechecks the host and
the fixed PostgreSQL container, and verifies the server on that connection.
"""

from __future__ import annotations

from contextlib import contextmanager
from contextvars import ContextVar
import hashlib
import os
from pathlib import Path
import re
import stat
import time

from scripts.durable_handoff_cutover import encoded, parsed, require
from services.sender_drain_contract import host_loopback_postgres_identity

SCHEMA = "host-loopback-postgres-v1"
HEX64 = re.compile(r"[0-9a-f]{64}\Z")
KEYS = {"schema", "dsn_sha256", "host", "postgres"}
HOST_KEYS = {"boot_id", "network_device", "network_inode"}
PG_KEYS = {
    "id",
    "image_id",
    "started_at",
    "restart_count",
    "pid",
    "start_ticks",
    "uid",
    "config_sha256",
    "mounts_sha256",
    "network_name",
    "network_id",
    "address",
}
SQL = """SELECT inet_server_addr()::text,inet_server_port(),current_database(),
 current_schema(),(SELECT system_identifier::text FROM pg_control_system()),
 pg_postmaster_start_time()::text,current_user,session_user"""
_NATIVE_HOST = ContextVar("synthetic_exact_private_daemon_host", default=None)


@contextmanager
def native_private_host(context):
    """Only a live, source-bound isolated native daemon; no receipt or flag.

    The ordinary production host checks below remain the default. The native
    wrapper validates its actual namespaces, peer, plan, routes and lifetime
    on entry, on every host observation, and again before leaving this scope.
    """
    from scripts.production_sender_bootstrap_host_native import native_host_identity
    require(_NATIVE_HOST.get() is None, "nested native host authority refused")
    native_host_identity(context)
    token = _NATIVE_HOST.set(context)
    try:
        yield
        from scripts import production_native_deadline as native_budget
        with native_budget.final_phase():
            native_host_identity(context)
    finally:
        _NATIVE_HOST.reset(token)


def source_sha256():
    return hashlib.sha256(Path(__file__).read_bytes()).hexdigest()


def _environment():
    # The literal URI and its original hash stay unchanged. In particular, an
    # omitted default port must not inherit PGPORT/PGHOSTADDR/PGSERVICE values.
    require(
        not any(key.startswith("PG") for key in os.environ),
        "PostgreSQL environment overrides are not part of this capability",
    )


def validate(value):
    require(
        isinstance(value, dict)
        and set(value) == KEYS
        and value["schema"] == SCHEMA
        and isinstance(value["dsn_sha256"], str)
        and HEX64.fullmatch(value["dsn_sha256"]),
        "sealed host-loopback PostgreSQL capability required",
    )
    host, pg = value["host"], value["postgres"]
    require(
        isinstance(host, dict)
        and set(host) == HOST_KEYS
        and isinstance(host["boot_id"], str)
        and re.fullmatch(
            r"[0-9a-f]{8}(?:-[0-9a-f]{4}){3}-[0-9a-f]{12}", host["boot_id"]
        )
        and all(
            type(host[k]) is int and host[k] > 0
            for k in ("network_device", "network_inode")
        ),
        "exact host boot and network namespace required",
    )
    require(
        isinstance(pg, dict)
        and set(pg) == PG_KEYS
        and all(
            isinstance(pg[k], str) and HEX64.fullmatch(pg[k])
            for k in ("id", "config_sha256", "mounts_sha256", "network_id")
        )
        and isinstance(pg["image_id"], str)
        and pg["image_id"].startswith("sha256:")
        and HEX64.fullmatch(pg["image_id"][7:])
        and isinstance(pg["started_at"], str)
        and 0 < len(pg["started_at"]) <= 64
        and not pg["started_at"].startswith("0001-")
        and type(pg["restart_count"]) is int
        and pg["restart_count"] >= 0
        and all(type(pg[k]) is int and pg[k] > 1 for k in ("pid", "start_ticks"))
        and type(pg["uid"]) is int
        and pg["uid"] >= 0
        and isinstance(pg["network_name"], str)
        and re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,127}", pg["network_name"]),
        "exact PostgreSQL container lifecycle required",
    )
    import ipaddress

    address = ipaddress.ip_address(pg["address"])
    require(
        address.version == 4
        and not address.is_loopback
        and not address.is_unspecified
        and not address.is_multicast
        and str(address) == pg["address"],
        "literal PostgreSQL bridge address required",
    )
    return value


def _small(path, maximum, *, root_owned=True):
    """Ordinary root-owned proc file, bounded and opened without symlink following."""
    before = os.lstat(path)
    require(
        stat.S_ISREG(before.st_mode)
        and (not root_owned or before.st_uid == 0)
        and before.st_nlink == 1,
        "unsafe host process metadata",
    )
    fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK | os.O_CLOEXEC)
    try:
        opened = os.fstat(fd)
        fields = ("st_dev", "st_ino", "st_uid", "st_gid", "st_mode", "st_nlink")
        require(
            all(getattr(before, k) == getattr(opened, k) for k in fields),
            "host metadata open changed",
        )
        raw = bytearray()
        while len(raw) <= maximum:
            block = os.read(fd, min(4096, maximum + 1 - len(raw)))
            if not block:
                break
            raw.extend(block)
        require(len(raw) <= maximum, "host process metadata bound")
        for after in (os.fstat(fd), os.lstat(path)):
            require(
                all(getattr(before, k) == getattr(after, k) for k in fields),
                "host metadata identity changed",
            )
        return bytes(raw)
    finally:
        os.close(fd)


def _process(pid):
    require(
        type(pid) is int and 1 < pid < 2**31, "exact live PostgreSQL process required"
    )
    root = os.stat(f"/proc/{pid}")
    raw = _small(f"/proc/{pid}/stat", 8192, root_owned=False)
    end = raw.rfind(b") ")
    require(
        raw.startswith(str(pid).encode() + b" (") and end > 0,
        "invalid process identity",
    )
    fields = raw[end + 2 :].split()
    require(
        len(fields) > 19 and fields[19].isdigit() and int(fields[19]) > 1,
        "missing PostgreSQL process start",
    )
    require(os.stat(f"/proc/{pid}").st_ino == root.st_ino, "process directory changed")
    return {"pid": pid, "start_ticks": int(fields[19]), "uid": root.st_uid}


def _host():
    context = _NATIVE_HOST.get()
    if context is not None:
        from scripts.production_sender_bootstrap_host_native import native_host_identity
        return native_host_identity(context)
    require(
        os.geteuid() == 0 and os.uname().sysname == "Linux",
        "native root host proof required",
    )
    boot = _small("/proc/sys/kernel/random/boot_id", 64).decode("ascii").strip()
    own = os.stat("/proc/self/ns/net")
    init = os.stat("/proc/1/ns/net")
    require(
        (own.st_dev, own.st_ino) == (init.st_dev, init.st_ino),
        "collector is not in host network namespace",
    )
    daemon = _small("/run/docker.pid", 32)
    require(
        re.fullmatch(rb"[1-9][0-9]{0,9}\n?", daemon) is not None,
        "fixed host Docker PID required",
    )
    pid = int(daemon)
    before = _process(pid)
    require(
        before["uid"] == 0 and os.readlink(f"/proc/{pid}/exe") == "/usr/bin/dockerd",
        "host daemon process identity differs",
    )
    net = os.stat(f"/proc/{pid}/ns/net")
    require(
        (net.st_dev, net.st_ino) == (own.st_dev, own.st_ino)
        and _process(pid) == before
        and _small("/run/docker.pid", 32) == daemon,
        "collector is not in the fixed daemon host namespace",
    )
    return {"boot_id": boot, "network_device": own.st_dev, "network_inode": own.st_ino}


def _inspect(identifier, deadline):
    from scripts.sender_namespace_http import _capture

    require(
        isinstance(identifier, str) and HEX64.fullmatch(identifier),
        "fixed PostgreSQL container required",
    )
    raw = _capture(
        [
            "/usr/bin/docker",
            "--host",
            "unix:///var/run/docker.sock",
            "inspect",
            identifier,
        ],
        deadline=deadline,
        max_bytes=1024**2,
    )
    rows = parsed(raw)
    require(
        isinstance(rows, list) and len(rows) == 1 and rows[0]["Id"] == identifier,
        "PostgreSQL container identity differs",
    )
    return rows[0]


def postgres_identity(row):
    state, config, host = row["State"], row["Config"], row["HostConfig"]
    require(
        state["Running"] is True
        and not any(state.get(k) for k in ("Paused", "Restarting", "Dead", "OOMKilled"))
        and state.get("Health", {}).get("Status") == "healthy",
        "PostgreSQL is not healthy and stable",
    )
    ports = {"5432/tcp": [{"HostIp": "127.0.0.1", "HostPort": "5432"}]}
    require(
        host.get("PortBindings") == ports
        and row["NetworkSettings"].get("Ports") == ports,
        "PostgreSQL must have one exact host-loopback published endpoint",
    )
    networks = row["NetworkSettings"]["Networks"]
    require(
        isinstance(networks, dict)
        and len(networks) == 1
        and host.get("NetworkMode") not in ("host", "none", "default")
        and not host.get("NetworkMode", "").startswith("container:"),
        "PostgreSQL must retain its independent bridge",
    )
    name, network = next(iter(networks.items()))
    process = _process(state["Pid"])
    return {
        "id": row["Id"],
        "image_id": row["Image"],
        "started_at": state["StartedAt"],
        "restart_count": row["RestartCount"],
        **process,
        "config_sha256": hashlib.sha256(encoded(config)).hexdigest(),
        "mounts_sha256": hashlib.sha256(encoded(row["Mounts"])).hexdigest(),
        "network_name": name,
        "network_id": network["NetworkID"],
        "address": network["IPAddress"],
    }


def observe(dsn, postgres_id):
    """Metadata observation only. Caller seals and reviews it before any use."""
    _environment()
    static = host_loopback_postgres_identity(dsn, True, "host")
    deadline = time.monotonic() + 3
    host = _host()
    first = postgres_identity(_inspect(postgres_id, deadline))
    require(
        postgres_identity(_inspect(postgres_id, deadline)) == first
        and _host() == host
        and time.monotonic() < deadline,
        "PostgreSQL lifecycle changed during observation",
    )
    return validate(
        {
            "schema": SCHEMA,
            "dsn_sha256": static["dsn_sha256"],
            "host": host,
            "postgres": first,
        }
    )


class HostLoopbackPostgres:
    """Not a callback or a success receipt: every operation performs real checks."""

    def __init__(self, binding):
        self.binding = parsed(encoded(validate(binding)))
        self.binding_sha256 = hashlib.sha256(encoded(self.binding)).hexdigest()

    def _sealed(self):
        require(
            hashlib.sha256(encoded(validate(self.binding))).hexdigest()
            == self.binding_sha256,
            "sealed PostgreSQL transport mutated",
        )

    def check(self, dsn, *, deadline):
        self._sealed()
        _environment()
        require(
            host_loopback_postgres_identity(dsn, True, "host")["dsn_sha256"]
            == self.binding["dsn_sha256"],
            "host-loopback PostgreSQL credential changed",
        )
        require(
            _host() == self.binding["host"], "host boot or network namespace changed"
        )
        actual = postgres_identity(_inspect(self.binding["postgres"]["id"], deadline))
        require(
            actual == self.binding["postgres"]
            and _host() == self.binding["host"]
            and time.monotonic() < deadline,
            "PostgreSQL container or published endpoint changed",
        )

    def check_sender(self, row):
        self._sealed()
        require(
            row["Mounts"] == []
            and row["HostConfig"].get("NetworkMode") == "host"
            and row["HostConfig"].get("ReadonlyRootfs") is True
            and not row["HostConfig"].get("PortBindings"),
            "sender host-loopback topology differs",
        )
        require(_host() == self.binding["host"], "sender host namespace changed")
        state = row["State"]
        require(
            state["Running"] is True
            and not any(state.get(k) for k in ("Paused", "Restarting", "Dead")),
            "sender host namespace requires a running process",
        )
        first = _process(state["Pid"])
        fd = os.open(f"/proc/{state['Pid']}/ns/net", os.O_RDONLY | os.O_CLOEXEC)
        try:
            net = os.fstat(fd)
            host = self.binding["host"]
            require(
                (net.st_dev, net.st_ino)
                == (host["network_device"], host["network_inode"])
                and _process(state["Pid"]) == first
                and _host() == host,
                "sender is not the bound host network process",
            )
        finally:
            os.close(fd)

    def check_connection(self, cursor, expected):
        """Verify the actual already-open connection, never a replacement probe."""
        self._sealed()
        cursor.execute(SQL)
        row = cursor.fetchone()
        keys = (
            "database",
            "schema",
            "system_identifier",
            "postmaster_started_at",
            "current_user",
            "session_user",
        )
        require(
            isinstance(row, tuple)
            and len(row) == 8
            and row[:2] == (self.binding["postgres"]["address"], 5432)
            and dict(zip(keys, row[2:])) == {key: expected[key] for key in keys},
            "actual PostgreSQL connection is not the sealed container instance",
        )
