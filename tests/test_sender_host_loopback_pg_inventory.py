"""Synthetic cursor/process proofs only; no database, Docker, network or model."""
from copy import deepcopy
import hashlib
import json
from types import SimpleNamespace

import pytest

from scripts import sender_drain_inventory as inventory
from scripts import sender_host_loopback_pg as local
from scripts.durable_handoff_cutover import CutoverError
from services import sender_drain_contract as contract
from test_sender_drain_contract import synthetic_manifest
from test_sender_host_loopback_pg import DSN, HOST


def capability():
    return {
        "schema": local.SCHEMA, "dsn_sha256": hashlib.sha256(DSN.encode()).hexdigest(), "host": deepcopy(HOST),
        "postgres": {"id": "a" * 64, "image_id": "sha256:" + "b" * 64, "started_at": "2026-10-31T00:00:00Z",
                     "restart_count": 0, "pid": 321, "start_ticks": 22, "uid": 999, "config_sha256": "c" * 64,
                     "mounts_sha256": "d" * 64, "network_name": "bridge_one", "network_id": "e" * 64,
                     "address": "172.19.0.2"},
    }


class Cursor:
    def __init__(self, connection):
        self.connection, self.last = connection, None

    def __enter__(self):
        return self

    def __exit__(self, *args):
        return False

    def execute(self, sql, params=None):
        self.last = sql
        self.connection.events.append(("sql", self.connection, self, sql, params))
        return self

    def fetchone(self):
        value = self.connection.case
        if self.last == local.SQL:
            keys = ("database", "schema", "system_identifier", "postmaster_started_at", "current_user", "session_user")
            return (value.address, 5432, *(value.expected[k] for k in keys))
        if self.last == inventory.IDENTITY_SQL:
            return tuple(value.expected[k] for k in inventory.IDENTITY_KEYS)
        if self.last == "SHOW transaction_read_only":
            return ("on",)
        if self.last.startswith("SELECT manifest,generation,phase,current_operation"):
            g = value.gate
            return (g["manifest"], g["generation"], g["phase"], g["current_operation"])
        if "confirmed_at IS NOT NULL" in self.last:
            g = value.gate
            return (g["manifest"], "pause", g["generation"] - 1, g["generation"], "confirmed", True)
        if self.last.startswith("SELECT manifest,action,"):
            r = value.manifest["control_receipt"]
            return tuple(r[k] for k in ("manifest", "action", "previous_generation", "generation")) + ("confirmed",)
        if "FROM handoff_leases" in self.last:
            return (2, 0, 0, 0, 0) if self.last.count("FILTER") == 4 else (2, 0, 0)
        if self.last == "SELECT count(*) FROM handoff_operations":
            return (3,)
        if self.last.startswith("SELECT pg_try_advisory_lock"):
            return (True,)
        if self.last == "SELECT pg_backend_pid()":
            return (777,)
        if self.last.startswith("SELECT pg_backend_pid(),count(*)"):
            return (777, 2)
        raise AssertionError("unexpected synthetic SQL result: " + self.last)

    def fetchall(self):
        assert self.last == inventory.capacity.PG_SAMPLE_SQL
        return []

    def __iter__(self):
        assert self.last == inventory.ROWS_SQL
        m, body = self.connection.case.manifest, self.connection.case.body
        row = m["entries"][0]
        yield (row["delivery_id"], row["body_sha256"], body, row["target_url"], row["signature"], row["contract"],
               row["user_id"], row["feed_id"], row["entry_id"], row["state"], row["attempts"])


class Connection:
    def __init__(self, case):
        self.case, self.events, self.closed = case, case.events, False
        self.autocommit = False

    def cursor(self):
        return Cursor(self)

    def close(self):
        self.closed = True
        self.events.append(("close", self))


@pytest.fixture
def fake(tmp_path, monkeypatch):
    manifest, body = synthetic_manifest(tmp_path)
    manifest["sender_binding"]["component"]["mounts"] = []
    manifest["sender_binding"]["component"]["host_loopback_postgres"] = contract.host_loopback_postgres_identity(DSN, True, "host")
    r = manifest["control_receipt"]
    value = SimpleNamespace(manifest=manifest, body=body, expected=manifest["postgres_identity"], address="172.19.0.2",
                            events=[], connections=[], checks=0, fail_at=None,
                            gate={"manifest": r["manifest"], "generation": r["generation"], "phase": "draining",
                                  "current_operation": r["operation_id"]})
    value.guard = local.HostLoopbackPostgres(capability())

    def check(dsn, *, deadline):
        value.checks += 1
        value.events.append(("guard", dsn, deadline))
        if value.checks == value.fail_at:
            raise CutoverError("synthetic lifecycle drift")

    def connect(dsn, **kwargs):
        assert dsn == DSN and 2 <= kwargs["connect_timeout"] <= 3
        connection = Connection(value)
        value.connections.append(connection)
        value.events.append(("connect", connection))
        return connection

    monkeypatch.setattr(value.guard, "check", check)
    monkeypatch.setattr(inventory.capacity, "_postgres_counts", lambda _rows: deepcopy(manifest["inventory_summary"]))
    value.connect = connect
    return value


def guarded_cursors(value):
    return [(event[1], event[2]) for event in value.events if event[0] == "sql" and event[3] == local.SQL]


def test_observe_checks_guard_before_and_after_and_actual_connection_once(fake):
    result = inventory.observe(fake.connect, DSN, fake.expected, transport=fake.guard)
    assert result["database_identity"] == fake.expected
    assert len(fake.connections) == 1 and fake.connections[0].closed
    assert fake.checks == 2 and fake.events[0][0] == "guard" and fake.events[1][0] == "connect"
    assert len(guarded_cursors(fake)) == 1 and guarded_cursors(fake)[0][0] is fake.connections[0]
    sql = [e[3] for e in fake.events if e[0] == "sql"]
    assert sql.index(local.SQL) < sql.index(inventory.IDENTITY_SQL) < sql.index(inventory.capacity.PG_SAMPLE_SQL)
    assert fake.events[-2][0] == "guard" and fake.events[-1][0] == "close"


@pytest.mark.parametrize("failure", [1, 2])
def test_observe_lifecycle_failure_is_not_retried_or_returned_as_success(fake, failure):
    fake.fail_at = failure
    with pytest.raises(CutoverError, match="lifecycle drift"):
        inventory.observe(fake.connect, DSN, fake.expected, transport=fake.guard)
    assert fake.checks == failure and len(fake.connections) == (0 if failure == 1 else 1)
    assert all(c.closed for c in fake.connections)


def test_wrong_server_on_open_connection_stops_before_business_queries(fake):
    fake.address = "172.19.0.3"
    with pytest.raises(CutoverError, match="not the sealed container"):
        inventory.observe(fake.connect, DSN, fake.expected, transport=fake.guard)
    assert len(fake.connections) == 1 and fake.connections[0].closed
    sql = [e[3] for e in fake.events if e[0] == "sql"]
    assert local.SQL in sql and inventory.IDENTITY_SQL not in sql and inventory.capacity.PG_SAMPLE_SQL not in sql


def test_freeze_bundle_seals_original_only_after_second_guard(fake, tmp_path):
    m, destination = fake.manifest, tmp_path / "frozen"
    result = inventory.freeze_bundle(fake.connect, DSN, fake.expected, m["sender_binding"], m["control_receipt"],
                                     m["window"], destination, transport=fake.guard)
    assert fake.checks == 2 and len(fake.connections) == 1 and fake.connections[0].closed
    assert guarded_cursors(fake)[0][0] is fake.connections[0]
    loaded, _digest = contract.load_bundle(destination)
    assert loaded == result and contract.read_original(destination, loaded["entries"][0]) == fake.body
    with pytest.raises(CutoverError):
        inventory.freeze_bundle(fake.connect, DSN, fake.expected, m["sender_binding"], m["control_receipt"],
                                 m["window"], destination, transport=fake.guard)
    assert fake.checks == 2 and len(fake.connections) == 1


def test_freeze_late_lifecycle_failure_retains_original_and_withholds_manifest(fake, tmp_path):
    m, destination = fake.manifest, tmp_path / "frozen-failed"
    fake.fail_at = 2
    with pytest.raises(CutoverError, match="lifecycle drift"):
        inventory.freeze_bundle(fake.connect, DSN, fake.expected, m["sender_binding"], m["control_receipt"],
                                 m["window"], destination, transport=fake.guard)
    assert (destination / m["entries"][0]["body_file"]).read_bytes() == fake.body
    assert not (destination / "manifest.json").exists()
    assert fake.connections[0].closed and fake.checks == 2


def test_host_loopback_bundle_without_capability_stops_before_connect(fake, tmp_path):
    m = fake.manifest
    with pytest.raises(CutoverError, match="actual transport capability"):
        inventory.freeze_bundle(fake.connect, DSN, fake.expected, m["sender_binding"], m["control_receipt"],
                                 m["window"], tmp_path / "no-capability")
    assert fake.connections == []


def closed_reader(fake):
    fake.gate.update(generation=4, phase="closed", current_operation="b" * 64)
    sample = deepcopy(fake.manifest["inventory_summary"])
    sample["states"].update(pending=0, acked=1)
    sample.update(gate=deepcopy(fake.gate), retained_leases={"active": 0, "uncertain": 0})
    return SimpleNamespace(expected=fake.expected, transport_guard=fake.guard, _dsn=lambda: DSN,
                           snapshot=lambda **kwargs: deepcopy(sample))


def test_frozen_locks_recheck_same_backend_connection_without_reconnect(fake, monkeypatch):
    import psycopg
    monkeypatch.setattr(psycopg, "connect", fake.connect)
    reader = closed_reader(fake)
    with inventory.FrozenPostgres(reader, "b" * 64, fake.gate["manifest"]) as held:
        assert held.lock_identity["backend_pid"] == 777
        assert held.check() == held.lock_identity
        assert len(fake.connections) == 1
        assert all(c is fake.connections[0] for c, _cursor in guarded_cursors(fake))
        assert len(guarded_cursors(fake)) == 3
    assert len(fake.connections) == 1 and fake.connections[0].closed
    assert len(guarded_cursors(fake)) == 4 and fake.checks == 4


def test_frozen_lifecycle_drift_closes_backend_without_reconnection(fake, monkeypatch):
    import psycopg
    monkeypatch.setattr(psycopg, "connect", fake.connect)
    fake.fail_at = 2
    reader = closed_reader(fake)
    with pytest.raises(CutoverError, match="lifecycle drift"):
        with inventory.FrozenPostgres(reader, "b" * 64, fake.gate["manifest"]):
            pytest.fail("changed PostgreSQL identity yielded a frozen context")
    assert len(fake.connections) == 1 and fake.connections[0].closed


def private_dsn(tmp_path):
    path = tmp_path / "dsn.json"
    path.write_text(json.dumps({"dsn": DSN}))
    path.chmod(0o600)
    return path


def test_reader_factory_propagates_exact_capability_and_credential_hash(fake, tmp_path):
    plan = {"binding": fake.manifest["sender_binding"], "postgres_identity": fake.expected, "postgres_transport": capability()}
    reader = inventory.PostgresReader.from_plan(private_dsn(tmp_path), plan)
    assert type(reader.transport_guard) is local.HostLoopbackPostgres
    assert reader.transport_binding == plan["postgres_transport"]
    assert reader.transport_binding is not plan["postgres_transport"]
    assert reader._dsn() == DSN


def test_reader_factory_keeps_omitted_default_port_raw_dsn_and_static_binding(fake, tmp_path):
    dsn = DSN.replace(":5432", "")
    assert dsn != DSN
    plan = {"binding": deepcopy(fake.manifest["sender_binding"]), "postgres_identity": fake.expected,
            "postgres_transport": capability()}
    static = contract.host_loopback_postgres_identity(dsn, True, "host")
    plan["binding"]["component"]["host_loopback_postgres"] = static
    plan["postgres_transport"]["dsn_sha256"] = static["dsn_sha256"]
    path = private_dsn(tmp_path)
    path.write_text(json.dumps({"dsn": dsn}))
    reader = inventory.PostgresReader.from_plan(path, plan)
    assert reader._dsn() == dsn and reader.transport_binding["dsn_sha256"] == hashlib.sha256(dsn.encode()).hexdigest()
    assert reader.transport_binding["dsn_sha256"] != hashlib.sha256(DSN.encode()).hexdigest()


def test_reader_factory_rejects_static_dsn_mismatch_before_reader_construction(fake, tmp_path, monkeypatch):
    plan = {"binding": deepcopy(fake.manifest["sender_binding"]), "postgres_identity": fake.expected,
            "postgres_transport": capability()}
    plan["binding"]["component"]["host_loopback_postgres"]["dsn_sha256"] = "f" * 64

    def forbidden(*args, **kwargs):
        pytest.fail("static sender/transport mismatch reached credential or reader initialization")

    monkeypatch.setattr(inventory.PostgresReader, "__init__", forbidden)
    with pytest.raises(CutoverError, match="DSN identities differ"):
        inventory.PostgresReader.from_plan(tmp_path / "nonexistent-credential", plan)


@pytest.mark.parametrize("change", ["missing", "unexpected", "dsn"])
def test_reader_factory_rejects_missing_unexpected_or_wrong_capability(fake, tmp_path, change):
    plan = {"binding": deepcopy(fake.manifest["sender_binding"]), "postgres_identity": fake.expected,
            "postgres_transport": capability()}
    if change == "missing":
        del plan["postgres_transport"]
    elif change == "unexpected":
        del plan["binding"]["component"]["host_loopback_postgres"]
    else:
        plan["postgres_transport"]["dsn_sha256"] = "f" * 64
    with pytest.raises(CutoverError):
        inventory.PostgresReader.from_plan(private_dsn(tmp_path), plan)


@pytest.mark.parametrize("method", ["snapshot", "initial_snapshot", "freeze"])
def test_reader_passes_its_guard_to_every_inventory_operation(fake, tmp_path, monkeypatch, method):
    plan = {"binding": fake.manifest["sender_binding"], "postgres_identity": {**fake.expected, "schema_version": 135},
            "postgres_transport": capability()}
    reader = inventory.PostgresReader.from_plan(private_dsn(tmp_path), plan)
    calls = []

    def operation(*args, **kwargs):
        calls.append((args, kwargs))
        return "synthetic-result"

    monkeypatch.setattr(inventory, "observe" if method != "freeze" else "freeze_bundle", operation)
    result = getattr(reader, method)(*(fake.manifest[k] for k in ("sender_binding", "control_receipt", "window")), tmp_path / "bundle") if method == "freeze" else getattr(reader, method)()
    assert result == "synthetic-result" and len(calls) == 1
    assert calls[0][0][1] == DSN and calls[0][1]["transport"] is reader.transport_guard
    assert calls[0][1].get("allow_uninitialized", False) is (method == "initial_snapshot")


@pytest.mark.parametrize("kind", ["symlink", "hardlink", "directory", "overlimit", "inode_changed"])
def test_process_metadata_reader_rejects_unsafe_or_changed_file(tmp_path, monkeypatch, kind):
    path = tmp_path / "metadata"
    path.write_bytes(b"123")
    if kind == "symlink":
        link = tmp_path / "link"
        link.symlink_to(path)
        target = link
    elif kind == "hardlink":
        (tmp_path / "link").hardlink_to(path)
        target = path
    elif kind == "directory":
        target = tmp_path
    else:
        target = path
    if kind == "inode_changed":
        original, count = local.os.lstat, [0]

        def lstat(name):
            value = original(name)
            if str(name) == str(path):
                count[0] += 1
                if count[0] == 2:
                    fields = {k: getattr(value, k) for k in ("st_dev", "st_ino", "st_uid", "st_gid", "st_mode", "st_nlink")}
                    fields["st_ino"] += 1
                    return SimpleNamespace(**fields)
            return value

        monkeypatch.setattr(local.os, "lstat", lstat)
    with pytest.raises((CutoverError, OSError)):
        local._small(target, 2 if kind == "overlimit" else 8, root_owned=False)
    assert path.read_bytes() == b"123"


def test_process_metadata_reader_returns_only_exact_bounded_bytes(tmp_path):
    path = tmp_path / "metadata"
    path.write_bytes(b"123")
    assert local._small(path, 3, root_owned=False) == b"123"


@pytest.mark.parametrize("change", [None, "pid", "short", "ticks", "directory"])
def test_process_identity_uses_stat_start_ticks_and_rejects_changed_directory(monkeypatch, change):
    calls = []

    def stat(_path):
        calls.append("stat")
        return SimpleNamespace(st_ino=18 + int(change == "directory" and len(calls) > 1), st_uid=999)

    values = ["S", *(["0"] * 18), "777"]
    if change == "short":
        values = values[:4]
    elif change == "ticks":
        values[-1] = "unknown"
    raw = (str(322 if change == "pid" else 321) + " (postgres helper) " + " ".join(values)).encode()
    monkeypatch.setattr(local, "os", SimpleNamespace(stat=stat))
    monkeypatch.setattr(local, "_small", lambda *args, **kwargs: raw)
    if change is None:
        assert local._process(321) == {"pid": 321, "start_ticks": 777, "uid": 999}
        assert calls == ["stat", "stat"]
    else:
        with pytest.raises(CutoverError):
            local._process(321)


@pytest.mark.parametrize("pid", [True, False, 0, 1, -1, 2**31, "321", None])
def test_process_identity_rejects_unknown_or_noncanonical_pid_before_proc(monkeypatch, pid):
    def forbidden(*args, **kwargs):
        raise AssertionError("invalid PID must not read proc")

    monkeypatch.setattr(local, "os", SimpleNamespace(stat=forbidden))
    monkeypatch.setattr(local, "_small", forbidden)
    with pytest.raises(CutoverError):
        local._process(pid)


@pytest.mark.parametrize("change", [None, "init_namespace", "daemon_namespace", "daemon_uid", "executable", "process_changed", "pid_changed"])
def test_host_proof_requires_same_init_daemon_namespace_and_lifecycle(monkeypatch, change):
    process_calls, pid_calls = [], []

    def small(path, _maximum):
        if path.endswith("boot_id"):
            return HOST["boot_id"].encode() + b"\n"
        pid_calls.append(path)
        return b"124\n" if change == "pid_changed" and len(pid_calls) > 1 else b"123\n"

    def process(pid):
        process_calls.append(pid)
        return {"pid": pid, "start_ticks": 45 + int(change == "process_changed" and len(process_calls) > 1),
                "uid": 999 if change == "daemon_uid" else 0}

    def stat(path):
        inode = HOST["network_inode"]
        if change == "init_namespace" and path == "/proc/1/ns/net":
            inode += 1
        if change == "daemon_namespace" and path == "/proc/123/ns/net":
            inode += 1
        return SimpleNamespace(st_dev=HOST["network_device"], st_ino=inode)

    os = SimpleNamespace(geteuid=lambda: 0, uname=lambda: SimpleNamespace(sysname="Linux"), stat=stat,
                         readlink=lambda _path: "/usr/bin/other" if change == "executable" else "/usr/bin/dockerd")
    monkeypatch.setattr(local, "os", os)
    monkeypatch.setattr(local, "_small", small)
    monkeypatch.setattr(local, "_process", process)
    if change is None:
        assert local._host() == HOST and process_calls == [123, 123]
    else:
        with pytest.raises(CutoverError):
            local._host()


def sender_row():
    return {"Mounts": [], "HostConfig": {"NetworkMode": "host", "ReadonlyRootfs": True, "PortBindings": {}},
            "State": {"Running": True, "Pid": 321}}


def sender_process_stubs(monkeypatch, *, namespace=HOST["network_inode"], drift=False):
    calls, closed = [], []

    def process(pid):
        calls.append(pid)
        return {"pid": pid, "start_ticks": 22 + int(drift and len(calls) > 1), "uid": 999}

    import os
    synthetic_os = SimpleNamespace(open=lambda *args: 9, fstat=lambda _fd: SimpleNamespace(st_dev=HOST["network_device"], st_ino=namespace),
                                   close=closed.append, O_RDONLY=os.O_RDONLY, O_CLOEXEC=os.O_CLOEXEC)
    monkeypatch.setattr(local, "os", synthetic_os)
    monkeypatch.setattr(local, "_host", lambda: deepcopy(HOST))
    monkeypatch.setattr(local, "_process", process)
    return calls, closed


@pytest.mark.parametrize("change", [None, "namespace", "process", "created", "bridge"])
def test_actual_sender_proof_checks_process_twice_and_closes_namespace_fd(monkeypatch, change):
    calls, closed = sender_process_stubs(monkeypatch, namespace=HOST["network_inode"] + int(change == "namespace"), drift=change == "process")
    row = sender_row()
    if change == "created":
        row["State"]["Running"] = False
    elif change == "bridge":
        row["HostConfig"]["NetworkMode"] = "bridge"
    guard = local.HostLoopbackPostgres(capability())
    if change is None:
        guard.check_sender(row)
        assert calls == [321, 321] and closed == [9]
    else:
        with pytest.raises(CutoverError):
            guard.check_sender(row)
        assert closed == ([] if change in {"created", "bridge"} else [9])


@pytest.mark.parametrize("entrypoint", ["check_sender", "check_connection"])
def test_all_guard_entrypoints_reject_mutated_sealed_capability(monkeypatch, entrypoint):
    sender_process_stubs(monkeypatch)
    guard = local.HostLoopbackPostgres(capability())
    guard.binding["postgres"]["config_sha256"] = "f" * 64
    with pytest.raises(CutoverError, match="mutated"):
        if entrypoint == "check_sender":
            guard.check_sender(sender_row())
        else:
            guard.check_connection(SimpleNamespace(execute=lambda _sql: None, fetchone=lambda: None), {})
